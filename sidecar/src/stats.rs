//! Per-call stats: the voice model's token use (from Gemini Live `usageMetadata`), an estimated
//! cost, and one JSON line per finished call in `call_stats.jsonl`. Holds no transcript, audio,
//! reason text or secrets.

use std::io::Write;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// Tokens the voice model reported, summed over every `usageMetadata` message of a call.
/// Gemini Live sends one per model response, and each counts the whole context it read, which is
/// what is billed, so a plain sum is the call's billed total.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Usage {
    pub prompt_tokens: u64,
    pub response_tokens: u64,
    pub total_tokens: u64,
    /// Part of `prompt_tokens` served from cache (included in `prompt_tokens`).
    pub cached_tokens: u64,
    pub tool_use_prompt_tokens: u64,
    pub thoughts_tokens: u64,
    /// Per-modality split of `prompt_tokens` / `response_tokens`, when the server sends it.
    pub prompt_audio_tokens: u64,
    pub prompt_text_tokens: u64,
    pub response_audio_tokens: u64,
    pub response_text_tokens: u64,
    /// How many `usageMetadata` messages were summed; 0 means the model reported nothing.
    pub reports: u32,
}

/// Sums a `ModalityTokenCount[]` list by modality: (audio, text). Other modalities are left out
/// and count as "unknown" in the cost estimate.
fn modalities(details: &Value) -> (u64, u64) {
    let (mut audio, mut text) = (0, 0);
    for d in details.as_array().into_iter().flatten() {
        let n = d["tokenCount"].as_u64().unwrap_or(0);
        match d["modality"].as_str() {
            Some("AUDIO") => audio += n,
            Some("TEXT") => text += n,
            _ => {}
        }
    }
    (audio, text)
}

impl Usage {
    /// Reads one Live API `usageMetadata` object.
    pub fn from_metadata(u: &Value) -> Self {
        let n = |k: &str| u[k].as_u64().unwrap_or(0);
        let (prompt_audio_tokens, prompt_text_tokens) = modalities(&u["promptTokensDetails"]);
        let (response_audio_tokens, response_text_tokens) = modalities(&u["responseTokensDetails"]);
        let (prompt, response) = (n("promptTokenCount"), n("responseTokenCount"));
        Self {
            prompt_tokens: prompt,
            response_tokens: response,
            total_tokens: match n("totalTokenCount") {
                0 => prompt + response + n("toolUsePromptTokenCount") + n("thoughtsTokenCount"),
                t => t,
            },
            cached_tokens: n("cachedContentTokenCount"),
            tool_use_prompt_tokens: n("toolUsePromptTokenCount"),
            thoughts_tokens: n("thoughtsTokenCount"),
            prompt_audio_tokens,
            prompt_text_tokens,
            response_audio_tokens,
            response_text_tokens,
            reports: 1,
        }
    }

    pub fn add(&mut self, o: &Usage) {
        self.prompt_tokens += o.prompt_tokens;
        self.response_tokens += o.response_tokens;
        self.total_tokens += o.total_tokens;
        self.cached_tokens += o.cached_tokens;
        self.tool_use_prompt_tokens += o.tool_use_prompt_tokens;
        self.thoughts_tokens += o.thoughts_tokens;
        self.prompt_audio_tokens += o.prompt_audio_tokens;
        self.prompt_text_tokens += o.prompt_text_tokens;
        self.response_audio_tokens += o.response_audio_tokens;
        self.response_text_tokens += o.response_text_tokens;
        self.reports += o.reports;
    }
}

/// USD prices for the cost estimate. Defaults are Google's published paid-tier prices for
/// gemini-3.8-live (https://ai.google.dev/gemini-api/docs/pricing): audio $3 in / $12 out per
/// million tokens (~$0.005 / ~$0.018 a minute at 25 audio tokens a second), text $0.75 in /
/// $4.50 out (output includes thinking tokens).
/// Set any subset through the `pricing` plugin setting (`HWV_PRICING`, a JSON object).
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct Pricing {
    pub audio_input_per_mtok: f64,
    pub audio_output_per_mtok: f64,
    pub text_input_per_mtok: f64,
    pub text_output_per_mtok: f64,
    /// Fallback when the model reported no tokens: billed per answered call minute.
    pub audio_input_per_min: f64,
    pub audio_output_per_min: f64,
}

