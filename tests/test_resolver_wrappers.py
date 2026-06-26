# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the two AskUserQuestion resolvers after the pane-keying cutover. The
# precedence/fallback logic lives in bridge_resolve (covered by
# test_bridge_resolve.py); these pin the wrapper-specific contract that survived
# the migration off the old cwd-only copies:
#
#   - both delegate the "which topic?" decision to bridge_resolve.resolve()
#   - the MCP returns the session DIR (inbox.parent); the hook returns the raw
#     inbox_path -- the two callers consume different shapes
#   - a None from resolve() (not a bridge session) propagates as None so the
#     caller abstains instead of mis-routing to a stranger's topic
#
# The modules are loaded by path (hyphenated filenames aren't importable names),
# with TELEGRAM_BRIDGE_STATE_DIR pointed at a tmp registry and resolve() stubbed
# so no test shells out to tmux or touches the real registry.

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _install_fake_mcp():
    """telegram-auq-mcp.py does `from mcp.server.fastmcp import FastMCP` -- a
    runtime-only dep absent from the dev group. Stub just the surface it touches
    (FastMCP with a no-op tool() decorator and run()) so the module imports."""
    if "mcp.server.fastmcp" in sys.modules:
        return

    class FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn

        def run(self):
            pass

    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = FastMCP
    mcp_mod.server = server_mod
    server_mod.fastmcp = fastmcp_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.fastmcp"] = fastmcp_mod


def _load(modname, filename, state_dir, monkeypatch):
    """Load a runtime module fresh with REGISTRY_DIR pointed at state_dir. The
    module binds REGISTRY_DIR from the env at import, so set it before exec."""
    monkeypatch.setenv("TELEGRAM_BRIDGE_STATE_DIR", str(state_dir))
    spec = importlib.util.spec_from_file_location(modname, RUNTIME / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def registry(tmp_path):
    """A state dir with a registry/ subdir; returns (state_dir, write_entry)."""
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()

    def write(tid, **fields):
        (reg_dir / "{}.json".format(tid)).write_text(
            json.dumps({"thread_id": tid, **fields}))

    return tmp_path, write


# --- AUQ hook: find_topic -> (thread_id, inbox_path) ------------------------

def test_auq_hook_find_topic_delegates(monkeypatch, registry):
    state_dir, write = registry
    write("2103", inbox_path="/x/sessions/2103/inbox.jsonl")
    mod = _load("auq_hook_ut1", "telegram-askuserquestion-hook.py", state_dir, monkeypatch)
    monkeypatch.setattr(mod.bridge_resolve, "resolve", lambda **kw: "2103")
    # Hook hands the caller the raw inbox_path (it derives read.offset itself).
    assert mod.find_topic() == ("2103", "/x/sessions/2103/inbox.jsonl")


def test_auq_hook_find_topic_none_when_not_bridge(monkeypatch, registry):
    state_dir, _ = registry
    mod = _load("auq_hook_ut2", "telegram-askuserquestion-hook.py", state_dir, monkeypatch)
    monkeypatch.setattr(mod.bridge_resolve, "resolve", lambda **kw: None)
    assert mod.find_topic() is None


def test_auq_hook_find_topic_none_when_registry_missing(monkeypatch, registry):
    # resolve() handed back a thread id but its registry entry is gone: don't
    # crash, return None so the caller abstains.
    state_dir, _ = registry
    mod = _load("auq_hook_ut3", "telegram-askuserquestion-hook.py", state_dir, monkeypatch)
    monkeypatch.setattr(mod.bridge_resolve, "resolve", lambda **kw: "9999")
    assert mod.find_topic() is None


# --- AUQ MCP: _find_topic -> (thread_id, session_dir) -----------------------

def test_auq_mcp_find_topic_returns_session_dir(monkeypatch, registry):
    _install_fake_mcp()
    state_dir, write = registry
    write("2164", inbox_path="/x/sessions/2164/inbox.jsonl")
    mod = _load("auq_mcp_ut1", "telegram-auq-mcp.py", state_dir, monkeypatch)
    monkeypatch.setattr(mod.bridge_resolve, "resolve", lambda **kw: "2164")
    # MCP wants the session DIR (where auq-pending/auq-answer live), i.e. parent.
    assert mod._find_topic() == ("2164", "/x/sessions/2164")


def test_auq_mcp_find_topic_none_when_not_bridge(monkeypatch, registry):
    _install_fake_mcp()
    state_dir, _ = registry
    mod = _load("auq_mcp_ut2", "telegram-auq-mcp.py", state_dir, monkeypatch)
    monkeypatch.setattr(mod.bridge_resolve, "resolve", lambda **kw: None)
    assert mod._find_topic() is None


def test_auq_mcp_find_topic_none_when_inbox_missing(monkeypatch, registry):
    # Registry entry exists but carries no inbox_path: no session dir to return.
    _install_fake_mcp()
    state_dir, write = registry
    write("2168")  # no inbox_path field
    mod = _load("auq_mcp_ut3", "telegram-auq-mcp.py", state_dir, monkeypatch)
    monkeypatch.setattr(mod.bridge_resolve, "resolve", lambda **kw: "2168")
    assert mod._find_topic() is None
