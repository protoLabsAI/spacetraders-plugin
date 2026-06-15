"""Fleet role-classification (roles.py).

The engine used to split the fleet on cargo capacity alone — "has a hold ⇒
trade/contract, else scout" — which mis-cast a mining drone (15-unit hold) as a hauler
and never ran it on the survey→extract loop. These cover the capability-based split that
fixes it, including the capital-base guard so the starting fleet's behaviour is preserved.

``roles`` has no relative imports, so it tests host-free (``import roles``) like
``client.py`` — no plugin package, no network.
"""

import roles


def _ship(symbol, *, cap, mounts=(), ):
    return {"symbol": symbol, "cargo": {"capacity": cap},
            "mounts": [{"symbol": m} for m in mounts]}


# Representative hulls (the mounts that matter for classification).
DRONE = _ship("DRONE-1", cap=15, mounts=["MOUNT_MINING_LASER_I"])
HAULER = _ship("HAULER-1", cap=40, mounts=["MOUNT_CARGO_HOLD_II"])
FRIGATE = _ship("FRIGATE-1", cap=40, mounts=["MOUNT_MINING_LASER_I", "MOUNT_SURVEYOR_I"])
PROBE = _ship("PROBE-1", cap=0, mounts=["MOUNT_SENSOR_ARRAY_I"])
SIPHON = _ship("SIPHON-1", cap=15, mounts=["MOUNT_GAS_SIPHON_I"])


# --- can_mine ----------------------------------------------------------------------

def test_can_mine_true_for_mining_laser():
    assert roles.can_mine(DRONE) is True
    assert roles.can_mine(FRIGATE) is True


def test_can_mine_false_for_hauler_probe_and_siphon():
    assert roles.can_mine(HAULER) is False
    assert roles.can_mine(PROBE) is False
    assert roles.can_mine(SIPHON) is False   # gas siphon ≠ ore mining


def test_can_mine_tolerates_missing_mounts():
    assert roles.can_mine({"symbol": "X"}) is False
    assert roles.can_mine({"symbol": "X", "mounts": None}) is False


# --- assign_roles ------------------------------------------------------------------

def test_probe_is_classified_by_zero_hold():
    r = roles.assign_roles([PROBE])
    assert [s["symbol"] for s in r["probes"]] == ["PROBE-1"]
    assert r["miners"] == [] and r["traders"] == []


def test_dedicated_drone_mines_when_a_hauler_covers_trade():
    # The original bug, fixed: a hauler is present for contracts/trade, so the drone
    # is free to MINE instead of being swept into the trade rotation.
    r = roles.assign_roles([HAULER, DRONE])
    assert [s["symbol"] for s in r["traders"]] == ["HAULER-1"]
    assert [s["symbol"] for s in r["miners"]] == ["DRONE-1"]


def test_frigate_plus_drone_keeps_frigate_on_contracts():
    # The live scenario (PROTOTRADERS): both hulls carry a mining laser. The roomier
    # frigate is drafted onto contracts (capital base); the dedicated drone mines.
    r = roles.assign_roles([FRIGATE, DRONE])
    assert [s["symbol"] for s in r["traders"]] == ["FRIGATE-1"]
    assert [s["symbol"] for s in r["miners"]] == ["DRONE-1"]


def test_lone_mine_haul_frigate_still_works_contracts():
    # Fresh agent: the only ship is the mine+haul frigate. Capital-base guard promotes
    # it to trader (today's proven behaviour) rather than mining with nothing on trade.
    r = roles.assign_roles([FRIGATE])
    assert [s["symbol"] for s in r["traders"]] == ["FRIGATE-1"]
    assert r["miners"] == []


def test_first_trader_is_the_contract_worker():
    # trader[0] (the contract worker) preserves acquisition order — first hauler leads.
    h2 = _ship("HAULER-2", cap=40)
    r = roles.assign_roles([HAULER, h2, DRONE])
    assert [s["symbol"] for s in r["traders"]] == ["HAULER-1", "HAULER-2"]
    assert [s["symbol"] for s in r["miners"]] == ["DRONE-1"]


def test_full_fleet_partitions_every_ship_once():
    fleet = [PROBE, HAULER, FRIGATE, DRONE]
    r = roles.assign_roles(fleet)
    classified = [s["symbol"] for grp in r.values() for s in grp]
    assert sorted(classified) == sorted(s["symbol"] for s in fleet)


# --- mining_enabled (the trade-max strategy lever) ---------------------------------

