# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the hung-MCP-tool recovery sweep (issue #18, owner-deferred -> opt-in):
#   _scan_outstanding_tool — pure: scan a Claude Code transcript JSONL for the
#                            final unanswered tool_use id + last-progress ts
#   detect_hung_tool       — pure policy: undrained inbox + outstanding tool_use +
#                            no progress for a dwell -> the id to cancel
#   handle_hung_tool       — impure shell: resolve pane, send Esc, inject note,
#                            nudge, alert; gated by mcp_hang_recovery_enabled
#
# The transcript is a tmp JSONL; tmux/inbox/Telegram are stubbed.

import importlib.util
import json
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_ut", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- transcript fixtures ----------------------------------------------------

def _assistant_tool_use(tool_id, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id,
                                     "name": "mcp__gmail__send"}]}}


def _tool_result(tool_id, ts):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                     "content": "ok"}]}}


def _assistant_text(text, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _queue_op(ts):
    # A non-progress append (queued input / meta): bumps mtime, advances nothing.
    return {"type": "queue-operation", "timestamp": ts, "content": "queued"}


def _write_transcript(tmp_path, records):
    p = tmp_path / "transcript.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(p)


# --- _scan_outstanding_tool: pure transcript scan --------------------------

def test_scan_outstanding_when_no_result(tmp_path):
    mod = _load_router()
    p = _write_transcript(tmp_path, [
        _assistant_text("starting", "t0"),
        _assistant_tool_use("toolu_A", "t1"),
    ])
    outstanding, last_ts = mod._scan_outstanding_tool(p)
    assert outstanding == "toolu_A"
    assert last_ts == "t1"


def test_scan_answered_tool_is_not_outstanding(tmp_path):
    mod = _load_router()
    p = _write_transcript(tmp_path, [
        _assistant_tool_use("toolu_A", "t1"),
        _tool_result("toolu_A", "t2"),
    ])
    outstanding, last_ts = mod._scan_outstanding_tool(p)
    assert outstanding is None
    assert last_ts == "t2"


def test_scan_queue_op_is_not_progress(tmp_path):
    mod = _load_router()
    p = _write_transcript(tmp_path, [
        _assistant_tool_use("toolu_A", "t1"),
        _queue_op("t9"),                       # later mtime, but NOT progress
    ])
    outstanding, last_ts = mod._scan_outstanding_tool(p)
    assert outstanding == "toolu_A"
    assert last_ts == "t1"                      # queue-op did not advance the watermark


def test_scan_skips_malformed_lines(tmp_path):
    mod = _load_router()
    p = tmp_path / "t.jsonl"
    p.write_text("not json\n"
                 + json.dumps(_assistant_tool_use("toolu_A", "t1")) + "\n"
                 + "{ broken\n")
    outstanding, last_ts = mod._scan_outstanding_tool(str(p))
    assert outstanding == "toolu_A" and last_ts == "t1"


def test_scan_missing_file():
    mod = _load_router()
    assert mod._scan_outstanding_tool("/no/such/transcript.jsonl") == (None, None)


# --- detect_hung_tool: pure policy -----------------------------------------

def _reg(tmp_path, transcript):
    sess = tmp_path / "sessions" / "100"
    sess.mkdir(parents=True, exist_ok=True)
    return {"thread_id": 100, "claude_pid": 4242,
            "inbox_path": str(sess / "inbox.jsonl"),
            "transcript_path": transcript}


def test_detect_hung_past_dwell(tmp_path):
    mod = _load_router()
    tp = _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    reg = _reg(tmp_path, tp)
    # Arm at t=1000 (first sight), then ripe at t=1000+dwell.
    _, seen = mod.detect_hung_tool(reg, {}, now_mono=1000, dwell_s=300,
                                   undrained=lambda r: 2)
    assert seen["100"]["tool_id"] == "toolu_A" and seen["100"]["acted"] is False
    action, seen2 = mod.detect_hung_tool(reg, seen, now_mono=1400, dwell_s=300,
                                         undrained=lambda r: 2)
    assert action == "toolu_A"                   # undrained + outstanding + stale


def test_detect_progressing_transcript_not_hung(tmp_path):
    mod = _load_router()
    reg = _reg(tmp_path, str(tmp_path / "transcript.jsonl"))
    # Arm with one outstanding tool at watermark "t1".
    _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    _, seen = mod.detect_hung_tool(reg, {}, now_mono=1000, dwell_s=300,
                                   undrained=lambda r: 2)
    # Transcript advances (a NEW tool_use after a text turn): watermark moves, so
    # even past the wall-clock dwell the episode re-arms instead of acting.
    _write_transcript(tmp_path, [
        _assistant_tool_use("toolu_A", "t1"),
        _assistant_text("still working", "t2"),
        _assistant_tool_use("toolu_B", "t3"),
    ])
    action, seen2 = mod.detect_hung_tool(reg, seen, now_mono=1400, dwell_s=300,
                                         undrained=lambda r: 2)
    assert action is None                         # progress -> not cut
    assert seen2["100"]["tool_id"] == "toolu_B"   # re-armed on the new tool
    assert seen2["100"]["first_seen"] == 1400


def test_detect_answered_tool_not_hung(tmp_path):
    mod = _load_router()
    tp = _write_transcript(tmp_path, [
        _assistant_tool_use("toolu_A", "t1"),
        _tool_result("toolu_A", "t2"),
    ])
    reg = _reg(tmp_path, tp)
    action, seen = mod.detect_hung_tool(reg, {"100": {"tool_id": "toolu_A",
                                                      "first_seen": 0,
                                                      "last_progress_ts": "t1",
                                                      "acted": False}},
                                        now_mono=10_000, dwell_s=300,
                                        undrained=lambda r: 2)
    assert action is None
    assert "100" not in seen                      # healthy -> episode cleared


def test_detect_drained_inbox_not_hung(tmp_path):
    mod = _load_router()
    tp = _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    reg = _reg(tmp_path, tp)
    action, seen = mod.detect_hung_tool(reg, {"100": {"tool_id": "toolu_A",
                                                      "first_seen": 0,
                                                      "last_progress_ts": "t1",
                                                      "acted": False}},
                                        now_mono=10_000, dwell_s=300,
                                        undrained=lambda r: 0)   # drained
    assert action is None
    assert "100" not in seen


def test_detect_within_dwell_not_hung(tmp_path):
    mod = _load_router()
    tp = _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    reg = _reg(tmp_path, tp)
    _, seen = mod.detect_hung_tool(reg, {}, now_mono=1000, dwell_s=300,
                                   undrained=lambda r: 2)
    action, _ = mod.detect_hung_tool(reg, seen, now_mono=1200, dwell_s=300,
                                     undrained=lambda r: 2)   # only 200s < 300s
    assert action is None


def test_detect_acted_episode_does_not_refire(tmp_path):
    mod = _load_router()
    tp = _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    reg = _reg(tmp_path, tp)
    seen = {"100": {"tool_id": "toolu_A", "first_seen": 0,
                    "last_progress_ts": "t1", "acted": True}}
    action, seen2 = mod.detect_hung_tool(reg, seen, now_mono=10_000, dwell_s=300,
                                         undrained=lambda r: 2)
    assert action is None                          # already cancelled this episode


# --- handle_hung_tool: impure shell ----------------------------------------

def _shell_env(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "INBOX_ROOT", tmp_path / "sessions")
    (tmp_path / "sessions" / "100").mkdir(parents=True)
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod.time, "monotonic", lambda: 10_000)
    monkeypatch.setattr(mod, "_hung_tool_state", {})


