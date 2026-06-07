"""SpaceTraders agent tools — protoTrader-in-space's hands on the galaxy.

A curated slice of the v2 API shaped for an LLM: each tool returns a compact,
human-readable line the model can reason over (not raw JSON), and turns API
errors into a clean ``Error: …`` instead of raising. Enough to run the core loop
— register → check the fleet → scan for an asteroid/market → navigate → mine →
sell → work a contract. Disable with ``plugins: { disabled: [spacetraders] }``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from langchain_core.tools import tool

from .client import SpaceTradersError, call, save_token

# Active surveys per ship, cached so st_extract can target a deposit. Surveys
# expire server-side (a few minutes); we filter expired ones lazily on use.
_SURVEYS: dict[str, list[dict]] = {}

# Ship purchases above this (credits) pause for operator approval (HITL gate).
_BUY_APPROVAL_THRESHOLD = 100_000

# Waypoint coordinates, cached per session (static within a reset).
_COORDS: dict[str, tuple] = {}


async def _coord(system: str, waypoint: str) -> tuple:
    if waypoint not in _COORDS:
        w = await call("GET", f"/systems/{system}/waypoints/{waypoint}")
        _COORDS[waypoint] = (w["x"], w["y"])
    return _COORDS[waypoint]


async def _distance(system: str, a: str, b: str) -> float:
    (ax, ay), (bx, by) = await _coord(system, a), await _coord(system, b)
    return math.hypot(ax - bx, ay - by)


async def _nearest_fuel(system: str, here: str) -> tuple | None:
    """(waypoint, distance) of the closest FUEL_STATION to ``here``, or None."""
    stations = await call("GET", f"/systems/{system}/waypoints",
                          params={"traits": "FUEL_STATION", "limit": 20})
    best = None
    for s in stations:
        d = await _distance(system, here, s["symbol"])
        if best is None or d < best[1]:
            best = (s["symbol"], d)
    return best


def _eta_seconds(nav: dict) -> int | None:
    arr = nav.get("route", {}).get("arrival")
    if not arr:
        return None
    try:
        return max(0, round((datetime.fromisoformat(arr.replace("Z", "+00:00"))
                             - datetime.now(timezone.utc)).total_seconds()))
    except ValueError:
        return None


def _system_from_symbol(symbol: str) -> str:
    return _system_of(symbol)


def _system_of(waypoint: str) -> str:
    """X1-DF55-A1 → X1-DF55 (system = first two dash-segments)."""
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else waypoint


def _cargo_line(cargo: dict) -> str:
    items = cargo.get("inventory", [])
    if not items:
        return f"cargo {cargo.get('units', 0)}/{cargo.get('capacity', 0)} (empty)"
    held = ", ".join(f"{i['units']}×{i['symbol']}" for i in items)
    return f"cargo {cargo.get('units', 0)}/{cargo.get('capacity', 0)}: {held}"


def _ship_line(s: dict) -> str:
    nav = s.get("nav", {})
    fuel = s.get("fuel", {})
    # A 0-capacity fuel tank means a probe/satellite — it navigates for FREE (no
    # fuel needed), so it's never "stranded". Say so, or the model thinks it's stuck.
    if fuel.get("capacity", 0) == 0:
        fuel_str = "fuel n/a (flies free)"
    else:
        fuel_str = f"fuel {fuel.get('current', '?')}/{fuel.get('capacity', '?')}"
    return (
        f"{s['symbol']} — {nav.get('status', '?')} @ {nav.get('waypointSymbol', '?')} "
        f"({nav.get('systemSymbol', '?')}) | {fuel_str} | {_cargo_line(s.get('cargo', {}))}"
    )


# ── Identity / registration ──────────────────────────────────────────────────


@tool
async def st_register(symbol: str, faction: str = "COSMIC", account_token: str = "") -> str:
    """Register a NEW SpaceTraders agent and save its token for all later calls.

    Needs an account token (from your spacetraders.io account) — pass it as
    account_token, or set SPACETRADERS_ACCOUNT_TOKEN. Faction options include
    COSMIC (default), GALACTIC, QUANTUM, DOMINION, ASTRO.

    Args:
        symbol: the agent call sign, 3–14 chars, alphanumeric/_/- (e.g. "PROTOTRADER").
        faction: starting faction enum.
        account_token: your account bearer token (required to register).
    """
    from .client import account_token as _config_account_token
    tok = (account_token or "").strip() or _config_account_token()
    if not tok:
        return ("Error: registration needs an account token. Create an account at "
                "spacetraders.io and paste its account token in the console "
                "(System → Settings → SpaceTraders → Account token), or pass it as account_token.")
    try:
        data = await call("POST", "/register", token=tok,
                          json={"symbol": symbol, "faction": faction})
    except SpaceTradersError as e:
        return f"Error: {e}"
    agent_token = data.get("token", "")
    if agent_token:
        save_token(agent_token)
    ag = data.get("agent", {})
    ships = data.get("ships") or ([data["ship"]] if "ship" in data else [])
    contract = data.get("contract", {})
    return (
        f"✦ Registered {ag.get('symbol')} ({faction}) — credits {ag.get('credits'):,}, "
        f"HQ {ag.get('headquarters')}. Starting ships: {', '.join(s['symbol'] for s in ships)}. "
        f"First contract: {contract.get('id', 'none')}. Agent token saved."
    )


@tool
async def st_agent() -> str:
    """Your SpaceTraders agent's status: credits, HQ, faction, ship/account totals."""
    try:
        a = await call("GET", "/my/agent")
    except SpaceTradersError as e:
        return f"Error: {e}"
    return (f"✦ {a['symbol']} | credits {a['credits']:,} | HQ {a['headquarters']} | "
            f"faction {a.get('startingFaction', '?')} | ships {a.get('shipCount', '?')}")


