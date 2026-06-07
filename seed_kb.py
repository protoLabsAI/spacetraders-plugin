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
]


def seed(db_path: str) -> int:
    # idempotent: clear prior seed rows, then re-add
    try:
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM chunks WHERE source = ?", (SOURCE,))
        con.commit()
        con.close()
    except sqlite3.DatabaseError:
        pass  # table may not exist yet; add_chunk will create the schema

    store = KnowledgeStore(db_path=db_path)
    n = 0
    for heading, content in LESSONS:
        rid = store.add_chunk(
            content, domain="fact", heading=f"spacetraders:{heading}",
            source=SOURCE, source_type="seed", finding_type="game-mechanic",
        )
        if rid is not None:
            n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.path.expanduser("~/.protoagent/knowledge/agent.db"))
    args = p.parse_args()
    n = seed(args.db)
    print(f"seeded {n}/{len(LESSONS)} durable SpaceTraders lessons → {args.db}")


if __name__ == "__main__":
    main()
