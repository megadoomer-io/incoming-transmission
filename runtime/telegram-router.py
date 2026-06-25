#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-router: the sole reader of the bridge bot's update stream.
#
# Telegram's getUpdates is single-consumer (offset-based), so exactly ONE
# process may poll a bot. This daemon is that process. It long-polls getUpdates,
# locks to a single owner (Telegram username/user id), and routes each message
# by its forum-topic id (message_thread_id) into a per-session inbox file that a
# live Claude session drains via a session-scoped cron. Messages for topics with
# no attached session get a polite "no session" reply (Phase 1; headless dispatch
# is Phase 2).
#
# Runtime state (NOT in the dotfiles repo): ~/.local/state/telegram-bridge/
#   state.json              {offset, chat_id, allowed_user_id, allowed_username}
#   registry/<thread>.json  ownership records written by the /telegram skill
# Inboxes (ephemeral):      /tmp/claude-telegram/sessions/<thread>/inbox.jsonl
#
# Stdlib only, targets /usr/bin/python3 (3.9) so launchd needs no PATH/uv.

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOKEN = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN", "")
API = "https://api.telegram.org/bot{}/{}".format(TOKEN, "{}")

STATE_DIR = Path(os.environ.get("TELEGRAM_BRIDGE_STATE_DIR",
                                Path.home() / ".local" / "state" / "telegram-bridge"))
STATE_FILE = STATE_DIR / "state.json"
REGISTRY_DIR = STATE_DIR / "registry"
# Per-topic pinned-status ("sticky") bookkeeping, owned exclusively by this
# daemon so it never races the /telegram skill's registry writes.
STICKY_DIR = STATE_DIR / "sticky"
# message_id -> topic index. A message_reaction update carries no
# message_thread_id, so to route a reaction back to its session we look the
# reacted message_id up here. Append-only JSONL written by BOTH this router
# (incoming messages + its own sends) and telegram-send.sh (session replies),
# read on demand when a reaction arrives. Bounded by MSG_INDEX_MAX_LINES.
MSG_INDEX = STATE_DIR / "msg-index.jsonl"
MSG_INDEX_MAX_LINES = 5000
INBOX_ROOT = Path("/tmp/claude-telegram/sessions")
BRIDGE_DIR = Path.home() / ".telegram-bridge"
SPAWN_SCRIPT = BRIDGE_DIR / "telegram-spawn.sh"
# Context gauge: in the smart-router / dumb-session model the router computes each
# attached session's context occupancy. It shells out to telegram-context.py on its
# own timer thread (OFF the getUpdates path so a big transcript scan can't stall
# message routing).
CONTEXT_SCRIPT = BRIDGE_DIR / "telegram-context.py"
# Tunables for the status sticky + auto-compaction, read live each loop so the
# user can edit thresholds without restarting the daemon. Defaults match the
# shipped compaction.json.
CONFIG_FILE = BRIDGE_DIR / "compaction.json"
CONFIG_DEFAULTS = {"trigger_pct": 0.85, "warn_pct": 0.75, "kill_old": False,
                   "context_interval_seconds": 90, "backstop_seconds": 300}

LONG_POLL_SECONDS = 30
# socket timeout must exceed the long-poll window so the connection isn't torn
# down mid-wait; +15s of slack covers handshake/latency.
SOCKET_TIMEOUT = LONG_POLL_SECONDS + 15
# After laptop sleep the process can get stuck on "[Errno 9] Bad file descriptor"
# that fresh urlopen calls don't clear. Rather than churn forever, exit after this
# many consecutive failures and let launchd (KeepAlive=true) respawn a clean
# process with fresh sockets.
MAX_NET_FAILURES = 6

# Keep the Mac awake while the bridge is running. We spawn `caffeinate -w <our pid>`
# as a child: it holds the no-sleep assertion until THIS router process exits, then
# releases cleanly — on `telegram-bridge stop`, on crash, or on a KeepAlive restart
# (the replacement spawns its own). Tying the assertion to our pid via -w avoids the
# orphaned-assertion leak you get from wrapping the launchd ProgramArguments.
CAFFEINATE_BIN = "/usr/bin/caffeinate"

_running = True
_caffeinate = None  # Popen handle for the keep-awake child
# Backstop throttle: monotonic timestamp of the last drain nudge per topic key.
# Written by wake_session (on both the main thread's push and the context
# thread's backstop) and read by the context loop to avoid re-nudging an
# undrained inbox more often than backstop_seconds. Dict get/set is atomic under
# the GIL; this is best-effort throttling, so the rare cross-thread race (one
# extra or one skipped nudge) is harmless.
_backstop_nudged_at = {}


def start_caffeinate():
    """Best-effort: prevent idle/system sleep for this router's lifetime."""
    global _caffeinate
    if not os.path.exists(CAFFEINATE_BIN):
        log("caffeinate not found at {}; sleep not inhibited".format(CAFFEINATE_BIN))
        return
    try:
        _caffeinate = subprocess.Popen(
            [CAFFEINATE_BIN, "-i", "-s", "-w", str(os.getpid())])
        log("caffeinate started (pid {}) — Mac stays awake while the bridge runs"
            .format(_caffeinate.pid))
    except OSError as e:
        log("WARN could not start caffeinate: {}".format(e))


def log(msg):
    print("[{}] {}".format(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg),
          flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- state -----------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError) as e:
            log("WARN: could not read state ({}); starting fresh".format(e))
    return {"offset": 0, "chat_id": None, "allowed_user_id": None, "allowed_username": None}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def thread_key(message_thread_id):
    return str(message_thread_id) if message_thread_id is not None else "general"


def normalize_command(text):
    """Strip the @botname suffix Telegram appends to slash-commands sent from the
    in-app command menu in a group (e.g. '/end@your_bot' -> '/end').

    Only the FIRST token is touched, and only when it starts with '/', so a real
    command never reaches a session (or the daemon dispatcher) carrying the bot
    handle. A non-command message, or a '@mention' that is its own token (e.g.
    '/track @someone'), is left untouched. This is the single point that lets
    BOTH session-level commands (/end, /compact, ...) and daemon-level commands
    (/new, /whoami, ...) match whether or not the menu appended the handle."""
    if not text or not text.startswith("/"):
        return text
    parts = text.split(maxsplit=1)
    head = parts[0].split("@", 1)[0]
    return head + (" " + parts[1] if len(parts) > 1 else "")


def load_registry(tkey):
    f = REGISTRY_DIR / "{}.json".format(tkey)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (ValueError, OSError):
            return None
    return None


# --- message_id -> topic index (for routing reactions) ---------------------

def record_msg_thread(message_id, tkey):
    """Remember which topic a message_id belongs to so a later reaction on it
    can be routed (reaction updates carry no message_thread_id). Best-effort:
    append a line, swallow errors, trim when the file grows past the cap."""
    if message_id is None or tkey is None:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with MSG_INDEX.open("a") as fh:
            fh.write(json.dumps({"message_id": int(message_id),
                                 "thread": str(tkey)}) + "\n")
        _maybe_trim_msg_index()
    except (OSError, ValueError) as e:
        log("WARN record_msg_thread: {}".format(e))


def _maybe_trim_msg_index():
    """Keep the index bounded: when it exceeds the cap, rewrite with the most
    recent MSG_INDEX_MAX_LINES lines (recent ids are the ones reactions hit)."""
    try:
        if not MSG_INDEX.is_file():
            return
        with MSG_INDEX.open() as fh:
            lines = fh.readlines()
        if len(lines) <= MSG_INDEX_MAX_LINES:
            return
        tmp = MSG_INDEX.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(lines[-MSG_INDEX_MAX_LINES:]))
        tmp.replace(MSG_INDEX)
    except OSError as e:
        log("WARN trim msg-index: {}".format(e))


