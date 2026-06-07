---
name: run-a-procurement-contract
description: >-
  Use when working a SpaceTraders PROCUREMENT contract — "deliver N units of X to
  waypoint Y". Covers the efficient buy-low / deliver pattern and when to delegate
  to the procurement-run workflow vs the trader/navigator crew. Triggers on "work
  the contract", "fulfill the contract", "we need to deliver", "get the ore".
---

# Run a procurement contract 🪐

A procurement contract = deliver **N units of a good** to a **destination
waypoint** for a payout (a small advance on accept, the big payment on fulfill).
The good is the prize; the only question is the cheapest way to get the units.

## Decide: buy or mine?
1. `st_find_market(system, good)` — where is the good **exported** (buy cheap) and
   where is it **imported** (the delivery sink, usually the contract destination)?
2. **Buy** if a market exports it — no mining cooldowns, far faster. This is the
   default for procurement.
3. **Mine** only if nothing sells it (or it's much cheaper to dig). Note: not every
   asteroid holds every ore — survey before committing a long mining run.

## The loop (one delivery trip; repeat if units > cargo capacity)
1. Accept the contract (`st_accept_contract`) if you haven't — bank the advance.
2. Move the carrier ship to the **buy** waypoint and **dock** (navigator).
3. Clear the hold of anything you don't need (`st_jettison`/`st_sell`), then
   `st_purchase` the good up to cargo capacity (trader).
4. Move to the **delivery** waypoint and **dock** (navigator).
5. `st_deliver(contract_id, ship, good, units)` (trader). When delivered ≥ required,
   `st_fulfill_contract` — that payout is the win.

## Delegate, don't micromanage
For the whole run in one shot, call:
```
run_workflow("procurement-run",
  {"good": "<GOOD>", "system": "<SYS>", "ship": "<SHIP>",
   "contract_id": "<ID>", "units": <N>})
```
It scouts → flies → buys → flies → delivers/fulfills over the **trader** and
**navigator** crew. For a single leg, delegate directly: `task` the **navigator**
to move a ship, the **trader** to buy/sell/deliver, the **miner** to dig an ore.

## Watch out
- **Cargo cap < required units** → multiple trips. Deliver per trip; fulfill only
  once the total is met.
- **Rate limit:** the tools self-pace (~2 req/s) — fine, but don't spin tight
  status-poll loops by hand.
- **Wipe cycle:** the universe resets every few weeks — ship/system/waypoint/
  contract ids are all per-reset. Re-scan with the tools each session; never assume
  yesterday's ids.