# ── Fleet ────────────────────────────────────────────────────────────────────


@tool
async def st_fleet() -> str:
    """List all your ships with location, status, fuel, and cargo."""
    try:
        ships = await call("GET", "/my/ships")
    except SpaceTradersError as e:
        return f"Error: {e}"
    if not ships:
        return "No ships."
    return f"Fleet ({len(ships)}):\n" + "\n".join("  " + _ship_line(s) for s in ships)


@tool
async def st_autopilot_status() -> str:
    """Fleet overview: which ships are IDLE vs BUSY, plus whether the background
    autopilot is running and its recent progress.

    Use this to supervise — start the engine with st_autopilot_start, then poll this.
    """
    from . import fleet
    try:
        ships = await call("GET", "/my/ships")
    except SpaceTradersError as e:
        return f"Error: {e}"
    idle, busy = [], []
    for s in ships:
        st = s["nav"]["status"]
        cd = s.get("cooldown", {}).get("remainingSeconds", 0)
        role = "cargo" if s["cargo"]["capacity"] > 0 else "scout (flies free)"
        if st == "IN_TRANSIT":
            busy.append(f"{s['symbol']} (in transit, {_eta_seconds(s['nav']) or '?'}s)")
        elif cd:
            busy.append(f"{s['symbol']} (cooldown {cd}s)")
        else:
            idle.append(f"{s['symbol']} [{role}] @ {s['nav']['waypointSymbol']}")
    ops = fleet.ops_status()
    lines = [f"IDLE ({len(idle)}): {', '.join(idle) or '—'}",
             f"BUSY ({len(busy)}): {', '.join(busy) or '—'}"]
    if ops["running"]:
        lines.append(f"AUTOPILOT: running (~{ops['started_minutes']:g} min window)")
        for m in ops["recent_log"]:
            lines.append(f"  · {m}")
    elif ops["result"]:
        r = ops["result"]
        if "gained" in r:
            lines.append(f"AUTOPILOT: last run gained {r['gained']:,} cr (≈ {r['per_hour']:,} cr/hr)")
        elif "error" in r:
            lines.append(f"AUTOPILOT: last run errored — {r['error']}")
        elif r.get("stopped"):
            lines.append("AUTOPILOT: stopped")
    else:
        lines.append("AUTOPILOT: idle (start it with st_autopilot_start)")
    return "\n".join(lines)


@tool
async def st_autopilot_start(minutes: float = 20) -> str:
    """Start the fleet autopilot in the BACKGROUND for `minutes`, then return at once.

    Every cargo ship works procurement contracts back-to-back; probes scout. All
    ships share the one rate budget. This does NOT block — it runs while you do
    other things; poll st_autopilot_status to watch progress and credits. This is the
    hands-off way to keep the fleet earning.

    Args:
        minutes: how long the background run should last before it stops on its own.
    """
    from . import fleet
    return fleet.start_ops(float(minutes))


@tool
async def st_autopilot_stop() -> str:
    """Stop the background fleet autopilot if it's running."""
    from . import fleet
    return fleet.stop_ops()


@tool
async def st_ship(ship: str) -> str:
    """Full status for one ship — nav, fuel, cargo, and crew/cooldown.

    Args:
        ship: ship symbol, e.g. "PROTOTRADER-1".
    """
    try:
        s = await call("GET", f"/my/ships/{ship}")
    except SpaceTradersError as e:
        return f"Error: {e}"
    cd = s.get("cooldown", {}).get("remainingSeconds", 0)
    extra = f" | cooldown {cd}s" if cd else ""
    if s.get("nav", {}).get("status") == "IN_TRANSIT":
        eta = _eta_seconds(s["nav"])
        if eta is not None:
            extra += f" | arriving in {eta}s"
    return _ship_line(s) + extra


