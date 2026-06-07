# SpaceTraders — a protoAgent full-bundle plugin 🛰️

Turn any [protoAgent](https://github.com/protoLabsAI/protoAgent) into an autonomous
**SpaceTraders** fleet commander. Play the live [SpaceTraders v2 API](https://spacetraders.io)
— a persistent, shared galactic economy — to grow your operator's treasury through
contracts, trade, and mining.

This is a **full-bundle plugin** (ADR 0027): one directory contributes the whole
extension set, all auto-discovered.

| Contribution | What |
|---|---|
| **Tools** (30) | register, agent/fleet status, fuel-aware `st_travel`, markets, contracts, mining, buy/sell, shipyard, and the background autopilot (`st_fleet_start`/`stop`/`status`) |
| **Subagents** (4) | `navigator`, `trader`, `miner`, `fleet-commander` |
| **Workflows** | `procurement-run`, `mining-run`, `fleet-bootstrap` (`workflows/`) |
| **Skills** | `play-spacetraders`, `run-a-procurement-contract`, `maximize-credits-per-hour` (`skills/`) |
| **Console view** (ADR 0026) | a **Fleet** rail dashboard — live credits, ships, contracts, autopilot |
| **Knowledge** | `LESSONS.md` + `seed_kb.py` — durable game-mechanic lessons the agent recalls |

## Install

**From a git URL** (ADR 0027) — review the manifest, then enable:

```sh
python -m server plugin install https://github.com/protoLabsAI/spacetraders-plugin
# review the printed manifest + capabilities, then enable it:
#   plugins: { enabled: [spacetraders] }   in your config
```

Or drop this directory into your protoAgent's `plugins/`. No core edits.
Needs **protoAgent ≥ 0.20.0** (ADR 0026 views + ADR 0027 bundle discovery). Pure
Python over `httpx` (a core dep) — no extra `pip install`.

## Set up

1. Get a SpaceTraders **account token** at <https://spacetraders.io>.
2. In the console: **System → Settings → SpaceTraders** — paste the **agent token**
   (or the account token + call sign to register a new agent).
3. *(Optional)* seed the durable lessons so the agent recalls them:
   `PYTHONPATH=. python plugins/spacetraders/seed_kb.py`
4. Tell the agent: *"grow the treasury"* — it sets a goal, runs the fleet engine in
   the background, and supervises. Watch it on the **Fleet** dashboard.

## How it plays (the loop)

Operator sets a **goal** → the agent plans (beads) → launches the **background
engine** (one shared rate budget; cargo ship works contracts, probes scout) →
**records findings + recalls lessons** → a **scheduler tick** keeps it going. Simple
and diversified by default (contracts + scouts — no min-maxing; over-mining crashes
prices). Expand into multi-ship trade/mining when ready.

> The universe **resets every few weeks** — re-register, re-seed `LESSONS.md`, and
> re-scan live state after a wipe. Nothing per-reset is hard-coded.
