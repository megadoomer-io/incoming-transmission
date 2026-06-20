# Changelog

## Unreleased — initial scaffold

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
