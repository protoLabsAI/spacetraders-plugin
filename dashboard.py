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
from . import roles as R

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
        "role": ("scout" if cargo.get("capacity", 0) == 0
                 else "miner" if R.can_mine(s) else "cargo"),
        "status": nav.get("status", "?"),
        "waypoint": nav.get("waypointSymbol", "?"),
        "fuel": ("∞" if fuel.get("capacity", 0) == 0
                 else f"{fuel.get('current', '?')}/{fuel.get('capacity', '?')}"),
        "cargo": f"{cargo.get('units', 0)}/{cargo.get('capacity', 0)}",
        # Where it's headed (in-transit only) — dest, ISO arrival (JS ticks the ETA), mode,
        # plus origin + departure so the map can interpolate its position along the route.
        "dest": (route.get("destination") or {}).get("symbol") if in_transit else None,
        "origin": (route.get("origin") or {}).get("symbol") if in_transit else None,
        "departure": route.get("departureTime") if in_transit else None,
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


_MAP_CACHE: dict = {}  # system -> waypoints (x,y,type,market); static within a reset


async def _system_map(system: str) -> dict:
    """The system's waypoints (position + type) for the star map, plus which markets
    we've scouted (the price-map coverage). Waypoint geometry is static within a reset."""
    if system in _MAP_CACHE:
        wps = _MAP_CACHE[system]
    else:
        raw: list = []
        try:
            page = 1
            while page <= 12:  # paginate the FULL system — far outposts (asteroid bases,
                batch = await C.call("GET", f"/systems/{system}/waypoints",  # jump gates) live
                                     params={"limit": 20, "page": page})     # on later pages
                if not batch:
                    break
                raw.extend(batch)
                if len(batch) < 20:
                    break
                page += 1
        except C.SpaceTradersError:
            if not raw:
                return {"system": system, "waypoints": []}
        wps = [{"symbol": w["symbol"], "type": w.get("type", "?"),
                "x": w.get("x", 0), "y": w.get("y", 0),
                "market": any(t.get("symbol") == "MARKETPLACE" for t in w.get("traits", []))}
               for w in raw]
        _MAP_CACHE[system] = wps
    from . import prices
    pm = {m["waypointSymbol"]: m["tradeGoods"] for m in prices.price_map(system)}
    out = []
    for w in wps:
        goods = [{"symbol": g["symbol"], "buy": g["purchasePrice"], "sell": g["sellPrice"]}
                 for g in pm.get(w["symbol"], [])][:8]
        out.append({**w, "scouted": w["symbol"] in pm, "goods": goods})
    return {"system": system, "waypoints": out}


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
    strat = fleet.current_strategy()
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
        "map": await _system_map("-".join(agent["headquarters"].split("-")[:2])),
        "contracts": [_contract_row(c) for c in contracts if not c.get("fulfilled")][:4],
        "autopilot": {
            "running": ops.get("running", False),
            "want_running": ops.get("want_running", False),
            "watchdog": ops.get("watchdog", False),  # the reliability heartbeat alive?
            "window": ops.get("started_minutes", 0),
            "log": ops.get("recent_log", []),
            "watchdog_log": ops.get("watchdog_log", []),  # recoveries the watchdog made
            "last_per_hour": last.get("per_hour"),
            "last_gained": last.get("gained"),
            "strategy": strat.get("name"),
            "mining": strat.get("mining"),
        },
        # The OODA strategist's control-surface decision trail — what the agent changed
        # and why, the visible proof of the self-improving brain steering the engine.
        "decisions": fleet.decisions()[-8:],
    }
    _emit_metrics(data["agent"], last)
    _CACHE.update(at=now, data=data)
    return data


def build_dashboard_router() -> APIRouter:
    """The PAGE router — stays on the PUBLIC ``/plugins/spacetraders`` prefix: a
    browser iframe page-load can't carry an Authorization bearer, so a gated page
    would 401-blank under the token gate (plugin-view rule 2). Everything the page
    FETCHES is gated (build_data_router)."""
    router = APIRouter()

    @router.get("/dashboard")
    async def _dashboard():
        return HTMLResponse(_PAGE)

    return router


