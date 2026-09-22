"""Async Supabase client for Conso.

One :class:`ConsoClient` per account. Auth is delegated to
:class:`AuthSession` (proactive refresh + persisted rotation); this layer owns
RPC transport, retries, and ban detection.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .accounts import Account, AccountStore
from .auth import AuthError, AuthSession
from .config import Settings

log = logging.getLogger("conso.client")


class ConsoError(RuntimeError):
    """Base error for Conso backend failures."""


class AccountBanned(ConsoError):
    """The account is banned server-side."""


class AuthExpired(AuthError, ConsoError):
    """Access token missing/expired and refresh could not recover it."""


@dataclass
class TurnResult:
    """Outcome of a single ``append_prompt`` call."""

    ok: bool
    credited_zaps: float = 0.0
    error: str | None = None
    banned: bool = False


class ConsoClient:
    """One client per account.

    Concurrency is bounded internally per client. Multi-account concurrency is
    handled by the runner spinning multiple clients in parallel.
    """

    def __init__(
        self,
        settings: Settings,
        store: AccountStore,
        account: Account,
        *,
        max_concurrency: int = 4,
    ) -> None:
        self._settings = settings
        self._store = store
        self._account = account
        self._label = account.label
        self._sem = asyncio.Semaphore(max_concurrency)
        self._banned = False
        self._http: httpx.AsyncClient | None = None
        self._auth: AuthSession | None = None

    @property
    def label(self) -> str:
        return self._label

    @property
    def banned(self) -> bool:
        return self._banned

    @property
    def auth(self) -> AuthSession:
        if self._auth is None:
            raise ConsoError("client not entered")
        return self._auth

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "ConsoClient":
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.request_timeout),
            headers={"User-Agent": "Mozilla/5.0 (compatible; conso-cli/0.2)"},
            follow_redirects=True,
        )
        self._auth = AuthSession(
            settings=self._settings,
            store=self._store,
            account=self._account,
            http=self._http,
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            self._auth = None

    # -- session -----------------------------------------------------------
    async def verify_session(self) -> dict[str, Any] | None:
        """Return the current user object; refresh once on 401/403."""
        if self._http is None or self._auth is None:
            raise ConsoError("client not entered")
        await self._auth.ensure_fresh()
        url = f"{self._settings.supabase_url}/auth/v1/user"
        resp = await self._http.get(url, headers=self._auth.base_headers())
        if resp.status_code in (401, 403):
            if await self._auth.refresh():
                resp = await self._http.get(url, headers=self._auth.base_headers())
        if resp.status_code != 200:
            log.warning(
                "[%s] verify failed: HTTP %s %s",
                self._label,
                resp.status_code,
                resp.text[:200],
            )
            return None
        return resp.json()

    async def get_profile(self) -> dict[str, Any] | None:
        if self._http is None or self._auth is None:
            raise ConsoError("client not entered")
        await self._auth.ensure_fresh()
        url = f"{self._settings.supabase_url}/rest/v1/consousers"
        headers = {**self._auth.base_headers()}
        params = {"select": "*"}
        resp = await self._http.get(url, headers=headers, params=params)
        if resp.status_code in (401, 403) and await self._auth.refresh():
            resp = await self._http.get(
                url, headers=self._auth.base_headers(), params=params
            )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        return rows[0] if isinstance(rows, list) and rows else None

    async def list_missions(self) -> list[dict[str, Any]]:
        if self._http is None or self._auth is None:
            raise ConsoError("client not entered")
        await self._auth.ensure_fresh()
        url = f"{self._settings.supabase_url}/rest/v1/bonus_missions"
        params = {"select": "*", "active": "eq.true", "order": "created_at.asc"}
        resp = await self._http.get(
            url, headers=self._auth.base_headers(), params=params
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []

    # -- RPC ---------------------------------------------------------------
    async def _rpc(
        self, name: str, payload: dict[str, Any], *, retries: int | None = None
    ) -> Any:
        if self._banned:
            raise AccountBanned("account is banned")
        if self._http is None or self._auth is None:
            raise ConsoError("client not entered")

        attempts = retries if retries is not None else self._settings.max_retries
        url = f"{self._settings.supabase_url}/rest/v1/rpc/{name}"
        last_error: str | None = None

        async with self._sem:
            for attempt in range(attempts):
                await self._auth.ensure_fresh()
                try:
                    resp = await self._http.post(
                        url, headers=self._auth.base_headers(), json=payload
                    )
                except httpx.HTTPError as exc:
                    last_error = f"transport: {exc}"
                    await self._backoff(attempt)
                    continue

                if resp.status_code in (200, 201):
                    text = resp.text.strip()
                    if not text:
                        return None
                    try:
                        return resp.json()
                    except ValueError:
                        return text

                if resp.status_code == 401 or (
                    resp.status_code == 403 and "bad_jwt" in resp.text
                ):
                    if await self._auth.refresh():
                        continue
                    raise AuthExpired("access token expired and refresh failed")

                message = _error_message(resp)
                if "account_banned" in message:
                    self._banned = True
                    raise AccountBanned(message)
                last_error = f"HTTP {resp.status_code}: {message}"
                if resp.status_code in (429, 500, 502, 503, 504):
                    await self._backoff(attempt)
                    continue
                break

        raise ConsoError(last_error or f"{name} failed")

    async def _backoff(self, attempt: int) -> None:
        base = max(1.0, self._settings.gap_min * 0.1)
        delay = base * (2**attempt) + random.uniform(0, base * 0.5)
        await asyncio.sleep(delay)

    # -- high-level -------------------------------------------------------
    async def append_prompt(
        self, entry: dict[str, Any], base_zaps: float, spend_usd: float
    ) -> TurnResult:
        try:
            credited = await self._rpc(
                "append_prompt",
                {
                    "p_entry": entry,
                    "p_base_zaps": base_zaps,
                    "p_spend_usd": spend_usd,
                },
            )
        except AccountBanned as exc:
            return TurnResult(ok=False, error=str(exc), banned=True)
        except ConsoError as exc:
            return TurnResult(ok=False, error=str(exc))
        if isinstance(credited, (int, float)):
            return TurnResult(ok=True, credited_zaps=float(credited))
        return TurnResult(ok=True, credited_zaps=0.0)

    async def get_todays_claims(self) -> list[str]:
        try:
            data = await self._rpc("get_todays_mission_claims", {})
        except ConsoError as exc:
            log.warning("[%s] todays claims failed: %s", self._label, exc)
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    async def claim_bonus_mission(self, mission_id: str) -> tuple[bool, str | None]:
        try:
            await self._rpc("claim_bonus_mission", {"p_mission_id": mission_id})
            return True, None
        except AccountBanned as exc:
            return False, str(exc)
        except ConsoError as exc:
            return False, str(exc)

    async def claim_daily_mission(
        self, mission_id: str, claim_ref: str | None = None
    ) -> tuple[bool, str | None]:
        try:
            await self._rpc(
                "claim_daily_mission",
                {"p_mission_id": mission_id, "p_claim_ref": claim_ref},
            )
            return True, None
        except AccountBanned as exc:
            return False, str(exc)
        except ConsoError as exc:
            return False, str(exc)


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)
    return str(body)[:300]


def new_entry(
    model: str,
    platform: str,
    input_tokens: int,
    output_tokens: int,
    *,
    input_files: int = 0,
    output_files: int = 0,
    prompt_quality: float = 1.0,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build the ``p_entry`` object exactly as the extension does."""
    return {
        "model": model,
        "platform": platform,
        "timestamp": timestamp
        or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "inputTokens": int(input_tokens),
        "outputTokens": int(output_tokens),
        "inputFilesCount": int(input_files),
        "outputFilesCount": int(output_files),
        "promptQuality": float(prompt_quality),
    }