def lookup_msg_thread(message_id):
    """Resolve a reacted message_id back to its topic key. Last write wins.
    Returns the topic key string, or None if the id isn't in the index."""
    if message_id is None or not MSG_INDEX.is_file():
        return None
    found = None
    try:
        with MSG_INDEX.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("message_id") == message_id:
                    found = rec.get("thread")
    except OSError as e:
        log("WARN lookup_msg_thread: {}".format(e))
    return found


def _emoji_set(reactions):
    """Flatten a Telegram ReactionType list to a set of display strings:
    standard emoji as their unicode char, custom emoji as 'custom:<id>'."""
    out = set()
    for r in reactions or []:
        rtype = r.get("type")
        if rtype == "emoji" and r.get("emoji"):
            out.add(r["emoji"])
        elif rtype == "custom_emoji" and r.get("custom_emoji_id"):
            out.add("custom:{}".format(r["custom_emoji_id"]))
    return out


# --- telegram api ----------------------------------------------------------

def api_call(method, params=None, timeout=SOCKET_TIMEOUT):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(API.format(method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send_message(chat_id, text, thread_id=None, reply_to=None):
    # Telegram caps message text at 4096 chars; chunk on the boundary.
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    last = None
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        if thread_id is not None and thread_id != "general":
            params["message_thread_id"] = thread_id
        if reply_to is not None and i == 0:
            params["reply_to_message_id"] = reply_to
        try:
            last = api_call("sendMessage", params)
        except (urllib.error.URLError, OSError) as e:
            log("ERROR sendMessage: {}".format(e))
            return None
        # Index this sent message so a reaction on it routes back here.
        mid = (last or {}).get("result", {}).get("message_id")
        tk = "general" if thread_id in (None, "general") else str(thread_id)
        record_msg_thread(mid, tk)
    return last


def react_eyes(chat_id, message_id):
    try:
        api_call("setMessageReaction", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": json.dumps([{"type": "emoji", "emoji": "\U0001F440"}]),
        })
    except (urllib.error.URLError, OSError) as e:
        log("WARN setMessageReaction: {}".format(e))


def answer_callback(cq_id, text=None):
    """Acknowledge a tapped inline button so the client stops its spinner."""
    params = {"callback_query_id": cq_id}
    if text:
        params["text"] = text
    try:
        api_call("answerCallbackQuery", params)
    except (urllib.error.URLError, OSError) as e:
        log("WARN answerCallbackQuery: {}".format(e))


def clear_buttons(chat_id, message_id):
    """Remove the inline keyboard from a decided approval message."""
    if chat_id is None or message_id is None:
        return
    try:
        api_call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id})
    except (urllib.error.URLError, OSError) as e:
        log("WARN editMessageReplyMarkup: {}".format(e))


def set_buttons(chat_id, message_id, markup):
    """Replace a message's inline keyboard in place (multi-select toggle)."""
    if chat_id is None or message_id is None or markup is None:
        return
    try:
        api_call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": json.dumps(markup)})
    except (urllib.error.URLError, OSError) as e:
        log("WARN editMessageReplyMarkup(set): {}".format(e))


def auq_multi_markup(tkey, pending):
    """Build the multi-select toggle keyboard from a pending-question record.

    The MCP server writes the option labels + current selection into
    auq-pending.json; we render one toggle button per option (✅ when selected,
    ▫️ when not) plus a Done button. callback_data mirrors the MCP's scheme so the
    initial keyboard and our re-renders are interchangeable.
    """
    opts = pending.get("options") or []
    selected = set(pending.get("selected") or [])
    qidx = pending.get("qidx", 0)
    rows = []
    for i, label in enumerate(opts):
        mark = "✅" if i in selected else "▫️"
        rows.append([{"text": "{} {}. {}".format(mark, i + 1, str(label)[:48]),
                      "callback_data": "auqm:{}:{}:{}".format(tkey, qidx, i)}])
    rows.append([{"text": "✅ Done", "callback_data": "auqd:{}:{}".format(tkey, qidx)}])
    return {"inline_keyboard": rows}


# --- status sticky (pinned per-topic context gauge) ------------------------
#
# Each attached topic shows a phone-can't-see-the-statusbar gauge as a pinned
# message: "cwd · N msgs · ~XX% ctx · updated HH:MM", with ⚠️ past the warn
# threshold. The numbers come from each topic's status.json (written by the
# router's context_loop). We own a per-thread sidecar in STICKY_DIR so we never
# race the /telegram skill's registry writes, and we only call editMessageText
# when the rendered text actually changed (avoids 429s and "not modified" 400s).

def load_compaction_cfg():
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except (ValueError, OSError):
        return dict(CONFIG_DEFAULTS)
    out = dict(CONFIG_DEFAULTS)
    if isinstance(cfg, dict):
        out.update({k: cfg[k] for k in CONFIG_DEFAULTS if k in cfg})
    return out


def read_status(tkey):
    f = INBOX_ROOT / tkey / "status.json"
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return None


def undrained_count(reg):
    """How many inbox lines the session has not yet consumed: total inbox lines
    minus SESS/read.offset (the count the session writes back after a drain). >0
    means messages are waiting for the session to read them. Best-effort — any
    read error returns 0 (assume drained) so the backstop never spins on a
    transient fs hiccup."""
    try:
        inbox = Path(reg.get("inbox_path", ""))
        if not inbox.is_file():
            return 0
        with inbox.open() as fh:
            total = sum(1 for _ in fh)
        off_f = inbox.parent / "read.offset"
        try:
            offset = int((off_f.read_text().strip() or "0"))
        except (ValueError, OSError):
            offset = 0
        return max(0, total - offset)
    except OSError:
        return 0


def load_sticky(tkey):
    f = STICKY_DIR / "{}.json".format(tkey)
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return None


def save_sticky(tkey, data):
    try:
        STICKY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STICKY_DIR / "{}.json.tmp".format(tkey)
        tmp.write_text(json.dumps(data))
        tmp.replace(STICKY_DIR / "{}.json".format(tkey))
    except OSError as e:
        log("WARN save_sticky {}: {}".format(tkey, e))


def edit_message_text(chat_id, message_id, text):
    try:
        api_call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "disable_web_page_preview": "true",
        })
        return True
    except (urllib.error.URLError, OSError) as e:
        # 400 "message is not modified" can't happen (we diff first); other
        # errors (deleted message, etc.) just drop the sticky so it's re-created.
        log("WARN editMessageText {}: {}".format(message_id, e))
        return False


def pin_message(chat_id, message_id, thread_id):
    """Pin a status message. For a forum topic, message_thread_id scopes the pin
    to that topic rather than the chat-level pin bar."""
    params = {"chat_id": chat_id, "message_id": message_id,
              "disable_notification": "true"}
    if thread_id not in (None, "general", ""):
        params["message_thread_id"] = thread_id
    try:
        api_call("pinChatMessage", params)
    except (urllib.error.URLError, OSError) as e:
        log("WARN pinChatMessage {}: {}".format(message_id, e))


