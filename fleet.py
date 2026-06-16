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
from . import roles as R
from . import tools as T
# Control surface (knobs + presets + per-ship pins + decision log) on the shared protoAgent
# SDK helpers — see knobs.py. Imports graph.sdk transitively, so fleet only loads at runtime
# (when the engine starts), not at plugin-register time. The engine reads KNOBS.get(...) live.
from .knobs import (  # noqa: F401
    DLOG, KNOBS, apply_strategy, current_strategy, decisions, knobs,
    overrides, set_knob, set_override,
)


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
        if out.startswith("REFUSED"):   # too far to auto-DRIFT — don't loop, let the job skip it
            return False
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
    """Buy up to ``target`` units of ``good`` — bounded by two guards:

      * profitability: if ``max_price`` is set and the live unit price exceeds it, buy NOTHING
        (a buy above the ceiling is a guaranteed loss — a saturated market or over-priced
        contract good);
      * working capital: never commit more than ``max_spend_frac`` of current credits to this
        buy, so one high-value good can't drain the treasury into cargo and crater us when the
        sell leg doesn't realize (analysis.affordable_units — the documented ASSAULT_RIFLES crash).
    """
    from .analysis import affordable_units
    await C.call("POST", f"/my/ships/{sym}/dock")
    # Live unit price — needed for BOTH guards. Fetched once up front (a buy is one round trip,
    # so the extra market read is cheap relative to the leg).
    s = await _ship(sym)
    wp = s["nav"]["waypointSymbol"]
    try:
        m = await C.call("GET", f"/systems/{T._system_of(wp)}/waypoints/{wp}/market")
        price = next((g["purchasePrice"] for g in m.get("tradeGoods", []) if g["symbol"] == good), None)
    except C.SpaceTradersError:
        price = None
    if max_price is not None and price is not None and price > max_price:
        if log:
            log(f"{sym}: SKIP buy {good} @ {price} > ceiling {max_price:.0f} (would lose money)")
        return
    frac = KNOBS.get("max_spend_frac") or 1.0
    while await _held(sym, good) < target:
        s = await _ship(sym)
        room = s["cargo"]["capacity"] - s["cargo"]["units"]
        want = min(target - await _held(sym, good), room, 20)
        if price:   # working-capital cap, re-checked against the LIVE (shared) treasury each chunk
            want = affordable_units(await _credits(), price, room, want, max_spend_frac=frac)
        if want <= 0:
            if log and price:
                log(f"{sym}: holding cash — {good} @ {price} exceeds the per-trade cap "
                    f"({frac:g}× credits); bought {await _held(sym, good)}/{target}")
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


async def _sell(sym: str, good: str, units: int, *, log=None) -> int:
    """Sell ``units`` of ``good`` at the current docked market, CHUNKED to the market's
    per-transaction volume limit (``tradeVolume``) — a low-limit good (e.g. CLOTHING cap 20)
    would otherwise error [4604] when dumped all at once and strand the ship. Returns sold."""
    s = await _ship(sym)
    wp = s["nav"]["waypointSymbol"]
    try:
        m = await C.call("GET", f"/systems/{T._system_of(wp)}/waypoints/{wp}/market")
        vol = next((g.get("tradeVolume", 20) for g in m.get("tradeGoods", []) if g["symbol"] == good), 20)
    except C.SpaceTradersError:
        vol = 20
    vol = vol or 20
    sold = 0
    while sold < units:
        try:
            r = await C.call("POST", f"/my/ships/{sym}/sell",
                             json={"symbol": good, "units": min(units - sold, vol)})
            sold += r["transaction"]["units"]
            if log:
                log(f"{sym}: sold {r['transaction']['units']}×{good} @ {r['transaction']['pricePerUnit']}")
        except C.SpaceTradersError as e:
            if log:
                log(f"{sym}: sell stopped at {sold}/{units} {good} — {e}")
            break
    return sold


