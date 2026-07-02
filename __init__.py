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
    from . import events
    from .client import set_config_token
    from .subagents import space_subagents
    from .tools import get_spacetraders_tools

    # Bind the bus handle FIRST — the engine emits through events.py long after
    # register() returns (ADR 0039); re-binding on hot-reload is idempotent.
    events.bind(registry)

    # Seed the token(s) the user set in the console (System → Settings →
    # SpaceTraders), so the tools authenticate without a hand-edited file.
    cfg = getattr(registry, "config", {}) or {}
    set_config_token(
        cfg.get("token"), cfg.get("account_token"),
        call_sign=cfg.get("call_sign"), faction=cfg.get("faction"),
    )

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

        async def _verify_fleet_size(spec, ctx):
            # A SECOND objective alongside credits — because a pure 'reach N credits' goal
            # punishes buying ships (spending dips credits), so the agent under-invests in
            # its own fleet. A standalone fleet-size monitor goal makes growth explicit.
            from graph.goals.types import VerifyResult

            from .client import call
            try:
                ships = await call("GET", "/my/ships", params={"limit": 20})
                have = len(ships)
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read fleet: {e}", "")
            want = int((spec.get("args") or {}).get("min", 0))
            return VerifyResult(have >= want, f"fleet {have} / {want} ships", str(have))

        registry.register_goal_verifier("spacetraders:fleet_size", _verify_fleet_size)
        log.info("[spacetraders] registered goal verifier spacetraders:fleet_size")

        async def _verify_cargo_capacity(spec, ctx):
            # HAULING POWER, not ship count. A 'reach N ships' goal gets gamed by buying
            # cheap zero-hold probes (5 scouts = "6 ships" but no cargo to carry). Summing
            # cargo capacity rewards the ship that actually grows income: another hauler.
            from graph.goals.types import VerifyResult

            from .client import call
            try:
                ships = await call("GET", "/my/ships", params={"limit": 20})
                have = sum((s.get("cargo") or {}).get("capacity", 0) for s in ships)
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read fleet: {e}", "")
            want = int((spec.get("args") or {}).get("min", 0))
            return VerifyResult(have >= want, f"cargo capacity {have} / {want} units", str(have))

        registry.register_goal_verifier("spacetraders:cargo_capacity", _verify_cargo_capacity)
        log.info("[spacetraders] registered goal verifier spacetraders:cargo_capacity")

        # ── the v1.8 tripwire/ladder verifiers (docs/sdk-round2.md) ──────────────
        # Thin async wrappers over the PURE predicates in watches.py (host-free
        # tested there) — the wrapper owns only the API call + VerifyResult shape.
        # Dual-use by design: a WATCH polls them as a tripwire; a GOAL grounds a
        # ladder rung on the same check.

        async def _verify_net_worth(spec, ctx):
            # Credits + conservative fleet book value. A pure-credits goal punishes
            # buying ships; net worth rewards the hauler that grows income. Evidence
            # is BUCKETED (~2k) so the st-flatline watch's stall detector reads
            # sub-noise drift as "unchanged" (ADR 0067 stall semantics).
            from graph.goals.types import VerifyResult

            from . import watches
            from .client import call
            try:
                agent = await call("GET", "/my/agent")
                ships = await call("GET", "/my/ships", params={"limit": 20})
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read agent/fleet: {e}", "")
            worth = watches.net_worth(agent.get("credits", 0), ships)
            want = int((spec.get("args") or {}).get("min", 0))
            return VerifyResult(worth >= want, f"net worth {worth:,} / {want:,}",
                                watches.worth_bucket(worth))

        registry.register_goal_verifier("spacetraders:net_worth", _verify_net_worth)

        async def _verify_drawdown(spec, ctx):
            # Met = the treasury CRASHED below frac × the persisted high-water mark
            # (the June incident's shape: 172k peak → 36k band). The mark ratchets
            # up inside the verifier — ground truth owns its own history until
            # protoAgent #1632 gives metrics a real home.
            from graph.goals.types import VerifyResult

            from . import watches
            from .client import call
            try:
                credits = (await call("GET", "/my/agent")).get("credits", 0)
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read credits: {e}", "")
            frac = float((spec.get("args") or {}).get("frac", 0.5))
            st = watches.load_state()
            tripped, hw = watches.drawdown(credits, st.get("high_water"), frac)
            if hw != st.get("high_water"):
                st["high_water"] = hw
                watches.save_state(st)
            return VerifyResult(tripped, f"credits {credits:,} vs high-water {hw:,} (frac {frac})",
                                str(credits))

        registry.register_goal_verifier("spacetraders:drawdown", _verify_drawdown)

        async def _verify_reset_detected(spec, ctx):
            # Met = the universe epoch CHANGED since the stored baseline. First
            # sighting only sets the baseline. On a trip the state resets to the new
            # epoch immediately (drops the high-water mark with it) so the watch
            # doesn't re-trip every poll of the same wipe.
            from graph.goals.types import VerifyResult

            from . import watches
            from .client import call
            try:
                server = await call("GET", "/")
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read server status: {e}", "")
            st = watches.load_state()
            changed, epoch = watches.reset_changed(server, st.get("reset_date"))
            if epoch and (changed or "reset_date" not in st):
                watches.save_state({"reset_date": epoch})   # new epoch: drop epoch-scoped state
            return VerifyResult(changed, f"epoch {epoch or '?'} (known: {st.get('reset_date') or 'none'})",
                                epoch)

        registry.register_goal_verifier("spacetraders:reset_detected", _verify_reset_detected)

        async def _verify_contract_deadline(spec, ctx):
            # Met = an accepted, unfulfilled contract is inside its deadline margin
            # (and still salvageable — already-past deadlines don't wake anyone).
            from datetime import UTC, datetime

            from graph.goals.types import VerifyResult

            from . import watches
            from .client import call
            try:
                cons = await call("GET", "/my/contracts", params={"limit": 20})
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read contracts: {e}", "")
            hours = float((spec.get("args") or {}).get("hours", 6.0))
            close, detail = watches.deadline_close(cons, datetime.now(UTC).isoformat(), hours)
            return VerifyResult(close, detail, detail)

        registry.register_goal_verifier("spacetraders:contract_deadline", _verify_contract_deadline)

        async def _verify_opportunity(spec, ctx):
            # Met = the FRESH price map shows a route with an outsized margin%. Reads
            # the same rank_routes the engine dispatches on — the tripwire and the
            # dispatcher can't disagree about what a route is.
            from graph.goals.types import VerifyResult

            from . import analysis, prices, watches
            from .client import call
            try:
                ships = await call("GET", "/my/ships", params={"limit": 1})
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read fleet: {e}", "")
            if not ships:
                return VerifyResult(False, "no ships — no system to scan", "")
            system = ships[0]["nav"]["systemSymbol"]
            routes = analysis.rank_routes(prices.price_map(system))
            best = watches.best_margin_pct(routes)
            want = float((spec.get("args") or {}).get("min_margin_pct", 15.0))
            return VerifyResult(best >= want, f"best fresh margin {best}% vs {want}%", f"{best}%")

        registry.register_goal_verifier("spacetraders:opportunity", _verify_opportunity)

        async def _verify_charted_count(spec, ctx):
            # Met = we CHARTED at least args.min waypoints in the fleet's home system
            # (chart.submittedBy == our call sign — contributions, not visits). The
            # exploration goal-ladder rung; a reputation rung is impossible today
            # (the v2 API exposes no /my/factions reputation surface).
            from graph.goals.types import VerifyResult

            from .client import call
            from .exploration import charted_by
            try:
                agent = await call("GET", "/my/agent")
                ships = await call("GET", "/my/ships", params={"limit": 1})
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read agent/fleet: {e}", "")
            if not ships:
                return VerifyResult(False, "no ships — no system to count charts in", "")
            system = ships[0]["nav"]["systemSymbol"]
            wps, page = [], 1
            try:
                while page <= 12:   # cap ~240 waypoints (st_waypoints' idiom) — a bigger
                    # system would undercount, which for a >=min goal only DELAYS
                    # achievement (never falsely meets); no real system is near the cap
                    batch = await call("GET", f"/systems/{system}/waypoints",
                                       params={"limit": 20, "page": page})
                    if not batch:
                        break
                    wps.extend(batch)
                    if len(batch) < 20:
                        break
                    page += 1
            except Exception as e:  # noqa: BLE001
                return VerifyResult(False, f"could not read waypoints: {e}", "")
            have = charted_by(wps, agent.get("symbol", ""))
            want = int((spec.get("args") or {}).get("min", 0))
            return VerifyResult(have >= want, f"charted {have} / {want} in {system}", str(have))

        registry.register_goal_verifier("spacetraders:charted_count", _verify_charted_count)
        log.info("[spacetraders] registered tripwire verifiers: net_worth, drawdown, "
                 "reset_detected, contract_deadline, opportunity, charted_count")

    # Goal hook (ADR 0028, PR3) — when the operator's substrate goal is achieved, wind
    # down the self-perpetuating engine. This is why WHEN to stop isn't hardcoded in the
    # engine: the target lives in the goal system (any spacetraders:credits value), and
    # achieving it stops the fleet here.
    #
    # v1.8 adds the LADDER (docs/sdk-round2.md): achieving a rung also enqueues a
    # follow-up turn (sdk.run_in_session — non-blocking, safe from a hook) in the goal's
    # own session, prompting the agent to propose the NEXT rung. The ladder lives in the
    # goal system + the agent's judgment, not hardcoded thresholds.
    if hasattr(registry, "register_goal_hook"):
        def _on_goal_achieved(goal) -> None:
            from . import fleet
            fleet.request_stop()
            log.info("[spacetraders] goal achieved (%s) — winding down the fleet engine",
                     getattr(goal, "condition", "?"))
            session = getattr(goal, "session_id", "") or ""
            if not session:
                return
            try:
                from graph.sdk import run_in_session
                run_in_session(
                    session,
                    f"GOAL ACHIEVED: {getattr(goal, 'condition', '?')} "
                    f"(evidence: {getattr(goal, 'last_evidence', '') or 'n/a'}). The fleet engine "
                    "is winding down. Decide the next rung of the ladder — the shape so far: "
                    "seed capital (contracts) → hauling power (cargo_capacity) → treasury "
                    "(credits/net_worth) → the frontier. Propose ONE next goal with a "
                    "spacetraders:* verifier and set it with set_goal, then restart the engine "
                    "(st_autopilot_start) — or, if the operator's intent is met, summarize and stop.",
                    job_id="spacetraders-goal-ladder",
                )
            except Exception:  # noqa: BLE001 — the ladder is optional; stopping cleanly is not
                log.debug("[spacetraders] goal-ladder follow-up not enqueued", exc_info=True)
        registry.register_goal_hook(on_achieved=_on_goal_achieved)
        log.info("[spacetraders] registered goal hook (stop engine + ladder follow-up)")

    # Watch hooks (ADR 0067) — every tripwire trip/expiry/stall lands in the strategist
    # DecisionLog (the dashboard's audit trail). st-flatline reacts HERE (on_stalled →
    # run_in_session) because a stall isn't "met": the watch stays active while the
    # diagnosis turn runs. Hooks fire for every plugin's watches — filter to ours.
    if hasattr(registry, "register_watch_hook"):
        def _ours(watch) -> bool:
            return str(getattr(watch, "id", "")).startswith("st-")

        def _on_watch_met(watch) -> None:
            if not _ours(watch):
                return
            from .knobs import DLOG
            DLOG.record("watch-tripped", f"{watch.id}: {getattr(watch, 'condition', '')} "
                                       f"— {getattr(watch, 'last_reason', '')}")

        def _on_watch_expired(watch) -> None:
            if not _ours(watch):
                return
            from .knobs import DLOG
            DLOG.record("watch-expired", f"{watch.id}: deadline passed unmet")

        def _on_watch_stalled(watch) -> None:
            if not _ours(watch):
                return
            from .knobs import DLOG
            DLOG.record("watch-stalled", f"{watch.id}: evidence frozen "
                                       f"({getattr(watch, 'last_evidence', '?')})")
            if getattr(watch, "id", "") == "st-flatline":
                try:
                    from graph.sdk import run_in_session

                    from . import watches
                    run_in_session(watches.ACTIVITY_SESSION, watches.FLATLINE_STALL_PROMPT,
                                   job_id="spacetraders-flatline")
                except Exception:  # noqa: BLE001
                    log.debug("[spacetraders] flatline follow-up not enqueued", exc_info=True)

        registry.register_watch_hook(on_met=_on_watch_met, on_expired=_on_watch_expired,
                                     on_stalled=_on_watch_stalled)
        log.info("[spacetraders] registered watch hooks (DecisionLog + flatline reflex)")

    # Own-bus subscriptions (ADR 0039) — reactive housekeeping, no polling:
    #  • window_closed → re-arm the tripwire suite (a met watch FINISHES host-side;
    #    stable ids make arm_all a replace, so the suite is always whole while the
    #    engine runs — and this costs nothing when nothing tripped).
    #  • reset_recovered → drop epoch-scoped state (high-water mark); the persisted
    #    plan is already cleared by the recovery path in fleet._recover.
    def _rearm(_payload) -> None:
        from . import watches
        watches.arm_all()

    def _epoch_clear(_payload) -> None:
        from . import watches
        watches.clear_epoch_state()
        log.info("[spacetraders] universe reset — epoch state (high-water mark) cleared")

    async def _synthesize(payload) -> None:
        # v1.9: every lesson_every-th window, distill ground truth into ONE durable
        # lesson (sdk.complete → knowledge_add, epoch-stamped). See lessons.py.
        from . import lessons
        await lessons.on_window_closed((payload or {}).get("data") or {})

    registry.on("spacetraders.window_closed", _rearm)
    registry.on("spacetraders.window_closed", _synthesize)
    registry.on("spacetraders.reset_recovered", _epoch_clear)

    # Console fleet dashboard (ADR 0026) — TWO routers at DISTINCT prefixes: the
    # PAGE stays on the public /plugins/spacetraders (an iframe page-load can't
    # carry a bearer), the /state DATA route mounts under /api/plugins/spacetraders
    # so it inherits the operator bearer gate (plugin-view rule 2, issue #5).
    from .dashboard import build_dashboard_router, build_data_router
    registry.register_router(build_dashboard_router())
    registry.register_router(build_data_router(), prefix="/api/plugins/spacetraders")

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

    # A2A card skills (v2.1, docs/sdk-round2.md) — advertised on the agent card so OTHER
    # agents (a portfolio commander over delegate_to, a sibling trader) consume this
    # fleet structurally. The declared output_schema + result_mime make the executor's
    # structured finalizer enforce the shape (#570): the caller gets JSON it can parse,
    # not prose it must scrape. This is the Syndicate's wire format (docs/syndicate.md).
    if hasattr(registry, "register_a2a_skill"):
        registry.register_a2a_skill({
            "id": "fleet_report",
            "name": "Fleet report",
            "description": (
                "Structured snapshot of this agent's SpaceTraders fleet: credits, net "
                "worth, ship count by role, engine state, active strategy, and credits/hr. "
                "Ground truth from the live game API (st_report / st_agent)."
            ),
            "tags": ["spacetraders", "fleet", "telemetry"],
            "examples": ["Send me your fleet report."],
            "output_schema": {
                "type": "object",
                "required": ["agent", "credits", "ships", "engine_running"],
                "properties": {
                    "agent": {"type": "string", "description": "call sign"},
                    "credits": {"type": "integer"},
                    "net_worth": {"type": "integer",
                                  "description": "credits + conservative fleet book value"},
                    "ships": {"type": "integer"},
                    "roles": {"type": "object",
                              "description": "role -> count (probes/miners/traders/…)"},
                    "engine_running": {"type": "boolean"},
                    "strategy": {"type": "string"},
                    "per_hour": {"type": "integer",
                                 "description": "credits/hr over the last engine window"},
                },
            },
            "result_mime": "application/json",
        })
        registry.register_a2a_skill({
            "id": "quote_route",
            "name": "Quote a trade route",
            "description": (
                "The best FRESH trade route this agent's price map can confirm right now "
                "(st_trade_routes): good, buy/sell waypoints, margin per unit. Empty route "
                "fields mean nothing fresh clears the margin floor — not an error."
            ),
            "tags": ["spacetraders", "trade", "intel"],
            "examples": ["Quote me your best route.", "Any route better than 15% margin?"],
            "output_schema": {
                "type": "object",
                "required": ["has_route"],
                "properties": {
                    "has_route": {"type": "boolean"},
                    "system": {"type": "string"},
                    "good": {"type": "string"},
                    "buy_at": {"type": "string"},
                    "sell_at": {"type": "string"},
                    "margin_per_unit": {"type": "integer"},
                    "margin_pct": {"type": "number"},
                },
            },
            "result_mime": "application/json",
        })
        log.info("[spacetraders] registered A2A card skills: fleet_report, quote_route")

    # /spacetraders chat command (ADR 0018) — a user-only control command: instant
    # fleet status straight off the live API, no model turn, no tokens spent. The
    # agent can't invoke it; that's the seam's design (control plane ≠ tool plane).
    if hasattr(registry, "register_chat_command"):
        async def _status_command(rest: str, session_id: str) -> str | None:
            from . import fleet
            from .client import call
            try:
                agent = await call("GET", "/my/agent")
            except Exception as e:  # noqa: BLE001 — a readable reply beats a stack trace
                return f"**SpaceTraders**: not reachable — {e}"
            try:
                ships = await call("GET", "/my/ships", params={"limit": 20})
            except Exception:  # noqa: BLE001
                ships = []
            ops = fleet.ops_status()
            running = "running" if ops.get("running") else "stopped"
            lines = [
                f"**{agent.get('symbol', '?')}** — {agent.get('credits', 0):,} credits",
                f"fleet: {len(ships)} ship(s) · engine: {running}",
            ]
            tail = (ops.get("recent_log") or [])[-3:]
            if tail:
                lines.append("recent: " + " · ".join(tail))
            return "\n".join(lines)

        registry.register_chat_command("spacetraders", _status_command)
        log.info("[spacetraders] registered /spacetraders chat command")

    # Token test route (ADR 0029 convention) — the manifest's `test: true` renders a
    # console Test button that POSTs here; validate the token against the live API.
    from fastapi import APIRouter

    test_router = APIRouter()

    @test_router.post("/api/config/test-spacetraders")
    async def _test(body: dict | None = None):
        from .client import call
        try:
            agent = await call("GET", "/my/agent")
            return {"ok": True,
                    "identity": f"{agent.get('symbol', '?')} ({agent.get('credits', 0):,} cr)",
                    "error": ""}
        except Exception as e:  # noqa: BLE001 — the button needs a message, not a 500
            return {"ok": False, "identity": "", "error": str(e)}

    registry.register_router(test_router, prefix="")
