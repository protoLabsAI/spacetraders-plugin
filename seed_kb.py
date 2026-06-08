#!/usr/bin/env python
"""Seed protoTrader-in-space's knowledge base with DURABLE SpaceTraders lessons.

These are game mechanics that survive a universe wipe — the agent recalls them
(memory_recall / KnowledgeMiddleware) so it doesn't relearn the same thing twice.
Per-reset state (waypoints, prices, contract ids) is NOT seeded here — the agent
re-scans that live each session. Re-run this after a wipe (it's idempotent).

    PYTHONPATH=. python plugins/spacetraders/seed_kb.py [--db PATH]

Human-readable lessons: plugins/spacetraders/LESSONS.md (same content).
"""

from __future__ import annotations

import argparse
import os
import sqlite3

from knowledge.store import KnowledgeStore

SOURCE = "spacetraders-seed"

LESSONS: list[tuple[str, str]] = [
    ("contracts-are-the-engine", (
        "Contracts are the EARLY-GAME capital engine — work the IN-RANGE ones aggressively, "
        "never idle. But a FAR contract (delivery beyond ~one tank from the source) is NOT "
        "worth chasing even when lucrative: the ship can only DRIFT there (1 fuel but HOURS), "
        "which won't finish inside an engine window and just WEDGES the only cargo ship on an "
        "un-fulfillable accepted contract (contracts can't be cancelled). We tried accepting "
        "big far hauls (a 169k DRUGS contract 729u out) — the occasional win wasn't worth the "
        "repeated wedging. RANGE is a SHIP problem, not a guard one: decline far contracts, "
        "work in-range contracts + supply-chain trade, and bank toward a longer-range / "
        "bigger-tank hauler. While the price-map fills, in-range contracts ARE the income.")),
    ("supply-chain-trading", (
        "SpaceTraders sustained trade is the SUPPLY CHAIN, not random arbitrage. Buy a good "
        "where a market EXPORTS it (type EXPORT, supply HIGH/ABUNDANT -> cheap, and it REFILLS "
        "each cycle) and sell where another market IMPORTS it (type IMPORT, supply SCARCE -> "
        "dear). Random buy-low/sell-high on arbitrary goods saturates fast: every trade moves "
        "the price, capped by tradeVolume, so a 50% spread dies in two trades while a 10% "
        "export->import route refills forever. Rank routes by margin x tradeVolume (throughput), "
        "not raw spread. The engine's best_route does this; trust export->import over a bigger "
        "one-off spread.")),
    ("engine-drives-ships", (
        "The deterministic autopilot engine MOVES SHIPS and BUYS GOODS itself (via direct "
        "API in its own loop, NOT the st_navigate/st_purchase tools). To fix a stuck or idle "
        "cargo ship, RE-KICK the engine — st_autopilot_stop then st_autopilot_start — do NOT "
        "hand-drive it with navigate/purchase/deliver. Manually driving a ship forces you to "
        "poll st_ship for arrival, ballooning the turn to hundreds of K tokens and stalling. "
        "The agent's job is STRATEGY; the engine does the clicking. NEVER conclude 'the "
        "autopilot can't purchase' — it can; an idle cargo ship means the engine stalled, "
        "so re-kick it.")),
    ("buying-ships", (
        "SpaceTraders ship-buying: you only need ANY of your ships PRESENT at a shipyard "
        "to buy — so buy at the NEAREST shipyard using a ship already close (probes fly "
        "free + fast). Do NOT route a distant/slow ship across the system just to buy — a "
        "long DRIFT to a shipyard wastes ~an hour. STOP the autopilot first so it doesn't "
        "re-task the ship you send to the shipyard. Reserve-protected growth: buy a 2nd/3rd "
        "CARGO ship first for resilience (one stuck ship must not halt all income), keep a "
        "working capital reserve, and grow toward the fleet-size goal.")),
    ("contracts", (
        "SpaceTraders: only ONE active contract per agent at a time — negotiating a "
        "second returns error 4103/4511 ('already has an active contract'). "
        "Procurement contracts are the biggest early earners (a small advance on "
        "accept + a large payment on fulfill, e.g. ~50k + ~142k) and build faction "
        "reputation. So put ONE cargo ship on the contract and the rest on "
        "hauling/trading/mining. Get a new contract with st_negotiate_contract from "
        "a ship at a faction waypoint, then st_accept_contract.")),
    ("fuel-travel", (
        "SpaceTraders fuel: CRUISE fuel cost is approximately the DISTANCE, so if a "
        "ship's fuel is less than the distance to the target it cannot reach it in "
        "one CRUISE and will strand. DRIFT costs ~1 fuel but is very slow (~7 sec "
        "per distance unit). Use st_travel — it auto-refuels when low, CRUISEs when "
        "fuel covers the distance, and DRIFTs to the nearest fuel station only when "
        "stranded; call it again after a fuel stop until the ship arrives. Probes "
        "(0 fuel capacity) move for free. Refuel only at markets that sell FUEL.")),
    ("rate-limit", (
        "SpaceTraders rate limit is per-ACCOUNT (~2 requests/second, token bucket) "
        "shared by ALL your ships — not per ship. Run the whole fleet from ONE "
        "process/engine (st_fleet_autopilot) so calls share the one budget; never "
        "launch parallel scripts — each self-paces to 2 req/s and together they "
        "exceed the limit (HTTP 429). Ships mostly wait on travel and cooldowns, so "
        "one budget easily covers a whole fleet acting concurrently.")),
    ("market-saturation", (
        "SpaceTraders markets have finite depth: selling a lot of one good crashes "
        "its price. A real builder saw credits/hour DECLINE as mining scaled — ~20 "
        "mining drones bottomed out a system's ore price and started losing money. "
        "So do NOT flood one market or over-mine. Diversify across goods and "
        "markets; higher-margin goods beat metals long-term. Simple and balanced "
        "beats greedy min-maxing, especially for a starter fleet.")),
    ("multiplayer", (
        "SpaceTraders is a shared, persistent universe but player interaction is "
        "limited: there is NO direct player-to-player trade, gifting, or combat. "
        "Collaboration is the shared market economy (your trades move prices "
        "everyone sees) and jump-gate construction (any agent in a system can "
        "supply materials to build the gate, unlocking interstellar travel for "
        "all). Cargo transfer (st_transfer) works only between YOUR OWN co-located "
        "ships.")),
    ("wipe-cycle", (
        "The SpaceTraders universe RESETS every few weeks (check the API root GET / "
        "for resetDate). A reset wipes the agent, token, ships, credits, and "
        "contracts. Durable game mechanics survive (these lessons); per-reset state "
        "does NOT — agent symbol, system, waypoint ids, prices, and contract ids "
        "all change. After a wipe: re-register the agent (needs an account token), "
        "re-seed this knowledge, and re-scan live state. Never assume yesterday's "
        "ids or prices.")),
    ("ship-roles", (
        "SpaceTraders ship roles: COMMAND frigate (versatile — can mine AND haul); "
        "PROBE (0 fuel, free-moving scout — station one at a market to read live "
        "prices); LIGHT_SHUTTLE / LIGHT_HAULER (cargo ships for trade and contract "
        "hauls); MINING_DRONE (mines unattended); SIPHON_DRONE (gas giants). "
        "Early-game loop: command ship + a hauler + light mining — the hauler's "
        "round trip is the bottleneck, so don't out-mine your haul capacity. Buy "
        "ships at a SHIPYARD with st_buy_ship (one of your ships must be present).")),
    ("self-improvement", (
        "How I (protoTrader-in-space) improve: after discovering something "
        "repeatable — a profitable route, a game rule, a pitfall — I record it with "
        "memory_ingest as a finding; before I plan, I recall relevant lessons with "
        "memory_recall. My plan lives in beads (beads_create/list/update); my "
        "objective is the operator's goal. When I hit a missing capability, I note "
        "it as a gap so it can become a new tool, skill, or workflow next time.")),
    ("autopilot-supervision", (
        "The background autopilot (st_autopilot_start) occasionally STRANDS a ship — "
        "it leaves a hauler DOCKED with a full hold, or stalls one mid-route. Don't "
        "trust it to be self-healing: each supervision tick, call st_autopilot_status, "
        "and if a ship is idle/stuck with cargo or the engine isn't running, restart "
        "it (st_autopilot_start) or nudge the ship (st_travel). Engine start/stop is a "
        "lead/commander job — specialists only READ st_autopilot_status and report up.")),
    ("goals-are-per-session", (
        "A goal (set via /goal) is evaluated only after a TERMINAL TURN IN THE SAME "
        "SESSION it was set in. Credits earned by the background engine or a scheduler "
        "tick (a different context) will NOT auto-close the goal — it stays 'active' "
        "even once the target is met. To close it, drive a turn in that session (or "
        "set the standing goal inside the loop's own context). Ground-truth the goal "
        "with a command/data verifier against live state, never the chat transcript.")),
    ("zero-to-million", (
        "Path from a fresh start to ~1,000,000 credits: (1) CONTRACTS are the capital "
        "base but capped at ONE active per agent — work them back-to-back with one "
        "cargo ship for fixed payouts; don't expect them to scale. (2) TRADE ARBITRAGE "
        "is the scaling lever — each hauler runs an INDEPENDENT buy-export-cheap / "
        "sell-import-expensive route, so credits compound with fleet size (the "
        "leaderboard whales run 27-39 ships). (3) SCOUT markets with probes — prices "
        "only show with a ship present, and trade decisions need that intel. (4) "
        "REINVEST profit into LIGHT_HAULERs once capital comfortably exceeds the ~290k "
        "cost AND a profitable route exists to put them on. (5) GUARD every buy: skip "
        "when sell<=buy (saturation), and never accept an un-sourceable contract. "
        "Contracts seed it, trade compounds it, scouting informs it, guards protect it.")),
    ("check-live-prices", (
        "Structural arbitrage (st_trade_routes export→import) tells you WHAT flows "
        "where, but not whether it's profitable RIGHT NOW. Saturated markets can have "
        "sell BELOW buy (seen live: H51 ALUMINUM_ORE buy 156, sell 76 — a guaranteed "
        "loss). Before committing a trade leg, confirm live per-unit prices with a "
        "ship/probe at the market (st_market), and prefer the price-ranked best route "
        "st_trade_routes surfaces. Contracts carry less price risk — they paid the "
        "fixed bonuses that grew the treasury past 500k.")),
]


