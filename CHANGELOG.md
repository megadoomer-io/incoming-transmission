# Changelog

## Unreleased — tmux window reaper

### Added
- **An opt-in janitor that kills confirmed-dead bridge windows in the shared `claude`
  tmux session** (issue #6). Two corpse classes: a retired handoff window (renamed
  `DEAD - <name>` by `kill_old=false`) and an abandoned spawn whose pane has died
  (`tg:*` with `pane_dead`), each killed after `window_reap_grace_seconds`. The
  reaper is **opt-in** (`window_reaper_enabled`, default off) because `kill-window`
  is irreversible — the same reason the topic delete-reaper is opt-in. It only ever
  targets **bridge-named** windows (`DEAD - ` exact prefix, or `tg:`), so a user's
  own claude window is never touched, and a **live** `tg:*` spawn (claude still
  running) is never killed — left for the owner to `/end`, since killing it could
  discard live work. Posts a one-line summary to the General topic when it acts.
  This is the tmux-side counterpart to the #7 reconciliation sweep, which cleans the
  registry/topic side. Pure policy is `_windows_to_reap`; the impure shell is
  `reap_stale_windows`, wired into `context_loop`. New config:
  `window_reaper_enabled` (false), `window_reap_grace_seconds` (3600),
  `window_reap_interval_seconds` (1800). (`runtime/telegram-router.py`; coverage in
  `tests/test_window_reaper.py`.)

## Unreleased — session↔topic reconciliation sweep

### Added
- **The router now reconciles sessions against forum topics on a ~20m sweep, so the
  two views can't silently drift** (issue #7). Two directions: a registry entry
  whose `claude_pid` is dead (the session exited or the box rebooted) has its forum
  topic **closed** and the registry entry dropped, so a phone message gets the "no
  session" reply instead of routing into a void; and a ledger topic still marked
  `open` with no backing session (aged past a grace window) is closed too. The sweep
  uses `closeForumTopic` (reversible — an owner can reopen), which is why it is
  **always-on**, unlike the irreversible delete-reaper. Safety: entries with no
  `claude_pid` are left alone (matches the startup prune), a session mid-rollover
  (`compacting.lock` present) is treated as alive so a handoff is never closed, and
  the owner is alerted in-topic before each close. A close that fails still drops the
  dead registry entry and leaves the ledger `open` so the next sweep retries. Policy
  is the pure `_reconcile_plan`; the impure shell is `reconcile_sessions_topics`,
  wired into `context_loop`. New config: `reconcile_interval_seconds` (1200) and
  `reconcile_grace_seconds` (600). (`runtime/telegram-router.py`; coverage in
  `tests/test_reconcile.py`.)

## Unreleased — drain lock-leak fix

