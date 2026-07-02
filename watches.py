"""Tripwire watches (ADR 0067) — the fleet's always-on reflexes.

Five concurrent WATCHes, each ground-truthed by a plugin verifier against the live
game (never the engine's own story — the strategist taught us self-reports confabulate,
see docs/engine-rewrite.md). When one trips, its ``run_prompt`` fires as an agent turn
in the durable Activity thread — the reflex arc is: poll → trip → wake the brain.

* ``st-reset``             — the universe wiped (GET / reset date changed) → recovery playbook
* ``st-drawdown``          — credits under ``frac`` × the persisted high-water mark → diagnose
* ``st-contract-deadline`` — an accepted contract is running out of runway → reprioritize
* ``st-flatline``          — net worth unchanged for ``stall_after`` checks → diagnose
  (this one never *meets*; it leans on the watch system's stall detector — evidence
  BUCKETS keep credit jitter from defeating it)
* ``st-opportunity``       — a fresh route with an outsized margin appeared → evaluate

Pure predicate helpers live here (host-free tests, like roles.py); the async verifier
closures that wrap them with ``client.call`` + ``VerifyResult`` are registered in
``__init__.py``. ``arm_all()`` (re)creates the suite idempotently — stable watch ids
mean re-arming REPLACES rather than duplicates, and a met (finished) watch comes back
on the next engine start or window close.

State (high-water mark, known reset epoch) persists next to the knobs/plan files in
the agent-scoped config dir — until protoAgent #1632 (metric timeseries) gives
high-water a real home.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# The Activity thread (events.ACTIVITY_CONTEXT host-side) — the durable session watch
# turns land in. A literal, not an import: this module stays host-free.
ACTIVITY_SESSION = "system:activity"

_STATE_FILE = "spacetraders_watch_state.json"


# ── persisted watch state (high-water mark, reset epoch) ────────────────────────────

def _state_path() -> Path:
    """Agent-scoped state file (same home as spacetraders_knobs.json / _plan.json)."""
    try:
        from graph.config_io import SECRETS_YAML_PATH   # the scoped, per-agent config dir
        return Path(SECRETS_YAML_PATH).parent / _STATE_FILE
    except Exception:  # noqa: BLE001 — host-free (tests): fall back to cwd
        return Path("config") / _STATE_FILE


def load_state() -> dict:
    try:
        p = _state_path()
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001 — unreadable state == no state
        return {}


def save_state(st: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")   # atomic: a mid-write crash can't corrupt the
        tmp.write_text(json.dumps(st))     # real file (a torn read would silently reset
        tmp.replace(p)                     # the high-water mark / reset baseline)
    except Exception:  # noqa: BLE001 — state is advisory; never break a verifier over it
        log.debug("[spacetraders] watch state save failed", exc_info=True)


def clear_epoch_state() -> None:
    """A universe wipe invalidates the treasury history: drop the high-water mark but
    KEEP the new reset epoch (the reset watch just learned it)."""
    st = load_state()
    st.pop("high_water", None)
    save_state(st)


# ── pure predicates (host-free tested) ──────────────────────────────────────────────

def reset_changed(server_status: dict, known_epoch: str | None) -> tuple[bool, str]:
    """(changed?, current_epoch). First sighting (no known epoch) is NOT a change —
    it's the baseline; the caller stores it."""
    current = str(server_status.get("resetDate") or "")
    if not current or known_epoch is None:
        return (False, current)
    return (current != known_epoch, current)


def drawdown(credits: int, high_water: int | None, frac: float) -> tuple[bool, int]:
    """(tripped?, new_high_water). The mark only ratchets UP; a trip needs a real mark
    first (a fresh agent's first reading can't be 'down' from anything)."""
    hw = max(int(high_water or 0), int(credits))
    tripped = high_water is not None and credits < int(high_water) * frac
    return (tripped, hw)


def deadline_close(contracts: list[dict], now_iso: str, hours: float) -> tuple[bool, str]:
    """(any accepted+unfulfilled contract within ``hours`` of its deadline?, detail).
    ISO-8601 strings compare safely after normalizing the Z suffix."""
    from datetime import datetime, timedelta

    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except ValueError:
        return (False, f"unparseable now: {now_iso}")
    for c in contracts:
        if not c.get("accepted") or c.get("fulfilled"):
            continue
        raw = (c.get("terms") or {}).get("deadline") or ""
        try:
            dl = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now <= dl <= now + timedelta(hours=hours):
            left = dl - now
            return (True, f"contract {c.get('id', '?')} deadline in {left.total_seconds() / 3600:.1f}h")
    return (False, "no accepted contract near its deadline")


# Conservative frame book values (cr) for net worth — deliberately rough: the number
# feeds goal thresholds and a flatline *bucket*, not accounting.
_FRAME_VALUE = {
    "FRAME_PROBE": 25_000, "FRAME_DRONE": 40_000, "FRAME_MINER": 60_000,
    "FRAME_LIGHT_FREIGHTER": 250_000, "FRAME_FRIGATE": 300_000,
    "FRAME_HEAVY_FREIGHTER": 1_000_000,
}


def net_worth(credits: int, ships: list[dict]) -> int:
    """credits + conservative fleet book value (frame-based; unknown frames count 25k)."""
    fleet_value = sum(
        _FRAME_VALUE.get(((s.get("frame") or {}).get("symbol") or ""), 25_000) for s in ships
    )
    return int(credits) + fleet_value


