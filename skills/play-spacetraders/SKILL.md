---
name: play-spacetraders
description: >-
  Use when the operator wants to play SpaceTraders — command the fleet, check the
  agent, mine asteroids, trade goods, or work a contract in the live galaxy
  (spacetraders.io). Drives the loop: status → scan → navigate → mine/trade →
  sell → contract, using the st_* tools. Triggers on "check our agent", "what
  ships do we have", "go mine", "find a market", "fly to", "work the contract".
---

# Play SpaceTraders 🛰️

protoTrader-in-space commands a real agent in the **live** SpaceTraders universe —
a shared, persistent economy that resets every few weeks. Everything is the `st_*`
tools; there is no local simulation. Money is credits; positions are ships at
waypoints; the "market" is real supply/demand at each station.

## 0. First time only — register
If `st_agent` errors with "no token", the agent isn't registered yet. Register
with `st_register(symbol, faction, account_token)` — the **account_token** comes
from the operator's spacetraders.io account (ask for it; never invent one). The
agent token is then saved and every other tool just works.

## 1. Orient
- `st_agent` — credits, HQ, faction, ship count.
- `st_fleet` — every ship: where it is, docked/orbit, fuel, cargo.
- `st_contracts` — open work and what it pays. `st_accept_contract(id)` takes one
  (pays an advance up front).

The HQ waypoint tells you your home **system** (e.g. HQ `X1-DF55-A1` → system
`X1-DF55`).

## 2. Scan the system
- `st_waypoints(system, "ASTEROID")` — rocks to mine.
- `st_waypoints(system, "MARKETPLACE")` — where to buy/sell.
- `st_market(waypoint)` — live buy/sell prices (only show with one of YOUR ships
  there; otherwise you see the import/export list, which tells you who BUYS what).

## 3. Move a ship — use st_travel
`st_travel(ship, waypoint)` is the reliable mover: it refuels when low, CRUISEs
when fuel covers the distance, and otherwise routes via the nearest fuel station
(DRIFTing only if truly stranded — CRUISE fuel cost ≈ distance, so a low tank far
from fuel strands a ship). ONE hop returning an ETA; if it reports a fuel stop,
wait (`st_ship` shows "arriving in Ns") and call it again for the same destination
until the ship is there. `st_plan_route(system, from, to)` previews distance + fuel.
Then `st_dock` to trade/refuel, `st_orbit` to mine or fly. Raw
`st_navigate(ship, waypoint, mode)` (CRUISE/DRIFT/BURN) is the manual escape hatch.

## 4. Two ways to make credits
- **Mine:** orbit an `ASTEROID`, `st_survey` then `st_extract(ship, prefer="ORE")`
  repeatedly (cooldown between — check `st_ship`). Fill cargo, haul to a
  `MARKETPLACE` that imports that ore, dock, `st_sell`. The whole loop is the
  **`mining-run`** workflow.
- **Trade:** `st_trade_routes(system)` maps buy-low/sell-high routes; buy a good
  where it's exported, carry it to where it's imported, sell dear.

## 5. Work the contract
Most contracts want N units delivered to a waypoint. Mine or buy the good, deliver
it, fulfill — the on-fulfilled payment is the prize. The whole thing is the
**`procurement-run`** workflow. Out of work? `st_negotiate_contract(ship)` at a
faction waypoint pulls a fresh one.

## 6. Scale the fleet
One ship is slow; the galaxy rewards a fleet. `st_shipyard(waypoint)` lists ships
for sale; **`fleet-bootstrap`** flies a scout to a shipyard and buys one (a mining
drone mines unattended, a hauler carries cargo, a probe scouts market prices).
`st_transfer` moves cargo between your own co-located ships (miner → hauler).
Delegate the work: the **navigator**, **trader**, and **miner** subagents each
take a scoped objective.

## Rules of the road
- Check before you act: a ship can't navigate while `IN_TRANSIT`, can't mine while
  `DOCKED`, can't sell while in `ORBIT`. If a tool errors, read it — it usually
  says exactly which state you need.
- Watch fuel before a long hop; strand a ship and it's stuck.
- One step at a time, narrate what you see, and let the operator steer the
  strategy. This is a game — have fun with it, but report honestly (real credits,
  real cargo, no made-up numbers).