def render_sticky(reg, status, warn_pct):
    base = os.path.basename(str(reg.get("cwd") or "")) or "session"
    if not status:
        return "\U0001F4CD {} · waiting for status…".format(base)
    pct = status.get("pct")
    msgs = status.get("msgs", 0)
    updated = str(status.get("updated") or "")
    # status.updated is UTC ISO; show local HH:MM for a phone glance.
    hhmm = "?"
    if updated:
        try:
            dt = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            hhmm = dt.astimezone().strftime("%H:%M")
        except ValueError:
            hhmm = updated[11:16] or "?"
    pct_txt = "~{}% ctx".format(int(round((pct or 0) * 100)))
    marker = "⚠️" if (pct is not None and pct >= warn_pct) else "\U0001F4CD"
    return "{} {} · {} msgs · {} · {}".format(marker, base, msgs, pct_txt, hhmm)


def update_stickies(state):
    """Refresh the pinned status message for every attached topic. Called once
    per main-loop iteration (≈ every long-poll window). Cheap: only edits when
    the rendered text changed; creates+pins on first sight; prunes sidecars whose
    registry is gone."""
    chat_id = state.get("chat_id")
    if chat_id is None or not REGISTRY_DIR.is_dir():
        return
    cfg = load_compaction_cfg()
    warn_pct = cfg.get("warn_pct", 0.75)

    live_keys = set()
    for f in REGISTRY_DIR.glob("*.json"):
        tkey = f.stem
        live_keys.add(tkey)
        try:
            reg = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        thread_id = reg.get("thread_id")
        if thread_id is None:
            continue
        status = read_status(tkey)
        text = render_sticky(reg, status, warn_pct)
        sticky = load_sticky(tkey) or {}
        if sticky.get("text") == text:
            continue  # nothing changed; skip the API call
        mid = sticky.get("message_id")
        if mid:
            if edit_message_text(chat_id, mid, text):
                save_sticky(tkey, {"message_id": mid, "text": text})
            else:
                # Edit failed (message gone) — drop so we re-create next loop.
                try:
                    (STICKY_DIR / "{}.json".format(tkey)).unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            sent = send_message(chat_id, text, thread_id=thread_id)
            new_id = (sent or {}).get("result", {}).get("message_id") if sent else None
            if new_id:
                pin_message(chat_id, new_id, thread_id)
                save_sticky(tkey, {"message_id": new_id, "text": text})

    # Prune sticky sidecars for topics that no longer have a registry (ended).
    if STICKY_DIR.is_dir():
        for sf in STICKY_DIR.glob("*.json"):
            if sf.stem not in live_keys:
                try:
                    sf.unlink(missing_ok=True)
                except OSError:
                    pass


# Command list registered with Telegram so the "/" menu offers tappable
# suggestions (easier than typing on a phone). Two groups share the menu:
#   daemon-handled  : new, sessions, whoami, help        (handled here, any topic)
#   session-handled : status, context, compact, dir,     (routed to the attached
#                     dirs, end                           session's inbox; the
#                                                         session drains + acts)
# context/compact have NO handler below, so they fall through to the routing path
# like status/dir — the session executes them. Names must be 1-32 chars, lowercase
# a-z/0-9/_. Order = menu order.
BRIDGE_COMMANDS = [
    ("new", "Spawn a new Claude session (dir/alias + optional intent note)"),
    ("attach", "Adopt an existing unattached Claude session into a topic"),
    ("sessions", "List attached Claude sessions"),
    ("status", "This session: cwd + messages processed"),
    ("context", "This session: live context % gauge"),
    ("compact", "Roll this session over to a fresh one now"),
    ("dir", "Change reply working dir (alias or path)"),
    ("dirs", "List directory aliases"),
    ("end", "Detach this session, close its topic"),
    ("whoami", "Show chat id, your user id, topic id"),
    ("help", "Bridge help"),
]


def register_commands(chat_id):
    """Publish BRIDGE_COMMANDS via setMyCommands, scoped to the control chat.

    BotCommandScopeChat makes the suggestions show in this group and its forum
    topics. Idempotent — safe on every startup and right after bootstrap. A
    failure is non-fatal (the commands still work when typed manually).
    """
    if chat_id is None:
        return
    cmds = [{"command": c, "description": d} for c, d in BRIDGE_COMMANDS]
    try:
        api_call("setMyCommands", {
            "commands": json.dumps(cmds),
            "scope": json.dumps({"type": "chat", "chat_id": chat_id}),
        })
        log("registered {} bot commands for chat {}".format(len(cmds), chat_id))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("WARN setMyCommands: {}".format(e))


def download_file(file_id, dest_dir, basename):
    """Download a Telegram file (photo/document) to dest_dir/<basename><ext>.

    Two hops: getFile resolves a temporary file_path, then we GET it from the
    file endpoint. Returns the saved path (str) or None on any failure — a
    missing image must never crash routing.
    """
    if not file_id:
        return None
    try:
        info = api_call("getFile", {"file_id": file_id})
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("WARN getFile: {}".format(e))
        return None
    if not info.get("ok"):
        log("WARN getFile not ok: {}".format(info))
        return None
    file_path = info.get("result", {}).get("file_path")
    if not file_path:
        return None
    ext = os.path.splitext(file_path)[1] or ".jpg"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log("WARN image dir: {}".format(e))
        return None
    dest = dest_dir / "{}{}".format(basename, ext)
    url = "https://api.telegram.org/file/bot{}/{}".format(TOKEN, file_path)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        dest.write_bytes(data)
    except (urllib.error.URLError, OSError) as e:
        log("WARN file download: {}".format(e))
        return None
    return str(dest)


# --- message handling ------------------------------------------------------

WELCOME = (
    "✅ Telegram bridge is live.\n\n"
    "I'm this Claude Code bridge bot. This chat is now the control group, locked "
    "to @{username}.\n\n"
    "How it works: in an active Claude session, run /telegram and I open a topic "
    "here for that session. Whatever you type in a topic goes straight to that "
    "live session, which replies here with full context, tools, and memory.\n\n"
    "Commands: /whoami, /sessions, /help\n\n"
    "Reply to this message to confirm the round-trip works. \U0001F355"
)

HELP = (
    "Telegram bridge — commands:\n"
    "/new [dir] [intent note] — spawn a NEW Claude session in dir (default: home) "
    "and auto-attach it to its own topic; any trailing text is an optional intent "
    "note the fresh session starts with\n"
    "/attach [n|window] — adopt an existing, unattached Claude session in the tmux "
    "session into its own topic (no arg lists candidates). Unlike /new, it's not "
    "reaped on /end — just detached.\n"
    "/whoami  — show this chat id, your user id, and the current topic id\n"
    "/sessions — list Claude sessions attached to topics\n"
    "/help — this message\n\n"
    "Inside a session's topic: /status, /context, /compact, /dir <alias|path>, "
    "/dirs, /end. Anything else (including /track, /ship, etc.) goes to that "
    "session as a task.\n\n"
    "To attach an EXISTING session: run /telegram inside it. It creates a topic "
    "here; type in that topic to talk to that session."
)


# --- wake: nudge a session's tmux pane to DRAIN its inbox NOW ------------------
# Best-effort and ADDITIVE: wake runs only AFTER a message is already in the
# inbox, so any failure here never affects delivery (the router's backstop still
# bounds the fallback latency). Pane matching is STRICT — a pane is only nudged when its
# live `claude` pid equals the topic's registry claude_pid — so keystrokes can
# never land in the wrong pane (an interactive session, another topic).
TMUX_BIN = os.environ.get("TELEGRAM_BRIDGE_TMUX", "/opt/homebrew/bin/tmux")
TMUX_SESSION = os.environ.get("TELEGRAM_BRIDGE_TMUX_SESSION", "claude")