async def _dump_except(sym: str, keep: str, *, log=None) -> None:
    """Sell (or jettison) everything that isn't ``keep`` to free the hold."""
    try:
        await C.call("POST", f"/my/ships/{sym}/dock")
    except C.SpaceTradersError:
        pass
    s = await _ship(sym)
    for it in list(s["cargo"]["inventory"]):
        if it["symbol"] == keep:
            continue
        await _sell(sym, it["symbol"], it["units"], log=log)   # chunked to the market limit
        left = await _held(sym, it["symbol"])
        if left > 0:  # market won't buy it → jettison to free the hold
            try:
                await C.call("POST", f"/my/ships/{sym}/jettison",
                             json={"symbol": it["symbol"], "units": left})
            except C.SpaceTradersError:
                pass


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
        # Buy: only purchase if we're actually AT the buy market (else we'd loop on a
        # bad-state error). If already holding enough of the good, skip the buy leg.
        if await _held(sym, good) < min(req - done, cap):
            if not await travel_to(sym, buy_wp, log=log):
                return f"could not reach buy market {buy_wp} for {good} (stuck at {done}/{req})"
            await _dump_except(sym, good, log=log)
            await _buy(sym, good, min(req - done, cap), max_price=pay_per_unit, log=log)
            if await _held(sym, good) == 0:
                return (f"skipped {good} contract at {done}/{req}: buy price exceeds the "
                        f"{pay_per_unit:.0f}/unit the contract pays (would lose money)")
        # Deliver: NEVER dock+deliver unless we actually reached the destination —
        # otherwise the API rejects it ([4510] not at delivery waypoint) and the loop
        # spins on the error (the recurring K93→J66 stall).
        if not await travel_to(sym, deliver_wp, log=log):
            return f"could not reach delivery waypoint {deliver_wp} (stuck at {done}/{req}, holding {good})"
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
    p, s, _ = await _good_quote(wp, good)
    return (p, s)


async def _good_quote(wp: str, good: str) -> tuple:
    """(purchasePrice, sellPrice, tradeVolume) for ``good`` at ``wp`` — needs a ship present;
    (None, None, None) if unknown. tradeVolume is the per-transaction cap the saturation
    guard sizes a delivery against."""
    try:
        m = await C.call("GET", f"/systems/{T._system_of(wp)}/waypoints/{wp}/market")
        g = next((x for x in m.get("tradeGoods", []) if x["symbol"] == good), None)
        return (g.get("purchasePrice"), g.get("sellPrice"), g.get("tradeVolume")) if g else (None, None, None)
    except C.SpaceTradersError:
        return (None, None, None)


async def job_trade(sym: str, good: str, buy_wp: str, sell_wp: str, *, log=None) -> str:
    """One buy-low / sell-high round trip for a good — only if the spread is positive, and
    sized so it doesn't crash the sink: buy at most ``sink_volume_mult × (sink tradeVolume)``,
    so one delivery moves the import price ~one tier-step instead of cratering it (the #1
    way an unattended bot kills its own routes). Raise sink_volume_mult for a thin fleet."""
    cap = (await _ship(sym))["cargo"]["capacity"]
    if not await travel_to(sym, buy_wp, log=log):   # too far to auto-DRIFT → skip, don't buy elsewhere
        return f"skipped {good} trade: couldn't reach buy waypoint {buy_wp} (too far)"
    # Profitability guard: confirm sell (at sell_wp) > buy (here) before committing.
    buy_price, _ = await _good_price(buy_wp, good)
    _, sell_price, sell_vol = await _good_quote(sell_wp, good)
    if buy_price and sell_price and sell_price <= buy_price:
        return (f"skipped {good} trade: buy {buy_price} ≥ sell {sell_price} "
                f"({buy_wp}→{sell_wp}) — no margin, would lose money")
    target = cap
    if sell_vol:                                  # saturation cap — keep a delivery ≈ one tier-step
        target = min(cap, max(1, round(KNOBS.get("sink_volume_mult") * sell_vol)))
    await _dump_except(sym, good, log=log)
    await _buy(sym, good, target, max_price=sell_price, log=log)
    held = await _held(sym, good)
    if held == 0:
        return f"could not buy {good} at {buy_wp} (or price ≥ resale — guarded)"
    if not await travel_to(sym, sell_wp, log=log):   # bought but the sink is too far to auto-DRIFT
        return f"bought {held}×{good} but couldn't reach sell waypoint {sell_wp} (too far) — holding"
    await C.call("POST", f"/my/ships/{sym}/dock")
    sold = await _sell(sym, good, held, log=log)
    return f"traded {sold}×{good} (of {held} hauled)"


async def job_scout(sym: str, market_waypoints: list, deadline: float, *, log=None) -> dict:
    """Visit markets and record live per-unit prices into the persistent price map (a
    ship present unlocks them) — this is what fills the map the trade finder reasons over.

    Stops at the window ``deadline`` so EVERY ship's job finishes together and the engine
    loops to a fresh window promptly — otherwise a long, unbounded sweep strands the faster
    contract/trade ships idle (they finish, then wait on the scouts). Resumes next window;
    the price map persists, so coverage accrues across windows either way.
    """
    from . import prices as _pricemem
    seen: dict[str, dict] = {}
    for wp in market_waypoints:
        if _now() >= deadline:
            break
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


