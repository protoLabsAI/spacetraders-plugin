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

## Onboarding note

A new operator sets their SpaceTraders **token in the console**: System → Settings →
**SpaceTraders** (the plugin contributes those fields via ADR 0019). The agent token
is all that's needed to play; the account token is only for registering a new agent.
