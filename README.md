# incoming-transmission

<img width="480" height="270" alt="Incoming transmission from... Earth?" src="https://github.com/user-attachments/assets/7975fc74-96e1-4134-b36f-543c1a874176" />


Drive your Claude Code sessions from your phone. `incoming-transmission` attaches
a live Claude Code session to a **Telegram forum topic** — whatever you type in
that topic routes to *that* running session (full context, MCP, hooks), and the
session replies in the topic. Multiple sessions attach concurrently, each its own
topic, like tabs.

Replies come from the **real session** (an in-session poll loop processes
messages), not a throwaway subagent. The session keeps all its working memory.

This is an [Invader Zim](https://en.wikipedia.org/wiki/Invader_Zim)-themed
extraction of a personal tool, published under the **megadoomer** brand (itself a
Zim reference — the Megadoomer is the giant mech the Tallest hand Zim). The core
runtime is pure Python stdlib + the Telegram Bot API — no third-party packages.
The one exception is the **optional** AskUserQuestion MCP server
(`telegram-auq-mcp.py`), which needs the `mcp` package and runs via
[`uv`](https://docs.astral.sh/uv/); you only need it if you spawn unattended
sessions with `/new` and want them to ask you questions on the phone. Everything
else — router, hooks, poll loop, wedge auto-clear — is stdlib-only.

> ## ⚠️ Status: early release
>
> The runtime is proven code in daily use, and a live deploy is byte-identical to
> what's packaged here. What's newer is the **packaged installer (`install.sh`) and
> the Claude Code wiring steps** — extracted recently and only lightly tested from a
> clean clone, so expect the occasional rough edge on first setup. Known caveats are
> listed in [Caveats](#caveats) below.

## Philosophy: transport, not workflow opinion

`incoming-transmission` is an **agnostic transport**. Its job is to move messages
between your phone and a live Claude Code session — nothing more. It deliberately
does **not** bake in any opinion about how an agent should behave, decide, or work.

By default, a bridged session behaves like normal collaborative Claude (you're in
the topic, so there's a human to talk to), and a `/new` spawn behaves like normal
Claude plus the bare minimum of transport mechanics. The only behavioral text the
bridge injects on its own is **transport-necessary mechanism** — e.g. telling an
unattended spawn that native `AskUserQuestion` is disabled (a blocking prompt has
no one to answer it in a detached pane), so it uses the Telegram AUQ path instead.

Anything beyond that — autonomy posture, "make decisions yourself", journal/save on
`/end`, restore-on-startup — is **yours to add**, via a documented seam, and is off
by default. See [Customizing agent behavior](#customizing-agent-behavior).

## The vocabulary

The metaphor maps onto the architecture, so the docs and logs use it:

| Zim term | What it is |
|----------|------------|
| **The Massive** | the router daemon — the one ship every transmission routes through |
| **Invader** | a Claude Code session, deployed to a planet (a repo / working dir) |
| **The Tallest** | you, issuing orders from orbit (your phone) |
| **Transmission** | a message to/from a session |
| **Incoming transmission** | the wake nudge when a message arrives |
| **PAK** | a session's context/memory; a compaction handoff is a *PAK transfer* |

## How it fits together

Smart router, dumb session: the Massive owns all timing (routing, push delivery,
the context gauge, and compaction triggering); a session just drains its inbox when
nudged.

```
Phone topic ──> the Massive (router daemon, sole getUpdates reader) ──> session inbox
                     │  send-keys "drain now" nudge ───────────────┐
                     │  context thread: gauge + compaction trigger   │
                     ▼                                              ▼
              status.json / pinned sticky          session drains inbox, replies
Phone topic <───────────────────────── telegram-send.sh ◀──────────┘
```

- **The Massive** (`telegram-router.py`, launchd) is the ONLY process that reads
  Telegram (getUpdates is single-consumer). It routes each message by
  `message_thread_id` to that session's `inbox.jsonl`, then **pushes** the session a
  one-line drain nudge. A separate thread computes each session's context gauge and
  triggers compaction — off the getUpdates path.
- **`/telegram`** (a Claude Code skill — see `skill/`) creates the topic, registers
  ownership, loads the bridge procedure, and does an initial drain. The session runs
  NO cron — the router pushes on arrival and re-pushes anything left undrained.
  Processing is in-session, so a message never interrupts running work.
- **Wedge auto-clear** (built into the router) is the safety net for a session
  wedged on a native prompt nobody can answer remotely. The router's context thread
  watches its own bridged panes and, if one persists on a blocking prompt, sends
  Esc (cancel-only, never approve) and tells the owner. No separate daemon.

See [docs/architecture.md](docs/architecture.md) for the full picture — diagrams of
the system topology, the message lifecycle (push delivery + backstop), the
compaction handoff (PAK transfer), and the Tier-2 permission round-trip.

## Features

- **One reader, many sessions** — single-consumer routing by topic; no polling collisions.
- **Push delivery** — the router `send-keys` a drain nudge to the session's pane
  (matched by pid identity) the moment a message arrives. The session runs no cron;
  the router backstops a missed push by re-nudging any undrained inbox (default every
  5m, only while non-empty). The nudge is a drain imperative only — payload always
  travels via the inbox.
- **Context gauge** — the router computes each session's occupancy on its own thread
  and shows it on a pinned per-topic sticky: `cwd · N msgs · ~XX% ctx`.
- **Auto-compaction (PAK transfer)** — at a context threshold the session saves
  state, spawns a fresh replacement in the same topic, and hands off, with a lock
  + handshake so no message is dropped or double-answered. *(Requires a
  save/restore mechanism — see [Customizing agent behavior](#customizing-agent-behavior).)*
- **Spawn from your phone** — `/new <dir>` launches a fresh attached session as a
  tmux tab. Add a free-text intent note (`/new <dir> <why you started it>`) and the
  fresh session starts already knowing the goal.
- **Risk-tiered permissions (Tier 2)** — **every** bridge session (a `/new` spawn
  or a `/telegram`-attached session alike) routes dangerous tool calls (Write/Edit/
  NotebookEdit, risky Bash, non-allowlisted MCP) to the topic as tap-to-approve
  buttons; safe tools run untouched. Reply **✅ Approve**, **✅ Always allow**
  (persists a narrow rule so it won't ask again), **✍️ Approve + note** / **✍️ Deny
  + redirect** (your free-text reaches the session), or **⛔ Deny**. Default posture
  is `spawned_mode: "risk-tiered"`; `auto-allow` (no round-trip) is opt-in.
- **Images** — attach a photo in the topic; the session reads it.
- **Self-healing** — the router's `context_loop` (no extra daemons) reconciles
  sessions↔topics so the two can't drift (closes orphan topics, drops dead registry
  entries), and auto-clears a session wedged on a native prompt nobody can answer
  remotely (sends Esc after a dwell, then alerts). Two opt-in janitors go further: a
  window reaper that kills confirmed-dead tmux windows, and recovery for a session
  hung on an MCP tool call that never returns — both off by default since they take
  irreversible/Esc actions.

## Requirements

- macOS (launchd). Linux support is a documented TODO — the runtime is portable
  (pure stdlib), only the installer/service layer is macOS-specific.
- Python 3 (system `/usr/bin/python3` is fine).
- [Claude Code](https://claude.com/claude-code).
- `tmux` (for `/new` spawns) and `uv` (only for the optional AskUserQuestion MCP).
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and a Telegram
  group with **topics enabled**.

## Install

```bash
git clone https://github.com/megadoomer-io/incoming-transmission
cd incoming-transmission
./install.sh
```

`install.sh` lays the runtime into `~/.telegram-bridge`, the control CLI into
`~/.local/bin`, and renders the MCP config. It does **not** start the daemon or
edit your Claude Code settings — it prints the exact next steps. Then follow the
printed checklist (bot token, username, `telegram-bridge start`, bootstrap message,
Claude Code wiring).

## Claude Code wiring

`incoming-transmission` plugs into Claude Code at three points (all optional except
the skill):

1. **The `/telegram` skill** — copy `skill/telegram-bridge/` into your Claude Code
   skills directory (`~/.claude/skills/`). This is what you invoke to attach a
   session.
2. **SessionStart hook** (recommended) — register `telegram-self-register.py` so
   each session records its transcript path (needed for the context gauge and
   auto-compaction). In `~/.claude/settings.json`:
   ```json
   { "hooks": { "SessionStart": [
       { "hooks": [ { "type": "command",
         "command": "~/.telegram-bridge/telegram-self-register.py" } ] } ] } }
   ```
3. **Tier-2 permission hook** (the tap-to-approve gate for bridge sessions) —
   register `telegram-permission-hook.py` as a PreToolUse hook **in
   `~/.claude/settings.json`**, and add the AskUserQuestion MCP from
   `~/.telegram-bridge/telegram-auq-mcp.json`:
   ```json
   { "hooks": { "PreToolUse": [
       { "matcher": "Write|Edit|NotebookEdit|Bash|mcp__.*",
         "hooks": [ { "type": "command", "timeout": 1800,
           "command": "~/.telegram-bridge/telegram-permission-hook.py" } ] } ] } }
   ```
   > **Must be `settings.json`, not `settings.local.json`.** Claude Code does not
   > load PreToolUse hooks from `.local` in spawned/headless sessions, so a `.local`
   > registration silently never fires. The hook self-scopes to bridge sessions (it
   > abstains in every non-bridge session), so registering it globally is safe.
4. **Allowlist the bridge scripts** so the drain procedure runs prompt-free (the
   session drains via fixed scripts, not ad-hoc bash). Add to `permissions.allow` in
   `~/.claude/settings.json`:
   ```json
   { "permissions": { "allow": [
       "Bash(~/.telegram-bridge/telegram-inbox.sh:*)",
       "Bash(~/.telegram-bridge/telegram-send.sh:*)"
   ] } }
   ```
   Each drain is `telegram-inbox.sh drain|ack <thread>` + `telegram-send.sh` replies —
   a fixed, allowlistable command set instead of per-call bash whose `$(...)` /
   variable expansion would dodge the allowlist and prompt you on every housekeeping
   step (especially once the session is gated).

## Commands

Typed by you in a topic:

| Command | Action |
|---------|--------|
| `/status` | session cwd + processed count |
| `/context` | live context gauge (non-destructive) |
| `/compact` | roll this session over to a fresh one now (PAK transfer) |
| `/end` | detach: close the topic, remove the registry (the session runs no cron) |
| `/dir <name\|path>` | change working dir (alias from `dir-aliases.json` or literal) |
| `/dirs` | list directory aliases |

Anything else is a task/question for the session — including other `/` text like
`/review`, which is passed through so skills can be driven from the phone.

Typed in the control group (handled by the Massive):

| Command | Action |
|---------|--------|
| `/new [dir] [intent note]` | deploy a new Invader (spawn a session) attached to its own topic; trailing text is an optional intent note the session starts with |
| `/attach [n\|window\|all]` | adopt an existing, unattached session into its own topic; no arg lists candidates (by project name), `all` binds every one |
| `/whoami` | chat_id, your user_id, current topic |
| `/sessions` | list attached sessions |

> **ℹ️ `/new` spawns are unattended but gated.** A spawned session runs with broad
> tool access, but under the default `spawned_mode: "risk-tiered"` every dangerous
> tool call (Write/Edit, risky Bash, non-allowlisted MCP) is routed to your phone
> for tap-to-approve before it runs — the same gate every bridge session uses. Set
> `spawned_mode: "auto-allow"` in `~/.telegram-bridge/permissions.json` only if you
> want a fully autonomous spawn with no round-trip.

## Customizing agent behavior

The bridge ships agnostic: by default it injects only transport mechanics and lets
the session behave like normal Claude (see
[Philosophy](#philosophy-transport-not-workflow-opinion)). Everything else — reply
style, context restore, journaling, save-on-rollover — is a **lifecycle hook** you
configure. None impose a workflow; the defaults make a fresh install behave like
plain Claude over the transport.

### Lifecycle hooks

Four small text files in `~/.telegram-bridge/lifecycle/`, each run at one moment in
a session's life. The session reads the relevant one **live** at that moment, so
edits take effect on the next attach/rollover/`/end` with no restart. The installer
seeds them from `*.example.txt` (which double as docs); lines starting with `#` and
blank lines are ignored.

| Hook | Runs when | Default (shipped) |
|------|-----------|-------------------|
| `style.txt` | every session, on attach — formats its replies | empty → normal formatting |
| `start.txt` | a **spawned** session is born (`/new` or rollover replacement) — restore context | read the handoff file if present |
| `save.txt` | a session **rolls over** (compaction handoff) — persist context | summarize state into the handoff |
| `end.txt` | a session ends (`/end`) — before detaching | empty → just detach |

`style`/`end` ship empty (pure preference). `start`/`save` ship a functional
agnostic default, because rollover continuity needs *some* save/restore — but the
mechanism is yours to swap. The one hard rule: whatever `save.txt` does, it MUST
leave the handoff at `SESS/context-restore.md`, since that's what the replacement
restores from. An interactive `/telegram` attach runs only `style` (it's already
live; restoring would clobber it).

Each `*.example.txt` includes an optional [gstack](https://garryslist.org) pointer
in a comment (e.g. `/context-save` / `/context-restore`) — uncomment to use it, or
write your own. Claude Code's **native auto-compact** remains the ultimate backstop
if you leave the handoff machinery alone.

## Known issues

Early release. Beyond the [Caveats](#caveats), these specific items are known:

- **AskUserQuestion over Telegram.** A bridge session asks you through the
  **AskUserQuestion MCP server** (`telegram-auq-mcp.py`): single-select renders
  one-tap buttons (or type a number / free text); multi-select renders a toggle
  keyboard (tap to check, then **Done**) and returns a `list[str]`. A **typed** reply
  works for both — a number picks that option, free text is an "Other" answer, `1 3`
  picks several, and input it can't read (out-of-range numbers, numbers mixed with
  stray words) gets re-asked instead of silently partial. The MCP server is **pinned
  at session start** — edits to it need a session restart (`/compact` or a fresh
  `/new`) to take effect. The legacy `telegram-askuserquestion-hook.py` is unused:
  native AskUserQuestion is disabled in spawns and PreToolUse hooks don't fire for it.
- **Permission hook must live in `settings.json`.** Claude Code does not load
  PreToolUse hooks from `settings.local.json` in spawned/headless sessions, so the
  Tier-2 gate must be registered in `~/.claude/settings.json` (see
  [Claude Code wiring](#claude-code-wiring)). A `.local` registration silently never
  fires — the symptom is gated tools getting hard-denied instead of prompting.

## Configuration

| File (`~/.telegram-bridge/`) | Purpose |
|------------------------------|---------|
| `dir-aliases.json` | short names → paths for `/dir` and `/new` (yours; gitignored) |
| `compaction.json` | `trigger_pct`, `warn_pct`, `kill_old`, `backstop_seconds`, `context_interval_seconds`, `poll_lock_ttl_seconds`, `handoff_lock_ttl_seconds`; reconciliation: `reconcile_interval_seconds`, `reconcile_grace_seconds`; wedge auto-clear: `wedge_dwell_seconds`; topic reaper: `topic_reaper_enabled` (default off — `deleteForumTopic` is irreversible), `topic_ttl_seconds` (default 7d), `topic_reap_interval_seconds`; window reaper: `window_reaper_enabled` (default off — `kill-window` is irreversible), `window_reap_grace_seconds`, `window_reap_interval_seconds`; hung-MCP recovery: `mcp_hang_recovery_enabled` (default off), `mcp_hang_dwell_seconds` |
| `permissions.json` | bridge-session permission posture: `risk-tiered` (default), `ask`, or `auto-allow` |
| `spawned-allow.json` | persisted "always allow" rules (grown by the hook on ✅ Always allow; auditable JSON) |
| `lifecycle/{style,start,save,end}.txt` | per-event behavior hooks (read live; gitignored). See [Customizing agent behavior](#customizing-agent-behavior) |

Identity (owner username, user id, chat id) is **not** in any file — it's captured
into `~/.local/state/telegram-bridge/state.json` on first contact with the bot.

| Env var | Purpose |
|---------|---------|
| `TELEGRAM_BRIDGE_BOT_TOKEN` | bot token (required; never passed on argv) |
| `TELEGRAM_BRIDGE_ALLOWED_USERNAME` | locks the bridge to your Telegram handle |
| `INCOMING_TRANSMISSION_PREFIX` | install dir (default `~/.telegram-bridge`) |
| `TELEGRAM_BRIDGE_TMUX` / `_CLAUDE` | override tmux / claude binary paths |

## Daemon control

```bash
telegram-bridge start|stop|restart|status|log
```

## Security notes

- **Owner-locked**: only the bootstrapped Telegram user is accepted; others are
  dropped. The daemon **refuses to start** unless an owner is configured — either
  `TELEGRAM_BRIDGE_ALLOWED_USERNAME` is set, or `state.json` already has an owner.
  This narrows the bootstrap-hijack window (where the first person to message the
  bot becomes the owner) to "you set your handle, then you message the bot".
- The bot token is read from the environment, never placed on a command line.
- **Token in the plist**: `telegram-bridge start` renders the bot token into the
  launchd plist under `~/Library/LaunchAgents/`. That file is
  `chmod 600` (owner-only) right after rendering so the token isn't group/world
  readable. A stronger option — keeping the token out of the plist entirely (e.g.
  sourcing it from the Keychain or a separate 600 env file the daemon reads at
  startup) — is a future improvement; `chmod 600` is the accepted minimum for now.
- Bridge sessions are **gated by default** (`spawned_mode: "risk-tiered"`): every
  dangerous tool call (Write/Edit, risky Bash, non-allowlisted MCP) is routed to
  your phone for tap-to-approve before it runs — the same model for `/new` spawns and
  `/telegram`-attached sessions. `auto-allow` (fully autonomous, no round-trip) is
  opt-in; set it only if you accept that a phone message can then drive a session
  that edits files and runs tools unattended.

## Caveats

- **Installer lightly tested.** The runtime is proven and in daily use; the
  packaged `install.sh` and wiring steps are newly extracted and not yet run from a
  clean clone by a third party. See the status banner up top.
- **macOS only (for now).** The daemon is a launchd service and the installer
  renders a launchd plist. The Python runtime is portable; a Linux
  (systemd) service layer is a TODO.
- **Claude Code specific.** Sessions are driven by `send-keys` into a Claude Code
  TUI and rely on Claude Code skills, hooks, and MCP. This is not a generic
  LLM/agent bridge.
- **gstack defaults (configurable).** The lifecycle hooks ship a functional default
  for the save/restore that rollover continuity needs; each example documents the
  gstack version (`/context-save` / `/context-restore` / `/track`). Style,
  restore-on-start, and journal-on-`/end` are all hooks, none baked into behavior.
  See [Customizing agent behavior](#customizing-agent-behavior).
- **Telegram setup gotchas.** The group MUST have **topics/forum mode enabled**,
  and the bot MUST be a group **admin** to create topics and read messages.
- **Elevated autonomy.** Bridge sessions run with broad tool access. By default
  (`spawned_mode: "risk-tiered"`) dangerous calls are gated to your phone for
  approval; set `auto-allow` only if you want no round-trip. See
  [Security notes](#security-notes).
- **Single owner.** Bootstraps to one Telegram user; not multi-user.

## License

Beerware (Revision 42). See [LICENSE](LICENSE).
