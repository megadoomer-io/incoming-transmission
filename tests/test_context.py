# ----------------------------------------------------------------------------
# "THE BEER-WARE LICENSE" (Revision 42):
# Mike Dougherty owns this file. As long as you retain this notice you
# can do whatever you want with this stuff. If we meet some day, and you think
# this stuff is worth it, you can buy me a beer in return.
# ----------------------------------------------------------------------------
#
# Tests for telegram-context.compute: the pure function that turns a session
# transcript into a context-occupancy status dict (pct/tokens/window/model).
#
# Regression anchor: Claude Code (update 2026-08-12) dropped the "[1m]" tier
# suffix from the model id — the bridged session now records "claude-opus-4-8"
# with no tier marker. The old detection keyed on the "[1m]" substring, so a
# session below 200k on a real 1m window was sized against the 200k default,
# inflating pct up to ~5x and tripping auto-compaction (trigger_pct 0.85) almost
# immediately. compute() must now size any opus-4-8 id as the 1m window.

import importlib.util
import json
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


def _load_context():
    spec = importlib.util.spec_from_file_location(
        "telegram_context_ut", RUNTIME / "telegram-context.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ctx = _load_context()


def _write_transcript(tmp_path, turns):
    """turns: list of (model, input_tokens) -> assistant JSONL lines."""
    p = tmp_path / "transcript.jsonl"
    with open(p, "w") as fh:
        for model, toks in turns:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": model,
                    "usage": {
                        "input_tokens": toks,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 500,
                    },
                },
            }) + "\n")
    return p


def test_opus_4_8_without_1m_suffix_is_wide(tmp_path):
    # The bug: below 200k on a real 1m window, sized against the default.
    p = _write_transcript(tmp_path, [("claude-opus-4-8", 170_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW
    assert data["pct"] == round(170_000 / 1_000_000, 4)  # 0.17, not 0.85


def test_opus_4_8_with_legacy_1m_suffix_still_wide(tmp_path):
    p = _write_transcript(tmp_path, [("claude-opus-4-8[1m]", 170_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW


def test_matching_is_case_insensitive(tmp_path):
    p = _write_transcript(tmp_path, [("Claude-Opus-4-8", 50_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW


def test_unknown_model_below_default_stays_narrow(tmp_path):
    p = _write_transcript(tmp_path, [("claude-sonnet-4-5-20250929", 50_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.DEFAULT_WINDOW
    assert data["pct"] == round(50_000 / 200_000, 4)


def test_unknown_model_over_default_trips_wide_fallback(tmp_path):
    # Safety net: any model already past 200k must be on a wide window.
    p = _write_transcript(tmp_path, [("some-future-model", 250_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW


def test_latest_usage_turn_wins(tmp_path):
    p = _write_transcript(tmp_path, [
        ("claude-opus-4-8", 100_000),
        ("claude-opus-4-8", 180_000),
    ])
    data = ctx.compute(p)
    assert data["tokens"] == 180_000
    assert data["msgs"] == 2


def test_no_usage_returns_none(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert ctx.compute(p) is None


# Claude 5 family: opus-5-x and fable-5-x run a 1m window too (transcripts seen
# at 416k / 261k), but the allowlist only knew opus-4-8, so they were sized at
# 200k and rolled over at ~170k.
def test_opus_5_is_wide(tmp_path):
    p = _write_transcript(tmp_path, [("claude-opus-5-5", 170_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW


def test_fable_5_is_wide(tmp_path):
    p = _write_transcript(tmp_path, [("claude-fable-5-1", 170_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW


def test_model_windows_config_overrides_guess(tmp_path, monkeypatch):
    cfg = tmp_path / "compaction.json"
    cfg.write_text(json.dumps({"model_windows": {"opus-5-5": 500_000}}))
    monkeypatch.setattr(ctx, "CONFIG_FILE", cfg)
    p = _write_transcript(tmp_path, [("claude-opus-5-5", 100_000)])
    data = ctx.compute(p)
    assert data["window"] == 500_000
    assert data["pct"] == round(100_000 / 500_000, 4)


def test_bad_config_falls_back_to_guess(tmp_path, monkeypatch):
    cfg = tmp_path / "compaction.json"
    cfg.write_text("{not json")
    monkeypatch.setattr(ctx, "CONFIG_FILE", cfg)
    p = _write_transcript(tmp_path, [("claude-opus-5-5", 100_000)])
    data = ctx.compute(p)
    assert data["window"] == ctx.WIDE_WINDOW
