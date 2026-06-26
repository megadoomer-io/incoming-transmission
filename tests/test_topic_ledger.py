# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the topic lifecycle ledger + reaper ("tidy forum"):
#   record_topic_event — fold a forum_topic_* service message into topics.json
#                        (OQ1: these arrive as bot-authored `message` updates)
#   _topics_to_reap    — pure: which closed topics have aged past the TTL
#   reap_topics        — delete the aged topics + prune the ledger (failed delete
#                        is retained for a later retry)
#
# The ledger path + state dir are redirected to tmp_path so nothing touches the
# real ~/.local/state; the Telegram API (delete_forum_topic) is stubbed.

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


def _ledger_env(mod, monkeypatch, tmp_path):
    """Point the ledger at a tmp file and silence logging."""
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "TOPICS_LEDGER", tmp_path / "topics.json")
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)


# --- record_topic_event: lifecycle folding ---------------------------------

def test_created_then_closed_records_state_and_closed_at(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    mod.record_topic_event({"date": 1000, "forum_topic_created": {"name": "proj@main"}}, 2103)
    mod.record_topic_event({"date": 5000, "forum_topic_closed": {}}, 2103)
    rec = mod.load_ledger()["2103"]
    assert rec["thread_id"] == 2103
    assert rec["name"] == "proj@main"
    assert rec["created_at"] == 1000      # preserved across the close
    assert rec["state"] == "closed"
    assert rec["closed_at"] == 5000       # the close message's date IS closed_at


def test_reopen_clears_closed_at(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    mod.record_topic_event({"date": 1000, "forum_topic_created": {"name": "p"}}, 7)
    mod.record_topic_event({"date": 5000, "forum_topic_closed": {}}, 7)
    mod.record_topic_event({"date": 6000, "forum_topic_reopened": {}}, 7)
    rec = mod.load_ledger()["7"]
    assert rec["state"] == "open"
    assert rec["closed_at"] is None


def test_edited_updates_name_only(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    mod.record_topic_event({"date": 1000, "forum_topic_created": {"name": "old"}}, 9)
    mod.record_topic_event({"date": 2000, "forum_topic_edited": {"name": "new"}}, 9)
    rec = mod.load_ledger()["9"]
    assert rec["name"] == "new"
    assert rec["state"] == "open"         # edit doesn't change lifecycle state


def test_close_without_prior_create_still_records(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    # A topic closed that the ledger never saw created (pre-ledger topic) still
    # gets a closed record so the reaper can age it.
    mod.record_topic_event({"date": 5000, "forum_topic_closed": {}}, 42)
    rec = mod.load_ledger()["42"]
    assert rec == {"thread_id": 42, "state": "closed", "closed_at": 5000}


# --- _topics_to_reap: pure aging policy ------------------------------------

def test_topics_to_reap_selects_only_aged_closed():
    mod = _load_router()
    ledger = {
        "1": {"state": "closed", "closed_at": 1000},      # aged -> reap
        "2": {"state": "closed", "closed_at": 999_950},   # within TTL -> keep
        "3": {"state": "open", "closed_at": None},        # open -> keep
        "4": {"state": "closed"},                         # no closed_at -> skip
        "5": "not-a-dict",                                # malformed -> skip
    }
    assert mod._topics_to_reap(ledger, now_epoch=1_000_000, ttl_seconds=100) == ["1"]


def test_topics_to_reap_empty():
    mod = _load_router()
    assert mod._topics_to_reap({}, 1_000_000, 100) == []


# --- reap_topics: delete + prune, retain on failure ------------------------

def test_reap_deletes_aged_and_prunes_ledger(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000)
    (tmp_path / "topics.json").write_text(json.dumps({
        "1": {"thread_id": 1, "state": "closed", "closed_at": 1000},     # aged -> reap
        "2": {"thread_id": 2, "state": "closed", "closed_at": 999_990},  # recent -> keep
        "3": {"thread_id": 3, "state": "open", "closed_at": None},       # open -> keep
    }))
    deleted = []
    monkeypatch.setattr(mod, "delete_forum_topic",
                        lambda chat_id, tid: deleted.append(tid) or True)
    mod.reap_topics(chat_id=42, ttl_seconds=100)
    assert deleted == [1]
    assert set(mod.load_ledger().keys()) == {"2", "3"}


def test_reap_retains_entry_when_delete_fails(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000)
    (tmp_path / "topics.json").write_text(json.dumps({
        "1": {"thread_id": 1, "state": "closed", "closed_at": 1000},
    }))
    monkeypatch.setattr(mod, "delete_forum_topic", lambda chat_id, tid: False)
    mod.reap_topics(chat_id=42, ttl_seconds=100)
    # Delete failed -> entry stays for a retry on the next sweep.
    assert "1" in mod.load_ledger()


def test_reap_noop_without_chat_id(monkeypatch, tmp_path):
    mod = _load_router()
    _ledger_env(mod, monkeypatch, tmp_path)
    (tmp_path / "topics.json").write_text(json.dumps({
        "1": {"thread_id": 1, "state": "closed", "closed_at": 0},
    }))
    called = []
    monkeypatch.setattr(mod, "delete_forum_topic",
                        lambda *a: called.append(a) or True)
    mod.reap_topics(chat_id=None, ttl_seconds=1)
    assert called == []                   # nothing deleted without a chat to act on
