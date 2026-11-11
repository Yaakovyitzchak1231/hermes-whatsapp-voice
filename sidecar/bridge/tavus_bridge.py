#!/usr/bin/env python3
"""Tavus avatar bridge for hermes-whatsapp-voice.

Same job and wire protocol as simli_bridge.py, with a Tavus Phoenix face: it shows emotions while
speaking and listening, and can be told which emotion to show. The sidecar's voice model keeps
the voice and the brain; Tavus renders the face from that audio (echo mode, persona pipecat0).

PROTOCOL (every message: 1-byte type, 4-byte little-endian payload length, payload)
  sidecar -> bridge   0x01 audio    PCM16 LE mono 16 kHz speech for the face to say
                      0x02 clear    the owner interrupted: drop what the face has not said
                      0x03 end      stop and exit
                      0x04 emotion  UTF-8 name: neutral, happiness, sadness, anger, fear,
                                    surprise, disgust or contempt ("" clears it)
                      0x05 camera   one Annex-B H.264 access unit of the owner's camera
  bridge -> sidecar   0x10 ready    the face is live
                      0x11 video    4-byte LE 90 kHz RTP timestamp, then one Annex-B access unit
                      0x12 audio    PCM16 LE mono 16 kHz, lip-synced with the video
                      0x13 snapshot JPEG of the owner's camera, about once a second
Logs go to stderr. The Tavus conversation ends with the call, or at --max-session seconds.
"""

import argparse
import base64
import fractions
import json
import os
import queue
import struct
import sys
import threading
import time
import urllib.request

import av
import numpy as np
from daily import CallClient, Daily, EventHandler

API = "https://tavusapi.com/v2"
IN_RATE = 16000
CHUNK_BYTES = IN_RATE * 2 // 10  # 100 ms per echo message
UTTERANCE_GAP = 0.45  # seconds without speech that close an utterance
SILENCE_PEAK = 24
SPEECH_HANGOVER = 1.0  # seconds of the face's silence still passed on after it spoke
EMOTIONS = {"neutral", "happiness", "sadness", "anger", "fear", "surprise", "disgust", "contempt", ""}

MSG_AUDIO, MSG_CLEAR, MSG_END, MSG_EMOTION, MSG_CAMERA = 0x01, 0x02, 0x03, 0x04, 0x05
OUT_READY, OUT_VIDEO, OUT_AUDIO, OUT_SNAPSHOT = 0x10, 0x11, 0x12, 0x13


def log(msg):
    print(f"tavus-bridge: {msg}", file=sys.stderr, flush=True)


def api(key, method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def is_silent(pcm: bytes) -> bool:
    if not pcm:
        return True
    samples = memoryview(pcm).cast("h")
    return max(max(samples), -min(samples)) < SILENCE_PEAK


def make_encoder(width, height, fps, bitrate_kbps):
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height = width, height
    enc.pix_fmt = "yuv420p"
    enc.framerate = fps
    enc.time_base = fractions.Fraction(1, fps)
    gop = fps
    enc.gop_size = gop
    enc.bit_rate = bitrate_kbps * 1000
    # Mirrors whatsapp-rust's reference encoder: baseline, zerolatency, one slice per frame,
    # SPS/PPS on every IDR, AUD-delimited access units; a keyframe each second for recovery.
    enc.options = {
        "profile": "baseline",
        "level": "3.1",
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x264-params": (
            f"keyint={gop}:min-keyint={gop}:scenecut=0:repeat-headers=1:aud=1"
            f":sliced-threads=0:threads=1:vbv-maxrate={bitrate_kbps}:vbv-bufsize={bitrate_kbps // 2}"
        ),
    }
    enc.open()
    return enc


