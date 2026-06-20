---
name: telegram-bridge
description: Open a Telegram forum topic bound to this Claude session — messages typed there route to this live session, which replies in-topic with full context.
version: 1.7.0
---

# /telegram

Attach the current Claude session to a Telegram **forum topic**. Whatever you type
in that topic on your phone routes to *this* live session (full context, MCP, hooks),
and the session replies in the topic. Multiple sessions can attach concurrently —
each gets its own topic in the same group, like tabs.

Replies come from the **real session** (a session-scoped cron processes messages
in-session), not a `claude -p --bare` subagent.

## Architecture (how the pieces fit)

```
Phone topic ──> router daemon (sole getUpdates reader) ──> this session's inbox.jsonl
                                                                  │
                          session-scoped cron (idle) reads inbox ─┘
                                  │ processes in-session, replies
Phone topic <── telegram-send.sh ─┘
```

- **Router daemon** (`telegram-bridge`, launchd) is the ONLY process that reads
  Telegram (getUpdates is single-consumer). It routes each message by
  `message_thread_id` to `/tmp/claude-telegram/sessions/<thread_id>/inbox.jsonl`.
- **This skill** creates the topic, registers ownership, and starts a session-scoped
  `CronCreate` poll that drains the inbox and replies. The cron fires on idle, so an
  incoming message waits until the session finishes its current work (it won't
  interrupt a running task).
- **Watchdog daemon** (`telegram-watchdog`, a separate launchd timer) is the safety
  net for the failure the poll cron *can't* catch: a session wedged on an
  interactive prompt nobody can answer. A blocked session can't run its own poll, so
  an independent watcher scans the tmux panes and alerts the owner. See "Wedge
  watchdog" below.

## When to Use

- You want to drive this session from your phone while away from the keyboard.
- The user says "start telegram", "/telegram", or "attach telegram".

## Prerequisites

- Daemon installed and running: `telegram-bridge status` shows `running` with a
  non-null `chat_id`. If `chat_id` is null, the bridge hasn't been bootstrapped —
  send `@mcd_claude_bot` any message once (in the control group) so the daemon
  captures the chat, then retry.
- `TELEGRAM_BRIDGE_BOT_TOKEN` in the environment (from `dotfiles-secrets sync`).

## Setup

### Step 1: Confirm the daemon is up and bootstrapped

```bash
~/.local/bin/telegram-bridge status
```

Read `~/.local/state/telegram-bridge/state.json` for `chat_id`. If it's null, stop
and tell the user to message the bot once to bootstrap. If the daemon isn't running,
offer to start it: `telegram-bridge start`.

### Step 2: Get a forum topic for this session

**First check for compaction-replacement (attach) mode.** If the env var
`TELEGRAM_BRIDGE_ATTACH_THREAD` is set, this session is a replacement for a topic
whose previous session ran out of context (see "Auto-compaction" below). In that
case **do NOT create a topic** — reuse the existing one:

```bash
CHAT_ID=$(python3 -c "import json;print(json.load(open('$HOME/.local/state/telegram-bridge/state.json'))['chat_id'])")
if [ -n "${TELEGRAM_BRIDGE_ATTACH_THREAD:-}" ]; then
  THREAD_ID="$TELEGRAM_BRIDGE_ATTACH_THREAD"
  echo "attach mode: reusing existing topic $THREAD_ID"
fi
```

Set `THREAD_ID` to that value and skip the `createForumTopic` call below.

**Otherwise (normal attach), create a fresh topic** named from the repo + branch
(fall back to the cwd basename):

```bash
NAME="$(basename "$PWD")@$(git branch --show-current 2>/dev/null || echo nogit)"
python3 - "$CHAT_ID" "$NAME" <<'PY'
import json, os, sys, urllib.request
tok = os.environ["TELEGRAM_BRIDGE_BOT_TOKEN"]
chat, name = sys.argv[1], sys.argv[2]
data = json.dumps({"chat_id": int(chat), "name": name}).encode()
req = urllib.request.Request(
    f"https://api.telegram.org/bot{tok}/createForumTopic",
    data=data, headers={"Content-Type": "application/json"})
r = json.load(urllib.request.urlopen(req, timeout=30))
print(r["result"]["message_thread_id"] if r.get("ok") else "ERROR: %s" % r)
PY
```

