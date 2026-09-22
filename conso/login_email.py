"""Full-CLI email login via captcha-solver.

Supabase's ``token?grant_type=password`` and ``otp`` endpoints on this
project require an hCaptcha token (see the empirical probe: ``captcha_failed
(no captcha_token found)``). This module drives waguriagentic/captcha-solver
to mint a replayable token, then completes the Supabase sign-in.

Prereq: a running captcha-solver sidecar reachable at
``$CAPTCHA_SOLVER_URL`` (default ``http://127.0.0.1:8877``). Its Bearer
token, if any, comes from ``$CAPTCHA_SOLVER_TOKEN``.

Sitekey must be supplied by the caller — Conso does not surface a public
email login UI, so we can't scrape it. Once discovered, cache it in
``$CONSO_HCAPTCHA_SITEKEY``.
"""

from __future__ import annotations

import getpass
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .accounts import AccountStore
from .config import Settings

log = logging.getLogger("conso.login_email")

DEFAULT_SOLVER = "http://127.0.0.1:8877"
DEFAULT_CAPTCHA_URL = "https://www.conso.xyz"


class EmailLoginError(RuntimeError):
    """Email login could not complete."""


@dataclass
class EmailLoginResult:
    label: str
    consoname: str
    email: str
    user_id: str
    access_token: str
    refresh_token: str
    expires_at: int


def _solver_url() -> str:
    return os.environ.get("CAPTCHA_SOLVER_URL", DEFAULT_SOLVER).rstrip("/")


def _solver_token() -> str | None:
    return os.environ.get("CAPTCHA_SOLVER_TOKEN") or None


def _sitekey_from_env() -> str:
    return os.environ.get("CONSO_HCAPTCHA_SITEKEY", "").strip()


def solve_hcaptcha(sitekey: str, *, page_url: str = DEFAULT_CAPTCHA_URL, timeout_s: int = 90) -> str:
    """Ask the local captcha-solver for an hCaptcha token."""
    url = f"{_solver_url()}/solve"
    headers = {"Content-Type": "application/json"}
    token = _solver_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "type": "hcaptcha",
        "sitekey": sitekey,
        "url": page_url,
        "timeout_s": timeout_s,
    }
    try:
        with httpx.Client(timeout=timeout_s + 15) as http:
            resp = http.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise EmailLoginError(
            f"captcha-solver unreachable at {url}: {exc}. Start it with "
            "`systemctl --user start captcha-solver` or override "
            "$CAPTCHA_SOLVER_URL."
        ) from exc

    if resp.status_code != 200:
        raise EmailLoginError(
            f"captcha-solver returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    if not data.get("solved") or not data.get("token"):
        raise EmailLoginError(
            f"captcha-solver did not mint a token: {data.get('error') or data}"
        )
    return str(data["token"])


def _supabase_password_signin(
    settings: Settings, *, email: str, password: str, captcha_token: str
) -> dict[str, Any]:
    """POST /auth/v1/token?grant_type=password with a captcha token."""
    url = f"{settings.supabase_url}/auth/v1/token"
    headers = {
        "apikey": settings.supabase_key,
        "Content-Type": "application/json",
    }
    body = {
        "email": email,
        "password": password,
        "gotrue_meta_security": {"captcha_token": captcha_token},
    }
    with httpx.Client(timeout=settings.request_timeout) as http:
        resp = http.post(url, params={"grant_type": "password"}, headers=headers, json=body)
    if resp.status_code != 200:
        raise EmailLoginError(
            f"Supabase password sign-in failed: HTTP {resp.status_code} "
            f"{resp.text[:400]}"
        )
    return resp.json()


def _supabase_send_otp(
    settings: Settings, *, email: str, captcha_token: str, create_user: bool = False
) -> None:
    """POST /auth/v1/otp — email magic-link + one-time code."""
    url = f"{settings.supabase_url}/auth/v1/otp"
    headers = {
        "apikey": settings.supabase_key,
        "Content-Type": "application/json",
    }
    body = {
        "email": email,
        "create_user": create_user,
        "gotrue_meta_security": {"captcha_token": captcha_token},
    }
    with httpx.Client(timeout=settings.request_timeout) as http:
        resp = http.post(url, headers=headers, json=body)
    if resp.status_code not in (200, 201):
        raise EmailLoginError(
            f"Supabase OTP request failed: HTTP {resp.status_code} "
            f"{resp.text[:400]}"
        )


def _supabase_verify_otp(
    settings: Settings, *, email: str, code: str
) -> dict[str, Any]:
    """POST /auth/v1/verify — exchange a 6-digit code for a session."""
    url = f"{settings.supabase_url}/auth/v1/verify"
    headers = {
        "apikey": settings.supabase_key,
        "Content-Type": "application/json",
    }
    body = {"type": "email", "email": email, "token": code}
    with httpx.Client(timeout=settings.request_timeout) as http:
        resp = http.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        raise EmailLoginError(
            f"Supabase OTP verify failed: HTTP {resp.status_code} "
            f"{resp.text[:400]}"
        )
    return resp.json()


def _persist_from_session(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    session: dict[str, Any],
) -> EmailLoginResult:
    """Common tail: verify user, resolve consoname, upsert account row."""
    access = str(session.get("access_token") or "")
    refresh = str(session.get("refresh_token") or "")
    if not access or not refresh:
        raise EmailLoginError("session missing access_token/refresh_token")
    expires_in = int(session.get("expires_in") or 0)
    expires_at = int(session.get("expires_at") or (int(time.time()) + expires_in))
    user = session.get("user") or {}
    email = str(user.get("email") or "")
    user_id = str(user.get("id") or "")

    # consoname
    consoname_url = f"{settings.supabase_url}/rest/v1/consousers"
    with httpx.Client(timeout=settings.request_timeout) as http:
        resp = http.get(
            consoname_url,
            params={"select": "consoname"},
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {access}",
            },
        )
    consoname = ""
    if resp.status_code == 200:
        rows = resp.json()
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            consoname = str(rows[0].get("consoname") or "")

    store.upsert(
        label=label, access_token=access, refresh_token=refresh,
        enabled=True, note=email,
    )
    store.update_profile(label, account_id=user_id, consoname=consoname)
    if expires_at:
        store.update_tokens(
            label, access_token=access, refresh_token=refresh, expires_at=expires_at,
        )
    return EmailLoginResult(
        label=label, consoname=consoname, email=email, user_id=user_id,
        access_token=access, refresh_token=refresh, expires_at=expires_at,
    )


