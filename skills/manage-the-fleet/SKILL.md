---
name: manage-the-fleet
description: >-
  The OODA strategist loop for the SpaceTraders fleet — the agentic BRAIN that steers the
  deterministic autopilot engine between windows. Each tick: Observe (st_report) → Orient
  (recall lessons, compare vs the goal) → Decide → Act (st_strategy / st_tune / st_assign /
  reinvest / un-strand) → Learn (memory_ingest). Sets up the recurring schedule + goal that
  makes it self-driving and self-improving. Triggers on "manage the fleet", "run the fleet
  to a million", "strategize the fleet", "keep steering the fleet", "self-improve the fleet".
---

# Manage the fleet — the OODA strategist loop 🧠🪐

This is the **two-loop fleet**. There are two control loops at different speeds, and *you
are the slow one*:

- **Inner loop (the muscle):** the deterministic `autopilot` engine runs windows
  back-to-back under one rate limiter — contracts, trade, mining, scouting — with a watchdog
  that re-kicks crashes, breaks stalls, and recovers from universe resets. It does NOT need
  you to click every leg. Start it once; it self-perpetuates.
- **Outer loop (the brain — YOU):** between windows you run **OODA** — observe what the
  engine did, orient against the goal and what you've learned, decide a steering change, act
  through the control surface, and record what you learned. The engine is the muscle; you are
  the strategist and the exception handler.

The whole point is to **demonstrate a self-improving agent**: not a static heuristic bot, but
one that watches its own performance, re-strategizes, and gets better across windows **and
across universe resets** (lessons are durable knowledge).

## One OODA tick

Run this every time you're woken (a scheduler tick, a notification, or by hand). Keep it
tight — a few tool calls, one decision, then let the engine work.

1. **OBSERVE** — `st_report`. It's built for this: credits + **cr/hr trajectory toward 1M**,
   each ship's role/status/health (it flags ⚠STRANDED), the live knobs + strategy + pins,
   your recent decisions, and deterministic **HINTS**. Read the hints first.

2. **ORIENT** — make sense of it:
   - `memory_recall` the SpaceTraders lessons (saturation, supply-chain, zero-to-million) and
     any route/strategy findings you saved on a previous tick or a previous reset.
   - Compare the **trajectory** to the goal: at this cr/hr, do you reach 1M before the weekly
     reset? Is cr/hr trending up or down vs last window (`MY RECENT DECISIONS` + `RECENT`)?
   - Diagnose the gap: saturating a market? capital sitting idle? a ship stranded? wrong
     doctrine for this map? engine earning nothing?

3. **DECIDE & ACT** — make ONE clear steering change (don't thrash), then stop. Map symptom →
   control-surface move:

   | What st_report shows | Act |
   |---|---|
   | `engine not earning` / no profitable route | `st_tune("min_margin","15")` to surface routes, or `st_strategy("trade-max")`; make sure probes are scouting |
   | route saturating / cr/hr falling on a route | tighten the saturation guard: `st_tune("sink_supply_cutoff","HIGH")` and/or `st_tune("sink_volume_mult","0.5")`; keep `route_diversify=1` |
   | idle capital, room to grow | let it reinvest (engine auto-buys probes→haulers), or lower `st_tune("buy_buffer","300000")` to deploy sooner |
   | ⚠STRANDED ship | `st_autopilot_stop`, `st_travel`/`st_refuel` to un-stick it, ALWAYS `st_autopilot_start` again — never leave it stopped |
   | a hull doing the wrong job | pin it: `st_assign("PROTOTRADERS-4","mine")` (or trade/scout/contract/idle) |
   | mining crashing ore price | `st_strategy("trade-max")` (mining off) or pin drones to `trade` |
   | off-track for the deadline, thin map | research with `st_docs` / `web_search`, then pick a doctrine with `st_strategy` |

   Prefer `st_strategy` for a whole doctrine in one move; `st_tune` for a single dial;
   `st_assign` to override one ship. Every change is logged to your decision trail and takes
   effect on the **next** window.

4. **LEARN** — `memory_ingest` anything durable you discovered: a profitable route, a market
   that saturates fast, a knob value that worked, a strategy that fit this map. Write it so
   **future you recalls it** — including after a reset, when waypoints change but mechanics
   don't. This is the self-improvement; don't skip it.

Then let the engine run the window. You'll be woken for the next tick.

## Make it self-driving (do this once, up front)

1. **Start the engine** (delegate to the **fleet-commander**, or `st_autopilot_start(20)`).
2. **Set the goal** so there's a terminus and a trigger: a `spacetraders:credits` goal for
   the target (e.g. 1,000,000). Its verifier ground-truths against live credits; its
   `on_achieved` hook winds the engine down. Use a **monitor** disposition so falling
   off-track nudges a re-strategize, not just a pass/fail at the end.
3. **Schedule the tick:** `schedule_task` a recurring prompt — *"Run the manage-the-fleet OODA
   tick: st_report, recall lessons, make one steering change, record what you learned."* —
   on a cadence near the window length (e.g. every 15–25 min, matching `window_minutes`).
   Tighten `st_tune("window_minutes", 8)` for faster OODA if the map is volatile.

That's the loop: the scheduler wakes you each window, you OODA, the engine executes, the goal
ends it. Hands-off and improving.

## Guardrails
- **One change per tick.** Thrashing knobs hides cause and effect. Change one thing, watch the
  next window's cr/hr, then adjust again.
- **Never leave the engine stopped.** If you stop it to un-stick a ship, start it again.
- **Spend is reserve-protected.** `reserve_floor` is the hard cash floor; raise it to keep a
  cushion. The engine only auto-buys probes/haulers, never drones (mining doesn't scale).
- **Ground-truth, don't trust the transcript.** Read `st_report` / `st_agent` for live state;
  a background window earns credits in a different context than your chat goal.

## Why this beats a static bot
Other SpaceTraders engines hard-code a planner (a VRP solver, fixed heuristics). Here the
deterministic engine stays simple and **you are the planner** — re-weighting strategy against
live telemetry each window and carrying lessons forward. That adaptivity, plus durable
self-improvement, is the edge. See `docs/two-loop-fleet.md`.
