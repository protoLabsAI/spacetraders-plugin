# SDK round 2 — full seam parity

**Status:** accepted (operator goal, 2026-07-01) · **builds on** [`two-loop-fleet.md`](two-loop-fleet.md) and [`engine-rewrite.md`](engine-rewrite.md)

> Round 1 ran the other direction: this plugin's needs drove the extraction of
> `graph.sdk.supervise`, `Knobs`, `DecisionLog`/`telemetry`, and the plugin testkit
> (protoAgent #1024–#1028). Since then the host shipped a **reactive surface** this
> plugin doesn't touch: WATCH tripwires (ADR 0067), drive-only goals with plugin
> verifiers as the ground-truth spine (ADR 0066), `run_in_session`, the plugin event
> bus with free console notification dots (ADR 0039), the knowledge SDK (ADR 0043 /
> 0031), and — in flight — background results (ADR 0070).
>
> **The goal of round 2, set by the operator: full seam parity.** Every seam the
> plugin SDK exposes is either adopted here with a *genuine* use, or recorded below
> as N/A with a rationale. Gaps the work surfaces become protoAgent enhancement
> issues (six filed so far, table at the bottom). SpaceTraders is the demonstration
> vehicle because the game schedules real disasters (the weekly universe wipe),
> pays for reactivity (markets move), and keeps score publicly (the leaderboard).

## The seam-parity matrix

Legend: ✅ adopted · 🔜 planned (stage) · ⛔ N/A with rationale. "Genuine use" is the
bar — a seam adopted for parity theater would be worse than a gap.

### Contribution seams (`PluginRegistry`)

| Seam | Status | Use here |
|---|---|---|
| `register_tools` | ✅ | 38 `st_*` tools |
| `register_router` | ✅ | public dashboard page + bearer-gated `/api/...{/state}` data router |
| `register_subagent` | ✅ | navigator / trader / miner / fleet-commander / strategist |
| skills dir (conventional) | ✅ | 4 skills incl. the OODA `manage-the-fleet` loop |
| workflows dir (conventional) | ✅ | procurement-run / mining-run / fleet-bootstrap |
| `register_goal_verifier` | ✅ (v1.8: 8 total) | credits / fleet_size / cargo_capacity + the tripwire five: `net_worth`, `drawdown`, `reset_detected`, `contract_deadline`, `opportunity` (`charted_count`/`reputation` move to v2.0 with the exploration tools they measure) |
| `register_goal_hook` | ✅ v1.8 | stops the engine on achievement + `run_in_session` next-rung ladder |
| `emit` | ✅ v1.7 | engine lifecycle + economy events (`window_closed`, `trade_executed`, …) |
| `register_chat_command` | ✅ v1.7 | `/spacetraders` — instant fleet status without an agent turn |
| `navigate` | ✅ v1.7 | flip the console to the Fleet view when the engine starts |
| `on` | ✅ v1.8 | own-bus housekeeping: `window_closed` → re-arm tripwires; `reset_recovered` → clear epoch state |
| `register_watch_hook` | ✅ v1.8 | DecisionLog entry per trip/expiry/stall + the flatline reflex (`on_stalled` → diagnosis turn) |
| `register_surface` | ✅ (pre-1.7) | engine shutdown lifecycle (boot re-arm turned out unnecessary — watches persist and keep polling host-side across restarts) |
| `register_a2a_skill` | 🔜 v2.1 | typed `fleet_report` / `quote_route` (output_schema + result_mime) |
| `register_late_tool_factory` | ⛔ | for meta-tools that wrap the *whole* toolset (e.g. execute_code); ST contributes domain tools only |
| `register_mcp_server` | ⛔ | seam manages config-gated *external MCP processes*; ST wraps a plain REST API natively |
| `register_knowledge_store` / `register_embedder` | ⛔ | infra-swap seam for backend plugins; ST is a knowledge *consumer* (v1.9) |
| `register_thread_id_resolver` | ⛔ | for comms plugins that own an external thread↔session mapping |
| `register_middleware` | ⛔ (revisit) | no cross-cutting model/tool concern that hooks don't serve better; revisit if one appears |

### Consumption seams (`graph.sdk` + host services)

| Seam | Status | Use here |
|---|---|---|
| `supervise` | ✅ | the engine's watchdog lifecycle |
| `Knobs` / `make_knob_tools` | ✅ | 17 tunables + presets (`st_tune`, `st_strategy`) |
| `telemetry` / `DecisionLog` / `render_html` | ✅ | `st_report` envelope + dashboard panels |
| `create_watch` | ✅ v1.8 | the tripwire suite (below) |
| `run_in_session` | ✅ v1.8 | goal-ladder + flatline-reflex hooks; watch `run_prompt`s use it via the controller |
| `knowledge_add` / `knowledge_search` | ✅ v1.9 | epoch-stamped route memory + window lessons (replaced the hand-rolled store, which read a HARDCODED config path — wrong KB for instance-scoped agents) |
| `complete` | ✅ v1.9 | lesson synthesis every `lesson_every`-th window (`lessons.py`) |
| `config` | ✅ v1.9 | `routes.py` no longer needs config at all (the SDK owns store resolution); `seed_kb.py` keeps its direct read — it's a standalone bootstrap script, no live `STATE` to tap |
| `host.publish` | ✅ v1.7 | via the `events.py` helper (engine runs off-register, needs the bound handle) |
| `host.on` | ✅ v1.8 | the re-arm + epoch-clear listeners (via `registry.on`) |
| `host.apply_settings` | ⛔ | `save_token` runs MID-TURN (from `st_register` / reset recovery) and `apply_settings` triggers a full graph reload — a reload under a running turn is the hazard, not a convenience. The direct scoped-secrets write (the #3 fix) hits the exact file the host seeds from; adopting the seam here would be parity theater. Right home: an operator-driven settings flow, which the console Test button already covers. |
| `run_subagent` / `subagent_types` | ⛔ | reached via the workflows plugin's recipes; no direct call site |
| `host.invoke` | ⛔ | for chat-surface plugins driving the agent from an external channel |

### Manifest surface

`config`/`secrets`/`settings` ✅ · `views` ✅ · `min_protoagent_version` ✅ ·
`emits:` ✅ v1.7 · `subscribes:` ✅ v1.8 · `test: true` + token test route ✅ v1.7 ·
`guide_url` ✅ v1.7 · `requires_pip` stays `[]` (pure httpx — and see protoAgent
#1631 for why that matters on desktop).

## The stages

Each stage is one release: PR → CI green → protoquinn review → merge → tag. Tests
stay host-free where possible (testkit for the rest).

### v1.7 — events & liveness

The engine becomes observable in real time, and the console feels it.

- **`events.py`**: `emit(event, data)` → `HOST.publish("spacetraders.<event>", data)`,
  None-safe so host-free tests and bare imports never break. The bus is threadsafe
  off-loop (protoAgent `events/bus.py` reroutes via `call_soon_threadsafe`), so the
  engine can emit mid-window.
- **Emissions**: `engine_started`, `engine_stopped`, `window_closed` (net worth,
  credits, trades, decisions), `trade_executed` (route, profit, ship),
  `ship_purchased`, `reset_recovered`. Declared in the manifest `emits:` list;
  payload shapes documented in `README` prose until protoAgent #1636 (typed event
  contracts) lands.
- **Dashboard goes live**: subscribe over the plugin-kit iframe bridge
  (`protoagent:subscribe {patterns: ["spacetraders.#"]}`), debounce-refresh `/state`
  on any event; keep the poll as a slow (60s) fallback. Free bonus: the rail
  notification dot now lights on fleet activity when the view is hidden.
- **`/spacetraders` chat command**: user-only instant status (credits, ships,
  engine state, active contract) — no model turn, no tokens.
- **`registry.navigate("fleet")`** when the engine starts: "grow the treasury" flips
  the console to the dashboard.
- **Config polish**: `test: true` + a `POST .../test-spacetraders` route that
  validates the token against `GET /my/agent`; `guide_url` → the README.

### v1.8 — tripwires & the goal ladder

The autonomy release: the plugin reacts to the world without a cron and without a
human. Bumps `min_protoagent_version` to the first host release with `sdk.create_watch`.

**Watch verifiers** (all ground-truthed against the live API, registered like the
goal verifiers):

| Watch | Trips when | `run_prompt` (fires in the Activity thread) |
|---|---|---|
| `spacetraders:reset_detected` | `GET /` reset date ≠ stored epoch | the full recovery playbook: recover/re-register, re-seed, restart engine |
| `spacetraders:drawdown` | credits < N% of persisted high-water mark | strategist diagnosis turn |
| `spacetraders:contract_deadline` | deadline − ETA margin below threshold | reprioritize delivery |
| `spacetraders:flatline` | `stall_after` windows with no net-worth movement | the June 36–43k band, caught in one window |
| `spacetraders:opportunity` | price-map spread > threshold | wake the strategist for a route it hasn't seen |

- Armed idempotently (stable `watch_id`s) when the engine starts, and re-armed by an
  own-bus `on("spacetraders.window_closed")` subscription — a met watch *finishes*
  host-side, so the suite heals itself while the engine runs. `run_session` is the
  durable Activity thread (`system:activity`) so no `InjectedState` is needed in
  module scope (the known host-free-register constraint). Watches persist and keep
  polling host-side across restarts — the planned boot re-arm turned out unnecessary.
- **`register_watch_hook`**: every trip/expiry/stall lands in the DecisionLog; the
  flatline reflex lives in `on_stalled` (a stall isn't "met" — the watch stays
  active while the diagnosis turn runs). `st-flatline` leans on ADR 0067's stall
  semantics deliberately: an unreachable `min` + evidence *buckets* (~2k) so credit
  jitter reads as "unchanged" — the watch never mets, it only stalls.
- **Epoch hygiene**: `on("spacetraders.reset_recovered")` clears the high-water
  mark; the recovery path already clears the persisted plan — the cross-wipe
  staleness class (v1.4.1) handled at the root.
- **Goal ladder**: a `net_worth` verifier (credits + conservative fleet book value,
  frame-based); `on_achieved` → `run_in_session` proposes the next rung (contracts →
  haulers → treasury → the frontier). `charted_count`/`reputation` rungs land with
  the v2.0 exploration tools they measure.
- High-water/flatline history persists next to the knobs file until protoAgent
  #1632 (metric timeseries) gives it a real home.

### v1.9 — memory that respects the wipe

- Migrate `routes.py` off its hand-rolled embeddings store (direct
  `graph.llm.create_embed_fn` import — a layering smell) onto
  `knowledge_add`/`knowledge_search`, domain `spacetraders`.
- **Epoch scoping**: lessons and routes carry the reset-date epoch; retrieval
  filters to the current epoch. Until protoAgent #1634 (knowledge lifecycle) lands,
  the epoch rides in the heading/domain convention; purge upgrades when the SDK can.
- **Lesson synthesis**: at window close, `sdk.complete()` distills the DecisionLog
  into one durable lesson; the strategist reads lessons before tuning.
- `save_token` → `host.apply_settings` (stop hand-writing `secrets.yaml`);
  `sdk.config()` replaces direct `graph.config` imports.

### v2.0 — the frontier (SHIPPED; ADR 0070 merged host-side #1604/#1605)

- **`st_explore`**: launches a detached background charting campaign — picks an idle
  probe, builds a nearest-first sweep over the system's uncharted waypoints
  (`exploration.py`, pure + host-free tested), and spawns a disposable `explorer`
  subagent via the host BackgroundManager (`campaigns.py`). The report rides the
  ADR 0070 pipeline for free — push-resume nudge, KB-indexed report, console report
  card — landing in the durable Activity thread (same home as the tripwire turns).
  Direct `STATE.background_mgr` access is the documented stopgap until protoAgent
  #1635 (`sdk.spawn_background`) lands.
- New tools: `st_chart`, `st_scan_waypoints`/`st_scan_systems`, `st_jump`/`st_warp`,
  `st_construction` + `st_supply_construction` (the jump-gate community goal — a
  literal shared, long-horizon objective the trade engine can source like any goods
  run) — plus the `explorer` crew subagent (bounded-worklist discipline).
- **`charted_count` verifier** (9 total): counts waypoints whose `chart.submittedBy`
  is OUR call sign — contributions, not visits. A `reputation` rung is confirmed
  IMPOSSIBLE today: the v2 API exposes no `/my/factions` reputation surface (checked
  against the OpenAPI spec), so that matrix row is an API-level ⛔, not a deferral.

### v2.1 — the Syndicate

- `register_a2a_skill`: typed `fleet_report` / `quote_route` (`output_schema` +
  `result_mime`) so other agents consume fleet state and price intel structurally.
- The multi-agent recipe: several instances with different doctrines (mining-heavy /
  trade-max / explorer), federated by a portfolio commander over `delegate_to`
  (ADR 0025), sharing intel over A2A — documented as a runnable setup, not code.
- Final pass on the ⛔ rows: confirm or overturn each N/A verdict.

## protoAgent issues this program filed

| # | Seam gap | Needed by |
|---|---|---|
| [#1631](https://github.com/protoLabsAI/protoAgent/issues/1631) | frozen-app opt-in for non-bundled `requires_pip` | ecosystem (not this plugin — pure httpx) |
| [#1632](https://github.com/protoLabsAI/protoAgent/issues/1632) | `sdk.record_metric` / `metric_history` timeseries | v1.8 drawdown/flatline, dashboard sparkline |
| [#1633](https://github.com/protoLabsAI/protoAgent/issues/1633) | `sdk.react_on` reactive-rule sugar | v1.7/v1.8 event→turn glue |
| [#1634](https://github.com/protoLabsAI/protoAgent/issues/1634) | knowledge lifecycle: purge / TTL / epoch | v1.9 wipe-scoped memory |
| [#1635](https://github.com/protoLabsAI/protoAgent/issues/1635) | `sdk.spawn_background` (with ADR 0070) | v2.0 exploration campaigns |
| [#1636](https://github.com/protoLabsAI/protoAgent/issues/1636) | typed event contracts in `emits:` | v1.7 cross-plugin consumers (Discord feed) |

New gaps found mid-stage get filed the same way — that feedback loop *is* the
point of the program.
