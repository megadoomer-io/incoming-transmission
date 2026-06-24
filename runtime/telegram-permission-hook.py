#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-permission-hook: Tier-2 permission routing for SPAWNED bridge
# sessions. A PreToolUse hook that asks for approval over Telegram instead of
# the local terminal prompt (which would hang forever in a detached pane).
#
# SAFETY: this hook runs on EVERY PreToolUse in EVERY Claude session. For any
# session that is NOT a spawned bridge session (no TELEGRAM_BRIDGE_SPAWNED=1),
# and for any tool we don't gate, it abstains INSTANTLY (no output, exit 0) so
# normal sessions are completely unaffected. Any error also abstains.
#
# Gated tools: Write, Edit, NotebookEdit, WebFetch, and any MCP tool
# (mcp__*) that is NOT already covered by the user's settings allowlist.
# Bash(*) and Read/Glob/Grep are allowlisted with GIR as the floor, so they
# would not prompt and we leave them alone. Allowlisted MCP tools (e.g.
# mcp__plugin_github_github__*) auto-allow too, so we abstain on those and only
# route the non-allowlisted MCP tools — which would otherwise prompt and hang a
# detached pane forever — to Telegram for y/n.
#
# Round-trip reuses existing bridge state (no new files):
#   - registry/<thread>.json : find this session's topic by matching cwd
#   - state.json             : chat_id
#   - inbox.jsonl            : the owner's y/n reply lands here (routed by daemon)
#   - read.offset            : advanced past the consumed reply so the session
#                              cron doesn't reprocess it as a task
#
# The session is BUSY (blocked in this hook) while we wait, so its idle poll
# cron will not fire and contend for the inbox.
#
# Stdlib only; resolves on /usr/bin/python3 via PATH.

import fnmatch
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GATED_TOOLS = {"Write", "Edit", "NotebookEdit", "WebFetch"}
WAIT_TIMEOUT_S = 240          # self-resolve (deny) before the settings.json timeout
POLL_INTERVAL_S = 2

STATE_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    Path.home() / ".local" / "state" / "telegram-bridge"))
REGISTRY_DIR = STATE_DIR / "registry"
STATE_FILE = STATE_DIR / "state.json"

# Spawned-session permission posture, read live (no restart) from the bridge
# config dir. Default "auto-allow": unattended sessions never block on a human
# round-trip -- gated tools auto-run and questions are denied so the model
# decides itself. Set to "ask" to fall back to the Telegram approval round-trip
# below (attended/cautious mode). A future "sophisticated" posture would be a
# new value handled here.
BRIDGE_CONF_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_CONF_DIR", Path.home() / ".telegram-bridge"))
PERMISSIONS_FILE = BRIDGE_CONF_DIR / "permissions.json"


def spawned_mode():
    try:
        return str(json.loads(PERMISSIONS_FILE.read_text())
                   .get("spawned_mode", "auto-allow")).strip().lower()
    except Exception:
        return "auto-allow"

# Claude Code merges allow rules from these in increasing precedence; for the
# spawned-session gate we only need the union of mcp__* patterns. Project-level
# .claude/settings*.json are intentionally NOT read: a spawned session must not
# let a checked-in repo allowlist silently widen what runs unattended.
SETTINGS_FILES = (
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
)

APPROVE = {"y", "yes", "ok", "okay", "approve", "approved", "allow", "go", "yep", "sure"}
DENY = {"n", "no", "deny", "denied", "reject", "stop", "nope"}


def abstain():
    """Emit nothing, exit 0. Normal flow / prompt proceeds as usual."""
    sys.exit(0)


