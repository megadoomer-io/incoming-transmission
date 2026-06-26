#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-permission-hook: Tier-2 permission routing for BRIDGE sessions -- ANY
# session attached to a Telegram topic, a /new spawn or a /telegram-attached
# session alike. The gate is bridge-MEMBERSHIP, not launch path: the experience is
# identical however the session started. A PreToolUse hook that asks the owner for
# approval over Telegram instead of the local terminal prompt.
#
# WHY THIS WORKS NOW (and didn't before): spawns used to launch with
# --dangerously-skip-permissions, under which this hook's decisions never fired,
# so "auto-allow" was the de-facto behavior by accident. Spawns now launch with
# --permission-mode dontAsk, under which:
#   - a tool the hook ABSTAINS on is decided by the engine: an allow-listed tool
#     (Read/Grep, safe Bash via Bash(*), allow-listed MCP) runs; anything else is
#     auto-DENIED (never a hanging prompt).
#   - a tool the hook returns "allow" on runs (verified: allow suppresses the
#     prompt in CC >= 2.1.178).
#   - a tool the hook returns "deny" on is blocked -- and a hook deny overrides
#     even a broad allow rule like Bash(*) (verified), which is how we gate risky
#     Bash even though the owner allow-lists Bash globally.
#
# SAFETY: this hook runs on EVERY PreToolUse in EVERY Claude session. It acts ONLY
# in a BRIDGE session -- one attached to a Telegram topic (detected via the topic
# registry, keyed by cwd). For any non-bridge session, and for any tool we don't
# gate, it abstains INSTANTLY (no output, exit 0) so normal sessions are completely
# unaffected. Any error also abstains. The gate is bridge-MEMBERSHIP, not how the
# session was started: a /new spawn and a /telegram-attached session gate
# identically -- the experience must not depend on the launch path.
#
# spawned_mode (read live from ~/.telegram-bridge/permissions.json, no restart):
#   "risk-tiered" (default) -- gate the dangerous set, ask the owner over Telegram,
#                              auto-allow everything else (the floor).
#   "ask"                   -- gate the SAME set, same round-trip (kept as an alias
#                              of risk-tiered for now; reserved for a future
#                              "ask about more").
#   "auto-allow"            -- never bother the owner: gated tools auto-run,
#                              AskUserQuestion-style MCP is denied so the model
#                              decides itself. (The old fully-autonomous posture.)
#
# Gated set (the "dangerous" tier):
#   - Write, Edit, NotebookEdit                 (file mutations; never allow-listed)
#   - Bash matching a RISKY pattern             (rm -rf, kubectl delete, force-push,
#                                                drop table, ...); safe Bash abstains
#                                                so the Bash(*) floor runs it
#   - any mcp__* tool NOT covered by the user's settings allowlist
# Everything else abstains (the floor). WebFetch is intentionally NOT gated here:
# allow-listed domains run via the engine, others are auto-denied by dontAsk.
#
# RICH ANSWERS over a dedicated side channel (NOT the task inbox), mirroring the
# AUQ MCP's pending/answer files:
#   session_dir/perm-pending.json  : written here when we start waiting; tells the
#                                    router to intercept a typed reply as the
#                                    decision, and holds the "armed" sub-state for
#                                    the two +note buttons.
#   session_dir/perm-answer.json   : written by the router (button tap or typed
#                                    reply); we poll it here.
# The owner's free-text note/redirect is delivered to the MODEL via the trusted
# INBOX (the router injects it) -- NOT via this hook's additionalContext, because a
# spawned model (correctly) treats hook-injected text as untrusted. We add only a
# short additionalContext POINTER ("owner left you a message -- read your inbox").
#
# "Always allow" persists a NARROW native allow-rule (never a wildcard) into a
# bridge-scoped settings-shaped file (~/.telegram-bridge/spawned-allow.json). It is
# auditable and editable as plain JSON. This hook READS it live and enforces it
# itself (auto-allowing matching calls) -- we deliberately do NOT load it via
# `claude --settings`, because that flag REPLACES the user's settings and drops the
# PreToolUse hooks (disabling this very gate). So enforcement is hook-side.
#
# Stdlib only; resolves on /usr/bin/python3 via PATH.

import fnmatch
import json
import os
import shlex
import sys
import time
import urllib.request
from pathlib import Path

import bridge_resolve  # shared pane-keyed resolver (pane option -> spawn env)

GATED_FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
# How long to wait for the owner to answer before self-resolving (deny). Generous
# on purpose: the owner may be away from their phone (driving, in a meeting). Must
# stay BELOW the hook's `timeout` in settings.json (1800s) so the hook resolves
# gracefully (sends a "timed out" message + explicit deny) before CC kills it.
WAIT_TIMEOUT_S = 1700
POLL_INTERVAL_S = 2