@tool
async def st_orbit(ship: str) -> str:
    """Move a ship into orbit (required before navigating, mining, or jumping).

    Args:
        ship: ship symbol.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/orbit")
    except SpaceTradersError as e:
        return f"Error: {e}"
    return f"{ship} now in ORBIT @ {d.get('nav', {}).get('waypointSymbol', '?')}"


@tool
async def st_dock(ship: str) -> str:
    """Dock a ship at its current waypoint (required to refuel, trade, or sell).

    Args:
        ship: ship symbol.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/dock")
    except SpaceTradersError as e:
        return f"Error: {e}"
    return f"{ship} now DOCKED @ {d.get('nav', {}).get('waypointSymbol', '?')}"


@tool
async def st_navigate(ship: str, waypoint: str, mode: str = "") -> str:
    """Low-level single hop with an explicit flight `mode` — prefer st_travel
    (it handles fuel + routing); use st_navigate only to force a mode. Auto-orbits.

    Burns fuel; returns the arrival time. Same system only — use a jump gate for
    interstellar travel. ``mode`` sets the flight mode before flying:
    CRUISE (default, fuel ≈ distance), DRIFT (≈1 fuel, slow — use to limp to a
    fuel station when low), BURN (2× fuel, fast), STEALTH. CRUISE fuel cost equals
    the distance, so if fuel < distance, DRIFT instead.

    Args:
        ship: ship symbol.
        waypoint: destination waypoint symbol, e.g. "X1-DF55-B6".
        mode: optional flight mode (CRUISE/DRIFT/BURN/STEALTH).
    """
    if mode:
        try:
            await call("PATCH", f"/my/ships/{ship}/nav", json={"flightMode": mode.upper()})
        except SpaceTradersError as e:
            return f"Error: {e}"
    try:
        await call("POST", f"/my/ships/{ship}/orbit")  # harmless if already orbiting
    except SpaceTradersError:
        pass
    try:
        d = await call("POST", f"/my/ships/{ship}/navigate", json={"waypointSymbol": waypoint})
    except SpaceTradersError as e:
        return f"Error: {e}"
    nav = d.get("nav", {})
    fuel = d.get("fuel", {})
    arrival = nav.get("route", {}).get("arrival", "?")
    return (f"{ship} → {waypoint} [{nav.get('flightMode', mode or 'CRUISE')}]: IN_TRANSIT, "
            f"arrival {arrival} | fuel {fuel.get('current', '?')}/{fuel.get('capacity', '?')}")


@tool
async def st_plan_route(system: str, from_waypoint: str, to_waypoint: str) -> str:
    """Plan a hop: distance, CRUISE fuel cost, and the nearest fuel station.

    CRUISE fuel cost ≈ the distance, so if a ship's fuel is below this, it can't
    reach the target in one CRUISE — DRIFT (≈1 fuel) to the nearest fuel station
    first, or use st_travel which handles that automatically.

    Args:
        system: system symbol, e.g. "X1-XU2".
        from_waypoint: origin waypoint symbol.
        to_waypoint: destination waypoint symbol.
    """
    try:
        d = await _distance(system, from_waypoint, to_waypoint)
        fuel = await _nearest_fuel(system, to_waypoint)
    except SpaceTradersError as e:
        return f"Error: {e}"
    fuel_note = (f"nearest fuel to dest: {fuel[0]} ({fuel[1]:.0f} away)"
                 if fuel else "no fuel station in system")
    return (f"{from_waypoint} → {to_waypoint}: distance {d:.0f}, "
            f"CRUISE fuel ≈ {d:.0f} | {fuel_note}")


