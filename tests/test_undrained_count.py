# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for the router's undrained_count: the backstop's "is anything waiting?"
# signal. A read.offset past the end of a recreated inbox (issue #32) must count
# the whole inbox as waiting, matching telegram-inbox.sh drain's reset to 0, so
# the backstop keeps nudging instead of treating stranded lines as drained.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_uc", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reg(tmp_path, lines, offset=None):
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text("".join('{"text":"%d"}\n' % i for i in range(lines)))
    if offset is not None:
        (tmp_path / "read.offset").write_text("%d\n" % offset)
    return {"inbox_path": str(inbox)}


def test_counts_lines_after_offset(tmp_path):
    assert _load_router().undrained_count(_reg(tmp_path, 3, offset=1)) == 2


def test_fully_drained_is_zero(tmp_path):
    assert _load_router().undrained_count(_reg(tmp_path, 2, offset=2)) == 0


def test_no_offset_counts_everything(tmp_path):
    assert _load_router().undrained_count(_reg(tmp_path, 2)) == 2


def test_offset_past_end_counts_whole_inbox(tmp_path):
    assert _load_router().undrained_count(_reg(tmp_path, 1, offset=576)) == 1
