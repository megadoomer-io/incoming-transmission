# Changelog

## Unreleased — review-fix pass (agnostic transport)

Implements an outside review's "do-now" set. The throughline: make
incoming-transmission an **agnostic transport** that injects only
transport-necessary mechanism, with all operator-style behavior moved behind a
documented, user-amendable seam (default = normal Claude).

### Changed
- **Separated transport mechanism from operator style (C1).** Stripped the
  autonomy/style language ("make decisions yourself", "be deliberate with writes")
  and the hardcoded `/context-restore` from the `/new` spawn prompts
  (`telegram-spawn.sh`); spawns now carry only the transport-necessary fact that
  native AskUserQuestion is disabled unattended (use the Telegram AUQ MCP instead).
  Removed style language from the bridged poll prompt (`poll-prompt.tmpl`) — a
  bridged session is human-in-the-loop and behaves like normal collaborative
  Claude. `/end` is now transport-teardown only; the compaction handoff's save step
  is described as a configurable mechanism (default gstack) rather than a baked-in
  call.
- **Self-register hook is true stdlib (H5).** Changed
  `telegram-self-register.py` from `#!/usr/bin/env -S uv run` (with `dependencies =
  []`) to `#!/usr/bin/env python3`, so the "pure stdlib, zero deps" claim holds.
  `uv` is now documented as needed ONLY for the optional AskUserQuestion MCP.
- **Genericized personal strings (L2).** Router WELCOME/HELP and comments no longer
  reference `@mcd_claude_bot` or "Mike's Claude Code"; SKILL.md uses generic
  placeholders. (Beerware license headers keep the author attribution by design.)
- **Polling-ladder docs match config (L5).** `poll-prompt.tmpl`'s inline comment
  now says `[60,120,300,1800]` (= 1m/2m/5m/30m), matching the shipped
  `compaction.json`.

### Added
- **Operator-style preamble seam (C1).** `spawn-preamble.example.txt` (for `/new`
  spawns) and `bridge-preamble.example.txt` (for bridged sessions). The installer
  seeds empty live copies (`spawn-preamble.txt` / `bridge-preamble.txt`, gitignored)
  only if absent; `telegram-spawn.sh` / `poll-render.sh` strip comments/blanks and
  prepend them when non-empty. Default install injects nothing → normal Claude.
- **Watchdog control subcommands (C2).** `telegram-bridge watchdog-start` /
  `watchdog-stop` render the watchdog plist (`__HOME__`/`__STATE_DIR__`/`__TOKEN__`)
  to `~/Library/LaunchAgents/`, `chmod 600`, and bootstrap/bootout it — mirroring
  the bridge-plist flow. README step 6 updated to use it (previously it told you to
  bootstrap a never-installed, unrendered plist).
- **README "Philosophy: transport, not workflow opinion" and "Customizing agent
  behavior" sections (H4)**, plus a "Known issues" section.

### Security
- **Plist token hardening (C3).** `telegram-bridge start` / `watchdog-start` now
  `chmod 600` the rendered launchd plists right after the sed render (they contain
  the bot token in plaintext; LaunchAgents default to 644). Security notes mention
  keeping the token out of the plist entirely as a future improvement.
- **Owner-lock on start (M5).** The daemon refuses to `start` when no owner is
  configured (no `TELEGRAM_BRIDGE_ALLOWED_USERNAME` env AND `state.json`
  `allowed_username`/`allowed_user_id` both null), narrowing the bootstrap-hijack
  window.
- **Executable bits on install (M4).** `install.sh` now `chmod +x` all installed
  `runtime/*.sh` and `runtime/*.py`.

### Documented (deferred — pending the first end-to-end test, not blind-rewritten)
- **AskUserQuestion mechanisms (H1).** The MCP server + button taps is the
  supported path (verified to work with what the router writes). Two alternates are
  flagged as unverified in README "Known issues": typed (non-button) MCP answers
  land in the inbox, not `auq-answer.json`; and `telegram-askuserquestion-hook.py`
  keys on a `via=="callback-auq"` marker the router doesn't emit. No safe,
  unambiguous code fix exists (the router only has the option index at callback
  time, not the label/text), so this is documented, not changed.
- **Spawned-session default (H2).** Left `spawned_mode: "auto-allow"` as-is (the
  approval round-trip is entangled with H1 and untested). README + SKILL now state
  honestly that the default is fully autonomous with no approval round-trip, with a
  bold warning at the `/new` enablement step; flagged as a pending decision.

## Initial scaffold

Greenfield extraction of a personal Telegram↔Claude Code bridge into a
standalone, open-sourceable repo under the megadoomer brand. The live personal
system is untouched; this is a parallel copy.

### Added
- Runtime copied verbatim from the proven system (router, send, spawn, context
  gauge, watchdog, permission hook, AskUserQuestion MCP + hook, poll loop).
- macOS `install.sh` that lays out `~/.telegram-bridge`, the control CLI, and
  renders the MCP config (templated paths).
- Genericized configs: `dir-aliases.example.json`, templated
  `telegram-auq-mcp.json.template`; identity stays runtime-bootstrapped in
  `state.json`.
- Branded README (the Massive / Invader / Tallest / transmission vocabulary),
  Beerware LICENSE, `.gitignore`.
- "Decoupling from gstack" doc: the exact save/restore touchpoints a vanilla
  install adjusts.

### Not yet done (test-gated for next week)
- End-to-end install + run on a clean setup (nothing here has been executed).
- Config-driven `context_skill` knob to make gstack save/restore fully optional
  at runtime (today it's a documented manual edit).
- Linux service layer (systemd) — runtime is portable, installer is macOS-only.
