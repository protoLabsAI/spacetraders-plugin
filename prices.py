"""Market price memory — record scouted prices OVER TIME so supply-chain routing works.

A market only shows live prices while a ship is parked there, so at any instant
only ~1 market in a system is "lit". Supply-chain routing needs two markets lit at
once (a good EXPORTED cheap at one + IMPORTED dear at another) — which almost never
happens with a couple of roving probes. So we PERSIST every observation: each time
a ship visits a market we record its prices (and its type/supply/tradeVolume), and the
trade-route finder runs over the recorded map (built up as probes sweep), not just the
one currently-lit market.

Structured sqlite next to the knowledge store; survives the window, rebuilds
naturally after a wipe (stale rows age out). Best-effort — never breaks the engine.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_CONN = None


def _db_path() -> str:
    for base in ("/sandbox/spacetraders", os.path.expanduser("~/.protoagent/spacetraders")):
        try:
            Path(base).mkdir(parents=True, exist_ok=True)
            return str(Path(base) / "prices.db")
        except OSError:
            continue
    return os.path.expanduser("~/.protoagent/spacetraders-prices.db")


def _conn():
    global _CONN
    if _CONN is not None:
        return _CONN
    c = sqlite3.connect(_db_path(), check_same_thread=False)
    # Migrate: older schema stored only buy/sell. The supply-chain finder needs the
    # type/supply/volume fields, so if an old table exists, drop it — price data is
    # ephemeral and rebuilds as probes scout. New rows carry the full shape.
    cols = {r[1] for r in c.execute("PRAGMA table_info(prices)").fetchall()}
    if cols and "type" not in cols:
        c.execute("DROP TABLE prices")
    c.execute("CREATE TABLE IF NOT EXISTS prices ("
              "waypoint TEXT, good TEXT, system TEXT, type TEXT, supply TEXT, "
              "volume INTEGER, buy INTEGER, sell INTEGER, ts REAL, "
              "PRIMARY KEY (waypoint, good))")
    c.commit()
    _CONN = c
    return c


def record_market(system: str, waypoint: str, trade_goods: list) -> None:
    """Persist a market's live prices (call whenever a ship reads a market)."""
    if not trade_goods:
        return
    try:
        c = _conn()
        now = time.time()
        for g in trade_goods:
            bp, sp = g.get("purchasePrice"), g.get("sellPrice")
            if bp is None or sp is None:
                continue
            c.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?)",
                      (waypoint, g["symbol"], system, g.get("type"), g.get("supply"),
                       g.get("tradeVolume"), bp, sp, now))
        c.commit()
    except Exception:  # noqa: BLE001
        pass


def price_map(system: str, max_age: float = 3600.0) -> list[dict]:
    """The recorded markets for a system as best_route input — latest prices per
    waypoint, dropping observations older than ``max_age`` seconds (prices drift).
    Each tradeGood carries the supply-chain shape (type/supply/tradeVolume)."""
    try:
        c = _conn()
        cutoff = time.time() - max_age
        rows = c.execute(
            "SELECT waypoint, good, type, supply, volume, buy, sell "
            "FROM prices WHERE system=? AND ts>=?",
            (system, cutoff)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    by_wp: dict[str, list] = {}
    for wp, good, gtype, supply, volume, buy, sell in rows:
        by_wp.setdefault(wp, []).append({
            "symbol": good, "type": gtype, "supply": supply,
            "tradeVolume": volume, "purchasePrice": buy, "sellPrice": sell})
    return [{"waypointSymbol": wp, "tradeGoods": tg} for wp, tg in by_wp.items()]


def stats(system: str) -> dict:
    """How much the price map has learned (for the dashboard / supervision)."""
    try:
        c = _conn()
        wps = c.execute("SELECT COUNT(DISTINCT waypoint) FROM prices WHERE system=?", (system,)).fetchone()[0]
        obs = c.execute("SELECT COUNT(*) FROM prices WHERE system=?", (system,)).fetchone()[0]
        return {"markets": wps, "observations": obs}
    except Exception:  # noqa: BLE001
        return {"markets": 0, "observations": 0}
