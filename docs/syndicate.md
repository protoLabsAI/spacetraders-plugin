# The Syndicate — a multi-agent SpaceTraders fleet (v2.1 runbook)

Several protoAgent instances, each playing its own SpaceTraders agent under its own
doctrine, federated by a commander that consumes them **structurally** over A2A. No new
code in this plugin makes this work — that's the point. It composes host seams that
already exist: per-instance scoping (ADR 0004/0065), the delegate registry
(`delegate_to`, ADR 0025), and this plugin's typed A2A card skills (v2.1).

## Roster

| Agent | Instance | Doctrine (`st_strategy`) | Role |
|---|---|---|---|
| `TRADER-1` | `:7871` | `trade-max` | pure arbitrage — the earner |
| `MINER-1` | `:7872` | `mining` | extraction-heavy — steady income floor |
| `SCOUT-1` | `:7873` | `balanced` + standing `st_explore` campaigns | charts the frontier, feeds intel |
| commander | `:7870` | (no game token needed) | portfolio brain — delegates, compares, steers |

## Setup (each fleet member)

1. Boot an isolated instance (own box root + instance + port — the standing rule):
   `PROTOAGENT_BOX_ROOT=~/syndicate/trader1 PROTOAGENT_INSTANCE=trader1 python -m server --port 7871`
2. Install + enable this plugin; paste a **distinct call sign's** agent token in
   Settings ▸ SpaceTraders (one game agent per instance — tokens are per-agent).
3. Set the doctrine: `st_strategy trade-max` (etc.), then "grow the treasury".
4. If members are on a tailnet, bind `--host` and set `A2A_AUTH_TOKEN` (ADR 0042 note).

## Setup (the commander)

Declare the members as delegates (Settings ▸ Workspace ▸ Delegates, or `delegates:`
config) — type `a2a`, URL `http://<host>:<port>`. The members' agent cards now
advertise `fleet_report` and `quote_route` (this plugin registers them), with
`output_schema` + `result_mime` enforced by the executor's structured finalizer —
so the commander's `delegate_to("TRADER-1", "Send me your fleet report.")` returns
**parseable JSON**, not prose to scrape.

## The command loop

A daily scheduled turn (or a goal-driven loop) on the commander:

1. `delegate_to` each member: `fleet_report` → compare `per_hour`, `net_worth`,
   `engine_running` across doctrines. A stopped engine or a flatlined member is the
   finding — each member's own tripwires (v1.8) should have caught it first; the
   commander is the second net.
2. `quote_route` across members: if `SCOUT-1`'s system quotes a fatter margin than
   `TRADER-1`'s, tell `TRADER-1` — cross-pollination the solo fleet can't do.
3. Steer doctrine, not positions (the engine-rewrite lesson holds at fleet scale):
   ask a member to `st_strategy` / `st_tune`, never to move a specific ship.
4. The shared long-horizon objective: pool construction supply on ONE jump gate
   (`st_construction` for the shopping list → each member `st_supply_construction`
   what its routes already carry). The gate is a literal community goal — the
   Syndicate finishes it faster than any solo agent.

## Known limits (filed upstream)

- Members' knowledge stores are per-instance; route/lesson intel crosses only via
  A2A answers (shared plugin *knowledge* is an open host gap — bundles share skills
  only).
- The commander can't subscribe to a member's `spacetraders.*` bus events remotely —
  the bus is in-process (SSE is console-facing); polling `fleet_report` is the loop.
- One rate budget per game agent (that's per-member, so the Syndicate scales the
  API budget linearly — the actual reason multi-agent beats one bigger fleet).
