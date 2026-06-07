---
name: maximize-credits-per-hour
description: >-
  Use to run the SpaceTraders fleet autonomously toward a goal — default: make the
  most credits per hour. Drives the fleet-commander + autopilot engine, and sets up
  the recurring loop (scheduler tick + goal). Triggers on "manage the fleet", "make
  money autonomously", "run the fleet", "maximize credits", "keep the fleet busy".
---

# Maximize credits per hour 🪐

The standing objective for protoTrader-in-space: keep every ship earning, hands-off,
toward a goal — by default, **the most credits per hour**. You *supervise*; the
**autopilot engine** executes. Don't fly ships leg by leg.

## The autonomous loop
1. **Assess** — `st_agent` (credits), `st_fleet_status` (idle vs busy), `st_contracts`.
2. **Run the engine** — `st_fleet_start(minutes)` (background). It drives every ship at once
   under the one per-account rate budget: cargo ships work procurement contracts
   back-to-back (negotiate → buy → deliver → fulfill), probes scout prices. Pick a
   15–30 min window. It returns **credits gained + cr/hr** and per-ship results.
3. **Review & adapt** — if a ship stalled (no sourceable contract, broke), check
   `st_negotiate_contract` / `st_trade_routes` / `st_find_market`; if a faster ship
   pays for itself, `st_buy_ship`. Note whether cr/hr is trending up.
4. **Repeat** the window.

Delegate the whole thing to the **fleet-commander** subagent
(`task("fleet-commander", "run the fleet for 30 min toward max cr/hr and report")`)
— it owns this loop.

## Make it truly autonomous (recurring, unattended)
Set it on a timer so it runs without you:
- **Scheduler tick:** `schedule_task` a recurring prompt, e.g. every 30 min:
  *"Run st_fleet_start(25), poll st_fleet_status, log the cr/hr; if a ship is stuck, fix it."*
  Each tick wakes the agent, runs a window, and adapts — a continuous money loop.
- **Goal:** for a target (not just open-ended), set a goal like *"reach 1,000,000
  credits"* and let auto-mode iterate the loop until the verifier passes.

## Why one engine, not many scripts
The rate limit is **per account** (~2 req/s), shared by all ships — but ships
mostly wait on travel/cooldowns, so one engine covers a whole fleet easily. Running
separate processes each self-pacing to 2 req/s sums past the limit (429s). The
autopilot engine is one in-process client = one budget = correct concurrency. Always
drive the fleet through it, not parallel ad-hoc scripts.

## Picking work for max cr/hr
- **Contracts are the big earners** — a single procurement can pay six figures
  (FOOD: 49.9k advance + 142k fulfill). Keep every cargo ship on one.
- **Trade loops** fill gaps: `st_trade_routes(system)` → buy-export, sell-import;
  station a probe at a market for live per-unit prices to find the fat margins.
- **Mining** is the fallback when nothing's sourceable to buy.

## Constraints to respect
- **Wipe cycle:** the universe resets every few weeks — ship/system/waypoint/
  contract ids are all per-reset. Re-assess with the tools each session; never assume
  yesterday's ids or balances.
- Report honestly — real credits, real cr/hr. The number is the scoreboard.
