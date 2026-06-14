"""Trade-route ranking + saturation guard (analysis.py).

Pure (no imports), tests host-free (``import analysis``). Covers the supply-chain ranking
(margin × tradeVolume, export→import over exchange) and the saturation guard that skips
importers already glutted — the mechanism the sink_supply_cutoff knob drives.
"""

import analysis


def _mkt(wp, goods):
    return {"waypointSymbol": wp, "tradeGoods": goods}


def _good(symbol, gtype, *, purchase=None, sell=None, vol=10, supply="MODERATE"):
    return {"symbol": symbol, "type": gtype, "purchasePrice": purchase,
            "sellPrice": sell, "tradeVolume": vol, "supply": supply}


def test_export_to_import_route_is_found_and_scored():
    markets = [
        _mkt("A", [_good("IRON_ORE", "EXPORT", purchase=10, vol=60)]),
        _mkt("B", [_good("IRON_ORE", "IMPORT", sell=50, supply="SCARCE")]),
    ]
    best = analysis.best_route(markets, min_margin=1)
    assert best["good"] == "IRON_ORE"
    assert best["buy_at"] == "A" and best["sell_at"] == "B"
    assert best["profit_per_unit"] == 40
    assert best["score"] == 40 * 60   # margin × buy-leg tradeVolume


def test_throughput_beats_raw_spread():
    # A 10-margin route moving 1000 units must outrank a 50-margin route capped at 5.
    markets = [
        _mkt("X", [_good("FUEL", "EXPORT", purchase=90, vol=1000),
                   _good("GOLD", "EXPORT", purchase=10, vol=5)]),
        _mkt("Y", [_good("FUEL", "IMPORT", sell=100, supply="SCARCE"),
                   _good("GOLD", "IMPORT", sell=60, supply="SCARCE")]),
    ]
    ranked = analysis.rank_routes(markets, min_margin=1)
    assert ranked[0]["good"] == "FUEL"   # 10×1000 = 10000 > 50×5 = 250


def test_min_margin_filters_thin_routes():
    markets = [
        _mkt("A", [_good("SILICON", "EXPORT", purchase=48, vol=20)]),
        _mkt("B", [_good("SILICON", "IMPORT", sell=50, supply="LIMITED")]),
    ]
    assert analysis.best_route(markets, min_margin=30) == {}   # 2 cr/unit < floor
    assert analysis.best_route(markets, min_margin=1)["good"] == "SILICON"


def test_saturation_guard_skips_glutted_importers():
    # The only importer is already ABUNDANT (glutted) → no sellable sink → no route.
    markets = [
        _mkt("A", [_good("COPPER", "EXPORT", purchase=10, vol=30)]),
        _mkt("B", [_good("COPPER", "IMPORT", sell=80, supply="ABUNDANT")]),
    ]
    assert analysis.best_route(markets, min_margin=1, sink_supply_cutoff="ABUNDANT") == {}


def test_saturation_cutoff_is_tunable():
    markets = [
        _mkt("A", [_good("COPPER", "EXPORT", purchase=10, vol=30)]),
        _mkt("B", [_good("COPPER", "IMPORT", sell=80, supply="HIGH")]),
    ]
    # Default cutoff (ABUNDANT) keeps a HIGH sink; tightening to HIGH skips it.
    assert analysis.best_route(markets, min_margin=1, sink_supply_cutoff="ABUNDANT")["good"] == "COPPER"
    assert analysis.best_route(markets, min_margin=1, sink_supply_cutoff="HIGH") == {}


def test_exchange_is_fallback_only():
    # No export→import pair exists, so a cross-market EXCHANGE spread is used as last resort.
    markets = [
        _mkt("A", [_good("ANTIMATTER", "EXCHANGE", purchase=100, sell=90, vol=10)]),
        _mkt("B", [_good("ANTIMATTER", "EXCHANGE", purchase=160, sell=150, vol=10)]),
    ]
    best = analysis.best_route(markets, min_margin=1)
    assert best["kind"] == "exchange"
    assert best["buy_at"] == "A" and best["sell_at"] == "B"


def test_empty_markets_return_empty():
    assert analysis.best_route([]) == {}
    assert analysis.rank_routes([]) == []
