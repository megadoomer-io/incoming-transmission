#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-spawn: launch a NEW Claude Code session in a detached tmux pane,
# auto-attached to its own Telegram topic via the /telegram skill.
#
# Invoked by the router daemon's /new handler, which runs under launchd with a
# minimal env. So this resolves absolute paths and injects exactly the env the
# spawned claude (and its /telegram skill + Tier-2 permission hook) need.
#
# We launch claude DIRECTLY as the tmux pane command, NOT through an interactive
# login shell. A login shell here would block on the ssh-add passphrase prompt
# during profile load (nobody is at the pane to type it) and emit asdf path
# noise. claude is a standalone cask binary, so it needs no profile.
#
# Usage: telegram-spawn.sh [--attach <thread> --restore <file>] [--intent <note>] <target_dir>
#   plain <dir>            -> open a fresh topic (normal /new)
#   --intent <note>        -> free-text operator intent (from `/new <dir> <note>`),
#                             injected into the spawn prompt as a clearly-delimited
#                             block so the fresh session knows why it was created,
#                             and exported as TELEGRAM_BRIDGE_INTENT for hooks.
#   --attach/--restore     -> compaction handoff: reattach to an existing topic.
#                             Attach mode is flag-only (never read from the env)
#                             so it can't be inherited and hijack a live topic.
set -euo pipefail

TMUX_BIN="${TELEGRAM_BRIDGE_TMUX:-/opt/homebrew/bin/tmux}"
CLAUDE_BIN="${TELEGRAM_BRIDGE_CLAUDE:-/opt/homebrew/bin/claude}"
ALIAS_FILE="${TELEGRAM_BRIDGE_DIR_ALIASES:-$HOME/.telegram-bridge/dir-aliases.json}"
AUQ_MCP_CONFIG="${TELEGRAM_BRIDGE_AUQ_MCP_CONFIG:-$HOME/.telegram-bridge/telegram-auq-mcp.json}"
# Persisted "always allow" rules live in ~/.telegram-bridge/spawned-allow.json and
# are read LIVE by the Tier-2 permission hook itself — deliberately NOT passed via
# --settings. Empirically, `claude --settings <file>` REPLACES the user's settings
# (it does not merge the hooks key), so it silently drops settings.local.json's
# PreToolUse hook — i.e. it disables the very gate we depend on. So the hook owns
# enforcement of those rules; the file stays auditable native-settings JSON.

# Attach (compaction-replacement) mode is requested EXPLICITLY via flags, never
# inherited from the environment. A session spawned for compaction exports
# TELEGRAM_BRIDGE_ATTACH_THREAD, so a plain `telegram-spawn.sh <dir>` run from
# inside such a session would otherwise inherit it and hijack that session's
# topic instead of opening a fresh one. Clear any inherited values up front and
# take attach intent only from --attach/--restore.
unset TELEGRAM_BRIDGE_ATTACH_THREAD TELEGRAM_BRIDGE_RESTORE_FILE
ATTACH_THREAD=""
RESTORE_FILE=""
INTENT=""
raw_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --attach)  ATTACH_THREAD="${2:?--attach needs a thread id}"; shift 2 ;;
        --restore) RESTORE_FILE="${2:?--restore needs a file path}"; shift 2 ;;
        --intent)  INTENT="${2?--intent needs a note}"; shift 2 ;;
        --)        shift; raw_dir="${1:-}"; break ;;
        *)         raw_dir="$1"; shift ;;
    esac
done
[ -n "$raw_dir" ] || { echo "usage: telegram-spawn.sh [--attach <thread> --restore <file>] <dir>" >&2; exit 1; }

# Resolve a short alias (e.g. "kdrift", "argo") to a full path via dir-aliases.json
# (case-insensitive). Falls through to literal-path handling on no match.
alias_hit="$(/usr/bin/python3 - "$ALIAS_FILE" "$raw_dir" <<'PY' 2>/dev/null
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
want = sys.argv[2].strip().lower()
for k, v in m.items():
    if k.strip().lower() == want:
        print(v); break