Capture the printed `message_thread_id` as `THREAD_ID`.

### Step 3: Register ownership (with this session's transcript)

Write `~/.local/state/telegram-bridge/registry/<THREAD_ID>.json` so the daemon routes
this topic's messages to this session's inbox. The registry also records this
session's **transcript path** so the poll cron can compute context occupancy (for the
status sticky + auto-compaction).

Discovering your own transcript: the `SessionStart` hook
(`telegram-self-register.py`) wrote `~/.local/state/telegram-bridge/self/<id>.json`
records of every live session's `{transcript_path, cwd}`. Pick the newest-mtime
transcript among records matching this cwd — since this session's transcript is being
written *right now* (you're running `/telegram`), it wins:

```bash
THREAD_ID=<from step 2>
INBOX="/tmp/claude-telegram/sessions/$THREAD_ID/inbox.jsonl"
mkdir -p "$(dirname "$INBOX")"
REG="$HOME/.local/state/telegram-bridge/registry/$THREAD_ID.json"
python3 - "$THREAD_ID" "$INBOX" "$PWD" <<'PY' > "$REG"
import json, sys, os, glob, datetime, subprocess

thread_id, inbox, cwd = sys.argv[1], sys.argv[2], sys.argv[3]

# Find this session's own transcript: newest-mtime self-record matching cwd.
selfdir = os.path.expanduser("~/.local/state/telegram-bridge/self")
transcript = ""
best_m = -1.0
for f in glob.glob(os.path.join(selfdir, "*.json")):
    try:
        rec = json.load(open(f))
    except Exception:
        continue
    if rec.get("cwd") != cwd:
        continue
    tp = rec.get("transcript_path")
    if not tp or not os.path.exists(tp):
        continue
    m = os.path.getmtime(tp)
    if m > best_m:
        best_m, transcript = m, tp

# PID of the owning `claude` process (walk up the tree). Lets consumers that
# share a cwd (the AskUserQuestion MCP server, permission hook) match THIS
# session's topic exactly instead of guessing among same-cwd registry entries.
def claude_pid():
    pid = os.getpid()
    for _ in range(12):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-c", "-o", "comm=,ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return None
        if not out:
            return None
        toks = out.rsplit(None, 1)
        comm = toks[0] if toks else ""
        if "claude" in comm.lower():
            return pid
        try:
            pid = int(toks[1]) if len(toks) > 1 else -1
        except ValueError:
            return None
    return None

print(json.dumps({
  "thread_id": int(thread_id),
  "inbox_path": inbox,
  "cwd": cwd,
  "transcript_path": transcript,   # "" if discovery failed; poll falls back to skip
  "claude_pid": claude_pid(),      # exact session match for same-cwd disambiguation
  "context": "session attached",
  "registered_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}, indent=2))
PY
```

If `transcript_path` comes out empty (rare — e.g. the SessionStart hook didn't run),
the context gauge and auto-compaction simply stay dormant for this topic; everything
else works. You can re-run this block to backfill it later.

### Step 3.5: (attach mode only) complete the handoff handshake

**Skip this step entirely unless `TELEGRAM_BRIDGE_ATTACH_THREAD` is set** (normal
attach goes straight to Step 4). If this session is a compaction replacement:

```bash
SESS="/tmp/claude-telegram/sessions/$THREAD_ID"
touch "$SESS/handoff-ready"          # tell the old session we've restored + attached
# Wait for the old session to release the handshake lock (it removes it once it
# sees handoff-ready, after deleting its own poll cron). If there's no lock, proceed.
for _ in $(seq 1 150); do            # ~5 min at 2s
  [ -f "$SESS/compacting.lock" ] || break
  sleep 2
done
```

Only after the lock is gone (or was never present) do you start your poll cron in
Step 4 — this guarantees the old and new pollers never run at the same time.

### Step 4: Start the session-scoped poll (CronCreate)

The poll prompt is maintained as a single-source template at
`~/.telegram-bridge/poll-prompt.tmpl` (the prompt's own backoff step re-renders
from it, so it must not be duplicated inline). Render it for THIS session, then
create the cron with the rendered text:

1. Render the prompt (substitutes this session's IDs/paths):
   ```bash
   ~/.telegram-bridge/poll-render.sh "$THREAD_ID" "$CHAT_ID" "$INBOX" "$PWD"
   ```
   Capture stdout — that is the prompt.
2. `CronCreate` a recurring job at the **fastest rung** (every minute, cron
   `* * * * *`) with that prompt (`recurring: true`, `durable: false`). It runs
   **in this session**, so replies carry full context. The cron fires on idle, so
   an incoming message waits until the session finishes its current work.
3. Record poll state so backoff + the handoff can find the cron (substitute the
   new cron id returned by CronCreate):
   ```bash
   SESS=/tmp/claude-telegram/sessions/$THREAD_ID
   echo "<new cron id>" > "$SESS/poll.cron.id"
   echo 0 > "$SESS/idle.count"
   echo 0 > "$SESS/poll.level"
   ```

The cron then self-adjusts its interval (see "Context gauge, backoff &
auto-compaction" below): fast (60s) while a conversation is active, ramping to a
slow cap (default 30m) after sustained idleness, so an idle session no longer
climbs to auto-compaction purely from poll-prompt re-injection.

Report to the user: the topic name, that it's live (with the context gauge,
adaptive backoff, and auto-compaction at the configured threshold), and to type in
that topic on their phone.

## Commands (typed by the user in the topic)

| Command | Action |
|---------|--------|
| `/status` | Show this session's cwd and processed count |
| `/context` | Report the live context gauge (pct, tokens, window, msgs) — non-destructive |
| `/compact` | Roll this session over to a fresh one now, preserving working state |
| `/end` | Detach: `/track` + `/context-save` first (preserve + checkpoint), then close the topic, remove the cron + registry |
| `/dir <name\|path>` | Change working dir. `<name>` is resolved against `~/.telegram-bridge/dir-aliases.json` (case-insensitive); otherwise treated as a literal path |
| `/dirs` | List the available directory aliases |

The `!`-prefixed forms (`!status`, `!context`, `!compact`, `!end`, `!dir`,
`!dirs`) still work as a legacy alias, but `/` is preferred — it's easier to type
on a phone and the bot registers these names with Telegram (`setMyCommands`), so
the `/` menu offers them as tappable suggestions.

In a group, tapping a command from Telegram's slash-menu appends the bot handle
(e.g. `/end@mcd_claude_bot`). The router's `normalize_command()` strips that
`@botname` suffix from the leading command token before routing, so the suffixed
and bare forms are equivalent for both session-level and daemon-level commands.
This also means already-running sessions get the fix for free — they read the
normalized text from their inbox, no cron re-creation needed.

Anything else is treated as a task/question for this session — including other
`/`-prefixed text like `/track` or `/ship`, which is passed through so Claude
skills can be invoked from the phone. Only `status`, `context`, `compact`, `end`,
`dir`, and `dirs` are reserved as bridge commands.

### Sending images

Attach a photo or image file in the topic (with an optional caption). The router
downloads it to the session's inbox (`/tmp/claude-telegram/sessions/<thread>/images/`)
and records an `image_path` on the message; the session `Read`s the image and
treats the caption as the instruction. Handy for showing an error or UI state
from your phone instead of typing it out.

## Context gauge, backoff, wake & auto-compaction

The phone can't see Claude Code's status bar, so the bridge surfaces context
occupancy two ways, backs off polling when the session is idle, and rolls a
filling session over automatically.

**Status sticky.** Each topic has a pinned message the router keeps updated:
`cwd · N msgs · ~XX% ctx · updated HH:MM`, with a ⚠️ once context passes the warn
threshold. The numbers come from the session's own transcript (the poll cron runs
`telegram-context.py` each tick and writes `status.json`; the router reads it and
edits the pin). Window detection is automatic — the Opus 1m variant gets a
1,000,000-token window, everything else 200,000, with a fallback to 1m if observed
tokens already exceed 200k (so a session whose model id lacks the `[1m]` suffix
isn't mis-scored).

**Backoff.** An idle session is not free: each poll tick re-injects the poll
prompt into context, so fixed 1-minute polling climbs to the auto-compaction
trigger in ~3 hours even with an empty inbox. The poll prompt is self-rescheduling
(section D): it tracks consecutive idle ticks in `SESS/idle.count` and walks an
interval ladder (`polling.idle_intervals_seconds`, default `[60,120,300,1800]` =
1/2/5/30m, advancing one rung every `polling.ticks_per_rung` idle ticks, both in
`compaction.json`). When a rung changes it CronCreates a new cron at the new
interval, records the id in `SESS/poll.cron.id`, then CronDeletes the old one
(create-before-delete, so there's never a gap). Any tick that drains a message
resets to the fastest rung, so an active conversation stays at 60s. Worst-case
latency for the first message after deep idle is the cap (30m) when wake is
unavailable; with wake (below) the message drains immediately.

**Wake.** Backoff trades latency for cost; wake buys the latency back. When the
router delivers a message to a topic, it finds that session's tmux pane by pid
identity (the registry `claude_pid` equals the pane's `#{pane_pid}` or an ancestor
of it) and `send-keys` a one-line nudge telling the session to run a full poll
tick now. The session drains + replies at once and backoff resets, so a backed-off
cron never sits on a message. Wake is best-effort and additive: it runs in the
router only AFTER the message is in the inbox and is wrapped so any failure is
logged and ignored, so it can never drop or delay delivery. The pane match is by
pid identity (never name or cwd), so a nudge can only reach the exact session that
owns the topic; a topic with no matching pane (a non-tmux session) simply isn't
woken and backoff's cap bounds its latency. Lives in `telegram-router.py`
(`wake_session`, `_pane_for_claude_pid`).

**Auto-compaction (process handoff).** A live process can't shrink its own context,
so at the trigger threshold the session does a *handoff*: `/context-save` → spawn a
fresh replacement in the SAME topic (attach mode) → the replacement `/context-restore`s,
attaches, and takes over → the old session stops. A per-topic mkdir lock plus a
`compacting.lock` / `handoff-ready` handshake guarantee the two pollers never overlap
and no message is dropped or double-answered across the cutover. This uses the gstack
`/context-save` + `/context-restore` skills (controllable and near-lossless), with
Claude Code's native auto-compact still underneath as a safety net.

**Tuning** (`~/.telegram-bridge/compaction.json`, read live — no restart):

| Field | Default | Meaning |
|-------|---------|---------|
| `trigger_pct` | `0.85` | Auto-compact at this fraction of the context window |
| `warn_pct` | `0.75` | Show ⚠️ on the sticky at this fraction |
| `kill_old` | `false` | After handoff, `false` renames the old tmux window `DEAD - <name>` (find + clean up by hand); `true` kills it |

Trigger it manually with `/compact` from the topic; check the gauge any time with
`/context`.

## Wedge watchdog

The poll cron rescues a session only when the session is *idle*. It cannot rescue a
session that is **wedged on an interactive prompt** — a native permission prompt
(from a pre-fix session or a tool the Tier-2 hook doesn't cover), an `ssh-add`
passphrase, a "trust this folder" dialog, or a native AskUserQuestion fallback.
While blocked at the prompt the session never goes idle, so its own cron never
fires. The session cannot notice its own wedge.

`telegram-watchdog.py` is the out-of-band watcher. A separate launchd timer
(`com.telegram.watchdog`, `StartInterval` 60s) scans every pane in the `claude`
tmux session, matches the captured tail against wedge signatures, and — if the same
prompt persists past the dwell window (`TELEGRAM_WATCHDOG_DWELL_S`, default 180s) —
posts a one-time `⚠️` alert. It maps the pane to its bridge topic (by the registry's
`claude_pid`) and alerts there; an unattached pane alerts the General topic. The
alert names the window, pane, elapsed time, and the prompt's first line, and tells
you to attach and answer.

- **Alert-only.** v1 never sends keystrokes — a blind keypress into a pane is too
  risky to do unattended. Auto-deny could be a future opt-in.
- **Dwell-gated + once-per-episode.** A human mid-thought at a prompt isn't flagged
  (must persist past the dwell), and a given wedge alerts once until it clears. State
  lives in `~/.local/state/telegram-bridge/watchdog-state.json`.
- **Off by removing the launchd job** (see Daemon control). The script is harmless
  until the timer loads it; `--dry-run` scans and prints without sending.

Signatures matched: `Do you want to proceed?`, `Do you want to make this edit`,
`Do you want to create`, `Do you trust the files`, `Enter passphrase`, and the
generic Claude selection menu (`❯ 1.` choice line plus the `Esc to cancel` footer).

## Setup commands (typed in the control group, handled by the daemon)

| Command | Action |
|---------|--------|
| `/new [dir]` | Spawn a NEW Claude session in `dir` (default: home), auto-attached to its own topic |
| `/whoami` | Report chat_id, your user_id, current topic id |
| `/sessions` | List attached sessions/topics |
| `/help` | Daemon help |

## Spawning new sessions (`/new`)

Type `/new ~/src/github.com/org/repo` in the control group (General). The `dir`
also accepts a short alias from `~/.telegram-bridge/dir-aliases.json` (e.g.
`/new kdrift`, `/new argo`), resolved case-insensitively. The daemon runs
`telegram-spawn.sh`, which launches a fresh `claude` as a **window in a shared
`claude` tmux session** and feeds it an initial prompt to self-attach via
`/telegram`. The new session opens its own topic (named from cwd/branch) and
posts there once ready.

The spawn prompt also has the new session invoke `/context-restore` first, so a
`/new <repo>` starts already loaded with that project's last saved checkpoint (it
labels the age; "no saved context" is a fine no-op for a fresh repo). This pairs
with `/end` running `/track` + `/context-save` on the way out: end a session and
its work is journaled and checkpointed, then the next `/new` for that project
picks up where it left off. Interactive (non-bridge) sessions get the same nudge
from the global `gstack-context-restore-nudge` SessionStart hook.

Key design points:

- **One shared session, one window (tab) per spawn**: every `/new` adds a window
  named for its repo to the `claude` tmux session. `tmux attach -t claude` shows
  them all as tabs in the status bar; cycle with prefix + `Tab`/`Shift-Tab` (or
  the default `n`/`p` and `0-9`). Kill a tab with prefix + `&`. The tmux server
  is independent of the router daemon, so spawns survive a daemon restart.
- **tmux, not a login shell**: claude is launched directly as the window command
  (`/opt/homebrew/bin/claude "<prompt>"`), bypassing the interactive shell
  profile. A login shell would block forever on the `ssh-add` passphrase prompt
  during init (nobody is at the pane to type it).
- **Tab names stick**: the `tmux` stow package sets `automatic-rename off` so the
  per-repo window names aren't clobbered by the running process name.
- **Env injection**: the daemon runs under launchd with a minimal env, so
  `telegram-spawn.sh` injects `TELEGRAM_BRIDGE_BOT_TOKEN`, `PATH`, `HOME`, and
  `TELEGRAM_BRIDGE_SPAWNED=1` into the pane.

## Permissions in spawned sessions (Tier 2)

A spawned session runs unattended, so a local permission prompt would hang the
pane. `telegram-permission-hook.py` (a PreToolUse hook wired in
`~/.claude/settings.local.json`, NOT the shared `settings.json`) routes approval
requests for `Write`/`Edit`/`NotebookEdit`/`WebFetch` and any non-allowlisted
MCP tool (`mcp__*`) to the session's topic: it posts the change preview with
inline **Approve / Deny** buttons (one tap on the phone; a typed `y`/`n` still
works as a fallback), blocks until you answer (auto-deny after 240s), then allows
or denies. Bash and reads are already covered by the `Bash(*)` allowlist with GIR
as the floor, so they don't prompt.

MCP coverage reuses your existing settings allowlist as the single source of
truth. The hook reads the `mcp__*` allow entries from `~/.claude/settings.json`
and `settings.local.json`; an allowlisted MCP tool (e.g.
`mcp__plugin_github_github__*`) is **explicitly allowed by the hook**, and only
the non-allowlisted ones get routed to Telegram. (The hook does not rely on
Claude Code's own permission flow to auto-allow them: in a spawned session an
allowlisted MCP tool like `mcp__telegram__AskUserQuestion` still hit the native
permission prompt, which hangs a detached pane, so the hook returns the allow
decision itself.) Project-level
`.claude/settings*.json` are deliberately ignored — a checked-in repo allowlist
must not silently widen what an unattended session can run.

The hook is gated on `TELEGRAM_BRIDGE_SPAWNED=1` and a
`Write|Edit|NotebookEdit|WebFetch|mcp__.*` matcher — it is a no-op (instant exit
0) for every normal session. It reuses the registry (cwd→topic), `state.json`
(chat_id), and the session inbox for the round-trip; a tapped Approve/Deny button
is delivered by the router (via `callback_query`) as a synthetic `y`/`n` line into
that inbox, which the blocked hook consumes exactly like a typed reply. While it
blocks, the session is busy so its poll cron doesn't contend.

## Known Limitations

- **Idle-only**: messages are processed when the session is idle (CronCreate fires on
  idle), so a message sent mid-task waits until the task finishes. This is intentional
  — it won't interrupt running work.
- **Session-scoped cron**: the poll dies when this Claude session exits. The daemon
  keeps running (launchd), but reattaching is a fresh `/telegram`.
- **Owner-locked**: only the bootstrapped owner (your Telegram user) is accepted; other
  senders are dropped by the daemon.
- **Plain text replies**: v1 sends plain text, chunked at 4096 chars. Rich
  formatting (HTML/code blocks) is a later enhancement.
- **Unattached topics**: messages to a topic with no attached session get a "no
  session" reply. Use `/new` to spawn a fresh attached session.
- **Tier-2 coverage**: `Write`/`Edit`/`NotebookEdit`/`WebFetch` and any
  non-allowlisted `mcp__*` tool route to Telegram for approval. Allowlisted MCP
  tools auto-run without a round-trip. A non-MCP tool outside the `Bash(*)` /
  read allowlist (rare) would still prompt and hang.
- **Spawn reply consumption**: the permission hook treats the next inbox message
  as the y/n answer and advances `read.offset` past it. A task message sent in
  the narrow window while an approval is pending is consumed as the answer.
- **Handoff window retirement is tmux-only**: the `DEAD - <name>` rename / kill on
  compaction applies to spawned sessions (which run in the shared `claude` tmux
  session). A manually-attached session in a plain terminal still saves, spawns the
  replacement, and hands off, but the old terminal session is left for you to `/end`
  or close yourself.

## Daemon control

| Command | Action |
|---------|--------|
| `telegram-bridge start` | Start the router daemon |
| `telegram-bridge stop` | Stop it |
| `telegram-bridge status` | Status, owner, chat id, attached topics |
| `telegram-bridge log` | Tail the router log |
| `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.telegram.watchdog.plist` | Start the wedge watchdog |
| `launchctl bootout gui/$(id -u)/com.telegram.watchdog` | Stop the wedge watchdog |
| `telegram-watchdog.py --dry-run` | Scan panes now, print findings, send nothing |
