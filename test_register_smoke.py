"""register() smoke — the full contribution set, host-only (testkit).

The pure suites prove the predicates; THIS proves the wiring: ``register(FakeRegistry)``
must contribute every seam the manifest and docs/sdk-round2.md claim — eight goal
verifiers, watch hooks, own-bus subscriptions, routers (dashboard + gated data + the
test-connection route), crew subagents, and the toolset. Plus the two verifiers whose
logic spans an API call + persisted state (drawdown, reset_detected), run end-to-end
against a faked client.

Note: FakeRegistry doesn't yet mirror ``register_chat_command`` — the plugin's
``hasattr`` guard makes that a silent skip here (gap filed upstream).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

testkit = pytest.importorskip("graph.plugins.testkit")  # skip when protoAgent isn't on the path

ROOT = Path(__file__).resolve().parent

EXPECTED_VERIFIERS = {
    "spacetraders:credits", "spacetraders:fleet_size", "spacetraders:cargo_capacity",
    "spacetraders:net_worth", "spacetraders:drawdown", "spacetraders:reset_detected",
    "spacetraders:contract_deadline", "spacetraders:opportunity",
    "spacetraders:charted_count",
}


@pytest.fixture(scope="module")
def pkg():
    return testkit.load_plugin(ROOT, "spacetraders")


@pytest.fixture()
def reg(pkg):
    r = testkit.FakeRegistry({"token": "", "account_token": "", "call_sign": "", "faction": ""})
    pkg.register(r)
    return r


def test_register_contributes_the_full_set(reg):
    assert set(reg.verifiers) == EXPECTED_VERIFIERS
    # one watch-hook triple, all three reflexes wired
    assert len(reg.watch_hooks) == 1 and all(reg.watch_hooks[0])
    # own-bus housekeeping subscriptions
    assert set(reg.handlers) == {"spacetraders.window_closed", "spacetraders.reset_recovered"}
    # goal hook (stop + ladder), crew, engine surface, tools
    assert len(reg.goal_hooks) == 1
    assert len(reg.subagents) == 6          # crew + the v2.0 explorer
    assert reg.surfaces == ["spacetraders-fleet"]
    assert len(reg.tools) >= 46             # 38 + the v2.0 exploration/construction set
    # routers: dashboard page (default prefix), gated /api data router, test route ("")
    prefixes = [p for p, _ in reg.routers]
    assert prefixes.count(None) == 1
    assert "/api/plugins/spacetraders" in prefixes
    assert "" in prefixes


def _fake_call(responses: dict):
    async def call(method, path, **kw):
        key = f"{method} {path}"
        out = responses[key]
        if isinstance(out, Exception):
            raise out
        return out
    return call


def test_drawdown_verifier_ratchets_and_trips(pkg, reg, tmp_path, monkeypatch):
    import importlib

    watches = importlib.import_module(f"{pkg.__name__}.watches")
    client = importlib.import_module(f"{pkg.__name__}.client")
    monkeypatch.setattr(watches, "_state_path", lambda: tmp_path / "state.json")
    verify = reg.verifiers["spacetraders:drawdown"]
    spec = {"args": {"frac": 0.5}}

    monkeypatch.setattr(client, "call", _fake_call({"GET /my/agent": {"credits": 172_000}}))
    res = asyncio.run(verify(spec, None))
    assert res.met is False                       # first reading sets the mark, no trip
    assert watches.load_state()["high_water"] == 172_000

    monkeypatch.setattr(client, "call", _fake_call({"GET /my/agent": {"credits": 36_000}}))
    res = asyncio.run(verify(spec, None))          # the June shape: 172k → 36k
    assert res.met is True and "36,000" in res.reason


def test_reset_verifier_baselines_then_trips_and_rolls_epoch(pkg, reg, tmp_path, monkeypatch):
    import importlib

    watches = importlib.import_module(f"{pkg.__name__}.watches")
    client = importlib.import_module(f"{pkg.__name__}.client")
    monkeypatch.setattr(watches, "_state_path", lambda: tmp_path / "state.json")
    watches.save_state({"high_water": 99_000})     # epoch-scoped state that must not survive
    verify = reg.verifiers["spacetraders:reset_detected"]

    monkeypatch.setattr(client, "call", _fake_call({"GET /": {"resetDate": "2026-06-27"}}))
    assert asyncio.run(verify({"args": {}}, None)).met is False   # baseline, not a trip

    monkeypatch.setattr(client, "call", _fake_call({"GET /": {"resetDate": "2026-07-04"}}))
    res = asyncio.run(verify({"args": {}}, None))
    assert res.met is True and res.evidence == "2026-07-04"
    st = watches.load_state()                      # epoch rolled; high-water dropped with it
    assert st == {"reset_date": "2026-07-04"}

    res = asyncio.run(verify({"args": {}}, None))  # same epoch again → no re-trip storm
    assert res.met is False


def test_verifier_api_failure_is_a_readable_miss(reg, pkg, monkeypatch):
    import importlib

    client = importlib.import_module(f"{pkg.__name__}.client")
    monkeypatch.setattr(client, "call",
                        _fake_call({"GET /my/agent": RuntimeError("api down")}))
    res = asyncio.run(reg.verifiers["spacetraders:drawdown"]({"args": {}}, None))
    assert res.met is False and "api down" in res.reason
