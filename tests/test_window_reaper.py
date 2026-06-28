# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the tmux window reaper (issue #6):
#   _windows_to_reap   — pure: which shared-session windows are confirmed-dead
#                        bridge corpses ripe for killing (with grace dwell)
#   reap_stale_windows — impure shell: list windows, kill ripe corpses, summarize
#
# tmux is stubbed (no live server); the corpse signals are window names + pane_dead.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_ut", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- _windows_to_reap: pure corpse policy ----------------------------------

def test_dead_window_past_grace_reaped():
    mod = _load_router()
    windows = [("@1", "DEAD - tg:joey", False)]
    reap, seen_next = mod._windows_to_reap(windows, {"@1": 0}, now_mono=10_000, grace_seconds=3600)
    assert reap == ["@1"]
    assert seen_next == {"@1": 0}                 # first_seen preserved


def test_dead_window_within_grace_kept():
    mod = _load_router()
    windows = [("@1", "DEAD - tg:joey", False)]
    # First sight: records first_seen=now, not yet ripe.
    reap, seen_next = mod._windows_to_reap(windows, {}, now_mono=5000, grace_seconds=3600)
    assert reap == []
    assert seen_next == {"@1": 5000}


def test_tg_dead_pane_past_grace_reaped():
    mod = _load_router()
    windows = [("@2", "tg:portal", True)]         # claude exited -> dead pane
    reap, _ = mod._windows_to_reap(windows, {"@2": 0}, now_mono=10_000, grace_seconds=3600)
    assert reap == ["@2"]


def test_tg_live_pane_never_reaped():
    mod = _load_router()
    windows = [("@2", "tg:portal", False)]        # claude still running
    reap, seen_next = mod._windows_to_reap(windows, {"@2": 0}, now_mono=10_000, grace_seconds=3600)
    assert reap == []
    assert seen_next == {}                         # non-corpse dropped -> re-arms


def test_user_window_never_reaped():
    mod = _load_router()
    # A user's own claude window (not tg:, not DEAD -) is never a corpse, even with a
    # dead pane.
    windows = [("@3", "dotfiles", True), ("@4", "incoming-transmission", False)]
    reap, seen_next = mod._windows_to_reap(windows, {}, now_mono=10_000, grace_seconds=3600)
    assert reap == []
    assert seen_next == {}


def test_deadline_prefix_not_matched():
    mod = _load_router()
    # 'DEADLINE' must NOT match the exact 'DEAD - ' retire prefix.
    windows = [("@5", "DEADLINE", False)]
    reap, seen_next = mod._windows_to_reap(windows, {"@5": 0}, now_mono=10_000, grace_seconds=3600)
    assert reap == []
    assert seen_next == {}


def test_grace_accumulates_across_sweeps():
    mod = _load_router()
    windows = [("@1", "DEAD - tg:x", False)]
    # Sweep 1 at t=1000: first sight, not ripe.
    reap1, seen1 = mod._windows_to_reap(windows, {}, now_mono=1000, grace_seconds=3600)
    assert reap1 == [] and seen1 == {"@1": 1000}
    # Sweep 2 at t=1000+3600: now ripe, reaped.
    reap2, _ = mod._windows_to_reap(windows, seen1, now_mono=4600, grace_seconds=3600)
    assert reap2 == ["@1"]


def test_mixed_windows():
    mod = _load_router()
    windows = [
        ("@1", "DEAD - tg:a", False),   # corpse, ripe -> reap
        ("@2", "tg:b", True),           # corpse (dead pane), ripe -> reap
        ("@3", "tg:c", False),          # live spawn -> keep
        ("@4", "myrepo", True),         # user window -> keep
    ]
    seen = {"@1": 0, "@2": 0, "@3": 0}
    reap, seen_next = mod._windows_to_reap(windows, seen, now_mono=10_000, grace_seconds=3600)
    assert set(reap) == {"@1", "@2"}
    assert set(seen_next) == {"@1", "@2"}          # only corpses retained


# --- reap_stale_windows: impure shell --------------------------------------

def _tmux_stub(list_windows_output, kill_calls):
    """Return a fake _tmux: answers list-windows with the given output, records
    kill-window targets, no-ops everything else."""
    def _fake(*args):
        if args and args[0] == "list-windows":
            return list_windows_output
        if args and args[0] == "kill-window":
            kill_calls.append(args[-1])            # the window id (last arg)
            return ""
        return ""
    return _fake


def test_shell_kills_ripe_corpses_and_reports(monkeypatch):
    mod = _load_router()
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod, "load_compaction_cfg",
                        lambda: {"window_reap_grace_seconds": 3600})
    monkeypatch.setattr(mod.time, "monotonic", lambda: 10_000)
    # Pre-seed first-seen so the grace has already elapsed.
    monkeypatch.setattr(mod, "_reap_seen", {"@1": 0, "@2": 0})
    lw = "@1\tDEAD - tg:a\t0\n@2\ttg:b\t1\n@3\ttg:c\t0\n@9\tmyrepo\t0"
    kills = []
    monkeypatch.setattr(mod, "_tmux", _tmux_stub(lw, kills))
    sent = []
    monkeypatch.setattr(mod, "send_message",
                        lambda *a, **k: sent.append((a, k)))
    mod.reap_stale_windows(chat_id=42)
    assert set(kills) == {"@1", "@2"}              # only ripe corpses
    assert len(sent) == 1                          # one summary to General
    assert sent[0][1].get("thread_id") is None     # posted to General topic


def test_shell_noop_when_no_corpses(monkeypatch):
    mod = _load_router()
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod, "load_compaction_cfg",
                        lambda: {"window_reap_grace_seconds": 3600})
    monkeypatch.setattr(mod.time, "monotonic", lambda: 10_000)
    monkeypatch.setattr(mod, "_reap_seen", {})
    lw = "@3\ttg:c\t0\n@9\tmyrepo\t0"             # live spawn + user window only
    kills = []
    monkeypatch.setattr(mod, "_tmux", _tmux_stub(lw, kills))
    sent = []
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: sent.append(a))
    mod.reap_stale_windows(chat_id=42)
    assert kills == []
    assert sent == []


def test_shell_first_sight_arms_grace_no_kill(monkeypatch):
    mod = _load_router()
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod, "load_compaction_cfg",
                        lambda: {"window_reap_grace_seconds": 3600})
    monkeypatch.setattr(mod.time, "monotonic", lambda: 5000)
    monkeypatch.setattr(mod, "_reap_seen", {})    # never seen before
    lw = "@1\tDEAD - tg:a\t0"
    kills = []
    monkeypatch.setattr(mod, "_tmux", _tmux_stub(lw, kills))
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: None)
    mod.reap_stale_windows(chat_id=42)
    assert kills == []                            # first sight only arms the grace
    assert mod._reap_seen == {"@1": 5000}         # first_seen recorded for next sweep