async def _cruise_reachable(system: str, frm: str, to: str, fuel_cap) -> bool:
    """Can a ship CRUISE from ``frm`` to ``to`` (fast) — directly within one tank, or via a fuel
    station within CRUISE range to hop from? False ⇒ only a multi-hour DRIFT would reach it (the
    leg that wedges a contract ship for hours, e.g. the J58 delivery). Fuel-free ships (probes)
    are always reachable. Best-effort: on any lookup failure it returns True, so the guard never
    false-declines a workable contract."""
    if not fuel_cap:
        return True
    try:
        if await T._distance(system, frm, to) <= fuel_cap:
            return True                      # one CRUISE covers it
        fstop = await T._nearest_fuel(system, frm)
    except C.SpaceTradersError:
        return True                          # can't tell → don't false-decline
    if not fstop or fstop[0] == frm:
        return False                         # no fuel station to break the leg up → long DRIFT only
    return fstop[1] <= fuel_cap              # a fuel station is within one CRUISE → can hop + continue


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
            # Reachability guard: decline a contract whose delivery is beyond ~one tank from
            # the source. The ship CAN technically DRIFT there (1 fuel), but a 700u+ DRIFT
            # takes HOURS and won't finish inside an engine window — it just wedges the only
            # cargo ship on an un-fulfillable accepted contract (the J66/DRUGS trap). Work
            # in-range contracts + supply-chain trade instead, and bank toward a longer-range
            # hauler. (We tried value-aware accept of far lucrative ones — the occasional 169k
            # win wasn't worth the repeated wedging; range is a SHIP problem, not a guard one.)
            try:
                deliver_wp = ndv["destinationSymbol"]
                fuel_cap = (await _ship(sym))["fuel"].get("capacity") or 400
                if not await _cruise_reachable(T._system_of(deliver_wp), buys[0], deliver_wp, fuel_cap):
                    return (f"{n} done; declined {g} contract — delivery {deliver_wp} is only "
                            f"reachable from the source ({buys[0]}) by a multi-hour DRIFT (no CRUISE "
                            f"route within fuel range). A far contract just wedges the ship for hours; "
                            f"working in-range trade + banking toward a longer-range hauler instead.")
            except Exception:  # noqa: BLE001 — the guard must never crash the loop
                pass
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
        before = await _credits()
        await _sell(sym, it["symbol"], it["units"], log=log)   # chunked to the market limit
        earned += await _credits() - before
    return earned


async def _mining_loop(sym: str, deadline: float, asteroid: str, market: str, *, log=None) -> str:
    """A mining ship: fill the hold at an asteroid, sell at a market, repeat.

    Guarded so an impossible job can't spam the rate budget: a ship with no mining mount
    (a pin can force a laser-less hull here), a failed travel to the rock, or an extract the
    API rejects all bail out cleanly instead of re-extracting wherever the ship sits — that
    was the J58 case (a pinned light-hauler 'extracting' at a marketplace, looping on 3001)."""
    if not R.can_mine(await _ship(sym)):
        if log:
            log(f"{sym}: no mining mount — can't mine (pin a mining-capable ship instead)")
        return f"{sym} has no mining laser — skipped mining"
    runs, earned = 0, 0
    while _now() < deadline:
        if not await travel_to(sym, asteroid, log=log):   # never extract unless we REACHED the rock
            if log:
                log(f"{sym}: couldn't reach asteroid {asteroid} — skipping mining this window")
            return f"could not reach {asteroid} — no mining"
        await C.call("POST", f"/my/ships/{sym}/orbit")
        cap = (await _ship(sym))["cargo"]["capacity"]
        while (await _ship(sym))["cargo"]["units"] < cap and _now() < deadline:
            await _wait_cooldown(sym)
            out = await T.st_extract.ainvoke({"ship": sym})
            if "Error" in out:
                # The API rejected the extract (not an asteroid / no mount / etc.) — terminal,
                # not transient: stop instead of retrying it every cooldown and burning budget.
                if log:
                    log(f"{sym}: extract rejected ({out.strip()[:60]}) — stopping mining")
                return f"{sym} can't extract at {asteroid}: {out.strip()[:80]}"
        await travel_to(sym, market, log=log)
        await C.call("POST", f"/my/ships/{sym}/dock")
        earned += await _sell_all_here(sym, log=log)
        runs += 1
        if log:
            log(f"{sym}: mining run {runs} done (+{earned:,} cr total)")
    return f"{runs} mining run(s), +{earned:,} cr"


