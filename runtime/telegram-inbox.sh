#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# telegram-inbox: the mechanical half of a bridge session's drain, as ONE
# whitelistable command instead of ad-hoc bash.
#
# WHY: the drain procedure used per-call bash (mkdir lock, tail inbox, echo offset,
# rmdir) with $(...) / variable expansion. Each unique expansion looks novel to the
# Bash permission firewall (nah / allowlist), so it misses the allowlist and prompts
# the owner -- death by a thousand approvals, and worse now that bridge sessions
# gate. Routing the mechanics through this fixed script means a single allowlist
# entry covers the whole drain:  Bash(~/.telegram-bridge/telegram-inbox.sh:*)
#
# The SESSION still owns the per-message reasoning + replies (telegram-send.sh).
# This script only does lock / read / offset, in two phases so a crash mid-reply
# never loses messages:
#
#   telegram-inbox.sh drain <thread>
#       Acquire the per-topic lock (reclaiming a stale one past poll_lock_ttl_seconds),
#       print the new inbox lines (JSON, one per line) after read.offset, and record
#       the would-be new offset in SESS/.drain-pending. With backlog the lock stays
#       HELD (read.offset is NOT advanced yet) until `ack`. With NO backlog it prints
#       nothing, releases the lock immediately, and needs no ack — so an empty drain
#       (e.g. the bind-time section-A drain before any message arrives) can't leak the
#       lock. Exit 3 (LOCKED on stderr) if a live drain already holds the lock.
#
#   telegram-inbox.sh ack <thread>
#       Commit SESS/.drain-pending -> read.offset and release the lock. Run this
#       only AFTER you've processed + replied to every line `drain` printed. If the
#       session dies between drain and ack, the offset isn't advanced and the lock
#       goes stale -> the lines are re-drained next cycle (at-least-once, no loss).
#
# Stdlib bash + coreutils + jq (already a bridge dependency). macOS/Linux.

set -euo pipefail

CMD="${1:-}"
THREAD="${2:-}"
if [ -z "$CMD" ] || [ -z "$THREAD" ]; then
    echo "usage: telegram-inbox.sh drain|ack <thread_id>" >&2
    exit 2
fi

SESS="/tmp/claude-telegram/sessions/$THREAD"
INBOX="$SESS/inbox.jsonl"
LOCK="$SESS/poll.lock.d"
OFFSET="$SESS/read.offset"
PENDING="$SESS/.drain-pending"
CONF="$HOME/.telegram-bridge/compaction.json"

mkdir -p "$SESS"

case "$CMD" in
    drain)
        if ! mkdir "$LOCK" 2>/dev/null; then
            # Lock held. Reclaim it only if it's stale (acquire-time = dir mtime,
            # older than poll_lock_ttl_seconds; default 1800). Otherwise a live
            # drain owns it -> bail so we don't double-reply.
            ttl="$(jq -r '.poll_lock_ttl_seconds // 1800' "$CONF" 2>/dev/null || echo 1800)"
            # Reclaim ONLY if the lock dir is actually stale. Test find's OUTPUT, not
            # its exit code: `find -mmin +N` exits 0 even when nothing matches, so
            # `if find ...` would always reclaim (incl. a live lock -> double-drain).
            if [ -n "$(find "$LOCK" -maxdepth 0 -mmin "+$((ttl / 60))" 2>/dev/null)" ]; then
                rmdir "$LOCK" 2>/dev/null || true
                mkdir "$LOCK" 2>/dev/null || { echo "LOCKED" >&2; exit 3; }
            else
                echo "LOCKED" >&2
                exit 3
            fi
        fi
        off="$(cat "$OFFSET" 2>/dev/null || echo 0)"
        total="$(wc -l 2>/dev/null < "$INBOX" | tr -d ' ' || echo 0)"
        off="${off:-0}"; total="${total:-0}"
        if [ "$total" -le "$off" ]; then
            # Nothing new to hand off: release the lock we just took and require
            # NO ack. drain/ack is a lock handshake (drain acquires, ack releases),
            # so an empty drain that kept the lock leaks it when the session
            # reasonably concludes "nothing to drain, no ack needed" — exactly what
            # the bind-time section-A drain does before any message has arrived. The
            # first real message then can't acquire the lock until the 30m stale-TTL
            # (or a self-heal) clears it. Self-releasing here makes the empty path
            # safe regardless of whether the session acks.
            rm -f "$PENDING" 2>/dev/null || true
            rmdir "$LOCK" 2>/dev/null || true
            exit 0
        fi
        echo "$total" > "$PENDING"
        tail -n "+$((off + 1))" "$INBOX"
        ;;
    ack)
        pend="$(cat "$PENDING" 2>/dev/null || echo "")"
        if [ -n "$pend" ]; then
            echo "$pend" > "$OFFSET"
            rm -f "$PENDING"
        fi
        rmdir "$LOCK" 2>/dev/null || true
        ;;
    *)
        echo "usage: telegram-inbox.sh drain|ack <thread_id>" >&2
        exit 2
        ;;
esac