PY
)"

if [ -n "$alias_hit" ]; then
    dir="$alias_hit"
else
    # The "~/" below is a literal case pattern to match, not a path to expand;
    # expansion is done explicitly via $HOME. SC2088 is a false positive here.
    # shellcheck disable=SC2088
    case "$raw_dir" in
        "~")    dir="$HOME" ;;
        "~/"*)  dir="$HOME/${raw_dir#\~/}" ;;
        *)      dir="$raw_dir" ;;
    esac
fi

[ -d "$dir" ]          || { echo "telegram-spawn: no such dir: $dir" >&2; exit 1; }
[ -x "$TMUX_BIN" ]     || { echo "telegram-spawn: tmux not executable at $TMUX_BIN" >&2; exit 1; }
[ -x "$CLAUDE_BIN" ]   || { echo "telegram-spawn: claude not executable at $CLAUDE_BIN" >&2; exit 1; }
[ -n "${TELEGRAM_BRIDGE_BOT_TOKEN:-}" ] || { echo "telegram-spawn: TELEGRAM_BRIDGE_BOT_TOKEN not set" >&2; exit 1; }

# --- programmatic bind: resolve the topic BEFORE launching claude ------------
# Binding moves off the LLM (it used to live in SKILL.md Steps 2-3 + the pane
# stamp). Knowing the thread id here lets us (a) inject it as the race-free
# TELEGRAM_BRIDGE_THREAD_ID spawn binding, (b) stamp the pane option, and
# (c) write the registry — all without the model. A compaction handoff REUSES the
# existing topic; a fresh /new creates one now.
STATE_DIR="${TELEGRAM_BRIDGE_STATE_DIR:-$HOME/.local/state/telegram-bridge}"
REGISTRY_DIR="$STATE_DIR/registry"
# chat_id is router-written in state.json; needed to create (and, on failure, close)
# the forum topic.
CHAT_ID="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["chat_id"])' "$STATE_DIR/state.json" 2>/dev/null || true)"

if [ -n "$ATTACH_THREAD" ]; then
    THREAD_ID="$ATTACH_THREAD"          # compaction rollover: reuse the topic
else
    [ -n "$CHAT_ID" ] || { echo "telegram-spawn: no chat_id in $STATE_DIR/state.json; cannot create topic" >&2; exit 1; }
    branch="$(git -C "$dir" branch --show-current 2>/dev/null || true)"
    [ -n "$branch" ] || branch="nogit"
    topic_name="$(basename "$dir")@${branch}"
    # createForumTopic with a small retry — a silent failure here would otherwise
    # leave a topicless session, so on total failure we abort the spawn instead.
    THREAD_ID="$(/usr/bin/python3 - "$CHAT_ID" "$topic_name" <<'PY'
import json, os, sys, time, urllib.request
chat, name = sys.argv[1], sys.argv[2]
tok = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN", "")
data = json.dumps({"chat_id": int(chat), "name": name}).encode()
for _ in range(3):
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/createForumTopic" % tok,
            data=data, headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=30))
        if r.get("ok"):
            print(r["result"]["message_thread_id"]); break
    except Exception:
        pass
    time.sleep(1.5)
PY
)"
    [ -n "$THREAD_ID" ] || { echo "telegram-spawn: createForumTopic failed; not spawning" >&2; exit 1; }
fi

# Every spawn lands as a WINDOW (a tab) in one shared "claude" tmux session, so
# `tmux attach -t claude` shows them all as tabs — cycle with prefix + Tab. The
# window is named "tg-<repo>" so the tab is recognizable AND visibly marked as
# bridge-managed (distinguishing it from any manually-opened claude window in the
# same session). The "tg-" prefix is added OUTSIDE the tr so it survives the
# charset filter. NOTE: the prefix MUST NOT contain ":" — tmux 3.7+ rejects a ":"
# in a window name ("invalid window name"), which silently broke every spawn. The
# code only ever targets windows by index or $TMUX_PANE, never by name.
SHARED="claude"
win="tg-$(basename "$dir" | tr -cd 'a-zA-Z0-9._-')"

