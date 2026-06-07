"""Fleet engine — run the whole fleet concurrently under ONE rate limiter.

The orchestration layer protoTrader-in-space uses to manage many ships toward a
goal (default: maximise credits/hour). Every ship's job is an async coroutine; the
orchestrator runs them all at once via asyncio over the *single* rate-limited
client (`client.call` self-paces to ~2 req/s) — so a fleet shares the one
per-account API budget correctly, which separate processes can't. Ships spend most
of their time waiting on travel/cooldowns, so the budget easily covers a fleet.

Jobs are deterministic (the proven travel→buy→deliver→fulfill / survey→extract
loops) — the LLM's role is strategy (which job for which ship), not clicking every
leg. See `autopilot()` in tools.py for the agent-facing entry point.
"""

from __future__ import annotations

import asyncio

from . import client as C
from . import tools as T


async def _ship(sym: str) -> dict:
    return await C.call("GET", f"/my/ships/{sym}")


async def _credits() -> int:
    a = await C.call("GET", "/my/agent")
    return a["credits"]


async def _wait_arrival(sym: str, poll: float = 8.0) -> dict:
    """Block (paced) until the ship is no longer IN_TRANSIT."""
    while True:
        s = await _ship(sym)
        if s["nav"]["status"] != "IN_TRANSIT":
            return s
        await asyncio.sleep(poll)


async def _wait_cooldown(sym: str, poll: float = 5.0) -> None:
    while True:
        s = await _ship(sym)
        cd = s.get("cooldown", {}).get("remainingSeconds", 0)
        if not cd:
            return
        await asyncio.sleep(min(cd + 1, poll if poll > cd else cd + 1))


async def _refuel_if_low(sym: str, frac: float = 0.6, *, log=None) -> None:
    """Top off a ship's fuel if it's below ``frac`` and the current waypoint sells
    fuel. Keeps ships from trickling down to DRIFT-only — call on arrival anywhere."""
    s = await _ship(sym)
    fuel = s.get("fuel", {})
    cap = fuel.get("capacity", 0)
    if cap == 0 or fuel.get("current", 0) >= frac * cap:   # probe (0 fuel) or already full enough
        return
    wp = s["nav"]["waypointSymbol"]
    try:
        m = await C.call("GET", f"/systems/{T._system_of(wp)}/waypoints/{wp}/market")
        if any(g.get("symbol") == "FUEL" for g in m.get("tradeGoods", [])):
            await C.call("POST", f"/my/ships/{sym}/dock")
            r = await C.call("POST", f"/my/ships/{sym}/refuel")
            if log:
                log(f"{sym}: refueled → {r['fuel']['current']}/{r['fuel']['capacity']}")
    except C.SpaceTradersError:
        pass


async def travel_to(sym: str, dest: str, *, max_hops: int = 12, log=None) -> bool:
    """Get a ship parked at ``dest``, looping st_travel through any fuel stops, and
    top off fuel on arrival wherever it's sold.

    st_travel issues one hop (CRUISE / DRIFT / fuel-station detour); we wait out
    each leg and call again until the ship is actually at the destination.
    """
    for _ in range(max_hops):
        s = await _ship(sym)
        nav = s["nav"]
        if nav["status"] != "IN_TRANSIT" and nav["waypointSymbol"] == dest:
            await _refuel_if_low(sym, log=log)   # arrived — top off if fuel is sold here
            return True
        if nav["status"] == "IN_TRANSIT":
            await _wait_arrival(sym)
            continue
        out = await T.st_travel.ainvoke({"ship": sym, "destination": dest})
        if log:
            log(f"{sym}: {out.splitlines()[0]}")
        await _wait_arrival(sym)
    return False


async def run_fleet(jobs: dict, *, log=None) -> dict:
    """Run a {ship_symbol: coroutine} map concurrently; return {ship: result}.

    One shared client → one rate budget. A job that raises is captured as an
    ``error: ...`` string so one ship's failure never sinks the fleet.
    """
    results: dict[str, str] = {}

    async def _run(sym: str, coro):
        try:
            results[sym] = await coro
        except C.SpaceTradersError as e:
            results[sym] = f"error: {e}"
        except Exception as e:  # noqa: BLE001 — isolate one ship's failure
            results[sym] = f"error: {type(e).__name__}: {e}"
        if log:
            log(f"{sym} done: {results[sym]}")

    await asyncio.gather(*[_run(s, c) for s, c in jobs.items()])
    return results