impl Default for Pricing {
    fn default() -> Self {
        Self {
            audio_input_per_mtok: 3.0,
            audio_output_per_mtok: 12.0,
            text_input_per_mtok: 0.75,
            text_output_per_mtok: 4.5,
            audio_input_per_min: 0.005,
            audio_output_per_min: 0.018,
        }
    }
}

impl Pricing {
    pub fn from_json(raw: &str) -> anyhow::Result<Self> {
        let p: Self = serde_json::from_str(if raw.trim().is_empty() { "{}" } else { raw })
            .map_err(|e| anyhow::anyhow!("pricing is not a valid price object: {e}"))?;
        let all = [
            p.audio_input_per_mtok,
            p.audio_output_per_mtok,
            p.text_input_per_mtok,
            p.text_output_per_mtok,
            p.audio_input_per_min,
            p.audio_output_per_min,
        ];
        if all.iter().any(|v| !v.is_finite() || *v < 0.0) {
            anyhow::bail!("pricing values must be zero or more");
        }
        Ok(p)
    }
}

/// An estimated cost; never a bill.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Cost {
    pub usd: f64,
    pub input_usd: f64,
    pub output_usd: f64,
    /// "tokens" (from usageMetadata), "minutes" (no token data: per-minute fallback) or "none".
    pub basis: &'static str,
    pub estimate: bool,
}

fn round6(v: f64) -> f64 {
    (v * 1e6).round() / 1e6
}

/// Prices a call. Tokens whose modality the server did not report are priced as audio (the
/// dearer rate), and cached tokens at the full input rate, so the estimate errs high.
pub fn estimate(usage: &Usage, talk_seconds: u64, p: &Pricing) -> Cost {
    let (input, output, basis) = if usage.reports > 0 && usage.total_tokens > 0 {
        let m = |tokens: u64, per_mtok: f64| tokens as f64 * per_mtok / 1e6;
        let prompt_known = usage.prompt_audio_tokens + usage.prompt_text_tokens;
        let prompt_audio =
            usage.prompt_audio_tokens + usage.prompt_tokens.saturating_sub(prompt_known);
        let response_known = usage.response_audio_tokens + usage.response_text_tokens;
        let response_audio =
            usage.response_audio_tokens + usage.response_tokens.saturating_sub(response_known);
        let input = m(prompt_audio, p.audio_input_per_mtok)
            + m(
                usage.prompt_text_tokens + usage.tool_use_prompt_tokens,
                p.text_input_per_mtok,
            );
        let output = m(response_audio, p.audio_output_per_mtok)
            + m(
                usage.response_text_tokens + usage.thoughts_tokens,
                p.text_output_per_mtok,
            );
        (input, output, "tokens")
    } else if talk_seconds > 0 {
        let minutes = talk_seconds as f64 / 60.0;
        (
            minutes * p.audio_input_per_min,
            minutes * p.audio_output_per_min,
            "minutes",
        )
    } else {
        (0.0, 0.0, "none")
    };
    Cost {
        usd: round6(input + output),
        input_usd: round6(input),
        output_usd: round6(output),
        basis,
        estimate: true,
    }
}

/// "41.2k tokens"-style count.
pub fn short_tokens(n: u64) -> String {
    match n {
        0..1_000 => n.to_string(),
        1_000..1_000_000 => format!("{:.1}k", n as f64 / 1e3),
        _ => format!("{:.2}M", n as f64 / 1e6),
    }
}

/// "~$0.046 est." / "~$0.12 est."
pub fn short_usd(usd: f64) -> String {
    if usd < 0.1 {
        format!("~${usd:.3} est.")
    } else {
        format!("~${usd:.2} est.")
    }
}

/// What the post-call message shows next to the duration: "3 min · 41.2k tokens · ~$0.074 est."
pub fn summary_note(minutes: u64, usage: &Usage, cost: &Cost) -> String {
    let mut parts = vec![format!("{minutes} min")];
    if usage.total_tokens > 0 {
        parts.push(format!("{} tokens", short_tokens(usage.total_tokens)));
    }
    if cost.basis != "none" {
        parts.push(short_usd(cost.usd));
    }
    parts.join(" · ")
}

/// Everything one stats line needs, from the finished call record.
pub struct CallStats<'a> {
    pub id: &'a str,
    pub direction: &'a str,
    pub status: Value,
    pub started_at: u64,
    pub answered_at: Option<u64>,
    pub ended_at: Option<u64>,
    pub ended_by: &'a str,
    /// "core_session" or "separate": how this call's voice actually ran.
    pub voice_mode: &'a str,
    pub tool_calls: u32,
    pub ask_hermes_calls: u32,
    pub usage: &'a Usage,
}