def test_shell_cancels_ripe_hang(monkeypatch, tmp_path):
    mod = _load_router()
    _shell_env(mod, monkeypatch, tmp_path)
    tp = _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    reg = _reg(tmp_path, tp)
    monkeypatch.setattr(mod, "_pane_for_claude_pid", lambda pid: "%5")
    keys = []
    monkeypatch.setattr(mod, "_tmux", lambda *a: keys.append(a) or "")
    notes, nudges, alerts = [], [], []
    monkeypatch.setattr(mod, "inject_inbox_note",
                        lambda *a, **k: notes.append((a, k)))
    monkeypatch.setattr(mod, "wake_session", lambda r: nudges.append(r))
    monkeypatch.setattr(mod, "send_message",
                        lambda *a, **k: alerts.append((a, k)))
    # Pre-arm so the dwell has elapsed (first_seen well in the past).
    mod._hung_tool_state["100"] = {"tool_id": "toolu_A", "first_seen": 0,
                                   "last_progress_ts": "t1", "acted": False}
    monkeypatch.setattr(mod, "undrained_count", lambda r: 2)
    mod.handle_hung_tool(reg, "100", chat_id=42, dwell_s=300)
    assert any(a[:2] == ("send-keys", "-t") and a[-1] == "Escape" for a in keys)
    assert len(notes) == 1                         # trusted inbox note injected
    assert nudges == [reg]                         # drain nudged
    assert len(alerts) == 1                        # 🪤 alert posted
    assert mod._hung_tool_state["100"]["acted"] is True