async def _siphon_loop(sym: str, deadline: float, giant: str, market: str, *, log=None) -> str:
    """A siphon ship: fill the hold at a gas giant, sell at a market, repeat. Guarded exactly
    like _mining_loop — no gas-siphon mount, an unreachable giant, or a rejected siphon all bail
    cleanly instead of spamming the rate budget."""
    if not R.can_siphon(await _ship(sym)):
        if log:
            log(f"{sym}: no gas-siphon mount — can't siphon (pin a siphon-capable ship instead)")
        return f"{sym} has no gas siphon — skipped siphoning"
    runs, earned = 0, 0
    while _now() < deadline:
        if not await travel_to(sym, giant, log=log):
            if log:
                log(f"{sym}: couldn't reach gas giant {giant} — skipping siphon this window")
            return f"could not reach {giant} — no siphon"
        await C.call("POST", f"/my/ships/{sym}/orbit")
        cap = (await _ship(sym))["cargo"]["capacity"]
        while (await _ship(sym))["cargo"]["units"] < cap and _now() < deadline:
            await _wait_cooldown(sym)
            out = await T.st_siphon.ainvoke({"ship": sym})
            if "Error" in out:
                if log:
                    log(f"{sym}: siphon rejected ({out.strip()[:60]}) — stopping siphon")
                return f"{sym} can't siphon at {giant}: {out.strip()[:80]}"
        await travel_to(sym, market, log=log)
        await C.call("POST", f"/my/ships/{sym}/dock")
        earned += await _sell_all_here(sym, log=log)
        runs += 1
        if log:
            log(f"{sym}: siphon run {runs} done (+{earned:,} cr total)")
    return f"{runs} siphon run(s), +{earned:,} cr"


# Engine route cache — the ranked trade routes, refreshed each window (a ship present at a
# market unlocks live prices). The control surface (knobs/presets/pins/decision-log) moved to
# knobs.py on the shared SDK helpers and is imported at the top of this module.
_ROUTE_CACHE: dict = {"at": -1e9, "routes": []}


async def _ranked_routes(system: str, market_wps: list) -> list:
    """All currently-profitable arbitrage routes, ranked best-first (a ship present unlocks
    live prices). Cached 60s — scanning every market is costly. Returns a LIST so several
    haulers can diversify across the top-N (anti-saturation) instead of stacking on one."""
    now = _now()
    if _ROUTE_CACHE["routes"] and now - _ROUTE_CACHE["at"] < 60:
        return _ROUTE_CACHE["routes"]
    from . import prices, routes
    from .analysis import rank_routes
    # Refresh the persistent PRICE MAP for any currently-lit markets, then rank arbitrage
    # over the RECORDED map (built up as probes sweep the system) — not just what's lit
    # right now, which is almost never two markets at once. REMEMBER the best for next wipe.
    for wp in market_wps:
        try:
            m = await C.call("GET", f"/systems/{system}/waypoints/{wp}/market")
        except C.SpaceTradersError:
            continue
        if m.get("tradeGoods"):
            prices.record_market(system, wp, m["tradeGoods"])
    ranked = rank_routes(prices.price_map(system), min_margin=KNOBS.get("min_margin"),
                         sink_supply_cutoff=KNOBS.get("sink_supply_cutoff"))
    if ranked:
        top = ranked[0]
        routes.remember_route(system, top["good"], top["buy_at"], top["sell_at"],
                              top["profit_per_unit"])
    else:
        # Live scan is dry (thin/aged price map) — instead of idling, fall back to routes we
        # LEARNED before (across windows/wipes). job_trade re-checks live prices at the buy/sell
        # legs before committing, so a stale recall is skipped, not a loss — and visiting the buy
        # waypoint re-lights that market for the price map. This is the engine USING what it learns.
        ranked = [{"good": r["good"], "buy_at": r["buy_at"], "sell_at": r["sell_at"],
                   "profit_per_unit": r["margin"], "volume": 0, "kind": "recalled",
                   "score": r["margin"]} for r in routes.recall_routes(system)]
    _ROUTE_CACHE.update(at=now, routes=ranked)
    return ranked


async def _best_trade_route(system: str, market_wps: list, rank: int = 0) -> dict | None:
    """The ``rank``-th best profitable route (0 = best). With route_diversify on, each
    hauler is handed a different rank so they spread across the top-N rather than all
    pile onto the single best route and saturate it."""
    ranked = await _ranked_routes(system, market_wps)
    if not ranked:
        return None
    return ranked[min(rank, len(ranked) - 1)]


async def _trade_loop(sym: str, deadline: float, system: str, market_wps: list,
                      *, rank: int = 0, log=None) -> str:
    """A cargo ship: run a profitable arbitrage route (its diversify ``rank``), re-evaluating
    each round so it adapts when a market saturates. job_trade is spread-guarded + sized to
    the sink's tradeVolume (no loss legs, no self-crash)."""
    runs = 0
    while _now() < deadline:
        route = await _best_trade_route(system, market_wps, rank=rank)
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