STATE_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    Path.home() / ".local" / "state" / "telegram-bridge"))
REGISTRY_DIR = STATE_DIR / "registry"
STATE_FILE = STATE_DIR / "state.json"

BRIDGE_CONF_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_CONF_DIR", Path.home() / ".telegram-bridge"))
PERMISSIONS_FILE = BRIDGE_CONF_DIR / "permissions.json"
# Bridge-scoped file holding persisted "always allow" rules in permissions.allow.
# It is settings-SHAPED (so it stays readable/auditable and could be hand-edited),
# but this hook reads it LIVE and enforces it itself. It is NOT passed to
# `claude --settings` (that flag replaces user settings and drops the hooks).
PERSISTED_ALLOW_FILE = Path(os.environ.get(
    "TELEGRAM_BRIDGE_SPAWN_SETTINGS", BRIDGE_CONF_DIR / "spawned-allow.json"))

# Bash commands we route to the owner. Safe Bash (anything not matching) abstains
# so the user's Bash(*) allow runs it unattended. Matched against the FIRST
# pipeline/&&/;-separated segment's program+subcommand, lower-cased. Deliberately
# conservative: catch the genuinely destructive/irreversible, not everyday work.
RISKY_BASH_SUBSTRINGS = (
    "rm -rf", "rm -fr", "rm -r", "rmdir",
    "git push --force", "git push -f", "git push --force-with-lease",
    "git reset --hard", "git clean -",
    "kubectl delete", "kubectl drain", "kubectl cordon", "kubectl apply",
    "kubectl replace", "kubectl patch", "kubectl scale",
    "helm uninstall", "helm delete", "helm upgrade",
    "terraform apply", "terraform destroy",
    "drop table", "drop database", "truncate ",
    "mkfs", "dd if=", "shutdown", "reboot", "killall",
    "chmod -r", "chown -r",
    "> /dev/", ":(){", "curl ", "wget ",  # exfil/pull-and-run surface
)

APPROVE = {"y", "yes", "ok", "okay", "approve", "approved", "allow", "go", "yep", "sure"}
DENY = {"n", "no", "deny", "denied", "reject", "stop", "nope"}


def abstain():
    """Emit nothing, exit 0. The engine's normal flow (allow rules / dontAsk) decides."""
    sys.exit(0)


def decide(decision, reason, additional_context=None):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,            # "allow" | "deny"
            "permissionDecisionReason": reason,
        }
    }
    if additional_context:
        out["hookSpecificOutput"]["additionalContext"] = additional_context
    print(json.dumps(out))
    sys.exit(0)


def spawned_mode():
    try:
        return str(json.loads(PERMISSIONS_FILE.read_text())
                   .get("spawned_mode", "risk-tiered")).strip().lower()
    except Exception:
        return "risk-tiered"


# Claude Code merges allow rules from these in increasing precedence; for the
# spawned-session gate we only need the union of mcp__* patterns. Project-level
# .claude/settings*.json are intentionally NOT read: a spawned session must not
# let a checked-in repo allowlist silently widen what runs unattended.
SETTINGS_FILES = (
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
)


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


