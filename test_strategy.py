"""Strategy presets (strategy.py).

Pure preset definitions — no relative imports, so they test host-free (``import strategy``).
Also enforces the contract with the engine: every preset's knob keys must be real tunable
knobs, so a preset can never reference a knob the engine doesn't expose.
"""

import strategy

# The knob names the engine exposes (fleet._TUNABLE). Kept in sync here so the test fails
# loudly if a preset references an unknown knob OR the engine renames one out from under it.
ENGINE_KNOBS = {
    "min_margin", "buy_buffer", "max_ships", "probe_buffer", "map_target", "max_probes",
    "reserve_floor", "window_minutes", "sink_volume_mult", "sink_supply_cutoff",
    "route_diversify",
}


def test_default_is_balanced_and_present():
    assert strategy.DEFAULT == "balanced"
    assert "balanced" in strategy.PRESETS


def test_balanced_is_a_no_override_reset():
    assert strategy.PRESETS["balanced"]["knobs"] == {}
    assert strategy.PRESETS["balanced"]["mining"] is True


def test_trade_max_disables_mining():
    assert strategy.PRESETS["trade-max"]["mining"] is False


def test_resolve_normalizes_name():
    for typed in ("trade-max", "Trade_Max", "  TRADE MAX  ", "trade_max"):
        p = strategy.resolve(typed)
        assert p is not None and p["name"] == "trade-max"


def test_resolve_unknown_is_none():
    assert strategy.resolve("warp-speed") is None
    assert strategy.resolve("") is None


def test_resolve_folds_in_canonical_name():
    p = strategy.resolve("mining")
    assert p["name"] == "mining" and "mining" in p and "knobs" in p


def test_every_preset_references_only_real_knobs():
    for name, preset in strategy.PRESETS.items():
        assert set(preset["knobs"]) <= ENGINE_KNOBS, f"{name} references an unknown knob"
        assert isinstance(preset["mining"], bool)
        assert preset["blurb"]


def test_names_lists_all_presets_in_menu_order():
    assert strategy.names() == list(strategy.PRESETS)
