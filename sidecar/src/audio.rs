//! Audio plumbing between a WhatsApp call and Gemini Live.
//!
//! Call audio (whatsapp-rust) is 16 kHz mono i16: we push 960-sample (60 ms) frames and receive
//! 320-sample (20 ms) frames. Gemini Live takes 16 kHz s16le and returns 24 kHz s16le.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::Mutex;

pub const FRAME_SAMPLES: usize = 960;

pub fn i16_to_le_bytes(samples: &[i16]) -> Vec<u8> {
    samples.iter().flat_map(|s| s.to_le_bytes()).collect()
}

pub fn le_bytes_to_i16(bytes: &[u8]) -> Vec<i16> {
    bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|b| i16::from_le_bytes(*b))
        .collect()
}

const HALF_TAPS: usize = 16;

/// Streaming 24 kHz -> 16 kHz converter with a windowed-sinc low-pass.
///
/// Output sample n sits at input position 1.5n, so only two filter phases exist (fraction 0 and
/// 0.5) and both are precomputed.
pub struct Downsampler {
    buf: Vec<f32>,
    pos: f64, // input index of the next output sample, relative to buf
    phases: [[f32; 2 * HALF_TAPS]; 2],
}

impl Downsampler {
    pub fn new() -> Self {
        let cutoff = 7400.0 / 24000.0; // just under the 8 kHz Nyquist of the output
        let mut phases = [[0f32; 2 * HALF_TAPS]; 2];
        for (phase, frac) in [0.0f64, 0.5].into_iter().enumerate() {
            let mut w = [0f64; 2 * HALF_TAPS];
            for (k, wk) in w.iter_mut().enumerate() {
                let x = (k as f64 - HALF_TAPS as f64 + 1.0) - frac; // offset from the output position
                let sinc = if x == 0.0 {
                    2.0 * cutoff
                } else {
                    (2.0 * std::f64::consts::PI * cutoff * x).sin() / (std::f64::consts::PI * x)
                };
                let hann = 0.5 + 0.5 * (std::f64::consts::PI * x / HALF_TAPS as f64).cos();
                *wk = sinc * hann;
            }
            let sum: f64 = w.iter().sum();
            for (tap, wk) in phases[phase].iter_mut().zip(w) {
                *tap = (wk / sum) as f32; // unity DC gain
            }
        }
        Self {
            buf: vec![0.0; HALF_TAPS],
            pos: HALF_TAPS as f64,
            phases,
        }
    }

    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        self.buf.extend(input.iter().map(|&s| s as f32));
        let mut out = Vec::with_capacity(input.len() * 2 / 3 + 1);
        loop {
            let base = self.pos as usize;
            if base + HALF_TAPS >= self.buf.len() {
                break;
            }
            let taps = &self.phases[usize::from(self.pos.fract() > 0.0)];
            let start = base + 1 - HALF_TAPS;
            let acc: f32 = self.buf[start..start + 2 * HALF_TAPS]
                .iter()
                .zip(taps)
                .map(|(x, h)| x * h)
                .sum();
            out.push(acc.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16);
            self.pos += 1.5;
        }
        let drop = (self.pos as usize).saturating_sub(HALF_TAPS);
        if drop > 0 {
            self.buf.drain(..drop);
            self.pos -= drop as f64;
        }
        out
    }
}

/// Model speech waiting to be played into the call, drained one 60 ms frame per tick.
#[derive(Default)]
pub struct Playout {
    queue: Mutex<VecDeque<i16>>,
}

impl Playout {
    pub fn push(&self, samples: &[i16]) {
        self.queue.lock().unwrap().extend(samples);
    }

    /// Drop queued speech, used when the caller barges in.
    pub fn clear(&self) {
        self.queue.lock().unwrap().clear();
    }

    pub fn buffered(&self) -> usize {
        self.queue.lock().unwrap().len()
    }

    /// The next frame, zero-padded on underrun so the call always gets a steady stream.
    pub fn next_frame(&self) -> Vec<i16> {
        let mut q = self.queue.lock().unwrap();
        let n = q.len().min(FRAME_SAMPLES);
        let mut frame: Vec<i16> = q.drain(..n).collect();
        frame.resize(FRAME_SAMPLES, 0);
        frame
    }
}

/// A full-pipeline face's sound and picture, sent out in step. Each access unit is placed on the
/// sound's timeline where it arrived (plus the measured offset between the two) and leaves with the
/// 60 ms of sound that contains that point, stamped with the same clock: the picture and the voice
/// reach the phone together and carry timestamps that agree.
#[derive(Default)]
pub struct SyncedPlayout {
    inner: Mutex<Synced>,
}

