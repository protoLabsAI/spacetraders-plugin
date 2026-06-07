# SpaceTraders — a protoAgent full-bundle plugin 🛰️

Turn any [protoAgent](https://github.com/protoLabsAI/protoAgent) into an autonomous,
**self-improving SpaceTraders fleet commander**. Play the live
[SpaceTraders v2 API](https://spacetraders.io) — a persistent, shared galactic economy —
and grow your operator's treasury from a fresh start toward **1,000,000 credits**,
hands-off: contracts seed it, trade compounds it, scouting informs it, guards protect it.

This is a **full-bundle plugin** (ADR 0027): one directory contributes the whole
extension set, all auto-discovered.

> **Just want to run it?** [**protoTrader-in-space**](https://github.com/protoLabsAI/protoTrader-in-space)
> is a ready-to-run reference agent that consumes this plugin — `git clone`, `plugin sync`,
> drop in a token, `python -m server`, and watch an autonomous fleet commander play.
> Spin-up-and-go.

| Contribution | What |
|---|---|
| **Tools** (31) | register, agent/fleet status, fuel-aware `st_travel`, markets, `st_trade_routes`, contracts, mining, buy/sell, shipyard, and the background **growth engine** (`st_autopilot_start`/`stop`/`status`) |
| **Subagents** (4) | `navigator`, `trader`, `miner`, `fleet-commander` |
| **Workflows** | `procurement-run`, `mining-run`, `fleet-bootstrap` (`workflows/`) |
| **Skills** | `play-spacetraders`, `run-a-procurement-contract`, `maximize-credits-per-hour` (`skills/`) |
| **Console view** (ADR 0026) | a **Fleet** rail dashboard — credits, ships + live ETAs, contracts, autopilot, the **galaxy leaderboard standing**, the **wipe countdown**, and the agent's **learned routes** |
| **Knowledge** | `LESSONS.md` + `seed_kb.py` (durable lessons) + **trade-route memory** (`routes.py` — the engine learns + recalls profitable routes across windows and wipes) |

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
4. Tell the agent: *"grow the treasury"* — it runs the growth engine in the
   background and supervises. Watch it on the **Fleet** dashboard.

**One-command fresh start / post-wipe recovery** — register → seed → kick the engine:

```sh
PYTHONPATH=. python plugins/spacetraders/fresh_start.py <CALLSIGN> [FACTION]
```

## How it plays — the zero-to-million growth engine

The background **growth engine** (`st_autopilot_start`, one shared rate budget) runs the
fleet by role, all guarded against loss:

- **probes SCOUT** markets (free) → build the price map trade needs;
- **one cargo ship works CONTRACTS** — the capital base (contracts are capped at one
  active per agent, so they seed, they don't scale);
- **every other cargo ship runs the best profitable TRADE route** — the scaling lever,
  each independent + spread-guarded, **re-evaluated as markets saturate**;
- profit is **reinvested into haulers** once capital is comfortable.

It **learns**: each discovered route is remembered in the knowledge store and recalled
before re-scanning, so every window — and every fresh start — is smarter than the last.
A **scheduler tick** keeps it going hands-off; the agent records findings + recalls
lessons each cycle.

> The universe **resets every few weeks** — durable lessons + learned routes survive
> (they're the agent's memory); the in-game agent/ships/token don't. After a wipe, just
> run `fresh_start.py` again — it re-registers, re-seeds, and the engine recalls what it
> learned last cycle. Nothing per-reset is hard-coded.

## Intentionally NOT min-maxed — room to make it yours

This plugin is a **demonstration of the substrate's capabilities** — an autonomous,
research-driven, self-improving agent that goes from a fresh start to a growing treasury —
**not a min-maxed, leaderboard-optimal bot.** That's deliberate. The engine plays a sound,
loss-guarded baseline and stops there, leaving the interesting decisions — and the headroom —
to **you and your strategies.**

What's left as **room to explore** (and how the pieces invite it):

- **Multiple goals, not one number.** It runs a `spacetraders:credits` target *and* a
  `spacetraders:fleet_size` target as parallel **monitor goals** — because optimizing one
  metric alone creates blind spots (a pure credits goal under-invests in ships). Add your
  own goals (reputation, a jump gate, a system to dominate) with a plugin verifier.
- **A `strategist` subagent that *researches and decides*.** It audits, reads the meta
  (`web_search` + the knowledge store), and acts within bounded authority (self-heal, tune,
  steady fleet growth). **Widen its mandate, sharpen its strategy, or replace it** — it's
  where your edge goes.
- **Tunable engine knobs** (`st_tune`) and a deliberately **conservative** posture
  (declines slow far-hauls, modest reserves, simple route math). Crank the aggression,
  expand into the outer system, specialize ships — the levers are exposed on purpose.
- **It learns + remembers** (knowledge store), so whatever strategy you layer on compounds
  across windows and survives the wipe.

In short: it gets the fleet *going and growing* on its own, and gets out of your way so the
**optimization, specialization, and clever plays are yours to add.**
