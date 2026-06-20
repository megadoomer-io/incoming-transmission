#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-watchdog: detect a tmux pane (Claude session) wedged on an
# interactive prompt that a detached/unattended session can't answer, and alert
# the owner over Telegram so they can rescue it.
#
# WHY: the permission hook auto-resolves gated tools in SPAWNED sessions, but a
# session can still wedge on a prompt the hook doesn't cover (a native prompt
# from a pre-fix session, an ssh-add passphrase, a "trust this folder" dialog,
# or a native AUQ fallback). When that happens the session is blocked, so its
# OWN idle poll cron can't fire to rescue it. A separate watcher is the only
# thing that can notice. This is that watcher.
#
# v1 is ALERT-ONLY: it never sends keystrokes. It scans the `claude` tmux
# session's panes, flags any pane sitting on a prompt for longer than the dwell
# window, maps the pane to its bridge topic when possible, and pings that topic
# (or General if unattached) ONCE per wedge episode. Auto-deny could be a future
# opt-in, but sending a blind keystroke to a pane is too risky to do unattended
# by default.
#
# Runs once per invocation; launchd (StartInterval) calls it on a cadence.
# Stdlib only.

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR", Path.home() / ".local" / "state" / "telegram-bridge"))
REGISTRY_DIR = STATE_DIR / "registry"
STATE_FILE = STATE_DIR / "state.json"
WATCH_STATE = STATE_DIR / "watchdog-state.json"

TMUX_SESSION = os.environ.get("TELEGRAM_WATCHDOG_TMUX_SESSION", "claude")
DWELL_S = int(os.environ.get("TELEGRAM_WATCHDOG_DWELL_S", "180"))
CAPTURE_LINES = 30

DRY_RUN = "--dry-run" in sys.argv

# A pane is "wedged" if its captured tail matches one of these. The named
# patterns are specific prompts; the generic selection-prompt rule catches any
# Claude Code choice menu (a "❯ 1." option line plus the "Esc to cancel" footer
# that only renders on a blocking prompt, not the normal input box).
PROMPT_PATTERNS = [
    ("native-permission", r"Do you want to proceed\?"),
    ("edit-permission", r"Do you want to make this edit"),
    ("create-permission", r"Do you want to create"),
    ("trust-folder", r"Do you trust the files"),
    ("ssh-passphrase", r"Enter passphrase"),
]


def log(msg):
    print("[{}] {}".format(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg), flush=True)


def detect_wedge(text):
    """Return a short signature string if the pane tail looks like a blocking
    prompt, else None."""
    for name, pat in PROMPT_PATTERNS:
        if re.search(pat, text):
            return name
    if "Esc to cancel" in text and re.search(r"❯\s*1\.", text):
        return "selection-prompt"
    return None


def prompt_excerpt(text):
    """A one-line human-readable hint of what the prompt is asking."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("❯") or line.startswith("Esc to cancel"):
            continue
        if re.match(r"\d+\.\s", line):
            continue
        return line[:120]
    return "(prompt)"


def tmux(*args):
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def list_panes():
    """[(pane_id, pane_pid, window_index, window_name), ...] for the session."""
    out = tmux("list-panes", "-s", "-t", TMUX_SESSION,
               "-F", "#{pane_id}\t#{pane_pid}\t#{window_index}\t#{window_name}")
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            panes.append(tuple(parts))
    return panes


def claude_pid_for_pane(pane_pid):
    """Walk descendants of the pane's shell to find the `claude` process pid."""
    try:
        pid = int(pane_pid)
    except (TypeError, ValueError):
        return None
    # BFS down the process tree (panes run a shell whose child is claude, or
    # claude is launched directly as the pane command).
    frontier = [pid]
    for _ in range(20):
        if not frontier:
            break
        nxt = []
        for p in frontier:
            try:
                out = subprocess.run(["ps", "-c", "-o", "pid=,comm=", "--ppid", str(p)],
                                     capture_output=True, text=True, timeout=5).stdout
            except Exception:
                out = ""
            if not out:
                # macOS `ps` lacks --ppid; fall back to scanning all procs once.
                return _claude_pid_scan(pid)
            for row in out.splitlines():
                row = row.strip()
                if not row:
                    continue
                toks = row.split(None, 1)
                cpid = int(toks[0])
                comm = toks[1] if len(toks) > 1 else ""
                if "claude" in comm.lower():
                    return cpid
                nxt.append(cpid)
        frontier = nxt
    return None