# ── shared job primitives ────────────────────────────────────────────────────


async def _held(sym: str, good: str) -> int:
    s = await _ship(sym)
    return sum(i["units"] for i in s["cargo"]["inventory"] if i["symbol"] == good)


async def _buy(sym: str, good: str, target: int, *, max_price: float | None = None, log=None) -> None:
    """Buy up to ``target`` units of ``good``. Profitability guard: if ``max_price``
    is set and the live unit price exceeds it, buy NOTHING — a buy above the ceiling
    is a guaranteed loss (a saturated market or an over-priced contract good)."""
    await C.call("POST", f"/my/ships/{sym}/dock")
    if max_price is not None:
        s = await _ship(sym)
        wp = s["nav"]["waypointSymbol"]
        try:
            m = await C.call("GET", f"/systems/{T._system_of(wp)}/waypoints/{wp}/market")
            price = next((g["purchasePrice"] for g in m.get("tradeGoods", []) if g["symbol"] == good), None)
        except C.SpaceTradersError:
            price = None
        if price is not None and price > max_price:
            if log:
                log(f"{sym}: SKIP buy {good} @ {price} > ceiling {max_price:.0f} (would lose money)")
            return
    while await _held(sym, good) < target:
        s = await _ship(sym)
        room = s["cargo"]["capacity"] - s["cargo"]["units"]
        want = min(target - await _held(sym, good), room, 20)
        if want <= 0:
            break
        try:
            r = await C.call("POST", f"/my/ships/{sym}/purchase",
                             json={"symbol": good, "units": want})
            if log:
                log(f"{sym}: bought {r['transaction']['units']}×{good} @ {r['transaction']['pricePerUnit']}")
        except C.SpaceTradersError as e:
            if log:
                log(f"{sym}: buy stopped — {e}")
            break


async def _dump_except(sym: str, keep: str, *, log=None) -> None:
    """Sell (or jettison) everything that isn't ``keep`` to free the hold."""
    s = await _ship(sym)
    for it in list(s["cargo"]["inventory"]):
        if it["symbol"] == keep:
            continue
        try:
            await C.call("POST", f"/my/ships/{sym}/dock")
            await C.call("POST", f"/my/ships/{sym}/sell",
                         json={"symbol": it["symbol"], "units": it["units"]})
        except C.SpaceTradersError:
            await C.call("POST", f"/my/ships/{sym}/jettison",
                         json={"symbol": it["symbol"], "units": it["units"]})


# ── jobs ─────────────────────────────────────────────────────────────────────


async def job_contract(sym: str, contract_id: str, *, log=None) -> str:
    """Work a procurement contract end to end: accept → buy → deliver → fulfill."""
    cs = await C.call("GET", "/my/contracts")
    ct = next((x for x in cs if x["id"] == contract_id), None)
    if not ct:
        return f"contract {contract_id} not found"
    if not ct["accepted"]:
        await C.call("POST", f"/my/contracts/{contract_id}/accept")
    dv = ct["terms"]["deliver"][0]
    good, deliver_wp, req = dv["tradeSymbol"], dv["destinationSymbol"], dv["unitsRequired"]
    system = T._system_of(deliver_wp)
    buys, _ = await T._good_markets(system, good)
    if not buys:
        return f"no market sells {good} in {system} (needs mining) — skipped"
    buy_wp = buys[0]
    # Profitability ceiling: the contract pays (advance + fulfillment) over the
    # required units. Buying a unit above that is a net loss — _buy refuses to.
    pay = ct["terms"].get("payment", {})
    pay_per_unit = (pay.get("onAccepted", 0) + pay.get("onFulfilled", 0)) / max(req, 1)
    cap = (await _ship(sym))["cargo"]["capacity"]
    while True:
        cs = await C.call("GET", "/my/contracts")
        ct = next(x for x in cs if x["id"] == contract_id)
        dv = ct["terms"]["deliver"][0]
        done = dv["unitsFulfilled"]
        if done >= req or ct["fulfilled"]:
            break
        await travel_to(sym, buy_wp, log=log)
        await _dump_except(sym, good, log=log)
        await _buy(sym, good, min(req - done, cap), max_price=pay_per_unit, log=log)
        if await _held(sym, good) == 0:
            return (f"skipped {good} contract at {done}/{req}: buy price exceeds the "
                    f"{pay_per_unit:.0f}/unit the contract pays (would lose money)")
        await travel_to(sym, deliver_wp, log=log)
        await C.call("POST", f"/my/ships/{sym}/dock")
        u = await _held(sym, good)
        await C.call("POST", f"/my/contracts/{contract_id}/deliver",
                     json={"shipSymbol": sym, "tradeSymbol": good, "units": u})
        if log:
            log(f"{sym}: delivered {u}×{good}")
    r = await C.call("POST", f"/my/contracts/{contract_id}/fulfill")
    return f"fulfilled {contract_id} ({req} {good}); credits {r['agent']['credits']:,}"


