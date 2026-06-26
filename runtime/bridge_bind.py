#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# bridge_bind: the ONE place that BINDS a tmux pane to a Telegram forum topic —
# create the topic, stamp the pane's @telegram_thread_id option, write the registry
# entry. The programmatic counterpart to bridge_resolve (which READS the binding);
# this WRITES it. Shared so every bind path runs the same code, off the LLM:
#
#   - router `/attach` adopt   -> bind_pane(<picked pane>, spawned=False) + send-keys
#   - keyboard `/telegram`     -> this module's CLI binds $TMUX_PANE (see __main__)
#
# (The spawn script binds /new + rollover with its own inline equivalent, pre-window;
#  it could be unified onto this module later.)
#
# Stdlib only; resolves on /usr/bin/python3 via PATH.

import datetime
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

TMUX_BIN = os.environ.get("TELEGRAM_BRIDGE_TMUX", "/opt/homebrew/bin/tmux")
PANE_OPTION = "@telegram_thread_id"
STATE_DIR = os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "state", "telegram-bridge"))
REGISTRY_DIR = os.path.join(STATE_DIR, "registry")


def _log(msg, log):
    if log:
        log(msg)


def create_forum_topic(token, chat_id, name, retries=3, log=None, sleep=time.sleep):
    """Create a Telegram forum topic; return its message_thread_id (int) or None.

    Retries a few times because a silent failure would leave a session with no
    topic — callers MUST NOT bind on None. Self-contained (takes the token) so it
    works from the router, a CLI, or a test without shared globals."""
    data = json.dumps({"chat_id": chat_id, "name": name}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                "https://api.telegram.org/bot%s/createForumTopic" % token,
                data=data, headers={"Content-Type": "application/json"})
            r = json.load(urllib.request.urlopen(req, timeout=30))
            if r.get("ok"):
                return r["result"]["message_thread_id"]
            _log("createForumTopic not ok: %s" % r, log)
        except (urllib.error.URLError, OSError, ValueError) as e:
            _log("createForumTopic attempt %d: %s" % (attempt + 1, e), log)
        sleep(1.5)
    _log("createForumTopic failed for %r after %d attempts" % (name, retries), log)
    return None


def stamp_pane(pane_id, thread_id, tmux_bin=TMUX_BIN):
    """Set the pane's @telegram_thread_id option (the authoritative, durable
    binding bridge_resolve reads). Best-effort; returns False on any failure."""
    if not pane_id:
        return False
    try:
        subprocess.run([tmux_bin, "set-option", "-p", "-t", pane_id,
                        PANE_OPTION, str(thread_id)],
                       capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def write_registry_entry(thread_id, *, pane_id=None, claude_pid=None, cwd="",
                         inbox_path=None, spawned=False, transcript_path="",
                         registry_dir=REGISTRY_DIR, log=None):
    """Write registry/<thread>.json atomically. transcript_path is left empty by
    default; the router's context-loop backfill fills it (matched by pane_id).
    Returns the entry dict, or None on error."""
    tid = int(thread_id)
    if inbox_path is None:
        inbox_path = "/tmp/claude-telegram/sessions/%d/inbox.jsonl" % tid
    entry = {
        "thread_id": tid,
        "pane_id": pane_id or None,
        "claude_pid": int(claude_pid) if claude_pid else None,
        "inbox_path": inbox_path,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "spawned": bool(spawned),
        "context": "session attached",
        "registered_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        os.makedirs(registry_dir, exist_ok=True)
        path = os.path.join(registry_dir, "%d.json" % tid)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(entry, fh, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        _log("write_registry_entry %d: %s" % (tid, e), log)
        return None
    return entry


def bind_pane(token, chat_id, pane_id, cwd, claude_pid=None, name=None,
              spawned=False, tmux_bin=TMUX_BIN, registry_dir=REGISTRY_DIR, log=None):
    """The full programmatic bind: create the topic, stamp the pane, write the
    registry. Returns the thread_id (int) or None if the topic couldn't be created
    (in which case nothing is stamped or written — no half-bind). An empty pane_id
    skips the stamp but still registers (the env-var binding can carry a spawn)."""
    if name is None:
        name = os.path.basename(cwd) or "session"
    tid = create_forum_topic(token, chat_id, name, log=log)
    if tid is None:
        return None
    stamp_pane(pane_id, tid, tmux_bin=tmux_bin)
    write_registry_entry(tid, pane_id=pane_id, claude_pid=claude_pid, cwd=cwd,
                         spawned=spawned, registry_dir=registry_dir, log=log)
    return tid


def _self_bind_main():
    """CLI: bind THIS pane ($TMUX_PANE) to a fresh topic, programmatically. Prints
    the thread_id to stdout (for the /telegram skill to capture) on success; writes
    a diagnostic to stderr and exits non-zero on failure. This is what lets a
    keyboard `/telegram` bind without the LLM running createForumTopic/registry —
    it calls the same bind_pane the router's /attach uses."""
    import sys

    token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN", "")
    pane = (os.environ.get("TMUX_PANE") or "").strip()
    if not token:
        sys.stderr.write("bind-self: TELEGRAM_BRIDGE_BOT_TOKEN not set\n")
        return 1
    if not pane:
        sys.stderr.write("bind-self: not in a tmux pane ($TMUX_PANE unset)\n")
        return 1
    try:
        chat_id = json.load(open(os.path.join(STATE_DIR, "state.json")))["chat_id"]
    except (OSError, ValueError, KeyError):
        sys.stderr.write("bind-self: no chat_id in %s/state.json\n" % STATE_DIR)
        return 1

    cwd = os.environ.get("PWD") or os.getcwd()
    # claude is in this pane; store the pane pid as claude_pid — the router resolves
    # a pane from claude_pid by walking UP, so a pane-pid ancestor matches.
    try:
        pane_pid = subprocess.run(
            [TMUX_BIN, "display-message", "-p", "-t", pane, "#{pane_pid}"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pane_pid = ""
    try:
        branch = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    name = "%s@%s" % (os.path.basename(cwd) or "session", branch or "nogit")

    tid = bind_pane(token, chat_id, pane, cwd, claude_pid=pane_pid or None,
                    name=name, spawned=False,
                    log=lambda m: sys.stderr.write(m + "\n"))
    if tid is None:
        sys.stderr.write("bind-self: createForumTopic failed; not bound\n")
        return 1
    print(tid)   # stdout = thread_id, captured by the /telegram skill
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_bind_main())
