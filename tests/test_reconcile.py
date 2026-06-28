# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the session<->topic reconciliation sweep (issue #7):
#   _reconcile_plan          — pure policy: which topics to close, which registry
#                              entries to drop, across both drift directions
#   reconcile_sessions_topics — impure shell: load registry, fold in pid-liveness
#                              + mid-handoff guard, close topics, drop entries,
#                              mark the ledger
#
# Filesystem (registry dir, ledger, session dirs) is redirected to tmp_path; the
# Telegram API (close_forum_topic, send_message) and _pid_alive are stubbed.

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


# --- _reconcile_plan: pure drift policy ------------------------------------

def test_plan_dead_session_closes_and_drops():
    mod = _load_router()
    entries = [("100", 100, False)]            # dead session
    close, drop = mod._reconcile_plan(entries, ledger={}, now_epoch=0, grace_seconds=600)
    assert close == [(100, "100", "session-ended")]
    assert drop == ["100"]


def test_plan_live_session_is_untouched():
    mod = _load_router()
    entries = [("100", 100, True)]
    close, drop = mod._reconcile_plan(entries, ledger={}, now_epoch=0, grace_seconds=600)
    assert close == []
    assert drop == []


def test_plan_dead_session_already_closed_drops_only():
    mod = _load_router()
    # The owner already closed the topic — drop the dead entry but don't re-close
    # (no redundant close/alert).
    entries = [("100", 100, False)]
    ledger = {"100": {"thread_id": 100, "state": "closed", "closed_at": 5}}
    close, drop = mod._reconcile_plan(entries, ledger, now_epoch=10, grace_seconds=600)
    assert close == []
    assert drop == ["100"]


def test_plan_orphan_open_topic_aged_is_closed():
    mod = _load_router()
    # Ledger topic open, no registry entry, created long ago -> orphan -> close,
    # but nothing to drop (there is no registry file).
    ledger = {"200": {"thread_id": 200, "state": "open", "created_at": 1000}}
    close, drop = mod._reconcile_plan([], ledger, now_epoch=2000, grace_seconds=600)
    assert close == [(200, "200", "orphan-topic")]
    assert drop == []


def test_plan_orphan_open_topic_within_grace_kept():
    mod = _load_router()
    ledger = {"200": {"thread_id": 200, "state": "open", "created_at": 1800}}
    close, drop = mod._reconcile_plan([], ledger, now_epoch=2000, grace_seconds=600)
    assert close == []                          # 200s < 600s grace
    assert drop == []


def test_plan_orphan_open_topic_no_created_at_kept():
    mod = _load_router()
    ledger = {"200": {"thread_id": 200, "state": "open"}}
    close, drop = mod._reconcile_plan([], ledger, now_epoch=10_000, grace_seconds=600)
    assert close == []                          # can't age -> leave alone
    assert drop == []


def test_plan_open_topic_with_live_registry_is_direction_a_only():
    mod = _load_router()
    # A live session's topic is 'open' in the ledger AND has a registry entry ->
    # direction B must skip it (it's accounted for); direction A keeps it (alive).
    entries = [("200", 200, True)]
    ledger = {"200": {"thread_id": 200, "state": "open", "created_at": 0}}
    close, drop = mod._reconcile_plan(entries, ledger, now_epoch=10_000, grace_seconds=600)
    assert close == []
    assert drop == []


def test_plan_guards_general_and_invalid_ids():
    mod = _load_router()
    ledger = {
        "1": {"thread_id": 1, "state": "open", "created_at": 0},        # General -> skip
        "x": {"thread_id": None, "state": "open", "created_at": 0},     # unparseable -> skip
        "closed": {"state": "closed", "closed_at": 0},                  # not open -> skip
    }
    close, drop = mod._reconcile_plan([], ledger, now_epoch=10_000, grace_seconds=600)
    assert close == []
    assert drop == []


def test_plan_mixed_scenario():
    mod = _load_router()
    entries = [
        ("100", 100, False),    # dead -> close + drop
        ("101", 101, True),     # live -> nothing
        ("102", 102, False),    # dead, already closed -> drop only
    ]
    ledger = {
        "102": {"thread_id": 102, "state": "closed", "closed_at": 5},
        "300": {"thread_id": 300, "state": "open", "created_at": 0},   # orphan aged -> close
        "301": {"thread_id": 301, "state": "open", "created_at": 9_999},  # within grace -> keep
    }
    close, drop = mod._reconcile_plan(entries, ledger, now_epoch=10_000, grace_seconds=600)
    assert (100, "100", "session-ended") in close
    assert (300, "300", "orphan-topic") in close
    assert (301, "301", "orphan-topic") not in [c for c in close]
    assert (102, "102", "session-ended") not in close       # already closed
    assert set(drop) == {"100", "102"}


# --- reconcile_sessions_topics: impure shell -------------------------------