def worth_bucket(worth: int, step: int = 2_000) -> str:
    """Evidence bucket for the flatline stall detector: the watch system counts a stall
    as N checks with UNCHANGED evidence, so sub-``step`` jitter must not read as motion."""
    return f"~{(int(worth) // step) * step:,} cr"


def best_margin_pct(routes: list[dict]) -> float:
    """Best margin%% among ranked routes (rank_routes output: buy_price + profit_per_unit)."""
    best = 0.0
    for r in routes:
        buy = r.get("buy_price") or 0
        margin = r.get("profit_per_unit") or 0
        if buy > 0:
            best = max(best, 100.0 * margin / buy)
    return round(best, 1)


# ── the suite: specs + arming ───────────────────────────────────────────────────────

# NOTE: st-flatline's min is unreachable BY DESIGN — that watch never *meets*; it exists
# for its stall detector (evidence buckets above). ADR 0067's stall_after counts
# unchanged-evidence checks, which is exactly "the fleet is running but nothing moves".
WATCH_SPECS: list[dict] = [
    {
        "watch_id": "st-reset",
        "condition": "the SpaceTraders universe has reset (new epoch)",
        "verifier": "spacetraders:reset_detected",
        "verifier_args": {},
        "interval_s": 900,
        "run_prompt": (
            "TRIPWIRE st-reset: the SpaceTraders universe has WIPED (new epoch). Run the "
            "recovery playbook now: 1) st_recover_token (if the call sign is claimed, "
            "st_register a NEW one); 2) verify with st_agent; 3) st_autopilot_start. "
            "The plan/high-water state was cleared automatically; learned lessons survive."
        ),
    },
    {
        "watch_id": "st-drawdown",
        "condition": "the treasury crashed below half its high-water mark",
        "verifier": "spacetraders:drawdown",
        "verifier_args": {"frac": 0.5},
        "interval_s": 600,
        "run_prompt": (
            "TRIPWIRE st-drawdown: credits fell below half the high-water mark. Diagnose from "
            "GROUND TRUTH (st_report + the engine log), not narrative: look for capital stuck in "
            "cargo, a route that went to loss, or a fuel/refit drain. Tune knobs (st_tune) only "
            "with evidence; do NOT micro-pin ships."
        ),
    },
    {
        "watch_id": "st-contract-deadline",
        "condition": "an accepted contract is close to its deadline",
        "verifier": "spacetraders:contract_deadline",
        "verifier_args": {"hours": 6.0},
        "interval_s": 1800,
        "run_prompt": (
            "TRIPWIRE st-contract-deadline: an accepted contract is inside its deadline margin. "
            "Check st_contracts: if it is still fulfillable, make sure the contract ship is on it "
            "(st_report shows roles); if it is NOT fulfillable, let it expire and note why in a "
            "lesson so the next negotiation avoids the shape."
        ),
    },
    {
        "watch_id": "st-flatline",
        "condition": "net worth is growing (stall detector: unchanged = flatline)",
        "verifier": "spacetraders:net_worth",
        "verifier_args": {"min": 10**12},
        "interval_s": 1800,
        "stall_after": 3,
        "run_prompt": "",   # never mets — reacts via the on_stalled hook instead
    },
    {
        "watch_id": "st-opportunity",
        "condition": "an outsized trade margin appeared on a fresh route",
        "verifier": "spacetraders:opportunity",
        "verifier_args": {"min_margin_pct": 15.0},
        "interval_s": 600,
        "run_prompt": (
            "TRIPWIRE st-opportunity: the price map shows a fresh route with an outsized margin. "
            "Evaluate it against the CURRENT plan (st_report, st_trade_routes): if it beats a "
            "hauler's assigned route meaningfully, retune (st_tune min_margin / st_assign) — "
            "otherwise leave the plan alone and log why."
        ),
    },
]

FLATLINE_STALL_PROMPT = (
    "TRIPWIRE st-flatline: net worth has not moved across ~90 minutes of engine windows. "
    "The engine is running but producing nothing. Diagnose from ground truth (st_report: "
    "roles, routes, the engine log): classic causes are an empty price map (scouts not "
    "covering), every route under min_margin, or the fleet wedged on an unreachable target."
)


def arm_all(session: str = ACTIVITY_SESSION) -> dict:
    """(Re)arm the tripwire suite — idempotent: stable ids REPLACE prior watches (a met
    one comes back armed). Called at engine start and window close. Returns a summary."""
    try:
        from graph.sdk import create_watch
    except Exception:  # noqa: BLE001 — host-free: nothing to arm against
        return {"ok": False, "armed": 0, "message": "no host — watches not armed"}
    armed, failed = [], []
    for spec in WATCH_SPECS:
        res = create_watch(
            condition=spec["condition"],
            verifier=spec["verifier"],
            verifier_args=spec["verifier_args"],
            watch_id=spec["watch_id"],
            interval_s=spec.get("interval_s"),
            stall_after=spec.get("stall_after"),
            run_prompt=spec["run_prompt"],
            run_session=session if spec["run_prompt"] else "",
        )
        (armed if res.get("ok") else failed).append(f"{spec['watch_id']}: {res.get('message', '')}")
    if failed:
        log.warning("[spacetraders] %d watch(es) failed to arm: %s", len(failed), "; ".join(failed))
    else:
        log.info("[spacetraders] armed %d tripwire watches", len(armed))
    return {"ok": not failed, "armed": len(armed), "failed": failed}
