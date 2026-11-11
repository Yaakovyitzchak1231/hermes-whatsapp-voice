//! The face of a video call: a bridge process (`sidecar/bridge/simli_bridge.py` or
//! `tavus_bridge.py`) turns the voice model's speech into a lip-synced talking face. The Tavus
//! face is expressive: it takes emotion cues, and it hands back snapshots of the owner's camera
//! so the voice model can see them. A full-pipeline Tavus face (`tavus_full_bridge.py`) goes
//! further and holds the conversation itself: it hears the owner, sees their camera and answers
//! in its own voice, handing tool calls back here for Hermes. It hands back H.264 access units for the
//! call's video and its own copy of the speech, which the call plays instead of the raw model audio
//! so lips and voice stay together.
//!
//! Wire format both ways: one type byte, a little-endian u32 payload length, the payload. The
//! message types are documented at the top of the bridge script.

use std::path::PathBuf;
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use whatsapp_rust::async_channel;
use whatsapp_rust::voip::{TimedVideoFrame, VideoFrame, VideoSource};

use crate::audio::{i16_to_le_bytes, le_bytes_to_i16};

const MSG_AUDIO: u8 = 0x01;
const MSG_CLEAR: u8 = 0x02;
const MSG_END: u8 = 0x03;
const MSG_EMOTION: u8 = 0x04;
const MSG_CAMERA: u8 = 0x05;
const MSG_CONTEXT: u8 = 0x06;
const MSG_RESULT: u8 = 0x07;
const MSG_START: u8 = 0x08;
const OUT_READY: u8 = 0x10;
const OUT_VIDEO: u8 = 0x11;
const OUT_AUDIO: u8 = 0x12;
const OUT_SNAPSHOT: u8 = 0x13;
const OUT_SAID: u8 = 0x14;
const OUT_TOOL: u8 = 0x15;
const OUT_HANGUP: u8 = 0x16;
/// Largest message the bridge may send; a keyframe at its bitrate cap is far smaller.
const MAX_MESSAGE: usize = 4 << 20;
/// Access units waiting for the call. When the call falls behind, frames are skipped from a
/// keyframe boundary rather than queued into latency.
const VIDEO_QUEUE: usize = 8;
/// RTP clock step at the bridge's nominal 20 fps; frames carry their own capture timestamps.
const STRIDE_20FPS: u32 = 90_000 / 20;
/// From handing Simli speech to hearing it back: its render delay plus the bridge's pacing lead.
const RENDER_LATENCY: Duration = Duration::from_millis(1500);
const SAMPLE_RATE: f64 = 16_000.0;

#[derive(Clone, Debug)]
pub struct AvatarConfig {
    /// Python interpreter with the bridge's packages (simli-ai or daily-python, av).
    pub python: String,
    pub script: PathBuf,
    pub face_id: String,
    /// The provider's API key, as (variable, value) for the bridge's environment.
    pub key: (String, String),
    /// The face shows emotions on cue and watches the owner's camera (Tavus).
    pub expressive: bool,
    /// The face holds the whole conversation, in its own voice (Tavus full pipeline).
    pub full: bool,
    /// Hard cap on one session, so a stuck call can never use up the account's minutes.
    pub max_session: Duration,
}

/// Emotions the expressive face can show.
pub const EMOTIONS: [&str; 8] = [
    "neutral",
    "happiness",
    "sadness",
    "anger",
    "fear",
    "surprise",
    "disgust",
    "contempt",
];

#[derive(Debug, PartialEq)]
pub enum AvatarEvent {
    /// Simli is connected and the first keyframe is out: the face is live.
    Ready,
    /// Lip-synced speech to play into the call (16 kHz mono).
    Audio(Vec<i16>),
    /// A JPEG of the owner's camera, about once a second.
    Snapshot(Vec<u8>),
    /// Full pipeline: something said on the call, by "owner" or "assistant".
    Said {
        role: &'static str,
        text: String,
    },
    /// Full pipeline: the face wants a tool run.
    ToolCall {
        id: String,
        name: String,
        args: serde_json::Value,
    },
    /// Full pipeline: the face ended the call.
    Hangup,
    Exited(String),
}

#[derive(Debug)]
enum Msg {
    Audio(Vec<i16>),
    Clear,
    End,
    Emotion(String),
    Camera(Vec<u8>),
    Context(String),
    Start,
    ToolResult(String),
}

