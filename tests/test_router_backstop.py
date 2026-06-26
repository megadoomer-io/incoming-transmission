# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the router's stale-pane-option backstop: the programmatic safety net
# that keeps the pane-keyed resolver's invariant ("a pane carries
# @telegram_thread_id only while its bridge session is live") true even when the
# in-session clear on /end / compaction is skipped. A retired window is
# DEAD-renamed (kill_old=false) but its pane survives; the backstop unsets the
# option on any such pane so a reused pane can't mis-resolve to a dead topic.
#
# telegram-router.py is stdlib-only and loads with no import side effects (top
# level is constants + a __main__ guard), so importlib loads it cleanly. _tmux is
# stubbed in the wrapper test so nothing shells out.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_ut", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Field order matches the router's format string:
#   #{pane_id}\t#{window_name}\t#{@telegram_thread_id}
SAMPLE = "\n".join([
    "%1\ttg:work\t2103",          # live bridge window, option set -> NOT stale
    "%2\tDEAD - tg:work\t2103",   # retired + option still set -> STALE
    "%3\tDEAD - old\t",           # retired, option already cleared -> not stale
    "%4\tbash\t",                 # ordinary pane -> not stale
    "%5\tDEAD - tg:other\t2168",  # retired + option -> STALE
])


# --- _stale_pane_ids: pure parse/select ------------------------------------

def test_stale_pane_ids_selects_dead_with_option():
    mod = _load_router()
    assert mod._stale_pane_ids(SAMPLE) == ["%2", "%5"]


def test_stale_pane_ids_empty_input():
    mod = _load_router()
    assert mod._stale_pane_ids("") == []


def test_stale_pane_ids_skips_malformed_lines():
    mod = _load_router()
    # Missing the option field entirely (2 cols) and a blank line are ignored;
    # a live window keeps its option (correctly left alone).
    out = "%9\tDEAD - oops\n\n%1\ttg:work\t2103"
    assert mod._stale_pane_ids(out) == []


def test_stale_pane_ids_live_window_with_option_not_cleared():
    mod = _load_router()
    # The whole point: an option on a LIVE (non-DEAD) window must never be picked.
    assert mod._stale_pane_ids("%1\ttg:work\t2103") == []


# --- clear_stale_pane_options: issues the unset per stale pane --------------

def test_clear_issues_unset_for_each_stale_pane(monkeypatch):
    mod = _load_router()
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        return SAMPLE if args[:1] == ("list-panes",) else ""

    monkeypatch.setattr(mod, "_tmux", fake_tmux)
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    mod.clear_stale_pane_options()

    unset = [c for c in calls if c[:1] == ("set-option",)]
    assert unset == [
        ("set-option", "-pu", "-t", "%2", "@telegram_thread_id"),
        ("set-option", "-pu", "-t", "%5", "@telegram_thread_id"),
    ]


def test_clear_noop_when_no_dead_panes(monkeypatch):
    mod = _load_router()
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        return "%1\ttg:work\t2103" if args[:1] == ("list-panes",) else ""

    monkeypatch.setattr(mod, "_tmux", fake_tmux)
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    mod.clear_stale_pane_options()

    assert not [c for c in calls if c[:1] == ("set-option",)]  # nothing cleared
