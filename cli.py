"""`hermes whatsapp-voice ...` subcommands: install, pair, serve, service, status, call, calls, stats."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .sidecar_client import (
    BINARY_NAME,
    Client,
    SidecarError,
    binary_path,
    data_dir,
    download_release,
    gemini_key,
    settings,
    sidecar_env,
    stats_path,
)

PLUGIN_DIR = Path(__file__).resolve().parent


def _build_from_source(dest: Path) -> None:
    cargo = shutil.which("cargo")
    if not cargo:
        raise SystemExit(
            "Rust is not installed (https://rustup.rs; cmake and a C compiler are also needed). "
            "Or drop --from-source to download a release build."
        )
    src = PLUGIN_DIR / "sidecar"
    print(f"Building the sidecar from {src} (takes a few minutes) ...")
    subprocess.run([cargo, "build", "--release", "--locked"], cwd=src, check=True)
    shutil.copy2(src / "target" / "release" / BINARY_NAME, dest)


def _download_release(dest: Path) -> None:
    try:
        download_release(dest)
    except SidecarError as e:
        raise SystemExit(str(e)) from e


def _install(args) -> None:
    dest = binary_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if args.from_source:
        _build_from_source(dest)
    else:
        _download_release(dest)
    print(f"Installed {dest}")
    print("Next: hermes whatsapp-voice pair")


def _require_ready(ctx) -> Path:
    if not settings(ctx)["owner_number"]:
        raise SystemExit(
            "Set your number first: hermes config set plugins.entries.hermes-whatsapp-voice.settings.owner_number +15551234567"
        )
    if not gemini_key():
        raise SystemExit(
            "No Gemini API key found. This plugin reuses the key of Hermes's Gemini provider "
            "(GOOGLE_API_KEY or GEMINI_API_KEY in ~/.hermes/.env). Add one with `hermes model` "
            "(choose Gemini) or put it in ~/.hermes/.env."
        )
    exe = binary_path()
    if not exe.exists():
        # First run: fetch the prebuilt sidecar so nobody needs a Rust toolchain.
        exe.parent.mkdir(parents=True, exist_ok=True)
        _download_release(exe)
    return exe


def _pair(ctx, phone: str | None) -> None:
    exe = _require_ready(ctx)
    env = {**os.environ, **sidecar_env(ctx)}
    argv = [str(exe), "pair"] + (["--phone", phone] if phone else [])
    raise SystemExit(subprocess.run(argv, env=env, check=False).returncode)


def _serve(ctx) -> None:
    exe = _require_ready(ctx)
    env = {**os.environ, **sidecar_env(ctx)}
    s = settings(ctx)
    if s["context_mode"] != "core_session":
        os.execve(str(exe), [str(exe), "serve"], env)
    # Core session: this process (Hermes's interpreter) serves the voice the owner's DM session,
    # and runs the sidecar as a child.
    import logging
    import signal

    from .core_session import Service

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    os.environ.setdefault("HERMES_EXEC_ASK", "1")  # approvals ask a human, as in the gateway
    Service(
        owner=str(s["owner_number"]),
        token=env["HWV_TOKEN"],
        voice_tools=str(s["voice_tools"]),
        history_chars=int(s["history_chars"]),
        voice_instructions=str(s["voice_instructions"]),
        languages=[c.strip() for c in str(s["languages"]).split(",") if c.strip()],
        vocabulary=[w.strip() for w in str(s["vocabulary"]).split(",") if w.strip()],
    ).serve(str(s["context_listen"]))
    child = subprocess.Popen([str(exe), "serve"], env=env)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, _frame: child.send_signal(signum))
    raise SystemExit(child.wait())


def _service() -> None:
    hermes = shutil.which("hermes") or "hermes"
    log = data_dir() / "sidecar.log"
    if platform.system() == "Darwin":
        print(f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Save as ~/Library/LaunchAgents/com.hermes.whatsapp-voice.plist, then:
     launchctl load -w ~/Library/LaunchAgents/com.hermes.whatsapp-voice.plist -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.hermes.whatsapp-voice</string>
  <key>ProgramArguments</key>
  <array><string>{hermes}</string><string>whatsapp-voice</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>""")
    else:
        print(f"""# Save as ~/.config/systemd/user/hermes-whatsapp-voice.service, then:
#   systemctl --user daemon-reload && systemctl --user enable --now hermes-whatsapp-voice
#   loginctl enable-linger $USER   # keep it running while logged out
[Unit]
Description=Hermes WhatsApp voice sidecar
After=network-online.target

[Service]
ExecStart={hermes} whatsapp-voice serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target""")


def _status(ctx) -> None:
    try:
        print(json.dumps(Client.from_ctx(ctx).health(), indent=2))
    except SidecarError as e:
        raise SystemExit(str(e)) from e


def _call(ctx, reason: str) -> None:
    try:
        print(json.dumps(Client.from_ctx(ctx).start_call(reason), indent=2))
    except SidecarError as e:
        raise SystemExit(str(e)) from e


