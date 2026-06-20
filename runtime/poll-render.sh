#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# poll-render: instantiate the bridge poll-cron prompt from the template for a
# specific session. The session-scoped poll cron is created with this rendered
# text; the prompt's BACKOFF RESCHEDULE step re-renders via this same helper, so
# the literal {{...}} placeholders live ONLY here and in poll-prompt.tmpl — never
# in a live prompt (where spawn-time substitution would clobber them).
#
# Usage:  poll-render.sh THREAD_ID CHAT_ID INBOX CWD
# Emits the rendered prompt on stdout.
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: poll-render.sh THREAD_ID CHAT_ID INBOX CWD" >&2
    exit 2
fi

BRIDGE_DIR="${TELEGRAM_BRIDGE_DIR:-$HOME/.telegram-bridge}"
TMPL="$BRIDGE_DIR/poll-prompt.tmpl"
[ -r "$TMPL" ] || { echo "poll-render: template not readable: $TMPL" >&2; exit 1; }

thread="$1"; chat="$2"; inbox="$3"; cwd="$4"

# `|` is safe as the sed delimiter: thread/chat are numeric, inbox/cwd are paths
# (no pipes). Values are positional args, never interpolated into the pattern.
sed -e "s|{{THREAD_ID}}|${thread}|g" \
    -e "s|{{CHAT_ID}}|${chat}|g" \
    -e "s|{{INBOX}}|${inbox}|g" \
    -e "s|{{CWD}}|${cwd}|g" \
    "$TMPL"
