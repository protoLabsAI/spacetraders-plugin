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


def test_knob_defaults_and_typed_set(knobs_mod):
    K = knobs_mod
    K.apply_strategy("balanced")  # known baseline
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