@tool
async def st_travel(ship: str, destination: str) -> str:
    """The DEFAULT way to move a ship: send it to a destination, handling fuel
    automatically (one hop).

    The reliable way to move: it refuels if docked low on fuel, picks CRUISE when
    fuel covers the distance, and otherwise routes via the nearest fuel station —
    CRUISE there if reachable, else DRIFT (≈1 fuel, slow) to avoid stranding. This
    is ONE hop and returns immediately with an ETA; if it reports a fuel stop,
    wait for arrival (st_ship shows the ETA) and call st_travel again for the same
    destination until the ship is there. Same system only.

    Args:
        ship: ship symbol.
        destination: destination waypoint symbol in the same system.
    """
    try:
        s = await call("GET", f"/my/ships/{ship}")
    except SpaceTradersError as e:
        return f"Error: {e}"
    nav = s["nav"]
    here = nav["waypointSymbol"]
    system = nav["systemSymbol"]
    if nav["status"] == "IN_TRANSIT":
        eta = _eta_seconds(nav)
        return f"{ship} already IN_TRANSIT → {here}, arriving in {eta}s. Wait, then call again."
    if here == destination:
        return f"{ship} is already at {destination}."

    fuel = s["fuel"]["current"]
    cap = s["fuel"]["capacity"]
    # Top off if we're sitting on a fuel market and not full (cheap insurance).
    if cap and fuel < cap:
        try:
            await call("POST", f"/my/ships/{ship}/dock")
            r = await call("POST", f"/my/ships/{ship}/refuel")
            fuel = r.get("fuel", {}).get("current", fuel)
        except SpaceTradersError:
            pass  # this waypoint doesn't sell fuel — carry on with what we have

    try:
        dist = await _distance(system, here, destination)
    except SpaceTradersError as e:
        return f"Error: {e}"

    # Direct CRUISE if fuel covers it (or the ship doesn't use fuel, e.g. a probe).
    if cap == 0 or fuel >= dist + 1:
        return await st_navigate.ainvoke({"ship": ship, "waypoint": destination, "mode": "CRUISE"})

    # Not enough fuel — go via the nearest fuel station first.
    try:
        fstop = await _nearest_fuel(system, here)
    except SpaceTradersError as e:
        return f"Error: {e}"
    if not fstop or fstop[0] == here:
        # already at (or no) fuel station yet can't reach dest → DRIFT straight there
        out = await st_navigate.ainvoke({"ship": ship, "waypoint": destination, "mode": "DRIFT"})
        return f"(low fuel, no closer fuel station) {out}"
    fwp, fdist = fstop
    mode = "CRUISE" if fuel >= fdist + 1 else "DRIFT"
    out = await st_navigate.ainvoke({"ship": ship, "waypoint": fwp, "mode": mode})
    return f"FUEL STOP en route to {destination}: {out}\n  → on arrival, call st_travel({ship}, {destination}) again."