async def job_mining(sym: str, asteroid: str, ore: str, sell_wp: str, *, log=None) -> str:
    """Fill the hold with an ore at an asteroid, haul it to a market, and sell."""
    await travel_to(sym, asteroid, log=log)
    await C.call("POST", f"/my/ships/{sym}/orbit")
    s = await _ship(sym)
    cap = s["cargo"]["capacity"]
    dry = 0
    while (await _ship(sym))["cargo"]["units"] < cap and dry < 4:
        await _wait_cooldown(sym)
        out = await T.st_extract.ainvoke({"ship": sym, "prefer": ore})
        if "Error" in out:
            break
        if ore not in out:
            # survey to try to target the ore
            await T.st_survey.ainvoke({"ship": sym})
            dry += 1
        else:
            dry = 0
    await _dump_except(sym, ore, log=log)
    held = await _held(sym, ore)
    if held == 0:
        return f"{asteroid} yielded no {ore} (mine elsewhere or buy it)"
    await travel_to(sym, sell_wp, log=log)
    await C.call("POST", f"/my/ships/{sym}/dock")
    r = await C.call("POST", f"/my/ships/{sym}/sell", json={"symbol": ore, "units": held})
    return f"mined+sold {held}×{ore} for {r['transaction']['totalPrice']:,} cr"


async def _good_price(wp: str, good: str) -> tuple:
    """(purchasePrice, sellPrice) for ``good`` at ``wp`` — needs a ship present; (None, None) if unknown."""
    try:
        m = await C.call("GET", f"/systems/{T._system_of(wp)}/waypoints/{wp}/market")
        g = next((x for x in m.get("tradeGoods", []) if x["symbol"] == good), None)
        return (g.get("purchasePrice"), g.get("sellPrice")) if g else (None, None)
    except C.SpaceTradersError:
        return (None, None)


async def job_trade(sym: str, good: str, buy_wp: str, sell_wp: str, *, log=None) -> str:
    """One buy-low / sell-high round trip for a good — only if the spread is positive."""
    cap = (await _ship(sym))["cargo"]["capacity"]
    await travel_to(sym, buy_wp, log=log)
    # Profitability guard: confirm sell (at sell_wp) > buy (here) before committing.
    buy_price, _ = await _good_price(buy_wp, good)
    _, sell_price = await _good_price(sell_wp, good)
    if buy_price and sell_price and sell_price <= buy_price:
        return (f"skipped {good} trade: buy {buy_price} ≥ sell {sell_price} "
                f"({buy_wp}→{sell_wp}) — no margin, would lose money")
    await _dump_except(sym, good, log=log)
    await _buy(sym, good, cap, max_price=sell_price, log=log)
    held = await _held(sym, good)
    if held == 0:
        return f"could not buy {good} at {buy_wp} (or price ≥ resale — guarded)"
    await travel_to(sym, sell_wp, log=log)
    await C.call("POST", f"/my/ships/{sym}/dock")
    r = await C.call("POST", f"/my/ships/{sym}/sell", json={"symbol": good, "units": held})
    return f"traded {held}×{good} → {r['transaction']['totalPrice']:,} cr"


