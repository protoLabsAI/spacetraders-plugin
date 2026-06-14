"""Reset-recovery + token-update behaviour (client.py).

Covers the universe-reset path: a 4113 token error is recognized, a (re-)register
goes through the single ``register_agent`` helper that saves the fresh token +
call sign, and ``recover_from_reset`` auto-re-registers the configured call sign.
``client`` imports cleanly on its own (no relative imports), so these run without
the full plugin package.
"""

import asyncio

import pytest

import client as C


@pytest.fixture(autouse=True)
def _reset_config():
    # set_config_token only *updates* fields it's given (so callers can set just the
    # token) — to fully clear between tests, reset the module globals directly.
    def _clear():
        C._CONFIG_TOKEN = C._CONFIG_ACCOUNT_TOKEN = None
        C._CONFIG_CALL_SIGN = C._CONFIG_FACTION = None
    _clear()
    yield
    _clear()


def test_is_reset_error_by_code():
    assert C.is_reset_error(C.SpaceTradersError("boom", code=4113)) is True
    assert C.is_reset_error(C.SpaceTradersError("nope", code=4111)) is False


def test_is_reset_error_by_message():
    assert C.is_reset_error(C.SpaceTradersError("Token reset_date does not match")) is True
    assert C.is_reset_error(C.SpaceTradersError("something else")) is False


def test_register_agent_saves_token_and_call_sign(monkeypatch):
    saved = {}
    monkeypatch.setattr(C, "save_token", lambda t: saved.update(token=t))

    async def fake_call(method, path, **kw):
        assert path == "/register"
        return {"token": "FRESH", "agent": {"symbol": "PROTO", "credits": 5}}

    monkeypatch.setattr(C, "call", fake_call)
    data = asyncio.run(C.register_agent("PROTO", "COSMIC", "acct-tok"))
    assert data["token"] == "FRESH"
    assert saved["token"] == "FRESH"          # token persisted
    assert C.call_sign() == "PROTO"           # call sign remembered for recovery


def test_recover_needs_account_token():
    out = asyncio.run(C.recover_from_reset())
    assert "no account token" in out


def test_recover_needs_call_sign():
    C.set_config_token(None, "acct-tok")
    out = asyncio.run(C.recover_from_reset())
    assert "no call sign" in out


def test_recover_reregisters_and_saves(monkeypatch):
    C.set_config_token(None, "acct-tok", call_sign="PROTO", faction="COSMIC")
    monkeypatch.setattr(C, "save_token", lambda t: None)

    async def fake_call(method, path, **kw):
        return {"token": "FRESH2", "agent": {"credits": 42}}

    monkeypatch.setattr(C, "call", fake_call)
    out = asyncio.run(C.recover_from_reset())
    assert out.startswith("re-registered PROTO")


def test_recover_surfaces_claimed_call_sign(monkeypatch):
    C.set_config_token(None, "acct-tok", call_sign="PROTO")

    async def fake_call(method, path, **kw):
        raise C.SpaceTradersError("[4111] claimed", code=4111)

    monkeypatch.setattr(C, "call", fake_call)
    out = asyncio.run(C.recover_from_reset())
    assert "already claimed" in out


def test_call_raises_4113_with_code(monkeypatch):
    # A 4113 envelope → an actionable, code-tagged error so callers can branch.
    class _Resp:
        status_code = 200  # envelope-level error, HTTP 200 with error body
        is_success = False

        def json(self):
            return {"error": {"code": 4113, "message": "reset_date does not match"}}

    class _Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(C.httpx, "AsyncClient", lambda **kw: _Http())
    with pytest.raises(C.SpaceTradersError) as ei:
        asyncio.run(C.call("GET", "/my/agent", token="dead"))
    assert ei.value.code == 4113 and C.is_reset_error(ei.value)