@tool
async def st_refuel(ship: str) -> str:
    """Refuel a ship (must be DOCKED at a waypoint that sells fuel).

    Args:
        ship: ship symbol.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/refuel")
    except SpaceTradersError as e:
        return f"Error: {e}"
    fuel = d.get("fuel", {})
    spent = d.get("transaction", {}).get("totalPrice", "?")
    return f"{ship} refueled to {fuel.get('current', '?')}/{fuel.get('capacity', '?')} (spent {spent} cr)"


# ── The galaxy: waypoints + markets ──────────────────────────────────────────


@tool
async def st_transfer(from_ship: str, to_ship: str, good: str, units: int) -> str:
    """Move cargo from one of YOUR ships to another (both at the same waypoint).

    The fleet-logistics move: a miner fills up, a hauler carries it to market. Only
    works between your own co-located ships — there's no trade with other players.

    Args:
        from_ship: ship to take cargo from.
        to_ship: ship to give cargo to.
        good: trade-good symbol.
        units: how many units to move.
    """
    try:
        d = await call("POST", f"/my/ships/{from_ship}/transfer",
                       json={"tradeSymbol": good.upper(), "units": int(units), "shipSymbol": to_ship})
    except SpaceTradersError as e:
        return f"Error: {e}"
    return f"Transferred {units}×{good.upper()} {from_ship} → {to_ship}. {_cargo_line(d.get('cargo', {}))}"


@tool
async def st_shipyard(waypoint: str) -> str:
    """List the ships a shipyard sells (with prices if one of your ships is there).

    Args:
        waypoint: a SHIPYARD waypoint symbol, e.g. "X1-XU2-A2".
    """
    system = _system_of(waypoint)
    try:
        sy = await call("GET", f"/systems/{system}/waypoints/{waypoint}/shipyard")
    except SpaceTradersError as e:
        return f"Error: {e}"
    ships = sy.get("ships")
    if ships:
        rows = [f"  {s['type']}: {s['purchasePrice']:,} cr — {s.get('frame', {}).get('name', '?')}"
                for s in ships]
        return f"Shipyard @ {waypoint} (live prices):\n" + "\n".join(rows)
    types = ", ".join(t["symbol"] if "symbol" in t else t["type"] for t in sy.get("shipTypes", []))
    return (f"Shipyard @ {waypoint} sells: {types or '—'}\n"
            f"  (dock a ship here to see prices; buy with st_buy_ship)")


@tool
async def st_buy_ship(waypoint: str, ship_type: str) -> str:
    """Buy a ship at a shipyard (one of your ships must be at the waypoint).

    Common types: SHIP_PROBE (cheap scout), SHIP_MINING_DRONE (mines unattended),
    SHIP_LIGHT_HAULER (cargo), SHIP_LIGHT_SHUTTLE. Use st_shipyard to see what's
    sold + the price.

    Args:
        waypoint: the SHIPYARD waypoint symbol.
        ship_type: the ship type to buy, e.g. "SHIP_MINING_DRONE".
    """
    # HITL gate: a high-value purchase pauses for operator approval (LangGraph
    # interrupt → A2A input-required → resume). The operator's fleet spend over a
    # threshold is genuinely their call, not the agent's.
    system = _system_of(waypoint)
    price = None
    try:
        sy = await call("GET", f"/systems/{system}/waypoints/{waypoint}/shipyard")
        price = next((sh["purchasePrice"] for sh in sy.get("ships", [])
                      if sh.get("type") == ship_type.upper()), None)
    except SpaceTradersError:
        pass  # price unknown → buy without the gate (the API still enforces credits)
    if price is not None and price > _BUY_APPROVAL_THRESHOLD:
        from langgraph.types import interrupt
        decision = interrupt({
            "kind": "approval",
            "title": "Approve high-value ship purchase?",
            "message": f"Buy {ship_type.upper()} at {waypoint} for {price:,} cr? "
                       f"Reply 'approve' to proceed or 'deny' to cancel.",
            "amount": price,
        })
        if str(decision).strip().lower() not in ("approve", "approved", "yes", "y", "ok", "true"):
            return f"Purchase of {ship_type.upper()} ({price:,} cr) cancelled — operator did not approve."
    try:
        d = await call("POST", "/my/ships",
                       json={"shipType": ship_type.upper(), "waypointSymbol": waypoint})
    except SpaceTradersError as e:
        return f"Error: {e}"
    s = d.get("ship", {})
    t = d.get("transaction", {})
    ag = d.get("agent", {})
    return (f"✦ Bought {s.get('symbol')} ({ship_type.upper()}) for "
            f"{t.get('price', '?'):,} cr. Credits now {ag.get('credits', '?'):,}.")


@tool
async def st_waypoints(system: str, trait: str = "") -> str:
    """Scan a system's waypoints, optionally filtered by a trait.

    Useful traits: MARKETPLACE, SHIPYARD, ASTEROID, ASTEROID_FIELD, FUEL_STATION,
    JUMP_GATE. Omit trait to list everything.

    Args:
        system: system symbol, e.g. "X1-DF55".
        trait: optional trait filter.
    """
    params = {"limit": 20}
    if trait:
        params["traits"] = trait.upper()
    try:
        wps = await call("GET", f"/systems/{system}/waypoints", params=params)
    except SpaceTradersError as e:
        return f"Error: {e}"
    if not wps:
        return f"No waypoints in {system}" + (f" with trait {trait}" if trait else "")
    lines = []
    for w in wps:
        traits = ",".join(t["symbol"] for t in w.get("traits", []))
        lines.append(f"  {w['symbol']} [{w['type']}] {traits}")
    return f"{system} waypoints ({len(wps)}):\n" + "\n".join(lines)


@tool
async def st_market(waypoint: str) -> str:
    """Market at a waypoint: imports, exports, and live trade-good prices.

    Live purchase/sell prices only show when one of YOUR ships is at the
    waypoint; otherwise you see the import/export list.

    Args:
        waypoint: a MARKETPLACE waypoint symbol, e.g. "X1-DF55-B6".
    """
    system = _system_of(waypoint)
    try:
        m = await call("GET", f"/systems/{system}/waypoints/{waypoint}/market")
    except SpaceTradersError as e:
        return f"Error: {e}"
    goods = m.get("tradeGoods")
    if goods:
        rows = [f"  {g['symbol']}: buy {g['purchasePrice']} / sell {g['sellPrice']} "
                f"({g.get('supply', '?')}, vol {g.get('tradeVolume', '?')})" for g in goods]
        return f"Market @ {waypoint} (live prices):\n" + "\n".join(rows)
    imp = ", ".join(g["symbol"] for g in m.get("imports", [])) or "—"
    exp = ", ".join(g["symbol"] for g in m.get("exports", [])) or "—"
    exch = ", ".join(g["symbol"] for g in m.get("exchange", [])) or "—"
    return (f"Market @ {waypoint} (no ship present — list only):\n"
            f"  imports: {imp}\n  exports: {exp}\n  exchange: {exch}")


async def _good_markets(system: str, good: str) -> tuple:
    """(buy_waypoints, sell_waypoints) for a good — where it's exported/exchanged
    (buy here) vs imported (sell/deliver here). Plain helper shared by the tool and
    the fleet engine."""
    want = good.upper()
    wps = await call("GET", f"/systems/{system}/waypoints",
                     params={"traits": "MARKETPLACE", "limit": 20})
    buy, sell = [], []
    for w in wps:
        try:
            m = await call("GET", f"/systems/{system}/waypoints/{w['symbol']}/market")
        except SpaceTradersError:
            continue
        if want in {g["symbol"] for g in m.get("exports", [])} | {g["symbol"] for g in m.get("exchange", [])}:
            buy.append(w["symbol"])
        if want in {g["symbol"] for g in m.get("imports", [])}:
            sell.append(w["symbol"])
    return buy, sell


@tool
async def st_find_market(system: str, good: str) -> str:
    """Scan every market in a system for who SELLS or BUYS a good (no ship needed).

    Reads each marketplace's import/export/exchange lists — the supply chain map
    for a trade or a procurement contract: buy where it's exported, sell/deliver
    where it's imported.

    Args:
        system: system symbol, e.g. "X1-XU2".
        good: trade-good symbol, e.g. "ALUMINUM_ORE".
    """
    try:
        buy, sell = await _good_markets(system, good)
    except SpaceTradersError as e:
        return f"Error: {e}"
    return (f"{good.upper()} in {system}:\n"
            f"  BUY at:  {', '.join(buy) or '— none'}\n"
            f"  SELL at: {', '.join(sell) or '— none'}")


@tool
async def st_trade_routes(system: str, limit: int = 8) -> str:
    """Map a system's arbitrage routes — goods exported one place and imported
    another, so you can buy low and sell high.

    Structural map from each market's import/export lists (no ship needed; live
    per-unit prices need a ship at the market — station a probe to scout them).
    Each route is a good with a source (export) and a sink (import).

    Args:
        system: system symbol, e.g. "X1-XU2".
        limit: max routes to list.
    """
    try:
        wps = await call("GET", f"/systems/{system}/waypoints",
                         params={"traits": "MARKETPLACE", "limit": 20})
    except SpaceTradersError as e:
        return f"Error: {e}"
    exporters: dict[str, list] = {}
    importers: dict[str, list] = {}
    markets: list[dict] = []
    for w in wps:
        try:
            m = await call("GET", f"/systems/{system}/waypoints/{w['symbol']}/market")
        except SpaceTradersError:
            continue
        markets.append({"waypointSymbol": w["symbol"], "tradeGoods": m.get("tradeGoods", [])})
        for g in m.get("exports", []):
            exporters.setdefault(g["symbol"], []).append(w["symbol"])
        for g in m.get("imports", []):
            importers.setdefault(g["symbol"], []).append(w["symbol"])
    routes = []
    for good in sorted(set(exporters) & set(importers)):
        routes.append(f"  {good}: buy @ {exporters[good][0]} → sell @ {importers[good][0]}")
    if not routes:
        return f"No export→import arbitrage routes found in {system}."
    # Price-ranked best, where live prices exist (a ship has visited the market).
    from .analysis import best_arbitrage
    best = best_arbitrage(markets)
    head = (f"💰 Best by price: {best['good']} buy @ {best['buy_at']} → sell @ "
            f"{best['sell_at']} (+{best['profit_per_unit']:,}/unit)\n" if best else "")
    return (head + f"Trade routes in {system} (buy-export → sell-import):\n"
            + "\n".join(routes[:limit])
            + "\n  (station a ship/probe at a market for live per-unit prices)")


# ── Mining + trading ─────────────────────────────────────────────────────────


@tool
async def st_survey(ship: str) -> str:
    """Survey the current asteroid to reveal its deposits (needs a SURVEYOR mount).

    Caches the surveys so a later st_extract(ship, prefer="ALUMINUM_ORE") can
    target a specific ore instead of mining blind. Surveys expire after a few
    minutes. Ship must be in ORBIT at an asteroid.

    Args:
        ship: ship symbol.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/survey")
    except SpaceTradersError as e:
        return f"Error: {e}"
    surveys = d.get("surveys", [])
    _SURVEYS[ship] = surveys
    cd = d.get("cooldown", {}).get("remainingSeconds", "?")
    lines = []
    for sv in surveys:
        deps = ", ".join(sorted({x["symbol"] for x in sv.get("deposits", [])}))
        lines.append(f"  [{sv['size']}] {deps}")
    return f"{ship} surveyed {len(surveys)} site(s) | cooldown {cd}s:\n" + "\n".join(lines)


