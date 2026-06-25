#!/usr/bin/env python3
# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# bridge_resolve: the ONE place that answers "which Telegram topic does THIS
# session belong to?" — shared by the permission hook, the AskUserQuestion MCP
# server, and the AskUserQuestion hook (which historically each carried their own
# near-identical copy that resolved by cwd). Keying on the tmux PANE instead of cwd
# kills the collision class: cwd is not unique per session (two Claudes in one repo
# share it); a pane is.
#
# Two binding signals, in precedence order:
#
#   1. TELEGRAM_BRIDGE_THREAD_ID env var — the RACE-FREE SPAWN BINDING. The spawn
#      sets it at process launch, which is BEFORE the pane exists and therefore
#      before anything can stamp the pane option. So a spawned session's very first
#      tool call resolves correctly even in the window before the router stamps the
#      pane. Absent on /attach-adopted sessions (you can't inject env into a running
#      process), which fall through to the pane option.
#
#   2. @telegram_thread_id tmux pane option — the AUTHORITATIVE, DURABLE binding.
#      Stamped programmatically at bind for spawned AND adopted sessions, survives
#      as long as the pane (and only as long as the pane → reboot-safe), and is
#      rebuildable/inspectable. Read against the caller's OWN inherited $TMUX (the
#      server its pane lives in), so resolution is self-correcting across tmux
#      servers: a non-bridge pane simply has no option → abstain. Option PRESENCE,
#      not server identity, is the discriminator.
#
# Precedence: when both are present they must agree; a mismatch is logged and the
# pane option (authoritative durable record) wins. Otherwise pane option if set,
# else env var (the spawn-race window), else None — "not a bridge session", and the
# caller abstains. A None result is the cheap common case (every non-bridge tool
# call in every session), so the no-match path does at most one tmux subprocess and
# nothing else.
#
# Stdlib only; resolves on /usr/bin/python3 via PATH.

import os
import subprocess

# The bridge's tmux binary. The hook/MCP run as children of an in-pane Claude, so a
# bare `tmux` would also work via inherited $TMUX; we use the configured binary for
# parity with the router and to be explicit about which tmux.
TMUX_BIN = os.environ.get("TELEGRAM_BRIDGE_TMUX", "/opt/homebrew/bin/tmux")
PANE_OPTION = "@telegram_thread_id"
ENV_THREAD = "TELEGRAM_BRIDGE_THREAD_ID"


def read_pane_option(pane, tmux_bin=TMUX_BIN):
    """Return @telegram_thread_id for `pane` (str), or "" on unset / any failure.

    Queried via the caller's own tmux server (the pane id came from the inherited
    $TMUX_PANE, so the matching server is the one this process lives in). Any error
    — tmux missing, pane gone, option unset, wrong server — collapses to "" so the
    caller treats it as "not bound here" and abstains."""
    if not pane:
        return ""
    try:
        out = subprocess.run(
            [tmux_bin, "display-message", "-p", "-t", pane, "#{%s}" % PANE_OPTION],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def resolve_topic(env=None, pane_option_reader=read_pane_option, log=None):
    """Resolve THIS session's Telegram thread id.

    Returns the thread id as a str, or None if this is not a bridge session
    (the caller then abstains — leaves the session's normal flow untouched).

    Args:
        env: mapping to read TMUX_PANE / TELEGRAM_BRIDGE_THREAD_ID from. Defaults
            to os.environ; tests inject a dict.
        pane_option_reader: callable(pane) -> option str. Defaults to the live tmux
            reader; tests inject a stub so they never shell out.
        log: optional callable(str) for the rare env-vs-pane mismatch warning.
    """
    env = os.environ if env is None else env
    env_tid = (env.get(ENV_THREAD) or "").strip()
    pane = (env.get("TMUX_PANE") or "").strip()
    pane_tid = pane_option_reader(pane).strip() if pane else ""

    if env_tid and pane_tid and env_tid != pane_tid:
        # Should never happen (env is immutable per process; a rollover spawns a
        # fresh process). If it does, the pane option is the durable truth.
        if log:
            log("bridge_resolve: {}={} disagrees with pane {}={}; using pane "
                "(authoritative)".format(ENV_THREAD, env_tid, PANE_OPTION, pane_tid))
        return pane_tid
    if pane_tid:
        return pane_tid          # authoritative, durable binding
    if env_tid:
        return env_tid           # spawn-race window: pane option not yet stamped
    return None                  # not a bridge session
