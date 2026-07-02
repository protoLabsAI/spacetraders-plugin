"""Exploration — charting, scanning, and construction math (SDK round-2, v2.0).

Pure helpers for the frontier campaign: which waypoints still need charting, the
cheapest sweep order for a probe, and what a construction site still needs. The
tools (`st_chart`, `st_scan_*`, `st_warp`, `st_construction*`) and the background
campaign wrapper live in tools.py / campaigns.py — these stay host-free-testable
(like roles.py / analysis.py).

Charting pays twice: an uncharted waypoint's traits are hidden until someone charts
it (the data unlocks markets/shipyards the price map can't see), and charts are the
`charted_count` goal-ladder rung. Construction supply (the jump-gate community goal)
is the long-horizon shared objective — `construction_gaps` turns the site status into
a shopping list the trade engine already knows how to fill.
"""

from __future__ import annotations

import math


def is_uncharted(waypoint: dict) -> bool:
    """A waypoint is uncharted when it carries no chart AND its traits are the single
    UNCHARTED marker (the API's shape for 'nobody has been here')."""
    if waypoint.get("chart"):
        return False
    traits = [t.get("symbol") for t in waypoint.get("traits", [])]
    return traits == ["UNCHARTED"] or not traits


def uncharted(waypoints: list[dict]) -> list[dict]:
    """The waypoints in a system that still need charting."""
    return [w for w in waypoints if is_uncharted(w)]


def sweep_order(origin: dict, targets: list[dict]) -> list[dict]:
    """Nearest-neighbor visit order from ``origin`` — greedy, not optimal, but a probe
    burns no fuel in CRUISE between close waypoints and the API pays per-visit, so
    'short hops first' is the whole requirement (an optimal TSP would be parity theater)."""
    remaining = list(targets)
    out: list[dict] = []
    x, y = origin.get("x", 0), origin.get("y", 0)
    while remaining:
        nxt = min(remaining, key=lambda w: math.dist((x, y), (w.get("x", 0), w.get("y", 0))))
        remaining.remove(nxt)
        out.append(nxt)
        x, y = nxt.get("x", 0), nxt.get("y", 0)
    return out


def charted_by(waypoints: list[dict], agent_symbol: str) -> int:
    """How many of these waypoints carry OUR chart (``chart.submittedBy`` — the goal
    ladder counts contributions, not visits)."""
    sym = (agent_symbol or "").upper()
    return sum(1 for w in waypoints
               if ((w.get("chart") or {}).get("submittedBy") or "").upper() == sym and sym)


def construction_gaps(construction: dict) -> list[dict]:
    """What a construction site still needs: ``[{good, required, fulfilled, missing}]``
    for every material short of its requirement. Empty when complete (or no site)."""
    out = []
    for m in (construction or {}).get("materials", []):
        req = int(m.get("required") or 0)
        got = int(m.get("fulfilled") or 0)
        if got < req:
            out.append({"good": m.get("tradeSymbol", "?"), "required": req,
                        "fulfilled": got, "missing": req - got})
    return out


def campaign_brief(system: str, targets: list[dict], *, probe: str) -> str:
    """The background explorer's marching orders — a bounded, concrete worklist (a
    detached worker with a vague brief wanders; one with a checklist finishes)."""
    hops = " → ".join(w.get("symbol", "?") for w in targets[:20])
    return (
        f"EXPLORATION CAMPAIGN in {system} with probe {probe}. Visit and chart each "
        f"waypoint in this order: {hops}. At each stop: st_chart (ignore 'already "
        "charted' errors — someone beat you to it), then st_scan_waypoints for anything "
        "adjacent worth noting. When the list is done (or a waypoint is unreachable "
        "twice), STOP and report: waypoints charted, notable traits found (markets, "
        "shipyards, asteroid fields, jump gates), and any error patterns. Do not trade, "
        "do not buy ships — chart, note, report."
    )
