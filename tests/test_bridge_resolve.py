# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the shared session->topic resolver. Pure: a stub pane_option_reader
# means no test ever shells out to tmux. Covers the full precedence table plus the
# resolver edge cases the eng review called out ($TMUX_PANE absent, empty option,
# env-vs-pane mismatch, wrong-server-as-empty).

import bridge_resolve


def _reader(mapping):
    """Build a pane_option_reader stub: pane id -> option value."""
    return lambda pane: mapping.get(pane, "")


def test_pane_only_resolves():
    # Adopted session: pane stamped, no env var.
    env = {"TMUX_PANE": "%7"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({"%7": "2103"})) == "2103"


def test_env_only_resolves_spawn_race_window():
    # Spawned session, first tool call before the router stamped the pane option.
    env = {"TMUX_PANE": "%7", "TELEGRAM_BRIDGE_THREAD_ID": "2103"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({})) == "2103"


def test_env_only_resolves_without_tmux_pane():
    # Defensive: env binding present even if TMUX_PANE somehow unset.
    env = {"TELEGRAM_BRIDGE_THREAD_ID": "2103"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({})) == "2103"


def test_both_agree():
    env = {"TMUX_PANE": "%7", "TELEGRAM_BRIDGE_THREAD_ID": "2103"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({"%7": "2103"})) == "2103"


def test_both_disagree_pane_wins_and_logs():
    env = {"TMUX_PANE": "%7", "TELEGRAM_BRIDGE_THREAD_ID": "999"}
    logged = []
    out = bridge_resolve.resolve_topic(
        env=env, pane_option_reader=_reader({"%7": "2103"}), log=logged.append)
    assert out == "2103"                      # pane is authoritative
    assert logged and "disagrees" in logged[0]


def test_not_a_bridge_session_no_pane_no_env():
    assert bridge_resolve.resolve_topic(env={}, pane_option_reader=_reader({})) is None


def test_not_a_bridge_session_pane_without_option():
    # In tmux but pane carries no option (a normal, non-bridge session).
    env = {"TMUX_PANE": "%9"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({})) is None


def test_wrong_tmux_server_reads_empty_abstains():
    # A pane in the owner's OWN tmux: $TMUX_PANE present but the query returns "".
    env = {"TMUX_PANE": "%3"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=lambda pane: "") is None


def test_whitespace_is_stripped():
    env = {"TMUX_PANE": "%7"}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({"%7": " 2103 \n"})) == "2103"


def test_blank_env_var_is_not_a_binding():
    # Empty/whitespace env var must not be treated as a thread id.
    env = {"TMUX_PANE": "%7", "TELEGRAM_BRIDGE_THREAD_ID": "  "}
    assert bridge_resolve.resolve_topic(env=env, pane_option_reader=_reader({})) is None


# --- cwd fallback (migration shim) ------------------------------------------

import json
import os


def _write_reg(regdir, thread_id, cwd, claude_pid=None, registered_at="2026-01-01T00:00:00Z"):
    regdir.mkdir(parents=True, exist_ok=True)
    (regdir / "{}.json".format(thread_id)).write_text(json.dumps({
        "thread_id": thread_id, "cwd": cwd, "claude_pid": claude_pid,
        "inbox_path": "/tmp/x/{}/inbox.jsonl".format(thread_id),
        "registered_at": registered_at,
    }))


def test_cwd_fallback_matches_live_entry(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, str(tmp_path), claude_pid=1234)
    out = bridge_resolve.resolve_topic_cwd_fallback(
        str(tmp_path), registry_dir=str(reg), pid_alive=lambda p: True)
    assert out == "2103"


def test_cwd_fallback_skips_dead_pid(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, str(tmp_path), claude_pid=99999)
    out = bridge_resolve.resolve_topic_cwd_fallback(
        str(tmp_path), registry_dir=str(reg), pid_alive=lambda p: False)
    assert out is None                      # orphaned entry not resolved


def test_cwd_fallback_no_match(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, "/somewhere/else", claude_pid=1234)
    out = bridge_resolve.resolve_topic_cwd_fallback(
        str(tmp_path), registry_dir=str(reg), pid_alive=lambda p: True)
    assert out is None


def test_cwd_fallback_multi_disambiguates_by_ancestor(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, str(tmp_path), claude_pid=111, registered_at="2026-01-01T00:00:00Z")
    _write_reg(reg, 2200, str(tmp_path), claude_pid=222, registered_at="2026-02-02T00:00:00Z")
    out = bridge_resolve.resolve_topic_cwd_fallback(
        str(tmp_path), registry_dir=str(reg), pid_alive=lambda p: True,
        ancestor_pid=lambda: 111)
    assert out == "2103"                    # ancestor pid picks the right one


def test_cwd_fallback_multi_newest_wins_without_ancestor(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, str(tmp_path), claude_pid=111, registered_at="2026-01-01T00:00:00Z")
    _write_reg(reg, 2200, str(tmp_path), claude_pid=222, registered_at="2026-02-02T00:00:00Z")
    out = bridge_resolve.resolve_topic_cwd_fallback(
        str(tmp_path), registry_dir=str(reg), pid_alive=lambda p: True,
        ancestor_pid=lambda: None)
    assert out == "2200"                    # newest registered_at


def test_resolve_prefers_pane_over_cwd_fallback(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 9999, str(tmp_path), claude_pid=1234)  # cwd would give 9999
    env = {"TMUX_PANE": "%7", "PWD": str(tmp_path)}
    out = bridge_resolve.resolve(
        env=env, pane_option_reader=_reader({"%7": "2103"}),
        registry_dir=str(reg))
    assert out == "2103"                    # pane path wins; cwd not consulted


def test_resolve_falls_back_to_cwd_when_no_pane(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, str(tmp_path), claude_pid=1234)
    env = {"PWD": str(tmp_path)}            # no pane, no env binding
    out = bridge_resolve.resolve(
        env=env, pane_option_reader=_reader({}), registry_dir=str(reg))
    assert out == "2103"


def test_resolve_pane_only_mode_skips_fallback(tmp_path):
    reg = tmp_path / "registry"
    _write_reg(reg, 2103, str(tmp_path), claude_pid=1234)
    env = {"PWD": str(tmp_path)}
    out = bridge_resolve.resolve(
        env=env, pane_option_reader=_reader({}), registry_dir=str(reg),
        cwd_fallback=False)
    assert out is None                      # post-cutover: no cwd fallback
