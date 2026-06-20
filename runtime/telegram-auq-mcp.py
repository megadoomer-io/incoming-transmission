#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.13"
# dependencies = ["mcp>=1.2.0"]
# ///
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-auq-mcp: an MCP server that exposes an AskUserQuestion tool which
# routes the question to the bridged Telegram topic as tappable inline buttons,
# blocks until the owner taps (or types) an answer, and returns the choice.
#
# WHY an MCP server (not a PreToolUse hook): in Claude Code 2.1.170 PreToolUse
# hooks no longer fire for the native AskUserQuestion tool, so a hook can't gate
# it. MCP tools always execute, so this server reliably runs. Spawned bridge
# sessions launch with `--disallowedTools AskUserQuestion` (native disabled) plus
# `--mcp-config` adding this server, so the model uses mcp__telegram__AskUserQuestion
# instead — and the question shows up on the phone with buttons.
#
# Round-trip:
#   - registry/<thread>.json : find this session's topic by matching cwd
#   - state.json             : chat_id
#   - sendMessage(reply_markup=inline_keyboard) : post the options as buttons
#   - the router converts a tap (callback_data "auq:<thread>:<qidx>:<oidx>") into
#     a response file  <session_dir>/auq-answer.json , which we poll for here.
#
# No-auto-pick: on timeout we return a structured "no answer" result (never a
# fabricated choice) so the model can re-ask or stop.
#
# cwd is inherited from the spawned `claude` (= the session's cwd), which is how
# we resolve the topic. Token comes from TELEGRAM_BRIDGE_BOT_TOKEN (spawn-injected).

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

WAIT_TIMEOUT_S = 1700
POLL_INTERVAL_S = 2

STATE_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    Path.home() / ".local" / "state" / "telegram-bridge"))
REGISTRY_DIR = STATE_DIR / "registry"
STATE_FILE = STATE_DIR / "state.json"

mcp = FastMCP("telegram")


