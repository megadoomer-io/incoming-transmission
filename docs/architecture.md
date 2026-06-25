# Architecture

How `incoming-transmission` moves a message between your phone and a live Claude
Code session, and how it keeps that session healthy over a long conversation.

The design is **smart router, dumb session**: one daemon (the Massive) owns all
timing — routing, push delivery, the context gauge, and compaction triggering — and
each session is a purely reactive processor that drains its inbox when nudged. The
session runs no cron and schedules nothing.

The diagrams below are graphviz sources in [`diagrams/`](diagrams/), rendered to SVG
with `make diagrams` (requires [graphviz](https://graphviz.org/)). The Zim
vocabulary (the Massive, Invader, the Tallest, transmission, PAK) matches the
[README](../README.md#the-vocabulary).

## System topology

The pieces and who talks to whom: your phone's forum topic, the router daemon that
is the only process allowed to read Telegram, and the sessions it routes to.

![System architecture](diagrams/system-architecture.svg)

<!-- Source: diagrams/system-architecture.dot — regenerate with `make diagrams` -->

- **The Massive** (`telegram-router.py`, launchd) is the sole `getUpdates` reader —
  Telegram's long-poll is single-consumer, so exactly one process owns it. It routes
  each message by `message_thread_id` into that session's `inbox.jsonl`, then pushes
  the session a drain nudge. Its `context_loop` thread computes each gauge, triggers
  compaction, and re-nudges undrained inboxes — all off the `getUpdates` path so a
  slow transcript scan can't stall routing.
- **An Invader** is a live Claude Code session bound to one topic. It keeps full
  context, MCP, and hooks, and only acts when nudged.
- **Wedge auto-clear** lives in the router's `context_loop` (no separate daemon). It
  watches only the bridge's own panes; when one persists on a native prompt nobody
  can answer remotely (a native AskUserQuestion menu, a trust-folder dialog, an ssh
  passphrase), it sends Esc — cancel-only, never approve — and pings the owner. This
  is the one failure push delivery can't catch, because a wedged session never goes
  idle to receive a drain nudge.

## Message lifecycle

One transmission from tap to reply. Push delivery is the primary path; the backstop
is what makes a missed push self-correct without the session polling.

![Message lifecycle](diagrams/message-lifecycle.svg)

<!-- Source: diagrams/message-lifecycle.dot — regenerate with `make diagrams` -->

The nudge is a drain **imperative only** — it never carries the message payload,
which always travels via the inbox, so a nudge can't be mistaken for task content.
If the session is mid-task when the keys arrive, the nudge buffers and the message
waits in the inbox (running work is never interrupted). The router's `context_loop`
notices the inbox is still undrained and re-nudges, throttled to `backstop_seconds`
(default 5m) and only while the inbox is non-empty — so an idle, drained session
costs zero tokens. Worst-case latency for a *missed* push is one backstop interval;
a delivered push drains immediately.

A per-topic `mkdir` lock (`poll.lock.d`) keeps a push drain and a backstop re-nudge
from double-replying; a stale lock (a drain that died before releasing) self-heals
past its TTL.

## Compaction handoff (PAK transfer)

A live process can't shrink its own context, so a filling session rolls over to a
fresh replacement in the **same topic**. The router detects the trigger; the session
does the handoff.

![Compaction handoff](diagrams/compaction-handoff.svg)

<!-- Source: diagrams/compaction-handoff.dot — regenerate with `make diagrams` -->

When the gauge crosses `trigger_pct` the router nudges `/compact`. The old session
saves its working state via the **save lifecycle hook** (which must leave a handoff
at `SESS/context-restore.md`), then spawns a replacement in attach mode pointed at
the same thread. The replacement restores from that file via its **start hook**,
attaches, and signals `handoff-ready`. The `compacting.lock` / `handoff-ready`
handshake guarantees the two sessions never drain at the same time and no message is
dropped or double-answered across the cutover. The save/restore mechanism is
pluggable — see [Customizing agent behavior](../README.md#customizing-agent-behavior).
Claude Code's native auto-compact remains the ultimate backstop if the router's
detection is ever down.

## Tier-2 permission approval

Applies to **every bridge session** — a `/new` spawn or a `/telegram`-attached
session alike — under the default `spawned_mode: "risk-tiered"`. A dangerous tool
call goes to your topic as tappable buttons rather than a local prompt (uniform
whether or not someone is at the pane).

![Permission approval](diagrams/permission-approval.svg)

<!-- Source: diagrams/permission-approval.dot — regenerate with `make diagrams` -->

`telegram-permission-hook.py` is a PreToolUse hook registered in
`~/.claude/settings.json` (**NOT** `settings.local.json` — Claude Code ignores
`.local` hooks in spawned/headless sessions). It self-scopes to bridge sessions
(resolves the topic from the registry by cwd; instant no-op otherwise), and routes
`Write` / `Edit` / `NotebookEdit`, **risky** Bash, and any non-allowlisted `mcp__*`
to the topic with **Approve / Always allow / Approve+note / Deny / Deny+redirect**
buttons, blocking until you answer (auto-deny after ~28 min). A hook `deny` overrides
even a broad `Bash(*)` allow. Decisions ride a `perm-pending.json` /
`perm-answer.json` side channel (the router translates a tap or typed reply into the
answer file); the owner's free-text note/redirect is delivered to the session via the
trusted inbox.

> The default is `spawned_mode: "risk-tiered"`; `auto-allow` (fully autonomous, no
> round-trip) is opt-in. The gate is bridge-membership, not how the session started.

## Regenerating the diagrams

```bash
make diagrams   # renders docs/diagrams/*.dot → *.svg
```

Edit the `.dot` source, re-run `make diagrams`, and commit both the `.dot` and the
regenerated `.svg`.
