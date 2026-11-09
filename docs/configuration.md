# Configuration

There are three places to change settings, and all of them write the same values:
- the **WhatsApp Voice** tab in the Hermes web dashboard;
- the plugin's settings page in the Hermes Desktop app (Settings > Plugins);
- the command line: `hermes config set plugins.entries.hermes-whatsapp-voice.settings.<key> <value>`.

Restart the sidecar after a change: the dashboard's **Restart sidecar** button, or
`systemctl --user restart hermes-whatsapp-voice` (Linux), or
`launchctl kickstart -k gui/$(id -u)/com.hermes.whatsapp-voice` (macOS).

## Settings

| key | default | |
|---|---|---|
| `context_mode` | `separate` | `separate`: a small voice agent with a short briefing that asks Hermes through `ask_hermes`. `core_session`: the voice is the model of your WhatsApp chat with Hermes (see below); costs several times more per call |
| `owner_number` | (required) | Your number, international format |
| `voice_model` | `gemini-3.8-live` | Gemini Live model |
| `voice` | `Kore` | Gemini prebuilt voice |
| `assistant_name` | `Hermes` | Fallback name when Hermes gives no persona briefing |
| `languages` | (empty) | Language codes the voice may speak, e.g. `en-US, ta-IN`. Speech recognition only takes them as hints; set `speech_language` too if it mishears your language |
| `speech_language` | (automatic) | Your main spoken language, one code (e.g. `ta-IN`). Gemini uses it for recognising your speech and for its voice; much stronger than `languages`, which are only hints |
| `voice_instructions` | (empty) | Your own extra rules for the voice: language, tone, call length |
| `vocabulary` | (empty) | Words transcripts should spell as given, e.g. names; your own name is added automatically |
| `voice_tools` | memory, skills, web, file reading, Hindsight… | `core_session`: tools the voice uses itself for quick lookups (only those your chat has); real jobs go to your main model |
| `history_chars` | `30000` | `core_session`: recent chat history given to the voice at call start |
| `live_config` | `{}` | Advanced: any Gemini Live setup field, merged over the defaults (see below) |
| `ring_timeout_seconds` | `45` | How long an outbound call rings |
| `max_call_minutes` | `15` | Hard cap per call |
| `silence_hangup_seconds` | `180` | End a call when nobody has spoken for this long (e.g. left on hold) |
| `pause_before_reply_ms` | `600` | How long you can pause before the voice takes your turn as over and replies; raise it if it cuts you off |
| `hangup_grace_seconds` | `3` | Quiet time after the assistant's goodbye before it hangs up; speaking in it keeps the call up |
| `hermes_timeout_seconds` | `180` | How long the voice waits for Hermes on one request, and for the call summary |
| `max_silent_tools` | `5` | Tools the voice may run in a row before it must tell you what it is doing |
| `record_calls` | `false` | Save your side of each call as `recordings/<call id>.wav` in the plugin's data folder (this machine only) |
| `answer_inbound` | `true` | Answer calls from `owner_number` |
| `send_summary` | `true` | Message a summary after each call (with its length, the voice model's tokens and an estimated cost) |
| `call_stats` | `true` | Append each call's stats (duration, Gemini tokens, estimated cost; no transcript or audio) to `call_stats.jsonl` in the plugin's data folder. Review with `hermes whatsapp-voice stats` |
| `pricing` | `{}` | Prices behind the cost estimate, merged over the defaults (see [Call stats and cost](#call-stats-and-cost)) |
| `listen` | `127.0.0.1:8796` | Sidecar API address (loopback) |
| `context_listen` | `127.0.0.1:8797` | `core_session` service address (loopback) |
| `voice_api_url` | Gemini Live endpoint | Advanced: the Live API WebSocket URL, for a proxy. The Gemini key is sent there |
| `hermes_api_url` | `http://127.0.0.1:8642` | Hermes API server |

**Every Gemini Live option.** `live_config` takes any field of the Live API's setup message and
merges it over the plugin's defaults. Objects merge key by key. `tools` is appended to the
plugin's own tools rather than replacing them. For example, to change the voice and temperature, and
let the caller pause longer:

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-whatsapp-voice:
      settings:
        live_config:
          generationConfig:
            temperature: 0.6
            speechConfig: {voiceConfig: {prebuiltVoiceConfig: {voiceName: Puck}}}
          realtimeInputConfig:
            automaticActivityDetection: {silenceDurationMs: 900, endOfSpeechSensitivity: END_SENSITIVITY_LOW}
```

For languages, prefer the `languages` setting. A `languageCodes` value inside `live_config` wins
over it.

What each field does, and which ones a given model supports:
[Live API reference (setup message)](https://ai.google.dev/api/live),
[Live API capabilities guide](https://ai.google.dev/gemini-api/docs/live-guide),
[session management](https://ai.google.dev/gemini-api/docs/live-session),
[tool use](https://ai.google.dev/gemini-api/docs/live-tools),
[best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices),
[gemini-3.8-live model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live), and
[voices](https://ai.google.dev/gemini-api/docs/speech-generation). There is no speaking-rate
parameter. Ask for pace and tone in `voice_instructions` instead.

**Personality.** The voice doesn't use a built-in persona. At startup and after each call, the
sidecar asks your Hermes for a short briefing: its name, its personality, and what it knows about
you. The voice then sounds like your Hermes, and changes to Hermes's memory carry over. Use
`voice_instructions` for anything else, for example:

```bash
hermes config set plugins.entries.hermes-whatsapp-voice.settings.voice_instructions \
  "Keep calls very short and direct. Never ask if there is anything else."
```

## Core-session mode

With `context_mode: core_session`, `hermes whatsapp-voice serve` also runs a small
loopback service in Hermes's own interpreter, and each call becomes part of your WhatsApp DM
session with Hermes:

- **Context:** the voice gets that session's stored system prompt (persona, memory, skills), its
  session context, and recent history (`history_chars`), loaded before the call starts.
- **Tools:** the voice handles quick lookups itself with `voice_tools` (no terminal or changes by
  default), run through Hermes in that session; it cannot run any tool you didn't list. Real jobs go
  to `ask_hermes_deep`: a full normal turn of your main model, with all of its tools, in the same
  session; the voice keeps talking and tells you the outcome. Approvals can't be given on a call: a
  command that needs one is denied, and the voice tells you to ask again in the WhatsApp text chat,
  where Hermes's own `/approve` applies.
- **Write-back:** what you and the voice say, and its tool calls, are saved into the session as the
  call goes (marked `display_kind: voice_call`), under Hermes's cross-process turn lock, so your next
  text message continues with the call in context.
- **Texts during a call** go into the call: the voice answers them, and the text agent stays quiet
  (a gateway hook; slash commands, media and other chats are unaffected). Needs a gateway restart
  after installing or updating the plugin.

**Cost:** a live voice model re-reads its whole context on every reply. In this mode that context is
the chat's system prompt, tool schemas and up to `history_chars` of history, so a call costs several
times more than with `separate`. Each call record shows the tokens the voice model used (`metrics.usage`),
and `hermes whatsapp-voice stats` shows them with an estimated cost per call.

It uses Hermes internals that are not part of the plugin API (listed in `CORE_PATCHES.md`); if the
service is unavailable, a call falls back to the separate voice agent. It needs a WhatsApp DM
session with the bot to exist (send it one text first).

## Call stats and cost

After each call the sidecar appends one JSON line to `call_stats.jsonl` in the plugin's data folder
(`call_stats: false` turns it off): call id, direction, status, start/answer/end times, seconds on
the line, who ended it, the model, the voice mode, tool and `ask_hermes` counts, the voice model's
token use and an estimated cost. It never holds the transcript, the call reason, audio or keys.

```bash
hermes whatsapp-voice stats             # last 10 calls, plus totals over all logged calls
hermes whatsapp-voice stats --limit 50
hermes whatsapp-voice stats --json      # the raw records
```

**Tokens** come from the `usageMetadata` Gemini Live sends with each model response, summed over the
call: `prompt_tokens`, `response_tokens`, `total_tokens`, cached / tool-use / thinking tokens, and the
audio/text split when Gemini reports it. Each response counts the whole context the model re-read,
which is what Google bills, so the sum is the call's billed total.

**Cost** is an estimate, never a bill. With token data it is priced per modality; without any (the
model reported nothing) it falls back to a per-minute rate over the answered time. Defaults, in USD,
are Google's published paid-tier prices for `gemini-3.8-live`
([pricing page](https://ai.google.dev/gemini-api/docs/pricing); 25 audio tokens a second, so ~$0.005/min in and ~$0.018/min
out, about 2.3 cents a call minute before text):

| Key | Default | Meaning |
|---|---|---|
| `audio_input_per_mtok` | `3.0` | Audio in, per million tokens |
| `audio_output_per_mtok` | `12.0` | Audio out, per million tokens |
| `text_input_per_mtok` | `0.75` | Text in (prompt, history, tool results), per million tokens |
| `text_output_per_mtok` | `4.5` | Text out and thinking, per million tokens |
| `audio_input_per_min` | `0.005` | Fallback only: per answered minute, in |
| `audio_output_per_min` | `0.018` | Fallback only: per answered minute, out |

Tokens Gemini does not label with a modality are priced as audio and cached tokens at the full input
rate, so the estimate errs high. Change a price when Google's changes, for example:

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hermes-whatsapp-voice:
      settings:
        pricing: {audio_output_per_mtok: 12.5}
```

Prices apply to calls logged after the sidecar restarts; earlier lines keep the prices they were
logged with (each line stores them).

The post-call WhatsApp message shows it too: `📞 Call summary (3 min · 41.2k tokens · ~$0.12 est.)`.

## Credentials

The plugin never asks for its own keys. It reuses what Hermes already has:
- **Gemini:** the key of Hermes's Gemini provider, `GOOGLE_API_KEY`, else `GEMINI_API_KEY`, from
  Hermes's `.env` (`~/.hermes/.env` by default). Add one with `hermes model` (choose Gemini). Google login (Cloud Code OAuth)
  doesn't work for the Live API.
- **Hermes API server:** `API_SERVER_KEY` from Hermes's `.env`. Without it, calls still work but
  the voice can't hand tasks to Hermes, and summaries are raw transcripts.

## Troubleshooting

See the table at the end of the [setup runbook](../skills/setup/SKILL.md#troubleshooting).