@tool
async def st_extract(ship: str, prefer: str = "") -> str:
    """Mine the current waypoint (ship must be in ORBIT at an asteroid).

    Yields ore into cargo and starts a cooldown. If ``prefer`` is given and a
    cached survey (from st_survey) for this ship contains that good, the extract
    targets it; otherwise it mines whatever the rock gives.

    Args:
        ship: ship symbol.
        prefer: optional trade-good to target, e.g. "ALUMINUM_ORE" (needs a prior survey).
    """
    body = None
    if prefer:
        want = prefer.upper()
        for sv in _SURVEYS.get(ship, []):
            if any(x.get("symbol") == want for x in sv.get("deposits", [])):
                body = {"survey": sv}
                break
    try:
        d = await call("POST", f"/my/ships/{ship}/extract", json=body)
    except SpaceTradersError as e:
        if body and "survey" in str(e).lower():        # survey expired/exhausted — drop & mine plain
            _SURVEYS.pop(ship, None)
            return await st_extract.ainvoke({"ship": ship})
        return f"Error: {e}"
    y = d.get("extraction", {}).get("yield", {})
    cd = d.get("cooldown", {}).get("remainingSeconds", "?")
    cargo = d.get("cargo", {})
    tag = " (targeted)" if body else ""
    return (f"{ship} extracted {y.get('units', '?')}×{y.get('symbol', '?')}{tag} | "
            f"cooldown {cd}s | {_cargo_line(cargo)}")