def login_via_email_password(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    email: str,
    password: str | None = None,
    sitekey: str = "",
    captcha_url: str = DEFAULT_CAPTCHA_URL,
) -> EmailLoginResult:
    """Full email + password flow. Password prompt is interactive if omitted."""
    sitekey = sitekey or _sitekey_from_env()
    if not sitekey:
        raise EmailLoginError(
            "hCaptcha sitekey not supplied. Pass --sitekey <uuid> or set "
            "$CONSO_HCAPTCHA_SITEKEY."
        )
    if password is None:
        password = getpass.getpass(f"password for {email}: ").strip()
    if not password:
        raise EmailLoginError("password is required")
    log.info("solving hCaptcha (sitekey=%s)", sitekey)
    token = solve_hcaptcha(sitekey, page_url=captcha_url)
    log.info("captcha solved; length=%d", len(token))
    session = _supabase_password_signin(
        settings, email=email, password=password, captcha_token=token,
    )
    return _persist_from_session(settings, store, label=label, session=session)


def login_via_email_otp(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    email: str,
    code: str | None = None,
    sitekey: str = "",
    captcha_url: str = DEFAULT_CAPTCHA_URL,
    create_user: bool = False,
) -> EmailLoginResult:
    """Email magic-link OTP flow. Prompts for the 6-digit code if omitted."""
    sitekey = sitekey or _sitekey_from_env()
    if not sitekey:
        raise EmailLoginError(
            "hCaptcha sitekey not supplied. Pass --sitekey <uuid> or set "
            "$CONSO_HCAPTCHA_SITEKEY."
        )
    log.info("solving hCaptcha (sitekey=%s)", sitekey)
    token = solve_hcaptcha(sitekey, page_url=captcha_url)
    _supabase_send_otp(settings, email=email, captcha_token=token, create_user=create_user)
    log.info("OTP email dispatched to %s", email)
    if code is None:
        code = input(f"paste the 6-digit code sent to {email}: ").strip()
    if not code:
        raise EmailLoginError("OTP code is required")
    session = _supabase_verify_otp(settings, email=email, code=code)
    return _persist_from_session(settings, store, label=label, session=session)


__all__ = [
    "EmailLoginError",
    "EmailLoginResult",
    "login_via_email_password",
    "login_via_email_otp",
    "solve_hcaptcha",
]
