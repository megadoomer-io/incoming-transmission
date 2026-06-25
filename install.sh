#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# incoming-transmission installer (macOS / launchd).
#
# Lays the runtime files into ~/.telegram-bridge, the control CLI into
# ~/.local/bin, and renders the MCP config. It does NOT start the daemon or edit
# your Claude Code settings — it prints the exact next steps so you stay in
# control. Re-running is safe (idempotent): it overwrites code, preserves your
# local dir-aliases.json and any runtime state.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${INCOMING_TRANSMISSION_PREFIX:-$HOME/.telegram-bridge}"
BIN_DIR="${INCOMING_TRANSMISSION_BIN:-$HOME/.local/bin}"
STATE_DIR="${INCOMING_TRANSMISSION_STATE:-$HOME/.local/state/telegram-bridge}"
UV_BIN="${INCOMING_TRANSMISSION_UV:-$(command -v uv || echo /opt/homebrew/bin/uv)}"

echo "incoming-transmission installer"
echo "  repo:   $REPO_DIR"
echo "  prefix: $PREFIX"
echo "  bin:    $BIN_DIR"
echo "  state:  $STATE_DIR"
echo

mkdir -p "$PREFIX" "$BIN_DIR" "$STATE_DIR"

# 1. Runtime code + configs (everything except *.template / *.example and the
#    lifecycle/ subdir, which is seeded separately below). The examples are seeded
#    to their live names later, only if absent.
for f in "$REPO_DIR"/runtime/*; do
    [ -d "$f" ] && continue   # skip subdirs (lifecycle/ handled below)
    base="$(basename "$f")"
    case "$base" in
        *.template|*.example|*.example.txt|*.example.json) continue ;;
        permissions.json) continue ;;   # spawned-session posture; seeded below, never clobbered
    esac
    cp "$f" "$PREFIX/$base"
    echo "installed $PREFIX/$base"
done

# 1b. Make installed runtime scripts executable. They are invoked directly
#     (telegram-spawn.sh, poll-render.sh, the .py hooks/servers run via their
#     shebangs), so the +x bit must survive the copy regardless of the source
#     file's mode in the checkout.
for s in "$PREFIX"/*.sh "$PREFIX"/*.py; do
    [ -f "$s" ] && chmod +x "$s"
done
echo "chmod +x runtime scripts in $PREFIX"

# 2. Render the MCP config from its template (path + uv binary are absolute).
sed -e "s|__HOME__|$HOME|g" -e "s|__UV__|$UV_BIN|g" \
    "$REPO_DIR/runtime/telegram-auq-mcp.json.template" \
    > "$PREFIX/telegram-auq-mcp.json"
echo "rendered $PREFIX/telegram-auq-mcp.json"

# 3. Seed dir-aliases.json from the example only if the user has none yet.
if [ ! -f "$PREFIX/dir-aliases.json" ]; then
    cp "$REPO_DIR/runtime/dir-aliases.example.json" "$PREFIX/dir-aliases.json"
    echo "seeded $PREFIX/dir-aliases.json (edit this with your repos)"
else
    echo "kept existing $PREFIX/dir-aliases.json"
fi

# 3a. Seed permissions.json (spawned-session posture) from the repo default only if
#     the user has none yet. Re-running install must NOT reset a posture the user
#     chose (e.g. "ask") — like dir-aliases, this is local config we preserve.
if [ ! -f "$PREFIX/permissions.json" ]; then
    cp "$REPO_DIR/runtime/permissions.json" "$PREFIX/permissions.json"
    echo "seeded $PREFIX/permissions.json (spawned_mode: risk-tiered)"
else
    echo "kept existing $PREFIX/permissions.json"
fi

# 3a-ii. Seed the bridge-scoped settings file that holds persisted "always allow"
#        rules. Starts empty; the Tier-2 permission hook appends narrow allow-rules
#        to it when the owner taps "Always allow". The spawn loads it via --settings
#        so the engine enforces those rules. Preserved across re-installs (it is
#        accumulated, auditable state — like dir-aliases and permissions.json).
if [ ! -f "$PREFIX/spawned-allow.json" ]; then
    printf '{\n  "permissions": {\n    "allow": []\n  }\n}\n' > "$PREFIX/spawned-allow.json"
    echo "seeded $PREFIX/spawned-allow.json (persisted always-allow rules; starts empty)"
else
    echo "kept existing $PREFIX/spawned-allow.json"
fi

# 3b. Seed the lifecycle hooks from their examples, only if absent. These are the
#     "Customizing agent behavior" seam: style/end ship empty (default = normal
#     Claude / just detach), start/save ship a functional agnostic default. Edit
#     them to layer in your own save/restore/journal mechanism. The session reads
#     these live at each lifecycle moment, so changes take effect without a restart.
mkdir -p "$PREFIX/lifecycle"
for h in style start save end; do
    if [ ! -f "$PREFIX/lifecycle/$h.txt" ]; then
        cp "$REPO_DIR/runtime/lifecycle/$h.example.txt" "$PREFIX/lifecycle/$h.txt"
        echo "seeded $PREFIX/lifecycle/$h.txt"
    else
        echo "kept existing $PREFIX/lifecycle/$h.txt"
    fi
done

# 4. Control CLI + SessionStart hook.
cp "$REPO_DIR/bin/telegram-bridge" "$BIN_DIR/telegram-bridge"
chmod +x "$BIN_DIR/telegram-bridge"
echo "installed $BIN_DIR/telegram-bridge"
cp "$REPO_DIR/hooks/telegram-self-register.py" "$PREFIX/telegram-self-register.py"
chmod +x "$PREFIX/telegram-self-register.py"
echo "installed $PREFIX/telegram-self-register.py"

cat <<EOF

Done. Next steps (not automated — you stay in control):

1. Create a Telegram bot via @BotFather, then export its token:
     export TELEGRAM_BRIDGE_BOT_TOKEN="123456:ABC..."
   (put it in your shell profile / secrets manager so the daemon inherits it)

2. Lock the bridge to your Telegram username:
     export TELEGRAM_BRIDGE_ALLOWED_USERNAME="your_tg_handle"

3. Start the router daemon (renders + loads the launchd plist):
     telegram-bridge start

4. Add the bot to a Telegram group with TOPICS enabled, make it an admin,
   then send the bot any message once to bootstrap (captures chat_id + user_id).
     telegram-bridge status   # confirm chat_id is no longer null

5. Wire Claude Code (per the README "Claude Code wiring" section):
   - SessionStart hook -> $PREFIX/telegram-self-register.py
   - Tier-2 permission hook -> $PREFIX/telegram-permission-hook.py as a PreToolUse
     hook in ~/.claude/settings.json (NOT settings.local.json -- Claude Code ignores
     .local hooks in spawned sessions). Matcher "Write|Edit|NotebookEdit|Bash|mcp__.*",
     timeout 1800. Gates dangerous tool calls in every bridge session (tap-to-approve
     on your phone); see the README "Claude Code wiring" snippet.
   - (optional) AskUserQuestion MCP from $PREFIX/telegram-auq-mcp.json

6. In any Claude Code session, run /telegram to attach it to a topic.

7. (optional) Customize agent behavior via the lifecycle hooks in
   $PREFIX/lifecycle/ (read live by the session at each lifecycle moment):
     style.txt  - reply formatting for every bridged session (default: empty)
     start.txt  - restore context on a spawned/rollover birth (default: read handoff)
     save.txt   - persist context on rollover (default: summarize to the handoff)
     end.txt    - actions on /end before detaching (default: empty)
   Defaults make a fresh install behave like normal Claude over the transport. Each
   file documents a gstack example. See the README "Customizing agent behavior".
EOF
