"""Strategy presets — named doctrines that retune the engine in one move.

A "strategy" is a curated set of engine-knob overrides + a mining toggle the operator
picks per run (``st_strategy``), instead of nudging single knobs with ``st_tune``. It
answers "which doctrine is the fleet running this reset?" — pure arbitrage, mining-heavy,
contract-safe, or the balanced default.

Pure + dependency-free (no relative imports), so it unit-tests host-free exactly like
``client.py`` / ``roles.py``. ``fleet.apply_strategy()`` pushes a preset's ``knobs``
through the existing ``set_knob`` path and records its ``mining`` flag, which
``autopilot()`` reads when it calls ``roles.assign_roles``.

Knob keys must be members of ``fleet._TUNABLE`` (min_margin, buy_buffer, max_ships,
probe_buffer, map_target, max_probes); ``test_strategy`` enforces that so a preset can
never reference a knob the engine doesn't have.
"""

from __future__ import annotations

# Each preset: a one-line blurb, a ``mining`` toggle (False ⇒ every hold trades), and
# knob OVERRIDES (any omitted knob keeps the engine default). ``balanced`` is today's
# behaviour verbatim — no overrides, mining on — so it's a safe "reset to default".
PRESETS: dict[str, dict] = {
    "balanced": {
        "blurb": "Default — contracts seed, trade compounds, dedicated drones mine.",
        "mining": True,
        "knobs": {},
    },
    "trade-max": {
        "blurb": "Pure arbitrage — mining off, every hold trades, haulers bought sooner.",
        "mining": False,
        # Lower the hauler buffer so the first trader lands fast (a hauler is ~290–390k,
        # and a weekly reset rewards getting capital onto routes early), and drop the
        # margin floor a touch to surface more routes in a thin map.
        "knobs": {"buy_buffer": 300_000, "min_margin": 20, "max_probes": 5},
    },
    "mining": {
        "blurb": "Mining-heavy — every mining-capable hull digs; a hauler trades/sells.",
        "mining": True,
        # Keep capital in the existing drones rather than diverting it to haulers/probes;
        # the saturation damping in the trade legs protects the ore market either way.
        "knobs": {"buy_buffer": 800_000, "max_probes": 3},
    },
    "contract-grind": {
        "blurb": "Capital-safe — prioritise contracts, conservative reinvestment.",
        "mining": True,
        "knobs": {"buy_buffer": 800_000, "min_margin": 40},
    },
}

DEFAULT = "balanced"


def _canon(name: str) -> str:
    """Normalise a user-typed name: case/space/underscore-insensitive (``Trade_Max`` →
    ``trade-max``)."""
    return (name or "").strip().lower().replace("_", "-").replace(" ", "-")


def names() -> list:
    """The preset names, in menu order."""
    return list(PRESETS)


def resolve(name: str) -> dict | None:
    """The preset dict for ``name`` (normalised), with its canonical ``name`` folded in,
    or ``None`` if unknown."""
    key = _canon(name)
    preset = PRESETS.get(key)
    return {"name": key, **preset} if preset else None
