"""Trade-route memory — the agent LEARNS profitable routes and remembers them.

The growth engine records every profitable arbitrage route it discovers as a finding
in the knowledge store, and recalls them before re-scanning — so each window (and each
fresh start after a wipe) is faster and smarter than the last. This is the same KB the
agent's ``memory_recall`` reads, so the lead agent learns the routes too. Best-effort:
if the KB is unavailable, learning silently no-ops and the engine just live-scans.

v1.9 (SDK round-2): rides ``graph.sdk.knowledge_add`` / ``knowledge_search`` instead of
hand-building a store from a hardcoded ``config/langgraph-config.yaml`` — which read the
WRONG store for any instance-scoped agent (fleet/workspace agents resolve their KB from
``STATE``, not the host's default config path). The SDK reads the live scoped store and
runs the sync backend off-loop; the direct ``graph.llm``/``knowledge.*`` imports are gone.

Routes are **epoch-stamped** (the universe reset date, from the watch state): recall
filters to the CURRENT epoch, so a remembered route can never steer a hauler toward a
market from a wiped universe — the v1.4.1 "cross-wipe recall" class, now fixed at the
data layer instead of by distrusting recall entirely. Un-stamped legacy rows are treated
as stale. (Purge of old epochs: protoAgent #1634.)
"""

from __future__ import annotations

import re

_TAG = re.compile(r"tag=route\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|(\d+)")
_EPOCH = re.compile(r"epoch=(\S+)")


def _current_epoch() -> str:
    """The stored universe reset date (baselined by the st-reset watch / verifier)."""
    try:
        from . import watches
    except ImportError:      # bare-module context (host-free tests import `routes` directly)
        import watches       # type: ignore[no-redef]
    return str(watches.load_state().get("reset_date") or "")


async def remember_route(system: str, good: str, buy_wp: str, sell_wp: str, margin: float) -> None:
    """Record a discovered profitable route as an epoch-stamped finding."""
    try:
        from graph.sdk import knowledge_add
    except Exception:  # noqa: BLE001 — host-free: no KB to learn into
        return
    epoch = _current_epoch()
    try:
        await knowledge_add(
            f"Profitable trade route in {system}: buy {good} at {buy_wp} → sell at "
            f"{sell_wp} for +{int(margin)} cr/unit. "
            f"tag=route|{system}|{good}|{buy_wp}|{sell_wp}|{int(margin)}"
            + (f" epoch={epoch}" if epoch else ""),
            domain="finding", heading=f"trade-route:{system}:{good}",
        )
    except Exception:  # noqa: BLE001 — learning is best-effort, never engine control flow
        pass


async def recall_routes(system: str) -> list[dict]:
    """Previously-discovered routes for ``system`` in the CURRENT epoch (deduped,
    most relevant first). Rows from another epoch — or legacy rows with no stamp —
    are dropped: a wiped universe's markets don't exist."""
    try:
        from graph.sdk import knowledge_search
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = await knowledge_search(f"trade route arbitrage {system}", k=12, domain="finding")
    except Exception:  # noqa: BLE001
        return []
    epoch = _current_epoch()
    seen, out = set(), []
    for r in rows:
        content = r.get("content") or r.get("preview") or ""
        m = _TAG.search(content)
        if not m or m.group(1) != system:
            continue
        em = _EPOCH.search(content)
        if epoch and (em.group(1) if em else "") != epoch:
            continue   # another universe's market — or an un-stamped legacy row
        good, buy_wp, sell_wp, margin = m.group(2), m.group(3), m.group(4), int(m.group(5))
        key = (good, buy_wp, sell_wp)
        if key in seen:
            continue
        seen.add(key)
        out.append({"good": good, "buy_at": buy_wp, "sell_at": sell_wp, "margin": margin})
    return out