def _calls(ctx, limit: int) -> None:
    try:
        print(json.dumps(Client.from_ctx(ctx).recent_calls(limit), indent=2))
    except SidecarError as e:
        raise SystemExit(str(e)) from e


def load_stats(path: Path) -> tuple[list[dict], int]:
    """Call stats lines, oldest first, and how many lines could not be read."""
    if not path.exists():
        return [], 0
    records, bad = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            bad += 1
    return records, bad


def _duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1000:
        return f"{n / 1e3:.1f}k"
    return str(n)


def _usd(record: dict) -> float:
    return float((record.get("cost_estimate") or {}).get("usd") or 0.0)


def format_stats(records: list[dict], limit: int, bad: int = 0) -> str:
    """Recent calls (newest first) and totals over every logged call. Costs are estimates."""
    if not records:
        return "No call stats yet. They are logged after each call (setting call_stats)."
    rows = [f"{'when':<16}  {'dir':<8}  {'length':>7}  {'tokens':>7}  {'est. cost':>9}  {'basis':<7}  ended by"]
    for r in list(reversed(records))[: max(limit, 0)]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(r.get("started_at") or 0)))
        usage = r.get("usage") or {}
        basis = (r.get("cost_estimate") or {}).get("basis", "?")
        rows.append(
            f"{when:<16}  {r.get('direction', '?')!s:<8}  {_duration(r.get('duration_seconds')):>7}  "
            f"{_tokens(usage.get('total_tokens')):>7}  {'$' + format(_usd(r), '.3f'):>9}  {basis:<7}  "
            f"{r.get('ended_by') or r.get('status') or '?'}"
        )
    seconds = sum(int(r.get("duration_seconds") or 0) for r in records)
    usage = [r.get("usage") or {} for r in records]
    total = sum(int(u.get("total_tokens") or 0) for u in usage)
    prompt = sum(int(u.get("prompt_tokens") or 0) for u in usage)
    response = sum(int(u.get("response_tokens") or 0) for u in usage)
    cost = sum(_usd(r) for r in records)
    answered = [r for r in records if int(r.get("duration_seconds") or 0) > 0]
    per_min = cost / (seconds / 60) if seconds else 0.0
    rows += [
        "",
        f"Totals, {len(records)} calls ({len(answered)} answered): {_duration(seconds)} on the line, "
        f"{_tokens(total)} tokens (in {_tokens(prompt)} / out {_tokens(response)}), est. ${cost:.2f}"
        + (f" (~${per_min:.3f}/min)" if seconds else ""),
        "Costs are estimates from Gemini's reported token counts and the `pricing` setting, not a bill.",
    ]
    if bad:
        rows.append(f"({bad} unreadable line(s) skipped)")
    return "\n".join(rows)


def _stats(limit: int, as_json: bool) -> None:
    path = stats_path()
    records, bad = load_stats(path)
    if as_json:
        print(json.dumps(list(reversed(records))[: max(limit, 0)], indent=2))
        return
    print(format_stats(records, limit, bad))
    if records:
        print(f"Log: {path}")


def setup_parser(subparser) -> None:
    subs = subparser.add_subparsers(dest="wv_command")
    install = subs.add_parser("install", help="Install the sidecar binary (release download, or build with Rust)")
    install.add_argument("--from-source", action="store_true", help="Build with the local Rust toolchain")
    pair = subs.add_parser("pair", help="Link the sidecar to your bot's WhatsApp number (shows a QR code)")
    pair.add_argument("--phone", help="Bot's phone number, to link with a pair code instead of a QR code")
    subs.add_parser("serve", help="Run the sidecar in the foreground")
    subs.add_parser("service", help="Print a systemd/launchd unit that keeps the sidecar running")
    subs.add_parser("status", help="Show sidecar health")
    call = subs.add_parser("call", help="Place a test call to the owner")
    call.add_argument("reason", nargs="?", default="This is a quick test call.")
    calls = subs.add_parser("calls", help="Show recent calls: transcript, end reason and timing metrics")
    calls.add_argument("--limit", type=int, default=5)
    stats = subs.add_parser("stats", help="Show per-call duration, Gemini tokens and estimated cost, with totals")
    stats.add_argument("--limit", type=int, default=10, help="How many recent calls to list")
    stats.add_argument("--json", action="store_true", help="Print the recent calls' raw stats records")


def make_handler(ctx):
    def handle(args) -> None:
        cmd = getattr(args, "wv_command", None)
        if cmd == "install":
            _install(args)
        elif cmd == "pair":
            _pair(ctx, args.phone)
        elif cmd == "serve":
            _serve(ctx)
        elif cmd == "service":
            _service()
        elif cmd == "status":
            _status(ctx)
        elif cmd == "call":
            _call(ctx, args.reason)
        elif cmd == "calls":
            _calls(ctx, args.limit)
        elif cmd == "stats":
            _stats(args.limit, args.json)
        else:
            print("Usage: hermes whatsapp-voice {install,pair,serve,service,status,call,calls,stats}", file=sys.stderr)

    return handle
