---
name: manage-the-fleet
description: >-
  The OODA strategist loop for the SpaceTraders fleet — the agentic BRAIN that steers the
  deterministic autopilot engine. The engine now self-stabilizes (capital-disciplined buys,
  confirmed-fresh routing, and a position-sticky plan that holds routes + stations probes), so
  the strategist steers POLICY SLOWLY — daily or on a real regime change — not every window.
  Each tick: Observe (st_report) → Orient (recall lessons, compare vs the goal) → Decide → Act
  (st_strategy / st_tune — one bounded change) → Learn (memory_ingest). Triggers on "manage the
  fleet", "run the fleet to a million", "strategize the fleet", "keep steering the fleet".
---

# Manage the fleet — the OODA strategist loop 🧠🪐

This is the **two-loop fleet**. Two control loops at very different speeds, and *you are the
slow one* — slower now than before, on purpose.

- **Inner loop (the muscle):** the deterministic `autopilot` engine runs windows back-to-back
  under one rate limiter — contracts, trade, mining, scouting — with a watchdog that re-kicks
  crashes, breaks stalls, and recovers universe resets. It is now **self-stabilizing**:
  - **capital-disciplined** — a single buy can never drain the treasury (`max_spend_frac`);
  - **never buys blind** — it only trades a route with a confirmed, *fresh* sell price, so it
    can't get stuck hauling into a dead-end / since-moved market;
  - **position-sticky** — with `stable_plan` on, a persistent plan holds each hauler on its
    route across windows and stations probes at the active route's endpoints (so both ends stay
    live-priced). Positions no longer evaporate every window.
- **Outer loop (the brain — YOU):** you steer **policy**. The engine holds its own positions
  now, so your job is *not* to re-place ships every window — it's to pick the doctrine and the
  guard settings, occasionally, when the data says the regime changed. Steer slowly.

The point is a **self-improving agent**: it watches its own performance, re-strategizes when it
matters, and carries lessons across windows **and universe resets** (durable knowledge).

## Turn it on (once, up front)

1. **Start the engine** — delegate to the **fleet-commander**, or `st_autopilot_start(20)`.
2. **Enable the stable plan** — `st_tune("stable_plan","1")`. This is what holds positions across
   windows (the fix for the old "every fix erased next window" churn). Watch a couple of windows
   on the **Fleet** dashboard: a hauler should *stay* on its route and credits should compound.
3. **Set the goal** — a `spacetraders:credits` target (e.g. 1,000,000), **monitor** disposition,
   so falling off-track nudges a re-strategize. Its verifier ground-truths live credits; its
   `on_achieved` hook winds the engine down.
4. **Schedule the tick — DAILY, not every window.** `schedule_task` the OODA prompt on a cadence
   near `strategist_cadence_min` (default **1440 min / daily**). The engine runs unattended in
   between; you only need to look in when a window's worth of data has accumulated or a goal
   nudge fires. Tighten the cadence only for a genuinely volatile map.

## One OODA tick

Run when woken (the daily tick, a goal nudge, or by hand). Keep it tight — observe, **at most
one** policy change, learn, stop.

1. **OBSERVE** — `st_report`: credits + **cr/hr trajectory toward 1M**, each ship's
   role/status/health (⚠STRANDED flag), the live knobs + strategy, your recent decisions, and
   deterministic **HINTS**. Read the hints first.

2. **ORIENT** —
   - `memory_recall` the SpaceTraders lessons (saturation, supply-chain, zero-to-million) and any
     route/strategy findings you saved before — including before a reset.
   - Compare the **trajectory** to the goal: at this cr/hr, do you reach 1M before the weekly
     reset? Is cr/hr trending up or down across the *last few* windows (not one)?
   - Diagnose the *regime*: is the whole map saturating? is the doctrine wrong for this map? is
     the engine earning nothing at all? Look for a sustained signal, not one noisy window.

3. **DECIDE & ACT** — make **at most one** policy change, then stop. Policy = doctrine + guards,
   *not* ship positions (the plan owns those). Map a *sustained* symptom → a control move:

   | What st_report shows (across windows) | Act (policy) |
   |---|---|
   | engine earning nothing / no profitable route mapped | `st_tune("min_margin","15")` to surface thinner routes, or `st_strategy("trade-max")`; confirm probes are scouting/stationed |
   | whole map saturating, cr/hr trending down | tighten the sink guard: `st_tune("sink_supply_cutoff","HIGH")` and/or `st_tune("sink_volume_mult","0.5")` |
   | haulers thrash between routes (stable_plan off) | `st_tune("stable_plan","1")` — let the plan hold positions |
   | routes go stale before haulers arrive (volatile map) | lower `st_tune("route_max_age","600")` so dispatch uses only fresh prices |
   | crashes from over-buying a pricey good | lower `st_tune("max_spend_frac","0.3")` — commit less cash per trade |
   | off-track for the deadline, thin map | research with `st_docs` / `web_search`, then pick a doctrine with `st_strategy` |
   | a genuinely mis-cast hull (rare) | `st_assign(...)` — but this is a **manual exception**, not a per-tick reflex (see Guardrails) |

   Prefer `st_strategy` (a whole doctrine) over `st_tune` (one dial). Every change is logged and
   takes effect on the **next** window.

4. **LEARN** — `memory_ingest` anything durable: a profitable route, a fast-saturating market, a
   knob value that worked, a doctrine that fit this map. Write it so **future you recalls it**,
   including after a reset. This is the self-improvement; don't skip it.

## Guardrails
- **Steer policy, not positions.** The plan holds routes and stations probes. **Do NOT re-pin
  ships every tick** — that hourly re-placement is exactly what lost the old 8-hour siege (every
  pin was erased by the next window). `st_assign` is a *rare manual override* for a real misclass,
  not a routine move.
- **One bounded change per tick, then wait a window.** The engine **rejects an oscillating tune**
  (one that reverts your last change) — so don't flip-flop; give a change a window to show its
  effect before adjusting.
- **Act on a trend, not a window.** One noisy window isn't a regime change. Look across several.
- **Never leave the engine stopped.** If you stop it to un-stick a ship, start it again.
- **Spend is guarded for you.** `max_spend_frac` caps per-trade spend; `reserve_floor` is the
  ship-reinvest floor. You rarely need to touch these — the engine won't bankrupt itself.
- **Ground-truth, don't trust the transcript.** Read `st_report` / `st_agent` for live state; a
  background window earns in a different context than your chat goal.

## Why this beats a static bot
Other engines hard-code a planner (a VRP solver, fixed heuristics). Here the deterministic engine
stays simple and **self-stabilizing**, and you re-weight *doctrine* against live telemetry — slowly,
and carrying lessons forward across resets. Adaptivity + durable self-improvement is the edge,
without the hourly micro-management that used to fight the engine. See `docs/engine-rewrite.md`
and `docs/two-loop-fleet.md`.
