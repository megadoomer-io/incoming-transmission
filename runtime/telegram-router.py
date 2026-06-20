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
INBOX_ROOT = Path("/tmp/claude-telegram/sessions")
BRIDGE_DIR = Path.home() / ".telegram-bridge"
SPAWN_SCRIPT = BRIDGE_DIR / "telegram-spawn.sh"
# Tunables for the status sticky + auto-compaction, read live each loop so the
# user can edit thresholds without restarting the daemon. Defaults match the
# shipped compaction.json.
CONFIG_FILE = BRIDGE_DIR / "compaction.json"
CONFIG_DEFAULTS = {"trigger_pct": 0.85, "warn_pct": 0.75, "kill_old": False}

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


# --- status sticky (pinned per-topic context gauge) ------------------------
#
# Each attached topic shows a phone-can't-see-the-statusbar gauge as a pinned
# message: "cwd · N msgs · ~XX% ctx · updated HH:MM", with ⚠️ past the warn
# threshold. The numbers come from each topic's status.json (written by the
# session's poll cron). We own a per-thread sidecar in STICKY_DIR so we never
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
#                                                         session's poll cron acts)
# context/compact have NO handler below, so they fall through to the routing path
# like status/dir — the session executes them. Names must be 1-32 chars, lowercase
# a-z/0-9/_. Order = menu order.
BRIDGE_COMMANDS = [
    ("new", "Spawn a new Claude session (dir or alias optional)"),
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
    "/new [dir] — spawn a NEW Claude session in dir (default: home) and "
    "auto-attach it to its own topic\n"
    "/whoami  — show this chat id, your user id, and the current topic id\n"
    "/sessions — list Claude sessions attached to topics\n"
    "/help — this message\n\n"
    "Inside a session's topic: /status, /context, /compact, /dir <alias|path>, "
    "/dirs, /end. Anything else (including /track, /ship, etc.) goes to that "
    "session as a task.\n\n"
    "To attach an EXISTING session: run /telegram inside it. It creates a topic "
    "here; type in that topic to talk to that session."
)


# --- wake (Phase 2): nudge a backed-off session's tmux pane to poll NOW --------
# Best-effort and ADDITIVE: wake runs only AFTER a message is already in the
# inbox, so any failure here never affects delivery (backoff still bounds the
# fallback latency). Pane matching is STRICT — a pane is only nudged when its
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


def wake_session(reg):
    """Best-effort: nudge the topic's pane to run a full poll tick NOW so a
    backed-off cron doesn't sit on the just-delivered message. Never raises."""
    try:
        cpid = reg.get("claude_pid")
        thread_id = reg.get("thread_id")
        if not cpid or thread_id is None:
            return
        pane = _pane_for_claude_pid(int(cpid))
        if not pane:
            log("wake: no matching pane for topic {} (claude_pid {})".format(thread_id, cpid))
            return
        # One line (no embedded newline so Enter submits exactly once). Delegates
        # to the session's existing, tested A/B/C/D poll procedure: that drains
        # the message, runs the compaction-safety check (so a wake-driven burst
        # can't blow past the trigger while the cron sleeps), and resets backoff.
        nudge = ("TELEGRAM WAKE {tid}: a message arrived and your poll cron may be backed off. "
                 "Run ONE full bridge poll tick NOW (the A/B/C/D procedure from your poll cron "
                 "prompt: status gauge, auto-compaction check, drain inbox + reply, backoff "
                 "reschedule). It drains the new message immediately and resets backoff to fast."
                 ).format(tid=thread_id)
        _tmux("send-keys", "-t", pane, "-l", nudge)
        _tmux("send-keys", "-t", pane, "Enter")
        log("wake: nudged topic {} (pane {})".format(thread_id, pane))
    except Exception as e:
        log("wake: error (ignored): {}".format(e))


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
        parts = text.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else "~"
        if not SPAWN_SCRIPT.exists():
            send_message(chat_id, "spawn script missing: {}".format(SPAWN_SCRIPT),
                         thread_id=thread_id, reply_to=message_id)
            return
        try:
            proc = subprocess.run(
                ["/bin/bash", str(SPAWN_SCRIPT), target],
                capture_output=True, text=True, timeout=30, env=dict(os.environ))
        except Exception as e:
            send_message(chat_id, "spawn failed: {}".format(e),
                         thread_id=thread_id, reply_to=message_id)
            log("new: spawn {} raised {}".format(target, e))
            return
        if proc.returncode == 0:
            send_message(chat_id,
                         "Spawning a session in {} ...\nIt will open its own topic "
                         "and post there once attached.".format(target),
                         thread_id=thread_id, reply_to=message_id)
        else:
            err = (proc.stderr or proc.stdout or "unknown error").strip()
            send_message(chat_id, "spawn failed: {}".format(err),
                         thread_id=thread_id, reply_to=message_id)
        log("new: spawn {} rc={}".format(target, proc.returncode))
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
    log("routed msg {} -> topic {} ({}{})".format(
        message_id, tkey, reg.get("cwd"), " +image" if image_path else ""))
    # Best-effort: nudge a backed-off session to poll now. Delivery is already
    # done above, so this can only ever help, never drop or delay the message.
    wake_session(reg)


