#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-context: compute a bridged session's context occupancy from its own
# transcript JSONL and write status.json for the sticky + auto-compaction.
#
# Context occupancy ~= the latest assistant turn's input side:
#   input_tokens + cache_read_input_tokens + cache_creation_input_tokens
# output_tokens is not resident context, so it is excluded. Window is 1,000,000
# for the Opus 1m variant (model id contains "[1m]"), else 200,000 — with a
# safety fallback to 1m when the observed token count already exceeds 200k (a
# session whose model id was recorded without the [1m] suffix).
#
# Writes /tmp/claude-telegram/sessions/<thread>/status.json:
#   {"pct":0.37,"tokens":372000,"window":1000000,"msgs":42,
#    "model":"claude-opus-4-8[1m]","updated":"2026-06-18T21:00:00Z"}
#
# Usage:
#   telegram-context.py --transcript PATH --thread THREAD_ID
#                       [--sessions-root /tmp/claude-telegram/sessions]
#
# Stdlib only; targets /usr/bin/python3 so it runs anywhere the other bridge
# scripts do. Exits non-zero (without clobbering a prior good status.json) when
# the transcript has no usable usage block yet.

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_WINDOW = 200_000
WIDE_WINDOW = 1_000_000
SESSIONS_ROOT = Path("/tmp/claude-telegram/sessions")


def compute(transcript_path):
    """Return the status dict for a transcript, or None if no usage seen yet."""
    tokens = 0
    model = ""
    msgs = 0
    try:
        with open(transcript_path) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                msgs += 1
                usage = msg.get("usage") or {}
                turn = ((usage.get("input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0))
                # Latest assistant turn with usage wins (overwrite as we go).
                if turn:
                    tokens = turn
                    model = msg.get("model") or model
    except OSError:
        return None

    if tokens == 0:
        return None

    wide = "[1m]" in (model or "") or tokens > DEFAULT_WINDOW
    window = WIDE_WINDOW if wide else DEFAULT_WINDOW
    return {
        "pct": round(tokens / window, 4),
        "tokens": tokens,
        "window": window,
        "msgs": msgs,
        "model": model,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main():
    ap = argparse.ArgumentParser(description="Compute bridge session context %.")
    ap.add_argument("--transcript", required=True, help="path to the session transcript JSONL")
    ap.add_argument("--thread", required=True, help="Telegram message_thread_id")
    ap.add_argument("--sessions-root", default=str(SESSIONS_ROOT))
    args = ap.parse_args()

    data = compute(args.transcript)
    if data is None:
        # No usage yet (brand-new session) — don't overwrite a prior good status.
        print("no usage in transcript yet", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.sessions_root) / str(args.thread)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / "status.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(out_dir / "status.json")
    except OSError as e:
        print("could not write status.json: {}".format(e), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(data))


if __name__ == "__main__":
    main()