@tool
async def st_jettison(ship: str, good: str, units: int) -> str:
    """Dump cargo into space — free up the hold for what you actually want.

    Args:
        ship: ship symbol.
        good: trade-good symbol to jettison.
        units: how many units to dump.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/jettison",
                       json={"symbol": good.upper(), "units": int(units)})
    except SpaceTradersError as e:
        return f"Error: {e}"
    return f"{ship} jettisoned {units}×{good.upper()}. {_cargo_line(d.get('cargo', {}))}"


@tool
async def st_purchase(ship: str, good: str, units: int) -> str:
    """Buy cargo at the current market (ship must be DOCKED at a MARKETPLACE).

    Args:
        ship: ship symbol.
        good: trade-good symbol to buy, e.g. "ALUMINUM_ORE".
        units: how many units to buy.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/purchase",
                       json={"symbol": good.upper(), "units": int(units)})
    except SpaceTradersError as e:
        return f"Error: {e}"
    t = d.get("transaction", {})
    ag = d.get("agent", {})
    return (f"Bought {t.get('units', '?')}×{t.get('tradeSymbol', good)} for "
            f"{t.get('totalPrice', '?')} cr ({t.get('pricePerUnit', '?')}/u). "
            f"Credits now {ag.get('credits', '?'):,}. {_cargo_line(d.get('cargo', {}))}")