# The new session self-attaches: /telegram creates its own topic (named from the
# cwd/branch) and loads the bridge procedure (no cron — the router drives timing).
# TELEGRAM_BRIDGE_SPAWNED=1 marks this as a /new spawn. NOTE: it is NOT what
# activates the Tier-2 permission hook -- that hook gates EVERY bridge session by
# topic-membership (so /new spawns and /telegram-attached sessions behave
# identically). The var is kept only as a launch-path marker for other consumers.
#
# Compaction-replacement mode: when --attach (and usually --restore) were passed,
# this spawn is a handoff replacement for a session whose context filled. It must
# restore the saved context first and then attach to the EXISTING topic (not
# create a new one). The values come from the flags parsed above, NOT the
# environment, so the mode can't be inherited by accident. The /telegram skill in
# the child still keys off TELEGRAM_BRIDGE_ATTACH_THREAD, which we inject into the
# child env below from these flag values.
# TRANSPORT-NECESSARY mechanism note (NOT an opinion on how to behave): this
# session has no human at THIS terminal — the owner is on Telegram. Native
# AskUserQuestion would render a blocking picker into a pane nobody is watching,
# so it is disabled for spawns (via --disallowedTools below); the optional Telegram
# AUQ MCP is the supported way to ask the owner. Behavior, style, and context
# restore are layered in via the lifecycle hooks (lifecycle/*.txt), never here.
spawn_mechanism="Transport note: there is no human at THIS terminal — you reach the owner through Telegram. Native AskUserQuestion is disabled here (it would render a blocking picker into a pane nobody is watching); if the Telegram AskUserQuestion MCP is configured, use it to ask the owner, otherwise proceed and state your assumptions."

# Operator intent (issue #11): the owner's free-text "why I started this" note,
# passed via --intent from `/new <dir> <note>`. Injected as a clearly-delimited
# block kept distinct from transport mechanics — it's the OWNER's words, not a
# behavioral opinion baked in by the bridge. Empty note -> no block, classic
# behavior. Only meaningful for a fresh spawn (a compaction handoff restores its
# own context), so it's woven into the else branch below.
operator_intent=""
if [ -n "$INTENT" ]; then
    operator_intent="Operator intent (the owner's words at spawn time — what this session is for):
${INTENT}

"
fi

if [ -n "$ATTACH_THREAD" ]; then
    prompt="You are a compaction replacement for a Telegram-bridged session whose context filled up. Do these IN ORDER, then wait for instructions:
