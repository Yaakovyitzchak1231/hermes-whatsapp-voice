#!/usr/bin/env python3
"""Tavus full-pipeline bridge for hermes-whatsapp-voice.

Here the Tavus PAL holds the whole conversation: it hears the owner (speech recognition and turn
taking), sees their camera (Raven perception), thinks (a Tavus-hosted LLM steered by Hermy's
persona), and answers with its own voice and face, rendered together. Hermes stays the agent
behind it: the PAL's ask_hermes tool comes back to the sidecar, which runs it on Hermes.

PROTOCOL (every message: 1-byte type, 4-byte little-endian payload length, payload)
  sidecar -> bridge   0x01 audio    the owner's speech, PCM16 LE mono 16 kHz
                      0x03 end      stop and exit
                      0x05 camera   1 orientation byte (0..3), then one Annex-B H.264 access unit
                                    of the owner's camera
                      0x06 context  JSON {"system_prompt", "context"}; must come first: the PAL and
                                    the conversation are created from it
                      0x07 result   JSON {"id", "output", "status": "success"|"error"}
                      0x08 start    the owner answered: join, which makes the face say hello
  bridge -> sidecar   0x10 ready    the face is live
                      0x11 video    4-byte LE 90 kHz RTP timestamp, then one Annex-B access unit
                      0x12 audio    the face's speech, PCM16 LE mono 16 kHz, in sync with the video
                      0x14 said     JSON {"role": "owner"|"assistant", "text"}
                      0x15 tool     JSON {"id", "name", "args"}
                      0x16 hangup   the face ended the call
Logs go to stderr. The conversation and its PAL are deleted when the call ends.
"""

import argparse
import json
import os
import queue
import struct
import threading
import time

import av
import numpy as np
from daily import CallClient, Daily, EventHandler
from tavus_bridge import (
    IN_RATE,
    MSG_AUDIO,
    MSG_CAMERA,
    MSG_END,
    OUT_AUDIO,
    OUT_READY,
    OUT_VIDEO,
    Out,
    api,
    log,
    make_encoder,
    portrait,
    read_messages,
)

MSG_CONTEXT, MSG_RESULT, MSG_START = 0x06, 0x07, 0x08
OUT_SAID, OUT_TOOL, OUT_HANGUP = 0x14, 0x15, 0x16
PAL_NAME = "hermes-whatsapp-video"
CAMERA_W, CAMERA_H, CAMERA_FPS = 480, 640, 10
SPEECH_NOTE = (
    "\n\nTHIS OVERRIDES ANY STYLE NOTE ABOVE: every character you write is read aloud by a speech "
    "voice, so never write stage directions, sound effects or anything between asterisks (no *sigh*, "
    "*sniffle*, *cough*). Show sighs, groans and moods with your face and with spoken words like "
    "'ugh', 'hmm' or 'oh man' instead. You can see the owner through their camera; react to what you "
    "see when it matters, the way a person on a video call would."
)
ASK_HERMES = {
    "name": "ask_hermes",
    "description": (
        "Hand a request to Hermes, the owner's full agent with memory and tools (calendar, "
        "reminders, messages, notes, web, home). Use it for any action or any information you do "
        "not have. Never make up results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "A complete, self-contained instruction or question for Hermes.",
            }
        },
        "required": ["request"],
    },
    "origin": "llm",
    "on_call": "generate_filler",
    "on_resolve": "generate_response",
    "delivery": {"app_message": True},
}


def ensure_tool(key):
    tools = api(key, "GET", "/tools?limit=100").get("data", [])
    tool = next((t for t in tools if t.get("name") == ASK_HERMES["name"]), None)
    return (tool or api(key, "POST", "/tools", ASK_HERMES))["tool_id"]


def delete_stale_pals(key):
    """PALs this bridge left behind (a crash before cleanup): they only clutter the account."""
    try:
        for pal in api(key, "GET", "/pals?limit=100").get("data", []):
            if pal.get("pal_name") == PAL_NAME:
                api(key, "DELETE", f"/pals/{pal.get('pal_id') or pal.get('persona_id')}")
    except Exception as e:  # noqa: BLE001 - housekeeping only
        log(f"stale PAL cleanup: {e!r}")