#[derive(Default)]
struct Synced {
    audio: VecDeque<i16>,
    /// Samples handed to the call so far, silence padding included: the call's sound clock.
    sent: u64,
    /// Access units waiting for their sound, by the sample count at which they are due.
    frames: VecDeque<(u64, Vec<u8>)>,
    last_ts: Option<u32>,
}

/// 90 kHz video RTP clock ticks per 16 kHz sound sample, as a fraction.
const VIDEO_TICKS: (u64, u64) = (90_000, 16_000);

impl SyncedPlayout {
    pub fn push_audio(&self, samples: &[i16]) {
        self.inner.lock().unwrap().audio.extend(samples);
    }

    /// One access unit of the picture. `offset` is in samples: positive when the sound that
    /// belongs with it has not arrived yet.
    pub fn push_video(&self, au: Vec<u8>, offset: i64) {
        let mut s = self.inner.lock().unwrap();
        let here = (s.sent + s.audio.len() as u64) as i64 + offset;
        let after = s.frames.back().map_or(s.sent, |f| f.0);
        let due = (here.max(0) as u64).max(after).max(s.sent);
        s.frames.push_back((due, au));
    }

    /// Keeps at most `max` samples of sound, dropping the oldest; the waiting picture moves up
    /// with it, so a trim never pulls the two apart.
    pub fn trim(&self, max: usize) {
        let mut s = self.inner.lock().unwrap();
        let excess = s.audio.len().saturating_sub(max);
        if excess == 0 {
            return;
        }
        s.audio.drain(..excess);
        let sent = s.sent;
        for f in s.frames.iter_mut() {
            f.0 = f.0.saturating_sub(excess as u64).max(sent);
        }
    }

    pub fn buffered(&self) -> usize {
        self.inner.lock().unwrap().audio.len()
    }

    /// The next 60 ms of sound (zero-padded) and the access units due with it, each with its
    /// 90 kHz timestamp.
    pub fn next_frame(&self) -> (Vec<i16>, Vec<(u32, Vec<u8>)>) {
        let mut s = self.inner.lock().unwrap();
        let n = s.audio.len().min(FRAME_SAMPLES);
        let mut frame: Vec<i16> = s.audio.drain(..n).collect();
        frame.resize(FRAME_SAMPLES, 0);
        s.sent += FRAME_SAMPLES as u64;
        let mut due = Vec::new();
        while s.frames.front().is_some_and(|f| f.0 <= s.sent) {
            let Some((at, au)) = s.frames.pop_front() else {
                break;
            };
            let mut ts = (at * VIDEO_TICKS.0 / VIDEO_TICKS.1) as u32;
            // Timestamps must move forward, even for two units due at the same sample.
            if let Some(last) = s.last_ts
                && (ts == last || ts.wrapping_sub(last) > 0x8000_0000)
            {
                ts = last.wrapping_add(1);
            }
            s.last_ts = Some(ts);
            due.push((ts, au));
        }
        (frame, due)
    }
}

/// Writes mono 16-bit PCM to a WAV file as it arrives; `finish` fills in the sizes.
pub struct WavWriter {
    file: BufWriter<File>,
    bytes: u32,
}

impl WavWriter {
    pub fn create(path: &Path, rate: u32) -> io::Result<Self> {
        let mut file = BufWriter::new(File::create(path)?);
        file.write_all(b"RIFF\0\0\0\0WAVEfmt ")?;
        for (v, n) in [
            (16, 4),
            (1, 2),
            (1, 2),
            (rate, 4),
            (rate * 2, 4),
            (2, 2),
            (16, 2),
        ] {
            file.write_all(&u32::to_le_bytes(v)[..n])?;
        }
        file.write_all(b"data\0\0\0\0")?;
        Ok(Self { file, bytes: 0 })
    }

    pub fn write(&mut self, pcm: &[i16]) -> io::Result<()> {
        self.bytes += (pcm.len() * 2) as u32;
        self.file.write_all(&i16_to_le_bytes(pcm))
    }

    pub fn finish(mut self) -> io::Result<()> {
        self.file.seek(SeekFrom::Start(4))?;
        self.file.write_all(&(36 + self.bytes).to_le_bytes())?;
        self.file.seek(SeekFrom::Start(40))?;
        self.file.write_all(&self.bytes.to_le_bytes())?;
        self.file.flush()
    }
}

/// Reads a mono 16-bit PCM WAV written by `WavWriter` (or any plain 44-byte-header WAV).
#[cfg(test)]
pub fn read_wav(path: &Path) -> io::Result<(u32, Vec<i16>)> {
    let bytes = std::fs::read(path)?;
    let rate = u32::from_le_bytes(bytes[24..28].try_into().unwrap());
    Ok((rate, le_bytes_to_i16(&bytes[44..])))
}

