# Agent instructions

Guidance for agents working **on** this repo and for agents running **as** a
bridged session. The README's
[Philosophy](README.md#philosophy-transport-not-workflow-opinion) is the source of
truth for what the bridge does and does not impose; this file adds the one
behavioral nuance that the spawn/poll prompts are easy to misread.

## Unattended spawn vs. human driving over the bridge

The spawn and poll prompts carry "unattended" framing — native `AskUserQuestion`
is disabled, prefer to proceed and state your assumptions, be deliberate with
writes. That language is **conditional on there being no human in the loop**. Read
it in two modes:

- **(a) Genuinely-unattended spawn** — a `/new` deploy with nobody watching the
  pane and no live operator answering. Here the autonomous framing applies: make
  reasonable decisions yourself, don't block on a picker no one can tap, be
  deliberate about irreversible writes.

- **(b) A human actively driving the session over the bridge** — the owner is on
  Telegram issuing instructions and reading replies turn by turn. Here the human is
  in the loop, so **normal collaborative mode fully applies**: propose, discuss,
  confirm scope, take turns. The autonomous framing does **not** apply — Telegram is
  just a different keyboard, not a license to act on your own.

The bridge is an agnostic transport (see Philosophy). The "unattended" wording in
the prompts describes a transport constraint of case (a), not a blanket posture for
every bridged session. When a human is talking to you, behave like normal Claude.
