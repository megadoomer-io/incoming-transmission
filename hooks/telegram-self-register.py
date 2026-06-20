#!/usr/bin/env python3
"""SessionStart hook: register this session's transcript for the Telegram bridge.

The bridge's context-% / auto-compaction feature needs a live session to find its
*own* transcript JSONL. The robust source is the SessionStart payload, which carries
`session_id` and `transcript_path`. This hook persists that to
`~/.local/state/telegram-bridge/self/<session_id>.json` so the `/telegram` skill can
later bind the right transcript to its topic.

At `/telegram` attach the skill picks the newest-mtime transcript among same-cwd
records (the attaching session's transcript is being written *right now*, so it wins)
and records that path into `registry/<thread>.json`. The poll cron then reads context
occupancy from that path each tick.

Fires for EVERY session (cheap, stdlib-only) since we can't know at startup which one
will attach. Records pointing at vanished transcripts are pruned. Never fails session
startup: any error is swallowed and the hook exits 0 silently.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    Path.home() / ".local" / "state" / "telegram-bridge"))
SELF_DIR = STATE_DIR / "self"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    session_id = str(payload.get("session_id") or "").strip()
    transcript = str(payload.get("transcript_path") or "").strip()
    cwd = str(payload.get("cwd") or os.getcwd())
    if not session_id or not transcript:
        return

    try:
        SELF_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    # Prune records whose transcript no longer exists (Claude Code cleaned up an
    # ended session). Best-effort; pruning failure must not block startup.
    try:
        for f in SELF_DIR.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
            except Exception:
                f.unlink(missing_ok=True)
                continue
            tp = rec.get("transcript_path")
            if not tp or not Path(tp).exists():
                f.unlink(missing_ok=True)
    except Exception:
        pass

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128] or "session"
    record = {
        "session_id": session_id,
        "transcript_path": transcript,
        "cwd": cwd,
        # getppid() is the launcher (uv/env), not the claude process, so it is NOT
        # a reliable liveness signal — selection uses transcript mtime instead.
        "pid": os.getppid(),
        "source": payload.get("source"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = SELF_DIR / (safe + ".json.tmp")
    dest = SELF_DIR / (safe + ".json")
    try:
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(dest)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute backstop: a hook bug must never fail session startup.
        pass