async def job_scout(sym: str, market_waypoints: list, *, log=None) -> dict:
    """Visit markets and record live per-unit prices into the persistent price map (a
    ship present unlocks them) — this is what fills the map the trade finder reasons over."""
    from . import prices as _pricemem
    seen: dict[str, dict] = {}
    for wp in market_waypoints:
        if not await travel_to(sym, wp, log=log):
            continue
        await C.call("POST", f"/my/ships/{sym}/dock")
        system = T._system_of(wp)
        m = await C.call("GET", f"/systems/{system}/waypoints/{wp}/market")
        tg = m.get("tradeGoods", [])
        _pricemem.record_market(system, wp, tg)
        seen[wp] = {g["symbol"]: (g["purchasePrice"], g["sellPrice"]) for g in tg}
    return seen


# ── autopilot — drive the whole fleet toward an objective for a time window ──


def _now() -> float:
    return asyncio.get_event_loop().time()


async def _contract_loop(sym: str, deadline: float, claimed: set, lock, *, log=None) -> str:
    """A cargo ship: claim/negotiate a procurement contract, work it, repeat."""
    hq = (await C.call("GET", "/my/agent")).get("headquarters")  # negotiate at a faction waypoint
    n = 0
    while _now() < deadline:
        async with lock:
            cs = await C.call("GET", "/my/contracts")
            # Prefer an accepted, unfulfilled contract; else pick up an OFFERED one — a
            # fresh agent STARTS with an offered contract, and the API refuses to
            # negotiate a new one (4511) while an offer is pending, so we must ACCEPT the
            # offer, not negotiate around it (the bug that parked the starter contract).
            ct = (next((c for c in cs if c["accepted"] and not c["fulfilled"]
                        and c["id"] not in claimed), None)
                  or next((c for c in cs if not c["accepted"] and not c["fulfilled"]
                           and c["id"] not in claimed), None))
            if ct is None:
                try:
                    # Negotiate only while DOCKED at a faction waypoint — travel to HQ
                    # and dock first, or it errors "not docked".
                    if hq:
                        await travel_to(sym, hq, log=log)
                        try:
                            await C.call("POST", f"/my/ships/{sym}/dock")
                        except C.SpaceTradersError:
                            pass
                    ct = (await C.call("POST", f"/my/ships/{sym}/negotiate/contract"))["contract"]
                except C.SpaceTradersError as e:
                    return f"{n} contract(s) done; no more available ({e})"
            # Sourceability guard (offered + negotiated alike): don't accept a contract
            # whose good no market in-system sells — it's un-fulfillable, and an accepted
            # contract can't be cancelled, so it would block this ship until it expires.
            ndv = ct["terms"]["deliver"][0]
            g = ndv["tradeSymbol"]
            buys, _ = await T._good_markets(T._system_of(ndv["destinationSymbol"]), g)
            if not buys:
                return (f"{n} done; declined an un-sourceable {g} contract "
                        f"(no market sells it in-system) — parking, not hauling dead weight")
            cid = ct["id"]
            if not ct["accepted"]:
                try:
                    await C.call("POST", f"/my/contracts/{cid}/accept")
                except C.SpaceTradersError as e:
                    return f"{n} contract(s) done; couldn't accept {g} contract ({e})"
            claimed.add(cid)
        if log:
            log(f"{sym}: working contract {cid}")
        res = await job_contract(sym, cid, log=log)
        if log:
            log(f"{sym}: {res}")
        n += 1
        if "skipped" in res or "could not" in res:
            return f"{n-1} done; stuck: {res}"
    return f"completed {n} contract(s)"


async def _sell_all_here(sym: str, *, log=None) -> int:
    """Sell every cargo good the current docked market will buy. Returns credits."""
    s = await _ship(sym)
    earned = 0
    for it in list(s["cargo"]["inventory"]):
        try:
            r = await C.call("POST", f"/my/ships/{sym}/sell",
                             json={"symbol": it["symbol"], "units": it["units"]})
            earned += r["transaction"]["totalPrice"]
        except C.SpaceTradersError:
            pass  # this market doesn't buy that good
    return earned


