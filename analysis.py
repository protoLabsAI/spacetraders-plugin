"""
SpaceTraders market analysis utilities.
"""

def best_arbitrage(markets: list[dict]) -> dict:
    """
    Find the most profitable export -> import pair across a list of markets.

    Args:
        markets: List of market dicts, each of the form:
            {
                'waypointSymbol': str,
                'tradeGoods': [
                    {
                        'symbol': str,
                        'type': str,
                        'purchasePrice': int,
                        'sellPrice': int
                    },
                    ...
                ]
            }

    Returns:
        A dict describing the best opportunity, or an empty dict if none:
            {
                'good': str,          # trade good symbol
                'buy_at': str,        # waypoint where it's cheapest to buy
                'sell_at': str,       # waypoint where it's most profitable to sell
                'profit_per_unit': int
            }
        Returns {} if no profitable pair exists (i.e., every buy price
        is >= every sell price for every good).

    Notes:
        - 'purchasePrice' is what a ship pays to buy the good at that market.
        - 'sellPrice' is what the market pays to buy the good FROM a ship.
        - A profitable pair requires purchasePrice < sellPrice.
        - profit_per_unit = sellPrice - purchasePrice.
    """
    if not markets:
        return {}

    best = {}
    best_profit = 0

    for market in markets:
        waypoint = market.get('waypointSymbol')
        if not waypoint:
            continue

        for good in (market.get('tradeGoods') or []):
            purchase_price = good.get('purchasePrice')
            if purchase_price is None or not isinstance(purchase_price, (int, float)):
                continue

            if purchase_price <= 0:
                continue

            # Scan all other markets for a sell price > purchase_price
            for other_market in markets:
                if other_market.get('waypointSymbol') == waypoint:
                    continue  # same market, skip

                for other_good in (other_market.get('tradeGoods') or []):
                    if other_good.get('symbol') != good.get('symbol'):
                        continue

                    sell_price = other_good.get('sellPrice')
                    if sell_price is None or not isinstance(sell_price, (int, float)):
                        continue

                    profit = int(sell_price - purchase_price)
                    if profit > best_profit:
                        best_profit = profit
                        best = {
                            'good': good.get('symbol', 'UNKNOWN'),
                            'buy_at': waypoint,
                            'sell_at': other_market.get('waypointSymbol', 'UNKNOWN'),
                            'profit_per_unit': profit
                        }

    return best
