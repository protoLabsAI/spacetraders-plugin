"""routes.py — epoch-stamped route memory over graph.sdk (host-free via a fake
graph.sdk in sys.modules; watches state via a tmp path). The epoch filter is the
v1.4.1 cross-wipe fix moved to the data layer: recall must NEVER return a route
from another universe, and legacy un-stamped rows count as another universe."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

import routes as R
import watches as W


@pytest.fixture()
def fake_sdk(monkeypatch, tmp_path):
    """Install a fake graph.sdk capturing knowledge calls; pin the epoch state file."""
    added: list[dict] = []
    rows: list[dict] = []

    async def knowledge_add(content, *, domain="general", heading=None):
        added.append({"content": content, "domain": domain, "heading": heading})
        return len(added)

    async def knowledge_search(query, *, k=5, domain=None):
        return rows

    graph = types.ModuleType("graph")
    sdk = types.ModuleType("graph.sdk")
    sdk.knowledge_add = knowledge_add
    sdk.knowledge_search = knowledge_search
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    monkeypatch.setattr(W, "_state_path", lambda: tmp_path / "state.json")
    return types.SimpleNamespace(added=added, rows=rows)


def _row(system="X1-AB", good="FUEL", buy="X1-AB-E1", sell="X1-AB-I2", margin=40, epoch=""):
    content = (f"Profitable trade route in {system}: buy {good} at {buy} → sell at {sell} "
               f"for +{margin} cr/unit. tag=route|{system}|{good}|{buy}|{sell}|{margin}"
               + (f" epoch={epoch}" if epoch else ""))
    return {"content": content}


def test_remember_stamps_the_current_epoch(fake_sdk):
    W.save_state({"reset_date": "2026-06-27"})
    asyncio.run(R.remember_route("X1-AB", "FUEL", "X1-AB-E1", "X1-AB-I2", 42.7))
    (entry,) = fake_sdk.added
    assert "tag=route|X1-AB|FUEL|X1-AB-E1|X1-AB-I2|42" in entry["content"]
    assert "epoch=2026-06-27" in entry["content"]
    assert entry["domain"] == "finding" and entry["heading"] == "trade-route:X1-AB:FUEL"


def test_recall_filters_to_the_current_epoch(fake_sdk):
    W.save_state({"reset_date": "2026-07-04"})
    fake_sdk.rows.extend([
        _row(good="FUEL", epoch="2026-07-04"),        # this universe → kept
        _row(good="IRON", epoch="2026-06-27"),        # wiped universe → dropped
        _row(good="GOLD"),                            # legacy, un-stamped → dropped
        _row(good="ALUMINUM", system="X9-ZZ", epoch="2026-07-04"),  # other system → dropped
    ])
    out = asyncio.run(R.recall_routes("X1-AB"))
    assert [r["good"] for r in out] == ["FUEL"]
    assert out[0] == {"good": "FUEL", "buy_at": "X1-AB-E1", "sell_at": "X1-AB-I2", "margin": 40}


def test_recall_dedupes_and_tolerates_no_epoch_state(fake_sdk):
    # No stored epoch (fresh install, watch not yet baselined) → no filter, legacy rows pass.
    fake_sdk.rows.extend([_row(), _row(), _row(good="IRON")])
    out = asyncio.run(R.recall_routes("X1-AB"))
    assert [r["good"] for r in out] == ["FUEL", "IRON"]


def test_recall_reads_preview_when_content_absent(fake_sdk):
    W.save_state({"reset_date": "2026-07-04"})
    row = _row(epoch="2026-07-04")
    fake_sdk.rows.append({"preview": row["content"]})
    assert len(asyncio.run(R.recall_routes("X1-AB"))) == 1


def test_host_free_paths_are_silent_noops(monkeypatch):
    # Without graph.sdk importable at all: remember no-ops, recall returns [].
    monkeypatch.setitem(sys.modules, "graph", None)     # force ImportError
    monkeypatch.setitem(sys.modules, "graph.sdk", None)
    asyncio.run(R.remember_route("X1-AB", "FUEL", "A", "B", 10))
    assert asyncio.run(R.recall_routes("X1-AB")) == []
