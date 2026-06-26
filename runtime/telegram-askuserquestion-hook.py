#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-askuserquestion-hook: route AskUserQuestion over Telegram for SPAWNED
# bridge sessions. A PreToolUse hook on AskUserQuestion that, instead of letting
# the tool render its interactive options in a TUI nobody is sitting at (which
# would hang a detached pane forever), posts each question to the session's topic
# as inline-keyboard buttons, blocks until the owner taps one (or types an
# answer), then ALLOWS the tool with the answers substituted in so the UI never
# renders and the model receives the choices as the tool result.
#
# MECHANISM (the supported way to gate AskUserQuestion — confirmed via the
# Claude Code hook docs): a PreToolUse hook returns permissionDecision "allow"
# together with `updatedInput` that echoes the original `questions` AND adds an
# `answers` object mapping each question's text to the chosen option label. That
# pre-answers the question; the interactive UI does not render. (Returning
# "deny" only BLOCKS the tool — it cannot substitute an answer — so we use it
# only for the no-answer / timeout case, where we deliberately do NOT fabricate
# a choice.)
#
# This generalizes telegram-permission-hook.py's Approve/Deny round-trip. Same
# machinery:
#   - bridge_resolve.resolve : find this session's topic (pane option -> spawn
#     env), then registry/<thread>.json for the inbox path
#   - state.json             : chat_id
#   - inbox.jsonl            : the owner's tap/reply lands here (routed by daemon)
#   - read.offset            : advanced past consumed replies so the session poll
#                              cron doesn't reprocess them as tasks
# The router writes a synthetic line on a button tap: callback_data
# "auq:<thread>:<qidx>:<oidx>" -> {"text": "<oidx>", "via": "callback-auq"}.
#
# SAFETY: gated on TELEGRAM_BRIDGE_SPAWNED=1 and tool_name == AskUserQuestion.
# Any non-spawned session, any other tool, or any error abstains INSTANTLY
# (no output, exit 0) so normal sessions are completely unaffected.
#
# Stdlib only; resolves on /usr/bin/python3 via PATH.

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import bridge_resolve  # shared pane-keyed resolver (pane option -> spawn env)

WAIT_TIMEOUT_S = 600          # per-question wait for a phone tap; never auto-picks
POLL_INTERVAL_S = 2

STATE_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    Path.home() / ".local" / "state" / "telegram-bridge"))
REGISTRY_DIR = STATE_DIR / "registry"
STATE_FILE = STATE_DIR / "state.json"

# Lightweight diagnostic trail (helps confirm the hook fired and where it exited).
DEBUG_LOG = Path("/tmp/claude-telegram/auq-hook.log")


def _dbg(msg):
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as fh:
            fh.write("{} pid={} {}\n".format(
                time.strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def abstain(why=""):
    """Emit nothing, exit 0. Normal flow (TUI prompt) proceeds as usual."""
    if why:
        _dbg("abstain: " + why)
    sys.exit(0)


def allow_with_answers(questions, answers_map):
    """Suppress the UI and return the chosen answers as the tool result.

    The supported AskUserQuestion gate: allow + updatedInput echoing the
    original questions plus an `answers` map (question text -> chosen label).
    """
    _dbg("ALLOW with answers: {}".format(answers_map))
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"questions": questions, "answers": answers_map},
        }
    }))
    sys.exit(0)