def build_data_router() -> APIRouter:
    """The DATA route — mounted under ``/api/plugins/spacetraders`` so it inherits
    the operator bearer gate (rule 2, issue #5). Previously ``/state`` lived under
    the public ``/plugins/`` prefix: on a token-gated deployment anyone who could
    reach the port could read fleet state (credits, ships, contracts) without the
    bearer."""
    router = APIRouter()

    @router.get("/state")
    async def _state():
        return JSONResponse(await _snapshot())

    return router


_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Fleet</title>
<script>
  // Slug-aware kit href (protoAgent ADR 0042, plugin-view rule 3): through the fleet
  // proxy the page lives at /agents/<slug>/plugins/... — a hardcoded /_ds/ resolves
  // against the hub root. Inject the <link> so the href carries the base. (The data
  // fetches below are RELATIVE, so they're already slug-safe.)
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,-apple-system,sans-serif);font-size:14px}
  .wrap{max-width:920px;margin:0 auto;padding:20px}
  h1{font-size:16px;margin:0;color:var(--pl-color-accent);letter-spacing:.3px}
  .sub{color:var(--pl-color-fg-muted);font-size:12px;margin-top:2px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--pl-space-4,14px);margin-top:16px}
  .pl-card{margin:0}
  #body>.pl-card,#body>.grid{margin-top:var(--pl-space-4,14px)}
  #body>.pl-stats{margin-top:16px}
  .pl-card>.pl-panel-header{margin:calc(-1*var(--pl-space-4)) calc(-1*var(--pl-space-4)) var(--pl-space-3);flex-wrap:wrap}
  .big{font-size:30px;font-weight:650;color:var(--pl-color-fg)}
  .big small{font-size:13px;color:var(--pl-color-fg-muted);font-weight:400}
  table{width:100%;border-collapse:collapse;font-size:13px}
  .log{font-family:var(--pl-font-mono,ui-monospace,monospace);font-size:11.5px;color:var(--pl-color-fg-muted);
       max-height:120px;overflow:auto;white-space:pre-wrap;line-height:1.5}
</style></head><body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <div><h1>🛰 protoTrader-in-space — Fleet</h1>
      <div class="sub" id="who">connecting…</div></div>
    <div class="sub" id="tick"></div>
  </div>
  <div id="body"></div>
