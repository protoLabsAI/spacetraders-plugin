"""Exploration campaigns — detached background workers (ADR 0050/0070, v2.0).

``launch()`` hands a bounded charting worklist (exploration.py builds it) to a
disposable background ``explorer`` subagent via the host's BackgroundManager. The
report comes back through the ADR 0070 results pipeline for free: a push-resume
nudge into the origin session, a KB-indexed report, and the console report card —
none of which this plugin implements.

Reports land in the durable Activity thread (``system:activity``) — the same home
as the v1.8 tripwire turns, and deliberately NOT the spawning chat session: a
campaign outlives its chat (that's the point of detaching it), and Activity is
where the fleet's autonomous life already lives. This also keeps module scope free
of ``InjectedState`` (the known host-free-register constraint).

Direct ``STATE.background_mgr`` access is a stopgap: protoAgent #1635 asks for
``sdk.spawn_background``; migrate when it lands.
"""

from __future__ import annotations

import logging

from . import exploration

log = logging.getLogger(__name__)

ACTIVITY_SESSION = "system:activity"


async def launch(system: str, probe: str, targets: list[dict]) -> dict:
    """Spawn the charting campaign; returns ``{ok, job_id?, message}`` immediately."""
    if not targets:
        return {"ok": False, "message": f"nothing to chart in {system} — every waypoint has a chart"}
    try:
        from runtime.state import STATE   # sdk.spawn_background when protoAgent #1635 lands
        mgr = STATE.background_mgr
    except Exception:  # noqa: BLE001 — host-free
        mgr = None
    if mgr is None:
        return {"ok": False, "message": "background workers unavailable on this host (no background_mgr)"}
    brief = exploration.campaign_brief(system, targets, probe=probe)
    job_id = await mgr.spawn(
        origin_session=ACTIVITY_SESSION,
        subagent_type="explorer",
        description=f"chart {len(targets)} waypoint(s) in {system} with {probe}",
        prompt=brief,
    )
    log.info("[spacetraders] exploration campaign %s: %d waypoint(s) in %s",
             job_id, len(targets), system)
    return {"ok": True, "job_id": job_id,
            "message": f"exploration campaign {job_id} launched: {len(targets)} waypoint(s) in "
                       f"{system} with {probe} — the report will land in the Activity thread"}