/// A running bridge for one call.
pub struct Avatar {
    to_bridge: mpsc::UnboundedSender<Msg>,
    pub events: mpsc::UnboundedReceiver<AvatarEvent>,
    child: Child,
    tasks: Vec<JoinHandle<()>>,
    /// The face is live.
    pub ready: bool,
    /// The call's speech goes through the face. Switched on at a quiet moment after `ready`, so
    /// a sentence never jumps from the plain voice to the face halfway through.
    pub speaking: bool,
    /// When the speech already handed to the face finishes playing back.
    talking_until: Option<Instant>,
    pub expressive: bool,
    pub full: bool,
}

/// The call's video: the bridge's access units with their capture timestamps.
pub struct AvatarVideo {
    timed: async_channel::Receiver<TimedVideoFrame>,
}

impl VideoSource for AvatarVideo {
    fn frames(&self) -> async_channel::Receiver<Vec<u8>> {
        // Unused: the timed channel below carries every frame.
        async_channel::bounded(1).1
    }

    fn timed_frames(&self) -> Option<async_channel::Receiver<TimedVideoFrame>> {
        Some(self.timed.clone())
    }

    fn rtp_timestamp_stride(&self) -> u32 {
        STRIDE_20FPS
    }
}

/// A sink for the owner's camera when the face does not watch it: frames are read and dropped.
pub fn discard_peer_video() -> async_channel::Sender<VideoFrame> {
    let (tx, rx) = async_channel::bounded::<VideoFrame>(4);
    tokio::spawn(async move { while rx.recv().await.is_ok() {} });
    tx
}

impl Avatar {
    pub fn spawn(cfg: &AvatarConfig) -> Result<(Self, AvatarVideo)> {
        let mut child = Command::new(&cfg.python)
            .arg(&cfg.script)
            .args(["--serve", "--face-id", &cfg.face_id])
            .args(["--max-session", &cfg.max_session.as_secs().to_string()])
            .env(&cfg.key.0, &cfg.key.1)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // The bridge's own log lines land in the sidecar's log.
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .with_context(|| {
                format!(
                    "starting the avatar bridge ({} {})",
                    cfg.python,
                    cfg.script.display()
                )
            })?;
        let stdin = child.stdin.take().context("avatar bridge stdin")?;
        let stdout = child.stdout.take().context("avatar bridge stdout")?;
        let (video_tx, video_rx) = async_channel::bounded(VIDEO_QUEUE);
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let (msg_tx, msg_rx) = mpsc::unbounded_channel();
        let tasks = vec![
            tokio::spawn(write_bridge(stdin, msg_rx)),
            tokio::spawn(read_bridge(stdout, video_tx, event_tx)),
        ];
        let avatar = Self {
            to_bridge: msg_tx,
            events: event_rx,
            child,
            tasks,
            ready: false,
            speaking: false,
            talking_until: None,
            expressive: cfg.expressive,
            full: cfg.full,
        };
        Ok((avatar, AvatarVideo { timed: video_rx }))
    }

    /// Model speech (16 kHz mono) for the face to say.
    pub fn say(&mut self, pcm: Vec<i16>) {
        let now = Instant::now();
        let length = Duration::from_secs_f64(pcm.len() as f64 / SAMPLE_RATE);
        let start = self.talking_until.map_or(now, |t| t.max(now));
        self.talking_until = Some(start + length);
        let _ = self.to_bridge.send(Msg::Audio(pcm));
    }

    /// Shows an emotion on the face (one of [`EMOTIONS`]); ignored by faces that cannot.
    pub fn express(&self, emotion: &str) {
        if self.expressive {
            let _ = self.to_bridge.send(Msg::Emotion(emotion.to_string()));
        }
    }

    /// Full pipeline: what the face needs to know before the call (JSON with `system_prompt` and
    /// `context`). The face is created from it while the phone rings.
    pub fn context(&self, json: String) {
        let _ = self.to_bridge.send(Msg::Context(json));
    }

    /// Full pipeline: the owner answered, so the face joins and says hello.
    pub fn start(&self) {
        let _ = self.to_bridge.send(Msg::Start);
    }

    /// Full pipeline: the owner's speech (16 kHz mono) for the face to hear.
    pub fn hear(&self, pcm: Vec<i16>) {
        let _ = self.to_bridge.send(Msg::Audio(pcm));
    }