def _tmux(*args):
    try:
        return subprocess.run([TMUX_BIN, *args], capture_output=True,
                              text=True, timeout=10).stdout
    except Exception:
        return ""


def _ppid_map():
    """pid -> ppid for every process, from one `ps` call (macOS-friendly)."""
    try:
        out = subprocess.run(["ps", "-o", "pid=,ppid=", "-ax"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    m = {}
    for line in out.splitlines():
        toks = line.split()
        if len(toks) < 2:
            continue
        try:
            m[int(toks[0])] = int(toks[1])
        except ValueError:
            continue
    return m


def _pane_for_claude_pid(target_pid):
    """The tmux pane whose process IS, or is an ancestor of, target_pid.

    Spawned bridge sessions launch `claude` directly as the pane command, so the
    registry's claude_pid equals the pane's #{pane_pid}; a shell-wrapped session
    has claude as a descendant. Walking UP from target_pid to the first pid that
    is a pane_pid handles both, and matches by pid identity (never by name), so a
    nudge can only ever reach the exact session that owns the topic."""
    out = _tmux("list-panes", "-s", "-t", TMUX_SESSION,
                "-F", "#{pane_id}\t#{pane_pid}")
    pane_by_pid = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        try:
            pane_by_pid[int(parts[1])] = parts[0]
        except ValueError:
            continue
    if not pane_by_pid:
        return None
    ppid_of = _ppid_map()
    pid = target_pid
    for _ in range(64):  # bounded walk up the process tree
        if pid in pane_by_pid:
            return pane_by_pid[pid]
        nxt = ppid_of.get(pid)
        if not nxt or nxt == pid or nxt <= 1:
            break
        pid = nxt
    return None


def list_unattached_claude_panes():
    """Claude panes in the shared tmux session that are NOT already bridged — the
    candidates for /attach. A pane is a claude session if its `pane_current_command`
    is claude (an idle TUI; a session mid-tool may briefly read as `bash` and be
    missed — fine, /attach is for an idle session you're choosing to bind). "Already
    bridged" = some registry entry's claude_pid resolves (via _pane_for_claude_pid)
    to that pane. Returns [(pane_id, window_index, window_name), ...]."""
    out = _tmux("list-panes", "-s", "-t", TMUX_SESSION,
                "-F", "#{pane_id}\t#{window_index}\t#{window_name}\t#{pane_current_command}")
    claude_panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        pane_id, widx, wname, curcmd = parts
        # Adopt only LIVE, user claude sessions in normally-named windows. Skip
        # bridge-managed windows (tg:* — the bridge's domain; an unregistered one is
        # a STALE bridge session, a #6 reap concern, not an adopt candidate) and
        # retired windows (DEAD ...).
        if ("claude" in curcmd.lower()
                and not wname.startswith("tg:")
                and not wname.startswith("DEAD")):
            claude_panes.append((pane_id, widx, wname))
    bridged = set()
    if REGISTRY_DIR.is_dir():
        for f in REGISTRY_DIR.glob("*.json"):
            try:
                cp = json.loads(f.read_text()).get("claude_pid")
            except (ValueError, OSError):
                continue
            if cp:
                pane = _pane_for_claude_pid(int(cp))
                if pane:
                    bridged.add(pane)
    return [p for p in claude_panes if p[0] not in bridged]


def _nudge_pane(reg, text, kind):
    """Best-effort: send-keys a one-line instruction into the topic's pane (the
    pid-matched session). One line (no embedded newline) so Enter submits exactly
    once. Never raises. Returns True if a pane was found and nudged.

    INVARIANT: a nudge is a drain/compact IMPERATIVE only — it never carries the
    message payload. The payload always travels via the inbox; the nudge just
    tells the session to go read it. Keeping payload out of the nudge means a
    nudge can never be mistaken for task content."""
    try:
        cpid = reg.get("claude_pid")
        thread_id = reg.get("thread_id")
        if not cpid or thread_id is None:
            return False
        pane = _pane_for_claude_pid(int(cpid))
        if not pane:
            log("{}: no matching pane for topic {} (claude_pid {})".format(kind, thread_id, cpid))
            return False
        _tmux("send-keys", "-t", pane, "-l", text)
        _tmux("send-keys", "-t", pane, "Enter")
        log("{}: nudged topic {} (pane {})".format(kind, thread_id, pane))
        return True
    except Exception as e:
        log("{}: error (ignored): {}".format(kind, e))
        return False


def wake_session(reg):
    """Push delivery (PRIMARY path): nudge the topic's pane to DRAIN its inbox now.

    In the smart-router / dumb-session model the session runs NO cron at all — it
    is purely reactive. A message waits on this push nudge (issued the instant the
    router routes it) or, if the nudge was missed (session busy, pane briefly
    gone), on the router's backstop re-nudge from context_loop. Either way the
    router owns all timing; the session just drains when told.

    The nudge is self-contained for the common case (it lists the drain steps
    inline) so a session whose attach-time bridge procedure has since compacted
    away can still drain from the nudge alone."""
    thread_id = reg.get("thread_id")
    nudge = ("TELEGRAM WAKE {tid}: a message arrived. DRAIN INBOX now (section A of your "
             "bridge procedure): run `~/.telegram-bridge/telegram-inbox.sh drain {tid}`, "
             "reply to each printed line via telegram-send.sh, then "
             "`~/.telegram-bridge/telegram-inbox.sh ack {tid}`. "
             "The router owns timing and your context gauge — you have no cron.").format(tid=thread_id)
    _nudge_pane(reg, nudge, "wake")
    # Throttle the backstop: record this attempt regardless of whether a pane was
    # found, so context_loop waits backstop_seconds before re-nudging (a missing
    # pane means a dead session; re-nudging it every tick would spin).
    tkey = Path(reg.get("inbox_path", "")).parent.name
    if tkey:
        _backstop_nudged_at[tkey] = time.monotonic()


def nudge_compact(reg):
    """Router-driven compaction: when the context thread sees a session cross
    trigger_pct (and it is not already compacting), nudge it to run its COMPACTION
    HANDOFF. Only the live process can /context-save + spawn its replacement, so
    the router can only ask; the session runs the handoff when nudged."""
    thread_id = reg.get("thread_id")
    nudge = ("TELEGRAM WAKE {tid}: /compact — your context gauge crossed the trigger. "
             "Run the COMPACTION HANDOFF from your bridge procedure now (save context, "
             "spawn a fresh replacement in this same topic, hand off).").format(tid=thread_id)
    _nudge_pane(reg, nudge, "compact")


def compute_status(reg, tkey):
    """Run telegram-context.py for one topic so it (re)writes SESS/status.json.
    The router owns the gauge. Best-effort with a timeout so a huge transcript scan
    can't wedge the context thread; runs under the router's own interpreter
    (context.py is pure stdlib python3)."""
    tp = reg.get("transcript_path")
    if not tp or not CONTEXT_SCRIPT.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(CONTEXT_SCRIPT), "--transcript", tp, "--thread", tkey],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        log("context: compute failed for topic {}: {}".format(tkey, e))


def _pid_alive(pid):
    """True if the process is alive. os.kill(pid, 0) signals nothing but raises
    ProcessLookupError if the pid is gone."""
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True  # exists, owned by another user (same-user here; defensive)


def _reclaim_stale_locks(reg, tkey, poll_ttl, handoff_ttl):
    """Clear a stale poll.lock.d / compacting.lock so a dead or abandoned drain or
    handoff can't wedge the topic (issue #8, FM1/FM2/FM5). A lock is stale when the
    session's claude_pid is dead, OR the lock's own mtime (its acquire time) is older
    than its TTL. pid-death is the fast, unambiguous signal; the TTL is the backstop
    for a session that is alive but abandoned its lock. The session also self-heals
    its own poll.lock.d in the drain procedure, but the router is the ONLY rescuer
    once the session process is gone."""
    cpid = reg.get("claude_pid")
    pid_dead = cpid is not None and not _pid_alive(cpid)
    now = time.time()
    sess = INBOX_ROOT / tkey
    poll = sess / "poll.lock.d"
    try:
        if poll.is_dir():
            age = now - poll.stat().st_mtime
            if pid_dead or age > poll_ttl:
                poll.rmdir()
                log("reclaimed stale poll.lock.d for topic {} (pid_dead={}, age={:.0f}s)".format(
                    tkey, pid_dead, age))
    except OSError:
        pass
    comp = sess / "compacting.lock"
    try:
        if comp.is_file():
            age = now - comp.stat().st_mtime
            if pid_dead or age > handoff_ttl:
                comp.unlink()
                log("reclaimed stale compacting.lock for topic {} (pid_dead={}, age={:.0f}s)".format(
                    tkey, pid_dead, age))
    except OSError:
        pass


def context_loop():
    """Daemon thread: the router's timing engine, OFF the getUpdates path (a big
    transcript scan or a re-nudge must never stall the message pump). On each
    interval, for every attached topic it:

      1. refreshes the context gauge (writes status.json the main loop reads),
      2. nudges /compact if the session crossed trigger_pct (and isn't already
         handing off),
      3. backstops delivery: re-nudges a DRAIN if the inbox is still undrained
         (the push nudge in handle_message was missed), throttled to
         backstop_seconds and skipped entirely when the inbox is drained.

    This is what lets the session run NO cron: the router both pushes on arrival
    and re-pushes undrained inboxes. update_stickies() (on the main loop) still
    just READS the status.json this thread writes — fast, no scan."""
    while _running:
        cfg = load_compaction_cfg()
        interval = max(15, int(cfg.get("context_interval_seconds", 90) or 90))
        trigger = cfg.get("trigger_pct", 0.85)
        backstop = max(60, int(cfg.get("backstop_seconds", 300) or 300))
        poll_lock_ttl = max(60, int(cfg.get("poll_lock_ttl_seconds", 1800) or 1800))
        handoff_lock_ttl = max(60, int(cfg.get("handoff_lock_ttl_seconds", 600) or 600))
        if REGISTRY_DIR.is_dir():
            for f in REGISTRY_DIR.glob("*.json"):
                tkey = f.stem
                try:
                    reg = json.loads(f.read_text())
                except (ValueError, OSError):
                    continue
                if reg.get("thread_id") is None:
                    continue
                compute_status(reg, tkey)
                # Clear a stale poll.lock.d / compacting.lock first, so a dead or
                # abandoned drain/handoff can't wedge the topic and block the
                # compaction + backstop checks below (issue #8, FM1/FM2/FM5).
                _reclaim_stale_locks(reg, tkey, poll_lock_ttl, handoff_lock_ttl)
                # Router-driven compaction trigger detection. Guard on
                # compacting.lock so we nudge once, not every interval while the
                # session is mid-handoff (the session creates the lock when it
                # starts the handoff and removes it at the cutover).
                status = read_status(tkey)
                pct = (status or {}).get("pct")
                lock = INBOX_ROOT / tkey / "compacting.lock"
                if pct is not None and pct >= trigger and not lock.exists():
                    nudge_compact(reg)
                # Backstop the push delivery. handle_message nudges the moment it
                # routes a message; if that nudge was missed (session mid-task, or
                # the pane was briefly gone) the inbox stays undrained — re-issue
                # the drain nudge, but ONLY while something is actually undrained
                # and no more than once per backstop_seconds. A drained inbox is
                # never nudged, so an idle session costs zero tokens. Skip while a
                # handoff is in flight (the session is busy compacting).
                elif undrained_count(reg) > 0 and not lock.exists():
                    if (time.monotonic() - _backstop_nudged_at.get(tkey, 0.0)) >= backstop:
                        wake_session(reg)
        # Sleep in 1s slices so SIGTERM/shutdown is responsive.
        slept = 0
        while _running and slept < interval:
            time.sleep(1)
            slept += 1


# --- rich permission round-trip --------------------------------------------
#
# The in-session permission hook writes perm-pending.json and polls
# perm-answer.json. We translate a button tap or a typed reply into that answer
# file. The owner's free-text note/redirect is delivered to the model via the
# TRUSTED inbox (a spawned model distrusts hook-injected text), while the
# allow/deny DECISION rides perm-answer.json to unblock the hook.
PERM_APPROVE = {"y", "yes", "ok", "okay", "approve", "approved", "allow", "go", "yep", "sure"}
PERM_DENY = {"n", "no", "deny", "denied", "reject", "stop", "nope"}
PERM_ALWAYS = {"always", "always-allow", "alwaysallow"}


def write_perm_answer(session_dir, decision, note=None):
    """Hand the hook its decision (and whether a note was sent)."""
    try:
        rec = {"decision": decision, "ts": now_iso()}
        if note:
            rec["note"] = note
        (session_dir / "perm-answer.json").write_text(json.dumps(rec))
        return True
    except OSError as e:
        log("ERROR perm-answer write: {}".format(e))
        return False


def inject_inbox_note(reg, text, from_id, username):
    """Deliver the owner's note/redirect as a TRUSTED inbox message the session
    drains once the gated tool resolves (the hook only carries the decision)."""
    try:
        inbox = Path(reg["inbox_path"])
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a") as fh:
            fh.write(json.dumps({"ts": now_iso(), "from": from_id,
                                 "username": username, "text": text}) + "\n")
        return True
    except OSError as e:
        log("ERROR inbox note inject: {}".format(e))
        return False


def handle_message(msg, state):
    chat = msg.get("chat", {})
    frm = msg.get("from", {})
    chat_id = chat.get("id")
    from_id = frm.get("id")
    username = frm.get("username")
    text = normalize_command((msg.get("text") or "").strip())
    caption = normalize_command((msg.get("caption") or "").strip())
    message_id = msg.get("message_id")
    thread_id = msg.get("message_thread_id")
    tkey = thread_key(thread_id)

    # Image attachments: a phone photo arrives as `photo` (size variants — last
    # is largest), an image file as a `document` with an image/* mime type. The
    # caption (if any) is the accompanying instruction. Commands (/...) never
    # carry images, so this only matters on the routing path below.
    photo = msg.get("photo") or []
    doc = msg.get("document") or {}
    image_file_id = None
    if photo:
        image_file_id = photo[-1].get("file_id")
    elif str(doc.get("mime_type", "")).startswith("image/"):
        image_file_id = doc.get("file_id")

    if from_id is None or chat_id is None:
        return

    # --- bootstrap: first message from the expected owner claims the bridge ---
    if state.get("allowed_user_id") is None:
        expected = state.get("allowed_username")
        if expected and username and username.lower() != expected.lower():
            log("bootstrap: ignoring message from @{} (expected @{})".format(username, expected))
            return
        state["allowed_user_id"] = from_id
        state["chat_id"] = chat_id
        if username and not state.get("allowed_username"):
            state["allowed_username"] = username
        save_state(state)
        log("BOOTSTRAP: owner=@{} ({}) chat_id={} is_forum={}".format(
            username, from_id, chat_id, chat.get("is_forum")))
        register_commands(chat_id)
        send_message(chat_id, WELCOME.format(username=username or "you"), thread_id=thread_id)
        return

    # --- enforce owner + chat lock ---
    if from_id != state["allowed_user_id"]:
        log("drop: from {} (@{}) not owner".format(from_id, username))
        return
    if state.get("chat_id") is not None and chat_id != state["chat_id"]:
        log("drop: chat {} not the bootstrapped control chat".format(chat_id))
        return

    # --- setup / meta commands ---
    cmd = text.split()[0].lstrip("/").lower() if text else ""
    cmd = cmd.split("@")[0]  # strip /cmd@botname form
    if cmd == "whoami":
        send_message(chat_id,
                     "chat_id: {}\nyour user_id: {}\ntopic (message_thread_id): {}\nis_forum: {}".format(
                         chat_id, from_id, thread_id, chat.get("is_forum")),
                     thread_id=thread_id, reply_to=message_id)
        return
    if cmd == "help":
        send_message(chat_id, HELP, thread_id=thread_id, reply_to=message_id)
        return
    if cmd == "sessions":
        entries = []
        if REGISTRY_DIR.exists():
            for f in sorted(REGISTRY_DIR.glob("*.json")):
                try:
                    reg = json.loads(f.read_text())
                    entries.append("• topic {} — {} ({})".format(
                        reg.get("thread_id"), reg.get("context") or "?", reg.get("cwd") or "?"))
                except (ValueError, OSError):
                    continue
        send_message(chat_id,
                     "Attached sessions:\n" + ("\n".join(entries) if entries else "(none)"),
                     thread_id=thread_id, reply_to=message_id)
        return
    if cmd == "new":
        # /new <dir|alias> [intent note...] — the first whitespace-delimited token
        # after the command is the dir/alias; everything after it is an optional
        # free-text intent note injected into the spawn prompt so the fresh session
        # starts knowing why it was created (issue #11). No dir -> default home, no
        # note. A dir with spaces must be an alias: the note boundary is the first
        # space after the dir token.
        parts = text.split(maxsplit=2)
        target = parts[1].strip() if len(parts) > 1 else "~"
        intent = parts[2].strip() if len(parts) > 2 else ""
        if not SPAWN_SCRIPT.exists():
            send_message(chat_id, "spawn script missing: {}".format(SPAWN_SCRIPT),
                         thread_id=thread_id, reply_to=message_id)
            return
        spawn_args = ["/bin/bash", str(SPAWN_SCRIPT)]
        if intent:
            spawn_args += ["--intent", intent]
        spawn_args.append(target)
        try:
            proc = subprocess.run(
                spawn_args,
                capture_output=True, text=True, timeout=30, env=dict(os.environ))
        except Exception as e:
            send_message(chat_id, "spawn failed: {}".format(e),
                         thread_id=thread_id, reply_to=message_id)
            log("new: spawn {} raised {}".format(target, e))
            return
        if proc.returncode == 0:
            note = "\nIntent: {}".format(intent) if intent else ""
            send_message(chat_id,
                         "Spawning a session in {} ...{}\nIt will open its own topic "
                         "and post there once attached.".format(target, note),
                         thread_id=thread_id, reply_to=message_id)
        else:
            err = (proc.stderr or proc.stdout or "unknown error").strip()
            send_message(chat_id, "spawn failed: {}".format(err),
                         thread_id=thread_id, reply_to=message_id)
        log("new: spawn {} intent={!r} rc={}".format(target, intent, proc.returncode))
        return

    if cmd == "attach":
        # /attach [<n>|<window>] — adopt an EXISTING, unattached Claude session in the
        # shared tmux session into its own topic. With no arg, list candidates. The
        # mechanism: send-keys `/telegram` into the chosen pane so the session attaches
        # ITSELF — it has no TELEGRAM_BRIDGE_SPAWNED, so it registers spawned:false
        # (user-owned), and is therefore never reaped on /end (only detached).
        parts = text.split(maxsplit=1)
        selector = parts[1].strip() if len(parts) > 1 else ""
        cands = list_unattached_claude_panes()
        if not cands:
            send_message(chat_id, "No unattached Claude sessions found in the tmux session.",
                         thread_id=thread_id, reply_to=message_id)
            return
        if not selector:
            lines = ["Unattached Claude sessions — reply /attach <n> to bind one:"]
            for i, (pid, widx, wname) in enumerate(cands, 1):
                lines.append("{}. {} (window {})".format(i, wname, widx))
            send_message(chat_id, "\n".join(lines), thread_id=thread_id, reply_to=message_id)
            return
        target = None
        if selector.isdigit():
            k = int(selector) - 1
            if 0 <= k < len(cands):
                target = cands[k]
        if target is None:
            for c in cands:
                if selector in (c[2], c[1]):   # window name or index
                    target = c
                    break
        if target is None:
            send_message(chat_id, "No match for {!r}. Send /attach with no argument to list."
                         .format(selector), thread_id=thread_id, reply_to=message_id)
            return
        pane_id = target[0]
        _tmux("send-keys", "-t", pane_id, "-l", "/telegram")
        _tmux("send-keys", "-t", pane_id, "Enter")
        send_message(chat_id, "Sent /telegram to {} — it'll open its own topic shortly."
                     .format(target[2]), thread_id=thread_id, reply_to=message_id)
        log("attach: sent /telegram to pane {} ({})".format(pane_id, target[2]))
        return

    # --- route to the session that owns this topic ---
    reg = load_registry(tkey)
    if reg is None:
        if tkey == "general":
            send_message(chat_id,
                         "This is the General topic — no session is attached here. "
                         "Run /telegram in a Claude session to open a dedicated topic, "
                         "or /sessions to see active ones.",
                         thread_id=thread_id, reply_to=message_id)
        else:
            send_message(chat_id,
                         "No Claude session is attached to this topic. The session may have "
                         "ended. Run /telegram in a session to claim a new topic.",
                         thread_id=thread_id, reply_to=message_id)
        return

    inbox = Path(reg["inbox_path"])
    inbox.parent.mkdir(parents=True, exist_ok=True)

    # AskUserQuestion in progress for this topic? A typed reply is the ANSWER, not
    # a new task: route it to the answer file the MCP server polls (instead of the
    # task inbox, which the blocked session can't drain anyway). The MCP maps a
    # bare number to that option, free text to an "Other" answer, and "1 3" to
    # multiple. Only plain text (no image) is treated as an answer; images and the
    # no-pending case fall through to normal routing.
    auq_pending = inbox.parent / "auq-pending.json"
    if text and not image_file_id and auq_pending.exists():
        try:
            (inbox.parent / "auq-answer.json").write_text(json.dumps({
                "text": text, "ts": now_iso()}))
            react_eyes(chat_id, message_id)
            record_msg_thread(message_id, tkey)
            log("routed AUQ typed-reply -> topic {} ({})".format(tkey, reg.get("cwd")))
            return
        except OSError as e:
            log("ERROR auq typed-reply write: {}; falling back to inbox".format(e))

    # A permission round-trip in progress for this topic? A typed reply is the
    # DECISION (not a task): route it to perm-answer.json the hook polls. The
    # owner's free-text rides the trusted inbox; the bare verb rides the answer.
    perm_pending = inbox.parent / "perm-pending.json"
    if text and not image_file_id and perm_pending.exists():
        session_dir = inbox.parent
        try:
            pend = json.loads(perm_pending.read_text())
        except (OSError, ValueError):
            pend = {}
        armed = pend.get("armed") or None
        if armed and armed.get("decision"):
            # Second step of an ✍️ button: this whole line is the note/redirect.
            inject_inbox_note(reg, text, from_id, username)
            write_perm_answer(session_dir, armed["decision"], note=text)
            react_eyes(chat_id, message_id)
            record_msg_thread(message_id, tkey)
            # Nudge a drain so the session reads the note right after the tool
            # resolves (else it waits on the backstop). Best-effort, like a message.
            wake_session(reg)
            log("perm armed-note ({}) -> topic {} ({})".format(
                armed["decision"], tkey, reg.get("cwd")))
            return
        # Bare typed reply: first token is the verb, the rest (if any) is the note.
        bits = text.split(maxsplit=1)
        head = bits[0].strip().lower()
        rest = bits[1].strip() if len(bits) > 1 else ""
        if head in PERM_ALWAYS:
            write_perm_answer(session_dir, "always")
            react_eyes(chat_id, message_id)
            record_msg_thread(message_id, tkey)
            return
        if head in PERM_APPROVE or head in PERM_DENY:
            decision = "allow" if head in PERM_APPROVE else "deny"
            if rest:
                inject_inbox_note(reg, rest, from_id, username)
            write_perm_answer(session_dir, decision, note=rest or None)
            react_eyes(chat_id, message_id)
            record_msg_thread(message_id, tkey)
            if rest:
                wake_session(reg)   # surface the note promptly (see armed branch)
            log("perm typed {} -> topic {} ({})".format(decision, tkey, reg.get("cwd")))
            return
        # Unreadable as a decision: re-prompt, keep waiting (pending stays set).
        send_message(chat_id,
                     "🤔 Reply y / n (optionally \"y also do X\" or \"n do this instead\"), "
                     "or tap a button above.",
                     thread_id=thread_id, reply_to=message_id)
        return

    image_path = None
    if image_file_id:
        image_path = download_file(image_file_id, inbox.parent / "images", str(message_id))

    record = {
        "ts": now_iso(),
        "message_id": message_id,
        "from": from_id,
        "username": username,
        "text": text or caption,   # caption rides with an image
    }
    if image_path:
        record["image_path"] = image_path
    with inbox.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    react_eyes(chat_id, message_id)
    # Remember this message_id's topic so a later reaction on it can be routed.
    record_msg_thread(message_id, tkey)
    log("routed msg {} -> topic {} ({}{})".format(
        message_id, tkey, reg.get("cwd"), " +image" if image_path else ""))
    # Best-effort: nudge a backed-off session to poll now. Delivery is already
    # done above, so this can only ever help, never drop or delay the message.
    wake_session(reg)


def handle_reaction(mr, state):
    """A message_reaction update: the owner added/removed an emoji reaction.

    Telegram doesn't include the topic in the update, so map the reacted
    message_id back to its topic via the msg index, then drop an informational
    `reaction` record into that session's inbox. A reaction is never a
    task/instruction — the session reads it as a signal only.
    """
    chat = mr.get("chat", {}) or {}
    frm = mr.get("user", {}) or {}
    chat_id = chat.get("id")
    from_id = frm.get("id")
    message_id = mr.get("message_id")

    # Owner + chat lock, same as handle_message. Per-user reactions carry `user`;
    # anonymous ones come as message_reaction_count (we don't subscribe) so a
    # missing user means we can't attribute it -> drop. The bot's own 👀
    # auto-reaction is attributed to the bot (not the owner) and is dropped here.
    if from_id is None or chat_id is None:
        return
    if state.get("allowed_user_id") is not None and from_id != state["allowed_user_id"]:
        return
    if state.get("chat_id") is not None and chat_id != state["chat_id"]:
        return

    added = _emoji_set(mr.get("new_reaction")) - _emoji_set(mr.get("old_reaction"))
    removed = _emoji_set(mr.get("old_reaction")) - _emoji_set(mr.get("new_reaction"))
    if not added and not removed:
        return

    tkey = lookup_msg_thread(message_id)
    if tkey is None:
        log("reaction on msg {} but no topic mapping; dropping".format(message_id))
        return
    reg = load_registry(tkey)
    if reg is None or not reg.get("inbox_path"):
        log("reaction on msg {} -> topic {} has no attached session".format(message_id, tkey))
        return

    inbox = Path(reg["inbox_path"])
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a") as fh:
        for emoji in sorted(added):
            fh.write(json.dumps({"ts": now_iso(), "type": "reaction",
                                 "message_id": message_id, "emoji": emoji,
                                 "action": "added", "from": from_id}) + "\n")
        for emoji in sorted(removed):
            fh.write(json.dumps({"ts": now_iso(), "type": "reaction",
                                 "message_id": message_id, "emoji": emoji,
                                 "action": "removed", "from": from_id}) + "\n")
    log("routed reaction (added={} removed={}) on msg {} -> topic {}".format(
        sorted(added), sorted(removed), message_id, tkey))
    # Surface it promptly: nudge the session to drain. Best-effort, like messages.
    wake_session(reg)


def handle_callback(cq, state):
    """An inline button was tapped. Several callback_data namespaces converge here:

      perm:<kind>:<thread>         permission decision the in-session hook polls via
                                   perm-answer.json. kind: y=approve, n=deny,
                                   a=always-allow, ym=approve+note, nm=deny+redirect
                                   (the two +note kinds ARM perm-pending so the next
                                   typed line becomes the note/redirect).
      auq:<thread>:<qidx>:<oidx>   single-select AUQ choice -> auq-answer.json the
                                   MCP server polls.
      auqm:<thread>:<qidx>:<oidx>  multi-select toggle -> flip the option in
                                   auq-pending.json and re-render the keyboard in
                                   place (no answer written yet).
      auqd:<thread>:<qidx>         multi-select Done -> write the selected list to
                                   auq-answer.json.
    """
    cq_id = cq.get("id")
    frm = cq.get("from", {}) or {}
    from_id = frm.get("id")
    data = cq.get("data", "") or ""
    message = cq.get("message", {}) or {}
    chat = message.get("chat", {}) or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    # Owner lock: only the bootstrapped owner may tap.
    if state.get("allowed_user_id") is None or from_id != state.get("allowed_user_id"):
        answer_callback(cq_id, "Not authorized")
        return

    parts = data.split(":")
    kind = parts[0] if parts else ""

    def resolve(tkey):
        reg = load_registry(tkey)
        if reg is None or not reg.get("inbox_path"):
            return None, None
        return reg, Path(reg["inbox_path"]).parent

    # --- permission decision -> perm-answer.json the in-session hook polls ---
    if kind == "perm" and len(parts) == 3:
        pkind, tkey = parts[1], parts[2]
        reg, session_dir = resolve(tkey)
        if reg is None:
            answer_callback(cq_id, "Session is gone")
            clear_buttons(chat_id, message_id)
            return
        if pkind in ("ym", "nm"):
            # Arm: the NEXT typed message becomes the note/redirect; don't decide yet.
            decision = "allow" if pkind == "ym" else "deny"
            pending_file = session_dir / "perm-pending.json"
            try:
                pend = json.loads(pending_file.read_text())
            except (OSError, ValueError):
                pend = {}
            pend["armed"] = {"decision": decision}
            try:
                pending_file.write_text(json.dumps(pend))
            except OSError as e:
                log("ERROR perm arm write: {}".format(e))
                answer_callback(cq_id, "Error")
                return
            answer_callback(cq_id, "Send your note as the next message")
            send_message(chat_id,
                         "✍️ Approve + note: send your note as the next message — "
                         "it runs, then the session reads your note."
                         if decision == "allow" else
                         "✍️ Deny + redirect: send what to do instead as the next message.",
                         thread_id=reg.get("thread_id"))
            clear_buttons(chat_id, message_id)
            log("callback perm:{} (arm) -> topic {} ({})".format(pkind, tkey, reg.get("cwd")))
            return
        decision = {"y": "allow", "a": "always", "n": "deny"}.get(pkind)
        if decision is None:
            answer_callback(cq_id)
            return
        if not write_perm_answer(session_dir, decision):
            answer_callback(cq_id, "Error")
            return
        answer_callback(cq_id, {"allow": "Approved ✅", "always": "Always allowed ✅",
                                "deny": "Denied ⛔"}[decision])
        clear_buttons(chat_id, message_id)
        log("callback perm:{} -> topic {} ({})".format(pkind, tkey, reg.get("cwd")))
        return

    # --- single-select AUQ tap -> answer file ---
    if kind == "auq" and len(parts) == 4:
        tkey, qidx_s, oidx_s = parts[1], parts[2], parts[3]
        try:
            oidx = int(oidx_s)
        except ValueError:
            answer_callback(cq_id)
            return
        reg, session_dir = resolve(tkey)
        if reg is None:
            answer_callback(cq_id, "Session is gone")
            clear_buttons(chat_id, message_id)
            return
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "auq-answer.json").write_text(json.dumps({
                "oidx": oidx, "qidx": int(qidx_s) if qidx_s.isdigit() else None,
                "ts": now_iso()}))
        except OSError as e:
            log("ERROR callback write: {}".format(e))
            answer_callback(cq_id, "Error")
            return
        answer_callback(cq_id, "Selected option {}".format(oidx + 1))
        clear_buttons(chat_id, message_id)
        log("callback opt{} (auq) -> topic {} ({})".format(oidx, tkey, reg.get("cwd")))
        return

    # --- multi-select toggle -> flip in pending, re-render keyboard ---
    if kind == "auqm" and len(parts) == 4:
        tkey, _qidx_s, oidx_s = parts[1], parts[2], parts[3]
        try:
            oidx = int(oidx_s)
        except ValueError:
            answer_callback(cq_id)
            return
        reg, session_dir = resolve(tkey)
        if reg is None:
            answer_callback(cq_id, "Session is gone")
            clear_buttons(chat_id, message_id)
            return
        pending_file = session_dir / "auq-pending.json"
        try:
            pending = json.loads(pending_file.read_text())
        except (OSError, ValueError):
            answer_callback(cq_id, "Question expired")
            clear_buttons(chat_id, message_id)
            return
        opts = pending.get("options") or []
        selected = [s for s in (pending.get("selected") or []) if isinstance(s, int)]
        name = opts[oidx] if 0 <= oidx < len(opts) else "option {}".format(oidx + 1)
        if oidx in selected:
            selected = [s for s in selected if s != oidx]
            toast = "✗ {}".format(name)
        else:
            selected.append(oidx)
            toast = "✓ {}".format(name)
        pending["selected"] = selected
        try:
            pending_file.write_text(json.dumps(pending))
        except OSError:
            pass
        set_buttons(chat_id, message_id, auq_multi_markup(tkey, pending))
        answer_callback(cq_id, toast)
        return

    # --- multi-select Done -> write the selected list ---
    if kind == "auqd" and len(parts) == 3:
        tkey = parts[1]
        reg, session_dir = resolve(tkey)
        if reg is None:
            answer_callback(cq_id, "Session is gone")
            clear_buttons(chat_id, message_id)
            return
        pending_file = session_dir / "auq-pending.json"
        try:
            pending = json.loads(pending_file.read_text())
        except (OSError, ValueError):
            answer_callback(cq_id, "Question expired")
            clear_buttons(chat_id, message_id)
            return
        selected = [s for s in (pending.get("selected") or []) if isinstance(s, int)]
        try:
            (session_dir / "auq-answer.json").write_text(json.dumps({
                "selected": selected, "qidx": pending.get("qidx"),
                "ts": now_iso()}))
        except OSError as e:
            log("ERROR callback write: {}".format(e))
            answer_callback(cq_id, "Error")
            return
        answer_callback(cq_id, "Recorded {} pick(s)".format(len(selected)))
        clear_buttons(chat_id, message_id)
        log("callback done (auqd {} sel) -> topic {} ({})".format(
            len(selected), tkey, reg.get("cwd")))
        return

    answer_callback(cq_id)


