"""Persistent fleet plan — the engine's memory ACROSS windows.

The stateless engine re-derived every ship's job from scratch each window, so probes
abandoned route hubs and haulers hopped routes — positions never stuck, so a route
never compounded (the "position sabotage" the hourly strategist fought and lost: each
window erased its fixes). This holds a PERSISTENT assignment reconciled INCREMENTALLY:

  * a hauler KEEPS its route until it's been unprofitable for ``route_strikes`` windows
    (hysteresis — one stale scan won't thrash it off a good route);
  * probes are STATIONED at the active route's two endpoints so both ends stay
    live-priced (the fix for the blind/stale-sink problem), surplus probes explore;
  * miners / siphoners keep their capability role (targets are picked by the engine).

The reconcile core is PURE (no I/O, no relative imports) so it unit-tests host-free
exactly like ``roles`` / ``analysis``. Persistence is a thin JSON file next to the
scoped knobs (same home as ``spacetraders_knobs.json``). Gated by the ``stable_plan``
knob — default off, so it A/Bs against the current dispatcher on a live fleet.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("spacetraders.plan")


def _route_key(r: dict | None):
    """Identity of a route — the good + both legs. None for a missing route."""
    if not r:
        return None
    return (r.get("good"), r.get("buy_at"), r.get("sell_at"))


def _bump(prev_ships: dict, sym: str, role: str) -> int:
    """Windows the ship has held this role: prior+1 if unchanged, else reset to 0."""
    pa = prev_ships.get(sym) or {}
    return (pa.get("since", 0) + 1) if pa.get("role") == role else 0


def reconcile(prev: dict | None, partition: dict, ranked: list, *, route_strikes: int = 2) -> dict:
    """Produce the next fleet plan from the previous one, the capability ``partition``
    (``roles.assign_roles`` output: probes / miners / siphoners / traders), and the freshly
    ``ranked`` routes — changing only what must change:

      * traders[0] → the contract lead (no fixed route);
      * each other trader KEEPS its prior route while that route is still in ``ranked``;
        if the route drops out, it's HELD for up to ``route_strikes`` windows (strike count)
        before being reassigned to the best unused fresh route — so a transient stale scan
        doesn't bounce a hauler off a good route (the anti-churn hysteresis);
      * probes STATION at the active route's buy/sell waypoints (the most-assigned route, else
        the top ranked), surplus probes scout;
      * miners / siphoners hold their capability role.

    Ships absent from ``partition`` are dropped (sold / gone). Pure — returns a dict
    ``{"ships": {sym: assignment}, "active": route|None}``; no I/O.
    """
    prev_ships = (prev or {}).get("ships", {}) if isinstance(prev, dict) else {}
    ranked = ranked or []
    by_key = {_route_key(r): r for r in ranked}
    new: dict[str, dict] = {}
    used: set = set()                       # routes already handed out this window (diversify)

    traders = partition.get("traders", []) or []
    for i, s in enumerate(traders):
        sym = s.get("symbol")
        if i == 0:                          # the lead works contracts (falls back to trade)
            new[sym] = {"role": "contract", "route": None, "station": None,
                        "strikes": 0, "since": _bump(prev_ships, sym, "contract")}
            continue
        pa = prev_ships.get(sym) or {}
        pr = pa.get("route")
        pk = _route_key(pr)
        if pr and pk in by_key and pk not in used:
            # still fresh + profitable → keep it, clear strikes
            used.add(pk)
            new[sym] = {"role": "trade", "route": by_key[pk], "station": None,
                        "strikes": 0, "since": _bump(prev_ships, sym, "trade")}
        elif pr and pk not in used and pa.get("strikes", 0) + 1 < route_strikes:
            # dropped out of the fresh ranking, but under the strike limit → HOLD one more
            # window (don't thrash off a route on a single stale/saturated scan)
            used.add(pk)
            new[sym] = {"role": "trade", "route": pr, "station": None,
                        "strikes": pa.get("strikes", 0) + 1, "since": _bump(prev_ships, sym, "trade")}
        else:
            # no usable prior route → take the best UNUSED fresh route (may be None → idle)
            route = next((r for r in ranked if _route_key(r) not in used), None)
            if route:
                used.add(_route_key(route))
            new[sym] = {"role": "trade", "route": route, "station": None,
                        "strikes": 0, "since": 0}

    # Active route = the one the most haulers are on (else the top ranked) — its endpoints
    # are what the probes hold live.
    counts: dict = {}
    routes_by_key: dict = {}
    for a in new.values():
        if a.get("route"):
            k = _route_key(a["route"])
            counts[k] = counts.get(k, 0) + 1
            routes_by_key[k] = a["route"]
    active = None
    if counts:
        active = routes_by_key[max(counts, key=lambda k: counts[k])]
    elif ranked:
        active = ranked[0]

    station_wps = [active["buy_at"], active["sell_at"]] if active else []
    for i, s in enumerate(partition.get("probes", []) or []):
        sym = s.get("symbol")
        if i < len(station_wps):
            new[sym] = {"role": "station", "route": None, "station": station_wps[i],
                        "strikes": 0, "since": _bump(prev_ships, sym, "station")}
        else:
            new[sym] = {"role": "scout", "route": None, "station": None,
                        "strikes": 0, "since": _bump(prev_ships, sym, "scout")}

    for s in partition.get("miners", []) or []:
        sym = s.get("symbol")
        new[sym] = {"role": "mine", "route": None, "station": None,
                    "strikes": 0, "since": _bump(prev_ships, sym, "mine")}
    for s in partition.get("siphoners", []) or []:
        sym = s.get("symbol")
        new[sym] = {"role": "siphon", "route": None, "station": None,
                    "strikes": 0, "since": _bump(prev_ships, sym, "siphon")}

    return {"ships": new, "active": active}


# ── persistence (thin; never breaks the engine) ─────────────────────────────────────────────
def _state_path() -> Path:
    try:
        from graph.config_io import SECRETS_YAML_PATH   # the scoped, per-agent config dir
        return Path(SECRETS_YAML_PATH).parent / "spacetraders_plan.json"
    except Exception:  # noqa: BLE001 — outside the host (tests / fresh_start)
        base = os.environ.get("PROTOAGENT_CONFIG_DIR") or str(Path.home() / ".protoagent")
        return Path(base) / "spacetraders_plan.json"


def load() -> dict:
    try:
        p = _state_path()
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception as e:  # noqa: BLE001
        _log.debug("plan load failed: %s", e)
        return {}


def save(pl: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(pl))
    except Exception as e:  # noqa: BLE001
        _log.debug("plan save failed: %s", e)


def clear() -> None:
    """Forget the plan — called on a universe reset (4113): the plan references per-reset
    waypoints + ship ids that no longer exist after a wipe, so it must NOT carry across
    (the learned-route memory in routes.py survives; the plan does not)."""
    try:
        p = _state_path()
        if p.exists():
            p.unlink()
    except Exception as e:  # noqa: BLE001
        _log.debug("plan clear failed: %s", e)