1. Restore prior working context. The rollover handoff file is at ${RESTORE_FILE}. Run your START hook now: follow the instructions in ~/.telegram-bridge/lifecycle/start.txt (ignore #-comment lines).
2. Invoke the /telegram skill. This session is ALREADY bound to its topic (the spawn created/reused the topic, wrote the registry, and stamped the pane), so the skill detects that, writes the handoff-ready marker, waits for the compaction lock to clear, then loads the bridge procedure and does an initial drain (it runs no cron — the router drives timing).
3. Continue the restored work.

${spawn_mechanism}"
else
    if [ -n "$INTENT" ]; then
        step3="Echo the operator intent below in your attach message so the owner sees their note reflected back, then begin working toward it (ask via the Telegram AskUserQuestion MCP if it needs clarifying)."
    else
        step3="Wait for instructions."
    fi
    prompt="You were spawned by the telegram bridge. Do these IN ORDER, then wait for instructions:
1. Restore any prior context for this project. Run your START hook: follow the instructions in ~/.telegram-bridge/lifecycle/start.txt (ignore #-comment lines). This is a fresh spawn with no rollover handoff file — if your hook looks for one, that's fine, just carry on.
2. Invoke the /telegram skill. This session is ALREADY bound to its topic (the spawn created the topic, wrote the registry, and stamped the pane); the skill detects that and loads the bridge procedure.
3. ${step3}

${operator_intent}${spawn_mechanism}"
fi

# Pre-trust the target dir so the spawned claude doesn't hang on the "Do you
# trust the files in this folder?" dialog (nobody is at the detached pane to
# answer). This sets ONLY the per-folder trust flag in ~/.claude.json.
# NOTE: spawned sessions launch with --permission-mode dontAsk (below): a tool the
# Tier-2 permission hook doesn't explicitly allow is auto-DENIED rather than
# prompting, so an unattended session can never hang on a native permission prompt
# (verified in CC 2.1.178: a hook "allow" suppresses the prompt and runs the tool,
# a hook "deny" blocks it and overrides even a broad allow rule like Bash(*)).
# Native AskUserQuestion is disabled for spawns (--disallowedTools below) because a
# blocking picker has no one to answer it in a detached pane; the Telegram AUQ MCP
# is the supported channel for asking the owner. Best-effort: a failure here just
# means the dialog may appear (it won't break the spawn). Atomic via tmp+rename.
/usr/bin/python3 - "$dir" <<'PY' 2>/dev/null || true
import json, os, sys, tempfile, pathlib
cfg = pathlib.Path.home() / ".claude.json"
target = sys.argv[1]
try:
    data = json.loads(cfg.read_text())
except Exception:
    data = {}
projects = data.setdefault("projects", {})
entry = projects.setdefault(target, {})
entry["hasTrustDialogAccepted"] = True
entry["hasCompletedProjectOnboarding"] = True
# Write to a temp file in the same dir, then atomically replace.
fd, tmp = tempfile.mkstemp(dir=str(cfg.parent), prefix=".claude.json.")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, cfg)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
PY

# Capture the user's REAL PATH so the spawned claude finds version-managed tools
# (node, uv, asdf shims, bun, ~/.local/bin, ...). The daemon runs under launchd
# with a minimal PATH, so a hardcoded list misses most of these. We source the
# user's login shell (zsh) to reproduce their interactive PATH — and zsh's rc,
# unlike .bashrc, does NOT run ssh-add, so this won't block on a passphrase
# prompt. We seed homebrew first so `$(brew --prefix ...)` inside .zshrc resolves.
# claude itself is still launched DIRECTLY (no interactive rc in its TUI); only
# the PATH is borrowed. Falls back to a safe default if capture fails.
FALLBACK_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
USER_PATH="$(PATH="$FALLBACK_PATH" zsh -ic 'print -rn -- $PATH' 2>/dev/null || true)"
case ":$USER_PATH:" in
    *":/opt/homebrew/bin:"*) ;;          # looks sane (has homebrew)
    *) USER_PATH="$FALLBACK_PATH" ;;     # capture failed/garbled — fall back
esac

# Env injected into the spawned claude, passed via a `/usr/bin/env KEY=VAL ...`
# wrapper on the launched command (below) rather than tmux `-e`.
# WHY NOT tmux `-e`: `new-session -e VAR=val` sets the variable in the tmux SESSION
# environment, which every window opened in that session later inherits — so
# TELEGRAM_BRIDGE_SPAWNED=1 (and the bot token!) leaked into the user's own attended
# windows, making the permission hook gate sessions it should not. The env wrapper
# scopes these to THIS claude process only. (PATH must be here regardless: tmux
# silently ignores `-e PATH=`, and the launchd daemon's PATH is minimal, so without
# it the spawned claude's hooks fail with `node: command not found`.)
ENV_WRAP=(
    "PATH=$USER_PATH"
    "TELEGRAM_BRIDGE_BOT_TOKEN=$TELEGRAM_BRIDGE_BOT_TOKEN"
    "TELEGRAM_BRIDGE_SPAWNED=1"
    # Race-free spawn binding: set BEFORE the pane exists, so the resolver
    # (bridge_resolve, env-first) resolves this session's topic even in the window
    # before the pane option is observable. The pane option (stamped below) is the
    # durable record; this env var closes the spawn-startup race.
    "TELEGRAM_BRIDGE_THREAD_ID=$THREAD_ID"
    "HOME=$HOME"
)