def _claude_pid_scan(root_pid):
    """macOS fallback: build the full pid->ppid map and walk down from root."""
    try:
        out = subprocess.run(["ps", "-c", "-o", "pid=,ppid=,comm=", "-ax"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return None
    children = {}
    comm = {}
    for row in out.splitlines():
        toks = row.split(None, 2)
        if len(toks) < 3:
            continue
        pid, ppid, name = int(toks[0]), int(toks[1]), toks[2]
        children.setdefault(ppid, []).append(pid)
        comm[pid] = name
    frontier = [root_pid]
    seen = set()
    while frontier:
        p = frontier.pop()
        if p in seen:
            continue
        seen.add(p)
        if "claude" in comm.get(p, "").lower() and p != root_pid:
            return p
        frontier.extend(children.get(p, []))
    return None


def topic_for_claude_pid(cpid):
    """Return (thread_id, cwd) for the registry entry whose claude_pid matches."""
    if cpid is None or not REGISTRY_DIR.is_dir():
        return None, None
    for f in REGISTRY_DIR.glob("*.json"):
        try:
            reg = json.loads(f.read_text())
        except Exception:
            continue
        if reg.get("claude_pid") == cpid:
            return reg.get("thread_id"), reg.get("cwd")
    return None, None


def send(token, chat_id, thread_id, text):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if thread_id not in (None, "general", ""):
        payload["message_thread_id"] = int(thread_id)
    req = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log("send failed: {}".format(e))


def load_watch_state():
    try:
        return json.loads(WATCH_STATE.read_text())
    except Exception:
        return {}


def save_watch_state(state):
    try:
        WATCH_STATE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log("could not write watch state: {}".format(e))


def main():
    token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN")
    chat_id = None
    try:
        chat_id = json.loads(STATE_FILE.read_text()).get("chat_id")
    except Exception:
        pass

    now = time.time()
    state = load_watch_state()
    seen_panes = set()
    findings = []

    for pane_id, pane_pid, win_idx, win_name in list_panes():
        seen_panes.add(pane_id)
        tail = tmux("capture-pane", "-p", "-t", pane_id)
        sig = detect_wedge(tail)
        prev = state.get(pane_id)

        if sig is None:
            # Pane is not wedged (or recovered). Clear any tracked episode.
            if prev is not None:
                state.pop(pane_id, None)
            continue

        if prev is None or prev.get("sig") != sig:
            # New wedge episode (or the prompt changed).
            state[pane_id] = {"sig": sig, "first_seen": now, "alerted": False,
                              "win": win_name}
            findings.append((pane_id, win_idx, win_name, sig, 0, False))
            continue

        dwell = now - prev.get("first_seen", now)
        already = prev.get("alerted", False)
        ripe = dwell >= DWELL_S
        findings.append((pane_id, win_idx, win_name, sig, int(dwell), already or ripe))

        if ripe and not already:
            cpid = claude_pid_for_pane(pane_pid)
            thread_id, cwd = topic_for_claude_pid(cpid)
            mins = int(dwell // 60)
            where = "topic {}".format(thread_id) if thread_id else "General (unattached)"
            excerpt = prompt_excerpt(tail)
            alert = (
                "⚠️ Wedged session: window '{}' (pane {}) has been stuck "
                "~{}m on a prompt:\n  {}\n\nIt can't rescue itself (its poll cron "
                "can't fire while blocked). Attach with `tmux attach -t {}`, switch "
                "to that window, and answer the prompt.".format(
                    win_name, pane_id, mins, excerpt, TMUX_SESSION))
            log("ALERT pane={} win={} sig={} dwell={}s -> {}".format(
                pane_id, win_name, sig, int(dwell), where))
            if not DRY_RUN and token and chat_id is not None:
                send(token, chat_id, thread_id, alert)
            if not DRY_RUN:
                prev["alerted"] = True
                state[pane_id] = prev

    # Prune entries for panes that no longer exist.
    for gone in [p for p in state if p not in seen_panes]:
        state.pop(gone, None)

    if DRY_RUN:
        print("DRY RUN — dwell={}s, panes scanned={}".format(DWELL_S, len(seen_panes)))
        if not findings:
            print("  no wedged panes detected")
        for pane_id, win_idx, win_name, sig, dwell, would_alert in findings:
            print("  pane {} win {}:{} sig={} dwell={}s would_alert={}".format(
                pane_id, win_idx, win_name, sig, dwell, would_alert))
        return

    save_watch_state(state)


if __name__ == "__main__":
    main()
