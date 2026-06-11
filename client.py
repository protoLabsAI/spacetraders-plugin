"""Thin async client for the SpaceTraders v2 API (https://spacetraders.io).

protoTrader-in-space's link to a *live* galactic economy — real ships, real
markets, real contracts on a shared persistent universe that resets every few
weeks. This module is just transport: token loading, the HTTP call, rate-limit
backoff, and turning the API's error envelope into a readable string. The agent
tools in ``tools.py`` build on it.

Auth: a per-agent **agent token** (bearer). Get one by registering an agent
(`st_register`, needs an **account token** from your spacetraders.io account),
or paste an existing one. Resolution order: ``SPACETRADERS_TOKEN`` env → in-process
config (seeded by ``register()``) → this agent's **scoped** ``secrets.yaml`` → the
legacy ``config/spacetraders.token`` (gitignored). Persistence is per-agent scoped
so fleet/workspace agents keep their token across reloads (not just the host).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

BASE_URL = "https://api.spacetraders.io/v2"
_TOKEN_FILE = Path(__file__).resolve().parents[2] / "config" / "spacetraders.token"

# SpaceTraders rate limit: ~2 requests/second (token bucket, short bursts allowed).
# We self-pace to a min interval so autonomous loops never trip the 429 path; the
# 429 handler below is the safety net for bursts and other clients on the token.
_MIN_INTERVAL = 0.55
_rl_lock = asyncio.Lock()
_last_call_at = 0.0


async def _pace() -> None:
    """Block until at least _MIN_INTERVAL has passed since the last request."""
    global _last_call_at
    async with _rl_lock:
        loop = asyncio.get_event_loop()
        wait = _MIN_INTERVAL - (loop.time() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = asyncio.get_event_loop().time()


class SpaceTradersError(Exception):
    """An API error envelope, already formatted for the agent to read."""


# Token set from the plugin config (console: System → Settings → SpaceTraders),
# seeded by the plugin's register() from secrets.yaml at graph build.
_CONFIG_TOKEN: str | None = None
_CONFIG_ACCOUNT_TOKEN: str | None = None


def set_config_token(token: str | None, account_token: str | None = None) -> None:
    global _CONFIG_TOKEN, _CONFIG_ACCOUNT_TOKEN
    _CONFIG_TOKEN = (token or "").strip() or None
    if account_token is not None:
        _CONFIG_ACCOUNT_TOKEN = (account_token or "").strip() or None


def account_token() -> str | None:
    """The account token used to register a new agent (config or env)."""
    return _CONFIG_ACCOUNT_TOKEN or (os.environ.get("SPACETRADERS_ACCOUNT_TOKEN", "").strip() or None)


def _secrets_yaml_path() -> Path:
    """The THIS-AGENT-scoped ``secrets.yaml`` — the exact file the server seeds the
    token from at graph build (``register()`` → plugin config). Resolving it is what
    makes a save survive a reload AND be visible to this agent's own views: a
    cwd-relative ``config/secrets.yaml`` only happens to be right for the HOST (cwd =
    repo root); a fleet/workspace agent's scoped secrets live at ``<workspace>/secrets.yaml``,
    so the old write stranded the token in the host's config and the agent re-seeded
    from its own (empty) secrets on reload → "no token set"."""
    try:
        # The host's resolved, instance-scoped path (graph/config_io). This is the
        # one the plugin-config layer reads `spacetraders.token` back from.
        from graph.config_io import SECRETS_YAML_PATH  # type: ignore

        return Path(SECRETS_YAML_PATH)
    except Exception:  # noqa: BLE001 — running outside the host (tests / fresh_start)
        cfg = os.environ.get("PROTOAGENT_CONFIG_DIR")
        return (Path(cfg) if cfg else Path("config")) / "secrets.yaml"


def _read_secrets_token() -> str | None:
    """Read ``spacetraders.token`` straight from the scoped secrets.yaml — the
    same file ``save_token`` writes, so reads resolve even if the in-memory seed
    (``register()``) hasn't run in this process."""
    try:
        import yaml

        path = _secrets_yaml_path()
        data = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
        tok = ((data.get("spacetraders") or {}).get("token") or "").strip()
        return tok or None
    except Exception:  # noqa: BLE001
        return None


def load_token() -> str | None:
    """Resolve the agent token: env → in-process config → scoped secrets.yaml → legacy file."""
    tok = os.environ.get("SPACETRADERS_TOKEN", "").strip()
    if tok:
        return tok
    if _CONFIG_TOKEN:
        return _CONFIG_TOKEN
    tok = _read_secrets_token()
    if tok:
        return tok
    try:
        tok = _TOKEN_FILE.read_text().strip()
        return tok or None
    except OSError:
        return None


def save_token(token: str) -> None:
    """Persist the active agent token to THIS AGENT's scoped ``secrets.yaml`` (the exact
    file the server seeds from on reload) AND apply it live in this process
    (``_CONFIG_TOKEN``) — so a fresh-start or agent swap takes effect immediately, with
    one consistent store that's also visible to this agent's fleet/dashboard views.
    Falls back to the legacy token file only if secrets.yaml can't be written."""
    global _CONFIG_TOKEN
    token = token.strip()
    _CONFIG_TOKEN = token  # live: this process switches agent right away
    try:
        import yaml

        path = _secrets_yaml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
        data.setdefault("spacetraders", {})["token"] = token
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
        try:
            path.chmod(0o600)  # secrets file — owner-only (matches the host's writer)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 — never lose the token
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token + "\n")


async def call(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json: dict | None = None,
    params: dict | None = None,
    auth: bool = True,
) -> dict:
    """Make one API call and return the parsed ``data`` payload (dict or list).

    Retries on 429 (rate limit) honoring ``Retry-After``. Raises
    ``SpaceTradersError`` with a readable message on any 4xx/5xx error envelope.
    """
    if auth:
        token = token or load_token()
        if not token:
            raise SpaceTradersError(
                "no SpaceTraders token — set SPACETRADERS_TOKEN or write "
                "config/spacetraders.token. Register an agent first (needs an "
                "account token from your spacetraders.io account)."
            )
    headers = {"Authorization": f"Bearer {token}"} if (auth and token) else {}
    if json is not None:
        headers["Content-Type"] = "application/json"
    url = f"{BASE_URL}{path}"

    async with httpx.AsyncClient(timeout=30.0) as http:
        for attempt in range(4):
            await _pace()
            try:
                resp = await http.request(method, url, headers=headers, json=json, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                # Transient network blip (ConnectError / ReadTimeout / etc.) — back off and
                # retry instead of letting it bubble up and drop the engine's ops loop (the
                # 'Autopilot errored (ConnectTimeout)' failures). Engine hardening, pt 2.
                if attempt < 3:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise SpaceTradersError(f"network error after retries: {type(e).__name__}") from e
            if resp.status_code == 429 and attempt < 3:
                await asyncio.sleep(float(resp.headers.get("Retry-After", "1")) + 0.2)
                continue
            try:
                body = resp.json()
            except ValueError:
                raise SpaceTradersError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.is_success:
                return body.get("data", body)
            err = body.get("error", {})
            code = err.get("code", resp.status_code)
            msg = err.get("message", "unknown error")
            raise SpaceTradersError(f"[{code}] {msg}")
    raise SpaceTradersError("rate limited — gave up after retries")
