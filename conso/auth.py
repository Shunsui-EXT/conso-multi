"""Auth session with proactive refresh.

The Supabase access token is an ES256 JWT with a ~1 h TTL. Reactively
refreshing on 401/403 works but wastes a request, so this session decodes the
JWT's ``exp`` claim once and refreshes proactively when the token is within
``refresh_leeway`` seconds of expiring. A reactive refresh remains as a
fallback for the case where the token was revoked before ``exp``.

Refreshed credentials are persisted to :class:`AccountStore` so an operator
never has to paste a new access token by hand.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx

from .accounts import Account, AccountStore
from .config import Settings

log = logging.getLogger("conso.auth")


class AuthError(RuntimeError):
    """Auth broke and refresh could not recover."""


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def jwt_expiry(token: str) -> int:
    """Return the ``exp`` claim (unix seconds) or 0 if it cannot be parsed."""
    if not token or token.count(".") != 2:
        return 0
    try:
        _header, payload, _sig = token.split(".", 2)
        data = json.loads(_b64url_decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


class AuthSession:
    """Owns the access/refresh tokens for one account.

    Every RPC goes through :meth:`ensure_fresh` first; concurrent callers are
    serialised behind an :class:`asyncio.Lock` so the refresh endpoint is only
    hit once per rotation.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        store: AccountStore,
        account: Account,
        http: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._store = store
        self._label = account.label
        self._access_token = account.access_token
        self._refresh_token = account.refresh_token
        self._expires_at = account.expires_at or jwt_expiry(account.access_token)
        self._http = http
        self._lock = asyncio.Lock()

    @property
    def label(self) -> str:
        return self._label

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def expires_at(self) -> int:
        return self._expires_at

    def seconds_until_expiry(self) -> float:
        if not self._expires_at:
            return 0.0
        return max(0.0, self._expires_at - time.time())

    def base_headers(self, *, with_auth: bool = True) -> dict[str, str]:
        headers = {
            "apikey": self._settings.supabase_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if with_auth and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    # -- refresh ----------------------------------------------------------
    async def ensure_fresh(self) -> None:
        """Refresh proactively when within ``refresh_leeway`` of ``exp``."""
        if not self._expires_at:
            # Unknown expiry — decode once, then decide.
            self._expires_at = jwt_expiry(self._access_token)
            if not self._expires_at:
                return
        if self.seconds_until_expiry() > self._settings.refresh_leeway:
            return
        await self.refresh()

    async def refresh(self) -> bool:
        """Rotate the access token; serialised so parallel calls share one hit."""
        if not self._refresh_token:
            return False
        async with self._lock:
            # Another coroutine may have refreshed while we waited.
            if self.seconds_until_expiry() > self._settings.refresh_leeway:
                return True
            url = f"{self._settings.supabase_url}/auth/v1/token"
            try:
                resp = await self._http.post(
                    url,
                    params={"grant_type": "refresh_token"},
                    headers=self.base_headers(with_auth=False),
                    json={"refresh_token": self._refresh_token},
                )
            except httpx.HTTPError as exc:
                log.warning("[%s] refresh transport error: %s", self._label, exc)
                return False
            if resp.status_code != 200:
                log.warning(
                    "[%s] refresh failed: HTTP %s %s",
                    self._label,
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            data: dict[str, Any] = resp.json()
            new_access = str(data.get("access_token") or "")
            if not new_access:
                return False
            new_refresh = str(data.get("refresh_token") or "") or self._refresh_token
            expires_in = int(data.get("expires_in") or 0)
            self._access_token = new_access
            self._refresh_token = new_refresh
            self._expires_at = (
                int(time.time()) + expires_in if expires_in else jwt_expiry(new_access)
            )
            self._store.update_tokens(
                self._label,
                access_token=self._access_token,
                refresh_token=self._refresh_token,
                expires_at=self._expires_at,
            )
            log.info(
                "[%s] refreshed session (expires in %ss)",
                self._label,
                max(0, self._expires_at - int(time.time())),
            )
            return True