def portrait(frame, w_out, h_out):
    """Center-crops a landscape I420 frame to the output's portrait shape, on the planes."""
    w, h = frame.width, frame.height
    yuv = np.frombuffer(frame.buffer, np.uint8)
    y = yuv[: w * h].reshape(h, w)
    u = yuv[w * h : w * h * 5 // 4].reshape(h // 2, w // 2)
    v = yuv[w * h * 5 // 4 : w * h * 3 // 2].reshape(h // 2, w // 2)
    crop_w = min(w, (h * w_out // h_out) & ~1)
    x0 = ((w - crop_w) // 2) & ~1
    planes = np.concatenate(
        [
            y[:, x0 : x0 + crop_w].reshape(-1),
            u[:, x0 // 2 : (x0 + crop_w) // 2].reshape(-1),
            v[:, x0 // 2 : (x0 + crop_w) // 2].reshape(-1),
        ]
    )
    vf = av.VideoFrame.from_ndarray(planes.reshape(h * 3 // 2, crop_w), format="yuv420p")
    if (crop_w, h) != (w_out, h_out):
        vf = vf.reformat(width=w_out, height=h_out)
    return vf


class Out:
    """Framed writes to stdout, shared by Daily's callback threads."""

    def __init__(self):
        self.w = sys.stdout.buffer
        self.lock = threading.Lock()

    def send(self, kind, payload=b""):
        with self.lock:
            self.w.write(struct.pack("<BI", kind, len(payload)))
            if payload:
                self.w.write(payload)
            self.w.flush()


def read_messages(q):
    stdin = sys.stdin.buffer

    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = stdin.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    while True:
        header = exact(5)
        if header is None:
            q.put((MSG_END, b""))
            return
        kind, length = struct.unpack("<BI", header)
        payload = exact(length) if length else b""
        if payload is None:
            q.put((MSG_END, b""))
            return
        q.put((kind, payload))
        if kind == MSG_END:
            return


class Face(EventHandler):
    """The Tavus side: the conversation, its Daily room, and the replica's media."""

    def __init__(self, args, out):
        super().__init__()
        self.args, self.out = args, out
        self.key = os.environ["TAVUS_API_KEY"]
        self.conversation_id = None
        self.client = None
        self.replica = None
        self.ready = False
        self.t0 = time.monotonic()
        self.stats = {"frames": 0, "late": 0, "audio_out": 0, "audio_in": 0}
        self.frames: queue.Queue = queue.Queue(maxsize=4)
        self.enc = make_encoder(args.width, args.height, args.fps, args.bitrate)
        self.first_us = None
        self.speech = bytearray()
        self.speech_lock = threading.Lock()
        self.last_speech = 0.0
        self.inference = None
        self.utterances = 0
        self.last_voice = 0.0

    # ---- conversation lifecycle

    def start(self):
        body = {
            "replica_id": self.args.face_id,
            "persona_id": "pipecat0",  # our own audio over conversation.echo
            "conversation_name": "hermes whatsapp video",
            "properties": {
                "max_call_duration": self.args.max_session,
                "participant_left_timeout": 0,
                "participant_absent_timeout": 60,
            },
        }
        conv = api(self.key, "POST", "/conversations", body)
        self.conversation_id = conv["conversation_id"]
        log(f"conversation {self.conversation_id} created in {time.monotonic() - self.t0:.2f}s")
        Daily.init()
        self.client = CallClient(event_handler=self)
        joined = threading.Event()
        self.client.join(
            conv["conversation_url"],
            client_settings={"inputs": {"camera": False, "microphone": False}},
            completion=lambda _data, _err: joined.set(),
        )
        joined.wait(30)
        self.client.set_user_name("hermes")
        deadline = time.monotonic() + 40
        while self.replica is None and time.monotonic() < deadline:
            for pid in self.client.participants():
                if pid != "local":
                    self.attach(pid)
            time.sleep(0.2)
        if self.replica is None:
            raise RuntimeError("the Tavus face never joined")

    def attach(self, pid):
        if self.replica is not None:
            return
        self.replica = pid
        # Highest simulcast layer: the face is cropped to portrait, so it needs the 720p one.
        self.client.update_subscriptions(
            participant_settings={
                pid: {"media": {"camera": {"subscriptionState": "subscribed", "settings": {"video": {"layer": 2}}}}}
            }
        )
        self.client.set_video_renderer(pid, self.on_video, video_source="camera", color_format="I420")
        self.client.set_audio_renderer(
            pid, self.on_audio, audio_source="microphone", sample_rate=IN_RATE, callback_interval_ms=20
        )
        log(f"face joined {time.monotonic() - self.t0:.2f}s after start")

    def on_participant_joined(self, participant):
        if not participant["info"].get("isLocal") and self.client is not None:
            self.attach(participant["id"])

    def stop(self):
        try:
            if self.client is not None:
                self.client.leave()
        except Exception as e:  # noqa: BLE001 - shutting down regardless
            log(f"leave: {e!r}")
        if self.conversation_id:
            try:
                api(self.key, "POST", f"/conversations/{self.conversation_id}/end")
            except Exception as e:  # noqa: BLE001
                log(f"end conversation: {e!r}")
        log(
            f"session ended after {time.monotonic() - self.t0:.1f}s: {self.stats['frames']} frames "
            f"({self.stats['late']} skipped), {self.stats['audio_in']} bytes in, "
            f"{self.stats['audio_out']} bytes out, {self.utterances} utterances"
        )

    def send(self, event, properties=None):
        message = {"message_type": "conversation", "event_type": event, "conversation_id": self.conversation_id}
        if properties is not None:
            message["properties"] = properties
        self.client.send_app_message(message)

    # ---- the replica's media (Daily threads)

    def on_video(self, _pid, frame, _source):
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            self.stats["late"] += 1

    def on_audio(self, _pid, data, _source):
        """The face's lip-synced speech. Silence is passed on only inside speech (its pauses),
        so the call can tell when the face has finished talking."""
        pcm = bytes(data.audio_frames)
        now = time.monotonic()
        if not is_silent(pcm):
            self.last_voice = now
        elif now - self.last_voice > SPEECH_HANGOVER:
            return
        self.out.send(OUT_AUDIO, pcm)
        self.stats["audio_out"] += len(pcm)

    def encode_loop(self):
        """Crops the landscape face to portrait, encodes H.264, stamps it with Tavus's clock."""
        w_out, h_out = self.args.width, self.args.height
        while True:
            frame = self.frames.get()
            if frame is None:
                return
            vf = portrait(frame, w_out, h_out)
            vf.pts = self.stats["frames"]
            self.stats["frames"] += 1
            if self.first_us is None:
                self.first_us = frame.timestamp_us
            ts = struct.pack("<I", ((frame.timestamp_us - self.first_us) * 90 // 1000) & 0xFFFFFFFF)
            for pkt in self.enc.encode(vf):
                self.out.send(OUT_VIDEO, ts + bytes(pkt))
                if pkt.is_keyframe and not self.ready:
                    self.ready = True
                    self.out.send(OUT_READY)
                    log(f"first keyframe {time.monotonic() - self.t0:.2f}s after start")

    # ---- speech for the face

    def say(self, pcm):
        with self.speech_lock:
            self.speech.extend(pcm)
            self.last_speech = time.monotonic()
        self.stats["audio_in"] += len(pcm)

    def clear(self):
        with self.speech_lock:
            self.speech.clear()
            self.inference = None
        self.send("conversation.interrupt")

    def speech_loop(self, stop):
        """Hands speech to Tavus in 100 ms echo messages; a pause closes the utterance."""
        while not stop.is_set():
            time.sleep(0.02)
            with self.speech_lock:
                chunks = []
                while len(self.speech) >= CHUNK_BYTES:
                    chunks.append(bytes(self.speech[:CHUNK_BYTES]))
                    del self.speech[:CHUNK_BYTES]
                idle = time.monotonic() - self.last_speech > UTTERANCE_GAP
                tail = bytes(self.speech) if idle and self.speech else b""
                if tail:
                    self.speech.clear()
                if (chunks or tail) and self.inference is None:
                    self.utterances += 1
                    self.inference = f"u{self.utterances}-{int(time.time() * 1000)}"
                inference = self.inference
                close = idle and inference is not None and not self.speech
                if close:
                    self.inference = None
            for chunk in [*chunks, tail] if tail else chunks:
                self.echo(chunk, False, inference)
            if close:
                self.echo(bytes(IN_RATE * 2 // 25), True, inference)  # 40 ms done marker

    def echo(self, pcm, done, inference):
        self.send(
            "conversation.echo",
            {
                "modality": "audio",
                "inference_id": inference,
                "audio": base64.b64encode(pcm).decode(),
                "done": done,
                "sample_rate": IN_RATE,
            },
        )


class Camera:
    """Decodes the owner's camera and hands the voice model a JPEG about once a second."""

    def __init__(self, out, every=1.0, width=640):
        self.out, self.every, self.width = out, every, width
        self.decoder = av.CodecContext.create("h264", "r")
        self.last = 0.0
        self.q: queue.Queue = queue.Queue(maxsize=30)

    def push(self, au):
        try:
            self.q.put_nowait(au)
        except queue.Full:
            pass

    def loop(self):
        while True:
            au = self.q.get()
            if au is None:
                return
            try:
                frames = [f for pkt in self.decoder.parse(au) for f in self.decoder.decode(pkt)]
            except av.error.FFmpegError:
                continue
            now = time.monotonic()
            if not frames or now - self.last < self.every:
                continue
            self.last = now
            f = frames[-1]
            h = max(2, int(f.height * self.width / f.width) & ~1)
            jpg = av.CodecContext.create("mjpeg", "w")
            jpg.width, jpg.height, jpg.pix_fmt = self.width, h, "yuvj420p"
            jpg.time_base = fractions.Fraction(1, 25)
            jpg.open()
            data = b"".join(bytes(p) for p in jpg.encode(f.reformat(width=self.width, height=h, format="yuvj420p")))
            data += b"".join(bytes(p) for p in jpg.encode(None))
            if data:
                self.out.send(OUT_SNAPSHOT, data)


def serve(args):
    out = Out()
    face = Face(args, out)
    camera = Camera(out)
    messages: queue.Queue = queue.Queue()
    threading.Thread(target=read_messages, args=(messages,), daemon=True).start()
    stop = threading.Event()
    try:
        face.start()
        threading.Thread(target=face.encode_loop, daemon=True).start()
        threading.Thread(target=face.speech_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=camera.loop, daemon=True).start()
        deadline = face.t0 + args.max_session
        while True:
            try:
                kind, payload = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                log("maximum session length reached")
                break
            if kind == MSG_AUDIO:
                face.say(payload)
            elif kind == MSG_CLEAR:
                face.clear()
            elif kind == MSG_EMOTION:
                emotion = payload.decode("utf-8", "replace").strip().lower()
                if emotion in EMOTIONS:
                    face.send("conversation.set_emotion", {"emotion": emotion})
            elif kind == MSG_CAMERA:
                camera.push(payload)
            elif kind == MSG_END:
                break
    finally:
        stop.set()
        face.frames.put(None)
        camera.q.put(None)
        face.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--face-id", required=True, help="Tavus face (replica) id, e.g. r3f4182ef554")
    p.add_argument("--serve", action="store_true", help="live call mode (framed stdin/stdout)")
    p.add_argument("--width", type=int, default=404, help="H.264 output width (even), portrait")
    p.add_argument("--height", type=int, default=720, help="H.264 output height (even)")
    p.add_argument("--fps", type=int, default=25)
    p.add_argument("--bitrate", type=int, default=1200, help="H.264 bitrate cap, kbps")
    p.add_argument("--max-session", type=int, default=900, help="hard cap, seconds")
    args = p.parse_args()
    if not args.serve:
        p.error("only --serve is supported")
    serve(args)


if __name__ == "__main__":
    main()