# Compaction-replacement env: the spawned /telegram skill reads these to take its
# existing-topic attach path instead of creating a fresh topic.
if [ -n "$ATTACH_THREAD" ]; then
    ENV_WRAP+=("TELEGRAM_BRIDGE_ATTACH_THREAD=$ATTACH_THREAD")
fi
if [ -n "$RESTORE_FILE" ]; then
    ENV_WRAP+=("TELEGRAM_BRIDGE_RESTORE_FILE=$RESTORE_FILE")
fi

# Operator intent pass-through (issue #11): expose the note to lifecycle hooks
# (e.g. start.txt) so a restore hook can fold it into the resumed context. The
# spawn prompt already surfaces it verbally; this makes it available to scripts.
if [ -n "$INTENT" ]; then
    ENV_WRAP+=("TELEGRAM_BRIDGE_INTENT=$INTENT")
fi

# Claude launch flags, shared by both tmux branches below.
#   --permission-mode dontAsk : an unattended spawn must never block on a native
#                          permission prompt. In dontAsk, a tool the Tier-2 hook
#                          doesn't explicitly allow is auto-DENIED (not prompted),
#                          so the pane can't wedge; the hook routes the dangerous
#                          set to the owner over Telegram and floors the rest.
#   --disallowedTools AskUserQuestion : native AUQ would render a blocking picker
#                          into the detached pane; disable it.
#   --mcp-config <auq>   : wire in the optional Telegram AUQ MCP so the session
#                          can still ask the owner via phone buttons (the
#                          supported substitute for the disabled native AUQ).
#                          Added ONLY if the rendered config exists.
# NOTE: we do NOT pass --settings — it would replace the user's settings and drop
# the PreToolUse permission hook (see SPAWN_SETTINGS note above). The hook reads
# the persisted-allow file directly instead.
CLAUDE_FLAGS=(
    --permission-mode dontAsk
    --disallowedTools AskUserQuestion
)
if [ -f "$AUQ_MCP_CONFIG" ]; then
    CLAUDE_FLAGS+=(--mcp-config "$AUQ_MCP_CONFIG")
fi

# Create the window/session and CAPTURE its pane id (-P -F '#{pane_id}') so we can
# stamp the pane option programmatically. rc is checked separately from pane-id
# emptiness: a non-zero rc means creation actually failed (clean up the topic we
# created); a zero rc with an empty pane id is pathological — warn but never tear
# down a live session.
if "$TMUX_BIN" has-session -t "=$SHARED" 2>/dev/null; then
    # Target "=$SHARED:" — the EXACT session, trailing colon = "pick the next
    # free window index". A bare "-t $SHARED" makes new-window target index 0,
    # which fails ("create window failed: index 0 in use") once the session
    # already has a window 0 (base-index is 0).
    set +e
    PANE_ID="$("$TMUX_BIN" new-window -t "=$SHARED:" -n "$win" -c "$dir" -P -F '#{pane_id}' \
        /usr/bin/env "${ENV_WRAP[@]}" "$CLAUDE_BIN" "${CLAUDE_FLAGS[@]}" -- "$prompt")"
    rc=$?
    set -e
else
    set +e
    PANE_ID="$("$TMUX_BIN" new-session -d -s "$SHARED" -n "$win" -c "$dir" -P -F '#{pane_id}' \
        /usr/bin/env "${ENV_WRAP[@]}" "$CLAUDE_BIN" "${CLAUDE_FLAGS[@]}" -- "$prompt")"
    rc=$?
    set -e
