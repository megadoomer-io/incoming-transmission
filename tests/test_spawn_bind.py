# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Behavioral test for telegram-spawn.sh's programmatic bind — the riskiest change
# in the binding cutover. Exercises the --attach (compaction rollover) path, which
# REUSES an existing thread id and so needs no Telegram network call: it still runs
# the full bind mechanics (env injection, pane-id capture, pane stamp, registry
# write, spawned carry-forward). tmux + claude are stubbed; HOME/STATE_DIR are
# redirected to a tmpdir so nothing touches the real environment. The /new path's
# createForumTopic is validated live (see the plan's verification section).

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPAWN = REPO / "runtime" / "telegram-spawn.sh"

# spawn.sh borrows the user's interactive PATH via `zsh -ic`; skip cleanly if zsh
# isn't present (it always is on the macOS dev box this bridge targets).
pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="spawn.sh needs zsh")


_FAKE_TMUX = """#!/usr/bin/env bash
# Record argv, emulate the subcommands telegram-spawn.sh uses.
echo "$@" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session)              exit 0 ;;   # pretend the shared session exists
  new-window|new-session)   echo "%99" ;;   # -P -F '#{pane_id}' -> fake pane id
  display-message)          echo "4242" ;;  # '#{pane_pid}' -> fake claude pid
esac
exit 0
"""


def test_spawn_attach_binds_programmatically(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    (state / "registry").mkdir(parents=True)
    (state / "state.json").write_text(json.dumps({"chat_id": -100123}))
    # Pre-existing entry for thread 9001 with spawned=False (an adopted session) so
    # we can assert the rollover carries `spawned` forward rather than forcing True.
    (state / "registry" / "9001.json").write_text(
        json.dumps({"thread_id": 9001, "spawned": False}))

    faketmux = tmp_path / "tmux"
    faketmux.write_text(_FAKE_TMUX)
    faketmux.chmod(0o755)
    fakeclaude = tmp_path / "claude"
    fakeclaude.write_text("#!/usr/bin/env bash\n")
    fakeclaude.chmod(0o755)
    log = tmp_path / "tmux.log"
    workdir = tmp_path / "work"
    workdir.mkdir()

    env = dict(
        os.environ,
        HOME=str(home),
        TELEGRAM_BRIDGE_STATE_DIR=str(state),
        TELEGRAM_BRIDGE_TMUX=str(faketmux),
        TELEGRAM_BRIDGE_CLAUDE=str(fakeclaude),
        TELEGRAM_BRIDGE_BOT_TOKEN="testtoken",
        FAKE_TMUX_LOG=str(log),
    )
    r = subprocess.run(
        ["bash", str(SPAWN), "--attach", "9001", "--restore", "/dev/null", str(workdir)],
        capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr

    # Registry rewritten with the NEW pane binding; spawned carried forward (False),
    # transcript left for the router to backfill.
    reg = json.loads((state / "registry" / "9001.json").read_text())
    assert reg["thread_id"] == 9001
    assert reg["pane_id"] == "%99"
    assert reg["claude_pid"] == 4242
    assert reg["spawned"] is False             # carried forward from the prior entry
    assert reg["transcript_path"] == ""
    assert reg["cwd"] == str(workdir)

    logtext = log.read_text()
    assert "-P -F #{pane_id}" in logtext                                  # pane-id capture
    assert "set-option -p -t %99 @telegram_thread_id 9001" in logtext     # pane stamped
    assert "TELEGRAM_BRIDGE_THREAD_ID=9001" in logtext                    # race-free env binding
