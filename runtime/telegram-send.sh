#!/usr/bin/env bash
# Send a message to a Telegram chat/topic via the Bot API.
# Usage: telegram-send.sh <chat_id> <thread_id|-> <text|->
#   thread_id "-" or "general" sends to the General topic (no thread).
#   text "-" reads the message from stdin, so a quoted heredoc (<<'EOF') can
#   carry "$" and backticks without the calling shell expanding them.
# Retries timeouts, connection resets, 429 and 5xx (3 attempts, backoff).
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
# python3 below reads its program from a heredoc on stdin, so slurp ours here.
if [[ "$text" == "-" ]]; then
    text="$(cat)"
fi

api="https://api.telegram.org/bot${TELEGRAM_BRIDGE_BOT_TOKEN}/sendMessage"
# Same message_id -> topic index the router reads to route emoji reactions back
# to the right session (a message_reaction update carries no thread id).
state_dir="${TELEGRAM_BRIDGE_STATE_DIR:-$HOME/.local/state/telegram-bridge}"

# Chunk into <=4000-char pieces and POST each as JSON (json.dumps handles escaping).
TG_CHAT="$chat_id" TG_THREAD="$thread_id" TG_API="$api" TG_STATE_DIR="$state_dir" \
python3 - "$text" <<'PY'
import json, os, sys, time, urllib.request, urllib.error

text = sys.argv[1]
chat = os.environ["TG_CHAT"]
thread = os.environ["TG_THREAD"]
api = os.environ["TG_API"]
state_dir = os.environ["TG_STATE_DIR"]
tkey = "general" if thread in ("-", "general", "") else str(int(thread))
index_path = os.path.join(state_dir, "msg-index.jsonl")


def record_sent(message_id):
    """Best-effort: index this sent message's id -> topic so a reaction on it
    routes back to this session. Never fail the send over an index write."""
    if message_id is None:
        return
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(index_path, "a") as fh:
            fh.write(json.dumps({"message_id": int(message_id), "thread": tkey}) + "\n")
    except (OSError, ValueError):
        pass


def send(req, attempts=3):
    """POST with retry on transient failures. A timeout after Telegram accepted
    the message can rarely duplicate it; a dropped reply is the worse outcome."""
    for n in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                try:
                    return json.load(e)
                except ValueError:
                    return {"ok": False, "error_code": e.code}
            wait = 2 * n
            if e.code == 429:
                try:
                    wait = int(json.load(e)["parameters"]["retry_after"])
                except (ValueError, KeyError, TypeError):
                    pass
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            wait, err = 2 * n, e
        if n < attempts:
            print("telegram-send: attempt {} failed ({}), retrying".format(n, err),
                  file=sys.stderr)
            time.sleep(wait)
    print("telegram-send: request failed: {}".format(err), file=sys.stderr)
    sys.exit(1)


chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
for chunk in chunks:
    payload = {"chat_id": chat, "text": chunk, "disable_web_page_preview": True}
    if thread not in ("-", "general", ""):
        payload["message_thread_id"] = int(thread)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(api, data=data,
                                 headers={"Content-Type": "application/json"})
    resp = send(req)
    if not resp.get("ok"):
        print("telegram-send: API error: {}".format(resp), file=sys.stderr)
        sys.exit(1)
    record_sent(resp.get("result", {}).get("message_id"))
PY
