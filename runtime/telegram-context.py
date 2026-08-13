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
# output_tokens is not resident context, so it is excluded.
#
# Window sizing: the model id used to carry the tier as a "[1m]" suffix, but a
# Claude Code update (2026-08-12) dropped it — the bridged session now records
# "claude-opus-4-8" with no tier marker, so the id alone can no longer tell 1m
# from 200k. Opus 4.8 as this bridge runs it IS the 1m variant, so treat any
# opus-4-8 id (suffixed or not) as wide. The tokens>200k safety fallback still
# catches any other model silently on a wide window. Without this fix a session
# below 200k on a real 1m window was sized against 200k, inflating pct up to ~5x
# and tripping auto-compaction almost immediately.
#   Downside if the policy is ever wrong (a genuinely-200k opus-4-8): the window
#   is over-sized, so compaction fires late instead of early. Preferable to
#   today's thrash, and overridable by editing WIDE_MODELS.
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

# Model-id substrings that mean a 1,000,000-token window. Matched case-insensitively
# against the recorded model id. The tier is no longer in the id (see module
# docstring), so this is a name-based allowlist, not a suffix check.
WIDE_MODELS = ("opus-4-8",)


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

    mid = (model or "").lower()
    wide = (
        "[1m]" in mid
        or any(w in mid for w in WIDE_MODELS)
        or tokens > DEFAULT_WINDOW
    )
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
