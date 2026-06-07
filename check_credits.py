#!/usr/bin/env python3
"""Goal verifier helper — exit 0 iff the agent's LIVE credits >= the threshold arg.

Used by a goal's `command` verifier so "reach N credits" is ground-truthed against
the real treasury (via the dashboard's /state endpoint, which fetches it with the
agent token), NOT judged from the chat transcript. Exit 1 = not yet; 2 = couldn't
read state.

    python3 plugins/spacetraders/check_credits.py 500000
"""
import json
import sys
import urllib.request

PORT = 7870  # the local server's port

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: check_credits.py <threshold>", file=sys.stderr)
        sys.exit(2)
    try:
        threshold = int(sys.argv[1])
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/plugins/spacetraders/state", timeout=12) as r:
            credits = json.load(r).get("agent", {}).get("credits")
        if credits is None:
            print("no credits in state (token set?)", file=sys.stderr)
            sys.exit(2)
        print(f"credits={credits:,} threshold={threshold:,}")
        sys.exit(0 if credits >= threshold else 1)
    except Exception as e:  # noqa: BLE001
        print(f"check failed: {e}", file=sys.stderr)
        sys.exit(2)