def decide(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,            # "allow" | "deny"
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def send(token, chat_id, thread_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if thread_id not in (None, "general", ""):
        payload["message_thread_id"] = int(thread_id)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    req = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        pass  # best-effort; a failed send must not crash the hook


def _claude_ancestor_pid():
    """PID of the `claude` process that owns this hook, by walking up the tree.
    Disambiguates multiple bridge sessions sharing one cwd. macOS `ps -o comm=`
    truncates to 16 chars (hides 'claude' in a long path), so use `ps -c`."""
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-c", "-o", "comm=,ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return None
        if not out:
            return None
        toks = out.rsplit(None, 1)
        comm = toks[0] if toks else ""
        if "claude" in comm.lower():
            return pid
        try:
            pid = int(toks[1]) if len(toks) > 1 else -1
        except ValueError:
            return None
    return None


def find_topic(cwd):
    """Return (thread_id, inbox_path) for THIS session's topic.

    cwd alone is ambiguous when multiple bridge sessions share a directory, so
    prefer the registry entry whose `claude_pid` matches our owning claude
    process; fall back to the most-recently-registered cwd match for legacy
    entries that predate claude_pid recording.
    """
    if not REGISTRY_DIR.is_dir():
        return None
    # Canonicalize before comparing: the registry stores cwd as the spawn saw it
    # (e.g. /tmp) while the hook payload / getcwd may report the symlink-resolved
    # path (e.g. /private/tmp on macOS). realpath both sides so they agree.
    # realpath("") == getcwd(), so skip any registry entry with a missing cwd.
    cwd = os.path.realpath(cwd)
    cpid = _claude_ancestor_pid()
    matches = []   # (registered_at, thread_id, inbox_path)
    for f in REGISTRY_DIR.glob("*.json"):
        try:
            reg = json.loads(f.read_text())
        except Exception:
            continue
        rc = reg.get("cwd")
        if not rc or os.path.realpath(rc) != cwd or reg.get("thread_id") is None:
            continue
        entry = (str(reg.get("registered_at", "")), str(reg["thread_id"]), reg.get("inbox_path"))
        if cpid is not None and reg.get("claude_pid") == cpid:
            return entry[1], entry[2]      # exact session-identity match
        matches.append(entry)
    if not matches:
        return None
    matches.sort(reverse=True)             # newest registered_at wins (fallback)
    return matches[0][1], matches[0][2]


def mcp_allow_patterns():
    """Union of `mcp__*` entries from the user's settings allow lists.

    Returns a list of fnmatch patterns. Best-effort: a missing or malformed
    settings file contributes nothing. We deliberately read only the user-level
    files so a repo's checked-in allowlist can't widen an unattended session.
    """
    patterns = []
    for sf in SETTINGS_FILES:
        try:
            allow = json.loads(sf.read_text()).get("permissions", {}).get("allow", [])
        except Exception:
            continue
        for rule in allow:
            if isinstance(rule, str) and rule.startswith("mcp__"):
                patterns.append(rule)
    return patterns


def mcp_allowlisted(tool_name, patterns):
    """True if an allowlisted mcp pattern covers tool_name.

    Mirrors Claude Code's matching closely enough for gating: an exact name
    matches itself, and a `*` is a wildcard (fnmatch). A bare `mcp__server__*`
    therefore covers every tool on that server. When uncertain we return False
    so the tool is routed to Telegram rather than silently auto-run.
    """
    for p in patterns:
        if tool_name == p or fnmatch.fnmatch(tool_name, p):
            return True
    return False


def _clip(text, limit=500):
    """Trim text to a phone-readable preview with a head/tail if long."""
    text = str(text)
    if len(text) <= limit:
        return text
    head = text[: limit - 120]
    tail = text[-100:]
    return "{}\n... [{} chars omitted] ...\n{}".format(head, len(text) - limit + 20, tail)


def summarize(tool_name, tool_input):
    fp = tool_input.get("file_path") or tool_input.get("notebook_path") or "?"
    if tool_name == "Write":
        content = str(tool_input.get("content", ""))
        return "Write {} ({} bytes):\n\n{}".format(fp, len(content), _clip(content))
    if tool_name == "Edit":
        old = str(tool_input.get("old_string", ""))
        new = str(tool_input.get("new_string", ""))
        return "Edit {}:\n\n- OLD:\n{}\n\n+ NEW:\n{}".format(fp, _clip(old), _clip(new))
    if tool_name == "NotebookEdit":
        src = str(tool_input.get("new_source", ""))
        return "NotebookEdit {} (cell {}):\n\n{}".format(
            fp, tool_input.get("cell_id", "?"), _clip(src))
    if tool_name == "WebFetch":
        return "WebFetch {}\nprompt: {}".format(
            tool_input.get("url", "?"), _clip(tool_input.get("prompt", ""), 200))
    if tool_name.startswith("mcp__"):
        try:
            args = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
        except Exception:
            args = str(tool_input)
        return "MCP {}\nargs: {}".format(tool_name, _clip(args, 400))
    return "{} {}".format(tool_name, fp)


def main():
    # Gate FIRST, before anything else. Non-spawned sessions exit instantly.
    if os.environ.get("TELEGRAM_BRIDGE_SPAWNED") != "1":
        abstain()
    token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN")
    if not token:
        abstain()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        abstain()

    tool_name = str(payload.get("tool_name", ""))
    is_mcp = tool_name.startswith("mcp__")
    if tool_name not in GATED_TOOLS and not is_mcp:
        abstain()

    # Keep an unattended (spawned) session flowing: it must NEVER block on a
    # human round-trip, because the idle drain that would deliver the answer
    # can't run while the session is blocked (self-deadlock). In "auto-allow"
    # mode (default) we resolve every gated tool synchronously here:
    #   - AskUserQuestion-style MCP tools -> DENY, so the model decides itself
    #     instead of stalling up to ~28m on the AUQ server's own timeout.
    #   - everything else -> ALLOW, so writes/edits/MCP just run. This also
    #     closes the old failure where an abstain fell through to Claude's
    #     native permission prompt and hung the detached pane forever.
    # "ask" mode falls through to the Telegram approval round-trip below.
    if spawned_mode() == "auto-allow":
        if is_mcp and tool_name.split("__")[-1] == "AskUserQuestion":
            decide("deny", "Spawned bridge session is unattended: AskUserQuestion "
                           "can't be answered without wedging the session. Decide "
                           "autonomously and state your assumption.")
        decide("allow", "Spawned bridge session: auto-approved (auto-allow mode).")

    # Allowlisted MCP tools should run without a Telegram round-trip. We can't
    # rely on Claude Code's own permission flow to auto-allow them: in a spawned
    # session an allowlisted MCP tool (e.g. mcp__telegram__AskUserQuestion, which
    # is exactly how decision B asks questions) still hit the native permission
    # prompt, which hangs a detached pane forever. So when the tool matches the
    # user's mcp__* allowlist, explicitly ALLOW it here instead of abstaining.
    # This only fires in spawned sessions (gated above), and only for tools the
    # user already allowlisted, so it can't widen what runs unattended.
    if is_mcp and mcp_allowlisted(tool_name, mcp_allow_patterns()):
        decide("allow", "mcp tool allowlisted in user settings")

    tool_input = payload.get("tool_input", {}) or {}
    cwd = str(payload.get("cwd", os.getcwd()))

    topic = find_topic(cwd)
    if topic is None:
        # Can't locate this session's topic; better to let normal flow handle it
        # than to wrongly deny. (Rare: /telegram registers cwd on attach.)
        abstain()
    thread_id, inbox_path = topic

    try:
        chat_id = json.loads(STATE_FILE.read_text()).get("chat_id")
    except Exception:
        abstain()
    if chat_id is None or not inbox_path:
        abstain()

    inbox = Path(inbox_path)
    offset_file = inbox.parent / "read.offset"

    def line_count():
        try:
            with inbox.open() as fh:
                return sum(1 for _ in fh)
        except FileNotFoundError:
            return 0

    baseline = line_count()
    # Inline-keyboard buttons for a one-tap decision on the phone. callback_data
    # is "tg:<y|n>:<thread_id>" (well under Telegram's 64-byte cap); the router
    # handles the callback_query by injecting a synthetic y/n line into THIS
    # inbox, which the poll loop below consumes. Typed "y"/"n" still works as a
    # fallback (the router routes it here the same way), so both paths converge.
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": "tg:y:{}".format(thread_id)},
        {"text": "⛔ Deny", "callback_data": "tg:n:{}".format(thread_id)},
    ]]}
    send(token, chat_id, thread_id,
         "\U0001F510 Approval needed:\n{}\n\nTap a button below, or reply y/n "
         "(auto-deny in {}s).".format(summarize(tool_name, tool_input), WAIT_TIMEOUT_S),
         reply_markup=keyboard)

    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        if line_count() <= baseline:
            continue
        # First new line after baseline is the owner's reply.
        try:
            with inbox.open() as fh:
                lines = fh.readlines()
        except Exception:
            continue
        reply_line = lines[baseline]
        try:
            answer = str(json.loads(reply_line).get("text", "")).strip().lower()
        except Exception:
            answer = ""
        # Consume through the reply so the session cron won't reprocess it.
        try:
            offset_file.write_text(str(baseline + 1))
        except Exception:
            pass
        if answer in APPROVE:
            send(token, chat_id, thread_id, "✅ Approved, running.")
            decide("allow", "approved by owner via Telegram")
        else:
            send(token, chat_id, thread_id, "⛔ Denied.")
            decide("deny", "denied by owner via Telegram (reply: {!r})".format(answer))

    send(token, chat_id, thread_id, "⏱ Approval timed out, denied.")
    decide("deny", "approval request timed out after {}s".format(WAIT_TIMEOUT_S))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Absolute backstop: never block a tool call due to a hook bug.
        abstain()