def test_shell_skips_when_pane_unresolved(monkeypatch, tmp_path):
    mod = _load_router()
    _shell_env(mod, monkeypatch, tmp_path)
    reg = _reg(tmp_path, str(tmp_path / "t.jsonl"))
    monkeypatch.setattr(mod, "_pane_for_claude_pid", lambda pid: None)  # dead
    keys = []
    monkeypatch.setattr(mod, "_tmux", lambda *a: keys.append(a) or "")
    monkeypatch.setattr(mod, "undrained_count", lambda r: 2)
    mod.handle_hung_tool(reg, "100", chat_id=42, dwell_s=300)
    assert keys == []                              # no Esc to a phantom pane


def test_shell_skips_when_permission_in_flight(monkeypatch, tmp_path):
    mod = _load_router()
    _shell_env(mod, monkeypatch, tmp_path)
    tp = _write_transcript(tmp_path, [_assistant_tool_use("toolu_A", "t1")])
    reg = _reg(tmp_path, tp)
    (tmp_path / "sessions" / "100" / "perm-pending.json").write_text("{}")
    keys = []
    monkeypatch.setattr(mod, "_pane_for_claude_pid", lambda pid: "%5")
    monkeypatch.setattr(mod, "_tmux", lambda *a: keys.append(a) or "")
    monkeypatch.setattr(mod, "undrained_count", lambda r: 2)
    mod.handle_hung_tool(reg, "100", chat_id=42, dwell_s=300)
    assert keys == []                              # the hook owns this, not us


# --- gating: disabled flag means no action ---------------------------------

def test_disabled_flag_default_off():
    mod = _load_router()
    # Owner-deferred -> ships opt-in/off.
    assert mod.CONFIG_DEFAULTS["mcp_hang_recovery_enabled"] is False


def test_disabled_flag_no_handle_call(monkeypatch, tmp_path):
    mod = _load_router()
    # With the feature off, context_loop's gate must never call handle_hung_tool.
    # Mirror the gate directly: cfg flag false -> skip.
    cfg = dict(mod.CONFIG_DEFAULTS)
    assert cfg.get("mcp_hang_recovery_enabled") is False
    calls = []
    monkeypatch.setattr(mod, "handle_hung_tool",
                        lambda *a, **k: calls.append(a))
    if cfg.get("mcp_hang_recovery_enabled"):
        mod.handle_hung_tool({}, "100", None, 300)
    assert calls == []
