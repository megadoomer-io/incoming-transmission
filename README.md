# incoming-transmission

> *"Incoming transmission from the Tallest."*

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
else — router, hooks, poll loop, watchdog — is stdlib-only.

> ## ⚠️ Status: pre-release, UNTESTED
>
> This is a fresh extraction. The runtime is the proven code from a working
> private setup, but the **packaged repo has not been installed or run
> end-to-end yet**. Treat it as a preview — read it, clone it, but expect rough
> edges if you run it before the first tested release (v0.1). **Install at your
> own risk until then.** Known caveats are listed in [Caveats](#caveats) below.

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
- **The wedge watchdog** (`telegram-watchdog.py`, a separate launchd timer) is the
  safety net for a session wedged on an interactive prompt nobody can answer.

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
  tmux tab.
- **Unattended permissions (Tier 2)** — optional tap-to-approve: with
  `spawned_mode: "ask"`, spawned sessions route Write/Edit/MCP approvals to the
  topic as buttons. **The shipped default is `auto-allow` (fully autonomous, no
  approval round-trip)** — see the warning under
  [Spawning new sessions](#spawning-new-sessions--new) and
  [Known issues](#known-issues).
- **Images** — attach a photo in the topic; the session reads it.

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
3. **Tier-2 permission hook** (only if you use `/new` to spawn unattended sessions)
   — register `telegram-permission-hook.py` as a PreToolUse hook in
   `settings.local.json` and add the AskUserQuestion MCP from
   `~/.telegram-bridge/telegram-auq-mcp.json`. See that file and the hook's header
   for the matcher.

## Commands

Typed by you in a topic:

| Command | Action |
|---------|--------|
| `/status` | session cwd + processed count |
| `/context` | live context gauge (non-destructive) |
| `/compact` | roll this session over to a fresh one now (PAK transfer) |
| `/end` | detach: close the topic, remove the cron + registry |
| `/dir <name\|path>` | change working dir (alias from `dir-aliases.json` or literal) |
| `/dirs` | list directory aliases |

Anything else is a task/question for the session — including other `/` text like
`/review`, which is passed through so skills can be driven from the phone.

Typed in the control group (handled by the Massive):

| Command | Action |
|---------|--------|
| `/new [dir]` | deploy a new Invader (spawn a session) attached to its own topic |
| `/whoami` | chat_id, your user_id, current topic |
| `/sessions` | list attached sessions |

> **⚠️ `/new` spawns are autonomous by default.** A spawned session ships with
> `spawned_mode: "auto-allow"`: it runs Write/Edit/MCP tools and answers its own
> questions with **no tap-to-approve round-trip**. A phone message can drive a
> session that edits files and runs commands unattended. Set `spawned_mode: "ask"`
> in `~/.telegram-bridge/permissions.json` for tap-to-approve. See
> [Known issues](#known-issues) — whether `auto-allow` should be the default is a
> pending decision.

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

This tool was extracted from a setup that uses [gstack](https://garryslist.org)
skills, so each `*.example.txt` documents the gstack version as a commented
example: `start` → `/context-restore`, `save`/`end` → `/track` + `/context-save`.
Uncomment to use them, or write your own. Claude Code's **native auto-compact**
remains the ultimate backstop if you leave the handoff machinery alone.

## Known issues

Pre-release, untested. Beyond the [Caveats](#caveats), these specific items are
known and pending the first end-to-end test:

- **Spawned-session default is fully autonomous.** `permissions.json` ships
  `spawned_mode: "auto-allow"`: a `/new` session runs Write/Edit/MCP and answers its
  own questions with no approval round-trip. Tap-to-approve requires
  `spawned_mode: "ask"`. Whether `auto-allow` should be the default is an open
  decision — it ships this way because the approval round-trip is not yet validated.
- **AskUserQuestion has one supported path and two unverified ones.** The intended,
  supported channel for an unattended spawn to ask you a question is the
  **AskUserQuestion MCP server** (`telegram-auq-mcp.py`), where a button **tap** is
  delivered to the session via `auq-answer.json`. Two alternates are present but
  unverified pending a runtime test: (a) a **typed** (non-button) answer to an MCP
  question lands in the inbox, not `auq-answer.json`, so the MCP server doesn't see
  it — use the buttons; (b) `telegram-askuserquestion-hook.py` is a PreToolUse-hook
  variant that keys on a `via=="callback-auq"` marker the router doesn't currently
  emit. Prefer the MCP server + button taps until the alternates are tested.

## Configuration

| File (`~/.telegram-bridge/`) | Purpose |
|------------------------------|---------|
| `dir-aliases.json` | short names → paths for `/dir` and `/new` (yours; gitignored) |
| `compaction.json` | `trigger_pct`, `warn_pct`, `kill_old`, `backstop_seconds`, `context_interval_seconds` |
| `permissions.json` | spawned-session permission mode (`auto-allow` default, or `ask`) |
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
telegram-bridge watchdog-start|watchdog-stop   # optional wedge watchdog
```

## Security notes

- **Owner-locked**: only the bootstrapped Telegram user is accepted; others are
  dropped. The daemon **refuses to start** unless an owner is configured — either
  `TELEGRAM_BRIDGE_ALLOWED_USERNAME` is set, or `state.json` already has an owner.
  This narrows the bootstrap-hijack window (where the first person to message the
  bot becomes the owner) to "you set your handle, then you message the bot".
- The bot token is read from the environment, never placed on a command line.
- **Token in the plist**: `telegram-bridge start` / `watchdog-start` render the bot
  token into the launchd plists under `~/Library/LaunchAgents/`. Those files are
  `chmod 600` (owner-only) right after rendering so the token isn't group/world
  readable. A stronger option — keeping the token out of the plist entirely (e.g.
  sourcing it from the Keychain or a separate 600 env file the daemon reads at
  startup) — is a future improvement; `chmod 600` is the accepted minimum for now.
- Spawned sessions run **unattended and, by default, fully autonomous**
  (`spawned_mode: "auto-allow"`): a phone message can drive a session that edits
  files and runs tools with no approval round-trip. Only enable `/new` if you accept
  that. Set `spawned_mode: "ask"` in `permissions.json` to route the riskier
  approvals (Write/Edit/non-allowlisted MCP) back to your phone as tap-to-approve.

## Caveats

- **Untested end-to-end.** See the status banner up top. The runtime is proven;
  the packaged install path is not yet validated.
- **macOS only (for now).** The daemon + watchdog are launchd services and the
  installer renders launchd plists. The Python runtime is portable; a Linux
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
- **Elevated autonomy (default).** Spawned sessions (`/new`) run with broad tool
  access and, by default (`spawned_mode: "auto-allow"`), no approval round-trip. A
  phone message can drive a session that edits files and runs commands. Only enable
  `/new` if you accept that; set `spawned_mode: "ask"` for tap-to-approve. See [Security notes](#security-notes) and
  [Known issues](#known-issues).
- **Single owner.** Bootstraps to one Telegram user; not multi-user.

## License

Beerware (Revision 42). See [LICENSE](LICENSE).