async def _mining_loop(sym: str, deadline: float, asteroid: str, market: str, *, log=None) -> str:
    """A cargo ship: fill the hold at an asteroid, sell at a market, repeat."""
    runs, earned = 0, 0
    while _now() < deadline:
        await travel_to(sym, asteroid, log=log)
        await C.call("POST", f"/my/ships/{sym}/orbit")
        cap = (await _ship(sym))["cargo"]["capacity"]
        while (await _ship(sym))["cargo"]["units"] < cap and _now() < deadline:
            await _wait_cooldown(sym)
            out = await T.st_extract.ainvoke({"ship": sym})
            if "Error" in out:
                break
        await travel_to(sym, market, log=log)
        await C.call("POST", f"/my/ships/{sym}/dock")
        earned += await _sell_all_here(sym, log=log)
        runs += 1
        if log:
            log(f"{sym}: mining run {runs} done (+{earned:,} cr total)")
    return f"{runs} mining run(s), +{earned:,} cr"


# Growth-engine knobs (zero-to-million doctrine): contracts seed, trade compounds.
_MIN_MARGIN = 30        # cr/unit floor for a trade route (fuel/time eats less than this)
_BUY_BUFFER = 600_000   # only reinvest in a hauler when this comfortable (cost ~290k)
_MAX_SHIPS = 8          # cap auto-bought fleet size
_PROBE_BUFFER = 80_000  # probes ~23k + fly free — buy them cheap to scout
_MAP_TARGET = 8         # markets to have in the price map before arbitrage surfaces
_MAX_PROBES = 5         # enough parallel scouts
_ROUTE_CACHE: dict = {"at": -1e9, "route": None}


async def _best_trade_route(system: str, market_wps: list) -> dict | None:
    """The best CURRENTLY-profitable arbitrage route from markets with live prices
    (a ship present unlocks prices). Cached 60s — scanning every market is costly."""
    now = _now()
    if _ROUTE_CACHE["route"] is not None and now - _ROUTE_CACHE["at"] < 60:
        return _ROUTE_CACHE["route"]
    from . import routes
    # Memory fast-path: re-verify a REMEMBERED route's live prices before scanning the
    # whole system. The agent learns routes over time, so this gets cheaper each window.
    for r in routes.recall_routes(system):
        buy_price, _ = await _good_price(r["buy_at"], r["good"])
        _, sell_price = await _good_price(r["sell_at"], r["good"])
        if buy_price and sell_price and (sell_price - buy_price) >= _MIN_MARGIN:
            route = {"good": r["good"], "buy_at": r["buy_at"], "sell_at": r["sell_at"],
                     "profit_per_unit": sell_price - buy_price}
            _ROUTE_CACHE.update(at=now, route=route)
            return route
    # Refresh the persistent PRICE MAP for any currently-lit markets, then run arbitrage
    # over the RECORDED map (built up as probes sweep the system) — not just what's lit
    # right now, which is almost never two markets at once. This is what makes spatial
    # arbitrage actually work, and REMEMBER the best so the agent recalls it next wipe.
    from . import prices
    for wp in market_wps:
        try:
            m = await C.call("GET", f"/systems/{system}/waypoints/{wp}/market")
        except C.SpaceTradersError:
            continue
        if m.get("tradeGoods"):
            prices.record_market(system, wp, m["tradeGoods"])
    from .analysis import best_arbitrage
    best = best_arbitrage(prices.price_map(system))
    route = best if best and best.get("profit_per_unit", 0) >= _MIN_MARGIN else None
    if route:
        routes.remember_route(system, route["good"], route["buy_at"],
                              route["sell_at"], route["profit_per_unit"])
    _ROUTE_CACHE.update(at=now, route=route)
    return route


async def _trade_loop(sym: str, deadline: float, system: str, market_wps: list, *, log=None) -> str:
    """A cargo ship: run the best profitable arbitrage route, re-evaluating each round
    so it adapts when a market saturates. job_trade is spread-guarded (no loss legs)."""
    runs = 0
    while _now() < deadline:
        route = await _best_trade_route(system, market_wps)
        if not route:
            await asyncio.sleep(15)           # wait for probes to scout a profitable spread
            continue
        res = await job_trade(sym, route["good"], route["buy_at"], route["sell_at"], log=log)
        if log:
            log(f"{sym}: {res}")
        runs += 1
        if "skipped" in res:                 # this route went unprofitable (saturation)
            _ROUTE_CACHE["at"] = -1e9         # force a re-scan next round
    return f"completed {runs} trade run(s)" if runs else "no profitable route this window — idled"


