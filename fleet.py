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


async def travel_to(sym: str, dest: str, *, max_hops: int = 12, log=None) -> bool:
    """Get a ship parked at ``dest``, looping st_travel through any fuel stops.

    st_travel issues one hop (CRUISE / DRIFT / fuel-station detour); we wait out
    each leg and call again until the ship is actually at the destination.
    """
    for _ in range(max_hops):
        s = await _ship(sym)
        nav = s["nav"]
        if nav["status"] != "IN_TRANSIT" and nav["waypointSymbol"] == dest:
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


async def _buy(sym: str, good: str, target: int, *, log=None) -> None:
    await C.call("POST", f"/my/ships/{sym}/dock")
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
        await _buy(sym, good, min(req - done, cap), log=log)
        if await _held(sym, good) == 0:
            return f"could not buy {good} (out of credits/supply) at {done}/{req}"
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


async def job_trade(sym: str, good: str, buy_wp: str, sell_wp: str, *, log=None) -> str:
    """One buy-low / sell-high round trip for a good."""
    cap = (await _ship(sym))["cargo"]["capacity"]
    await travel_to(sym, buy_wp, log=log)
    await _dump_except(sym, good, log=log)
    await _buy(sym, good, cap, log=log)
    held = await _held(sym, good)
    if held == 0:
        return f"could not buy {good} at {buy_wp}"
    await travel_to(sym, sell_wp, log=log)
    await C.call("POST", f"/my/ships/{sym}/dock")
    r = await C.call("POST", f"/my/ships/{sym}/sell", json={"symbol": good, "units": held})
    return f"traded {held}×{good} → {r['transaction']['totalPrice']:,} cr"


async def job_scout(sym: str, market_waypoints: list, *, log=None) -> dict:
    """Visit markets and record live per-unit prices (a ship present unlocks them)."""
    prices: dict[str, dict] = {}
    for wp in market_waypoints:
        if not await travel_to(sym, wp, log=log):
            continue
        await C.call("POST", f"/my/ships/{sym}/dock")
        system = T._system_of(wp)
        m = await C.call("GET", f"/systems/{system}/waypoints/{wp}/market")
        prices[wp] = {g["symbol"]: (g["purchasePrice"], g["sellPrice"])
                      for g in m.get("tradeGoods", [])}
    return prices


# ── autopilot — drive the whole fleet toward an objective for a time window ──


def _now() -> float:
    return asyncio.get_event_loop().time()


async def _contract_loop(sym: str, deadline: float, claimed: set, lock, *, log=None) -> str:
    """A cargo ship: claim/negotiate a procurement contract, work it, repeat."""
    n = 0
    while _now() < deadline:
        async with lock:
            cs = await C.call("GET", "/my/contracts")
            cid = next((c["id"] for c in cs
                        if c["accepted"] and not c["fulfilled"] and c["id"] not in claimed), None)
            if not cid:
                try:
                    d = await C.call("POST", f"/my/ships/{sym}/negotiate/contract")
                    cid = d["contract"]["id"]
                    await C.call("POST", f"/my/contracts/{cid}/accept")
                except C.SpaceTradersError as e:
                    return f"{n} contract(s) done; no more available ({e})"
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


async def autopilot(minutes: float, *, log=None) -> dict:
    """Run the fleet toward max credits for ``minutes``. Contracts are per-AGENT
    (only one active), so ONE cargo ship works contracts (negotiate→buy→deliver→
    fulfill, repeat) while the others mine ore and sell it; probes scout markets.
    One shared rate budget. Returns a credits/$-per-hour summary + per-ship results."""
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

    # Starter policy (simple, never loss-making): ONE cargo ship works contracts —
    # the reliable earner. Other cargo ships stay PARKED: default mining/trading
    # just burned fuel for ~0 cr (and over-mining crashes prices), so it's an opt-in
    # expansion (job_mining / job_trade / the workflows), not the default. Probes
    # scout for free. The operator grows into multi-ship trade when ready.
    jobs = {}
    cargo_ships = [s for s in ships if s["cargo"]["capacity"] > 0]
    if cargo_ships:
        sym = cargo_ships[0]["symbol"]
        jobs[sym] = _contract_loop(sym, deadline, claimed, lock, log=log)
    for s in ships:  # probes scout for free — no fuel, no loss
        if s["cargo"]["capacity"] == 0 and market_wps:
            jobs[s["symbol"]] = job_scout(s["symbol"], market_wps[:8], log=log)

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
    _OPS["log"] = []
    try:
        _OPS["result"] = await autopilot(minutes, log=lambda m: _OPS["log"].append(m))
    except asyncio.CancelledError:
        _OPS["result"] = {"stopped": True}
        raise
    except Exception as e:  # noqa: BLE001 — surface, don't crash the loop
        _OPS["result"] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        _OPS["task"] = None


def start_ops(minutes: float) -> str:
    task = _OPS.get("task")
    if task is not None and not task.done():
        return "Fleet ops already running — check status before starting another."
    _OPS["started_minutes"] = minutes
    _OPS["result"] = None
    _OPS["task"] = asyncio.create_task(_run_ops(minutes))
    return (f"Fleet ops started in the background for ~{minutes:g} min. The whole "
            f"fleet is working under one rate budget. Check st_fleet_status.")


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