# --- main loop -------------------------------------------------------------

def _term(_signum, _frame):
    global _running
    _running = False
    log("received SIGTERM/SIGINT, shutting down")


def main():
    if not TOKEN:
        log("FATAL: TELEGRAM_BRIDGE_BOT_TOKEN not set")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    start_caffeinate()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    STICKY_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_ROOT.mkdir(parents=True, exist_ok=True)

    state = load_state()
    log("telegram-router up. state_dir={} owner=@{} chat_id={} offset={}".format(
        STATE_DIR, state.get("allowed_username"), state.get("chat_id"), state.get("offset")))

    # Refresh the tappable "/" command menu on every startup (idempotent).
    register_commands(state.get("chat_id"))

    # Smart-router context thread: computes each attached session's context gauge
    # and drives compaction, OFF the getUpdates path. daemon=True so it dies with
    # the process; the main loop owns shutdown via _running.
    threading.Thread(target=context_loop, name="context", daemon=True).start()

    net_failures = 0
    while _running:
        try:
            resp = api_call("getUpdates", {
                "offset": state["offset"],
                "timeout": LONG_POLL_SECONDS,
                "allowed_updates": json.dumps(
                    ["message", "callback_query", "message_reaction"]),
            })
            net_failures = 0
        except (urllib.error.URLError, OSError, ValueError) as e:
            net_failures += 1
            log("getUpdates failed (#{}) : {}".format(net_failures, e))
            if net_failures >= MAX_NET_FAILURES:
                log("{} consecutive failures; exiting for a clean launchd restart"
                    .format(net_failures))
                sys.exit(1)
            time.sleep(min(30, 2 * net_failures))
            continue

        if not resp.get("ok"):
            log("getUpdates not ok: {}".format(resp))
            time.sleep(5)
            continue

        updates = resp.get("result", [])
        for upd in updates:
            state["offset"] = upd["update_id"] + 1
            msg = upd.get("message")
            cq = upd.get("callback_query")
            mr = upd.get("message_reaction")
            try:
                if msg:
                    handle_message(msg, state)
                elif cq:
                    handle_callback(cq, state)
                elif mr:
                    handle_reaction(mr, state)
            except Exception as e:  # never let one bad update kill the daemon
                log("ERROR handling update {}: {}".format(upd.get("update_id"), e))
        if updates:
            save_state(state)

        # Refresh per-topic status stickies once per loop (≈ every long-poll
        # window). Never let a sticky error break the router's update pump.
        try:
            update_stickies(state)
        except Exception as e:
            log("ERROR update_stickies: {}".format(e))

    log("telegram-router stopped")


if __name__ == "__main__":
    main()