fi

if [ "$rc" -ne 0 ]; then
    # Creation failed. Close the topic WE just created (not a rollover reuse) so a
    # failed spawn doesn't leak an orphan topic.
    if [ -z "$ATTACH_THREAD" ] && [ -n "$CHAT_ID" ]; then
        /usr/bin/python3 - "$CHAT_ID" "$THREAD_ID" <<'PY' || true
import json, os, sys, urllib.request
chat, thread = sys.argv[1], sys.argv[2]
tok = os.environ.get("TELEGRAM_BRIDGE_BOT_TOKEN", "")
data = json.dumps({"chat_id": int(chat), "message_thread_id": int(thread)}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(
        "https://api.telegram.org/bot%s/closeForumTopic" % tok,
        data=data, headers={"Content-Type": "application/json"}), timeout=20).read()
except Exception:
    pass
PY
    fi
    echo "telegram-spawn: tmux window/session creation failed (rc=$rc)" >&2
    exit 1
fi

# --- stamp the pane + write the registry (the programmatic bind) -------------
# claude is the pane's DIRECT command, so the pane pid IS the claude pid (no tree
# walk). Stamp the durable pane option; an empty pane id (pathological) just means
# the option isn't stamped — the env-var binding above still resolves the session.
CLAUDE_PID=""
if [ -n "$PANE_ID" ]; then
    CLAUDE_PID="$("$TMUX_BIN" display-message -p -t "$PANE_ID" '#{pane_pid}' 2>/dev/null || true)"
    "$TMUX_BIN" set-option -p -t "$PANE_ID" @telegram_thread_id "$THREAD_ID" 2>/dev/null || true
else
    echo "telegram-spawn: WARNING could not capture pane id; pane option not stamped (env-var binding still applies)" >&2
fi

# Write the registry entry LAST so a half-finished spawn never leaves a partial
# record. transcript_path is left empty here (the transcript doesn't exist until
# the session starts) — the router backfills it from the SessionStart self-record.
# On a compaction rollover, carry the prior entry's `spawned` flag forward so an
# adopted (user-owned) session that rolls over still DETACHES, not reaps, on /end.
INBOX="/tmp/claude-telegram/sessions/$THREAD_ID/inbox.jsonl"
mkdir -p "$REGISTRY_DIR" "/tmp/claude-telegram/sessions/$THREAD_ID"
ROLLOVER=0; [ -n "$ATTACH_THREAD" ] && ROLLOVER=1
/usr/bin/python3 - "$REGISTRY_DIR" "$THREAD_ID" "$PANE_ID" "$CLAUDE_PID" "$INBOX" "$dir" "$ROLLOVER" <<'PY' || true
import datetime, json, os, sys
reg_dir, thread_id, pane_id, claude_pid, inbox, cwd, rollover = sys.argv[1:8]
path = os.path.join(reg_dir, "%s.json" % thread_id)
if rollover == "1":
    # Carry `spawned` forward from the session being replaced (default True if its
    # entry is gone — most rollovers are /new, which are bridge-owned).
    try:
        spawned = bool(json.load(open(path)).get("spawned", True))
    except Exception:
        spawned = True
else:
    spawned = True   # a fresh /new spawn is bridge-owned
entry = {
    "thread_id": int(thread_id),
    "pane_id": pane_id or None,
    "claude_pid": int(claude_pid) if claude_pid else None,
    "inbox_path": inbox,
    "cwd": cwd,
    "transcript_path": "",
    "spawned": spawned,
    "context": "session attached",
    "registered_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(entry, fh, indent=2)
os.replace(tmp, path)
PY

echo "telegram-spawn: launched window '$win' (pane ${PANE_ID:-?}, topic $THREAD_ID) in session $SHARED (dir: $dir)"
