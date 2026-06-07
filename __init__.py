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

    # Console fleet dashboard (ADR 0026) — rail view at /plugins/spacetraders/*.
    from .dashboard import build_dashboard_router
    registry.register_router(build_dashboard_router())

    for cfg in space_subagents():
        registry.register_subagent(cfg)
    log.info("[spacetraders] registered crew subagents: navigator, trader, miner, fleet-commander")
