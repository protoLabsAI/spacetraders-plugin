"""exploration.py pure helpers — host-free (like roles/analysis/watches)."""

from __future__ import annotations

import exploration as X


def _wp(symbol, x=0, y=0, traits=None, chart=None):
    w = {"symbol": symbol, "x": x, "y": y,
         "traits": [{"symbol": t} for t in (traits or [])]}
    if chart:
        w["chart"] = chart
    return w


# ── uncharted detection ─────────────────────────────────────────────────────────────

def test_uncharted_marker_and_empty_traits_count():
    assert X.is_uncharted(_wp("A", traits=["UNCHARTED"])) is True
    assert X.is_uncharted(_wp("B")) is True


def test_charted_or_traited_waypoints_are_skipped():
    assert X.is_uncharted(_wp("C", traits=["MARKETPLACE"])) is False
    assert X.is_uncharted(_wp("D", traits=["UNCHARTED"], chart={"submittedBy": "X"})) is False


def test_uncharted_filters_a_system():
    wps = [_wp("A", traits=["UNCHARTED"]), _wp("B", traits=["MARKETPLACE"]), _wp("C")]
    assert [w["symbol"] for w in X.uncharted(wps)] == ["A", "C"]


# ── sweep ordering ──────────────────────────────────────────────────────────────────

def test_sweep_is_nearest_neighbor_from_origin():
    origin = _wp("HOME", 0, 0)
    targets = [_wp("FAR", 100, 0), _wp("NEAR", 1, 0), _wp("MID", 10, 0)]
    assert [w["symbol"] for w in X.sweep_order(origin, targets)] == ["NEAR", "MID", "FAR"]


def test_sweep_chains_from_each_stop_not_origin():
    # After hopping to (10,0), (12,0) is closer than (5,5) — greedy chains position.
    origin = _wp("HOME", 0, 0)
    targets = [_wp("A", 10, 0), _wp("B", 12, 0), _wp("C", 5, 5)]
    order = [w["symbol"] for w in X.sweep_order(origin, targets)]
    assert order.index("B") == order.index("A") + 1


def test_sweep_handles_empty():
    assert X.sweep_order(_wp("HOME"), []) == []


# ── charted-by count ────────────────────────────────────────────────────────────────

def test_charted_by_counts_only_our_charts():
    wps = [_wp("A", chart={"submittedBy": "PROTO"}),
           _wp("B", chart={"submittedBy": "RIVAL"}),
           _wp("C")]
    assert X.charted_by(wps, "PROTO") == 1
    assert X.charted_by(wps, "proto") == 1   # case-insensitive
    assert X.charted_by(wps, "") == 0        # no agent → never counts


# ── construction gaps ───────────────────────────────────────────────────────────────

def test_construction_gaps_lists_only_shortfalls():
    site = {"materials": [
        {"tradeSymbol": "FAB_MATS", "required": 4000, "fulfilled": 1200},
        {"tradeSymbol": "ADVANCED_CIRCUITRY", "required": 1000, "fulfilled": 1000},
    ]}
    gaps = X.construction_gaps(site)
    assert gaps == [{"good": "FAB_MATS", "required": 4000, "fulfilled": 1200, "missing": 2800}]


def test_construction_gaps_handles_missing_site():
    assert X.construction_gaps({}) == []
    assert X.construction_gaps(None) == []


# ── campaign brief ──────────────────────────────────────────────────────────────────

def test_campaign_brief_is_a_bounded_checklist():
    brief = X.campaign_brief("X1-TEST", [_wp("X1-A"), _wp("X1-B")], probe="P-1")
    assert "X1-A → X1-B" in brief and "P-1" in brief
    assert "report" in brief.lower() and "do not trade" in brief.lower()