async def _contract_then_trade(sym: str, deadline: float, claimed: set, lock, system: str,
                               market_wps: list, *, log=None) -> str:
    """The lead cargo ship works contracts, but if there's no WORKABLE contract this window
    (declined as un-sourceable/unreachable, or a slot blocked by an un-fulfillable accepted
    one), it FALLS BACK to trade routes for the rest of the window instead of sitting idle —
    so the only cargo ship keeps earning even while a dead contract waits out its expiry."""
    res = await _contract_loop(sym, deadline, claimed, lock, log=log)
    stuck = any(k in res for k in ("declined", "unreachable", "no more available",
                                   "could not", "couldn't", "skipped"))
    if stuck and _now() < deadline and system and market_wps:
        if log:
            log(f"{sym}: no workable contract ({res}) — trading instead")
        tres = await _trade_loop(sym, deadline, system, market_wps, log=log)
        return f"{res}; fell back to trade → {tres}"
    return res


# Conservative ship-cost estimates (cr) — for the reserve-floor guard before a reinvest buy.
_SHIP_COST = {"SHIP_PROBE": 40_000, "SHIP_LIGHT_HAULER": 450_000, "SHIP_HEAVY_FREIGHTER": 1_500_000}
_LONG_RANGE_FUEL = 1500   # a hull with this fuel cap can reach far contracts/routes a hauler can't


