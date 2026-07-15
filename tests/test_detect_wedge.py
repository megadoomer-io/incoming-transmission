# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for detect_wedge: the pure signature matcher that decides whether a
# bridged pane's tail looks like a native prompt the bridge can't answer over
# Telegram (a wedge). It must fire on a live selection menu / passphrase prompt
# and stay silent on content that merely DISPLAYS those strings in scrollback.
#
# Regression anchor: a rich AskUserQuestion (e.g. /plan-eng-review) renders a
# tall side-preview box beside the options, pushing the "❯ N." cursor line well
# above the "Esc to cancel" footer. The original 12-non-empty-line window
# dropped the cursor line, so the wedge went undetected and a phone-driven
# session sat stranded on the menu. The cursor is now searched over a wider
# window while the footer stays anchored to the last 3 lines.
#
# telegram-router.py is stdlib-only and loads with no import side effects, so
# importlib loads it cleanly; pyproject puts runtime/ on pythonpath for the
# bridge_bind import.

import importlib.util
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_router():
    spec = importlib.util.spec_from_file_location(
        "telegram_router_wedge_ut", RUNTIME / "telegram-router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


detect_wedge = _load_router().detect_wedge


# A rich AskUserQuestion frame: three options with a tall multi-line preview box
# rendered beside them, then the interactive footer. This mirrors the real
# /plan-eng-review menu that stranded a live session — the "❯ 1." cursor sits
# more than 12 non-empty lines above "Esc to cancel".
TALL_PREVIEW_MENU = """\
D2 — how far to take it now, and what do identifiers link to? <gstack-qid:demo>

❯ 1. Phase 1 now, defer links     ┌────────────────────────────────────┐
 2. Full send → UI links          │ Superseded by frozen-lynx  6943c65 │
                                  │                                    │
 3. Full send → commit links      │ (vs today: 6943c65271c4e5cb...)    │
   links                          │                                    │
                                  │ - codename + short id              │
                                  │ - ships clean pre-vacation         │
                                  │ - Phase 2 (UI link) deferred       │
                                  └────────────────────────────────────┘

                                  Notes: press n to add notes

──────────────────────────────────────────────────────────────────────
  Chat about this

Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel"""


SHORT_MENU = """\
Do you want to proceed?
❯ 1. Yes
 2. No
Enter to select · Esc to cancel"""


# The cursor points at the currently-selected option, not always option 1.
SELECTED_OPTION_7 = """\
Select a model
  1. Opus
❯ 7. Sonnet
Enter to select · Esc to cancel"""


PASSPHRASE = """\
Cloning into 'repo'...
Enter passphrase for key '/Users/x/.ssh/id_ed25519':"""


def test_tall_preview_menu_is_detected():
    # Regression: the wedge that a 12-line window missed.
    assert detect_wedge(TALL_PREVIEW_MENU) == "selection-prompt"


def test_cursor_is_more_than_12_nonempty_lines_above_footer():
    # Guards the regression's premise: if the fixture ever gets shorter than the
    # old window, the test above would pass even with the bug present.
    ne = [ln for ln in TALL_PREVIEW_MENU.splitlines() if ln.strip()]
    cursor = next(i for i, l in enumerate(ne) if l.lstrip().startswith("❯"))
    footer = next(i for i, l in enumerate(ne) if "esc to cancel" in l.lower())
    assert footer - cursor >= 12


def test_short_menu_is_detected():
    assert detect_wedge(SHORT_MENU) == "selection-prompt"


def test_selected_option_other_than_one_is_detected():
    assert detect_wedge(SELECTED_OPTION_7) == "selection-prompt"


def test_passphrase_prompt_is_detected():
    assert detect_wedge(PASSPHRASE) == "ssh-passphrase"


def test_footer_must_be_near_the_end():
    # "Esc to cancel" buried in scrollback (not the live footer) is NOT a wedge,
    # even with a "❯ N." cursor present above it. This is the anti-false-positive
    # guard the widened cursor search must not weaken.
    scrollback = "\n".join([
        "❯ 1. some captured option",
        "Enter to select · Esc to cancel",
    ] + ["output line {}".format(i) for i in range(10)])
    assert detect_wedge(scrollback) is None


def test_footer_without_cursor_is_not_a_wedge():
    # A page whose tail happens to end with the footer words but has no menu
    # cursor is content, not a live prompt.
    text = "\n".join(["some log line"] * 5 + ["press Esc to cancel the operation"])
    assert detect_wedge(text) is None


def test_empty_is_not_a_wedge():
    assert detect_wedge("") is None
    assert detect_wedge("   \n  \n") is None
