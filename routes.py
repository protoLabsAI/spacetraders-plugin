"""Trade-route memory — the agent LEARNS profitable routes and remembers them.

The growth engine records every profitable arbitrage route it discovers as a finding
in the knowledge store, and recalls them before re-scanning — so each window (and each
fresh start after a wipe) is faster and smarter than the last. This is the same KB the
agent's ``memory_recall`` reads, so the lead agent learns the routes too. Best-effort:
if the KB is unavailable, learning silently no-ops and the engine just live-scans.
"""

from __future__ import annotations

import re

_STORE = None  # cached store handle (False = unavailable)
_SOURCE = "spacetraders-routes"
_TAG = re.compile(r"tag=route\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|(\d+)")


def _store():
    global _STORE
    if _STORE is not None:
        return _STORE
    try:
        from graph.config import LangGraphConfig
        cfg = LangGraphConfig.from_yaml("config/langgraph-config.yaml")
        if getattr(cfg, "knowledge_embeddings", False):
            from graph.llm import create_embed_fn
            from knowledge.hybrid_store import HybridKnowledgeStore
            fn = create_embed_fn(cfg)
            if fn is not None:
                _STORE = HybridKnowledgeStore(db_path=cfg.knowledge_db_path, embed_fn=fn)
                return _STORE
        from knowledge.store import KnowledgeStore
        _STORE = KnowledgeStore(db_path=cfg.knowledge_db_path)
    except Exception:  # noqa: BLE001 — KB optional; degrade to live-scan only
        _STORE = False
    return _STORE


def remember_route(system: str, good: str, buy_wp: str, sell_wp: str, margin: float) -> None:
    """Record a discovered profitable route as a finding the agent can recall."""
    s = _store()
    if not s:
        return
    try:
        s.add_chunk(
            f"Profitable trade route in {system}: buy {good} at {buy_wp} → sell at "
            f"{sell_wp} for +{int(margin)} cr/unit. "
            f"tag=route|{system}|{good}|{buy_wp}|{sell_wp}|{int(margin)}",
            domain="finding", heading=f"trade-route:{system}:{good}",
            source=_SOURCE, source_type="engine", finding_type="trade-route",
        )
    except Exception:  # noqa: BLE001
        pass


def recall_routes(system: str) -> list[dict]:
    """Previously-discovered routes for a system (most relevant first), deduped."""
    s = _store()
    if not s:
        return []
    try:
        rows = s.search(f"trade route arbitrage {system}", k=12, domain="finding")
    except Exception:  # noqa: BLE001
        return []
    seen, out = set(), []
    for r in rows:
        m = _TAG.search(r.get("content", ""))
        if not m or m.group(1) != system:
            continue
        good, buy_wp, sell_wp, margin = m.group(2), m.group(3), m.group(4), int(m.group(5))
        key = (good, buy_wp, sell_wp)
        if key in seen:
            continue
        seen.add(key)
        out.append({"good": good, "buy_at": buy_wp, "sell_at": sell_wp, "margin": margin})
    return out