    /// Full pipeline: the result of a tool the face asked for.
    pub fn tool_result(&self, id: &str, output: &str, ok: bool) {
        let status = if ok { "success" } else { "error" };
        let json = serde_json::json!({"id": id, "output": output, "status": status});
        let _ = self.to_bridge.send(Msg::ToolResult(json.to_string()));
    }

    /// Where the call delivers the owner's camera. An expressive face gets every access unit
    /// from the first keyframe on, and sends back snapshots the voice model can look at.
    pub fn camera(&self) -> async_channel::Sender<VideoFrame> {
        if !self.expressive && !self.full {
            return discard_peer_video();
        }
        let (tx, rx) = async_channel::bounded::<VideoFrame>(16);
        let to_bridge = self.to_bridge.clone();
        tokio::spawn(async move {
            let mut started = false;
            while let Ok(frame) = rx.recv().await {
                started |= frame.keyframe;
                if !started {
                    continue;
                }
                // The phone sends its camera as captured; the orientation says how to turn it.
                let mut payload = Vec::with_capacity(frame.data.len() + 1);
                payload.push(frame.orientation & 3);
                payload.extend_from_slice(&frame.data);
                if to_bridge.send(Msg::Camera(payload)).is_err() {
                    break;
                }
            }
        });
        tx
    }

    /// The owner barged in: drop the speech the face has not said yet.
    pub fn clear(&mut self) {
        self.talking_until = None;
        let _ = self.to_bridge.send(Msg::Clear);
    }

    /// Speech handed to the face is still on its way back or playing.
    pub fn busy(&self, now: Instant) -> bool {
        self.talking_until
            .is_some_and(|until| now < until + RENDER_LATENCY)
    }

    /// Ends the face's session and the bridge process.
    pub async fn finish(mut self) {
        let _ = self.to_bridge.send(Msg::End);
        if tokio::time::timeout(Duration::from_secs(5), self.child.wait())
            .await
            .is_err()
        {
            log::warn!("avatar bridge did not exit; killing it");
            let _ = self.child.kill().await;
        }
        self.tasks.iter().for_each(JoinHandle::abort);
    }
}

pub fn encode(kind: u8, payload: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(5 + payload.len());
    frame.push(kind);
    frame.extend((payload.len() as u32).to_le_bytes());
    frame.extend_from_slice(payload);
    frame
}