async def _buy_ship_at_yard(ships: list, system: str, ship_type: str, *, log=None) -> bool:
    """Ferry a free-flying probe to a shipyard and buy ``ship_type``. Best-effort."""
    try:
        yards = await C.call("GET", f"/systems/{system}/waypoints",
                             params={"traits": "SHIPYARD", "limit": 20})
    except C.SpaceTradersError:
        return False
    if not yards:
        return False
    yard = yards[0]["symbol"]
    mover = next((s for s in ships if s["fuel"].get("capacity", 0) == 0), None) or (ships[0] if ships else None)
    if mover is None or not await travel_to(mover["symbol"], yard, log=log):
        return False
    try:
        r = await C.call("POST", "/my/ships", json={"shipType": ship_type, "waypointSymbol": yard})
        if log:
            log(f"reinvest: bought {r['ship']['symbol']} ({ship_type}) @ {yard}")
        return True
    except C.SpaceTradersError as e:
        if log:
            log(f"reinvest skipped ({ship_type}): {e}")
        return False


async def _maybe_reinvest(ships: list, system: str, *, log=None) -> None:
    """Self-scaling reinvestment: fill the price map FIRST with cheap probes (parallel
    scouting → arbitrage surfaces sooner), then scale trade with haulers once the map is
    covered and capital is comfortable."""
    if len(ships) >= _MAX_SHIPS:
        return
    from . import prices
    coverage = prices.stats(system).get("markets", 0)
    probes = sum(1 for s in ships if s["fuel"].get("capacity", 0) == 0)
    if coverage < _MAP_TARGET and probes < _MAX_PROBES:
        if await _credits() >= _PROBE_BUFFER:
            await _buy_ship_at_yard(ships, system, "SHIP_PROBE", log=log)
    elif await _credits() >= _BUY_BUFFER:
        await _buy_ship_at_yard(ships, system, "SHIP_LIGHT_HAULER", log=log)


async def _maybe_buy_hauler(ships: list, system: str, *, log=None) -> None:
    """Reinvest profit into a LIGHT_HAULER when capital is comfortable and there's
    room to grow. Best-effort: a probe (flies free) ferries to a shipyard to buy it;
    the new hauler joins the trade rotation next window. Guarded by _BUY_BUFFER."""
    if len(ships) >= _MAX_SHIPS or await _credits() < _BUY_BUFFER:
        return
    try:
        yards = await C.call("GET", f"/systems/{system}/waypoints",
                             params={"traits": "SHIPYARD", "limit": 20})
    except C.SpaceTradersError:
        return
    if not yards:
        return
    yard = yards[0]["symbol"]
    mover = next((s for s in ships if s["fuel"].get("capacity", 0) == 0), None)  # probe = free
    if mover is None:
        return
    if not await travel_to(mover["symbol"], yard, log=log):
        return
    try:
        r = await C.call("POST", "/my/ships",
                         json={"shipType": "SHIP_LIGHT_HAULER", "waypointSymbol": yard})
        if log:
            log(f"reinvest: bought {r['ship']['symbol']} (LIGHT_HAULER) @ {yard}")
    except C.SpaceTradersError as e:
        if log:
            log(f"reinvest skipped: {e}")