class Pal(EventHandler):
    def __init__(self, args, out, key):
        super().__init__()
        self.args, self.out, self.key = args, out, key
        self.t0 = time.monotonic()
        self.pal_id = self.conversation_id = self.url = None
        self.client = self.replica = None
        self.ready = False
        self.frames: queue.Queue = queue.Queue(maxsize=4)
        self.enc = make_encoder(args.width, args.height, args.fps, args.bitrate)
        self.first_us = None
        self.stats = {"frames": 0, "late": 0, "heard": 0, "said": 0, "tools": 0}
        self.mic = self.cam = None
        self.hung_up = False
        self.last_said = {}
        self.sizes = {}
        self.outgoing: queue.Queue = queue.Queue()
        self.dump = Dump(dump_folder())

    # ---- setup

    def create(self, ctx):
        delete_stale_pals(self.key)
        tool_id = ensure_tool(self.key)
        pal = api(
            self.key,
            "POST",
            "/pals",
            {
                "pal_name": PAL_NAME,
                "system_prompt": ctx["system_prompt"] + SPEECH_NOTE,
                "pipeline_mode": "full",
                "default_face_id": self.args.face_id,
                "disclosure_type": "off",
                "layers": {
                    "perception": {"perception_model": "raven-2", "callouts": "off"},
                    "llm": {"model": self.args.llm},
                    "conversational_flow": {"turn_detection_model": "sparrow-2"},
                },
            },
        )
        self.pal_id = pal.get("pal_id") or pal.get("persona_id")
        api(self.key, "POST", f"/pals/{self.pal_id}/tools", {"tool_ids": [tool_id]})
        conv = api(
            self.key,
            "POST",
            "/conversations",
            {
                "pal_id": self.pal_id,
                "face_id": self.args.face_id,
                "conversation_name": "hermes whatsapp video",
                "conversational_context": ctx.get("context", ""),
                "dynamic_greeting": True,
                "properties": {
                    "max_call_duration": self.args.max_session,
                    "participant_left_timeout": 0,
                    "participant_absent_timeout": 120,
                },
            },
        )
        self.conversation_id, self.url = conv["conversation_id"], conv["conversation_url"]
        Daily.init()
        self.mic = Daily.create_microphone_device("owner-mic", sample_rate=IN_RATE, channels=1, non_blocking=True)
        self.cam = Daily.create_camera_device("owner-cam", width=CAMERA_W, height=CAMERA_H, color_format="I420")
        self.client = CallClient(event_handler=self)
        log(f"conversation {self.conversation_id} ready {time.monotonic() - self.t0:.2f}s after start")

    def join(self):
        joined = threading.Event()
        self.client.join(
            self.url,
            client_settings={
                "inputs": {
                    "camera": {"isEnabled": True, "settings": {"deviceId": "owner-cam"}},
                    "microphone": {"isEnabled": True, "settings": {"deviceId": "owner-mic"}},
                }
            },
            completion=lambda _d, _e: joined.set(),
        )
        joined.wait(30)
        self.client.set_user_name("owner")
        deadline = time.monotonic() + 30
        while self.replica is None and time.monotonic() < deadline:
            for pid in self.client.participants():
                if pid != "local":
                    self.attach(pid)
            time.sleep(0.1)
        if self.replica is None:
            raise RuntimeError("the Tavus face never joined")

    def attach(self, pid):
        if self.replica is not None:
            return
        self.replica = pid
        # The face is cropped to portrait, so it needs the top simulcast layer, not what the SFU
        # picks for a busy receiver.
        self.client.update_subscriptions(
            participant_settings={
                pid: {
                    "media": {"camera": {"subscriptionState": "subscribed", "receiveSettings": {"maxQuality": "high"}}}
                }
            }
        )
        self.client.set_video_renderer(pid, self.on_video, video_source="camera", color_format="I420")
        self.client.set_audio_renderer(
            pid, self.on_audio, audio_source="microphone", sample_rate=IN_RATE, callback_interval_ms=20
        )
        log(f"face attached {time.monotonic() - self.t0:.2f}s after start")

    def stop(self):
        try:
            if self.client is not None:
                self.client.leave()
        except Exception as e:  # noqa: BLE001
            log(f"leave: {e!r}")
        cleanup = []
        if self.conversation_id:
            cleanup.append(("POST", f"/conversations/{self.conversation_id}/end"))
        if self.pal_id:
            cleanup.append(("DELETE", f"/pals/{self.pal_id}"))
        for method, path in cleanup:
            try:
                api(self.key, method, path)
            except Exception as e:  # noqa: BLE001 - shutting down regardless
                log(f"{method} {path}: {e!r}")
        self.dump.close()
        log(f"face video sizes: {self.sizes}")
        log(
            f"session ended after {time.monotonic() - self.t0:.1f}s: {self.stats['frames']} frames "
            f"({self.stats['late']} skipped), {self.stats['heard']} bytes heard, {self.stats['said']} bytes said, "
            f"{self.stats['tools']} tool calls"
        )

    def send(self, event, properties):
        self.client.send_app_message(
            {
                "message_type": "conversation",
                "event_type": event,
                "conversation_id": self.conversation_id,
                "properties": properties,
            }
        )

    # ---- Daily callbacks

    def on_participant_joined(self, participant):
        if not participant["info"].get("isLocal") and self.client is not None:
            self.attach(participant["id"])

    def on_participant_left(self, participant, _reason):
        if participant.get("id") == self.replica:
            self.hangup("the face left the room")

    def on_app_message(self, message, _sender):
        if not isinstance(message, dict):
            return
        event, props = message.get("event_type", ""), message.get("properties") or {}
        if event == "conversation.utterance" and props.get("speech"):
            role = {"user": "owner", "pal": "assistant"}.get(props.get("role"))
            if role:  # "replica" duplicates "pal"
                text = props["speech"]
                # An utterance cut short and then finished arrives again in full: send only what is new.
                last = self.last_said.get(role, "")
                self.last_said[role] = text
                if last and text.startswith(last):
                    text = text[len(last) :]
                if text.strip():
                    self.out.send(OUT_SAID, json.dumps({"role": role, "text": text.strip()}).encode())
        elif event == "conversation.tool_call":
            name = props.get("name", "")
            try:
                args = json.loads(props.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            args.pop("response_to_user", None)
            if name == "end_call":
                self.hangup("the face said goodbye")
            elif props.get("tool_call_id"):
                self.stats["tools"] += 1
                self.out.send(OUT_TOOL, json.dumps({"id": props["tool_call_id"], "name": name, "args": args}).encode())
        elif event == "system.shutdown":
            self.hangup(f"Tavus ended the conversation ({props.get('reason', '')})")

    def hangup(self, why):
        if not self.hung_up:
            self.hung_up = True
            log(why)
            self.out.send(OUT_HANGUP)

    def on_video(self, _pid, frame, _source):
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            self.stats["late"] += 1

    def on_audio(self, _pid, data, _source):
        pcm = bytes(data.audio_frames)
        if pcm:
            self.dump.audio(pcm)
            self.out.send(OUT_AUDIO, pcm)
            self.stats["said"] += len(pcm)

    def send_loop(self):
        """Lets each frame go `--video-delay-ms` after it was encoded. The face's speech reaches the
        phone later than its picture (Daily hands the audio over later, and the call plays it out
        in 60 ms frames), so the picture waits for it: measured 120 to 180 ms at the bridge."""
        delay = self.args.video_delay_ms / 1000
        while True:
            item = self.outgoing.get()
            if item is None:
                return
            due, payload = item
            wait = due + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.dump.video(payload[4:])
            self.out.send(OUT_VIDEO, payload)

    def encode_loop(self):
        while True:
            frame = self.frames.get()
            if frame is None:
                return
            size = f"{frame.width}x{frame.height}"
            self.sizes[size] = self.sizes.get(size, 0) + 1
            vf = portrait(frame, self.args.width, self.args.height)
            vf.pts = self.stats["frames"]
            self.stats["frames"] += 1
            if self.first_us is None:
                self.first_us = frame.timestamp_us
            ts = struct.pack("<I", ((frame.timestamp_us - self.first_us) * 90 // 1000) & 0xFFFFFFFF)
            for pkt in self.enc.encode(vf):
                self.outgoing.put((time.monotonic(), ts + bytes(pkt)))
                if pkt.is_keyframe and not self.ready:
                    self.ready = True
                    self.out.send(OUT_READY)
                    log(f"face live {time.monotonic() - self.t0:.2f}s after start")

    # ---- the owner, into the room

    def hear(self, pcm):
        self.mic.write_frames(pcm)
        self.stats["heard"] += len(pcm)


def dump_folder():
    """HWV_AVATAR_DUMP, or a fresh folder per call while `$HWV_DATA_DIR/avatar-dump` exists."""
    if os.environ.get("HWV_AVATAR_DUMP"):
        return os.environ["HWV_AVATAR_DUMP"]
    data = os.environ.get("HWV_DATA_DIR", "")
    if data and os.path.exists(os.path.join(data, "avatar-dump")):
        return os.path.join(data, "avatar-dumps", time.strftime("%Y%m%d-%H%M%S"))
    return ""


class Dump:
    """Debug: with HWV_AVATAR_DUMP set to a directory, keeps what the face sent this call (H.264,
    PCM, and when each piece left), so lip sync can be measured after the call."""

    def __init__(self, folder):
        self.on = bool(folder)
        if not self.on:
            return
        os.makedirs(folder, exist_ok=True)
        self.t0 = time.monotonic()
        self.h264 = open(os.path.join(folder, "face.h264"), "wb")  # noqa: SIM115 - closed in close()
        self.pcm = open(os.path.join(folder, "face_audio.pcm"), "wb")  # noqa: SIM115
        self.times = open(os.path.join(folder, "face_times.jsonl"), "w")  # noqa: SIM115
        self.lock = threading.Lock()

    def _mark(self, kind, n):
        self.times.write(json.dumps([round(time.monotonic() - self.t0, 4), kind, n]) + "\n")

    def video(self, au):
        if self.on:
            with self.lock:
                self.h264.write(au)
                self._mark("v", len(au))

    def audio(self, pcm):
        if self.on:
            with self.lock:
                self.pcm.write(pcm)
                self._mark("a", len(pcm))

    def close(self):
        if self.on:
            with self.lock:
                for f in (self.h264, self.pcm, self.times):
                    f.close()
                self.on = False


def fit_camera(frame, orientation):
    """The owner's camera, upright and letterboxed into the virtual camera's portrait frame."""
    img = frame.to_ndarray(format="rgb24")
    if orientation:
        # Quarter turns. Measured on a live call: orientation 1 turned the other way came out
        # upside down, so the phone's turns count counter-clockwise in numpy's sense.
        img = np.rot90(img, k=orientation)
    h, w = img.shape[:2]
    scale = min(CAMERA_W / w, CAMERA_H / h)
    nw, nh = max(2, int(w * scale) & ~1), max(2, int(h * scale) & ~1)
    small = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format="rgb24").reformat(width=nw, height=nh)
    canvas = np.zeros((CAMERA_H, CAMERA_W, 3), np.uint8)
    y0, x0 = (CAMERA_H - nh) // 2, (CAMERA_W - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = small.to_ndarray(format="rgb24")
    i420 = av.VideoFrame.from_ndarray(canvas, format="rgb24").reformat(format="yuv420p")
    return i420.to_ndarray().tobytes()  # (h * 3 / 2, w): the Y, U and V planes, packed


def camera_loop(pal, q):
    decoder = av.CodecContext.create("h264", "r")
    last, orientations = 0.0, set()
    while True:
        item = q.get()
        if item is None:
            return
        orientation, au = item
        if orientation not in orientations:
            orientations.add(orientation)
            log(f"owner camera orientation {orientation}")
        try:
            frames = [f for pkt in decoder.parse(au) for f in decoder.decode(pkt)]
        except av.error.FFmpegError:
            continue
        now = time.monotonic()
        if frames and now - last >= 1 / CAMERA_FPS:
            last = now
            pal.cam.write_frame(fit_camera(frames[-1], orientation))


def serve(args):
    out = Out()
    pal = Pal(args, out, os.environ["TAVUS_API_KEY"])
    messages: queue.Queue = queue.Queue()
    threading.Thread(target=read_messages, args=(messages,), daemon=True).start()
    camera_q: queue.Queue = queue.Queue(maxsize=30)
    try:
        kind, payload = messages.get(timeout=30)
        if kind != MSG_CONTEXT:
            raise RuntimeError(f"expected the call context first, got message {kind:#x}")
        pal.create(json.loads(payload))
        threading.Thread(target=pal.encode_loop, daemon=True).start()
        threading.Thread(target=pal.send_loop, daemon=True).start()
        threading.Thread(target=camera_loop, args=(pal, camera_q), daemon=True).start()
        started = False
        deadline = pal.t0 + args.max_session
        while True:
            try:
                kind, payload = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                log("maximum session length reached")
                break
            if kind == MSG_START and not started:
                started = True
                pal.join()
            elif kind == MSG_AUDIO and started:
                pal.hear(payload)
            elif kind == MSG_CAMERA and started and len(payload) > 1:
                try:
                    camera_q.put_nowait((payload[0] & 3, payload[1:]))
                except queue.Full:
                    pass
            elif kind == MSG_RESULT and started:
                r = json.loads(payload)
                pal.send(
                    "conversation.tool_result",
                    {"tool_call_id": r["id"], "output": r.get("output", ""), "status": r.get("status", "success")},
                )
            elif kind == MSG_END:
                break
    finally:
        pal.frames.put(None)
        pal.outgoing.put(None)
        camera_q.put(None)
        pal.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--face-id", required=True, help="Tavus face id, e.g. rccbeedfc684")
    p.add_argument("--serve", action="store_true", help="live call mode (framed stdin/stdout)")
    p.add_argument("--llm", default="tavus-gemma", help="Tavus-hosted model behind the face")
    p.add_argument("--width", type=int, default=404)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=25)
    p.add_argument("--bitrate", type=int, default=1200)
    p.add_argument("--max-session", type=int, default=900)
    p.add_argument(
        "--video-delay-ms",
        type=int,
        default=int(os.environ.get("HWV_AVATAR_VIDEO_DELAY_MS") or 180),
        help="hold the picture back this long so the voice catches up (lip sync)",
    )
    args = p.parse_args()
    if not args.serve:
        p.error("only --serve is supported")
    serve(args)


if __name__ == "__main__":
    main()
