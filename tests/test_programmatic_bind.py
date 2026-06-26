# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the router's gauge-backfill transcript selection: _select_transcript_by_pane
# (exact pane_id join) and _select_transcript_for_cwd (the cwd fallback). The bind
# helpers (create_forum_topic, write_registry_entry, bind_pane) now live in
# bridge_bind — see test_bridge_bind.py. telegram-router.py is stdlib-only and loads
# with no import side effects, so importlib loads it.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_pb", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- _select_transcript_by_pane (exact gauge backfill join) ----------------

def test_select_transcript_by_pane_exact_match():
    mod = _load_router()
    # Two sessions in ONE cwd — cwd alone can't disambiguate, pane_id can.
    records = [
        {"cwd": "/repo", "pane_id": "%23", "transcript_path": "/t/old.jsonl"},
        {"cwd": "/repo", "pane_id": "%24", "transcript_path": "/t/new.jsonl"},
    ]
    out = mod._select_transcript_by_pane(records, "%24", mtime_of=lambda p: 1.0)
    assert out == "/t/new.jsonl"               # the replacement's pane, not the sibling


def test_select_transcript_by_pane_none_pane_returns_empty():
    mod = _load_router()
    out = mod._select_transcript_by_pane(
        [{"pane_id": "%1", "transcript_path": "/t/x.jsonl"}], None, mtime_of=lambda p: 1.0)
    assert out == ""                           # no pane id -> no exact match


def test_select_transcript_by_pane_newest_when_pane_reused():
    mod = _load_router()
    # A pane that ran two sessions in sequence: newest-mtime wins.
    records = [
        {"pane_id": "%9", "transcript_path": "/t/first.jsonl"},
        {"pane_id": "%9", "transcript_path": "/t/second.jsonl"},
    ]
    mtimes = {"/t/first.jsonl": 10.0, "/t/second.jsonl": 99.0}
    assert mod._select_transcript_by_pane(records, "%9", mtime_of=lambda p: mtimes[p]) == "/t/second.jsonl"


def test_select_transcript_by_pane_no_match_returns_empty():
    mod = _load_router()
    out = mod._select_transcript_by_pane(
        [{"pane_id": "%1", "transcript_path": "/t/x.jsonl"}], "%2", mtime_of=lambda p: 1.0)
    assert out == ""


# --- _select_transcript_for_cwd (legacy cwd fallback) ----------------------

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
