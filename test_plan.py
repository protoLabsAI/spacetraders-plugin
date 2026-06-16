"""Persistent fleet-plan reconcile (plan.py).

The stateless engine re-planned every window, so positions never stuck (the "position
sabotage" the strategist fought). These cover the incremental reconcile that fixes it:
a hauler KEEPS its route while it's still fresh, is HELD through a strike before being
reassigned, probes STATION the active route's endpoints, and departed ships are dropped.

``plan.reconcile`` is pure (no I/O, no relative imports), so it tests host-free like
``roles`` / ``analysis`` — ``import plan``.
"""

import plan


def _ship(sym):
    return {"symbol": sym}


def _route(good, buy, sell, *, sell_price=200, sink_volume=10):
    return {"good": good, "buy_at": buy, "sell_at": sell,
            "profit_per_unit": 100, "sell_price": sell_price, "sink_volume": sink_volume}


R1 = _route("FUEL", "A", "B")
R2 = _route("IRON", "C", "D")


def _partition(*, probes=(), miners=(), siphoners=(), traders=()):
    return {"probes": [_ship(s) for s in probes], "miners": [_ship(s) for s in miners],
            "siphoners": [_ship(s) for s in siphoners], "traders": [_ship(s) for s in traders]}


def test_lead_trader_works_contracts_others_get_routes():
    pl = plan.reconcile({}, _partition(traders=["LEAD", "H1"]), [R1, R2])
    assert pl["ships"]["LEAD"]["role"] == "contract" and pl["ships"]["LEAD"]["route"] is None
    assert pl["ships"]["H1"]["role"] == "trade"
    assert pl["ships"]["H1"]["route"]["good"] == "FUEL"     # best unused fresh route


def test_haulers_diversify_across_distinct_routes():
    pl = plan.reconcile({}, _partition(traders=["LEAD", "H1", "H2"]), [R1, R2])
    goods = {pl["ships"]["H1"]["route"]["good"], pl["ships"]["H2"]["route"]["good"]}
    assert goods == {"FUEL", "IRON"}                        # not both stacked on R1


def test_route_is_kept_while_still_fresh():
    prev = {"ships": {"H1": {"role": "trade", "route": R1, "strikes": 0, "since": 3}}}
    pl = plan.reconcile(prev, _partition(traders=["LEAD", "H1"]), [R1, R2])
    assert pl["ships"]["H1"]["route"]["good"] == "FUEL"     # kept, not hopped to R2
    assert pl["ships"]["H1"]["strikes"] == 0
    assert pl["ships"]["H1"]["since"] == 4                  # held one more window


def test_dropped_route_is_held_one_window_then_reassigned():
    # R1 fell out of the fresh ranking; under route_strikes=2 it's HELD with a strike.
    prev = {"ships": {"H1": {"role": "trade", "route": R1, "strikes": 0, "since": 2}}}
    held = plan.reconcile(prev, _partition(traders=["LEAD", "H1"]), [R2], route_strikes=2)
    assert held["ships"]["H1"]["route"]["good"] == "FUEL"   # still on R1
    assert held["ships"]["H1"]["strikes"] == 1
    # next window it's still gone → strike hits the limit → reassign to the fresh route R2
    reassigned = plan.reconcile(held, _partition(traders=["LEAD", "H1"]), [R2], route_strikes=2)
    assert reassigned["ships"]["H1"]["route"]["good"] == "IRON"
    assert reassigned["ships"]["H1"]["strikes"] == 0


def test_probes_station_active_route_endpoints_then_scout():
    pl = plan.reconcile({}, _partition(probes=["P1", "P2", "P3"], traders=["LEAD", "H1"]), [R1])
    # active route = R1 (the only hauler is on it) → its two endpoints get stationed probes
    assert pl["active"]["good"] == "FUEL"
    assert pl["ships"]["P1"] == {"role": "station", "route": None, "station": "A",
                                 "strikes": 0, "since": 0}
    assert pl["ships"]["P2"]["station"] == "B"
    assert pl["ships"]["P3"]["role"] == "scout"             # surplus probe explores


def test_no_routes_means_all_probes_scout_and_haulers_idle():
    pl = plan.reconcile({}, _partition(probes=["P1", "P2"], traders=["LEAD", "H1"]), [])
    assert pl["active"] is None
    assert pl["ships"]["P1"]["role"] == "scout" and pl["ships"]["P2"]["role"] == "scout"
    assert pl["ships"]["H1"]["route"] is None              # nothing fresh → idle


def test_departed_ship_is_dropped():
    prev = {"ships": {"GONE": {"role": "trade", "route": R1, "strikes": 0, "since": 5},
                      "H1": {"role": "trade", "route": R2, "strikes": 0, "since": 1}}}
    pl = plan.reconcile(prev, _partition(traders=["LEAD", "H1"]), [R1, R2])
    assert "GONE" not in pl["ships"]                        # sold/lost ship not carried


def test_miners_and_siphoners_keep_capability_role():
    pl = plan.reconcile({}, _partition(miners=["M1"], siphoners=["S1"], traders=["LEAD"]), [R1])
    assert pl["ships"]["M1"]["role"] == "mine"
    assert pl["ships"]["S1"]["role"] == "siphon"
