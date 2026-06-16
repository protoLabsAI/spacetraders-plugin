# Engine rewrite — a stable, capital-disciplined controller

**Status:** proposed (design under review) · **supersedes the engine half of** [`two-loop-fleet.md`](two-loop-fleet.md)

> The two-loop framing — a deterministic ENGINE (muscle) steered by an agentic OODA
> STRATEGIST (brain) — stays. What changes is the **engine**: from a stateless,
> re-plan-every-window dispatcher with no cash discipline into a **stable controller**
> that holds a persistent plan, never spends below a reserve, and keeps its route
> endpoints live. The strategist is demoted from an hourly micro-manager to a slow,
> bounded, ground-truthed policy tuner.

## Why — the diagnosis

A live run sat in a **36–43k credit band for 8+ hours** after crashing from a 172k peak,
while the hourly strategist applied positional fix after positional fix that the next
engine window erased. The strategist's own logs blamed "position sabotage," a "`buy_buffer`
deadlock," and "hauler misclassification." Reading the code, **two of those three are
wrong**, and the real causes are structural.

### What the strategist got wrong (unreliable narrator)

- **"`buy_buffer` is the root cause / 50k deadlock paralyzed goods-buying."** False.
  `buy_buffer` is read in exactly two spots (`fleet.py:691,705,1006`) and gates **whether
  to buy a new LIGHT_HAULER ship** — nothing else. The goods-buy path (`_buy` `fleet.py:133`,
  `job_trade` `:315`) never reads `buy_buffer`, `credits`, or any cash floor. Tuning it
  300k→50k→30k changed nothing about the behavior it was blamed for. There is no
  "needs ≥buffer credits to buy goods" mechanism; the 36k flatline was not a buy_buffer
  deadlock.
- **"P3-1 keeps getting relabeled a miner."** Almost certainly confabulated. The run used
  the `trade-max` preset (`mining: false`), under which `assign_roles` produces **zero**
  auto-miners (`roles.py:108-109`); a laser-less hull is never classified a miner under any
  setting (`roles.py:27,107`). The classifier the strategist kept "fixing" was not
  misclassifying anything — it was pattern-matching the old v1.3.0 bug that is already fixed.

The lesson: an LLM steering layer that reasons about internal mechanics it can't see will
invent plausible-but-wrong causes and act on them. The engine must be **sound on its own**,
and the strategist must steer **policy from ground truth**, not micro-manage mechanics.

### The real root causes

