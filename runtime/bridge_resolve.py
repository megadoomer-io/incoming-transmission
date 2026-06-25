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

import glob
import json
import os
import subprocess

# The bridge's tmux binary. The hook/MCP run as children of an in-pane Claude, so a
# bare `tmux` would also work via inherited $TMUX; we use the configured binary for
# parity with the router and to be explicit about which tmux.
TMUX_BIN = os.environ.get("TELEGRAM_BRIDGE_TMUX", "/opt/homebrew/bin/tmux")
PANE_OPTION = "@telegram_thread_id"
ENV_THREAD = "TELEGRAM_BRIDGE_THREAD_ID"

STATE_DIR = os.environ.get(
    "TELEGRAM_BRIDGE_STATE_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "state", "telegram-bridge"))
REGISTRY_DIR = os.path.join(STATE_DIR, "registry")


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


# --- migration fallback (cwd-keyed) -----------------------------------------
# Removed after the pane-keyed cutover. During migration, a session bound before
# the upgrade has no pane option and no env var, so it must still resolve by cwd
# the way the pre-pane resolvers did. Centralizing the OLD logic here too means the
# three callers share ONE implementation during the transition, not three copies.

def _pid_alive(pid):
    """True if the process is alive. A dead claude_pid means an orphaned registry
    entry that must not gate a fresh session sharing the same cwd."""
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True  # exists, owned by another user (defensive; same-user here)


def _claude_ancestor_pid():
    """PID of the `claude` process owning this call, by walking up the tree. Lets
    same-cwd sessions disambiguate to the right registry entry. macOS `ps -o comm=`
    truncates to 16 chars, so use `ps -c`."""
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-c", "-o", "comm=,ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
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


def resolve_topic_cwd_fallback(cwd, registry_dir=REGISTRY_DIR,
                               pid_alive=_pid_alive, ancestor_pid=_claude_ancestor_pid):
    """Old cwd-keyed resolution, preserved for the migration window. Returns the
    thread id (str) for the registry entry matching `cwd`, or None.

    Mirrors the pre-pane behavior exactly so a pre-upgrade session keeps resolving:
    match on realpath(cwd) + a non-null thread_id, SKIP entries whose claude_pid is
    dead (the shipped orphan-gate fix), then on multiple live matches disambiguate by
    the owning claude pid, newest registered_at as the last resort."""
    if not cwd or not os.path.isdir(registry_dir):
        return None
    cwd = os.path.realpath(cwd)
    matches = []   # (registered_at, claude_pid, thread_id)
    for f in glob.glob(os.path.join(registry_dir, "*.json")):
        try:
            reg = json.loads(open(f).read())
        except (OSError, ValueError):
            continue
        rc = reg.get("cwd")
        if not rc or os.path.realpath(rc) != cwd or reg.get("thread_id") is None:
            continue
        cp = reg.get("claude_pid")
        if cp is not None and not pid_alive(cp):
            continue   # orphaned entry (dead session) — don't resolve to it
        matches.append((str(reg.get("registered_at", "")), cp, str(reg["thread_id"])))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][2]
    cpid = ancestor_pid()
    if cpid is not None:
        for m in matches:
            if m[1] == cpid:
                return m[2]
    matches.sort(reverse=True)   # newest registered_at wins
    return matches[0][2]


def resolve(env=None, pane_option_reader=read_pane_option, cwd=None,
            registry_dir=REGISTRY_DIR, log=None, cwd_fallback=True):
    """Top-level resolver used by the hook / MCP / AUQ-hook. Pane-keyed first
    (`resolve_topic`); during migration falls back to cwd-keyed resolution so a
    session bound before the upgrade still resolves. Pass `cwd_fallback=False` for
    the post-cutover pane-only behavior. Returns a thread id (str) or None; the
    caller then loads the registry entry by thread id for inbox/session_dir."""
    tid = resolve_topic(env=env, pane_option_reader=pane_option_reader, log=log)
    if tid is not None:
        return tid
    if not cwd_fallback:
        return None
    e = os.environ if env is None else env
    cwd = cwd or e.get("PWD") or os.getcwd()
    return resolve_topic_cwd_fallback(cwd, registry_dir=registry_dir)


def _which_path():
    """Diagnostic: report how THIS pane/session resolves (pane / env / cwd / none).
    Used to validate the canary by eye — run from a pane and see which path wins."""
    env_tid = (os.environ.get(ENV_THREAD) or "").strip()
    pane = (os.environ.get("TMUX_PANE") or "").strip()
    pane_tid = read_pane_option(pane).strip() if pane else ""
    if pane_tid:
        return "pane", pane_tid
    if env_tid:
        return "env", env_tid
    tid = resolve_topic_cwd_fallback(os.environ.get("PWD") or os.getcwd())
    if tid:
        return "cwd-fallback", tid
    return "none", None


if __name__ == "__main__":
    path, tid = _which_path()
    print("resolved via {}: thread_id={}".format(path, tid))
