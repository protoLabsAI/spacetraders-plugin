"""Event-bus emissions (ADR 0039) — the engine's live wire to the console and other plugins.

The fleet engine runs long after ``register()`` returns, so this module holds the one
handle it needs: the plugin registry, bound once at register time. ``emit()`` rides
``registry.emit`` (auto-namespaced to ``spacetraders.<event>``, no-cross-namespace rule
kept by the host) and ``navigate()`` rides ``registry.navigate`` (the scoped
``ui.navigate`` intent, ADR 0044). Everything degrades to a silent no-op when unbound —
host-free tests and bare imports never need a bus.

Topics published (declared in the manifest ``emits:`` list; payload shapes documented in
the README until typed event contracts land — protoAgent #1636):

* ``engine_started``  — {window_minutes}
* ``engine_stopped``  — {reason}
* ``window_closed``   — {minutes, credits_start, credits_end, gained, per_hour, ships}
* ``trade_executed``  — {ship, good, buy_at, sell_at, units}
* ``ship_purchased``  — {ship, type, yard}
* ``reset_recovered`` — {status}

Consumers today: the Fleet dashboard (live refresh over the iframe event bridge) and the
console rail notification dot (free, host-side). A Discord/ops plugin can subscribe
``spacetraders.#`` without importing this plugin — that decoupling is the point.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_REGISTRY = None  # the PluginRegistry, bound by register() — None in host-free contexts


def bind(registry) -> None:
    """Bind the plugin registry (called once from ``register()``; re-called on hot-reload)."""
    global _REGISTRY
    _REGISTRY = registry


def emit(event: str, data: dict | None = None) -> bool:
    """Fire-and-forget publish of ``spacetraders.<event>``. True if handed to the bus.

    Never raises: an emission is telemetry, not control flow — the engine must run
    identically with or without a host (the ``supervise`` contract).
    """
    if _REGISTRY is None:
        return False
    try:
        _REGISTRY.emit(event, data or {})
        return True
    except Exception:  # noqa: BLE001 — telemetry must never take down an engine window
        log.debug("[spacetraders] emit(%s) failed", event, exc_info=True)
        return False


def navigate(view: str = "fleet") -> bool:
    """Ask the console to focus this plugin's view (ADR 0044). True if handed to the bus."""
    if _REGISTRY is None:
        return False
    try:
        _REGISTRY.navigate(view)
        return True
    except Exception:  # noqa: BLE001
        log.debug("[spacetraders] navigate(%s) failed", view, exc_info=True)
        return False