async def autopilot(minutes: float, *, log=None) -> dict:
    """Run the fleet toward max credits for ``minutes`` — the zero-to-million engine.
    Contracts are per-AGENT (one active), so ONE cargo ship works contracts (the
    capital base) while the OTHERS run profitable trade routes (the scaling lever,
    each independent + spread-guarded); probes scout markets for price intel; profit
    is reinvested into haulers when capital allows. One shared rate budget. Returns a
    credits/hr summary + per-ship results."""
    start_credits = await _credits()
    deadline = _now() + minutes * 60.0
    ships = await C.call("GET", "/my/ships")
    claimed: set = set()
    lock = asyncio.Lock()

    system = ships[0]["nav"]["systemSymbol"] if ships else None
    market_wps = []
    if system:
        wps = await C.call("GET", f"/systems/{system}/waypoints",
                           params={"traits": "MARKETPLACE", "limit": 20})
        market_wps = [w["symbol"] for w in wps]

    # Growth engine (zero-to-million): reinvest profit first, then assign by role.
    #   • probes SCOUT markets — free, and it builds the price map trade needs;
    #   • ONE cargo ship works CONTRACTS (the capital base — capped at 1/agent);
    #   • every OTHER cargo ship runs the best profitable TRADE route (the scaling
    #     lever — independent + spread-guarded; idles, never loses, if none yet).
    if system:
        await _maybe_reinvest(ships, system, log=log)   # probes to fill the map, then haulers
        ships = await C.call("GET", "/my/ships")   # refresh after a possible buy
    jobs = {}
    cargo_ships = [s for s in ships if s["cargo"]["capacity"] > 0]
    probes = [s for s in ships if s["cargo"]["capacity"] == 0]
    # Spread probes ACROSS the markets (round-robin) so they cover ground instead of
    # all clustering on the first few — that's what fills the price map fast.
    for i, p in enumerate(probes):
        if market_wps:
            share = market_wps[i::len(probes)] or market_wps
            jobs[p["symbol"]] = job_scout(p["symbol"], share, log=log)
    if cargo_ships:
        jobs[cargo_ships[0]["symbol"]] = _contract_loop(
            cargo_ships[0]["symbol"], deadline, claimed, lock, log=log)
        for s in cargo_ships[1:]:
            if system and market_wps:
                jobs[s["symbol"]] = _trade_loop(s["symbol"], deadline, system, market_wps, log=log)

    results = await run_fleet(jobs, log=log)
    end_credits = await _credits()
    gained = end_credits - start_credits
    rate = round(gained / (minutes / 60.0)) if minutes else 0
    return {
        "minutes": minutes,
        "credits_start": start_credits,
        "credits_end": end_credits,
        "gained": gained,
        "per_hour": rate,
        "ships": results,
    }


# ── background ops — run the engine in the server loop so the agent doesn't block ─

# The autopilot blocks for its whole window; an agent driving over A2A can't tie up
# a turn for 20 minutes. So launch it as a background task in the SAME event loop
# (one shared rate budget), return immediately, and let the agent supervise via
# ops_status() between turns. This is what makes hands-off autonomy practical.
_OPS: dict = {"task": None, "started_minutes": 0.0, "result": None, "log": []}


async def _run_ops(minutes: float) -> None:
    """Run autopilot windows BACK-TO-BACK so the engine self-perpetuates — it no longer
    depends on a scheduler tick to relaunch it (the loopback self-POST is flaky under
    load). WHEN to stop is NOT hardcoded here: the operator's objective lives in the
    substrate's goal system (a `spacetraders:credits` plugin verifier, any target), and
    its on_achieved hook calls request_stop(). The engine just earns until then."""
    _OPS["log"] = []
    _OPS["stop"] = False
    try:
        while not _OPS["stop"]:
            _OPS["result"] = await autopilot(minutes, log=lambda m: _OPS["log"].append(m))
            await asyncio.sleep(3)   # a breath between windows
        _OPS["log"].append("engine wound down (goal reached or operator stop)")
    except asyncio.CancelledError:
        _OPS["result"] = {"stopped": True}
        raise
    except Exception as e:  # noqa: BLE001 — surface, don't crash the loop
        _OPS["result"] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        _OPS["task"] = None


def request_stop() -> None:
    """Signal the self-perpetuating engine to wind down after the current window. Called
    by the plugin's goal hook when the operator's substrate goal is achieved."""
    _OPS["stop"] = True


def start_ops(minutes: float) -> str:
    task = _OPS.get("task")
    if task is not None and not task.done():
        return "Fleet ops already running — check status before starting another."
    _OPS["started_minutes"] = minutes
    _OPS["result"] = None
    _OPS["task"] = asyncio.create_task(_run_ops(minutes))
    return (f"Fleet ops started in the background for ~{minutes:g} min. The whole "
            f"fleet is working under one rate budget. Check st_autopilot_status.")


def stop_ops() -> str:
    task = _OPS.get("task")
    if task is None or task.done():
        return "No fleet ops running."
    task.cancel()
    return "Stopping fleet ops."


def ops_status() -> dict:
    task = _OPS.get("task")
    running = task is not None and not task.done()
    return {
        "running": running,
        "started_minutes": _OPS.get("started_minutes", 0),
        "recent_log": _OPS.get("log", [])[-6:],
        "result": _OPS.get("result"),
    }
