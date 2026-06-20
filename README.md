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
Zim reference — the Megadoomer is the giant mech the Tallest hand Zim). Zero
runtime dependencies: pure Python stdlib + the Telegram Bot API.

> ## ⚠️ Status: pre-release, UNTESTED
>
> This is a fresh extraction. The runtime is the proven code from a working
> private setup, but the **packaged repo has not been installed or run
> end-to-end yet**. Treat it as a preview — read it, clone it, but expect rough
> edges if you run it before the first tested release (v0.1). **Install at your
> own risk until then.** Known caveats are listed in [Caveats](#caveats) below.

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

```
Phone topic ──> the Massive (router daemon, sole getUpdates reader) ──> session inbox
                                                                            │
                          in-session poll loop (fires on idle) reads inbox ─┘
                                  │ processes in-session, replies
Phone topic <── telegram-send.sh ─┘
```

- **The Massive** (`telegram-router.py`, launchd) is the ONLY process that reads
  Telegram (getUpdates is single-consumer). It routes each message by
  `message_thread_id` to that session's `inbox.jsonl`.
- **`/telegram`** (a Claude Code skill — see `skill/`) creates the topic,
  registers ownership, and starts an in-session poll (a session-scoped cron) that
  drains the inbox and replies. The poll fires on idle, so a message never
  interrupts running work.
- **The wedge watchdog** (`telegram-watchdog.py`, a separate launchd timer) is the
  safety net for a session wedged on an interactive prompt nobody can answer.

## Features

- **One reader, many sessions** — single-consumer routing by topic; no polling collisions.
- **Adaptive backoff** — polls every 60s while a conversation is active, ramps to
  a slow cap when idle so an idle session doesn't burn context. A router-side
  *wake* nudge pulls a new message immediately, so backoff never sits on a message.
- **Context gauge** — a pinned per-topic sticky shows `cwd · N msgs · ~XX% ctx`.
- **Auto-compaction (PAK transfer)** — at a context threshold the session saves
  state, spawns a fresh replacement in the same topic, and hands off, with a lock
  + handshake so no message is dropped or double-answered. *(Requires a
  save/restore skill — see "Decoupling from gstack" below.)*
- **Spawn from your phone** — `/new <dir>` launches a fresh attached session as a
  tmux tab.
- **Unattended permissions (Tier 2)** — spawned sessions route Write/Edit/MCP
  approvals to the topic as tap-to-approve buttons.
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

## Decoupling from gstack

This tool was extracted from a setup that uses
[gstack](https://garryslist.org) skills for context save/restore. Three touchpoints
reference them; a vanilla install must adjust them. They are all in
`runtime/poll-prompt.tmpl` and `runtime/telegram-spawn.sh`:

1. **`/end`** (`poll-prompt.tmpl`) calls `/track` then `/context-save` before
   detaching. Without those skills, drop those two calls — `/end` just closes the
   topic.
2. **Auto-compaction handoff** (`poll-prompt.tmpl`, COMPACTION HANDOFF block) calls
   `/context-save` to seed the replacement and the replacement runs
   `/context-restore`. Without a save/restore skill, either:
   - disable the process handoff and rely on Claude Code's **native auto-compact**
     (set `trigger_pct` high in `compaction.json` and treat the section as
     warn-only), or
   - point those two calls at your own save/restore commands.
3. **Spawn prompt** (`telegram-spawn.sh`) tells new sessions to run
   `/context-restore` first. Harmless if the skill is absent (it just no-ops), but
   you can remove the line.

Making save/restore fully config-driven (a `context_skill` knob) is on the roadmap.

## Configuration

| File (`~/.telegram-bridge/`) | Purpose |
|------------------------------|---------|
| `dir-aliases.json` | short names → paths for `/dir` and `/new` (yours; gitignored) |
| `compaction.json` | `trigger_pct`, `warn_pct`, `kill_old`, polling ladder |
| `permissions.json` | spawned-session permission mode |

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

- **Owner-locked**: only the bootstrapped Telegram user is accepted; others are dropped.
- The bot token is read from the environment, never placed on a command line.
- Spawned sessions run with elevated autonomy by design (unattended). Only enable
  `/new` if you understand that a phone message can drive a session that can edit
  files and run tools. The Tier-2 hook routes the riskier approvals back to you.

## Caveats

- **Untested end-to-end.** See the status banner up top. The runtime is proven;
  the packaged install path is not yet validated.
- **macOS only (for now).** The daemon + watchdog are launchd services and the
  installer renders launchd plists. The Python runtime is portable; a Linux
  (systemd) service layer is a TODO.
- **Claude Code specific.** The in-session poll loop is a session-scoped cron, a
  Claude Code feature. This is not a generic LLM/agent bridge.
- **gstack coupling.** Context save/restore and the compaction handoff call
  gstack skills by default. Without them, follow
  [Decoupling from gstack](#decoupling-from-gstack).
- **Telegram setup gotchas.** The group MUST have **topics/forum mode enabled**,
  and the bot MUST be a group **admin** to create topics and read messages.
- **Elevated autonomy.** Spawned sessions (`/new`) run unattended with broad tool
  access. A phone message can drive a session that edits files and runs commands.
  Only enable `/new` if you accept that. See [Security notes](#security-notes).
- **Single owner.** Bootstraps to one Telegram user; not multi-user.

## License

Beerware (Revision 42). See [LICENSE](LICENSE).
