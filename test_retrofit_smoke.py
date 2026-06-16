"""Engine retrofit smoke — the control surface on the shared protoAgent SDK helpers.

After the retrofit the engine modules (knobs.py, fleet.py) import ``graph.sdk`` (Knobs /
DecisionLog / supervise / telemetry), so they need the host on the path — which standalone
CI doesn't have. This test SKIPS there and runs when protoAgent is importable (its own test
env, or ``PYTHONPATH=<protoAgent> pytest`` locally), loading the plugin as a package via the
host testkit and exercising the full control surface. The pure-domain suites (test_roles /
test_analysis / test_reset_recovery) stay host-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

testkit = pytest.importorskip("graph.plugins.testkit")  # skip when protoAgent isn't on the path

ROOT = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def knobs_mod():
    pkg = testkit.load_plugin(ROOT, "spacetraders")
    import importlib

    return importlib.import_module(f"{pkg.__name__}.knobs")


@pytest.fixture(autouse=True)
def _isolate_knob_state(knobs_mod, tmp_path, monkeypatch):
    """Persist to a throwaway path (never the real per-agent config) and start each test from
    clean defaults — so the persistence/no-op behaviour is deterministic regardless of any
    state file on the machine."""
    monkeypatch.setattr(knobs_mod, "_state_path", lambda: tmp_path / "knobs.json")
    knobs_mod.KNOBS.reset()
    knobs_mod._STRATEGY["name"] = "balanced"
    yield


def test_knob_defaults_and_typed_set(knobs_mod):
    K = knobs_mod
    assert K.KNOBS.get("mining") is True and K.KNOBS.get("min_margin") == 30
    K.set_knob("min_margin", "15")            # number-as-string coerced
    assert K.KNOBS.get("min_margin") == 15
    K.set_knob("sink_supply_cutoff", "high")  # choice normalized
    assert K.KNOBS.get("sink_supply_cutoff") == "HIGH"


def test_strategy_presets_are_non_cumulative(knobs_mod):
    K = knobs_mod
    K.set_knob("min_margin", "99")
    K.apply_strategy("trade-max")
    assert K.KNOBS.get("mining") is False and K.KNOBS.get("buy_buffer") == 300_000
    assert K.KNOBS.get("min_margin") == 20            # not the cumulative 99
    K.apply_strategy("balanced")
    assert K.KNOBS.get("min_margin") == 30 and K.KNOBS.get("mining") is True


def test_per_ship_pin_and_decision_log(knobs_mod):
    K = knobs_mod
    K.set_override("PROTOTRADERS-4", "mine")
    assert K.overrides().get("PROTOTRADERS-4") == "mine"
    K.set_override("PROTOTRADERS-4", "auto")
    assert "PROTOTRADERS-4" not in K.overrides()
    actions = {d["action"] for d in K.decisions()}
    assert {"tune", "strategy", "assign"} <= actions      # the audit trail captured all three


def test_engine_status_without_a_running_engine(knobs_mod):
    import importlib

    fleet = importlib.import_module(knobs_mod.__name__.rsplit(".", 1)[0] + ".fleet")
    st = fleet.ops_status()                                # supervise-backed status, no engine yet
    assert st["running"] is False and st["watchdog"] is False
    assert st["started_minutes"] == fleet.KNOBS.get("window_minutes")


def test_knobs_persist_across_a_restart(knobs_mod):
    # The systemic "buy_buffer reverts to 600K": a tune must survive a restart, not reset.
    K = knobs_mod
    K.set_knob("buy_buffer", "23000")                      # tuned + saved to the isolated path
    assert K.KNOBS.get("buy_buffer") == 23_000
    K.KNOBS.reset()                                        # simulate a restart wiping in-memory state
    assert K.KNOBS.get("buy_buffer") == 600_000
    K._load()                                              # startup restore from the state file
    assert K.KNOBS.get("buy_buffer") == 23_000            # the tune survived


def test_demotion_knobs_present(knobs_mod):
    # Strategist-demotion + persistent-plan knobs are registered with safe defaults. The
    # greenfield removed stable_plan (the plan is always-on now) and route_diversify (the
    # reconcile diversifies haulers across distinct routes itself).
    K = knobs_mod
    assert K.KNOBS.get("strategist_cadence_min") == 1440   # daily — the strategist steers slowly
    assert K.KNOBS.get("route_strikes") == 2
    assert "stable_plan" not in K.KNOBS.values()           # removed — plan is the engine
    assert "route_diversify" not in K.KNOBS.values()       # removed — superseded by reconcile


def test_cash_policy_reserve_floor_default_and_buy_buffer_legacy(knobs_mod):
    # Unified cash policy: reserve_floor is the single hard floor with a sensible default;
    # buy_buffer stays functional (legacy back-compat) so presets / persisted state don't break.
    K = knobs_mod
    assert K.KNOBS.get("reserve_floor") == 25_000
    K.set_knob("buy_buffer", "120000")
    assert K.KNOBS.get("buy_buffer") == 120_000


def test_oscillating_tune_is_rejected(knobs_mod):
    # Bounded authority: a tune that reverts the knob's last change is refused (no flip-flopping).
    K = knobs_mod
    K._PREV_VALUE.clear()
    assert K.KNOBS.get("min_margin") == 30
    K.set_knob("min_margin", "15")                         # 30 → 15 (accepted)
    assert K.KNOBS.get("min_margin") == 15
    out = K.set_knob("min_margin", "30")                   # 15 → 30 would revert the last change
    assert "ignored" in out.lower()
    assert K.KNOBS.get("min_margin") == 15                 # rejected — value held, not flipped back
    K.set_knob("min_margin", "40")                         # a DISTINCT value is still allowed
    assert K.KNOBS.get("min_margin") == 40


def test_reapplying_current_strategy_keeps_tunes(knobs_mod):
    # A redundant st_strategy(current) must not wipe manual tunes back to the preset's values.
    K = knobs_mod
    K.set_knob("buy_buffer", "23000")
    out = K.apply_strategy("balanced")                     # already on balanced → no-op
    assert "already on" in out
    assert K.KNOBS.get("buy_buffer") == 23_000            # NOT reset to 600K
    # but switching to a DIFFERENT preset still resets to its doctrine
    K.apply_strategy("mining")
    assert K.KNOBS.get("buy_buffer") == 800_000
