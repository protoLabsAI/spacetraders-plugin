"""The fleet's control surface — runtime knobs + strategy presets + per-ship pins + the
decision log — built on the shared protoAgent SDK helpers (``graph.sdk``: ``Knobs``,
``DecisionLog``). Retrofitted from the hand-rolled ``_TUNABLE``/``set_knob``/``strategy.py``/
``_DECISIONS`` the plugin used to carry (protoAgent #1027/#1028).

The OODA strategist steers the deterministic engine through this surface; the engine reads
``KNOBS.get(...)`` LIVE each window, so a tune takes effect on the running autopilot
immediately. Host import (``graph.sdk``) — loaded only when the engine is, at runtime.
"""

from __future__ import annotations

from graph.sdk import DecisionLog, Knobs

from . import roles as _roles

# The audit trail — every control-surface change the strategist makes (tune / strategy / pin).
DLOG = DecisionLog(cap=40)

# The tunable engine knobs (the strategist's dials), on the shared Knobs helper. Read live in
# the engine via KNOBS.get(name). `mining` is a knob too, so a preset can flip it.
KNOBS = (
    Knobs()
    .define("min_margin", 30, lo=0, help="cr/unit floor below which a route isn't worth the fuel")
    .define("buy_buffer", 600_000, lo=0, help="reinvest a hauler once credits exceed this")
    .define("max_ships", 8, lo=1, help="cap on auto-bought fleet size")
    .define("probe_buffer", 150_000, lo=0, help="keep this reserve before scouting-buys")
    .define("map_target", 8, lo=1, help="markets in the price map before arbitrage surfaces")
    .define("max_probes", 5, lo=0, help="parallel scouts")
    .define("reserve_floor", 0, lo=0, help="hard cash floor — no auto-buy below this")
    .define("window_minutes", 15.0, lo=1.0, help="autopilot window length / OODA cadence (min)")
    .define("sink_volume_mult", 1.0, lo=0.1, help="sell at most mult×(sink tradeVolume) per visit")
    .define("sink_supply_cutoff", "ABUNDANT",
            choices=["SCARCE", "LIMITED", "MODERATE", "HIGH", "ABUNDANT"],
            help="skip importers already saturated at/above this supply tier")
    .define("route_diversify", 1, lo=0, hi=1, help="1=spread haulers across the top-N routes")
    .define("mining", True, help="mining-capable hulls mine (off = every hold trades)")
)
KNOBS.preset("balanced", {}, blurb="contracts seed, trade compounds, dedicated drones mine")
KNOBS.preset("trade-max",
             {"mining": False, "buy_buffer": 300_000, "min_margin": 20, "max_probes": 5},
             blurb="pure arbitrage — mining off, haulers sooner")
KNOBS.preset("mining", {"mining": True, "buy_buffer": 800_000, "max_probes": 3},
             blurb="mining-heavy — every mining-capable hull digs")
KNOBS.preset("contract-grind", {"mining": True, "buy_buffer": 800_000, "min_margin": 40},
             blurb="capital-safe — prioritise contracts, conservative reinvest")

_STRATEGY = {"name": "balanced"}   # active preset name (KNOBS holds the values, `mining` knob)
_OVERRIDES: dict = {}              # {ship_symbol: role} per-ship pins (st_assign)


def knobs() -> dict:
    """Current knob name -> value (audit / telemetry)."""
    return KNOBS.values()


def decisions() -> list:
    """The recent control-surface decision log (for st_report / the dashboard)."""
    return DLOG.entries()


def set_knob(name: str, value) -> str:
    """Tune one knob (typed-coerced, clamped, validated); log the change to the audit trail."""
    before = KNOBS.values()
    msg = KNOBS.set(name, value)
    if KNOBS.values() != before:                 # only a real change is a decision
        DLOG.record("tune", msg)
    return msg


def current_strategy() -> dict:
    """The active strategy preset + the live mining flag (for st_report / st_strategy)."""
    return {"name": _STRATEGY["name"], "mining": bool(KNOBS.get("mining"))}


def apply_strategy(name: str) -> str:
    """Switch to a named strategy preset (resets knobs to defaults, applies the preset's
    overrides incl. the `mining` flag). Takes effect on the running engine next window."""
    msg = KNOBS.apply_preset(name)
    if msg.startswith("unknown preset"):
        return msg
    _STRATEGY["name"] = (name or "").strip()
    DLOG.record("strategy", f"→ {_STRATEGY['name']}")
    return msg


def set_override(ship: str, role: str) -> str:
    """Pin a ship to a role (st_assign): mine|trade|contract|scout|idle, or auto to clear."""
    sym = (ship or "").upper()
    r = (role or "").lower()
    if r not in _roles.ROLE_NAMES:
        return f"unknown role {role!r}; valid: {', '.join(sorted(_roles.ROLE_NAMES))}"
    if r == "auto":
        _OVERRIDES.pop(sym, None)
        DLOG.record("assign", f"{sym} → auto (pin cleared)")
        return f"{sym}: override cleared → auto-classified"
    _OVERRIDES[sym] = r
    DLOG.record("assign", f"{sym} → {r}")
    return f"{sym}: pinned → {r} (overrides auto-classification next window)"


def overrides() -> dict:
    """The current per-ship role pins (for st_report / audit)."""
    return dict(_OVERRIDES)
