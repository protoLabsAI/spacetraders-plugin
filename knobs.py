"""The fleet's control surface — runtime knobs + strategy presets + per-ship pins + the
decision log — built on the shared protoAgent SDK helpers (``graph.sdk``: ``Knobs``,
``DecisionLog``). Retrofitted from the hand-rolled ``_TUNABLE``/``set_knob``/``strategy.py``/
``_DECISIONS`` the plugin used to carry (protoAgent #1027/#1028).

The OODA strategist steers the deterministic engine through this surface; the engine reads
``KNOBS.get(...)`` LIVE each window, so a tune takes effect on the running autopilot
immediately. Host import (``graph.sdk``) — loaded only when the engine is, at runtime.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from graph.sdk import DecisionLog, Knobs

from . import roles as _roles

_log = logging.getLogger("spacetraders.knobs")

# The audit trail — every control-surface change the strategist makes (tune / strategy / pin).
DLOG = DecisionLog(cap=40)

# The tunable engine knobs (the strategist's dials), on the shared Knobs helper. Read live in
# the engine via KNOBS.get(name). `mining` is a knob too, so a preset can flip it.
KNOBS = (
    Knobs()
    .define("min_margin", 30, lo=0, help="cr/unit floor below which a route isn't worth the fuel")
    .define("route_max_age", 900.0, lo=30.0,
            help="only DISPATCH on prices fresher than this (s); stale legs aren't live routes")
    .define("buy_buffer", 600_000, lo=0,
            help="(legacy) extra comfort threshold for the light-hauler reinvest — reserve_floor "
                 "is now the canonical cash guard; kept for back-compat")
    .define("heavy_buffer", 1_500_000, lo=0,
            help="buy a long-range HEAVY_FREIGHTER above this (unlocks far contracts/routes)")
    .define("max_ships", 8, lo=1, help="cap on auto-bought fleet size")
    .define("probe_buffer", 150_000, lo=0, help="keep this reserve before scouting-buys")
    .define("map_target", 8, lo=1, help="markets in the price map before arbitrage surfaces")
    .define("max_probes", 5, lo=0, help="parallel scouts")
    .define("reserve_floor", 25_000, lo=0,
            help="the single hard cash floor — a reinvest never dips below it (goods buys are "
                 "separately capped by max_spend_frac); the one knob to keep a cushion")
    .define("max_spend_frac", 0.5, lo=0.1, hi=1.0,
            help="cap one trade's buy at this fraction of credits (working-capital guard; 1=off)")
    .define("window_minutes", 15.0, lo=1.0, help="autopilot window length / OODA cadence (min)")
    .define("max_drift_min", 30.0, lo=1.0,
            help="st_travel refuses an auto-DRIFT leg longer than this (min) — raise for reach")
    .define("sink_volume_mult", 1.0, lo=0.1, help="sell at most mult×(sink tradeVolume) per visit")
    .define("sink_supply_cutoff", "ABUNDANT",
            choices=["SCARCE", "LIMITED", "MODERATE", "HIGH", "ABUNDANT"],
            help="skip importers already saturated at/above this supply tier")
    .define("mining", True, help="mining-capable hulls mine (off = every hold trades)")
    .define("route_strikes", 2, lo=1,
            help="windows a hauler holds a route after it drops out of the fresh ranking")
    .define("strategist_cadence_min", 1440, lo=15,
            help="OODA tick cadence target (min) — daily by default; the strategist steers slowly")
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
_PREV_VALUE: dict = {}            # value each knob held BEFORE its last accepted change (anti-oscillation)


def _is_oscillation(before, after, prev) -> bool:
    """A tune oscillates if it REVERSES the knob's last change — it moves the value
    (after != before) right back to what it was before that change (after == prev). Bounded
    authority (ADR engine-rewrite stage 4): the strategist steers slowly and shouldn't flip-flop
    a knob before a window shows the effect. Pure — host-free testable."""
    return after != before and after == prev


# ── persistence ────────────────────────────────────────────────────────────────────────
# Knobs live in an in-memory KNOBS singleton, so a server RESTART or plugin reload would reset
# every tune back to the defaults (the systemic "buy_buffer keeps reverting to 600K"). Persist
# the tuned values to a per-agent state file next to the scoped secrets and reload them at
# startup, so a strategist's tunes survive restarts. (Per-ship pins are intentionally NOT
# persisted — a stale pin is what mis-mined at J58; they should reset on restart.)
def _state_path() -> Path:
    try:
        from graph.config_io import SECRETS_YAML_PATH   # the scoped, per-agent config dir
        return Path(SECRETS_YAML_PATH).parent / "spacetraders_knobs.json"
    except Exception:  # noqa: BLE001 — outside the host (tests / fresh_start)
        base = os.environ.get("PROTOAGENT_CONFIG_DIR") or str(Path.home() / ".protoagent")
        return Path(base) / "spacetraders_knobs.json"


def _save() -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"strategy": _STRATEGY["name"], "knobs": KNOBS.values()}))
    except Exception as e:  # noqa: BLE001 — persistence must never break a tune
        _log.debug("knob persist failed: %s", e)


def _load() -> None:
    try:
        p = _state_path()
        if not p.exists():
            return
        data = json.loads(p.read_text())
        for k, v in (data.get("knobs") or {}).items():
            KNOBS.set(k, v)               # coerces + clamps + ignores unknown knobs
        name = (data.get("strategy") or "").strip()
        if name in KNOBS.presets():
            _STRATEGY["name"] = name
        _log.info("restored persisted knobs (strategy=%s)", _STRATEGY["name"])
    except Exception as e:  # noqa: BLE001
        _log.debug("knob restore failed: %s", e)


_load()   # restore tuned values at import (when the engine first loads, at runtime)


def knobs() -> dict:
    """Current knob name -> value (audit / telemetry)."""
    return KNOBS.values()


def decisions() -> list:
    """The recent control-surface decision log (for st_report / the dashboard)."""
    return DLOG.entries()


def set_knob(name: str, value) -> str:
    """Tune one knob (typed-coerced, clamped, validated); log the change + persist it so it
    survives a restart. Bounded authority: a tune that immediately REVERTS the knob's last
    change is rejected (no flip-flopping — give a change a window to show its effect)."""
    before_all = KNOBS.values()
    before = before_all.get(name)
    msg = KNOBS.set(name, value)
    after_all = KNOBS.values()
    if after_all == before_all:                  # no-op / clamped-to-same / unknown knob
        return msg
    after = after_all.get(name)
    if name in _PREV_VALUE and _is_oscillation(before, after, _PREV_VALUE[name]):
        KNOBS.set(name, before)                  # restore — don't flip-flop within a window
        DLOG.record("tune", f"ignored oscillating {name} → {after} (reverts → {before})")
        return (f"ignored {name}={after}: it reverts the last change (was {before}). The engine "
                f"won't flip-flop a knob — give the change a window to show its effect first.")
    _PREV_VALUE[name] = before                   # remember what we changed away from
    DLOG.record("tune", msg)
    _save()
    return msg


def current_strategy() -> dict:
    """The active strategy preset + the live mining flag (for st_report / st_strategy)."""
    return {"name": _STRATEGY["name"], "mining": bool(KNOBS.get("mining"))}


def apply_strategy(name: str) -> str:
    """Switch to a named strategy preset (resets knobs to defaults, applies the preset's
    overrides incl. the `mining` flag); persisted. Re-applying the CURRENT strategy is a
    no-op so a redundant call doesn't wipe manual tunes back to the preset's values."""
    want = (name or "").strip()
    if want and want == _STRATEGY["name"] and want in KNOBS.presets():
        return (f"already on strategy {want} — knobs unchanged (st_tune to adjust, or "
                f"st_strategy a different preset to reset to its doctrine)")
    msg = KNOBS.apply_preset(name)
    if msg.startswith("unknown preset"):
        return msg
    _STRATEGY["name"] = want
    DLOG.record("strategy", f"→ {_STRATEGY['name']}")
    _save()
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