</div>
<script type="module">
// The DS plugin-kit owns the protoagent:init handshake (bearer + theme, incl. live
// re-themes onto the --pl-* tokens) and slug-aware authed fetches — replacing the
// hand-rolled TMAP/listener this page carried. plugin-kit.js is an ES MODULE, so it
// loads via dynamic import (a classic <script src> throws on its exports; see
// protoAgent docs/how-to/build-a-plugin-view.md). Older host without /_ds: fall
// back to a tokenless same-origin shim (fine locally; gated instances serve the kit).
let kit;
try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(window.__base + p, i) }; }
let MAPCARDS = {};
let LAST_D = null;  // last polled state — the 1s ticker re-renders the map so in-transit ships glide
// System-map pan/zoom — the full system spans far outposts, so let the operator explore.
let MAP_VIEW = {s:1, tx:0, ty:0};
function applyMapView(){
  const g = document.getElementById("mapworld");
  if(g) g.setAttribute("transform", `translate(${MAP_VIEW.tx},${MAP_VIEW.ty}) scale(${MAP_VIEW.s})`);
}
document.addEventListener("wheel", e=>{
  const svg = e.target.closest && e.target.closest("#mapsvg"); if(!svg) return;
  e.preventDefault();
  const r = svg.getBoundingClientRect();
  const cx = (e.clientX-r.left)/r.width*860, cy = (e.clientY-r.top)/r.height*300;  // viewBox space (860x300)
  const ns = Math.max(0.5, Math.min(40, MAP_VIEW.s * (e.deltaY<0 ? 1.15 : 1/1.15)));
  const k = ns/MAP_VIEW.s;
  MAP_VIEW.tx = cx-(cx-MAP_VIEW.tx)*k; MAP_VIEW.ty = cy-(cy-MAP_VIEW.ty)*k; MAP_VIEW.s = ns;
  applyMapView();
}, {passive:false});
let _mapDrag = null;
document.addEventListener("mousedown", e=>{
  const svg = e.target.closest && e.target.closest("#mapsvg");
  if(svg) _mapDrag = {x:e.clientX, y:e.clientY, tx:MAP_VIEW.tx, ty:MAP_VIEW.ty, w:svg.getBoundingClientRect().width};
});
document.addEventListener("mousemove", e=>{
  if(!_mapDrag) return;
  const f = 860/_mapDrag.w;  // px → viewBox units
  MAP_VIEW.tx = _mapDrag.tx+(e.clientX-_mapDrag.x)*f; MAP_VIEW.ty = _mapDrag.ty+(e.clientY-_mapDrag.y)*f;
  applyMapView();
});
document.addEventListener("mouseup", ()=>{ _mapDrag = null; });
// Hover detail card for the system map — populated per-element by renderMap.
const _mc = document.createElement("div"); _mc.id = "mapcard";
_mc.style.cssText = "position:fixed;display:none;z-index:60;background:var(--pl-color-bg-raised);border:var(--pl-border-width,1px) solid var(--pl-color-border);"
  + "border-radius:var(--pl-radius,8px);padding:8px 11px;font-size:12px;line-height:1.5;pointer-events:none;max-width:260px;"
  + "box-shadow:0 6px 22px rgba(0,0,0,.5)";
document.addEventListener("DOMContentLoaded", ()=>document.body.appendChild(_mc));
document.addEventListener("mouseover", e=>{
  const k = e.target && e.target.getAttribute && e.target.getAttribute("data-k");
  if(k && MAPCARDS[k]){ _mc.innerHTML = MAPCARDS[k]; _mc.style.display = "block"; }
});
document.addEventListener("mouseout", e=>{
  if(e.target && e.target.getAttribute && e.target.getAttribute("data-k")) _mc.style.display = "none";
});
document.addEventListener("mousemove", e=>{
  if(_mc.style.display !== "block") return;
  const ox = (e.clientX + 16 + 260 > innerWidth) ? e.clientX - 16 - 260 : e.clientX + 16;
  _mc.style.left = ox + "px"; _mc.style.top = (e.clientY + 16) + "px";
});
const cr = n => n == null ? "—" : n.toLocaleString() + " cr";
const stClass = s => ({IN_TRANSIT:"warning",DOCKED:"success",IN_ORBIT:"info"}[s]||"");
const compact = n => n==null?"—":n>=1e9?(n/1e9).toFixed(2)+"B":n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?Math.round(n/1e3)+"k":""+n;
function eta(iso){ if(!iso) return ""; const ms=new Date(iso)-new Date(); if(ms<=0) return "arriving";
  const s=Math.round(ms/1000); return Math.floor(s/60)+"m"+("0"+(s%60)).slice(-2)+"s"; }
function dur(iso){ if(!iso) return "—"; const ms=new Date(iso)-new Date(); if(ms<=0) return "now";
  const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return (d?d+"d ":"")+h+"h "+m+"m"; }