impl CallStats<'_> {
    /// Seconds from answer to hang-up: the time the voice model was on the line.
    pub fn talk_seconds(&self) -> u64 {
        self.answered_at
            .zip(self.ended_at)
            .map(|(a, e)| e.saturating_sub(a))
            .unwrap_or(0)
    }
}

/// Appends one JSON line per finished call to `<data dir>/call_stats.jsonl`.
#[derive(Clone, Debug)]
pub struct StatsLog {
    pub path: PathBuf,
    pub model: String,
    pub pricing: Pricing,
}

impl StatsLog {
    pub fn line(&self, s: &CallStats) -> Value {
        let talk = s.talk_seconds();
        json!({
            "call_id": s.id,
            "direction": s.direction,
            "status": s.status,
            "started_at": s.started_at,
            "answered_at": s.answered_at,
            "ended_at": s.ended_at,
            "duration_seconds": talk,
            "ended_by": s.ended_by,
            "model": self.model,
            "voice_mode": s.voice_mode,
            "tool_calls": s.tool_calls,
            "ask_hermes_calls": s.ask_hermes_calls,
            "usage": s.usage,
            "cost_estimate": estimate(s.usage, talk, &self.pricing),
            "pricing": self.pricing,
        })
    }

    pub fn append(&self, s: &CallStats) -> std::io::Result<()> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut opts = std::fs::OpenOptions::new();
        opts.create(true).append(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut opts, 0o600);
        let mut line = self.line(s).to_string();
        line.push('\n');
        // One write per line keeps concurrent appends whole.
        opts.open(&self.path)?.write_all(line.as_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn turn(prompt_audio: u64, prompt_text: u64, out_audio: u64, out_text: u64) -> Value {
        json!({
            "promptTokenCount": prompt_audio + prompt_text,
            "responseTokenCount": out_audio + out_text,
            "totalTokenCount": prompt_audio + prompt_text + out_audio + out_text,
            "promptTokensDetails": [
                {"modality": "AUDIO", "tokenCount": prompt_audio},
                {"modality": "TEXT", "tokenCount": prompt_text},
            ],
            "responseTokensDetails": [
                {"modality": "AUDIO", "tokenCount": out_audio},
                {"modality": "TEXT", "tokenCount": out_text},
            ],
        })
    }

    #[test]
    fn reads_usage_metadata_with_modalities() {
        let u = Usage::from_metadata(&json!({
            "promptTokenCount": 1200, "responseTokenCount": 80, "totalTokenCount": 1300,
            "cachedContentTokenCount": 100, "thoughtsTokenCount": 20,
            "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 1000}, {"modality": "TEXT", "tokenCount": 200}],
            "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 80}],
        }));
        assert_eq!(
            u,
            Usage {
                prompt_tokens: 1200,
                response_tokens: 80,
                total_tokens: 1300,
                cached_tokens: 100,
                thoughts_tokens: 20,
                prompt_audio_tokens: 1000,
                prompt_text_tokens: 200,
                response_audio_tokens: 80,
                reports: 1,
                ..Usage::default()
            }
        );
        // No totalTokenCount: derived from the parts.
        let u = Usage::from_metadata(&json!({"promptTokenCount": 10, "responseTokenCount": 5}));
        assert_eq!(u.total_tokens, 15);
    }

    #[test]
    fn accumulates_across_responses() {
        let mut total = Usage::default();
        total.add(&Usage::from_metadata(&turn(1500, 500, 300, 0)));
        total.add(&Usage::from_metadata(&turn(3000, 500, 600, 10)));
        assert_eq!(total.prompt_tokens, 5500);
        assert_eq!(total.response_tokens, 910);
        assert_eq!(total.total_tokens, 6410);
        assert_eq!(total.prompt_audio_tokens, 4500);
        assert_eq!(total.prompt_text_tokens, 1000);
        assert_eq!(total.response_audio_tokens, 900);
        assert_eq!(total.response_text_tokens, 10);
        assert_eq!(total.reports, 2);
    }

    #[test]
    fn token_cost_uses_per_modality_rates() {
        let mut u = Usage::default();
        // One minute of audio each way (25 tokens/s) plus 1M text tokens in.
        u.add(&Usage::from_metadata(&turn(1500, 1_000_000, 1500, 0)));
        let c = estimate(&u, 60, &Pricing::default());
        assert_eq!(c.basis, "tokens");
        assert!(c.estimate);
        assert_eq!(c.input_usd, 0.7545);
        assert_eq!(c.output_usd, 0.018);
        assert_eq!(c.usd, 0.7725);
        // Text out and thinking at the text output rate.
        let u = Usage::from_metadata(&json!({
            "promptTokenCount": 0, "responseTokenCount": 1_000_000, "thoughtsTokenCount": 1_000_000,
            "responseTokensDetails": [{"modality": "TEXT", "tokenCount": 1_000_000}],
        }));
        assert_eq!(estimate(&u, 60, &Pricing::default()).output_usd, 9.0);
    }

    #[test]
    fn unreported_modalities_price_as_audio() {
        let u = Usage::from_metadata(
            &json!({"promptTokenCount": 1_000_000, "responseTokenCount": 1_000_000}),
        );
        let c = estimate(&u, 60, &Pricing::default());
        assert_eq!((c.input_usd, c.output_usd), (3.0, 12.0));
    }

    #[test]
    fn falls_back_to_minutes_without_token_data() {
        let c = estimate(&Usage::default(), 120, &Pricing::default());
        assert_eq!(c.basis, "minutes");
        assert_eq!(c.usd, 0.046);
        let c = estimate(&Usage::default(), 0, &Pricing::default());
        assert_eq!((c.basis, c.usd), ("none", 0.0));
    }

    #[test]
    fn pricing_overrides_merge_over_defaults() {
        let p = Pricing::from_json(r#"{"audio_output_per_mtok": 20}"#).unwrap();
        assert_eq!(p.audio_output_per_mtok, 20.0);
        assert_eq!(p.audio_input_per_mtok, 3.0);
        assert_eq!(Pricing::from_json("").unwrap(), Pricing::default());
        assert!(Pricing::from_json(r#"{"audio_out": 1}"#).is_err(), "typo");
        assert!(Pricing::from_json(r#"{"audio_input_per_min": -1}"#).is_err());
    }

    #[test]
    fn summary_note_formats() {
        let mut u = Usage::default();
        u.add(&Usage::from_metadata(&turn(40_000, 1_000, 200, 0)));
        let c = estimate(&u, 180, &Pricing::default());
        assert_eq!(
            summary_note(3, &u, &c),
            "3 min · 41.2k tokens · ~$0.12 est."
        );
        let c = estimate(&Usage::default(), 180, &Pricing::default());
        assert_eq!(
            summary_note(3, &Usage::default(), &c),
            "3 min · ~$0.069 est."
        );
        assert_eq!(short_usd(1.234), "~$1.23 est.");
        assert_eq!(short_tokens(2_500_000), "2.50M");
    }

    #[test]
    fn appends_one_line_per_call_without_content() {
        let dir = std::env::temp_dir().join(format!("hwv-stats-{}", rand::random::<u32>()));
        let log = StatsLog {
            path: dir.join("call_stats.jsonl"),
            model: "gemini-3.8-live".into(),
            pricing: Pricing::default(),
        };
        let usage = Usage::from_metadata(&turn(1500, 0, 1500, 0));
        let stats = |id| CallStats {
            id,
            direction: "inbound",
            status: json!("completed"),
            started_at: 100,
            answered_at: Some(105),
            ended_at: Some(165),
            ended_by: "phone",
            voice_mode: "separate",
            tool_calls: 2,
            ask_hermes_calls: 1,
            usage: &usage,
        };
        log.append(&stats("a")).unwrap();
        log.append(&stats("b")).unwrap();
        let text = std::fs::read_to_string(&log.path).unwrap();
        let lines: Vec<Value> = text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        let l = &lines[0];
        assert_eq!(l["call_id"], "a");
        assert_eq!(l["duration_seconds"], 60);
        assert_eq!(l["model"], "gemini-3.8-live");
        assert_eq!(l["ask_hermes_calls"], 1);
        assert_eq!(l["usage"]["prompt_audio_tokens"], 1500);
        assert_eq!(l["cost_estimate"]["basis"], "tokens");
        assert_eq!(l["cost_estimate"]["estimate"], true);
        assert_eq!(l["cost_estimate"]["usd"], 0.0225);
        for banned in ["transcript", "reason", "summary", "token\""] {
            assert!(!text.contains(banned), "{banned} must not be logged");
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&log.path).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        std::fs::remove_dir_all(dir).unwrap();
    }
}
