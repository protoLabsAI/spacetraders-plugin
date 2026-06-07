"""spacetraders plugin — protoTrader-in-space.

Wires the agent to the live SpaceTraders v2 API (https://spacetraders.io): a
persistent, shared galactic economy of ships, markets, and contracts. Contributes
the fleet/market/mining/contract **tools** so the agent can actually play —
register, scan, navigate, mine, sell, work contracts. Needs a SpaceTraders token
(``SPACETRADERS_TOKEN`` env or ``config/spacetraders.token``); the tools return a
clear hint until one is present. Built on the plugin reach (ADR 0018) — no core
edit. Disable: ``plugins: { disabled: [spacetraders] }``.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.spacetraders")


def register(registry) -> None:
    from .client import set_config_token
    from .subagents import space_subagents
    from .tools import get_spacetraders_tools

    # Seed the token(s) the user set in the console (System → Settings →
    # SpaceTraders), so the tools authenticate without a hand-edited file.
    cfg = getattr(registry, "config", {}) or {}
    set_config_token(cfg.get("token"), cfg.get("account_token"))

    registry.register_tools(get_spacetraders_tools())
    log.info("[spacetraders] registered galactic fleet/market/contract tools")

    # Goal verifier (ADR 0028, PR1) — ground-truth "reach N credits" against LIVE
    # game state, in-process, no host shell (replaces the check_credits.py shell-out).
    # Use: /goal {"condition": "...", "verifier":
    #   {"type": "plugin", "check": "spacetraders:credits", "args": {"min": 1000000}}}
    if hasattr(registry, "register_goal_verifier"):
        async def _verify_credits(spec, ctx):
            from graph.goals.types import VerifyResult

            from .client import call
            try:
                have = (await call("GET", "/my/agent")).get("credits", 0)
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read credits: {e}", "")
            want = int((spec.get("args") or {}).get("min", 0))
            return VerifyResult(have >= want, f"credits {have:,} / {want:,}", str(have))

        registry.register_goal_verifier("spacetraders:credits", _verify_credits)
        log.info("[spacetraders] registered goal verifier spacetraders:credits")

    # Goal hook (ADR 0028, PR3) — when the operator's substrate goal is achieved, wind
    # down the self-perpetuating engine. This is why WHEN to stop isn't hardcoded in the
    # engine: the target lives in the goal system (any spacetraders:credits value), and
    # achieving it stops the fleet here.
    if hasattr(registry, "register_goal_hook"):
        def _on_goal_achieved(goal) -> None:
            from . import fleet
            fleet.request_stop()
            log.info("[spacetraders] goal achieved (%s) — winding down the fleet engine",
                     getattr(goal, "condition", "?"))
        registry.register_goal_hook(on_achieved=_on_goal_achieved)
        log.info("[spacetraders] registered goal hook (stop engine on goal achieved)")

    # Console fleet dashboard (ADR 0026) — rail view at /plugins/spacetraders/*.
    from .dashboard import build_dashboard_router
    registry.register_router(build_dashboard_router())

    for cfg in space_subagents():
        registry.register_subagent(cfg)
    log.info("[spacetraders] registered crew subagents: navigator, trader, miner, fleet-commander")

    # Fleet-engine lifecycle surface (ADR 0018) — the background autopilot starts
    # on demand (st_autopilot_start) as an asyncio task; register a surface so it's
    # cleanly STOPPED on server shutdown/reload instead of orphaned mid-loop.
    from . import fleet

    async def _fleet_surface_start() -> None:
        log.info("[spacetraders] fleet-engine surface ready (autopilot starts on demand)")

    async def _fleet_surface_stop() -> None:
        try:
            fleet.stop_ops()
            log.info("[spacetraders] fleet engine stopped cleanly on shutdown")
        except Exception:  # noqa: BLE001 — shutdown must not raise
            pass

    registry.register_surface(_fleet_surface_start, stop=_fleet_surface_stop,
                              name="spacetraders-fleet")
