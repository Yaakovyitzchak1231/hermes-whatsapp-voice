#!/usr/bin/env python3
"""Simli avatar bridge for hermes-whatsapp-voice.

The sidecar sends the voice model's speech (PCM16 mono 16 kHz); Simli turns it into a lip-synced
talking face. This process returns H.264 access units for the WhatsApp video call and Simli's
own copy of the audio, which is what the call plays so voice and lips stay in step.

Modes:
  --serve        live call: framed messages on stdin/stdout (see PROTOCOL below)
  --pcm-in FILE  offline test: render FILE to --h264-out / --audio-out

PROTOCOL (every message: 1-byte type, 4-byte little-endian payload length, payload)
  sidecar -> bridge   0x01 audio   PCM16 LE mono 16 kHz
                      0x02 clear   (no payload) drop queued speech: the owner interrupted
                      0x03 end     (no payload) stop and exit
  bridge -> sidecar   0x10 ready   (no payload) Simli connected, video is flowing
                      0x11 video   4-byte LE 90 kHz RTP timestamp, then one Annex-B access unit
                      0x12 audio   PCM16 LE mono 16 kHz, lip-synced with the video (silence dropped)
Logs go to stderr. The Simli session ends when the call does (end / stdin EOF) or at
--max-session seconds, so a stuck call can never burn the account's minutes.
"""

import argparse
import asyncio
import fractions
import os
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import av
from av.audio.resampler import AudioResampler
from simli import SimliClient, SimliConfig

IN_RATE = 16000
CHUNK_BYTES = 960 * 2  # 60 ms of PCM16 mono @16 kHz

MSG_AUDIO, MSG_CLEAR, MSG_END = 0x01, 0x02, 0x03
OUT_READY, OUT_VIDEO, OUT_AUDIO = 0x10, 0x11, 0x12

# Simli pads quiet stretches with digital silence. Frames this quiet are not forwarded: the
# sidecar's playout fills gaps with silence on its own real-time clock, so dropping them keeps the
# timing and lets the sidecar tell "the assistant is speaking" from "the line is idle".
SILENCE_PEAK = 24


def log(msg):
    print(f"simli-bridge: {msg}", file=sys.stderr, flush=True)


def make_encoder(width, height, fps, bitrate_kbps):
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height = width, height
    enc.pix_fmt = "yuv420p"
    enc.framerate = fps
    enc.time_base = fractions.Fraction(1, fps)
    gop = fps  # one keyframe a second: fast recovery after loss, since PLIs can't reach us
    enc.gop_size = gop
    enc.bit_rate = bitrate_kbps * 1000
    # Mirrors whatsapp-rust's reference encoder (examples/voip-cli/src/video.rs): baseline,
    # zerolatency, one slice per frame, SPS/PPS on every IDR, AUD-delimited access units.
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


def is_silent(pcm: bytes) -> bool:
    if not pcm:
        return True
    samples = memoryview(pcm).cast("h")
    return max(max(samples), -min(samples)) < SILENCE_PEAK


def simli_config(args):
    return SimliConfig(
        faceId=args.face_id,
        maxSessionLength=args.max_session,
        maxIdleTime=args.max_idle,
        handleSilence=True,
        streaming_width=args.stream_width,
        streaming_height=args.stream_height,
        avatar_position_x=0.5,
        avatar_position_y=1.0,
    )


# ---------------------------------------------------------------- live call (--serve)


class Out:
    """Serialised writes to stdout from several tasks."""

    def __init__(self):
        self.w = sys.stdout.buffer

    def send(self, kind, payload=b""):
        self.w.write(struct.pack("<BI", kind, len(payload)))
        if payload:
            self.w.write(payload)
        self.w.flush()


async def read_messages(queue):
    """Reads framed messages from stdin in a thread (stdin is a pipe from the sidecar)."""
    loop = asyncio.get_running_loop()
    stdin = sys.stdin.buffer

    def read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = stdin.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    while True:
        header = await loop.run_in_executor(None, read_exact, 5)
        if header is None:
            await queue.put((MSG_END, b""))
            return
        kind, length = struct.unpack("<BI", header)
        payload = b""
        if length:
            payload = await loop.run_in_executor(None, read_exact, length)
            if payload is None:
                await queue.put((MSG_END, b""))
                return
        await queue.put((kind, payload))
        if kind == MSG_END:
            return