async def _buy_ship_at_yard(ships: list, system: str, ship_type: str, *, log=None) -> bool:
    """Ferry a free-flying probe (or any ship) to a shipyard that SELLS ``ship_type`` and buy it.
    Best-effort. Picks a yard whose ``shipTypes`` includes the type — buying at the first
    shipyard regardless used to silently error + no-op when that yard didn't stock it."""
    try:
        yards = await C.call("GET", f"/systems/{system}/waypoints",
                             params={"traits": "SHIPYARD", "limit": 20})
    except C.SpaceTradersError:
        return False
    if not yards:
        return False
    yard = None
    for y in yards:
        try:
            sy = await C.call("GET", f"/systems/{system}/waypoints/{y['symbol']}/shipyard")
        except C.SpaceTradersError:
            continue
        if any(t.get("type") == ship_type for t in sy.get("shipTypes", [])):
            yard = y["symbol"]
            break
    if yard is None:
        if log:
            log(f"reinvest skipped: no shipyard in {system} sells {ship_type}")
        return False
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
    covered and capital is comfortable. Every buy also keeps credits at/above reserve_floor."""
    if len(ships) >= KNOBS.get("max_ships"):
        return
    credits = await _credits()
    floor = KNOBS.get("reserve_floor")

    def _affordable(ship_type: str, buffer_knob: str) -> bool:
        # comfortable enough (buffer) AND the buy won't dip below the hard cash floor
        return credits >= KNOBS.get(buffer_knob) and credits - _SHIP_COST.get(ship_type, 0) >= floor

    from . import prices
    coverage = prices.stats(system).get("markets", 0)
    probes = sum(1 for s in ships if s["fuel"].get("capacity", 0) == 0)
    have_long_range = any(s["fuel"].get("capacity", 0) >= _LONG_RANGE_FUEL for s in ships)
    if coverage < KNOBS.get("map_target") and probes < KNOBS.get("max_probes"):
        if _affordable("SHIP_PROBE", "probe_buffer"):
            await _buy_ship_at_yard(ships, system, "SHIP_PROBE", log=log)
    elif not have_long_range and _affordable("SHIP_HEAVY_FREIGHTER", "heavy_buffer"):
        # Range-upgrade rung: once very comfortable and still all short-range haulers, buy a
        # long-range HEAVY_FREIGHTER (2300 fuel + big hold) — far contracts/routes a 600-fuel
        # light hauler can't reach (and the drift-cap correctly declines) become workable.
        await _buy_ship_at_yard(ships, system, "SHIP_HEAVY_FREIGHTER", log=log)
    elif _affordable("SHIP_LIGHT_HAULER", "buy_buffer"):
        await _buy_ship_at_yard(ships, system, "SHIP_LIGHT_HAULER", log=log)


def _d2(a: dict, b: dict) -> int:
    return (a.get("x", 0) - b.get("x", 0)) ** 2 + (a.get("y", 0) - b.get("y", 0)) ** 2


async def _mining_targets(system: str) -> list:
    """Mineable (asteroid, sell-market) pairs in ``system``, ranked by SHORTEST extract→sell
    round trip first — so the engine mines the most CENTRAL asteroids (near the markets =
    near the system core = reachable without a marathon DRIFT), not an arbitrary ``asteroids[0]``
    that a low-fuel drone can't get to (which, with the travel drift-cap, left miners idle).

    Returns a list so the autopilot can SPREAD several miners across different asteroids/markets
    instead of piling them all on one rock + one buyer (which craters that ore's price). Empty
    list ⇒ no asteroid or no market; the miner falls back to trade. Prefers ENGINEERED_ASTEROIDs.
    """
    async def _of_type(t: str) -> list:
        try:
            return await C.call("GET", f"/systems/{system}/waypoints",
                                params={"type": t, "limit": 20})
        except C.SpaceTradersError:
            return []
    asteroids = (await _of_type("ENGINEERED_ASTEROID")
                 or await _of_type("ASTEROID_FIELD")
                 or await _of_type("ASTEROID"))
    if not asteroids:
        return []
    try:
        markets = await C.call("GET", f"/systems/{system}/waypoints",
                               params={"traits": "MARKETPLACE", "limit": 20})
    except C.SpaceTradersError:
        return []
    if not markets:
        return []
    pairs = []
    for ast in asteroids:
        mkt = min(markets, key=lambda w: _d2(w, ast))
        pairs.append((ast["symbol"], mkt["symbol"], _d2(ast, mkt)))
    pairs.sort(key=lambda p: p[2])                       # shortest round trip / most central first
    return [(a, m) for a, m, _ in pairs[:4]]


async def _siphon_targets(system: str) -> list:
    """Gas-giant (siphon, sell-market) pairs in ``system``, ranked by shortest round trip —
    same shape as ``_mining_targets`` but for GAS_GIANT waypoints. Empty ⇒ no gas giant/market."""
    try:
        giants = await C.call("GET", f"/systems/{system}/waypoints",
                              params={"type": "GAS_GIANT", "limit": 20})
        markets = await C.call("GET", f"/systems/{system}/waypoints",
                               params={"traits": "MARKETPLACE", "limit": 20})
    except C.SpaceTradersError:
        return []
    if not giants or not markets:
        return []
    pairs = []
    for gg in giants:
        mkt = min(markets, key=lambda w: _d2(w, gg))
        pairs.append((gg["symbol"], mkt["symbol"], _d2(gg, mkt)))
    pairs.sort(key=lambda p: p[2])
    return [(g, m) for g, m, _ in pairs[:4]]


async def autopilot(minutes: float, *, log=None) -> dict:
    """Run the fleet toward max credits for ``minutes`` — the zero-to-million engine.
    Contracts are per-AGENT (one active), so ONE trader works contracts (the capital
    base) while the OTHERS run profitable trade routes (the scaling lever, each
    independent + spread-guarded); ships with a mining laser run the extract→sell loop
    at the nearest asteroid; probes scout markets for price intel; profit is reinvested
    into haulers when capital allows. One shared rate budget. Returns a credits/hr
    summary + per-ship results."""
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

    # Growth engine (zero-to-million): reinvest profit first, then assign by role
    # (roles.assign_roles — classified by CAPABILITY, not just "has a hold"):
    #   • probes SCOUT markets — free, and it builds the price map trade needs;
    #   • MINERS (a mining laser) run the survey→extract→sell loop at the nearest
    #     asteroid — a dedicated mining drone finally mines instead of being mis-cast
    #     as a hauler (its 15-unit hold used to sweep it into the trade rotation);
    #   • ONE trader works CONTRACTS (the capital base — capped at 1/agent);
    #   • every OTHER trader runs the best profitable TRADE route (the scaling lever
    #     — independent + spread-guarded; idles, never loses, if none yet).
    if system:
        await _maybe_reinvest(ships, system, log=log)   # probes to fill the map, then haulers
        ships = await C.call("GET", "/my/ships")   # refresh after a possible buy
    jobs = {}
    # Strategy preset (mining on/off) + per-ship pins (st_assign) drive the split.
    role = R.assign_roles(ships, mining_enabled=KNOBS.get("mining"), overrides=overrides())
    probes, miners, siphoners, traders = (
        role["probes"], role["miners"], role["siphoners"], role["traders"])
    # Spread probes ACROSS the markets (round-robin) so they cover ground instead of
    # all clustering on the first few — that's what fills the price map fast.
    for i, p in enumerate(probes):
        if market_wps:
            share = market_wps[i::len(probes)] or market_wps
            jobs[p["symbol"]] = job_scout(p["symbol"], share, deadline, log=log)
    # Miners dig at a CENTRAL asteroid and sell the ore — spread round-robin across the ranked
    # (asteroid, market) pairs so several miners don't pile on one rock + one buyer (saturation)
    # and so each gets a reachable target. If the system has no asteroid/market, fall back to
    # trade so the ship still earns — never stranded.
    if miners:
        targets = await _mining_targets(system) if system else []
        for i, s in enumerate(miners):
            if targets:
                ast, mkt = targets[i % len(targets)]
                jobs[s["symbol"]] = _mining_loop(s["symbol"], deadline, ast, mkt, log=log)
            elif system and market_wps:
                jobs[s["symbol"]] = _trade_loop(s["symbol"], deadline, system, market_wps, log=log)
    # Siphoners (pin-only, st_assign siphon) work gas giants — same spread/reachability model.
    if siphoners:
        gtargets = await _siphon_targets(system) if system else []
        for i, s in enumerate(siphoners):
            if gtargets:
                gg, mkt = gtargets[i % len(gtargets)]
                jobs[s["symbol"]] = _siphon_loop(s["symbol"], deadline, gg, mkt, log=log)
            elif system and market_wps:
                jobs[s["symbol"]] = _trade_loop(s["symbol"], deadline, system, market_wps, log=log)
    if traders:
        jobs[traders[0]["symbol"]] = _contract_then_trade(
            traders[0]["symbol"], deadline, claimed, lock, system, market_wps, log=log)
        # Diversify haulers across the top-N routes (rank by position) when route_diversify
        # is on, so they don't all stack on the single best route and saturate it.
        for i, s in enumerate(traders[1:], start=1):
            if system and market_wps:
                rank = i if KNOBS.get("route_diversify") else 0
                jobs[s["symbol"]] = _trade_loop(s["symbol"], deadline, system, market_wps,
                                                rank=rank, log=log)

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


# ── background ops — the engine runs as a SUPERVISED background task (graph.sdk.supervise) ─
# The autopilot blocks for its whole window; an agent over A2A can't tie up a turn for 20
# minutes. So the engine runs as a self-perpetuating background task with a watchdog — re-kick
# a crash, restart a stall, recover a universe reset — all from the shared Supervisor helper
# (protoAgent #1025): we supply only the window + the predicates. The agent supervises via
# ops_status() between turns. The deterministic watchdog is the heartbeat; the LLM agent is the
# exception handler, not the other way around.
_LOG: list = []      # the running progress log (capped) — for status + the dashboard
_LOGSEQ = 0          # monotonic log counter — the supervisor's "progress" signal for stalls
_ENGINE = None       # the Supervisor singleton (lazy — start() needs a running loop)


def _record(msg: str) -> None:
    global _LOGSEQ
    _LOG.append(msg)
    del _LOG[:-200]
    _LOGSEQ += 1


async def _window() -> dict:
    """One autopilot window — the supervised unit of work. Reads the window length from the knob
    each time, so the strategist can retune the OODA cadence live (st_tune window_minutes)."""
    return await autopilot(KNOBS.get("window_minutes") or 15.0, log=_record)


async def _stalled() -> bool:
    """Confirm a REAL stall: the engine's log is frozen (progress) AND no ship is in transit — a
    long DRIFT legitimately freezes the log, so don't false-trip on it."""
    try:
        ships = await C.call("GET", "/my/ships", params={"limit": 20})
        return not any(s["nav"]["status"] == "IN_TRANSIT" for s in ships)
    except Exception:  # noqa: BLE001 — can't tell → don't false-trip
        return False


async def _recover(result) -> bool:
    """on_crash: a universe reset (4113) kills the token — re-register the call sign once for a
    fresh token, then let the supervisor re-kick. Any other crash → re-kick. An unrecoverable
    reset (no account token / call sign claimed) → return False so the supervisor stops the storm."""
    err = (result or {}).get("error") or ""
    if "4113" in err or "reset_date" in err:
        try:
            status = await C.recover_from_reset()
        except Exception as e:  # noqa: BLE001
            status = f"reset recovery errored: {e}"
        _record(f"universe reset — {status}")
        return status.startswith("re-registered")
    return True


def _engine():
    """The Supervisor singleton (lazy: start() creates tasks, so build it in the async context
    of the first start)."""
    global _ENGINE
    if _ENGINE is None:
        from graph.sdk import supervise
        _ENGINE = supervise(_window, name="fleet", interval=90, breath=3.0, stall_ticks=3,
                            progress=lambda: _LOGSEQ, stall_check=_stalled, on_crash=_recover)
    return _ENGINE


def request_stop() -> None:
    """Wind the engine down gracefully after the current window (no re-kick) — called by the
    plugin's goal on_achieved hook when the operator's target is reached."""
    _engine().request_stop()


def start_ops(minutes: float) -> str:
    set_knob("window_minutes", minutes)   # seed the knob so st_report/st_tune agree
    _LOG.clear()
    if "already running" in _engine().start():
        return "Fleet ops already running — check status before starting another."
    return (f"Fleet ops started in the background (~{minutes:g} min windows, watchdog keeping it "
            f"alive). The whole fleet works under one rate budget. Check st_autopilot_status.")


def stop_ops() -> str:
    if _ENGINE is None or not _engine().running():
        return "No fleet ops running."
    _engine().stop()
    return "Stopping fleet ops."


def ops_status() -> dict:
    st = _engine().status() if _ENGINE is not None else {
        "running": False, "want_running": False, "watchdog": False, "result": None, "events": []}
    return {
        "running": st["running"],
        "want_running": st["want_running"],
        "watchdog": st["watchdog"],
        "started_minutes": KNOBS.get("window_minutes"),
        "recent_log": _LOG[-6:],
        "watchdog_log": st.get("events", [])[-5:],
        "result": st.get("result"),
    }


def _eta_hours(credits: int, per_hour: float, target: int):
    """Hours to ``target`` credits at the current rate, or None if already there / idle."""
    if per_hour <= 0 or credits >= target:
        return None
    return round((target - credits) / per_hour, 1)


async def report() -> dict:
    """Rich fleet telemetry for the OODA strategist's OBSERVE step — credits/hr trajectory,
    per-ship role + health, engine state, the live knobs + strategy + pins, and a few
    deterministic HINTS (stranded ships, idle capital, dead routes) to seed ORIENT. Costs
    one /my/agent + one /my/ships call; everything else is local engine state."""
    try:
        agent = await C.call("GET", "/my/agent")
        ships = await C.call("GET", "/my/ships", params={"limit": 20})
    except C.SpaceTradersError as e:
        return {"ok": False, "error": str(e)}
    credits = agent.get("credits", 0)
    ops = ops_status()
    last = ops.get("result") or {}
    per_hour = last.get("per_hour", 0) or 0

    # Label each ship with the role the engine WOULD assign it this window (same call the
    # autopilot makes), so the report and the engine never disagree.
    role = R.assign_roles(ships, mining_enabled=KNOBS.get("mining"), overrides=overrides())
    label = {}
    for p in role["probes"]:
        label[p["symbol"]] = "scout"
    for m in role["miners"]:
        label[m["symbol"]] = "miner"
    for x in role["siphoners"]:
        label[x["symbol"]] = "siphon"
    for i, t in enumerate(role["traders"]):
        label[t["symbol"]] = "contract" if i == 0 else "trader"

    rows, hints = [], []
    for s in ships:
        sym, nav, cargo = s["symbol"], s["nav"], s["cargo"]
        units, cap = cargo.get("units", 0), cargo.get("capacity", 0)
        in_transit = nav["status"] == "IN_TRANSIT"
        r = label.get(sym) or ("idle" if sym in overrides() else "—")
        stranded = (not in_transit) and cap > 0 and units >= cap   # parked with a full hold
        rows.append({"symbol": sym, "role": r, "pinned": overrides().get(sym),
                     "status": nav["status"], "at": nav["waypointSymbol"],
                     "cargo": f"{units}/{cap}", "stranded": stranded})
        if stranded:
            hints.append(f"{sym} parked FULL ({units}/{cap}) at {nav['waypointSymbol']} — may be stranded")

    if ops["running"] and last and per_hour <= 0:
        hints.append("engine not earning this window — check routes/saturation or lower min_margin")
    if credits >= KNOBS.get("buy_buffer") and len(ships) < KNOBS.get("max_ships") and ops["running"]:
        hints.append(f"{credits:,} cr idle with room to grow — reinvest (buy_buffer {KNOBS.get('buy_buffer'):,})")
    if not _ROUTE_CACHE.get("routes") and any(v in ("trader", "contract") for v in label.values()):
        hints.append("no profitable route mapped — scout more markets or lower min_margin")

    # Build the standard telemetry envelope (status / metrics / hints / decisions / sections),
    # on the shared SDK helper (protoAgent #1027), carrying the existing keys as extras so the
    # st_report tool + dashboard consume it unchanged.
    from graph.sdk import telemetry
    eta_1m = _eta_hours(credits, per_hour, 1_000_000)
    status = (f"{'running' if ops['running'] else 'stopped'} · {credits:,} cr · {per_hour:,} cr/hr"
              + (f" · ~{eta_1m}h to 1M" if eta_1m else ""))
    fleet_section = {
        "title": "Fleet",
        "columns": ["ship", "role", "status", "at", "cargo"],
        "rows": [[r["symbol"], r["role"] + (" ⚠" if r["stranded"] else ""),
                  r["status"], r["at"], r["cargo"]] for r in rows],
    }
    return telemetry(
        status=status,
        metrics={"credits": credits, "ships": len(ships), "cr/hr": per_hour},
        hints=hints,
        decisions=decisions()[-6:],
        sections=[fleet_section],
        # extras (consumed by the st_report tool / any caller):
        ok=True, credits=credits, ships=len(ships), strategy=current_strategy()["name"],
        mining=KNOBS.get("mining"),
        engine={"running": ops["running"], "window_min": KNOBS.get("window_minutes"),
                "last_gained": last.get("gained"), "per_hour": per_hour,
                "recent_log": ops["recent_log"][-4:]},
        projection={"to_250k": _eta_hours(credits, per_hour, 250_000), "to_1m": eta_1m},
        knobs=knobs(), overrides=overrides(), fleet=rows,
    )
