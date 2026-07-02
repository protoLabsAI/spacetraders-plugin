"""Lesson synthesis — the fleet writes its own operations manual (SDK round-2, v1.9).

Every ``lesson_every``-th engine window, distill the window's ground truth (the stats
payload + the DecisionLog tail + the live knobs) into ONE durable lesson via
``sdk.complete`` (a bare one-shot completion — no tools, no persona) and file it in the
knowledge store under the ``spacetraders-lessons`` domain, stamped with the current
universe epoch. The strategist's ORIENT step recalls these — so the slow loop learns
from windows it never saw, and a post-wipe fresh start inherits doctrine, not stale
positions (epoch-stamped: retrieval can prefer the current universe).

Cadence is a knob (``lesson_every``, 0 = off): at 15-minute windows the default 4 is
one LLM call an hour — bounded and cheap. Synthesis must never touch engine control
flow: everything here is best-effort and exception-swallowing by design.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Process-lifetime window counter (module state, like fleet._LOG). A restart resets the
# cadence — the first post-restart lesson waits a full `lesson_every` windows. That's
# deliberate slack, not drift: the cadence bounds LLM spend, it doesn't schedule anything,
# so persisting it would buy nothing (and the counter feeds no control flow).
_COUNT = {"windows": 0}

_SYSTEM = (
    "You are the fleet-operations analyst for an autonomous SpaceTraders trading fleet. "
    "From one engine window's telemetry, distill AT MOST one durable lesson: a cause→effect "
    "observation that would change how the fleet is tuned or dispatched next time. "
    "2-4 sentences, concrete numbers where they matter. If the window was routine and "
    "teaches nothing, reply exactly NO_LESSON."
)


def _window_brief(payload: dict, decisions: list, knobs: dict, strategy: str) -> str:
    """The synthesis prompt body — ground truth only (stats, decisions, knobs), never
    the engine's narrative log (the strategist taught us self-reports confabulate)."""
    dec = "\n".join(f"- {d.get('action', '?')}: {d.get('detail', '')}" for d in decisions[-8:]) or "- none"

    def n(key: str, sign: str = "") -> str:   # missing numbers render as '?', never raise
        v = payload.get(key)
        return format(v, f"{sign},") if isinstance(v, (int, float)) else "?"

    return (
        f"Engine window: {payload.get('minutes', '?')} min, "
        f"credits {n('credits_start')} → {n('credits_end')} "
        f"({n('gained', '+')}; {n('per_hour')}/hr), "
        f"{payload.get('ships', '?')} ships.\n"
        f"Strategy: {strategy}\nKnobs: {knobs}\nRecent decisions:\n{dec}\n\n"
        "One durable lesson, or NO_LESSON."
    )


async def on_window_closed(payload: dict) -> str | None:
    """Bus-handler body for ``spacetraders.window_closed`` — count windows, synthesize on
    cadence. Returns the lesson text (for tests), None when skipped/off/no-lesson."""
    from .knobs import KNOBS, current_strategy, decisions, knobs

    try:
        every = int(KNOBS.get("lesson_every") or 0)   # Knobs.get RAISES on an unknown key
    except Exception:  # noqa: BLE001 — knob not defined (older knobs.py) → feature off
        return None
    if every <= 0:
        return None
    _COUNT["windows"] += 1
    if _COUNT["windows"] % every:
        return None
    try:
        from graph.sdk import complete, knowledge_add
    except Exception:  # noqa: BLE001 — host-free: nothing to synthesize with
        return None
    try:
        brief = _window_brief(payload or {}, decisions(), knobs(), current_strategy()["name"])
        lesson = (await complete(brief, system=_SYSTEM)).strip()
        if not lesson or "NO_LESSON" in lesson:
            return None
        from . import watches
        epoch = watches.load_state().get("reset_date", "")
        stamped = f"{lesson}\n\ntag=lesson{f' epoch={epoch}' if epoch else ''}"
        await knowledge_add(stamped, domain="spacetraders-lessons",
                            heading=f"fleet lesson (window {_COUNT['windows']})")
        log.info("[spacetraders] filed a window lesson (window %d)", _COUNT["windows"])
        return lesson
    except Exception:  # noqa: BLE001 — synthesis is telemetry, never control flow
        log.debug("[spacetraders] lesson synthesis failed", exc_info=True)
        return None