def _claude_ancestor_pid():
    """PID of the `claude` process that owns this MCP server, by walking up the
    process tree. Lets us disambiguate multiple bridge sessions sharing one cwd.
    Returns None if it can't be determined (macOS/Linux `ps`)."""
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(
                ["ps", "-c", "-o", "comm=,ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return None
        if not out:
            return None
        toks = out.rsplit(None, 1)          # comm may contain spaces/paths
        comm = toks[0] if toks else ""
        if "claude" in comm.lower():
            return pid
        try:
            pid = int(toks[1]) if len(toks) > 1 else -1
        except ValueError:
            return None
    return None


def _find_topic(cwd: str):
    """Return (thread_id, session_dir) for THIS session's topic.

    Multiple bridge sessions can share a cwd, so cwd alone is ambiguous. Prefer
    the registry entry whose `claude_pid` matches our owning claude process;
    fall back to the most-recently-registered cwd match for legacy entries that
    predate claude_pid recording.
    """
    if not REGISTRY_DIR.is_dir():
        return None
    cpid = _claude_ancestor_pid()
    matches = []   # (registered_at, thread_id, session_dir)
    for f in REGISTRY_DIR.glob("*.json"):
        try:
            reg = json.loads(f.read_text())
        except Exception:
            continue
        if reg.get("cwd") != cwd or reg.get("thread_id") is None:
            continue
        inbox = reg.get("inbox_path") or ""
        session_dir = str(Path(inbox).parent) if inbox else ""
        entry = (str(reg.get("registered_at", "")), str(reg["thread_id"]), session_dir)
        if cpid is not None and reg.get("claude_pid") == cpid:
            return entry[1], entry[2]      # exact session-identity match
        matches.append(entry)
    if not matches:
        return None
    matches.sort(reverse=True)             # newest registered_at wins (fallback)
    return matches[0][1], matches[0][2]


def _chat_id():
    try:
        return json.loads(STATE_FILE.read_text()).get("chat_id")
    except Exception:
        return None


def _send(token, chat_id, thread_id, text, reply_markup=None):
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
        pass


def _clip(text, limit=160):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _render(q, idx, total):
    header = str(q.get("header", "")).strip()
    question = str(q.get("question", "")).strip()
    tag = "Q{}/{}".format(idx + 1, total) if total > 1 else "Question"
    lines = ["❓ {} — {}".format(tag, header) if header else "❓ {}".format(tag),
             question, ""]
    for i, opt in enumerate(q.get("options", []) or []):
        label = str(opt.get("label", "option {}".format(i)))
        desc = str(opt.get("description", "")).strip()
        lines.append("{}) {} — {}".format(i + 1, label, _clip(desc)) if desc
                     else "{}) {}".format(i + 1, label))
    lines.append("")
    if q.get("multiSelect"):
        lines.append("(multi-select: this records one pick)")
    lines.append("Tap a button, or reply with the number / your own text. "
                 "I'll wait for your answer (no auto-pick).")
    return "\n".join(lines)


def _keyboard(q, thread_id, qidx):
    rows = []
    for i, opt in enumerate(q.get("options", []) or []):
        label = str(opt.get("label", "option {}".format(i)))
        rows.append([{"text": "{}. {}".format(i + 1, _clip(label, 50)),
                      "callback_data": "auq:{}:{}:{}".format(thread_id, qidx, i)}])
    return {"inline_keyboard": rows} if rows else None


@mcp.tool()
def AskUserQuestion(questions: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask the user one or more multiple-choice questions over Telegram (buttons).

    Use this exactly like the native AskUserQuestion tool. Each entry in
    `questions` is an object:
      - question (str):   the question text
      - header (str):     a short label/topic for the question
      - multiSelect (bool): whether multiple options may be chosen (this MVP
                            records a single pick)
      - options (list):   [{label: str, description: str}, ...]

    Returns {"answers": {<question text>: <chosen option label or typed text>}}.
    On no answer within the wait window, returns {"answers": {}, "timed_out": true}
    so you can re-ask or proceed without fabricating a choice.
    """
    token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN")
    cwd = os.getcwd()
    topic = _find_topic(cwd)
    chat_id = _chat_id()
    if not token or topic is None or chat_id is None:
        # Can't reach Telegram for this session — surface, don't hang.
        return {"answers": {}, "error": "telegram bridge not available for this session"}
    thread_id, session_dir = topic
    answer_file = Path(session_dir) / "auq-answer.json"

    answers: dict[str, str] = {}
    total = len(questions)
    for qidx, q in enumerate(questions):
        options = q.get("options", []) or []
        try:
            answer_file.unlink()           # clear any stale answer
        except FileNotFoundError:
            pass
        _send(token, chat_id, thread_id, _render(q, qidx, total),
              reply_markup=_keyboard(q, thread_id, qidx))

        chosen = None
        deadline = time.monotonic() + WAIT_TIMEOUT_S
        while time.monotonic() < deadline and chosen is None:
            time.sleep(POLL_INTERVAL_S)
            if not answer_file.exists():
                continue
            try:
                rec = json.loads(answer_file.read_text())
            except Exception:
                rec = {}
            try:
                answer_file.unlink()
            except FileNotFoundError:
                pass
            oidx = rec.get("oidx")
            text = str(rec.get("text", "")).strip()
            if isinstance(oidx, int) and 0 <= oidx < len(options):
                chosen = str(options[oidx].get("label", "option {}".format(oidx)))
            elif text:
                chosen = text              # free-text "Other" escape
        if chosen is None:
            _send(token, chat_id, thread_id,
                  "⏱ No answer in {}m — not picking for you.".format(
                      round(WAIT_TIMEOUT_S / 60)))
            return {"answers": answers, "timed_out": True}
        _send(token, chat_id, thread_id, "✓ Recorded: {}".format(chosen))
        answers[str(q.get("question", "Q{}".format(qidx + 1)))] = chosen

    return {"answers": answers}


if __name__ == "__main__":
    mcp.run()
