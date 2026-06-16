"""
SpaceTraders market analysis — supply-chain trade routing.

The sustained money in SpaceTraders v2 is the SUPPLY CHAIN, not random arbitrage.
A market EXPORTS a good (type=="EXPORT") when it produces it: supply runs
HIGH/ABUNDANT, the purchase price is cheap, and it REFILLS every cycle. Another
market IMPORTS that good (type=="IMPORT") when it consumes it: supply runs SCARCE,
the sell price is dear, and the demand refills every cycle too. Pairing those —
buy where it's exported, sell where it's imported — is a route that keeps paying.

Random buy-low/sell-high on arbitrary goods looks great on paper but saturates
fast: every trade moves the price (capped by tradeVolume), so a 50% spread dies in
two trades while a 10% export->import route refills forever. So we DON'T rank by raw
spread — we rank by margin × tradeVolume (per-cycle throughput): a 10% route moving
60 units beats a 50% route capped at 5.
"""


# Supply tiers, ordered cheapest-to-buy / most-saturated-to-sell-into.
_SUPPLY_ORDER = {"SCARCE": 0, "LIMITED": 1, "MODERATE": 2, "HIGH": 3, "ABUNDANT": 4}


def _supply_tier(supply) -> int:
    """Numeric supply tier (0=SCARCE … 4=ABUNDANT); unknown sorts as MODERATE."""
    return _SUPPLY_ORDER.get((supply or "").upper(), 2)


def rank_routes(markets: list[dict], min_margin: int = 1,
                sink_supply_cutoff: str = "ABUNDANT") -> list[dict]:
    """All profitable supply-chain routes, ranked best-first (same route shape as
    ``best_route``). ``sink_supply_cutoff`` is the saturation guard: an importer whose
    supply is already AT OR ABOVE this tier is skipped as a sell target — it's saturated,
    won't pay the premium, and dumping into it just craters the price further (the #1
    documented way an unattended bot crashes its own market). Default ABUNDANT skips only
    the most saturated sinks; the strategist can tighten it to HIGH/MODERATE via st_tune.

    Returns ``[]`` if nothing clears ``min_margin``. Used by the engine to diversify
    several haulers across the top-N routes instead of stacking them all on one (which
    would saturate it); ``best_route`` returns just the top entry.
    """
    if not markets:
        return []
    cutoff = _supply_tier(sink_supply_cutoff)

    exports: dict[str, list] = {}    # good -> [(purchasePrice, waypoint, tradeVolume, supply)]
    imports: dict[str, list] = {}    # good -> [(sellPrice, waypoint, supply, tradeVolume)]
    exchanges: dict[str, list] = {}  # good -> [(purchasePrice, sellPrice, waypoint, tradeVolume)]
    for market in markets:
        waypoint = market.get("waypointSymbol")
        if not waypoint:
            continue
        for good in (market.get("tradeGoods") or []):
            symbol = good.get("symbol")
            if not symbol:
                continue
            gtype = good.get("type")
            bp = good.get("purchasePrice")
            sp = good.get("sellPrice")
            vol = good.get("tradeVolume") or 0
            supply = good.get("supply")
            if gtype == "EXPORT":
                if bp is None:
                    continue
                exports.setdefault(symbol, []).append((bp, waypoint, vol, supply))
            elif gtype == "IMPORT":
                if sp is None or _supply_tier(supply) >= cutoff:   # saturation guard
                    continue
                imports.setdefault(symbol, []).append((sp, waypoint, supply, vol))
            elif gtype == "EXCHANGE":
                if bp is None or sp is None:
                    continue
                exchanges.setdefault(symbol, []).append((bp, sp, waypoint, vol))

    primary: list[dict] = []
    for good in set(exports) & set(imports):
        buy_price, buy_wp, buy_vol, _ = min(exports[good], key=lambda x: x[0])
        sell_price, sell_wp, _, sink_vol = max(imports[good], key=lambda x: x[0])
        if buy_wp == sell_wp:
            continue
        margin = int(sell_price - buy_price)
        if margin < min_margin:
            continue
        primary.append({"good": good, "buy_at": buy_wp, "sell_at": sell_wp,
                        "profit_per_unit": margin, "volume": buy_vol,
                        "buy_price": buy_price, "sell_price": sell_price,
                        "sink_volume": sink_vol, "kind": "export→import",
                        "score": margin * max(buy_vol, 1)})
    if primary:
        return sorted(primary, key=lambda r: r["score"], reverse=True)

    # FALLBACK: cross-market EXCHANGE spreads (no refilling supply chain available).
    fallback: list[dict] = []
    for good, listings in exchanges.items():
        for buy_price, _, buy_wp, buy_vol in listings:
            for _, sell_price, sell_wp, sell_vol in listings:
                if buy_wp == sell_wp:
                    continue
                margin = int(sell_price - buy_price)
                if margin < min_margin:
                    continue
                fallback.append({"good": good, "buy_at": buy_wp, "sell_at": sell_wp,
                                 "profit_per_unit": margin, "volume": buy_vol,
                                 "buy_price": buy_price, "sell_price": sell_price,
                                 # the SINK is the sell waypoint — size the saturation cap
                                 # against ITS tradeVolume, not the buy leg's.
                                 "sink_volume": sell_vol, "kind": "exchange",
                                 "score": margin * max(buy_vol, 1)})
    return sorted(fallback, key=lambda r: r["score"], reverse=True)


