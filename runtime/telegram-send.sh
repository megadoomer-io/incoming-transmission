#!/usr/bin/env bash
# Send a message to a Telegram chat/topic via the Bot API.
# Usage: telegram-send.sh <chat_id> <thread_id|-> <text>
#   thread_id "-" or "general" sends to the General topic (no thread).
# Reads TELEGRAM_BRIDGE_BOT_TOKEN from the environment (never an argv).
# Chunks text to Telegram's 4096-char limit. Plain text, no parse_mode.
set -euo pipefail

if [[ -z "${TELEGRAM_BRIDGE_BOT_TOKEN:-}" ]]; then
    echo "TELEGRAM_BRIDGE_BOT_TOKEN not set, cannot send" >&2
    exit 1
fi

chat_id="${1:?usage: telegram-send.sh <chat_id> <thread_id|-> <text>}"
thread_id="${2:?usage: telegram-send.sh <chat_id> <thread_id|-> <text>}"
text="${3:?usage: telegram-send.sh <chat_id> <thread_id|-> <text>}"

api="https://api.telegram.org/bot${TELEGRAM_BRIDGE_BOT_TOKEN}/sendMessage"

# Chunk into <=4000-char pieces and POST each as JSON (json.dumps handles escaping).
TG_CHAT="$chat_id" TG_THREAD="$thread_id" TG_API="$api" \
python3 - "$text" <<'PY'
import json, os, sys, urllib.request, urllib.error

text = sys.argv[1]
chat = os.environ["TG_CHAT"]
thread = os.environ["TG_THREAD"]
api = os.environ["TG_API"]

chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
for chunk in chunks:
    payload = {"chat_id": chat, "text": chunk, "disable_web_page_preview": True}
    if thread not in ("-", "general", ""):
        payload["message_thread_id"] = int(thread)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(api, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
        if not resp.get("ok"):
            print("telegram-send: API error: {}".format(resp), file=sys.stderr)
            sys.exit(1)
    except urllib.error.URLError as e:
        print("telegram-send: request failed: {}".format(e), file=sys.stderr)
        sys.exit(1)
PY
