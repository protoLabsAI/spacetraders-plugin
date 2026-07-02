"""lessons.py — the synthesis brief is pure (host-free); cadence/off/error paths are
exercised with the host on the path (the module leans on knobs + graph.sdk)."""

from __future__ import annotations

import lessons as L


def test_window_brief_carries_ground_truth_only():
    brief = L._window_brief(
        {"minutes": 15, "credits_start": 40_000, "credits_end": 47_500,
         "gained": 7_500, "per_hour": 30_000, "ships": 6},
        [{"action": "tune", "detail": "min_margin 30 → 20"}],
        {"min_margin": 20}, "balanced",
    )
    assert "40,000 → 47,500" in brief and "+7,500" in brief
    assert "tune: min_margin 30 → 20" in brief
    assert "balanced" in brief and "NO_LESSON" in brief


def test_window_brief_survives_missing_fields():
    brief = L._window_brief({}, [], {}, "trade-max")
    assert "- none" in brief and "trade-max" in brief