def mcp_allow_patterns():
    """Union of `mcp__*` entries from the user's settings allow lists.

    Best-effort: a missing or malformed settings file contributes nothing. We
    deliberately read only the user-level files so a repo's checked-in allowlist
    can't widen an unattended session.
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
    """True if an allowlisted mcp pattern covers tool_name (exact or fnmatch '*')."""
    for p in patterns:
        if tool_name == p or fnmatch.fnmatch(tool_name, p):
            return True
    return False


# --- persisted "always allow" rules (the bridge's own, auditable floor) -------

def read_persisted_rules():
    """The permissions.allow list from the bridge-scoped settings file. These are
    NARROW rules this hook wrote on a prior 'always allow'. Read live so a rule
    taught earlier in THIS session suppresses re-asking immediately (the engine
    also enforces them from --settings, but only as of the spawn's launch)."""
    try:
        data = json.loads(PERSISTED_ALLOW_FILE.read_text())
        rules = data.get("permissions", {}).get("allow", [])
        return [r for r in rules if isinstance(r, str)]
    except Exception:
        return []


def _bash_skeleton(command):
    """A conservative command skeleton for matching/persisting: program plus one
    subcommand for the common multi-verb CLIs, else just the program. Returns ""
    when we can't parse a sane skeleton (caller then refuses to persist)."""
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    toks = [t for t in toks if t and not t.startswith("-")]
    if not toks:
        return ""
    prog = os.path.basename(toks[0])
    # A real program name starts with a letter. Reject numeric/junk first tokens
    # (e.g. a stray "1680") so a mis-parsed command never persists a bogus
    # "always allow" rule -- the caller degrades to allow-once instead.
    if not prog[:1].isalpha():
        return ""
    multi = {"git", "kubectl", "helm", "docker", "uv", "npm", "pnpm", "yarn",
             "cargo", "go", "terraform", "gh", "brew", "systemctl", "make"}
    if prog in multi and len(toks) > 1 and toks[1][:1].isalpha():
        return "{} {}".format(prog, toks[1])
    return prog


def signature_rule(tool, tool_input):
    """The narrow native allow-rule string an 'always allow' would persist, or
    None if we can't form one narrow enough (then 'always allow' degrades to
    allow-once). NEVER returns a wildcard like Bash(*) or a bare tool name."""
    if tool in GATED_FILE_TOOLS:
        fp = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return "{}({})".format(tool, fp) if fp else None
    if tool == "Bash":
        skel = _bash_skeleton(str(tool_input.get("command", "")))
        return "Bash({}:*)".format(skel) if skel else None
    if tool.startswith("mcp__"):
        return tool        # exact MCP tool name; never a server wildcard
    return None


def matches_persisted(tool, tool_input, rules):
    """True if this exact call is covered by a persisted narrow rule we wrote.
    We only need to match OUR rule forms (Write(path)/Edit(path)/NotebookEdit(path),
    Bash(skel:*), exact mcp__...). Anything else in the list we ignore here (the
    engine still enforces it)."""
    rule = signature_rule(tool, tool_input)
    if rule and rule in rules:
        return True
    # Bash needs prefix matching for the trailing :* form.
    if tool == "Bash":
        skel = _bash_skeleton(str(tool_input.get("command", "")))
        if skel and "Bash({}:*)".format(skel) in rules:
            return True
    return False


def persist_always_allow(tool, tool_input):
    """Append the narrow signature rule to the bridge-scoped settings file
    (creating it as a valid CC settings doc). Idempotent; returns the rule string
    persisted, or None if it couldn't form a narrow rule."""
    rule = signature_rule(tool, tool_input)
    if not rule:
        return None
    try:
        try:
            data = json.loads(PERSISTED_ALLOW_FILE.read_text())
        except Exception:
            data = {}
        perms = data.setdefault("permissions", {})
        allow = perms.setdefault("allow", [])
        if rule not in allow:
            allow.append(rule)
        PERSISTED_ALLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PERSISTED_ALLOW_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(PERSISTED_ALLOW_FILE)
        return rule
    except OSError:
        return None


# --- risk classification ------------------------------------------------------

def is_risky_bash(command):
    c = " ".join(str(command).lower().split())
    return any(sub in c for sub in RISKY_BASH_SUBSTRINGS)


def is_gated(tool_name, tool_input):
    """Should this call be routed to the owner (in risk-tiered/ask mode)?"""
    if tool_name in GATED_FILE_TOOLS:
        return True
    if tool_name == "Bash":
        return is_risky_bash(tool_input.get("command", ""))
    if tool_name.startswith("mcp__"):
        return not mcp_allowlisted(tool_name, mcp_allow_patterns())
    return False


def _clip(text, limit=500):
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
    if tool_name == "Bash":
        return "Bash:\n  {}".format(_clip(tool_input.get("command", "?"), 400))
    if tool_name.startswith("mcp__"):
        try:
            args = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
        except Exception:
            args = str(tool_input)
        return "MCP {}\nargs: {}".format(tool_name, _clip(args, 400))
    return "{} {}".format(tool_name, fp)


def perm_keyboard(thread_id):
    """Five-button decision keyboard. callback_data is perm:<kind>:<thread>:
    y=approve, n=deny, a=always-allow, ym=approve+note (arm), nm=deny+redirect (arm).
    The router translates a tap into perm-answer.json (or arms perm-pending for the
    two +note kinds)."""
    return {"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": "perm:y:{}".format(thread_id)},
         {"text": "⛔ Deny", "callback_data": "perm:n:{}".format(thread_id)}],
        [{"text": "✅ Always allow", "callback_data": "perm:a:{}".format(thread_id)}],
        [{"text": "✍️ Approve + note", "callback_data": "perm:ym:{}".format(thread_id)},
         {"text": "✍️ Deny + redirect", "callback_data": "perm:nm:{}".format(thread_id)}],
    ]}


