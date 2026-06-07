#!/usr/bin/env python3
"""One-command fresh start / post-wipe recovery for protoTrader-in-space.

    register a fresh agent  →  seed the durable lessons  →  kick the growth engine

so spinning up a new account (or recovering after the weekly universe wipe) is a
single step. Needs the SpaceTraders ACCOUNT token (config/secrets.yaml
``spacetraders.account_token`` or the SPACETRADERS_ACCOUNT_TOKEN env var) to register,
and — for the kick — the server running on 127.0.0.1:7870.

    PYTHONPATH=. python config/plugins/spacetraders/fresh_start.py <CALLSIGN> [FACTION]
    PYTHONPATH=. python config/plugins/spacetraders/fresh_start.py     # reuse saved token, just seed + kick
"""

import asyncio
import os
import sys


def _account_token() -> str | None:
    tok = os.environ.get("SPACETRADERS_ACCOUNT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        import yaml
        d = yaml.safe_load(open("config/secrets.yaml")) or {}
        return ((d.get("spacetraders") or {}).get("account_token") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


async def main() -> None:
    # Make `spacetraders` importable regardless of CWD — the plugin lives next to this
    # file (…/config/plugins/spacetraders/), so add its parent (…/config/plugins).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    callsign = sys.argv[1] if len(sys.argv) > 1 else None
    faction = sys.argv[2] if len(sys.argv) > 2 else "COSMIC"
    from spacetraders import client as C

    # 1 — register a fresh agent (account token → agent token, saved). Skipped if no
    #     callsign given (reuse the saved agent token — e.g. a plain re-kick).
    if callsign:
        acct = _account_token()
        if not acct:
            print("✗ no account token — set spacetraders.account_token in config/secrets.yaml "
                  "or SPACETRADERS_ACCOUNT_TOKEN, then retry.")
            sys.exit(1)
        try:
            d = await C.call("POST", "/register", token=acct,
                             json={"symbol": callsign, "faction": faction})
            C.save_token(d["token"])
            a = d["agent"]
            print(f"✓ registered {a['symbol']} ({faction}) — {a['credits']:,} cr, HQ {a['headquarters']}; token saved")
        except C.SpaceTradersError as e:
            print(f"… register failed ({e}); continuing with the saved agent token if any")
    else:
        print("· no callsign — reusing the saved agent token")

    # 2 — seed the durable lessons (idempotent; survives the wipe as the agent's memory)
    try:
        from graph.config import LangGraphConfig
        db = LangGraphConfig.from_yaml("config/langgraph-config.yaml").knowledge_db_path
        from spacetraders import seed_kb
        n, kind = seed_kb.seed(db)
        print(f"✓ seeded {n} durable lessons [{kind}]")
    except Exception as e:  # noqa: BLE001
        print(f"… lesson seed skipped ({e})")

    # 3 — kick the growth engine via the running server (best-effort)
    try:
        from evals.client import AgentClient
        c = AgentClient(base_url="http://127.0.0.1:7870")
        r = await c.ask(
            "Fresh start: confirm our agent + credits, then start the fleet autopilot for 60 "
            "minutes and grow the treasury toward 1,000,000 credits. Briefly say what each ship is doing.",
            timeout_s=150, context_id="fresh-start")
        print("✓ engine kicked:\n" + (getattr(r, "text", "") or "")[:400])
    except Exception as e:  # noqa: BLE001
        print(f"… couldn't auto-kick the engine ({e}); start the server, then say "
              "'grow the treasury' in the console (the scheduler tick will also pick it up).")


if __name__ == "__main__":
    asyncio.run(main())