function renderMap(d){
  const wps=((d.map||{}).waypoints)||[];
  if(!wps.length) return '<div class="sub">no map data yet</div>';
  const xs=wps.map(w=>w.x),ys=wps.map(w=>w.y);
  const minx=Math.min(...xs),maxx=Math.max(...xs),miny=Math.min(...ys),maxy=Math.max(...ys);
  const W=860,H=300,pad=26;
  const sx=v=>pad+(v-minx)/((maxx-minx)||1)*(W-2*pad);
  const sy=v=>pad+(v-miny)/((maxy-miny)||1)*(H-2*pad);
  const by={}; wps.forEach(w=>by[w.symbol]=w);
  const col={PLANET:'#6db3f2',GAS_GIANT:'#46c46a',MOON:'#9aa0aa',ASTEROID:'#b08d57',ENGINEERED_ASTEROID:'#b08d57',ASTEROID_BASE:'#b08d57',ASTEROID_FIELD:'#b08d57',FUEL_STATION:'#e0a23c',JUMP_GATE:'var(--pl-color-accent)',ORBITAL_STATION:'#9aa0aa'};
  const lines=(d.routes||[]).map(r=>{const a=by[r.buy_at],b=by[r.sell_at];return (a&&b)?`<line x1="${sx(a.x)}" y1="${sy(a.y)}" x2="${sx(b.x)}" y2="${sy(b.y)}" stroke="var(--pl-color-accent)" stroke-width="1" stroke-dasharray="3 3" opacity="0.7"/>`:'';}).join('');
  MAPCARDS={};
  const esc=s=>(''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const dots=wps.map(w=>{const c=col[w.type]||'#9aa0aa',rad=(w.type==='PLANET'||w.type==='GAS_GIANT')?5:3;const ring=w.scouted?`<circle cx="${sx(w.x)}" cy="${sy(w.y)}" r="${rad+3}" fill="none" stroke="#46c46a" stroke-width="1" opacity="0.6"/>`:'';
    const tags=[w.market?'market':'',w.scouted?'scouted':''].filter(Boolean).join(' · ');
    const goods=(w.goods||[]).map(g=>`${esc(g.symbol)}: <span style="color:var(--pl-color-status-success)">${g.buy}</span> / <span style="color:var(--pl-color-status-warning)">${g.sell}</span>`).join('<br>');
    MAPCARDS['w:'+w.symbol]=`<b style="color:var(--pl-color-accent)">${esc(w.symbol)}</b> · ${esc(w.type)}`+(tags?`<br><span class="sub">${tags}</span>`:'')+(goods?`<br><span style="font-size:11px">buy/sell:<br>${goods}</span>`:(w.market?'<br><span class="sub">not scouted yet</span>':''));
    return `${ring}<circle cx="${sx(w.x)}" cy="${sy(w.y)}" r="${rad}" fill="${c}" data-k="w:${w.symbol}" style="cursor:pointer"/>`;}).join('');
  let paths='';
  const ships=(d.ships||[]).map(s=>{
    let x,y,extra='';
    if(s.status==='IN_TRANSIT' && by[s.origin] && by[s.dest]){
      const o=by[s.origin], dd=by[s.dest];
      const dep=new Date(s.departure).getTime(), arr=new Date(s.arrival).getTime();
      let p=(Date.now()-dep)/((arr-dep)||1); p=Math.max(0,Math.min(1,p));
      x=sx(o.x+(dd.x-o.x)*p); y=sy(o.y+(dd.y-o.y)*p);
      paths+=`<line x1="${sx(o.x)}" y1="${sy(o.y)}" x2="${sx(dd.x)}" y2="${sy(dd.y)}" stroke="#fff" stroke-width="0.6" stroke-dasharray="2 4" opacity="0.35"/>`;
      extra=' · in transit';
    } else { const w=by[s.waypoint]; if(!w) return ''; x=sx(w.x); y=sy(w.y); }
    MAPCARDS['s:'+s.symbol]=`<b style="color:#fff">${esc(s.symbol)}</b> · ${esc(s.status)}<br><span class="sub">@ ${esc(s.waypoint)} · ${esc(s.role)} · cargo ${esc(s.cargo)}${s.dest?' · → '+esc(s.dest)+extra:''}</span>`;
    return `<rect x="${x-3.5}" y="${y-3.5}" width="7" height="7" fill="#fff" transform="rotate(45 ${x} ${y})" data-k="s:${s.symbol}" style="cursor:pointer"/>`;}).join('');
  return `<svg id="mapsvg" viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" style="background:#070b10;border:1px solid var(--pl-color-border);border-radius:8px;cursor:grab;touch-action:none">`
    + `<g id="mapworld" transform="translate(${MAP_VIEW.tx},${MAP_VIEW.ty}) scale(${MAP_VIEW.s})">${lines}${paths}${dots}${ships}</g></svg>`
    + `<div class="sub" style="margin-top:4px">scroll to zoom · drag to pan · <a href="#" onclick="MAP_VIEW={s:1,tx:0,ty:0};applyMapView();return false" style="color:var(--pl-color-accent)">reset view</a></div>`;
}
async function poll(){
  try{
    const d = await (await kit.apiFetch("/api/plugins/spacetraders/state")).json();
    render(d);
  }catch(e){ document.getElementById("body").innerHTML =
    '<div class="pl-callout pl-callout--error"><div class="pl-callout__body">dashboard offline — '+e+'</div></div>'; }
  document.getElementById("tick").textContent = "updated " + new Date().toLocaleTimeString();
}
function render(d){
  LAST_D = d;
  const body = document.getElementById("body");
  const esc = s => (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;');  // local to render() (renderMap has its own)
  if(d.error){
    document.getElementById("who").textContent = "";
    body.innerHTML = '<div class="pl-callout pl-callout--error"><div class="pl-callout__body">'+
      (d.token? "API error: "+d.error : "No SpaceTraders token set. Add it in System → Settings → SpaceTraders.")+
      '</div></div>'; return;
  }
  const a=d.agent, ap=d.autopilot;
  document.getElementById("who").textContent =
    a.symbol+" · "+a.faction+" · HQ "+a.hq+" · "+a.ships+" ships";
  const ships = d.ships.map(s=>`<tr><td>${s.symbol}</td><td>${s.role}</td>
    <td><span class="pl-badge${stClass(s.status)?' pl-badge--'+stClass(s.status):''}">${s.status}</span></td>
    <td>${s.waypoint}</td>
    <td>${s.dest?`→ ${s.dest} · <span class="eta" data-arr="${s.arrival}">${eta(s.arrival)}</span> · ${s.mode}`:'<span style="color:var(--pl-color-fg-muted)">—</span>'}</td>
    <td>${s.fuel}</td><td>${s.cargo}</td></tr>`).join("");
  const lb=d.standing||{}, srv=d.server||{};
  const board=(lb.top||[]).map((x,i)=>`<tr><td style="color:var(--pl-color-fg-muted)">#${i+1}</td><td>${x.agent}</td>
    <td style="text-align:right">${compact(x.credits)}</td></tr>`).join("");
  const youLine=`You — <b>${a.symbol}</b>: ${cr(a.credits)} · `+(lb.rank?`ranked #${lb.rank}`:"unranked")
    +(lb.cutoff?` · top ${lb.board_size} needs ${compact(lb.cutoff)}`:"");
  const rts=(d.routes||[]);
  const routesRows=rts.length?rts.map(r=>`<tr><td>${r.good}</td><td>${r.buy_at}</td>
    <td>${r.sell_at}</td><td style="text-align:right;color:var(--pl-color-status-success)">+${r.margin}</td></tr>`).join("")
    :'<tr><td colspan="4"><div class="pl-empty">none learned yet — probes are scouting…</div></td></tr>';
  const cons = d.contracts.length ? d.contracts.map(c=>`<tr><td>${c.type}</td>
    <td>${c.deliver}</td><td>${c.to}</td><td>${cr(c.pay)}</td>
    <td><span class="pl-badge${c.state==='accepted'?' pl-badge--success':(c.state==='fulfilled'?' pl-badge--info':'')}">${c.state}</span></td></tr>`).join("")
    : '<tr><td colspan="5"><div class="pl-empty">no open contracts</div></td></tr>';
  body.innerHTML = `
  <div class="pl-stats">
    <div class="pl-stat"><div class="pl-stat__num">${a.credits.toLocaleString()} cr</div><div class="pl-stat__label">Treasury</div></div>
    <div class="pl-stat"><div class="pl-stat__num"><span class="pl-dot ${ap.running?'pl-dot--success pl-dot--pulse':''}"></span> ${ap.running?'running':'idle'}${ap.running?` · ${ap.window}m`:''}</div><div class="pl-stat__label">Autopilot${ap.last_per_hour!=null?` · last ${cr(ap.last_gained)} (≈ ${cr(ap.last_per_hour)}/hr)`:''}</div></div>
    <div class="pl-stat"><div class="pl-stat__num">${ap.strategy||'balanced'}</div><div class="pl-stat__label">Strategy · mining ${ap.mining===false?'off':'on'}</div></div>
  </div>
  <div class="grid">
    <div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Standing — most credits</h2></div>
      <table class="pl-table">${board||'<tr><td><div class="pl-empty">leaderboard unavailable</div></td></tr>'}</table>
      <div class="sub" style="margin-top:8px">${youLine}</div></div>
    <div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Universe</h2></div>
      <div class="big" style="font-size:20px">wipe in <span class="wipe" data-next="${srv.next_reset||''}">${dur(srv.next_reset)}</span></div>
      <div class="sub" style="margin-top:4px">${srv.frequency||'?'} reset${srv.next_reset?` · ${new Date(srv.next_reset).toLocaleString()}`:''}</div>
      <div class="sub" style="margin-top:6px">galaxy: ${srv.agents!=null?srv.agents.toLocaleString():'?'} agents · ${compact(srv.ships)} ships · ${compact(srv.systems)} systems</div></div>
  </div>
  <div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">System map — ${(d.map||{}).system||''}</h2>
    <span class="pl-panel-header__kicker">◆ ships · ◯ scouted markets · ┄ learned routes</span></div>
    <div id="mapbox">${renderMap(d)}</div></div>
  <div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Fleet (${d.ships.length})</h2></div>
    <table class="pl-table"><tr><th>Ship</th><th>Role</th><th>Status</th><th>Location</th><th>Headed</th><th>Fuel</th><th>Cargo</th></tr>
    ${ships}</table></div>
  <div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Contracts</h2></div>
    <table class="pl-table"><tr><th>Type</th><th>Deliver</th><th>To</th><th>Pays</th><th>State</th></tr>${cons}</table></div>
  <div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Learned routes</h2><span class="pl-panel-header__kicker">the agent's trade-route memory</span></div>
    <table class="pl-table"><tr><th>Good</th><th>Buy at</th><th>Sell at</th><th>+/unit</th></tr>${routesRows}</table></div>
  ${(d.decisions && d.decisions.length)?`<div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Strategist decisions</h2><span class="pl-panel-header__kicker">the OODA brain steering the engine</span></div>
    <table class="pl-table"><tr><th>Move</th><th>Detail</th></tr>${d.decisions.slice().reverse().map(x=>`<tr><td><span class="pl-badge pl-badge--info">${esc(x.action)}</span></td><td>${esc(x.detail)}</td></tr>`).join("")}</table></div>`:''}
  ${ap.log && ap.log.length?`<div class="pl-card"><div class="pl-panel-header"><h2 class="pl-panel-header__title">Engine log</h2></div>
    <div class="log">${ap.log.map(l=>l.replace(/</g,'&lt;')).join("\n")}</div></div>`:''}
  `;
}
// Boot ONCE, on whichever fires first: the handshake (normal — the bearer arrives
// with protoagent:init, so the gated /state poll authenticates), or a short timer
// for the no-handshake case (standalone page / older host).
let booted = false;
function boot(){ if (booted) return; booted = true; poll(); setInterval(poll, 8000); }
kit.initPluginView(boot);
setTimeout(boot, 800);
// Tick the countdowns every second between polls, and re-render the map so in-transit
// ships glide along their routes (re-interpolated against the current time).
setInterval(()=>{
  document.querySelectorAll('.eta').forEach(e=>{const v=eta(e.dataset.arr); if(v) e.textContent=v;});
  document.querySelectorAll('.wipe').forEach(e=>{e.textContent=dur(e.dataset.next);});
  const mb=document.getElementById('mapbox'); if(mb && LAST_D) mb.innerHTML=renderMap(LAST_D);
},1000);
</script></body></html>"""