def main():
    token = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN")
    if not token:
        abstain()

    try:
        payload = json.load(sys.stdin)
    except Exception:
        abstain()

    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {}) or {}
    is_mcp = tool_name.startswith("mcp__")

    # THE GATE: this permission model applies to EVERY bridge session -- one
    # attached to a Telegram topic -- and to nothing else, regardless of HOW the
    # session started (/new spawn OR /attach adopt). The experience must not depend
    # on the launch path. We resolve this session's topic via the shared resolver:
    # the pane option (authoritative) or the spawn env. A non-bridge session (no
    # pane option) resolves to None and we abstain instantly, leaving its normal
    # permission flow untouched.
    thread_id = bridge_resolve.resolve()
    if thread_id is None:
        abstain()
    # Load this thread's registry entry for the inbox / session dir (where the
    # perm-pending / perm-answer side-channel files live).
    try:
        _reg = json.loads((REGISTRY_DIR / "{}.json".format(thread_id)).read_text())
        _inbox = _reg.get("inbox_path") or ""
    except (OSError, ValueError):
        abstain()
    if not _inbox:
        abstain()
    session_dir = Path(_inbox).parent

    mode = spawned_mode()

    # --- auto-allow posture: never bother the owner (fully-autonomous) ---
    if mode == "auto-allow":
        if is_mcp and tool_name.split("__")[-1] == "AskUserQuestion":
            decide("deny", "Unattended bridge session: AskUserQuestion can't be "
                           "answered without wedging the session. Decide "
                           "autonomously and state your assumption.")
        if tool_name in GATED_FILE_TOOLS or is_mcp or tool_name == "Bash":
            decide("allow", "Bridge session: auto-approved (auto-allow mode).")
        abstain()

    # --- risk-tiered / ask: gate the dangerous set, floor everything else ---
    if not is_gated(tool_name, tool_input):
        abstain()

    # Already taught "always allow" for this exact call? Don't re-ask.
    if matches_persisted(tool_name, tool_input, read_persisted_rules()):
        decide("allow", "covered by a persisted bridge allow-rule (always-allow)")

    try:
        chat_id = json.loads(STATE_FILE.read_text()).get("chat_id")
    except Exception:
        abstain()
    if chat_id is None:
        abstain()

    pending_file = session_dir / "perm-pending.json"
    answer_file = session_dir / "perm-answer.json"
    try:
        answer_file.unlink()           # clear any stale answer
    except FileNotFoundError:
        pass
    # Tell the router a permission answer is awaited for this topic, so it routes a
    # typed reply (and the +note arming) into perm-answer.json. Removed below.
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        pending_file.write_text(json.dumps({
            "thread_id": thread_id, "tool": tool_name,
            "summary": summarize(tool_name, tool_input)[:160], "armed": None}))
    except OSError:
        abstain()

    send(token, chat_id, thread_id,
         "\U0001F510 Approval needed:\n{}\n\nTap a button, or reply (e.g. \"y also run "
         "tests\" / \"n use --dry-run first\"). Auto-deny in {}s.".format(
             summarize(tool_name, tool_input), WAIT_TIMEOUT_S),
         reply_markup=perm_keyboard(thread_id))

    def finish(decision, reason, note):
        try:
            pending_file.unlink()
        except FileNotFoundError:
            pass
        ctx = None
        if note:
            if decision == "deny":
                ctx = ("The owner denied this and sent a redirect instruction to your "
                       "chat inbox. Read your inbox and follow it before your next step.")
            else:
                ctx = ("The owner approved this and left a follow-up note in your chat "
                       "inbox. Read your inbox for it.")
        decide(decision, reason, ctx)

    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
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
        decision = str(rec.get("decision", "")).strip().lower()
        note = rec.get("note") or ""
        if decision in ("allow", "approve", "y", "yes"):
            send(token, chat_id, thread_id, "✅ Approved, running.")
            finish("allow", "approved by owner via Telegram", note)
        if decision in ("always", "always-allow", "a"):
            rule = persist_always_allow(tool_name, tool_input)
            send(token, chat_id, thread_id,
                 "✅ Always allowing {}.".format(rule) if rule
                 else "✅ Approved (couldn't persist a narrow rule; allowed once).")
            finish("allow", "always-allowed by owner via Telegram (rule: {})".format(rule), note)
        if decision in ("deny", "n", "no", "reject"):
            send(token, chat_id, thread_id, "⛔ Denied.")
            finish("deny", "denied by owner via Telegram", note)
        # Unrecognized record: keep waiting until timeout.

    try:
        pending_file.unlink()
    except FileNotFoundError:
        pass
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