#[cfg(test)]
mod tests {

    #[test]
    fn synced_playout_sends_the_picture_with_its_sound() {
        let p = SyncedPlayout::default();
        p.push_audio(&[1; FRAME_SAMPLES * 2]);
        p.push_video(vec![7], 0); // belongs at the end of the queued sound
        let (a, v) = p.next_frame();
        assert_eq!(
            (a.len(), v.len()),
            (FRAME_SAMPLES, 0),
            "its sound has not gone out yet"
        );
        let (_, v) = p.next_frame();
        let at = (FRAME_SAMPLES * 2) as u64;
        assert_eq!(v, vec![((at * 90_000 / 16_000) as u32, vec![7])]);
    }

    #[test]
    fn synced_playout_offset_and_trim_keep_the_pair_together() {
        let p = SyncedPlayout::default();
        p.push_video(vec![1], FRAME_SAMPLES as i64); // its sound is 60 ms behind
        let (_, v) = p.next_frame();
        assert_eq!(v.len(), 1, "due at the end of the first 60 ms");
        p.push_audio(&[0; FRAME_SAMPLES * 10]);
        p.push_video(vec![2], 0);
        p.trim(FRAME_SAMPLES);
        assert_eq!(p.buffered(), FRAME_SAMPLES);
        let (_, v) = p.next_frame();
        assert_eq!(v.len(), 1, "moved up with the trimmed sound");
        let (_, v) = p.next_frame();
        assert!(v.is_empty());
    }

    #[test]
    fn synced_playout_timestamps_always_advance() {
        let p = SyncedPlayout::default();
        p.push_video(vec![1], 0);
        p.push_video(vec![2], 0);
        let (_, v) = p.next_frame();
        assert_eq!(v.len(), 2);
        assert!(v[1].0.wrapping_sub(v[0].0) == 1);
    }

    use super::*;

    fn sine(freq: f64, rate: f64, n: usize) -> Vec<i16> {
        (0..n)
            .map(|i| (16384.0 * (2.0 * std::f64::consts::PI * freq * i as f64 / rate).sin()) as i16)
            .collect()
    }

    fn rms(s: &[i16]) -> f64 {
        (s.iter().map(|&v| (v as f64).powi(2)).sum::<f64>() / s.len() as f64).sqrt()
    }

    #[test]
    fn wav_round_trip() {
        let path = std::env::temp_dir().join(format!("hwv-{}.wav", std::process::id()));
        let mut w = WavWriter::create(&path, 16_000).unwrap();
        w.write(&[1, -2, 3]).unwrap();
        w.write(&[4]).unwrap();
        w.finish().unwrap();
        assert_eq!(read_wav(&path).unwrap(), (16_000, vec![1, -2, 3, 4]));
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 44 + 8);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn pcm_bytes_round_trip() {
        let s = [0i16, 1, -1, i16::MAX, i16::MIN, 12345];
        assert_eq!(le_bytes_to_i16(&i16_to_le_bytes(&s)), s);
    }

    #[test]
    fn downsampler_ratio_across_uneven_chunks() {
        let mut d = Downsampler::new();
        let input = sine(440.0, 24000.0, 24000);
        let out: Vec<i16> = input.chunks(731).flat_map(|c| d.process(c)).collect();
        assert!((15980..=16000).contains(&out.len()), "got {}", out.len());
    }

    #[test]
    fn downsampler_keeps_passband_and_cuts_aliases() {
        let pass = Downsampler::new().process(&sine(1000.0, 24000.0, 24000));
        let r = rms(&pass[1000..]);
        assert!((10900.0..12300.0).contains(&r), "1 kHz rms {r}"); // 16384/sqrt(2) ≈ 11585
        // 11 kHz would alias to 5 kHz at 16 kHz without the low-pass.
        let stop = Downsampler::new().process(&sine(11000.0, 24000.0, 24000));
        let r = rms(&stop[1000..]);
        assert!(r < 400.0, "11 kHz leaked with rms {r}");
    }

    #[test]
    fn playout_pads_and_clears() {
        let p = Playout::default();
        assert_eq!(p.next_frame(), vec![0; FRAME_SAMPLES]);
        p.push(&vec![1; 1000]);
        assert_eq!(p.buffered(), 1000);
        assert_eq!(p.next_frame(), vec![1; FRAME_SAMPLES]);
        let tail = p.next_frame();
        assert_eq!(tail.len(), FRAME_SAMPLES);
        assert_eq!(&tail[..40], &[1; 40]);
        assert!(tail[40..].iter().all(|&s| s == 0));
        p.push(&vec![1; 5000]);
        p.clear();
        assert_eq!(p.buffered(), 0);
    }
}