def _build_store(db_path: str):
    """The SAME store the server uses: HybridKnowledgeStore (FTS5 + embeddings via
    the gateway) when knowledge.embeddings is on, so seeded lessons get vectors and
    semantic recall works — not just keyword FTS5. Degrades to base on any failure."""
    try:
        from graph.config import LangGraphConfig
        config = LangGraphConfig.from_yaml("config/langgraph-config.yaml")
        if getattr(config, "knowledge_embeddings", False):
            from graph.llm import create_embed_fn
            from knowledge.hybrid_store import HybridKnowledgeStore
            embed_fn = create_embed_fn(config)
            if embed_fn is not None:
                return HybridKnowledgeStore(db_path=db_path, embed_fn=embed_fn), "hybrid (FTS5 + embeddings)"
    except Exception as e:  # noqa: BLE001 — never fail the seed; fall back to FTS5
        print(f"  (hybrid store unavailable: {e}; seeding FTS5-only)")
    return KnowledgeStore(db_path=db_path), "FTS5-only"


def seed(db_path: str) -> tuple[int, str]:
    # idempotent: clear prior seed rows (and their vectors) before re-adding
    try:
        con = sqlite3.connect(db_path)
        try:
            con.execute("DELETE FROM chunk_vectors WHERE chunk_id IN "
                        "(SELECT id FROM chunks WHERE source = ?)", (SOURCE,))
        except sqlite3.DatabaseError:
            pass  # no vector table yet
        con.execute("DELETE FROM chunks WHERE source = ?", (SOURCE,))
        con.commit()
        con.close()
    except sqlite3.DatabaseError:
        pass  # tables may not exist yet; add_chunk will create the schema

    store, kind = _build_store(db_path)
    n = 0
    for heading, content in LESSONS:
        rid = store.add_chunk(
            content, domain="fact", heading=f"spacetraders:{heading}",
            source=SOURCE, source_type="seed", finding_type="game-mechanic",
        )
        if rid is not None:
            n += 1
    return n, kind


def main() -> None:
    p = argparse.ArgumentParser()
    default_db = os.path.expanduser("~/.protoagent/knowledge/agent.db")
    try:
        from graph.config import LangGraphConfig
        default_db = LangGraphConfig.from_yaml("config/langgraph-config.yaml").knowledge_db_path or default_db
    except Exception:  # noqa: BLE001
        pass
    p.add_argument("--db", default=default_db)
    args = p.parse_args()
    n, kind = seed(args.db)
    print(f"seeded {n}/{len(LESSONS)} durable SpaceTraders lessons → {args.db} [{kind}]")


if __name__ == "__main__":
    main()