def handle_callback(cq, state):
    """An inline approval button was tapped (Tier-2 permission prompt).

    callback_data is "tg:<y|n>:<thread_id>". We inject a synthetic y/n line into
    that topic's inbox so the blocking permission hook (which polls the inbox)
    consumes it exactly like a typed reply, then ack the tap and strip the
    keyboard so it can't be tapped twice.
    """
    cq_id = cq.get("id")
    frm = cq.get("from", {}) or {}
    from_id = frm.get("id")
    data = cq.get("data", "") or ""
    message = cq.get("message", {}) or {}
    chat = message.get("chat", {}) or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    # Owner lock: only the bootstrapped owner may approve/deny.
    if state.get("allowed_user_id") is None or from_id != state.get("allowed_user_id"):
        answer_callback(cq_id, "Not authorized")
        return

    # Two button namespaces converge here, each delivered to a blocking poller:
    #   tg:<y|n>:<thread>          -> permission Approve/Deny: synthetic inbox line
    #                                 the permission hook (in-session) polls.
    #   auq:<thread>:<qidx>:<oidx> -> AskUserQuestion choice: a response file
    #                                 <session_dir>/auq-answer.json the MCP server polls.
    parts = data.split(":")
    kind = parts[0] if parts else ""
    if kind == "tg" and len(parts) == 3:
        decision, tkey = parts[1], parts[2]
        answer, toast = ("y", "Approved ✅") if decision == "y" else ("n", "Denied ⛔")
        logval = answer
    elif kind == "auq" and len(parts) == 4:
        tkey, qidx_s, oidx_s = parts[1], parts[2], parts[3]
        try:
            oidx = int(oidx_s)
        except ValueError:
            answer_callback(cq_id)
            return
        toast = "Selected option {}".format(oidx + 1)
        logval = "opt{}".format(oidx)
    else:
        answer_callback(cq_id)
        return

    reg = load_registry(tkey)
    if reg is None or not reg.get("inbox_path"):
        answer_callback(cq_id, "Session is gone")
        clear_buttons(chat_id, message_id)
        return

    session_dir = Path(reg["inbox_path"]).parent
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        if kind == "auq":
            (session_dir / "auq-answer.json").write_text(json.dumps({
                "oidx": oidx,
                "qidx": int(qidx_s) if qidx_s.isdigit() else None,
                "ts": now_iso(),
            }))
        else:
            with Path(reg["inbox_path"]).open("a") as fh:
                fh.write(json.dumps({
                    "ts": now_iso(),
                    "message_id": message_id,
                    "from": from_id,
                    "username": frm.get("username"),
                    "text": answer,
                    "via": "callback",
                }) + "\n")
    except OSError as e:
        log("ERROR callback write: {}".format(e))
        answer_callback(cq_id, "Error")
        return

    answer_callback(cq_id, toast)
    clear_buttons(chat_id, message_id)
    log("callback {} ({}) -> topic {} ({})".format(logval, kind, tkey, reg.get("cwd")))


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

    net_failures = 0
    while _running:
        try:
            resp = api_call("getUpdates", {
                "offset": state["offset"],
                "timeout": LONG_POLL_SECONDS,
                "allowed_updates": json.dumps(["message", "callback_query"]),
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
            try:
                if msg:
                    handle_message(msg, state)
                elif cq:
                    handle_callback(cq, state)
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