@tool
async def st_sell(ship: str, good: str, units: int) -> str:
    """Sell cargo at the current market (ship must be DOCKED at a MARKETPLACE).

    Args:
        ship: ship symbol.
        good: trade-good symbol to sell, e.g. "IRON_ORE".
        units: how many units to sell.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/sell",
                       json={"symbol": good.upper(), "units": int(units)})
    except SpaceTradersError as e:
        return f"Error: {e}"
    t = d.get("transaction", {})
    ag = d.get("agent", {})
    return (f"Sold {t.get('units', '?')}×{t.get('tradeSymbol', good)} for "
            f"{t.get('totalPrice', '?')} cr ({t.get('pricePerUnit', '?')}/u). "
            f"Credits now {ag.get('credits', '?'):,}. {_cargo_line(d.get('cargo', {}))}")


# ── Contracts ────────────────────────────────────────────────────────────────


@tool
async def st_contracts() -> str:
    """List your contracts — terms, pay, deadline, and accepted/fulfilled state."""
    try:
        cs = await call("GET", "/my/contracts")
    except SpaceTradersError as e:
        return f"Error: {e}"
    if not cs:
        return "No contracts."
    lines = []
    for c in cs:
        terms = c.get("terms", {})
        pay = terms.get("payment", {})
        deliver = "; ".join(
            f"{d['unitsFulfilled']}/{d['unitsRequired']} {d['tradeSymbol']} → {d['destinationSymbol']}"
            for d in terms.get("deliver", [])
        ) or "—"
        state = "fulfilled" if c.get("fulfilled") else ("accepted" if c.get("accepted") else "OPEN")
        lines.append(f"  {c['id']} [{c['type']}, {state}] pay {pay.get('onAccepted', 0)}+"
                     f"{pay.get('onFulfilled', 0)} cr | deliver: {deliver} | by {terms.get('deadline', '?')}")
    return f"Contracts ({len(cs)}):\n" + "\n".join(lines)


@tool
async def st_accept_contract(contract_id: str) -> str:
    """Accept a contract by id (pays the on-accepted advance immediately).

    Args:
        contract_id: the contract id from st_contracts.
    """
    try:
        d = await call("POST", f"/my/contracts/{contract_id}/accept")
    except SpaceTradersError as e:
        return f"Error: {e}"
    ag = d.get("agent", {})
    return f"Accepted {contract_id}. Advance paid — credits now {ag.get('credits', '?'):,}."


@tool
async def st_negotiate_contract(ship: str) -> str:
    """Get a fresh contract (a ship must be at a faction's waypoint, e.g. its HQ).

    Use when you've run out of work — negotiates a new contract from the local
    faction. You can only hold a limited number of unfulfilled contracts at once.

    Args:
        ship: a ship parked at a faction-controlled waypoint.
    """
    try:
        d = await call("POST", f"/my/ships/{ship}/negotiate/contract")
    except SpaceTradersError as e:
        return f"Error: {e}"
    c = d.get("contract", {})
    terms = c.get("terms", {})
    deliver = "; ".join(f"{x['unitsRequired']} {x['tradeSymbol']} → {x['destinationSymbol']}"
                        for x in terms.get("deliver", [])) or "—"
    pay = terms.get("payment", {})
    return (f"New contract {c.get('id')} [{c.get('type')}]: deliver {deliver} | "
            f"pay {pay.get('onAccepted', 0)}+{pay.get('onFulfilled', 0)} cr. Accept it to start.")


@tool
async def st_deliver(contract_id: str, ship: str, good: str, units: int) -> str:
    """Deliver cargo toward a contract (ship must be DOCKED at the destination).

    Args:
        contract_id: the contract id.
        ship: the ship carrying the goods.
        good: trade-good symbol to deliver, e.g. "ALUMINUM_ORE".
        units: how many units to deliver.
    """
    try:
        d = await call("POST", f"/my/contracts/{contract_id}/deliver",
                       json={"shipSymbol": ship, "tradeSymbol": good.upper(), "units": int(units)})
    except SpaceTradersError as e:
        return f"Error: {e}"
    deliv = d.get("contract", {}).get("terms", {}).get("deliver", [{}])[0]
    return (f"Delivered {units}×{good.upper()} to {contract_id}: now "
            f"{deliv.get('unitsFulfilled', '?')}/{deliv.get('unitsRequired', '?')}. "
            f"{_cargo_line(d.get('cargo', {}))}")


@tool
async def st_fulfill_contract(contract_id: str) -> str:
    """Fulfill a fully-delivered contract — collect the on-fulfilled payout.

    Args:
        contract_id: the contract id (all deliver terms must be met).
    """
    try:
        d = await call("POST", f"/my/contracts/{contract_id}/fulfill")
    except SpaceTradersError as e:
        return f"Error: {e}"
    ag = d.get("agent", {})
    return f"✦ Contract {contract_id} FULFILLED. Credits now {ag.get('credits', '?'):,}."


def _harden(tools: list) -> list:
    """Belt-and-suspenders: a tool that raises crashes the agent's whole turn, so
    wrap every coroutine to turn ANY error into a readable 'Error: ...' string the
    model can recover from. (create_agent doesn't expose handle_tool_errors.)"""
    def _wrap(fn):
        async def _safe(**kwargs):
            try:
                return await fn(**kwargs)
            except SpaceTradersError as e:
                return f"Error: {e}"
            except BaseException as e:  # noqa: BLE001
                # NEVER swallow LangGraph control flow (interrupt/resume for HITL,
                # GraphBubbleUp) — re-raise so a tool can pause the turn for an
                # operator approval. Only game/runtime errors become strings.
                if type(e).__module__.split(".")[0] == "langgraph":
                    raise
                if not isinstance(e, Exception):
                    raise
                return f"Error: unexpected {type(e).__name__}: {e}"
        return _safe
    for t in tools:
        if getattr(t, "coroutine", None) is not None:
            t.coroutine = _wrap(t.coroutine)
    return tools


def get_spacetraders_tools() -> list:
    return _harden([
        st_register, st_agent, st_fleet, st_autopilot_status,
        st_autopilot_start, st_autopilot_stop, st_ship,
        st_orbit, st_dock, st_navigate, st_travel, st_plan_route, st_refuel,
        st_transfer, st_shipyard, st_buy_ship,
        st_waypoints, st_market, st_find_market, st_trade_routes,
        st_survey, st_extract, st_jettison, st_purchase, st_sell,
        st_contracts, st_accept_contract, st_negotiate_contract,
        st_deliver, st_fulfill_contract,
    ])