def best_route(markets: list[dict], min_margin: int = 1,
               sink_supply_cutoff: str = "ABUNDANT") -> dict:
    """
    Find the best SUPPLY-CHAIN trade route across a list of markets.

    Args:
        markets: List of market dicts, each of the form:
            {
                'waypointSymbol': str,
                'tradeGoods': [
                    {
                        'symbol': str,
                        'type': str,          # EXPORT | IMPORT | EXCHANGE
                        'supply': str,        # SCARCE | LIMITED | MODERATE | HIGH | ABUNDANT
                        'tradeVolume': int,   # units per trade before the price moves
                        'purchasePrice': int, # what a ship PAYS to buy here
                        'sellPrice': int      # what the market PAYS a ship to sell here
                    },
                    ...
                ]
            }
        min_margin: cr/unit floor below which a route isn't worth the fuel.

    Returns:
        The best route, or {} if none:
            {
                'good': str,
                'buy_at': str,            # waypoint to buy at (an EXPORT, cheap)
                'sell_at': str,           # waypoint to sell at (an IMPORT, dear)
                'profit_per_unit': int,   # sellPrice - purchasePrice
                'volume': int,            # tradeVolume at the buy leg (throughput)
                'kind': str,              # "export→import" | "exchange"
                'score': int              # margin * max(volume, 1) — the ranking key
            }

    Strategy:
        PRIMARY — the supply chain. For each good that is EXPORTED somewhere and
        IMPORTED somewhere else, buy at the cheapest exporter and sell at the dearest
        importer. These legs refill every cycle, so the route sustains. Rank by
        margin × tradeVolume (throughput), NOT raw spread.

        FALLBACK — only when no export->import pair exists at all: cross-market
        EXCHANGE spreads (buy an exchange good at one waypoint, sell it at another).
        These don't refill the way an export/import pair does, so they're a last resort.

        ``sink_supply_cutoff`` skips importers already saturated at/above that supply
        tier (see ``rank_routes``). This is the single best of the ranked routes; the
        engine uses ``rank_routes`` directly to spread haulers across the top-N.
    """
    routes = rank_routes(markets, min_margin=min_margin, sink_supply_cutoff=sink_supply_cutoff)
    return routes[0] if routes else {}


# Backward-compat alias — callers should prefer best_route.
best_arbitrage = best_route


def affordable_units(credits: int, unit_price, room: int, vol_cap: int,
                     *, max_spend_frac: float = 0.5) -> int:
    """How many units to buy in ONE trade without draining the treasury.

    Sizes a purchase by the smaller of three caps: cargo ``room``, the saturation cap
    ``vol_cap`` (≈ one tier-step of the sink's tradeVolume), and — the working-capital guard
    the engine was missing — the cash we'll commit, at most ``max_spend_frac`` of current
    ``credits``. Without the cash cap, a single high-value buy (a full hold of ASSAULT_RIFLES
    at ~80k against a 109k treasury) spends almost everything into cargo, and a sell leg that
    doesn't fully realize then craters the agent (the documented crash). Capping per-trade
    spend to a fraction of cash means even a failed trade leaves most of the treasury intact,
    while small trades still proceed at low credits — no deadlock. ``max_spend_frac >= 1``
    disables the cash cap. Returns 0 when nothing is affordable (caller skips the buy).

    Pure: no I/O, host-free testable.
    """
    room = max(0, room)
    vol_cap = max(0, vol_cap)
    if not unit_price or unit_price <= 0:        # price unknown → fall back to size caps only
        return min(room, vol_cap)
    if max_spend_frac >= 1:                       # cash cap disabled
        return min(room, vol_cap)
    budget = max(0, int(max_spend_frac * max(int(credits), 0)))
    return max(0, min(room, vol_cap, budget // int(unit_price)))
