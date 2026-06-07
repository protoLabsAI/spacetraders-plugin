"""SpaceTraders fleet dashboard — a console rail view (ADR 0026).

A plugin-contributed console surface: a left-rail "Fleet" icon opens this live
view of the agent's galaxy — credits, ships, contracts, and the background
autopilot — so the operator can WATCH the autonomous fleet instead of polling over
A2A. The console embeds `GET /plugins/spacetraders/dashboard` in an iframe; the page
polls `GET /plugins/spacetraders/state` (server-side, uses the agent token) and
renders. The snapshot is cached briefly so dashboard polling doesn't eat the
per-account rate budget the fleet engine shares.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from . import client as C

_CACHE: dict = {"at": -999.0, "data": None}
_TTL = 8.0  # seconds — cap how often the dashboard hits the live API

# Prometheus gauges so /metrics can alert on a stalled or money-losing fleet
# (T3.1). Best-effort: no-op if prometheus_client isn't installed. Same AGENT_NAME
# prefix as the substrate metrics so they group on /metrics.
try:
    import os
    import re as _re

    from prometheus_client import Gauge

    _p = _re.sub(r"[^a-z0-9]+", "_", os.environ.get("AGENT_NAME", "protoagent").lower()).strip("_") or "protoagent"
    _G_CREDITS = Gauge(f"{_p}_spacetraders_credits", "SpaceTraders agent credits (live)")
    _G_CRHR = Gauge(f"{_p}_spacetraders_credits_per_hour", "SpaceTraders last autopilot run cr/hr")
    _G_SHIPS = Gauge(f"{_p}_spacetraders_ships", "SpaceTraders fleet size")
except Exception:  # noqa: BLE001
    _G_CREDITS = _G_CRHR = _G_SHIPS = None


def _emit_metrics(agent: dict, last: dict) -> None:
    if _G_CREDITS is None:
        return
    try:
        _G_CREDITS.set(agent.get("credits", 0))
        _G_SHIPS.set(agent.get("ships", 0) or 0)
        if last.get("per_hour") is not None:
            _G_CRHR.set(last["per_hour"])
    except Exception:  # noqa: BLE001 — metrics must never break the dashboard
        pass


def _ship_row(s: dict) -> dict:
    nav = s.get("nav", {})
    fuel = s.get("fuel", {})
    cargo = s.get("cargo", {})
    in_transit = nav.get("status") == "IN_TRANSIT"
    route = nav.get("route", {})
    return {
        "symbol": s["symbol"],
        "role": "cargo" if cargo.get("capacity", 0) > 0 else "scout",
        "status": nav.get("status", "?"),
        "waypoint": nav.get("waypointSymbol", "?"),
        "fuel": ("∞" if fuel.get("capacity", 0) == 0
                 else f"{fuel.get('current', '?')}/{fuel.get('capacity', '?')}"),
        "cargo": f"{cargo.get('units', 0)}/{cargo.get('capacity', 0)}",
        # Where it's headed (in-transit only) — dest, ISO arrival (JS ticks the ETA), mode.
        "dest": (route.get("destination") or {}).get("symbol") if in_transit else None,
        "arrival": route.get("arrival") if in_transit else None,
        "mode": nav.get("flightMode") if in_transit else None,
    }


_STATUS_CACHE: dict = {"at": -999.0, "data": None}
_STATUS_TTL = 120.0  # server status (leaderboard + reset + stats) barely moves


async def _server_status() -> dict:
    """The galaxy status root (GET /) — leaderboards, serverResets, stats. Cached."""
    now = asyncio.get_event_loop().time()
    if _STATUS_CACHE["data"] is None or now - _STATUS_CACHE["at"] >= _STATUS_TTL:
        try:
            _STATUS_CACHE["data"] = await C.call("GET", "/")
        except C.SpaceTradersError:
            _STATUS_CACHE["data"] = _STATUS_CACHE["data"] or {}
        _STATUS_CACHE["at"] = now
    return _STATUS_CACHE["data"] or {}


def _standing(status: dict, agent_symbol: str, my_credits: int) -> dict:
    """Top-credits leaderboard + where we stack up."""
    mc = status.get("leaderboards", {}).get("mostCredits", []) or []
    rank = next((i + 1 for i, x in enumerate(mc) if x.get("agentSymbol") == agent_symbol), None)
    return {
        "top": [{"agent": x.get("agentSymbol"), "credits": x.get("credits")} for x in mc[:5]],
        "rank": rank,
        "board_size": len(mc),
        "cutoff": mc[-1].get("credits") if mc else None,   # credits to make the board (#last)
        "you": my_credits,
    }


def _server_info(status: dict) -> dict:
    """Reset cycle + galaxy scale — so the operator knows the clock + the field size."""
    sr = status.get("serverResets", {})
    stats = status.get("stats", {})
    return {
        "next_reset": sr.get("next"),        # ISO — JS ticks the countdown
        "frequency": sr.get("frequency"),    # e.g. "weekly"
        "last_reset": status.get("resetDate"),
        "agents": stats.get("agents"),
        "ships": stats.get("ships"),
        "systems": stats.get("systems"),
    }


def _contract_row(c: dict) -> dict:
    terms = c.get("terms", {})
    dv = (terms.get("deliver") or [{}])[0]
    pay = terms.get("payment", {})
    state = "fulfilled" if c.get("fulfilled") else ("accepted" if c.get("accepted") else "open")
    return {
        "id": c.get("id", "")[-6:],
        "type": c.get("type", "?"),
        "deliver": f"{dv.get('unitsFulfilled', 0)}/{dv.get('unitsRequired', 0)} {dv.get('tradeSymbol', '?')}",
        "to": dv.get("destinationSymbol", "?"),
        "pay": pay.get("onFulfilled", 0),
        "state": state,
    }


async def _snapshot() -> dict:
    now = asyncio.get_event_loop().time()
    if _CACHE["data"] is not None and now - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    try:
        agent = await C.call("GET", "/my/agent")
        ships = await C.call("GET", "/my/ships")
        contracts = await C.call("GET", "/my/contracts")
    except C.SpaceTradersError as e:
        data = {"error": str(e), "token": bool(C.load_token())}
        _CACHE.update(at=now, data=data)
        return data
    from . import fleet
    ops = fleet.ops_status()
    last = ops.get("result") or {}
    status = await _server_status()
    from . import routes as _routes
    learned = _routes.recall_routes("-".join(agent["headquarters"].split("-")[:2]))
    data = {
        "agent": {"symbol": agent["symbol"], "credits": agent["credits"],
                  "hq": agent["headquarters"], "faction": agent.get("startingFaction"),
                  "ships": agent.get("shipCount")},
        "ships": [_ship_row(s) for s in ships],
        "standing": _standing(status, agent["symbol"], agent["credits"]),
        "server": _server_info(status),
        "routes": learned[:6],  # trade routes the agent has learned (route memory)
        "contracts": [_contract_row(c) for c in contracts if not c.get("fulfilled")][:4],
        "autopilot": {
            "running": ops.get("running", False),
            "window": ops.get("started_minutes", 0),
            "log": ops.get("recent_log", []),
            "last_per_hour": last.get("per_hour"),
            "last_gained": last.get("gained"),
        },
    }
    _emit_metrics(data["agent"], last)
    _CACHE.update(at=now, data=data)
    return data


def build_dashboard_router() -> APIRouter:
    router = APIRouter()

    @router.get("/state")
    async def _state():
        return JSONResponse(await _snapshot())

    @router.get("/dashboard")
    async def _dashboard():
        return HTMLResponse(_PAGE)

    return router


_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Fleet</title>
<style>
  :root{--bg:#0a0f14;--fg:#e6e6e6;--mut:#9aa0aa;--acc:#9b87f2;--ok:#46c46a;
        --warn:#e0a23c;--card:#121922;--line:#1f2730}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;font-size:14px}
  .wrap{max-width:920px;margin:0 auto;padding:20px}
  h1{font-size:16px;margin:0;color:var(--acc);letter-spacing:.3px}
  .sub{color:var(--mut);font-size:12px;margin-top:2px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);margin:0 0 10px}
  .big{font-size:30px;font-weight:650;color:var(--fg)}
  .big small{font-size:13px;color:var(--mut);font-weight:400}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--mut);font-weight:500;padding:4px 8px 4px 0;border-bottom:1px solid var(--line)}
  td{padding:5px 8px 5px 0;border-bottom:1px solid var(--line)}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px}
  .pill.run{background:rgba(70,196,106,.15);color:var(--ok)}
  .pill.idle{background:rgba(154,160,170,.15);color:var(--mut)}
  .pill.transit{color:var(--warn)} .pill.docked{color:var(--ok)} .pill.orbit{color:var(--acc)}
  .log{font-family:ui-monospace,monospace;font-size:11.5px;color:var(--mut);
       max-height:120px;overflow:auto;white-space:pre-wrap;line-height:1.5}
  .err{color:var(--warn);padding:24px;text-align:center}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
  .dot.on{background:var(--ok)} .dot.off{background:var(--mut)}
</style></head><body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <div><h1>🛰 protoTrader-in-space — Fleet</h1>
      <div class="sub" id="who">connecting…</div></div>
    <div class="sub" id="tick"></div>
  </div>
  <div id="body"></div>
</div>
<script>
let TOKEN = null;
window.addEventListener("message", e => {
  const m = e.data || {};
  if (m.type !== "protoagent:init") return;
  TOKEN = m.token || null;
  if (m.theme && m.theme.bg) document.documentElement.style.setProperty("--bg", m.theme.bg);
  if (m.theme && m.theme.fg) document.documentElement.style.setProperty("--fg", m.theme.fg);
});
const cr = n => n == null ? "—" : n.toLocaleString() + " cr";
const stClass = s => ({IN_TRANSIT:"transit",DOCKED:"docked",IN_ORBIT:"orbit"}[s]||"");
const compact = n => n==null?"—":n>=1e9?(n/1e9).toFixed(2)+"B":n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?Math.round(n/1e3)+"k":""+n;
function eta(iso){ if(!iso) return ""; const ms=new Date(iso)-new Date(); if(ms<=0) return "arriving";
  const s=Math.round(ms/1000); return Math.floor(s/60)+"m"+("0"+(s%60)).slice(-2)+"s"; }
function dur(iso){ if(!iso) return "—"; const ms=new Date(iso)-new Date(); if(ms<=0) return "now";
  const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return (d?d+"d ":"")+h+"h "+m+"m"; }
async function poll(){
  try{
    const h = TOKEN ? {Authorization:"Bearer "+TOKEN} : {};
    const d = await (await fetch("state",{headers:h})).json();
    render(d);
  }catch(e){ document.getElementById("body").innerHTML =
    '<div class="err">dashboard offline — '+e+'</div>'; }
  document.getElementById("tick").textContent = "updated " + new Date().toLocaleTimeString();
}
function render(d){
  const body = document.getElementById("body");
  if(d.error){
    document.getElementById("who").textContent = "";
    body.innerHTML = '<div class="card err">'+
      (d.token? "API error: "+d.error : "No SpaceTraders token set. Add it in System → Settings → SpaceTraders.")+
      '</div>'; return;
  }
  const a=d.agent, ap=d.autopilot;
  document.getElementById("who").textContent =
    a.symbol+" · "+a.faction+" · HQ "+a.hq+" · "+a.ships+" ships";
  const ships = d.ships.map(s=>`<tr><td>${s.symbol}</td><td>${s.role}</td>
    <td><span class="pill ${stClass(s.status)}">${s.status}</span></td>
    <td>${s.waypoint}</td>
    <td>${s.dest?`→ ${s.dest} · <span class="eta" data-arr="${s.arrival}">${eta(s.arrival)}</span> · ${s.mode}`:'<span style="color:var(--mut)">—</span>'}</td>
    <td>${s.fuel}</td><td>${s.cargo}</td></tr>`).join("");
  const lb=d.standing||{}, srv=d.server||{};
  const board=(lb.top||[]).map((x,i)=>`<tr><td style="color:var(--mut)">#${i+1}</td><td>${x.agent}</td>
    <td style="text-align:right">${compact(x.credits)}</td></tr>`).join("");
  const youLine=`You — <b>${a.symbol}</b>: ${cr(a.credits)} · `+(lb.rank?`ranked #${lb.rank}`:"unranked")
    +(lb.cutoff?` · top ${lb.board_size} needs ${compact(lb.cutoff)}`:"");
  const rts=(d.routes||[]);
  const routesRows=rts.length?rts.map(r=>`<tr><td>${r.good}</td><td>${r.buy_at}</td>
    <td>${r.sell_at}</td><td style="text-align:right;color:var(--ok)">+${r.margin}</td></tr>`).join("")
    :'<tr><td colspan="4" style="color:var(--mut)">none learned yet — probes are scouting…</td></tr>';
  const cons = d.contracts.length ? d.contracts.map(c=>`<tr><td>${c.type}</td>
    <td>${c.deliver}</td><td>${c.to}</td><td>${cr(c.pay)}</td>
    <td><span class="pill ${c.state==='accepted'?'run':'idle'}">${c.state}</span></td></tr>`).join("")
    : '<tr><td colspan="5" style="color:var(--mut)">no open contracts</td></tr>';
  body.innerHTML = `
  <div class="grid">
    <div class="card"><h2>Treasury</h2><div class="big">${a.credits.toLocaleString()}<small> cr</small></div></div>
    <div class="card"><h2>Autopilot</h2>
      <div class="big" style="font-size:18px">
        <span class="dot ${ap.running?'on':'off'}"></span>${ap.running?'running':'idle'}
        ${ap.running?`<small> · ${ap.window}m window</small>`:''}</div>
      ${ap.last_per_hour!=null?`<div class="sub">last run: ${cr(ap.last_gained)} (≈ ${cr(ap.last_per_hour)}/hr)</div>`:''}
    </div>
  </div>
  <div class="grid" style="margin-top:14px">
    <div class="card"><h2>Standing — most credits</h2>
      <table>${board||'<tr><td style="color:var(--mut)">leaderboard unavailable</td></tr>'}</table>
      <div class="sub" style="margin-top:8px">${youLine}</div></div>
    <div class="card"><h2>Universe</h2>
      <div class="big" style="font-size:20px">wipe in <span class="wipe" data-next="${srv.next_reset||''}">${dur(srv.next_reset)}</span></div>
      <div class="sub" style="margin-top:4px">${srv.frequency||'?'} reset${srv.next_reset?` · ${new Date(srv.next_reset).toLocaleString()}`:''}</div>
      <div class="sub" style="margin-top:6px">galaxy: ${(srv.agents||'?').toLocaleString?srv.agents.toLocaleString():srv.agents} agents · ${compact(srv.ships)} ships · ${compact(srv.systems)} systems</div></div>
  </div>
  <div class="card" style="margin-top:14px"><h2>Fleet (${d.ships.length})</h2>
    <table><tr><th>Ship</th><th>Role</th><th>Status</th><th>Location</th><th>Headed</th><th>Fuel</th><th>Cargo</th></tr>
    ${ships}</table></div>
  <div class="card" style="margin-top:14px"><h2>Contracts</h2>
    <table><tr><th>Type</th><th>Deliver</th><th>To</th><th>Pays</th><th>State</th></tr>${cons}</table></div>
  <div class="card" style="margin-top:14px"><h2>Learned routes <span style="color:var(--mut);font-weight:400;text-transform:none">— the agent's trade-route memory</span></h2>
    <table><tr><th>Good</th><th>Buy at</th><th>Sell at</th><th>+/unit</th></tr>${routesRows}</table></div>
  ${ap.log && ap.log.length?`<div class="card" style="margin-top:14px"><h2>Engine log</h2>
    <div class="log">${ap.log.map(l=>l.replace(/</g,'&lt;')).join("\n")}</div></div>`:''}
  `;
}
poll(); setInterval(poll, 8000);
// Tick the countdowns every second between polls.
setInterval(()=>{
  document.querySelectorAll('.eta').forEach(e=>{const v=eta(e.dataset.arr); if(v) e.textContent=v;});
  document.querySelectorAll('.wipe').forEach(e=>{e.textContent=dur(e.dataset.next);});
},1000);
</script></body></html>"""
