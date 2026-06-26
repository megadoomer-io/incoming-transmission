# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for /attach candidate labelling (issue #17) and the shared single/bulk
# bind helper:
#   _pane_label                 — friendly label is the cwd basename, not the
#                                 process-tracked window name or volatile pane title
#   list_unattached_claude_panes — labels by cwd basename; skips tg:/DEAD/non-claude
#   _attach_one                  — the one bind path both /attach <n> and /attach all
#                                 run (create topic + stamp + register + send-keys)
#
# telegram-router.py is stdlib-only and loads with no import side effects, so
# importlib loads it cleanly. _tmux / bridge_bind / subprocess are stubbed so
# nothing shells out or hits the network.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_ut", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- _pane_label: cwd basename, with fallbacks -----------------------------

def test_pane_label_uses_cwd_basename():
    mod = _load_router()
    assert mod._pane_label(
        "/Users/x/src/github.com/megadoomer-io/incoming-transmission", "zsh"
    ) == "incoming-transmission"


def test_pane_label_strips_trailing_slash():
    mod = _load_router()
    assert mod._pane_label("/Users/x/src/joey/", "zsh") == "joey"


def test_pane_label_falls_back_to_window_name_when_cwd_empty():
    mod = _load_router()
    assert mod._pane_label("", "my-window") == "my-window"


def test_pane_label_last_resort_is_session():
    mod = _load_router()
    assert mod._pane_label("", "") == "session"


# --- list_unattached_claude_panes: parse, label, filter --------------------

# Field order matches the router's format string:
#   #{pane_id}\t#{window_index}\t#{window_name}\t#{pane_current_command}\t#{pane_current_path}
LIST_SAMPLE = "\n".join([
    "%3\t1\tzsh\tclaude\t/Users/x/src/megadoomer-io/incoming-transmission",  # adopt
    "%29\t2\tzsh\tclaude\t/Users/x/src/missionlane/joey",                    # adopt
    "%4\t3\tbash\tbash\t/Users/x/other",        # not claude -> skip
    "%5\t4\ttg:work\tclaude\t/Users/x/work",    # bridge window -> skip
    "%6\t5\tDEAD - old\tclaude\t/Users/x/old",  # retired window -> skip
    "malformed-line-no-tabs",                   # ignored
])


def test_list_labels_by_cwd_and_filters(monkeypatch, tmp_path):
    mod = _load_router()
    monkeypatch.setattr(mod, "_tmux",
                        lambda *a: LIST_SAMPLE if a[:1] == ("list-panes",) else "")
    # Point REGISTRY_DIR at a non-existent dir so nothing reads as already-bridged.
    monkeypatch.setattr(mod, "REGISTRY_DIR", tmp_path / "registry")
    assert mod.list_unattached_claude_panes() == [
        ("%3", "1", "incoming-transmission"),
        ("%29", "2", "joey"),
    ]


def test_list_excludes_already_bridged(monkeypatch, tmp_path):
    mod = _load_router()
    monkeypatch.setattr(mod, "_tmux",
                        lambda *a: LIST_SAMPLE if a[:1] == ("list-panes",) else "")
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "2103.json").write_text('{"claude_pid": 555}')
    # claude_pid 555 resolves to pane %29 -> %29 is bridged, filtered out.
    monkeypatch.setattr(mod, "REGISTRY_DIR", reg)
    monkeypatch.setattr(mod, "_pane_for_claude_pid",
                        lambda pid: "%29" if pid == 555 else None)
    assert mod.list_unattached_claude_panes() == [
        ("%3", "1", "incoming-transmission"),
    ]


# --- _attach_one: the shared single/bulk bind path -------------------------

class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def _stub_attach(mod, monkeypatch, bind_result):
    """Wire up _tmux / bridge_bind.bind_pane / git so _attach_one runs without
    shelling out. Returns (sendkeys, binds) lists capturing the side effects."""
    sendkeys, binds = [], []

    def fake_tmux(*a):
        if a[:1] == ("display-message",):
            if a[-1] == "#{pane_current_path}":
                return "/Users/x/src/myproj\n"
            if a[-1] == "#{pane_pid}":
                return "12345\n"
        if a[:1] == ("send-keys",):
            sendkeys.append(a)
        return ""

    def fake_bind_pane(token, chat_id, pane_id, cwd, **kw):
        binds.append({"pane_id": pane_id, "cwd": cwd, "name": kw.get("name"),
                      "claude_pid": kw.get("claude_pid"), "spawned": kw.get("spawned")})
        return bind_result

    monkeypatch.setattr(mod, "_tmux", fake_tmux)
    monkeypatch.setattr(mod, "log", lambda *a, **k: None)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc("main\n"))
    monkeypatch.setattr(mod.bridge_bind, "bind_pane", fake_bind_pane)
    return sendkeys, binds


def test_attach_one_binds_and_sends_keys(monkeypatch):
    mod = _load_router()
    sendkeys, binds = _stub_attach(mod, monkeypatch, bind_result=999)
    tid = mod._attach_one(chat_id=42, target=("%7", "1", "myproj"))
    assert tid == 999
    # Bound the picked pane with cwd-derived name "myproj@main", as a user session.
    assert binds == [{"pane_id": "%7", "cwd": "/Users/x/src/myproj",
                      "name": "myproj@main", "claude_pid": "12345", "spawned": False}]
    # Sent the drain-only command then Enter into that pane.
    assert sendkeys == [
        ("send-keys", "-t", "%7", "-l", "/telegram-bridge"),
        ("send-keys", "-t", "%7", "Enter"),
    ]


def test_attach_one_returns_none_without_sending_keys_on_bind_failure(monkeypatch):
    mod = _load_router()
    sendkeys, binds = _stub_attach(mod, monkeypatch, bind_result=None)
    tid = mod._attach_one(chat_id=42, target=("%7", "1", "myproj"))
    assert tid is None
    assert len(binds) == 1          # attempted the bind
    assert sendkeys == []           # but no half-bind: no /telegram-bridge sent
