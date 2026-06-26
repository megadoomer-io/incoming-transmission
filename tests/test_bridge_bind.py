# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for bridge_bind — the shared programmatic bind (create topic + stamp pane +
# write registry) used by the router's /attach and the keyboard /telegram self-bind.
# bridge_bind is stdlib + importable by name (pythonpath=["runtime"]); urlopen,
# subprocess, and sleep are stubbed so nothing hits the network or tmux.

import json

import bridge_bind


class _Resp:
    """Minimal file-like for json.load (the code does json.load(urlopen(...)))."""
    def __init__(self, payload):
        self._p = json.dumps(payload)

    def read(self, *a):
        return self._p


# --- create_forum_topic ----------------------------------------------------

def test_create_forum_topic_returns_thread_id(monkeypatch):
    monkeypatch.setattr(bridge_bind.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"ok": True, "result": {"message_thread_id": 777}}))
    assert bridge_bind.create_forum_topic("tok", 123, "repo@main") == 777


def test_create_forum_topic_retries_then_none(monkeypatch):
    calls = []

    def boom(req, timeout=None):
        calls.append(1)
        raise OSError("network down")

    monkeypatch.setattr(bridge_bind.urllib.request, "urlopen", boom)
    assert bridge_bind.create_forum_topic("tok", 1, "x", retries=3, sleep=lambda *_: None) is None
    assert len(calls) == 3                       # exhausted all retries


def test_create_forum_topic_not_ok_returns_none(monkeypatch):
    monkeypatch.setattr(bridge_bind.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"ok": False, "description": "boom"}))
    assert bridge_bind.create_forum_topic("tok", 1, "x", retries=2, sleep=lambda *_: None) is None


# --- write_registry_entry --------------------------------------------------

def test_write_registry_entry_shape_and_roundtrip(tmp_path):
    reg = tmp_path / "registry"
    entry = bridge_bind.write_registry_entry(
        555, pane_id="%3", claude_pid="999", cwd="/x", spawned=True, registry_dir=str(reg))
    assert entry["thread_id"] == 555
    assert entry["pane_id"] == "%3"
    assert entry["claude_pid"] == 999            # coerced to int
    assert entry["cwd"] == "/x"
    assert entry["spawned"] is True
    assert entry["transcript_path"] == ""        # router backfills it
    assert entry["inbox_path"].endswith("/sessions/555/inbox.jsonl")
    on_disk = json.loads((reg / "555.json").read_text())
    assert on_disk == entry


def test_write_registry_entry_defaults(tmp_path):
    reg = tmp_path / "registry"
    entry = bridge_bind.write_registry_entry(42, pane_id="%9", cwd="/c", registry_dir=str(reg))
    assert entry["spawned"] is False             # default = adopt
    assert entry["claude_pid"] is None           # none given


# --- stamp_pane ------------------------------------------------------------

def test_stamp_pane_issues_set_option(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge_bind.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    assert bridge_bind.stamp_pane("%5", 2103, tmux_bin="tmux") is True
    assert calls == [["tmux", "set-option", "-p", "-t", "%5", "@telegram_thread_id", "2103"]]


def test_stamp_pane_empty_pane_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge_bind.subprocess, "run", lambda argv, **kw: calls.append(argv))
    assert bridge_bind.stamp_pane("", 2103) is False
    assert calls == []                           # nothing to stamp


# --- bind_pane (orchestration) ---------------------------------------------

def test_bind_pane_happy_path(monkeypatch, tmp_path):
    reg = tmp_path / "registry"
    stamped = []
    monkeypatch.setattr(bridge_bind, "create_forum_topic", lambda *a, **k: 2164)
    monkeypatch.setattr(bridge_bind, "stamp_pane", lambda p, t, **k: stamped.append((p, t)))
    tid = bridge_bind.bind_pane("tok", -100, "%7", "/repo", claude_pid="55",
                                name="repo@main", spawned=False, registry_dir=str(reg))
    assert tid == 2164
    assert stamped == [("%7", 2164)]             # pane stamped with the new tid
    entry = json.loads((reg / "2164.json").read_text())
    assert entry["pane_id"] == "%7" and entry["claude_pid"] == 55 and entry["spawned"] is False


def test_bind_pane_no_topic_no_halfbind(monkeypatch, tmp_path):
    reg = tmp_path / "registry"
    wrote = []
    monkeypatch.setattr(bridge_bind, "create_forum_topic", lambda *a, **k: None)
    monkeypatch.setattr(bridge_bind, "stamp_pane", lambda *a, **k: wrote.append("stamp"))
    monkeypatch.setattr(bridge_bind, "write_registry_entry", lambda *a, **k: wrote.append("reg"))
    tid = bridge_bind.bind_pane("tok", -100, "%7", "/repo", registry_dir=str(reg))
    assert tid is None
    assert wrote == []                           # topic failed -> nothing stamped or written
