"""Tool schemas and handlers."""

from __future__ import annotations

import json
import time

from .sidecar_client import Client, SidecarError, settings

CALL_OWNER = {
    "name": "whatsapp_call_owner",
    "description": (
        "Place a live WhatsApp voice call to the owner. A voice agent with access to Hermes "
        "talks with them in real time. It always calls the owner's own number; there is no "
        "way to call anyone else. Use it for things that are important or time-sensitive "
        "enough to interrupt the owner, such as a top-priority reminder, an urgent alert, or "
        "when the owner asked to be called. Returns once the call is answered or has "
        "clearly not been answered. When it is not answered, fall back to another channel, "
        "for example an urgent reminder or a message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "What the call is about, in one or two sentences. The voice agent opens the call with this.",
            },
            "details": {
                "type": "string",
                "description": "Optional background the voice agent should know during the call (task details, deadlines, options).",
            },
            "video": {
                "type": "boolean",
                "description": "Video call with the live avatar (true) or voice only (false). Leave it out to use the owner's default.",
            },
        },
        "required": ["reason"],
    },
}

CALL_STATUS = {
    "name": "whatsapp_call_status",
    "description": (
        "Get the status and transcript of a WhatsApp voice call. Pass the call_id that "
        "whatsapp_call_owner returned. Leave it out to list the most recent calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "description": "Call id from whatsapp_call_owner"},
        },
    },
}

# Statuses that mean the ringing phase is over.
_SETTLED = {"in_progress", "completed", "no_answer", "rejected", "failed"}

_FALLBACK_HINT = (
    "The owner did not pick up. If this matters, use another channel now, for example an urgent reminder or a message."
)


def _result(**fields) -> str:
    return json.dumps(fields)


def make_handlers(ctx):
    def call_owner(args: dict, **kwargs) -> str:
        reason = (args.get("reason") or "").strip()
        if not reason:
            return _result(error="reason is required")
        try:
            client = Client.from_ctx(ctx)
            video = args.get("video")
            call = client.start_call(
                reason, (args.get("details") or "").strip(), video if isinstance(video, bool) else None
            )
            deadline = time.monotonic() + int(settings(ctx)["ring_timeout_seconds"]) + 20
            while call.get("status") not in _SETTLED and time.monotonic() < deadline:
                time.sleep(1)
                call = client.get_call(call["id"])
        except SidecarError as e:
            return _result(error=str(e), hint="Could not place the call. Use another channel to reach the owner.")

        status = call.get("status")
        out = {"call_id": call.get("id"), "status": status}
        if status == "in_progress":
            out["note"] = (
                "The owner answered and is talking with the voice agent now. A summary is "
                "sent to WhatsApp when the call ends; use whatsapp_call_status for the transcript."
            )
        elif status == "completed":
            out["transcript"] = call.get("transcript", [])
        elif status in ("no_answer", "rejected"):
            out["note"] = _FALLBACK_HINT
        elif status == "failed":
            out["error"] = call.get("end_reason", "call failed")
            out["note"] = _FALLBACK_HINT
        else:
            out["note"] = "Still ringing past the timeout; check again with whatsapp_call_status."
        return _result(**out)

    def call_status(args: dict, **kwargs) -> str:
        try:
            client = Client.from_ctx(ctx)
            call_id = (args.get("call_id") or "").strip()
            if call_id:
                return _result(call=client.get_call(call_id))
            return _result(calls=client.recent_calls())
        except SidecarError as e:
            return _result(error=str(e))

    return call_owner, call_status
