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
# Usage: telegram-spawn.sh [--attach <thread> --restore <file>] <target_dir>
#   plain <dir>            -> open a fresh topic (normal /new)
#   --attach/--restore     -> compaction handoff: reattach to an existing topic.
#                             Attach mode is flag-only (never read from the env)
#                             so it can't be inherited and hijack a live topic.
set -euo pipefail

TMUX_BIN="${TELEGRAM_BRIDGE_TMUX:-/opt/homebrew/bin/tmux}"
CLAUDE_BIN="${TELEGRAM_BRIDGE_CLAUDE:-/opt/homebrew/bin/claude}"
ALIAS_FILE="${TELEGRAM_BRIDGE_DIR_ALIASES:-$HOME/.telegram-bridge/dir-aliases.json}"
AUQ_MCP_CONFIG="${TELEGRAM_BRIDGE_AUQ_MCP_CONFIG:-$HOME/.telegram-bridge/telegram-auq-mcp.json}"

# Attach (compaction-replacement) mode is requested EXPLICITLY via flags, never
# inherited from the environment. A session spawned for compaction exports
# TELEGRAM_BRIDGE_ATTACH_THREAD, so a plain `telegram-spawn.sh <dir>` run from
# inside such a session would otherwise inherit it and hijack that session's
# topic instead of opening a fresh one. Clear any inherited values up front and
# take attach intent only from --attach/--restore.
unset TELEGRAM_BRIDGE_ATTACH_THREAD TELEGRAM_BRIDGE_RESTORE_FILE
ATTACH_THREAD=""
RESTORE_FILE=""
raw_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --attach)  ATTACH_THREAD="${2:?--attach needs a thread id}"; shift 2 ;;
        --restore) RESTORE_FILE="${2:?--restore needs a file path}"; shift 2 ;;
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

# Every spawn lands as a WINDOW (a tab) in one shared "claude" tmux session, so
# `tmux attach -t claude` shows them all as tabs — cycle with prefix + Tab. The
# window is named for the repo so the tab is recognizable.
SHARED="claude"
win="$(basename "$dir" | tr -cd 'a-zA-Z0-9._-')"

# The new session self-attaches: /telegram creates its own topic (named from the
# cwd/branch) and loads the bridge procedure (no cron — the router drives timing).
# TELEGRAM_BRIDGE_SPAWNED=1 also activates the Tier-2 permission hook for this
# session only.
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

if [ -n "$ATTACH_THREAD" ]; then
    prompt="You are a compaction replacement for a Telegram-bridged session whose context filled up. Do these IN ORDER, then wait for instructions:
1. Restore prior working context. The rollover handoff file is at ${RESTORE_FILE}. Run your START hook now: follow the instructions in ~/.telegram-bridge/lifecycle/start.txt (ignore #-comment lines).
2. Invoke the /telegram skill. It will detect TELEGRAM_BRIDGE_ATTACH_THREAD=${ATTACH_THREAD} and attach to that EXISTING topic (skipping topic creation), register ownership, write the handoff-ready marker, wait for the compaction lock to clear, then load the bridge procedure and do an initial drain (it runs no cron — the router drives timing).
3. Continue the restored work.

${spawn_mechanism}"
else
    prompt="You were spawned by the telegram bridge. Do these IN ORDER, then wait for instructions:
1. Restore any prior context for this project. Run your START hook: follow the instructions in ~/.telegram-bridge/lifecycle/start.txt (ignore #-comment lines). This is a fresh spawn with no rollover handoff file — if your hook looks for one, that's fine, just carry on.
2. Invoke the /telegram skill to attach yourself to a Telegram topic.
3. Wait for instructions.

${spawn_mechanism}"
fi

# Pre-trust the target dir so the spawned claude doesn't hang on the "Do you
# trust the files in this folder?" dialog (nobody is at the detached pane to
# answer). This sets ONLY the per-folder trust flag in ~/.claude.json.
# NOTE: spawned sessions launch with --dangerously-skip-permissions (below) so
# unattended tool calls never block on the native permission prompt. A hook
# returning permissionDecision="allow" does NOT suppress that prompt in Claude
# Code 2.1.170 (only "deny" is honored), so the engine-level flag is the only
# reliable way to keep an unattended session flowing. Native AskUserQuestion is
# disabled for spawns (--disallowedTools below) because a blocking prompt has no
# one to answer it in a detached pane; the optional Telegram AUQ MCP is the
# supported channel for asking the owner instead. Best-effort: a failure here
# just means the dialog may appear (it won't break the spawn). Atomic via tmp+rename.
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

# Shared env injected into the spawned claude (the daemon's launchd env is minimal).
# NOTE: PATH is deliberately NOT here. tmux silently ignores `new-window -e PATH=`
# (arbitrary vars work, but the child always inherits PATH from the tmux server's
# global environment, which the launchd daemon started minimal). So PATH is forced
# via a `/usr/bin/env PATH=...` wrapper on the launched command below instead. Get
# this wrong and the spawned claude's hooks (claude-mem, etc.) fail with
# `node: command not found` because /opt/homebrew/bin isn't on PATH.
ENV_ARGS=(
    -e "TELEGRAM_BRIDGE_BOT_TOKEN=$TELEGRAM_BRIDGE_BOT_TOKEN"
    -e "TELEGRAM_BRIDGE_SPAWNED=1"
    -e "HOME=$HOME"
)

# Compaction-replacement env: the spawned /telegram skill reads these to take its
# existing-topic attach path instead of creating a fresh topic.
if [ -n "$ATTACH_THREAD" ]; then
    ENV_ARGS+=(-e "TELEGRAM_BRIDGE_ATTACH_THREAD=$ATTACH_THREAD")
fi
if [ -n "$RESTORE_FILE" ]; then
    ENV_ARGS+=(-e "TELEGRAM_BRIDGE_RESTORE_FILE=$RESTORE_FILE")
fi

# Claude launch flags, shared by both tmux branches below.
#   --*-skip-permissions : an unattended spawn must not block on the native
#                          permission prompt (no human at the pane to answer it).
#   --disallowedTools AskUserQuestion : native AUQ would render a blocking picker
#                          into the detached pane; disable it.
#   --mcp-config <auq>   : wire in the optional Telegram AUQ MCP so the session
#                          can still ask the owner via phone buttons (the
#                          supported substitute for the disabled native AUQ).
#                          Added ONLY if the rendered config exists — the MCP is
#                          optional (needs uv + the mcp package). Absent config =
#                          skip it: the spawn still works, the session just can't
#                          ask questions. Without this flag a spawn had native AUQ
#                          disabled AND no replacement, so it could not ask at all.
CLAUDE_FLAGS=(
    --allow-dangerously-skip-permissions
    --dangerously-skip-permissions
    --disallowedTools AskUserQuestion
)
if [ -f "$AUQ_MCP_CONFIG" ]; then
    CLAUDE_FLAGS+=(--mcp-config "$AUQ_MCP_CONFIG")
fi

if "$TMUX_BIN" has-session -t "=$SHARED" 2>/dev/null; then
    # Target "=$SHARED:" — the EXACT session, trailing colon = "pick the next
    # free window index". A bare "-t $SHARED" makes new-window target index 0,
    # which fails ("create window failed: index 0 in use") once the session
    # already has a window 0 (base-index is 0).
    "$TMUX_BIN" new-window -t "=$SHARED:" -n "$win" -c "$dir" "${ENV_ARGS[@]}" \
        /usr/bin/env "PATH=$USER_PATH" "$CLAUDE_BIN" "${CLAUDE_FLAGS[@]}" -- "$prompt"
else
    "$TMUX_BIN" new-session -d -s "$SHARED" -n "$win" -c "$dir" "${ENV_ARGS[@]}" \
        /usr/bin/env "PATH=$USER_PATH" "$CLAUDE_BIN" "${CLAUDE_FLAGS[@]}" -- "$prompt"
fi

echo "telegram-spawn: launched window '$win' in session $SHARED (dir: $dir)"