def _shell_env(mod, monkeypatch, tmp_path):
    """Redirect filesystem to tmp_path and silence logging. Returns the registry
    dir so a test can plant entries."""
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "REGISTRY_DIR", reg)
    monkeypatch.setattr(mod, "TOPICS_LEDGER", tmp_path / "topics.json")
    monkeypatch.setattr(mod, "INBOX_ROOT", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod, "load_compaction_cfg", lambda: dict(mod.CONFIG_DEFAULTS))
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000)
    return reg


def _write_reg(reg_dir, tkey, thread_id, claude_pid):
    (reg_dir / "{}.json".format(tkey)).write_text(json.dumps(
        {"thread_id": thread_id, "claude_pid": claude_pid,
         "inbox_path": "/tmp/x", "cwd": "/p"}))


def test_shell_dead_session_closes_topic_and_drops_entry(monkeypatch, tmp_path):
    mod = _load_router()
    reg = _shell_env(mod, monkeypatch, tmp_path)
    _write_reg(reg, "100", 100, 4242)
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: False)   # dead
    closed, alerts = [], []
    monkeypatch.setattr(mod, "close_forum_topic",
                        lambda chat_id, tid: closed.append(tid) or True)
    monkeypatch.setattr(mod, "send_message",
                        lambda *a, **k: alerts.append((a, k)))
    mod.reconcile_sessions_topics(chat_id=42)
    assert closed == [100]
    assert len(alerts) == 1                                     # owner told once
    assert not (reg / "100.json").exists()                     # entry dropped
    rec = mod.load_ledger()["100"]
    assert rec["state"] == "closed" and rec["closed_at"] == 1_000_000


def test_shell_live_session_untouched(monkeypatch, tmp_path):
    mod = _load_router()
    reg = _shell_env(mod, monkeypatch, tmp_path)
    _write_reg(reg, "100", 100, 4242)
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: True)    # alive
    closed = []
    monkeypatch.setattr(mod, "close_forum_topic",
                        lambda chat_id, tid: closed.append(tid) or True)
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: None)
    mod.reconcile_sessions_topics(chat_id=42)
    assert closed == []
    assert (reg / "100.json").exists()                         # entry kept


def test_shell_pid_none_is_treated_alive(monkeypatch, tmp_path):
    mod = _load_router()
    reg = _shell_env(mod, monkeypatch, tmp_path)
    _write_reg(reg, "100", 100, None)                          # pre-pid-format entry
    # _pid_alive should never even be consulted for a None pid, but make it dead
    # to prove the None short-circuit protects the entry.
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: False)
    closed = []
    monkeypatch.setattr(mod, "close_forum_topic",
                        lambda chat_id, tid: closed.append(tid) or True)
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: None)
    mod.reconcile_sessions_topics(chat_id=42)
    assert closed == []
    assert (reg / "100.json").exists()


def test_shell_midhandoff_lock_protects_dead_pid(monkeypatch, tmp_path):
    mod = _load_router()
    reg = _shell_env(mod, monkeypatch, tmp_path)
    _write_reg(reg, "100", 100, 4242)
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: False)   # old pid dead...
    (tmp_path / "sessions" / "100").mkdir()
    (tmp_path / "sessions" / "100" / "compacting.lock").touch()  # ...but rolling over
    closed = []
    monkeypatch.setattr(mod, "close_forum_topic",
                        lambda chat_id, tid: closed.append(tid) or True)
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: None)
    mod.reconcile_sessions_topics(chat_id=42)
    assert closed == []                                        # handoff not closed
    assert (reg / "100.json").exists()


def test_shell_close_failure_still_drops_registry_and_leaves_ledger_open(monkeypatch, tmp_path):
    mod = _load_router()
    reg = _shell_env(mod, monkeypatch, tmp_path)
    _write_reg(reg, "100", 100, 4242)
    (tmp_path / "topics.json").write_text(json.dumps(
        {"100": {"thread_id": 100, "state": "open", "created_at": 0}}))
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(mod, "close_forum_topic", lambda chat_id, tid: False)  # fails
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: None)
    mod.reconcile_sessions_topics(chat_id=42)
    # Registry dropped regardless (so messages get 'no session'); ledger stays
    # open so the NEXT sweep retries the close via direction B.
    assert not (reg / "100.json").exists()
    assert mod.load_ledger()["100"]["state"] == "open"


def test_shell_noop_without_chat_id(monkeypatch, tmp_path):
    mod = _load_router()
    reg = _shell_env(mod, monkeypatch, tmp_path)
    _write_reg(reg, "100", 100, 4242)
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: False)
    called = []
    monkeypatch.setattr(mod, "close_forum_topic",
                        lambda *a: called.append(a) or True)
    mod.reconcile_sessions_topics(chat_id=None)
    assert called == []
    assert (reg / "100.json").exists()                         # nothing touched
