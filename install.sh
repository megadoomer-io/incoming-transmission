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

# 1. Runtime code + configs (everything except *.template / *.example).
for f in "$REPO_DIR"/runtime/*; do
    base="$(basename "$f")"
    case "$base" in
        *.template|*.example) continue ;;
    esac
    cp "$f" "$PREFIX/$base"
    echo "installed $PREFIX/$base"
done

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
   - (optional) Tier-2 permission hook for unattended spawns
   - (optional) AskUserQuestion MCP from $PREFIX/telegram-auq-mcp.json

6. (optional) Start the wedge watchdog:
     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.telegram.watchdog.plist

7. In any Claude Code session, run /telegram to attach it to a topic.
EOF
