#!/usr/bin/env python3
"""Simli avatar bridge for hermes-whatsapp-voice (video-avatar branch).

Reads Gemini-style PCM16 mono audio, streams it to Simli (lip-sync), and writes:
  * H.264 Annex-B video (baseline, zerolatency) to --h264-out
  * Simli's lip-synced audio as PCM16 mono 16 kHz to --audio-out

Offline test mode (--pcm-in FILE) reads a prerecorded file and paces it in real
time. Production mode (sidecar) is started with --stdin-pcm and reads PCM from stdin.
Never leave a session open: the SDK disconnects after --max-session seconds.
"""
import argparse
import asyncio
import os
import sys
import time

import av
from av.audio.resampler import AudioResampler

from simli import SimliClient, SimliConfig

IN_RATE = 16000
CHUNK_BYTES = 960 * 2  # 60 ms of PCM16 mono @16 kHz


async def feed(client, src, realtime=True):
    """Push PCM16 16k mono audio to Simli at real-time pace, then sendSilence."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    sent = 0
    while True:
        chunk = await loop.run_in_executor(None, src.read, CHUNK_BYTES)
        if not chunk:
            break
        await client.send(chunk)
        sent += len(chunk)
        if realtime:
            target = t0 + sent / (IN_RATE * 2)
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
    await client.sendSilence(0.5)
    return sent


async def video_task(client, enc_ctx, out, width, height, stop_evt, counter):
    async for frame in client.getVideoStreamIterator("rgb24"):
        if stop_evt.is_set():
            break
        vf = frame.reformat(width=width, height=height, format="yuv420p")
        vf.pts = counter["n"]
        counter["n"] += 1
        for pkt in enc_ctx.encode(vf):
            out.write(bytes(pkt))
    for pkt in enc_ctx.encode(None):
        out.write(bytes(pkt))


async def audio_task(client, out, stop_evt, counter):
    resampler = AudioResampler(format="s16", layout="mono", rate=IN_RATE)
    async for frame in client.getAudioStreamIterator(targetSampleRate=48000):
        if stop_evt.is_set():
            break
        for f in resampler.resample(frame):
            out.write(bytes(f.planes[0])[: f.samples * 2])
            counter["audio_bytes"] += f.samples * 2


async def run(args):
    api_key = os.environ["SIMLI_API_KEY"]
    cfg = SimliConfig(
        faceId=args.face_id,
        maxSessionLength=args.max_session,
        maxIdleTime=args.max_idle,
        handleSilence=True,
        streaming_width=args.size,
        streaming_height=args.size,
    )
    client = SimliClient(api_key, cfg)

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height = args.width, args.height
    enc.pix_fmt = "yuv420p"
    enc.framerate = args.fps
    enc.time_base = __import__("fractions").Fraction(1, args.fps)
    enc.gop_size = args.fps * 2
    enc.options = {"profile": "baseline", "preset": "veryfast", "tune": "zerolatency",
                   "crf": "26", "x264-params": "repeat-headers=1:keyint=%d" % (args.fps * 2)}
    enc.open()

    src = sys.stdin.buffer if args.stdin_pcm else open(args.pcm_in, "rb")
    stop_evt = asyncio.Event()
    counter = {"n": 0, "audio_bytes": 0}
    stats = {}
    with open(args.h264_out, "wb") as vout, open(args.audio_out, "wb") as aout:
        await client.start()
        vt = asyncio.create_task(video_task(client, enc, vout, args.width, args.height, stop_evt, counter))
        at = asyncio.create_task(audio_task(client, aout, stop_evt, counter))
        t0 = time.time()
        try:
            stats["sent_bytes"] = await asyncio.wait_for(feed(client, src, realtime=not args.fast),
                                                         timeout=args.max_session)
            # let the tail of the avatar render drain
            await asyncio.sleep(args.tail)
        finally:
            stop_evt.set()
            await asyncio.wait_for(asyncio.gather(vt, at, return_exceptions=True), timeout=10)
            await client.stop()
    stats["video_frames"] = counter["n"]
    stats["audio_bytes_out"] = counter["audio_bytes"]
    stats["wall_s"] = round(time.time() - t0, 2)
    print("SIMLI_BRIDGE_OK " + " ".join(f"{k}={v}" for k, v in stats.items()), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--face-id", required=True)
    p.add_argument("--pcm-in", help="PCM16 mono 16k file")
    p.add_argument("--stdin-pcm", action="store_true")
    p.add_argument("--h264-out", required=True)
    p.add_argument("--audio-out", required=True)
    p.add_argument("--size", type=int, default=512, help="Simli stream size (square), >=512")
    p.add_argument("--width", type=int, default=480)
    p.add_argument("--height", type=int, default=640)
    p.add_argument("--fps", type=int, default=20)
    p.add_argument("--max-session", type=int, default=60)
    p.add_argument("--max-idle", type=int, default=20)
    p.add_argument("--tail", type=float, default=1.0)
    p.add_argument("--fast", action="store_true", help="don't pace input (tests only)")
    args = p.parse_args()
    if not args.stdin_pcm and not args.pcm_in:
        p.error("need --pcm-in or --stdin-pcm")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
