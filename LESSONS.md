# SpaceTraders — durable lessons (knowledge seed)

These are the **durable game mechanics** protoTrader-in-space should always know —
they survive a universe wipe. They're loaded into the agent's knowledge base so it
**recalls** them (`memory_recall` / KnowledgeMiddleware) instead of relearning the
hard way. Per-reset state (waypoints, prices, contract/agent ids) is **not** here —
the agent re-scans that live each session.

**Canonical source + seeder:** `plugins/spacetraders/seed_kb.py` (lessons inline).
Re-seed after a wipe (idempotent):

```sh
PYTHONPATH=. python plugins/spacetraders/seed_kb.py        # → ~/.protoagent/knowledge/agent.db
```

## The lessons (headings: `spacetraders:<topic>`, domain `fact`)

- **supply-chain-trading** — sustained trade is the SUPPLY CHAIN, not random arbitrage.
  Buy where a market EXPORTS a good (type EXPORT, supply HIGH/ABUNDANT → cheap, refills
  each cycle) and sell where another IMPORTS it (type IMPORT, supply SCARCE → dear).
  Random buy-low/sell-high saturates fast — every trade moves the price (capped by
  tradeVolume), so a 50% spread dies in two trades while a 10% export→import route
  refills forever. Rank by margin × tradeVolume (throughput), not raw spread. `best_route`
  does this; trust export→import over a bigger one-off spread.
- **contracts** — only ONE active contract per agent at a time (error 4103/4511 on
  a second). Procurement contracts are the biggest early earners (advance + large
  fulfillment) and build reputation. One ship on the contract; the rest haul/trade/mine.
- **fuel-travel** — CRUISE fuel ≈ distance; DRIFT (~1 fuel) escapes a stranded ship,
  slowly. Use `st_travel` (auto refuel/CRUISE/DRIFT-via-fuel-station). Probes fly free.
- **rate-limit** — the rate limit is per-ACCOUNT (~2 req/s), shared by all ships. Run
  the fleet from ONE engine; parallel scripts sum past the limit (429s).
- **market-saturation** — selling a lot of one good crashes its price (≈20 mining
  drones bottomed out ore prices → losing money). Diversify; don't over-mine. Simple
  beats greedy.
- **multiplayer** — shared universe, but no direct player trade/gift/combat.
  Collaboration = the shared market economy + jump-gate construction. `st_transfer`
  is between your OWN ships only.
- **wipe-cycle** — the universe resets every few weeks; it wipes agent/token/ships/
  credits/contracts. Re-register, re-seed this, re-scan after a wipe.
- **ship-roles** — frigate (mine+haul), probe (free scout), shuttle/hauler (cargo),
  mining/siphon drones. Early loop: command ship + hauler + light mining; the
  hauler's round trip is the bottleneck.
- **self-improvement** — record findings (`memory_ingest`), recall before planning
  (`memory_recall`), track the plan in beads, pursue the operator's goal, and note
  capability gaps so they can become new tools/skills/workflows.
- **autopilot-supervision** — the background engine (`st_autopilot_start`) sometimes
  strands a ship (DOCKED full, or stuck mid-route). Each tick, read
  `st_autopilot_status`; restart the engine or nudge the ship if stuck. Engine
  start/stop is a lead/commander job — specialists only READ status and report up.
- **goals-are-per-session** — a `/goal` is evaluated only after a terminal turn in
  the session it was set in; credits earned by the background engine or a scheduler
  tick (a different context) won't auto-close it. Drive a turn in that session to
  close it, and ground-truth the verifier against live state, not the transcript.
- **zero-to-million** — fresh-start → ~1M: contracts are the capital base (capped at
  ONE active/agent, don't scale); **trade arbitrage is the scaling lever** (each hauler
  an independent buy-cheap/sell-high route — whales run 27–39 ships); scout markets with
  probes (prices need a ship present); reinvest into LIGHT_HAULERs once capital > ~290k +
  a profitable route exists; guard every buy (skip sell≤buy). Contracts seed, trade
  compounds, scouting informs, guards protect.
- **check-live-prices** — `st_trade_routes` shows structural arbitrage; saturated
  markets can have sell BELOW buy (seen live: H51 ALUMINUM 156/76 — a guaranteed
  loss). Confirm live prices (`st_market`) before a trade leg; contracts carry less
  price risk and paid the fixed bonuses that grew the treasury past 500k.

## Onboarding note

A new operator sets their SpaceTraders **token in the console**: System → Settings →
**SpaceTraders** (the plugin contributes those fields via ADR 0019). The agent token
is all that's needed to play; the account token is only for registering a new agent.
