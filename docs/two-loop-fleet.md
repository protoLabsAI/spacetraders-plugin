# Two-loop fleet — deterministic engine + agentic OODA strategist

**Status:** implemented · **Date:** 2026-06-14 · **Scope:** the SpaceTraders plugin

This is the architecture the plugin uses to play SpaceTraders autonomously: a fast
deterministic **engine** that does the grind, steered by a slow agentic **strategist** that
reasons between windows. It's the design that makes protoTrader-in-space a *self-improving*
agent rather than a static bot.

## Context

We surveyed the leading open-source SpaceTraders engines (whyando's Rust engine with a
`vrp-core` Vehicle-Routing-Problem solver; Stafford Williams' TS engine that hit ~12M/reset;
Moosbee's manager-actor model). They all win the same way: a **smarter static heuristic** —
hard-coded planners that assign ships to value-weighted tasks. They are deterministic top to
bottom.

We have something they don't: an agentic LLM substrate (protoAgent) with a scheduler, a goal
system, durable knowledge/memory, and subagents. Competing on "better static heuristic" is
not our edge. Putting an LLM *in the strategy loop* — observing the engine's own performance
and re-strategizing — is. The constraint that shapes everything is the **weekly reset**:
~7 days to go 0 → 1M credits, so the system must bootstrap fast, run unattended, and survive
resets by carrying lessons forward.

## Decision: two control loops at different timescales

```
┌─ OUTER LOOP — agentic, per-window (the BRAIN / OODA) ──────────────┐
│  the strategist subagent, woken by the SCHEDULER between windows:   │
│   Observe → st_report (cr/hr trajectory, per-ship health, knobs,    │
│             strategy, decision trail, deterministic HINTS)          │
│   Orient  → memory_recall lessons + compare trajectory vs the goal  │
│   Decide  → ONE steering change                                     │
│   Act     → st_strategy · st_tune · st_assign · reinvest · un-strand│
│   Learn   → memory_ingest the finding (survives the next reset)     │
└────────────────────────────────────────────────────────────────────┘
        ▲ telemetry                              │ strategy / knobs / pins
        │                                         ▼
┌─ INNER LOOP — deterministic, sub-second (the MUSCLE) ──────────────┐
│  autopilot() windows back-to-back · ONE rate limiter · proven jobs  │
│  (contract / trade / mine / scout) · watchdog (re-kick, stall,      │
│  reset-recovery) · saturation damping · fuel-safety                 │
└────────────────────────────────────────────────────────────────────┘
```

- **Inner loop (the muscle).** `fleet.autopilot()` runs a time window: it reinvests, assigns
  each ship a role (`roles.assign_roles`), and runs every ship's deterministic job coroutine
  concurrently through the single rate-limited client (`client.call`, ~2 req/s for the whole
  fleet — the per-account budget other engines also design around). `_run_ops` loops windows
  back-to-back; the watchdog re-kicks crashes, breaks stalls, and auto-recovers from a
  universe reset (4113 → re-register). No LLM in this loop — it's too rate-limited to click
  every leg, exactly as the surveyed engines found.

- **Outer loop (the brain).** Between windows the strategist runs **OODA** (the
  `manage-the-fleet` skill). It reads `st_report`, recalls lessons, makes one steering change
  through the control surface, records what it learned, and lets the engine run the next
  window. It is the strategist *and* the exception handler; the deterministic watchdog is the
  heartbeat.

**The key trade-off / insight:** the value-weighted task pool the other engines hard-code
with a VRP solver, we get from the LLM. The engine stays simple (one job per role); the agent
is the *soft planner*, re-weighting doctrine against live telemetry each window and — uniquely
— learning across windows **and across resets**. We deliberately did **not** build a VRP
solver (see Alternatives).

## The control surface (the contract between the loops)

Three tiers, coarse → fine, plus the gauges. All take effect on the **next** window (the
engine reads the module-global knobs at call time, so changes apply to the running engine).

| Tier | Tool | What it does |
|---|---|---|
| Observe | `st_report` | Rich telemetry: credits + cr/hr trajectory to 1M, per-ship role/health (flags stranded), live knobs+strategy+pins, the decision trail, deterministic hints. |
| Macro | `st_strategy` | Switch doctrine in one move: `balanced` / `trade-max` / `mining` / `contract-grind` (a curated knob bundle + mining toggle — `strategy.py`). |
| Micro | `st_tune` | One knob: `min_margin`, `buy_buffer`, `max_ships`, `probe_buffer`, `map_target`, `max_probes`, `reserve_floor`, `window_minutes`, `sink_volume_mult`, `sink_supply_cutoff`, `route_diversify`. |
| Per-ship | `st_assign` | Pin a ship to `mine`/`trade`/`contract`/`scout`/`idle` (or `auto` to clear), overriding the auto-classifier (`roles.assign_roles`). |

Every macro/micro/per-ship change is appended to a **decision log** (`fleet.decisions()`),
surfaced in `st_report` and the dashboard — the audit trail of the self-improving brain.

## Engine hardening (best-of-the-engines, ported)

The deterministic layer absorbs the proven patterns so the agent isn't firefighting basics:

- **Single global rate gate** — the whole fleet self-paces through one client (we already had
  this; the surveyed engines converge on it).
- **Market-saturation damping** — the #1 way an unattended bot crashes its own routes. A trade
  is sized to ≈ one sink `tradeVolume` per visit (`sink_volume_mult`), importers already
  glutted at/above a supply tier are skipped (`sink_supply_cutoff`), and haulers diversify
  across the top-N routes (`route_diversify`) instead of stacking on one (`analysis.rank_routes`).
- **Reserve-protected spend** — `reserve_floor` is a hard cash floor on auto-buys; the engine
  only ever buys probes (data) then light haulers (throughput), never mining drones (mining is
  cooldown-bound and self-depressing — a zero-capital opener, not a scaling lever).
- **Fuel-safety + stall recovery** — auto-refuel, DRIFT escape for a near-empty ship, and a
  watchdog that breaks frozen-log stalls and recovers from resets.

## OODA wiring (making it self-driving)

1. **Start the engine** once (`st_autopilot_start`, or delegate to fleet-commander).
2. **Set the goal** — a `spacetraders:credits` goal for the target; its verifier ground-truths
   live credits and its `on_achieved` hook winds the engine down (`fleet.request_stop`). A
   *monitor* disposition lets falling off-track trigger a re-strategize.
3. **Schedule the tick** — a recurring scheduler prompt that runs one OODA tick, cadence near
   `window_minutes`. The scheduler is the OODA clock; the goal is the terminus.

## Consequences

- **Pro:** adaptivity without a bespoke solver; the agent explains its reasoning; lessons
  compound across resets (durable self-improvement); the engine stays small and testable; the
  whole control surface is host-free unit-tested (`roles`/`strategy`/`analysis`).
- **Con / watch:** the outer loop costs LLM tokens per tick (bounded by the window cadence);
  a bad steering decision persists for one window before the next OODA corrects it (mitigated
  by one-change-per-tick + the engine's own guards). The LLM is *not* in the hot path, so it
  can never blow the rate budget.

## Alternatives considered

- **Hard-coded VRP solver (whyando model).** Most powerful static optimizer, but it's the
  thing we'd be *adding to compete on their axis*, and it can't learn or explain. We let the
  LLM be the soft planner instead. A VRP layer could later sit *inside* the engine as one more
  deterministic job-assigner the strategist tunes — not ruled out, just not first.
- **Pure-agentic (LLM clicks every leg).** Impossible under the ~2 req/s budget and absurdly
  token-expensive. Rejected — hence the deterministic inner loop.

## Map of the code

`fleet.py` engine + control surface (knobs, `apply_strategy`, `set_override`, `report`,
`decisions`) · `roles.py` capability-based role classification + per-ship overrides ·
`strategy.py` presets · `analysis.py` route ranking + saturation guard · `tools.py`
`st_report`/`st_strategy`/`st_assign`/`st_tune` · `skills/manage-the-fleet` the OODA loop ·
`dashboard.py` the strategist decision-log showcase.