async def serve(args):
    out = Out()
    client = SimliClient(os.environ["SIMLI_API_KEY"], simli_config(args))
    enc = make_encoder(args.width, args.height, args.fps, args.bitrate)
    # One worker keeps frames in order; encoding off the event loop keeps aiortc's RTP receive
    # (Simli's audio and video) from starving on a busy host.
    encoder = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    reader = asyncio.create_task(read_messages(queue))
    t0 = time.monotonic()
    stats = {"frames": 0, "late": 0, "audio_out": 0, "audio_in": 0}
    stop = asyncio.Event()
    speech = bytearray()  # voice-model audio not yet handed to Simli

    await client.start()
    log(f"Simli session started in {time.monotonic() - t0:.2f}s")
    await client.sendSilence()

    def encode(vf):
        return [(bytes(pkt), pkt.is_keyframe) for pkt in enc.encode(vf)]

    async def video():
        started = None
        ready_sent = False
        pending = None
        async for frame in client.getVideoStreamIterator("yuv420p"):
            if stop.is_set():
                break
            now = time.monotonic()
            if started is None:
                started = now
            if pending is not None and not pending.done():
                # The encoder is behind: skip this frame rather than queue latency.
                stats["late"] += 1
                continue
            if frame.width != args.width or frame.height != args.height:
                frame = frame.reformat(width=args.width, height=args.height, format="yuv420p")
            frame.pts = stats["frames"]
            stats["frames"] += 1
            # Capture time on the 90 kHz RTP clock: frames go out as Simli delivers them, so
            # WhatsApp plays them at Simli's real pace even if it isn't exactly --fps.
            ts = struct.pack("<I", int((now - started) * 90_000) & 0xFFFFFFFF)
            pending = loop.run_in_executor(encoder, encode, frame)

            def sent(fut, ts=ts):
                nonlocal ready_sent
                if fut.cancelled() or fut.exception():
                    return
                for data, key in fut.result():
                    out.send(OUT_VIDEO, ts + data)
                    if key and not ready_sent:
                        out.send(OUT_READY)
                        ready_sent = True
                        log(f"first keyframe {time.monotonic() - t0:.2f}s after start")

            pending.add_done_callback(sent)

    async def audio():
        resampler = AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        async for frame in client.getAudioStreamIterator(targetSampleRate=48000):
            if stop.is_set():
                break
            for f in resampler.resample(frame):
                pcm = bytes(f.planes[0])[: f.samples * 2]
                if not is_silent(pcm):
                    out.send(OUT_AUDIO, pcm)
                    stats["audio_out"] += len(pcm)

    async def pace():
        """Hands speech to Simli at real-time pace with a short lead. The voice model produces
        speech in bursts faster than real time; Simli renders it live and drops what it is fed
        far ahead of playback."""
        lead = int(IN_RATE * 2 * args.lead)
        clock = None  # (monotonic start, bytes sent since start)
        while not stop.is_set():
            await asyncio.sleep(0.02)
            if not speech:
                clock = None
                continue
            now = time.monotonic()
            if clock is None:
                clock = (now, 0)
            began, given = clock
            allowed = int((now - began) * IN_RATE * 2) + lead - given
            allowed -= allowed % 2
            if allowed <= 0:
                continue
            chunk = bytes(speech[:allowed])
            del speech[: len(chunk)]
            clock = (began, given + len(chunk))
            await client.send(chunk)

    tasks = [asyncio.create_task(video()), asyncio.create_task(audio()), asyncio.create_task(pace())]
    getter = None
    try:
        while True:
            if getter is None:
                getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                [getter, *tasks],
                timeout=max(0.0, args.max_session - (time.monotonic() - t0)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                log("maximum session length reached")
                break
            for t in tasks:
                if t in done:
                    raise RuntimeError(f"Simli stream ended: {t.exception()!r}")
            kind, payload = getter.result()
            getter = None
            if kind == MSG_AUDIO:
                stats["audio_in"] += len(payload)
                speech.extend(payload)
            elif kind == MSG_CLEAR:
                speech.clear()
                await client.clearBuffer()
            elif kind == MSG_END:
                break
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        if getter is not None:
            getter.cancel()
        reader.cancel()
        encoder.shutdown(wait=False, cancel_futures=True)
        try:
            await asyncio.wait_for(client.stop(), timeout=5)
        except Exception as e:  # noqa: BLE001 - shutting down regardless
            log(f"stop: {e!r}")
        log(
            f"session ended after {time.monotonic() - t0:.1f}s: {stats['frames']} frames "
            f"({stats['late']} skipped), {stats['audio_in']} bytes in, {stats['audio_out']} bytes out"
        )


# ---------------------------------------------------------------- offline test (--pcm-in)


async def feed(client, src, realtime=True):
    """Push PCM16 16k mono audio to Simli at real-time pace, then a little silence."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    sent = 0
    while True:
        chunk = src.read(CHUNK_BYTES)
        if not chunk:
            break
        await client.send(chunk)
        sent += len(chunk)
        if realtime:
            delay = t0 + sent / (IN_RATE * 2) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
    await client.sendSilence(0.5)
    return sent


async def render(args, src, vout, aout):
    client = SimliClient(os.environ["SIMLI_API_KEY"], simli_config(args))
    enc = make_encoder(args.width, args.height, args.fps, args.bitrate)
    stop = asyncio.Event()
    stats = {"frames": 0, "audio_bytes": 0}
    await client.start()

    async def video():
        async for frame in client.getVideoStreamIterator("rgb24"):
            if stop.is_set():
                break
            vf = frame.reformat(width=args.width, height=args.height, format="yuv420p")
            vf.pts = stats["frames"]
            stats["frames"] += 1
            for pkt in enc.encode(vf):
                vout.write(bytes(pkt))

    async def audio():
        resampler = AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        async for frame in client.getAudioStreamIterator(targetSampleRate=48000):
            if stop.is_set():
                break
            for f in resampler.resample(frame):
                aout.write(bytes(f.planes[0])[: f.samples * 2])
                stats["audio_bytes"] += f.samples * 2

    tasks = [asyncio.create_task(video()), asyncio.create_task(audio())]
    t0 = time.time()
    try:
        stats["sent_bytes"] = await asyncio.wait_for(feed(client, src), timeout=args.max_session)
        await asyncio.sleep(args.tail)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await client.stop()
    stats["wall_s"] = round(time.time() - t0, 2)
    print("SIMLI_BRIDGE_OK " + " ".join(f"{k}={v}" for k, v in stats.items()), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--face-id", required=True)
    p.add_argument("--serve", action="store_true", help="live call mode (framed stdin/stdout)")
    p.add_argument("--pcm-in", help="offline test: PCM16 mono 16k file")
    p.add_argument("--h264-out")
    p.add_argument("--audio-out")
    # Simli Trinity-2 canvas: width >=512, aspect 1:1, 16:9 or 9:16. Default portrait 9:16.
    p.add_argument("--stream-width", type=int, default=576)
    p.add_argument("--stream-height", type=int, default=1024)
    p.add_argument("--width", type=int, default=576, help="H.264 output width (even)")
    p.add_argument("--height", type=int, default=1024, help="H.264 output height (even)")
    p.add_argument("--fps", type=int, default=20)
    p.add_argument("--bitrate", type=int, default=1000, help="H.264 bitrate cap, kbps")
    p.add_argument("--max-session", type=int, default=900, help="hard cap, seconds")
    p.add_argument("--max-idle", type=int, default=300)
    p.add_argument("--tail", type=float, default=1.0)
    p.add_argument("--lead", type=float, default=0.4, help="seconds of speech Simli may hold ahead")
    args = p.parse_args()
    if args.serve:
        asyncio.run(serve(args))
    elif args.pcm_in and args.h264_out and args.audio_out:
        with open(args.pcm_in, "rb") as src, open(args.h264_out, "wb") as vout, open(args.audio_out, "wb") as aout:
            asyncio.run(render(args, src, vout, aout))
    else:
        p.error("need --serve, or --pcm-in with --h264-out and --audio-out")


if __name__ == "__main__":
    main()