### Fixed
- **An empty `telegram-inbox.sh drain` no longer leaks `poll.lock.d`.** `drain`/`ack`
  is a lock handshake: `drain` acquires the per-topic lock, `ack` releases it. The
  bind-time section-A drain runs before any message has arrived, so it found an empty
  inbox, kept the lock, and the session reasonably skipped the ack ("nothing to
  drain"). The first real message then couldn't acquire the leaked lock and stalled
  until the 30-minute stale-TTL (or a session self-heal) cleared it — a multi-minute
  delay on the first reply of every freshly bound session. An empty drain now
  releases the lock immediately and needs no ack, so the empty path can't leak
  regardless of whether the session acks. Also silenced the `wc: No such file or
  directory` stderr on that path (the `<` redirect error escaped `2>/dev/null` —
  reordered so an empty drain is truly silent). Surfaced by the keyboard `/telegram`
  round-trip test. (`runtime/telegram-inbox.sh`; regression coverage in
  `tests/test_inbox_drain.py`.)

## Unreleased — pane-keyed session↔topic resolution (Phase 1)

### Changed
- **All three resolvers now share `bridge_resolve`, keyed on the tmux pane.** The
  permission hook, the AskUserQuestion MCP server, and the AskUserQuestion hook
  each carried their own near-identical copy that resolved a session's topic by
  cwd. cwd is not unique per session — two Claudes in one repo share it — so a
  shared cwd could route one session's prompts to the other's topic. They now
  call `bridge_resolve.resolve()`, which keys on the `@telegram_thread_id` pane
  option (authoritative) and the `TELEGRAM_BRIDGE_THREAD_ID` spawn env
  (race-free), with a cwd fallback retained for sessions bound before the
  migration. (`runtime/telegram-auq-mcp.py`, `runtime/telegram-askuserquestion-hook.py`;
  resolver added earlier in `runtime/bridge_resolve.py`.)
- **Binding moved off the LLM — programmatic topic creation, registration, and
  pane stamping.** Creating the forum topic, writing `registry/<thread>.json`, and
  stamping `@telegram_thread_id` used to run in the skill (the model). Now the
  spawn script does it for `/new` and compaction rollover (it creates/reuses the
  topic, injects the race-free `TELEGRAM_BRIDGE_THREAD_ID`, captures the new pane id,
  stamps the option, and writes the registry — carrying `spawned` forward on
  rollover), and the router does it for `/attach` (create topic, stamp, register,
  then send `/telegram-bridge`). The skill now DETECTS it's already bound and loads
  only the drain procedure; its self-bind remains as a guarded legacy fallback
  (removed in the clean cutover). Registry entries gain `pane_id`; the router
  lazily backfills `transcript_path` (unknown at spawn time) so the context gauge
  still starts — matched to the session by an EXACT `pane_id` join (the SessionStart
  hook now records `TMUX_PANE`), re-checked each sweep so a rollover re-points to the
  replacement's transcript; cwd-newest is only a fallback for pre-`pane_id` records.
  (`runtime/telegram-spawn.sh`, `runtime/telegram-router.py`, `skill/telegram-bridge/SKILL.md`,
  `hooks/telegram-self-register.py`.)

- **Diagnostic: `debug_raw_updates` config flag.** When set in `compaction.json`
  (read live per batch, default off, zero cost when off), the router logs the raw
  JSON of every `getUpdates` result before dispatch. Added to answer OQ1 — and it
  did: `forum_topic_created`/`closed`/`reopened` DO arrive as `message` updates
  (the close message's `date` is the `closed_at` a TTL needs). They were invisible
  before only because they're bot-authored and the owner-filter drops them, so a
  future topic ledger must intercept `forum_topic_*` ahead of that drop.
  (`runtime/telegram-router.py`.)

- **Shared programmatic bind (`bridge_bind.py`) + keyboard `/telegram` self-bind.**
  Extracted the bind primitives (create topic, stamp pane, write registry) into one
  stdlib module, the write-side counterpart to `bridge_resolve`. The router's
  `/attach` now calls `bridge_bind.bind_pane` instead of its own copies, and a new
  self-bind CLI (`python3 bridge_bind.py`, binds `$TMUX_PANE`) lets a keyboard
  `/telegram` bind programmatically — same `bind_pane`, no LLM-run createForumTopic
  or registry heredoc. The SKILL.md legacy heredoc is gone; its not-bound branch is
  now a one-line CLI call. One bind implementation, three entry points (spawn,
  router, keyboard). (`runtime/bridge_bind.py`, `runtime/telegram-router.py`,
  `skill/telegram-bridge/SKILL.md`.)

- **`/attach` lists candidates by cwd basename, not the window name (issue #17).**
  With `automatic-rename` on, the window name tracks the foreground process, so the
  list read `zsh`, `zsh`, `zsh` — useless. Claude Code's pane title is a volatile,
  spinner-prefixed activity line, also no good as a stable handle. The list now
  labels each candidate with the cwd basename (`incoming-transmission`, `joey`), the
  project name you actually pick a session by; selectors match that label or the
  window index. (`runtime/telegram-router.py` — new `_pane_label`.)

### Added
- **`/attach all` — bulk-adopt every unattached session.** Binds each candidate in
  one command (loop create-topic + stamp + register + send-keys), reporting the
  roster of topics opened and any that failed. The single and bulk paths now share
  one bind helper (`_attach_one`), so both run identical logic.
  (`runtime/telegram-router.py`.)

- **Topic lifecycle ledger + reaper ("tidy forum").** Building on OQ1
  (`forum_topic_*` arrive as bot-authored `message` updates), the router now folds
  those events into a `topics.json` ledger — intercepted ahead of the owner-filter
  that would drop them — recording each topic's state and `closed_at` (the close
  message's `date`). An opt-in reaper (`topic_reaper_enabled`, off by default since
  `deleteForumTopic` is irreversible) runs on the context-loop timer, throttled to
  `topic_reap_interval_seconds`, and deletes any topic closed longer than
  `topic_ttl_seconds` (default 7d), so ended and rolled-over sessions stop piling up
  closed topics. Pure `_topics_to_reap` carries the aging policy.
  (`runtime/telegram-router.py`, `runtime/compaction.json`.)

### Removed
- **Clean cutover: the resolver's cwd fallback is gone.** With binding now
  programmatic on every path (spawn for `/new` + rollover, router for `/attach`),
  `bridge_resolve.resolve()` is pane-keyed only — pane option then spawn env, no
  cwd. Deleted `resolve_topic_cwd_fallback` and its `_pid_alive` /
  `_claude_ancestor_pid` helpers, the `cwd_fallback`/`cwd`/`registry_dir` params,
  and the now-dead cwd-keyed `find_topic` (+ helpers) still sitting in the
  permission hook. The three callers call `resolve()` with no cwd. A pane with no
  `@telegram_thread_id` is, by definition, not a bridge session.
  (`runtime/bridge_resolve.py`, `runtime/telegram-permission-hook.py`,
  `runtime/telegram-auq-mcp.py`, `runtime/telegram-askuserquestion-hook.py`.)

### Fixed
- **`poll_lock_ttl_seconds` / `handoff_lock_ttl_seconds` from `compaction.json` are
  now honored.** `load_compaction_cfg` only carries through keys present in
  `CONFIG_DEFAULTS`, and these two weren't — so the shipped file values were
  silently dropped and only the context-loop inline fallbacks ran. Added both to
  `CONFIG_DEFAULTS` (same numbers, so no behavior change), making the config keys
  live. (`runtime/telegram-router.py`.)
- **The AUQ MCP and AUQ hook no longer mis-route to an orphaned topic.** The
  earlier dead-pid backstop only patched the permission hook; routing the AUQ
  resolvers through the shared resolver closes the same latent mis-route in both
  (a fresh session sharing a dead session's cwd is no longer gated against the
  stale topic). (`runtime/telegram-auq-mcp.py`, `runtime/telegram-askuserquestion-hook.py`.)
- **A reaped pane no longer keeps its topic binding.** The default reap path
  (`kill_old=false`) renames the retired window `DEAD - <name>` but leaves the
  pane alive, so its `@telegram_thread_id` stamp lingered — a fresh session later
  started in that pane would inherit it and resolve to the dead topic. The reap
  now also `set-option -pu`s the binding, and the router unsets it on any pane in a
  `DEAD`-renamed window each sweep as a backstop against a missed in-session clear.
  (`runtime/poll-prompt.tmpl`, `runtime/telegram-router.py`.)

## Unreleased — router-integrated wedge auto-clear

### Changed
- **Wedge auto-clear folded into the router; watchdog retired.** Detection of a
  session stuck on an interactive menu (a "wedge") now lives in the router's
  `context_loop` instead of a separate `telegram-watchdog.py` daemon + launchd
  job, both removed. After `wedge_dwell_seconds` on a detected wedge the router
  sends Escape to the session's pane. Scope is bridged panes only: it never
  touches a non-bridge session, and it skips a topic that already has a
  permission pending on the phone. (`telegram-router.py`; removed
  `telegram-watchdog.py`, `com.telegram.watchdog.plist`, and the watchdog control
  subcommands.)

### Fixed
- **Wedge detection is bottom-anchored (no more false positives).** Only the last
  ~12 pane lines are examined, an "Esc to cancel" footer must appear in the last 3
  lines, and the cursor must sit on a numbered option — so a session that merely
  *displays* the menu pattern as content (code, a captured menu) is no longer
  Escaped. Also dropped a race where a nudge could fire immediately after the Esc.
  (`telegram-router.py`.)
- **A dead-pid registry entry never gates a session.** Real-time backstop to the
  router's startup prune: the permission hook skips registry entries whose
  `claude_pid` is dead, so even between router restarts an orphaned entry can't
  hang a fresh session that shares its cwd. (`telegram-permission-hook.py`.)

## Unreleased — session lifecycle: reaping, /attach, orphan cleanup

### Added
- **`/attach` — adopt an existing Claude session.** Lists unattached `claude`
  panes in the shared tmux session and binds one to a topic by sending it
  `/telegram-bridge`, so a session you started by hand becomes phone-reachable
  without respawning. (`telegram-router.py`.)
- **Bridge-owned flag + reaping policy.** A `/new` spawn is marked
  `TELEGRAM_BRIDGE_SPAWNED=1`; `/end` reaps the tmux window only when the bridge
  owns it, while a compaction rollover reaps the superseded window regardless of
  ownership. An adopted (user-started) session is detached on `/end`, never
  reaped. (`telegram-router.py`, `telegram-spawn.sh`.)

### Changed
- **Daemon commands work from any topic.** `/new`, `/attach`, `/sessions`,
  `/whoami`, `/help` are handled by the router from whatever topic they're typed
  in — every topic doubles as a control channel. (`telegram-router.py`.)

### Fixed
- **Orphaned registry entries no longer gate fresh sessions.** A reboot or crash
  leaves a dead session's registry JSON on disk; because the permission hook
  resolves a bridge session by cwd, a fresh Claude opened in that directory
  matched the dead entry and routed its approvals to a Telegram topic no one was
  watching, hanging ~28 min. The router now prunes registry entries whose
  `claude_pid` is dead at startup. (`telegram-router.py`.)

## Unreleased — risk-tiered permissions (Tier 2)

The Tier-2 permission gate now works for real and applies uniformly to **every**
bridge session. Previously it was effectively dead code: spawns launched with
`--dangerously-skip-permissions`, under which the hook's decisions never fired, so
`auto-allow` was the de-facto behavior by accident.

### Added
- **Risk-tiered permission round-trip (the new default).** Dangerous tool calls —
  `Write`/`Edit`/`NotebookEdit`, **risky** Bash (`rm -rf`, force-push, `kubectl
  delete`, `drop table`, `curl … | sh`, …), and any non-allowlisted `mcp__*` — are
  routed to the owner's Telegram topic for tap-to-approve; safe Bash, reads, and
  allowlisted tools run untouched. (`telegram-permission-hook.py`.)
- **Rich answers.** Inline buttons: ✅ Approve · ✅ Always allow · ✍️ Approve + note ·
  ⛔ Deny · ✍️ Deny + redirect — plus typed `y`/`n` and `y <note>` / `n <redirect>`.
  The owner's free-text note/redirect is delivered to the session as a **trusted
  inbox message** (a spawned model distrusts hook-injected text), with the hook's
  decision carrying only a "read your inbox" pointer. (`telegram-router.py`.)
- **Persisted "always allow" rules.** ✅ Always allow writes a NARROW native rule
  (e.g. `Write(/abs/path)`, `Bash(git push:*)`, exact `mcp__server__tool`; never a
  wildcard) to `~/.telegram-bridge/spawned-allow.json`, read live by the hook so the
  same call won't ask again. Auditable, editable plain JSON.

### Changed
- **Spawns launch with `--permission-mode dontAsk`** instead of
  `--dangerously-skip-permissions`. In dontAsk a tool the hook doesn't allow is
  auto-denied (never a hanging prompt); a hook `allow` runs it; a hook `deny`
  overrides even a broad `Bash(*)` allow (CC ≥ 2.1.178). (`telegram-spawn.sh`.)
- **Default `spawned_mode` is now `risk-tiered`** (was `auto-allow`). `auto-allow`
  (fully autonomous, no round-trip) is opt-in. (`runtime/permissions.json`.)
- **The gate is bridge-MEMBERSHIP, not launch path.** A `/new` spawn and a
  `/telegram`-attached session gate identically — the hook keys off the session's
  topic in the registry, not `TELEGRAM_BRIDGE_SPAWNED`.
- **Register the hook in `settings.json`, not `settings.local.json`** — CC does not
  load `.local` PreToolUse hooks in spawned/headless sessions. Matcher
  `Write|Edit|NotebookEdit|Bash|mcp__.*`. (README / SKILL wiring updated.)
- **Approval wait raised to ~28 min** (hook `timeout` 30 min) so the owner can be
  away from their phone. (Was 240s.)

### Fixed
- **`claude --settings <file>` silently dropped the user's hooks.** The spawn used
  it to load persisted allow-rules, which disabled the very permission hook. Removed;
  the hook reads `spawned-allow.json` directly instead. (`telegram-spawn.sh`.)
- **tmux session-env leak.** `TELEGRAM_BRIDGE_SPAWNED` and the bot token were set via
  `tmux new-session -e`, which is session-scoped — so every window opened in the
  shared `claude` tmux session inherited them, making attended sessions gate (and
  exposing the token). Now passed via the `/usr/bin/env` command wrapper
  (process-scoped). (`telegram-spawn.sh`.)
- **"Always allow" could persist a bogus rule.** A mis-parsed command skeleton (e.g.
  a numeric first token) produced rules like `Bash(1680:*)`; the skeleton extractor
  now rejects non-command-shaped tokens and degrades to allow-once.

## Unreleased — review-fix pass (agnostic transport)

Implements an outside review's "do-now" set. The throughline: make
incoming-transmission an **agnostic transport** that injects only
transport-necessary mechanism, with all operator-style behavior moved behind a
documented, user-amendable seam (default = normal Claude).

### Added
- **AskUserQuestion over Telegram (verified end-to-end).** A spawned session asks
  the owner through the `mcp__telegram__AskUserQuestion` MCP server: single-select
  options render as one-tap buttons; multi-select renders a toggle keyboard (tap to
  check, then Done) and returns a `list[str]`. **Typed replies** now work — a number
  picks that option, free text is an "Other" answer, `1 3` picks several — with a
  re-prompt on input that mixes out-of-range numbers or stray words instead of a
  silent partial. Fires under `spawned_mode: "ask"` (or with the tool allowlisted);
  `auto-allow` still denies the question so an unattended model self-decides.
  (`telegram-auq-mcp.py`, `telegram-router.py`.)

### Fixed
- **Symlinked cwd broke session-topic lookup.** The registry stored a session's cwd
  as the spawn saw it (e.g. `/tmp`) while the MCP server and permission hook resolve
  `os.getcwd()` (e.g. `/private/tmp` on macOS), so the topic match silently failed and
  AUQ / approval round-trips couldn't reach the session. Both sides now compare
  canonical `realpath`s. (`telegram-auq-mcp.py`, `telegram-permission-hook.py`,
  `telegram-askuserquestion-hook.py`.)
- **`install.sh` reset spawned-session posture on every run.** It copied
  `runtime/permissions.json` over the deployed copy, clobbering a user-chosen
  `spawned_mode`. Now seeded only when absent, like `dir-aliases.json`.

### Changed
- **Smart-router / dumb-session redesign (core).** Moved polling/timing
  intelligence from per-session poll crons into the router daemon. The router now
  (a) **pushes** a one-line drain nudge to the session's pane the moment a message
  arrives (primary delivery), (b) computes each session's context gauge itself on a
  dedicated thread (`context_loop`, off the getUpdates path), and (c) detects the
  compaction trigger and nudges the session to hand off, and (d) **backstops**
  delivery — if a pushed message stays undrained it re-nudges, throttled to
  `backstop_seconds` and only while the inbox is non-empty. The session is reduced to
  a "dumb" processor that runs NO cron at all: it loads the bridge procedure at attach
  and drains purely on the router's nudges — the adaptive backoff ladder
  (`idle.count`/`poll.level`/`idle_intervals_seconds`/`ticks_per_rung`), the
  session-side status gauge, and the session heartbeat cron are all gone.
  `compaction.json` swaps the `polling` ladder for `backstop_seconds` +
  `context_interval_seconds`. `poll-prompt.tmpl`,
  `telegram-router.py`, the `/telegram` skill, and the README are updated together.
  **UNTESTED end-to-end** (no live token); the wake path in particular
  (send-keys into a busy TUI) is unverified pending the first test pass.
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
- **Configurable lifecycle hooks (C1).** Four per-event hook files in
  `~/.telegram-bridge/lifecycle/` — `style` (reply formatting, every attach),
  `start` (restore on a spawned/rollover birth), `save` (persist on rollover), `end`
  (actions on `/end`). The session reads the relevant one live at each lifecycle
  moment, so edits take effect with no re-render. `style`/`end` ship empty (default
  = normal Claude / just detach); `start`/`save` ship a functional agnostic default
  (rollover continuity needs save/restore), with the gstack version documented as a
  commented example. This pulls the last hardcoded gstack-isms (`/track`,
  `/context-save`, `/context-restore`) out of `poll-prompt.tmpl` and
  `telegram-spawn.sh`. Replaces the earlier spawn/bridge preamble seam.
- **Watchdog control subcommands (C2).** `telegram-bridge watchdog-start` /
  `watchdog-stop` render the watchdog plist (`__HOME__`/`__STATE_DIR__`/`__TOKEN__`)
  to `~/Library/LaunchAgents/`, `chmod 600`, and bootstrap/bootout it — mirroring
  the bridge-plist flow. README step 6 updated to use it (previously it told you to
  bootstrap a never-installed, unrendered plist).
- **README "Philosophy: transport, not workflow opinion" and "Customizing agent
  behavior" sections (H4)**, plus a "Known issues" section.

### Fixed
- **`/new` spawns now wire in the AskUserQuestion MCP (#1).** `telegram-spawn.sh`
  passes `--mcp-config <auq config>` to the spawned `claude` when the rendered
  config exists, so an unattended spawn that has native AUQ disabled gets the
  Telegram button-based AUQ as its replacement. Previously a spawn disabled native
  AUQ but wired no substitute, so it could not ask the owner anything unless the
  user had separately added the MCP to their global settings. The flag is added
  only when `~/.telegram-bridge/telegram-auq-mcp.json` exists (the MCP is
  optional), so installs without it are unaffected. The launch flags are now
  collected in one `CLAUDE_FLAGS` array shared by both tmux branches instead of
  being duplicated.

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
