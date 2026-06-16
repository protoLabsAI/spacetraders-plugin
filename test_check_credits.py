"""Goal-verifier helper (check_credits.py).

Regression guard for issue #5's route move: the ``/state`` data route is now
bearer-gated under ``/api/``, so the verifier (a) must hit the ``/api/`` path, not
the old public ``/plugins/`` one (which 404s), and (b) must send the operator bearer
on a gated box. The bug went unnoticed because nothing pinned the path. These run
host-free — ``check_credits`` is stdlib-only at import (the host loader is a guarded,
in-function fallback), and the ``__main__`` block doesn't execute on import.
"""

import check_credits as cc


def test_state_url_is_gated_api_path():
    # The public /plugins/ prefix carries the page; the JSON moved under /api/ (#5).
    # The old path (/plugins/spacetraders/state, no /api/) would fail this endswith.
    assert cc.STATE_URL.endswith("/api/plugins/spacetraders/state")


def test_token_prefers_env(monkeypatch):
    monkeypatch.setenv("A2A_AUTH_TOKEN", "  tok-123  ")
    assert cc.operator_token() == "tok-123"  # trimmed


def test_token_blank_on_open_box(monkeypatch):
    # No env var and no importable host secrets → open box → no header sent.
    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "graph.config_io", None)
    assert cc.operator_token() == ""