1. **No working-capital discipline (the crash class).** `_buy` (`fleet.py:133-164`) buys
   until the hold is full or the API throws `[4600]` insufficient-funds. The only guard is
   `max_price ≤ sell_price` (don't pay above resale). Nothing checks that the **total outlay
   leaves a cash reserve.** `reserve_floor` exists but is applied **only** to ship reinvestment
   (`:687-691`), never to goods. So one high-value "profitable-looking" route (≈80k of
   ASSAULT_RIFLES against a 109k treasury) drains the treasury into cargo; if the sell leg
   doesn't fully realize (saturated sink, moved price, unreachable), the cash is trapped → crash.
   **`buy_buffer` was never the lever — a reserve floor on goods is.**

2. **Stateless re-planning churns positions (the "8-hour flatline").** Every window,
   `autopilot()` (`fleet.py:769`) re-fetches the fleet, re-classifies, and re-assigns jobs
   **from scratch.** It has no memory that "P3-1 is mid-route" or "P3-4 is the live price feed
   at E42." Probes round-robin across *all* markets each window (`:809-812`), abandoning
   route-critical hubs. That is the "position sabotage" — and **no amount of hourly re-pinning
   survives the next window's from-scratch re-plan.**

3. **Reasoning over a stale / cross-wipe price map.** Routes rank over the *recorded* map;
   when it's dry the engine falls back to `recall_routes` — possibly routes learned in a
   **previous universe** (`:579-585`). No probe is dedicated to holding the active route's two
   endpoints live, so "both ends priced" happens only by accident, briefly. A hauler then
   buys at a stale price or into a saturated/missing sink (the "D40 attractor").

4. **The hourly strategist amplifies the instability.** Running every hour on wrong theories,
   it churns knobs and re-pins ships that the next 20-minute window un-pins — high-frequency
   meddling stacked on an unstable base.

## Design principles

1. **Working-capital discipline.** Enforce `reserve_floor` on *every* goods purchase. Never
   spend the treasury into cargo. This alone makes the crash class impossible.
2. **Persistent fleet plan.** Assign ship→route/station and *keep it* until it's exhausted or
   clearly unprofitable. Reconcile incrementally each window; never re-plan from zero.
3. **Hold the route endpoints live.** Dedicate probes to *stay* at the active route's buy/sell
   waypoints; rotate only surplus probes to explore. Prefer fresh, live-lit prices.
4. **No dead-end routing.** Never send a hauler to a buy market without a confirmed,
   fresh-priced, profitable sink. Recalled/cross-wipe routes only *seed exploration*, never
   dispatch a blind buy.
5. **Demote the strategist.** The engine is sound unattended. The LLM steers slowly (daily /
   on a real regime change), with bounded + validated knob deltas, from ground truth — it does
   not micro-pin ships every hour.
6. **One coherent cash policy.** Fold reinvestment into the reserve model; retire/alias the
   confusingly-named `buy_buffer`.

## Detailed design

### 1 · Working-capital discipline  *(PR 1 — highest leverage, smallest change)*

Add a pure sizing helper and thread it through every buy:

```python
def affordable_units(credits, reserve_floor, unit_price, room, vol_cap):
    """Max units to buy keeping ≥ reserve_floor in cash."""
    if unit_price <= 0:
        return 0
    spendable = max(0, credits - reserve_floor)
    return min(room, vol_cap, spendable // unit_price)
```

- `_buy` gains a reserve-aware ceiling: it fetches `credits` once, computes
  `affordable_units(...)`, and never exceeds it. If it's 0, **skip the buy and log
  "below reserve floor — holding cash"** (no `[4600]` storm, no trapped capital).
- `job_trade` sizes `target` by the same helper (alongside the existing `tradeVolume`
  saturation cap), so a buy is bounded by *the smaller of* cargo room, one tier-step of
  throughput, and affordable cash.
- `reserve_floor` default rises from `0` to a meaningful cushion (proposed: `25_000`,
  tunable). The pure helper unit-tests host-free.

**Effect:** the ASSAULT_RIFLES-class crash cannot recur regardless of any knob.

### 2 · Price-map freshness + no dead-end routing  *(PR 2)*

- `prices.record_market` stamps each market entry with a logical timestamp. `price_map`
  exposes age.
- `analysis.rank_routes` gains a `max_age` filter: a leg whose price is older than the
  freshness window is **not** treated as live for dispatch (it can still seed exploration).
- A hauler is dispatched to buy at `W` only when there is a **confirmed sink**: an importer of
  the good with a *fresh* price and positive margin after the saturation guard. No confirmed
  sink → no dispatch there. This kills the D40 attractor.
- `recall_routes` (cross-window/cross-wipe memory) is demoted to an **exploration seed** —
  it sends a *probe* to re-light a remembered market, never a *hauler* to buy blind.

### 3 · Persistent fleet plan + probe stationing  *(PR 3 — the structural fix)*

Introduce **`plan.py`** — the persistent fleet assignment, JSON-backed like `knobs.json`:

```
Plan = { ship_symbol: Assignment }
Assignment = { role, route?: {good, buy_at, sell_at}, station?: waypoint, since, strikes }
```

`autopilot()` is reframed **reconcile-then-dispatch**:

1. **Reconcile** (pure, deterministic, host-free testable): load the saved plan, validate each
   assignment against the live fleet + market state, and change **only** what's invalid:
   - ship gone → drop it; new ship → assign by capability + current needs;
   - a hauler's route unprofitable for `N` consecutive attempts (`strikes`) → reassign;
     otherwise **keep it** (hysteresis stops the churn);
   - ensure the active primary route's two endpoints each have a **stationed probe**; assign
     surplus probes to exploration.
2. **Dispatch** jobs per the (mostly unchanged) plan.

New **station job** for probes: travel to the endpoint, dock, re-poll its market every `M`
seconds to keep the price fresh, hold until reassigned. Surplus probes keep the existing
round-robin `job_scout`, but only over markets **not** currently stationed.

This is the fix that makes positions *stick* — the engine stops dismantling its own setups,
so a route can fire across multiple windows and compound.

### 4 · Strategist demotion  *(PR 4)*

- Cadence: `manage-the-fleet` moves from hourly to **daily** (knob: `strategist_cadence_min`,
  default `1440`), or fires on a real **regime-change trigger** (e.g. cr/hr crosses zero for
  K consecutive windows, or a reset).
- Bounded authority: knob deltas are **clamped** to sane ranges and **no-op/oscillating**
  changes are rejected (the engine refuses a tune that reverts the last one within a window).
- Remove per-ship micro-pinning from the recurring loop. Pins (`st_assign`) remain a **rare
  manual operator tool**, not the strategist's hourly reflex — the persistent plan is now the
  positional authority, so the strategist sets *policy*, not positions.

### 5 · One coherent cash policy  *(PR 5)*

- Reinvestment thresholds derive from `reserve_floor` (buy a hauler when
  `credits − ship_cost ≥ reserve_floor + headroom`), so there is **one** cash story.
- `buy_buffer` is retired as a separate knob (aliased to the derived threshold for back-compat,
  with a migration note); `st_tune buy_buffer` maps onto the unified model or warns.

## Module impact

| Module | Change |
|---|---|
| **`plan.py`** *(new)* | persistent fleet plan + pure reconcile logic (host-free testable) |
| `fleet.py` | `autopilot()` → reconcile-then-dispatch; reserve-aware `_buy`; probe station job; freshness/no-dead-end in `_ranked_routes` |
| `analysis.py` | freshness filter + confirmed-sink requirement in `rank_routes` |
| `prices.py` | per-market freshness timestamps + age in `price_map` |
| `knobs.py` | meaningful `reserve_floor` default; `strategist_cadence_min`; retire/alias `buy_buffer` |
| `roles.py` | minimal — the classifier is **not** the bug |
| `skills/manage-the-fleet` + scheduler | daily/regime-change cadence; bounded, no micro-pinning |

## Staged delivery (the schedule)

Each stage is its own PR + version bump + tag, matching the repo's cadence. Ordered by
leverage-per-risk so the crash class dies first and the structural fix lands after the cheap
safety wins.

| PR | Ver | Scope | Risk |
|---|---|---|---|
| 1 | v1.4.0 | Working-capital discipline (reserve-aware buys) | low — localized to `_buy`/`job_trade`, pure helper + test |
| 2 | v1.4.1 | Price-map freshness + no dead-end routing | medium |
| 3 | v1.5.0 | Persistent plan + probe stationing (reconcile-not-replan) | high — the core rewrite; behind a `stable_plan` knob to A/B |
| 4 | v1.5.1 | Strategist demotion (daily cadence, bounded, no micro-pins) | low |
| 5 | v1.5.2 | Unified cash policy (retire `buy_buffer`) | low |

PR 3 ships behind a `stable_plan` knob (default off → on after a clean live window) so the new
controller can be A/B'd against the current dispatcher on a live fleet before it becomes the
default.

## Testing

- **Host-free** (the bar set by `roles`/`analysis`/`client`/`check_credits`): `affordable_units`
  sizing; `plan.py` reconcile over synthetic state dicts (keep/reassign/strike/station cases);
  `rank_routes` freshness + confirmed-sink filtering.
- **Engine smoke** (`importorskip graph.plugins.testkit`, per the v1.3.0 retrofit): dispatch
  wiring still compiles and assigns sane jobs.
- **Live validation gate** per stage: PR 1 → no `[4600]` storms + credits never dip below the
  floor; PR 3 → a route holds across ≥2 windows and credits compound (the metric the 8-hour
  flatline failed).

## Open questions

- **Reserve floor default** — fixed (e.g. 25k) or scaled to fleet size / early-game phase?
- **Strategist regime-change trigger** — pure cadence (daily) is simplest; a cr/hr-crosses-zero
  trigger is more responsive but adds state. Start with cadence, add the trigger if needed.
- **`buy_buffer` back-compat** — silent alias vs. a deprecation warning on `st_tune buy_buffer`.
- **Plan persistence across a wipe** — the plan references per-reset waypoints/ship ids, so it
  must **clear on a detected reset** (4113 recovery) the way the agent token does, while the
  learned-route memory (`routes.py`) survives.
