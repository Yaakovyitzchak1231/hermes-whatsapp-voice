"""`hermes whatsapp-voice stats`: reads the sidecar's call_stats.jsonl and shows calls and totals."""

from __future__ import annotations

import argparse
import json

from conftest import FakeCtx


def _record(call_id, started_at, seconds, total, usd, basis="tokens", direction="inbound"):
    return {
        "call_id": call_id,
        "direction": direction,
        "status": "completed",
        "started_at": started_at,
        "answered_at": started_at + 2,
        "ended_at": started_at + 2 + seconds,
        "duration_seconds": seconds,
        "ended_by": "phone",
        "model": "gemini-3.8-live",
        "voice_mode": "separate",
        "tool_calls": 1,
        "ask_hermes_calls": 1,
        "usage": {"prompt_tokens": total - 100, "response_tokens": 100, "total_tokens": total},
        "cost_estimate": {"usd": usd, "input_usd": usd / 2, "output_usd": usd / 2, "basis": basis, "estimate": True},
    }


def _write(plugin, records, extra=""):
    path = plugin.sidecar_client.stats_path()
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + extra)
    return path


def _run(plugin, capsys, *argv):
    ctx = FakeCtx()
    plugin.register(ctx)
    parser = argparse.ArgumentParser()
    ctx.cli["whatsapp-voice"]["setup_fn"](parser)
    ctx.cli["whatsapp-voice"]["handler_fn"](parser.parse_args(list(argv)))
    return capsys.readouterr().out


def test_stats_lives_in_the_plugin_data_dir(plugin):
    assert plugin.sidecar_client.stats_path() == plugin.sidecar_client.data_dir() / "call_stats.jsonl"


def test_settings_reach_the_sidecar(plugin):
    (plugin.sidecar_client.data_dir() / "sidecar.token").write_text("tok")
    env = plugin.sidecar_client.sidecar_env(FakeCtx(owner_number="+15550000000"))
    assert env["HWV_CALL_STATS"] == "true"
    assert json.loads(env["HWV_PRICING"]) == {}
    env = plugin.sidecar_client.sidecar_env(
        FakeCtx(owner_number="+15550000000", call_stats=False, pricing={"audio_output_per_mtok": 20})
    )
    assert env["HWV_CALL_STATS"] == "false"
    assert json.loads(env["HWV_PRICING"]) == {"audio_output_per_mtok": 20}


def test_no_stats_yet(plugin, capsys):
    assert "No call stats yet" in _run(plugin, capsys, "stats")


def test_lists_recent_calls_newest_first_with_totals(plugin, capsys):
    _write(
        plugin,
        [
            _record("a", 1_700_000_000, 60, 3000, 0.0225),
            _record("b", 1_700_000_600, 125, 41_200, 0.1229, direction="outbound"),
            _record("c", 1_700_001_200, 0, 0, 0.0, basis="none"),
        ],
        extra="not json\n",
    )
    out = _run(plugin, capsys, "stats", "--limit", "2")
    lines = out.splitlines()
    assert lines[0].split()[:2] == ["when", "dir"]
    # Newest first, limited to 2: c then b.
    assert "none" in lines[1] and "0m00s" in lines[1]
    assert "outbound" in lines[2] and "2m05s" in lines[2] and "41.2k" in lines[2] and "$0.123" in lines[2]
    assert not any("1m00s" in line for line in lines[1:3])
    totals = next(line for line in lines if line.startswith("Totals"))
    assert "3 calls (2 answered)" in totals
    assert "3m05s on the line" in totals
    assert "44.2k tokens" in totals
    assert "est. $0.15" in totals
    assert "estimates" in out
    assert "1 unreadable line(s) skipped" in out


def test_json_output(plugin, capsys):
    _write(plugin, [_record("a", 1_700_000_000, 60, 3000, 0.0225), _record("b", 1_700_000_600, 60, 10, 0.01)])
    out = json.loads(_run(plugin, capsys, "stats", "--json", "--limit", "1"))
    assert [r["call_id"] for r in out] == ["b"]
    assert out[0]["cost_estimate"]["estimate"] is True