def deny(reason):
    """Block the tool without answering (used only for the no-answer case)."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
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


def find_topic():
    """Return (thread_id, inbox_path) for THIS session's topic, or None.

    Resolves via the shared pane-keyed resolver (the `@telegram_thread_id` pane
    option, or the spawn env in the spawn-race window), then loads the registry
    entry by thread_id for its inbox path. None means "not a bridge session" and
    the caller abstains.
    """
    thread_id = bridge_resolve.resolve()
    if thread_id is None:
        return None
    try:
        reg = json.loads((REGISTRY_DIR / "{}.json".format(thread_id)).read_text())
    except (OSError, ValueError):
        return None
    return thread_id, reg.get("inbox_path")


def _clip(text, limit=300):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_question(q, idx, total):
    """Build the message text for one question."""
    header = str(q.get("header", "")).strip()
    question = str(q.get("question", "")).strip()
    multi = q.get("multiSelect")
    lines = []
    tag = "Q{}/{}".format(idx + 1, total) if total > 1 else "Question"
    lines.append("❓ {} — {}".format(tag, header) if header else "❓ {}".format(tag))
    lines.append(question)
    lines.append("")
    options = q.get("options", []) or []
    for i, opt in enumerate(options):
        label = str(opt.get("label", "option {}".format(i)))
        desc = str(opt.get("description", "")).strip()
        if desc:
            lines.append("{}) {} — {}".format(i + 1, label, _clip(desc, 160)))
        else:
            lines.append("{}) {}".format(i + 1, label))
    lines.append("")
    if multi:
        lines.append("(multi-select: this MVP records one pick)")
    lines.append("Tap a button, or reply with the number / your own text. "
                 "I'll wait for your answer (no auto-pick).")
    return "\n".join(lines)


def build_keyboard(q, thread_id, qidx):
    """One button per option, vertical (long labels read better stacked)."""
    options = q.get("options", []) or []
    rows = []
    for i, opt in enumerate(options):
        label = str(opt.get("label", "option {}".format(i)))
        rows.append([{
            "text": "{}. {}".format(i + 1, _clip(label, 50)),
            "callback_data": "auq:{}:{}:{}".format(thread_id, qidx, i),
        }])
    return {"inline_keyboard": rows} if rows else None


def main():
    _dbg("fired: SPAWNED={} tool_name={!r}".format(
        os.environ.get("TELEGRAM_BRIDGE_SPAWNED"),
        None))  # tool_name filled in after payload parse
    if os.environ.get("TELEGRAM_BRIDGE_SPAWNED") != "1":
        abstain("not spawned")
    token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN")
    if not token:
        abstain("no token")

    try:
        payload = json.load(sys.stdin)
    except Exception:
        abstain("bad stdin payload")

    tname = str(payload.get("tool_name", ""))
    _dbg("tool_name={!r}".format(tname))
    if tname != "AskUserQuestion":
        abstain("wrong tool: " + tname)

    tool_input = payload.get("tool_input", {}) or {}
    questions = tool_input.get("questions") or []
    if not questions:
        abstain("no questions")

    cwd = str(payload.get("cwd", os.getcwd()))   # kept for the debug trail below
    topic = find_topic()
    if topic is None:
        abstain("not a bridge session (cwd=%s)" % cwd)   # let normal flow handle it
    thread_id, inbox_path = topic
    _dbg("topic={} inbox={} cwd={}".format(thread_id, inbox_path, cwd))

    try:
        chat_id = json.loads(STATE_FILE.read_text()).get("chat_id")
    except Exception:
        abstain("no chat_id")
    if chat_id is None or not inbox_path:
        abstain("chat_id/inbox missing")
    _dbg("posting {} question(s) to topic {}".format(len(questions), thread_id))

    inbox = Path(inbox_path)
    offset_file = inbox.parent / "read.offset"

    def line_count():
        try:
            with inbox.open() as fh:
                return sum(1 for _ in fh)
        except FileNotFoundError:
            return 0

    answers_map = {}      # question text -> chosen label / typed value
    total = len(questions)

    for qidx, q in enumerate(questions):
        options = q.get("options", []) or []
        baseline = line_count()
        send(token, chat_id, thread_id,
             render_question(q, qidx, total),
             reply_markup=build_keyboard(q, thread_id, qidx))

        chosen = None
        deadline = time.monotonic() + WAIT_TIMEOUT_S
        while time.monotonic() < deadline and chosen is None:
            time.sleep(POLL_INTERVAL_S)
            if line_count() <= baseline:
                continue
            try:
                with inbox.open() as fh:
                    lines = fh.readlines()
            except Exception:
                continue
            try:
                rec = json.loads(lines[baseline])
            except Exception:
                rec = {}
            answer_text = str(rec.get("text", "")).strip()
            via = str(rec.get("via", ""))
            # Consume through this reply so the session cron won't reprocess it.
            try:
                offset_file.write_text(str(baseline + 1))
            except Exception:
                pass
            # A tap delivers the 0-based option index as text; a typed number is
            # 1-based (matches the displayed list); anything else is free text.
            idx = None
            if answer_text.isdigit():
                n = int(answer_text)
                idx = n if via == "callback-auq" else n - 1
            if idx is not None and 0 <= idx < len(options):
                chosen = str(options[idx].get("label", "option {}".format(idx)))
            elif answer_text:
                chosen = answer_text          # free-text "Other" escape
            # else: empty reply, keep waiting

        if chosen is None:
            # No answer: do NOT fabricate. Block the tool and tell the model.
            send(token, chat_id, thread_id,
                 "⏱ No answer in {}m — not picking for you.".format(
                     round(WAIT_TIMEOUT_S / 60)))
            deny("The user did not answer this AskUserQuestion within {}s. Do NOT "
                 "assume or pick an answer on their behalf. If you still need it, "
                 "call AskUserQuestion again to re-post the question; otherwise "
                 "stop and wait for the user.".format(WAIT_TIMEOUT_S))
        send(token, chat_id, thread_id, "✓ Recorded: {}".format(chosen))
        answers_map[str(q.get("question", "Q{}".format(qidx + 1)))] = chosen

    # All questions answered: suppress the UI and return the choices.
    allow_with_answers(questions, answers_map)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Absolute backstop: never block a tool call due to a hook bug.
        abstain()
