# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for telegram-inbox.sh's drain/ack lock handshake — and the regression it
# was written for: an EMPTY drain (no backlog) must self-release the lock so it
# can't leak. The bind-time section-A drain runs before any message has arrived;
# when it left the lock held, the session reasonably skipped the ack ("nothing to
# drain") and the first real message then couldn't acquire the lock until the 30m
# stale-TTL or a self-heal cleared it (a multi-minute stall on the first reply).
#
# The script hardcodes its session root at /tmp/claude-telegram/sessions/<thread>,
# so each test uses a unique thread id and cleans up.

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "runtime" / "telegram-inbox.sh"


def _sess(thread):
    return Path("/tmp/claude-telegram/sessions") / str(thread)


def _run(cmd, thread):
    return subprocess.run(["bash", str(SCRIPT), cmd, str(thread)],
                          capture_output=True, text=True)


def _thread(suffix):
    # Unique per test + process so parallel runs don't collide.
    return "99{}{}".format(os.getpid(), suffix)


def _cleanup(thread):
    shutil.rmtree(_sess(thread), ignore_errors=True)


# --- empty drain self-releases (the fix) -----------------------------------

def test_empty_drain_releases_lock_and_is_silent():
    t = _thread("01")
    _cleanup(t)
    try:
        r = _run("drain", t)
        assert r.returncode == 0
        assert r.stdout.strip() == ""              # "prints nothing if there's no backlog"
        assert r.stderr.strip() == ""              # no "No such file or directory" leak
        assert not (_sess(t) / "poll.lock.d").is_dir()   # lock NOT left held
    finally:
        _cleanup(t)


# --- non-empty drain holds the lock until ack ------------------------------

def test_drain_holds_lock_then_ack_releases():
    t = _thread("02")
    _cleanup(t)
    try:
        sess = _sess(t)
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "inbox.jsonl").write_text('{"text":"a"}\n{"text":"b"}\n')

        d = _run("drain", t)
        assert d.returncode == 0
        assert d.stdout.count("\n") == 2           # both lines printed
        assert (sess / "poll.lock.d").is_dir()     # lock HELD awaiting ack
        assert (sess / ".drain-pending").read_text().strip() == "2"

        a = _run("ack", t)
        assert a.returncode == 0
        assert not (sess / "poll.lock.d").is_dir()  # lock released
        assert (sess / "read.offset").read_text().strip() == "2"
    finally:
        _cleanup(t)


# --- the bind-time regression: empty drain, then a real message ------------

def test_empty_drain_does_not_block_first_real_message():
    t = _thread("03")
    _cleanup(t)
    try:
        sess = _sess(t)
        # 1) Bind-time section-A drain: no inbox yet -> empty, must release.
        first = _run("drain", t)
        assert first.returncode == 0
        assert not (sess / "poll.lock.d").is_dir()

        # 2) First real message arrives; the next drain must ACQUIRE the lock and
        #    print it — NOT hit exit 3 LOCKED against a leaked bind-time lock.
        (sess / "inbox.jsonl").write_text('{"text":"first real msg"}\n')
        second = _run("drain", t)
        assert second.returncode == 0, "drain returned {}: {}".format(
            second.returncode, second.stderr)
        assert "first real msg" in second.stdout
        assert (sess / "poll.lock.d").is_dir()      # held, awaiting ack
    finally:
        _cleanup(t)


# --- a genuinely live lock is still respected (no double-drain) -------------

def test_live_lock_blocks_concurrent_drain():
    t = _thread("04")
    _cleanup(t)
    try:
        sess = _sess(t)
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "inbox.jsonl").write_text('{"text":"x"}\n')
        held = _run("drain", t)                      # acquires + holds (backlog)
        assert (sess / "poll.lock.d").is_dir()
        assert held.returncode == 0

        blocked = _run("drain", t)                   # fresh lock, not stale
        assert blocked.returncode == 3               # LOCKED — refuses to double-drain
        assert "LOCKED" in blocked.stderr
    finally:
        _cleanup(t)