def test_mining_disabled_sends_every_hold_to_trade():
    r = roles.assign_roles([FRIGATE, DRONE, HAULER], mining_enabled=False)
    assert r["miners"] == []
    assert sorted(s["symbol"] for s in r["traders"]) == ["DRONE-1", "FRIGATE-1", "HAULER-1"]


def test_mining_disabled_still_scouts_probes():
    r = roles.assign_roles([PROBE, DRONE], mining_enabled=False)
    assert [s["symbol"] for s in r["probes"]] == ["PROBE-1"]
    assert [s["symbol"] for s in r["traders"]] == ["DRONE-1"]


# --- per-ship overrides (st_assign) ------------------------------------------------

def test_mine_pin_on_a_laserless_ship_routes_to_trade():
    # A "mine" pin can't make a laser-less HAULER mine (no extraction mount) — it would just
    # burn the rate budget on failed extracts (the J58 bug). It's routed to trade instead;
    # the DRONE (a real miner) mines.
    r = roles.assign_roles([HAULER, DRONE], overrides={"HAULER-1": "mine"})
    assert "HAULER-1" in [s["symbol"] for s in r["traders"]]
    assert "HAULER-1" not in [s["symbol"] for s in r["miners"]]
    assert [s["symbol"] for s in r["miners"]] == ["DRONE-1"]


def test_mine_pin_on_a_mining_capable_ship_is_honoured():
    # FRIGATE carries a mining laser, so its "mine" pin stands.
    r = roles.assign_roles([FRIGATE, HAULER], overrides={"FRIGATE-1": "mine"})
    assert [s["symbol"] for s in r["miners"]] == ["FRIGATE-1"]


def test_both_pinned_to_mine_only_the_laser_ship_mines():
    # Pin both: the DRONE (laser) mines; the HAULER (no laser) is routed to trade, not mining.
    r = roles.assign_roles([HAULER, DRONE], overrides={"HAULER-1": "mine", "DRONE-1": "mine"})
    assert [s["symbol"] for s in r["miners"]] == ["DRONE-1"]
    assert [s["symbol"] for s in r["traders"]] == ["HAULER-1"]


def test_override_idle_parks_a_ship():
    r = roles.assign_roles([HAULER, DRONE], overrides={"DRONE-1": "idle"})
    syms = [s["symbol"] for grp in r.values() for s in grp]
    assert "DRONE-1" not in syms          # parked — no job this window
    assert [s["symbol"] for s in r["traders"]] == ["HAULER-1"]


def test_override_contract_leads_the_trader_list():
    h2 = _ship("HAULER-2", cap=40)
    r = roles.assign_roles([HAULER, h2], overrides={"HAULER-2": "contract"})
    assert r["traders"][0]["symbol"] == "HAULER-2"   # pinned contract worker leads


def test_override_scout_parks_a_hauler_as_price_feed():
    r = roles.assign_roles([HAULER, DRONE], overrides={"HAULER-1": "scout"})
    assert [s["symbol"] for s in r["probes"]] == ["HAULER-1"]


def test_explicit_mine_pin_is_not_drafted_for_capital_base():
    # Pin the only cargo ship to mine: the capital-base guard must RESPECT it (no draft),
    # unlike the auto case where a lone frigate is drafted onto contracts.
    r = roles.assign_roles([FRIGATE], overrides={"FRIGATE-1": "mine"})
    assert [s["symbol"] for s in r["miners"]] == ["FRIGATE-1"]
    assert r["traders"] == []


# --- gas siphoning (st_assign siphon, pin-only) ------------------------------------
def test_can_siphon_reads_the_gas_siphon_mount():
    assert roles.can_siphon(SIPHON) is True
    assert roles.can_siphon(HAULER) is False
    assert roles.can_siphon(DRONE) is False        # a mining laser is not a gas siphon


def test_siphoners_key_present_and_empty_without_a_pin():
    # siphon is PIN-ONLY — an auto siphon-capable ship trades, it isn't auto-siphoned.
    r = roles.assign_roles([SIPHON, HAULER])
    assert r["siphoners"] == []
    assert "SIPHON-1" in [s["symbol"] for s in r["traders"]]


def test_siphon_pin_on_a_capable_ship():
    r = roles.assign_roles([SIPHON, HAULER], overrides={"SIPHON-1": "siphon"})
    assert [s["symbol"] for s in r["siphoners"]] == ["SIPHON-1"]
    assert "SIPHON-1" not in [s["symbol"] for s in r["traders"]]


def test_siphon_pin_on_a_non_siphon_ship_routes_to_trade():
    r = roles.assign_roles([HAULER, DRONE], overrides={"HAULER-1": "siphon"})
    assert r["siphoners"] == []
    assert "HAULER-1" in [s["symbol"] for s in r["traders"]]
