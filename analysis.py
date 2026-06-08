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


def best_route(markets: list[dict], min_margin: int = 1) -> dict:
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
    """
    if not markets:
        return {}

    exports: dict[str, list] = {}    # good -> [(purchasePrice, waypoint, tradeVolume, supply)]
    imports: dict[str, list] = {}    # good -> [(sellPrice, waypoint, supply)]
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
                if sp is None:
                    continue
                imports.setdefault(symbol, []).append((sp, waypoint, supply))
            elif gtype == "EXCHANGE":
                if bp is None or sp is None:
                    continue
                exchanges.setdefault(symbol, []).append((bp, sp, waypoint, vol))

    # PRIMARY: export -> import supply chain.
    best: dict = {}
    best_score = 0
    for good in set(exports) & set(imports):
        buy_price, buy_wp, buy_vol, _ = min(exports[good], key=lambda x: x[0])
        sell_price, sell_wp, _ = max(imports[good], key=lambda x: x[0])
        if buy_wp == sell_wp:
            continue
        margin = int(sell_price - buy_price)
        if margin < min_margin:
            continue
        score = margin * max(buy_vol, 1)
        if score > best_score:
            best_score = score
            best = {
                "good": good,
                "buy_at": buy_wp,
                "sell_at": sell_wp,
                "profit_per_unit": margin,
                "volume": buy_vol,
                "kind": "export→import",
                "score": score,
            }
    if best:
        return best

    # FALLBACK: cross-market EXCHANGE spreads (no refilling supply chain available).
    for good, listings in exchanges.items():
        for buy_price, _, buy_wp, buy_vol in listings:
            for _, sell_price, sell_wp, _ in listings:
                if buy_wp == sell_wp:
                    continue
                margin = int(sell_price - buy_price)
                if margin < min_margin:
                    continue
                score = margin * max(buy_vol, 1)
                if score > best_score:
                    best_score = score
                    best = {
                        "good": good,
                        "buy_at": buy_wp,
                        "sell_at": sell_wp,
                        "profit_per_unit": margin,
                        "volume": buy_vol,
                        "kind": "exchange",
                        "score": score,
                    }
    return best


# Backward-compat alias — callers should prefer best_route.
best_arbitrage = best_route
