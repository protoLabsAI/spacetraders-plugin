#!/usr/bin/env python3
"""Goal verifier helper — exit 0 iff the agent's LIVE credits >= the threshold arg.

Used by a goal's `command` verifier so "reach N credits" is ground-truthed against
the real treasury (via the dashboard's /state endpoint, which fetches it with the
agent token), NOT judged from the chat transcript. Exit 1 = not yet; 2 = couldn't
read state.

    python3 plugins/spacetraders/check_credits.py 500000

The state route is **bearer-gated** — issue #5 moved it under ``/api/`` so fleet
state isn't readable without the operator token on a gated deployment. This helper
sends that bearer when the box is gated, sourced (in the server's own order) from
``A2A_AUTH_TOKEN`` (the env the goal-verifier subprocess inherits from the server),
falling back to ``secrets.yaml``'s ``auth.token`` via the host config loader when
the package is importable. On an open (token-less, loopback) box no header is needed.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PORT = 7870  # the local server's port
# The DATA route is mounted under /api/ (issue #5) so it inherits the operator
# bearer gate — the public /plugins/ prefix carries the page, not the state JSON.
STATE_URL = f"http://127.0.0.1:{PORT}/api/plugins/spacetraders/state"


def operator_token() -> str:
    """The operator bearer, mirroring the server's resolution order: the
    ``A2A_AUTH_TOKEN`` env first, else ``secrets.yaml``'s ``auth.token`` via the host
    loader (best-effort — handles config-dir/instance scoping). Blank on an open box,
    in which case no Authorization header is sent and the request still succeeds."""
    tok = os.environ.get("A2A_AUTH_TOKEN", "").strip()
    if tok:
        return tok
    try:  # the blessed resolver; absent when the host isn't on path (a bare run)
        from graph.config_io import load_secrets

        return ((load_secrets().get("auth") or {}).get("token") or "").strip()
    except Exception:  # noqa: BLE001 — no host package / no secrets file → treat as open
        return ""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: check_credits.py <threshold>", file=sys.stderr)
        sys.exit(2)
    try:
        threshold = int(sys.argv[1])
        req = urllib.request.Request(STATE_URL)
        token = operator_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=12) as r:
            credits = json.load(r).get("agent", {}).get("credits")
        if credits is None:
            print("no credits in state (token set?)", file=sys.stderr)
            sys.exit(2)
        print(f"credits={credits:,} threshold={threshold:,}")
        sys.exit(0 if credits >= threshold else 1)
    except urllib.error.HTTPError as e:
        hint = " — gated box: export A2A_AUTH_TOKEN for the verifier" if e.code == 401 else ""
        print(f"check failed: HTTP {e.code}{hint}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # noqa: BLE001
        print(f"check failed: {e}", file=sys.stderr)
        sys.exit(2)