/// Reads one message; None at a clean end of stream.
async fn read_message(r: &mut (impl AsyncRead + Unpin)) -> Result<Option<(u8, Vec<u8>)>> {
    let mut header = [0u8; 5];
    match r.read_exact(&mut header).await {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let len = u32::from_le_bytes([header[1], header[2], header[3], header[4]]) as usize;
    anyhow::ensure!(
        len <= MAX_MESSAGE,
        "avatar bridge sent a {len}-byte message"
    );
    let mut payload = vec![0u8; len];
    r.read_exact(&mut payload).await?;
    Ok(Some((header[0], payload)))
}

async fn write_bridge(mut w: impl AsyncWrite + Unpin, mut rx: mpsc::UnboundedReceiver<Msg>) {
    while let Some(msg) = rx.recv().await {
        let (kind, payload) = match msg {
            Msg::Audio(pcm) => (MSG_AUDIO, i16_to_le_bytes(&pcm)),
            Msg::Clear => (MSG_CLEAR, Vec::new()),
            Msg::End => (MSG_END, Vec::new()),
            Msg::Emotion(e) => (MSG_EMOTION, e.into_bytes()),
            Msg::Camera(au) => (MSG_CAMERA, au),
            Msg::Context(json) => (MSG_CONTEXT, json.into_bytes()),
            Msg::Start => (MSG_START, Vec::new()),
            Msg::ToolResult(json) => (MSG_RESULT, json.into_bytes()),
        };
        if w.write_all(&encode(kind, &payload)).await.is_err() || w.flush().await.is_err() {
            return;
        }
        if kind == MSG_END {
            // Dropping stdin as well tells the bridge the call is over.
            return;
        }
    }
}

/// Whether an Annex-B access unit holds an IDR slice (a point the peer can start decoding).
pub fn has_idr(au: &[u8]) -> bool {
    au.windows(4)
        .any(|w| w[0] == 0 && w[1] == 0 && w[2] == 1 && (w[3] & 0x1f) == 5)
}

/// Passes access units on to the call, skipping from a keyframe boundary when the call is behind:
/// one dropped frame would corrupt the picture until the next keyframe anyway.
#[derive(Default)]
struct VideoGate {
    started: bool,
    dropping: bool,
}

impl VideoGate {
    /// Whether to offer this AU to the call at all.
    fn admit(&mut self, au: &[u8]) -> bool {
        let idr = has_idr(au);
        self.started |= idr;
        if self.dropping && idr {
            self.dropping = false;
        }
        self.started && !self.dropping
    }

    fn full(&mut self) {
        self.dropping = true;
    }
}

async fn read_bridge(
    mut out: impl AsyncRead + Unpin,
    video: async_channel::Sender<TimedVideoFrame>,
    events: mpsc::UnboundedSender<AvatarEvent>,
) {
    let mut gate = VideoGate::default();
    let why = loop {
        let (kind, mut payload) = match read_message(&mut out).await {
            Ok(Some(m)) => m,
            Ok(None) => break "the avatar bridge exited".to_string(),
            Err(e) => break format!("avatar bridge: {e:#}"),
        };
        match kind {
            OUT_READY => {
                let _ = events.send(AvatarEvent::Ready);
            }
            OUT_VIDEO if payload.len() > 4 => {
                let au = payload.split_off(4);
                let timestamp =
                    u32::from_le_bytes([payload[0], payload[1], payload[2], payload[3]]);
                if !gate.admit(&au) {
                    continue;
                }
                let frame = TimedVideoFrame::builder()
                    .data(au)
                    .timestamp(timestamp)
                    .build();
                if let Err(async_channel::TrySendError::Full(_)) = video.try_send(frame) {
                    gate.full();
                }
            }
            OUT_AUDIO => {
                let _ = events.send(AvatarEvent::Audio(le_bytes_to_i16(&payload)));
            }
            OUT_SNAPSHOT if payload.starts_with(&[0xff, 0xd8]) => {
                let _ = events.send(AvatarEvent::Snapshot(payload));
            }
            OUT_SAID => {
                if let Some(said) = parse_said(&payload) {
                    let _ = events.send(said);
                }
            }
            OUT_TOOL => {
                if let Ok(call) = serde_json::from_slice::<serde_json::Value>(&payload)
                    && let (Some(id), Some(name)) = (call["id"].as_str(), call["name"].as_str())
                {
                    let _ = events.send(AvatarEvent::ToolCall {
                        id: id.to_string(),
                        name: name.to_string(),
                        args: call["args"].clone(),
                    });
                }
            }
            OUT_HANGUP => {
                let _ = events.send(AvatarEvent::Hangup);
            }
            _ => {}
        }
    };
    let _ = events.send(AvatarEvent::Exited(why));
}

fn parse_said(payload: &[u8]) -> Option<AvatarEvent> {
    let said: serde_json::Value = serde_json::from_slice(payload).ok()?;
    let role = match said["role"].as_str()? {
        "owner" => "owner",
        "assistant" => "assistant",
        _ => return None,
    };
    let text = said["text"].as_str()?.trim();
    (!text.is_empty()).then(|| AvatarEvent::Said {
        role,
        text: text.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const IDR: &[u8] = &[
        0, 0, 0, 1, 0x09, 0xf0, 0, 0, 0, 1, 0x67, 1, 0, 0, 1, 0x65, 9,
    ];
    const P: &[u8] = &[0, 0, 0, 1, 0x09, 0xf0, 0, 0, 1, 0x41, 9];

    #[test]
    fn finds_idr_slices() {
        assert!(has_idr(IDR));
        assert!(!has_idr(P));
    }

    #[test]
    fn video_waits_for_a_keyframe_and_skips_to_the_next_one_when_full() {
        let mut gate = VideoGate::default();
        assert!(!gate.admit(P), "nothing before the first keyframe");
        assert!(gate.admit(IDR));
        assert!(gate.admit(P));
        gate.full();
        assert!(!gate.admit(P), "skip until the next keyframe");
        assert!(gate.admit(IDR));
        assert!(gate.admit(P));
    }

    #[tokio::test]
    async fn bridge_messages_round_trip() {
        let mut wire = Vec::new();
        wire.extend(encode(OUT_READY, &[]));
        let mut video = 4500u32.to_le_bytes().to_vec();
        video.extend_from_slice(P);
        wire.extend(encode(OUT_VIDEO, &video));
        let mut video = 9000u32.to_le_bytes().to_vec();
        video.extend_from_slice(IDR);
        wire.extend(encode(OUT_VIDEO, &video));
        wire.extend(encode(OUT_AUDIO, &i16_to_le_bytes(&[1, -2, 3])));
        wire.extend(encode(OUT_SNAPSHOT, b"not a jpeg"));
        wire.extend(encode(OUT_SNAPSHOT, &[0xff, 0xd8, 0xff, 0xd9]));
        wire.extend(encode(OUT_SAID, br#"{"role":"owner","text":" hi there "}"#));
        wire.extend(encode(OUT_SAID, br#"{"role":"someone","text":"no"}"#));
        wire.extend(encode(
            OUT_TOOL,
            br#"{"id":"c1","name":"ask_hermes","args":{"request":"weather"}}"#,
        ));
        wire.extend(encode(OUT_HANGUP, &[]));

        let (vtx, vrx) = async_channel::bounded(8);
        let (etx, mut erx) = mpsc::unbounded_channel();
        read_bridge(wire.as_slice(), vtx, etx).await;

        assert_eq!(erx.recv().await, Some(AvatarEvent::Ready));
        assert_eq!(erx.recv().await, Some(AvatarEvent::Audio(vec![1, -2, 3])));
        assert_eq!(
            erx.recv().await,
            Some(AvatarEvent::Snapshot(vec![0xff, 0xd8, 0xff, 0xd9])),
            "only JPEG snapshots pass"
        );
        assert_eq!(
            erx.recv().await,
            Some(AvatarEvent::Said {
                role: "owner",
                text: "hi there".into()
            }),
            "unknown roles are dropped"
        );
        assert_eq!(
            erx.recv().await,
            Some(AvatarEvent::ToolCall {
                id: "c1".into(),
                name: "ask_hermes".into(),
                args: serde_json::json!({"request": "weather"}),
            })
        );
        assert_eq!(erx.recv().await, Some(AvatarEvent::Hangup));
        assert!(matches!(erx.recv().await, Some(AvatarEvent::Exited(_))));
        // The P frame came before any keyframe, so only the IDR reaches the call.
        let frame = vrx.recv().await.unwrap();
        assert_eq!((frame.data.as_slice(), frame.timestamp), (IDR, 9000));
        assert!(vrx.try_recv().is_err());
    }

    #[tokio::test]
    async fn speech_and_controls_are_framed_for_the_bridge() {
        let (tx, rx) = mpsc::unbounded_channel();
        tx.send(Msg::Audio(vec![7, -7])).unwrap();
        tx.send(Msg::Clear).unwrap();
        tx.send(Msg::Emotion("happiness".into())).unwrap();
        tx.send(Msg::Camera(vec![0, 0, 0, 1, 0x65])).unwrap();
        tx.send(Msg::Context("{}".into())).unwrap();
        tx.send(Msg::Start).unwrap();
        tx.send(Msg::ToolResult("{}".into())).unwrap();
        tx.send(Msg::End).unwrap();
        let mut wire = Vec::new();
        write_bridge(&mut wire, rx).await;
        let mut r = wire.as_slice();
        assert_eq!(
            read_message(&mut r).await.unwrap(),
            Some((MSG_AUDIO, i16_to_le_bytes(&[7, -7])))
        );
        assert_eq!(
            read_message(&mut r).await.unwrap(),
            Some((MSG_CLEAR, vec![]))
        );
        assert_eq!(
            read_message(&mut r).await.unwrap(),
            Some((MSG_EMOTION, b"happiness".to_vec()))
        );
        assert_eq!(
            read_message(&mut r).await.unwrap(),
            Some((MSG_CAMERA, vec![0, 0, 0, 1, 0x65]))
        );
        for kind in [MSG_CONTEXT, MSG_START, MSG_RESULT] {
            let (k, _) = read_message(&mut r).await.unwrap().unwrap();
            assert_eq!(k, kind);
        }
        assert_eq!(read_message(&mut r).await.unwrap(), Some((MSG_END, vec![])));
        assert_eq!(read_message(&mut r).await.unwrap(), None);
    }
}
