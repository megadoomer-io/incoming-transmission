# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the router-side programmatic-binding helpers added when binding moved
# off the LLM: create_forum_topic (retry + None-on-failure), write_registry_entry
# (schema round-trip), and _select_transcript_for_cwd (the gauge backfill's pure
# selection). telegram-router.py is stdlib-only and loads with no import side
# effects, so importlib loads it; api_call / time.sleep are stubbed so nothing
# shells out or actually sleeps.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_pb", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- create_forum_topic ----------------------------------------------------

def test_create_forum_topic_returns_thread_id(monkeypatch):
    mod = _load_router()
    monkeypatch.setattr(
        mod, "api_call",
        lambda method, params=None: {"ok": True, "result": {"message_thread_id": 777}})
    assert mod.create_forum_topic(123, "repo@main") == 777


def test_create_forum_topic_retries_then_none_on_network_error(monkeypatch):
    mod = _load_router()
    calls = []

    def boom(method, params=None):
        calls.append(method)
        raise OSError("network down")

    monkeypatch.setattr(mod, "api_call", boom)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    assert mod.create_forum_topic(123, "x", retries=3) is None
    assert len(calls) == 3                     # exhausted all retries


def test_create_forum_topic_not_ok_returns_none(monkeypatch):
    mod = _load_router()
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod, "api_call",
                        lambda method, params=None: {"ok": False, "description": "boom"})
    assert mod.create_forum_topic(1, "x", retries=2) is None


# --- write_registry_entry --------------------------------------------------

def test_write_registry_entry_shape_and_roundtrip(monkeypatch, tmp_path):
    # REGISTRY_DIR is bound from the env at import, so set it before loading.
    monkeypatch.setenv("TELEGRAM_BRIDGE_STATE_DIR", str(tmp_path))
    mod = _load_router()
    entry = mod.write_registry_entry(
        555, pane_id="%3", claude_pid="999", cwd="/x", spawned=True)
    assert entry["thread_id"] == 555
    assert entry["pane_id"] == "%3"
    assert entry["claude_pid"] == 999          # coerced to int
    assert entry["cwd"] == "/x"
    assert entry["spawned"] is True
    assert entry["transcript_path"] == ""      # backfilled later by the router
    assert entry["inbox_path"].endswith("/sessions/555/inbox.jsonl")
    # Round-trips through the router's own reader.
    assert mod.load_registry("555") == entry


def test_write_registry_entry_defaults_spawned_false_and_no_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BRIDGE_STATE_DIR", str(tmp_path))
    mod = _load_router()
    entry = mod.write_registry_entry(42, pane_id="%9", cwd="/c")
    assert entry["spawned"] is False           # default = adopt
    assert entry["claude_pid"] is None         # no pid given


# --- _select_transcript_for_cwd (gauge backfill selection) -----------------

def test_select_transcript_newest_mtime_for_cwd():
    mod = _load_router()
    records = [
        {"cwd": "/repo/a", "transcript_path": "/t/a1.jsonl"},
        {"cwd": "/repo/a", "transcript_path": "/t/a2.jsonl"},
        {"cwd": "/repo/b", "transcript_path": "/t/b1.jsonl"},
    ]
    mtimes = {"/t/a1.jsonl": 100.0, "/t/a2.jsonl": 200.0, "/t/b1.jsonl": 300.0}
    out = mod._select_transcript_for_cwd(records, "/repo/a", mtime_of=lambda p: mtimes[p])
    assert out == "/t/a2.jsonl"                # newest among the cwd matches only


def test_select_transcript_no_cwd_match_returns_empty():
    mod = _load_router()
    out = mod._select_transcript_for_cwd(
        [{"cwd": "/x", "transcript_path": "/t/x.jsonl"}], "/y", mtime_of=lambda p: 1.0)
    assert out == ""


def test_select_transcript_skips_unstatable():
    mod = _load_router()
    records = [
        {"cwd": "/a", "transcript_path": "/gone.jsonl"},
        {"cwd": "/a", "transcript_path": "/here.jsonl"},
    ]

    def mt(p):
        if p == "/gone.jsonl":
            raise OSError("vanished")
        return 50.0

    assert mod._select_transcript_for_cwd(records, "/a", mtime_of=mt) == "/here.jsonl"
