"""Full-CLI login via Playwright + the real Conso extension.

The Conso extension owns the Google OAuth `client_id` and hardcodes the
`chrome.identity.getRedirectURL()` redirect. Google will not issue an
`id_token` for any other client/redirect. The only fully-headless path is
therefore to reproduce the extension's exact environment: launch Chromium
with the extension loaded, let the operator sign in once, then read the
Supabase session out of the extension's `chrome.storage.local` and hand it
to :class:`AccountStore`.

Runtime cost: one `playwright` install (optional dep) and one Chromium
download (`python3 -m playwright install chromium`). After that every login
is one CLI command.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounts import AccountStore
from .config import Settings

log = logging.getLogger("conso.login_browser")

SESSION_KEY = "sb-jzxlayjrsdbyzykuiqns-auth-token"
DEFAULT_EXTENSION = Path("/home/elzanom/work/airdrop/Conso/Extension")


class BrowserLoginError(RuntimeError):
    """Login flow could not complete."""


@dataclass
class BrowserLoginResult:
    label: str
    consoname: str
    email: str
    user_id: str
    access_token: str
    refresh_token: str
    expires_at: int


def _import_playwright():
    """Import lazily; keep playwright an optional dep."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - clear error path
        raise BrowserLoginError(
            "playwright is not installed. Run:\n"
            "  pip install -r requirements-login.txt && "
            "python3 -m playwright install chromium"
        ) from exc
    return sync_playwright


def _find_extension(explicit: Path | None) -> Path:
    """Locate the Conso extension folder."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            DEFAULT_EXTENSION,
            Path.cwd() / "Extension",
            Path.cwd().parent / "Extension",
        ]
    )
    for path in candidates:
        if path.is_dir() and (path / "manifest.json").exists():
            return path
    raise BrowserLoginError(
        "Conso extension folder not found. Point --extension at the "
        "unpacked extension directory (with manifest.json)."
    )


def _read_session_from_sw(sw, timeout: float) -> dict[str, Any] | None:
    """Poll the extension's service worker for the persisted Supabase session."""
    deadline = time.time() + timeout
    poll = f"""
    async () => {{
        const key = {SESSION_KEY!r};
        const rec = await chrome.storage.local.get(key);
        const raw = rec[key];
        if (!raw) return null;
        try {{ return typeof raw === 'string' ? JSON.parse(raw) : raw; }}
        catch (e) {{ return {{ __parse_error: String(e), raw: String(raw).slice(0, 400) }}; }}
    }}
    """
    while time.time() < deadline:
        try:
            value = sw.evaluate(poll)
        except Exception as exc:  # noqa: BLE001
            log.debug("service worker evaluate failed: %s", exc)
            value = None
        if isinstance(value, dict) and value.get("access_token"):
            return value
        if isinstance(value, dict) and value.get("__parse_error"):
            raise BrowserLoginError(
                f"extension session unreadable: {value['__parse_error']}"
            )
        time.sleep(1.0)
    return None


def _verify_session_sync(settings: Settings, access_token: str) -> dict[str, Any] | None:
    """Synchronous ``/auth/v1/user`` probe using the httpx sync client."""
    import httpx

    url = f"{settings.supabase_url}/auth/v1/user"
    with httpx.Client(timeout=settings.request_timeout) as http:
        resp = http.get(
            url,
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {access_token}",
            },
        )
    if resp.status_code != 200:
        return None
    return resp.json()


def _fetch_consoname_sync(settings: Settings, access_token: str) -> str:
    import httpx

    url = f"{settings.supabase_url}/rest/v1/consousers"
    with httpx.Client(timeout=settings.request_timeout) as http:
        resp = http.get(
            url,
            params={"select": "consoname"},
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {access_token}",
            },
        )
    if resp.status_code != 200:
        return ""
    rows = resp.json()
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("consoname") or "")
    return ""


def _persist_account(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    session: dict[str, Any],
) -> BrowserLoginResult:
    access = str(session.get("access_token") or "")
    refresh = str(session.get("refresh_token") or "")
    if not access or not refresh:
        raise BrowserLoginError("session missing access_token/refresh_token")
    user = _verify_session_sync(settings, access)
    if not user:
        raise BrowserLoginError("Supabase rejected the captured access_token")
    consoname = _fetch_consoname_sync(settings, access)
    expires_at = int(session.get("expires_at") or 0)

    store.upsert(
        label=label,
        access_token=access,
        refresh_token=refresh,
        enabled=True,
        note=str(user.get("email") or ""),
    )
    store.update_profile(
        label,
        account_id=str(user.get("id") or ""),
        consoname=consoname,
    )
    if expires_at:
        store.update_tokens(
            label,
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
        )
    return BrowserLoginResult(
        label=label,
        consoname=consoname,
        email=str(user.get("email") or ""),
        user_id=str(user.get("id") or ""),
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
    )


def login_via_browser(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    extension_dir: Path | None = None,
    user_data_dir: Path | None = None,
    keep_profile: bool = False,
    timeout: float = 600.0,
) -> BrowserLoginResult:
    """Launch Chromium with the extension, wait for sign-in, capture session.

    Steps:

    1. Launch a persistent Chromium context with `--load-extension=<ext>`.
    2. Open a page that instructs the operator to click the CONSO toolbar
       icon and sign in with Google (the extension owns the OAuth flow).
    3. Poll the extension's service worker for
       ``chrome.storage.local["sb-jzxlayjrsdbyzykuiqns-auth-token"]`` until
       a Supabase session with ``access_token`` appears or ``timeout`` fires.
    4. Verify the token against Supabase, resolve consoname, persist via
       :class:`AccountStore`.

    ``keep_profile=True`` keeps the Chromium user-data dir so a repeat login
    for the same OS account skips the Google consent screen. Default is
    False — the profile is created in a temp dir and deleted at close.
    """
    sync_playwright = _import_playwright()
    ext_path = _find_extension(extension_dir)
    log.info("using extension folder: %s", ext_path)

    profile_owned = user_data_dir is None
    if user_data_dir is None:
        user_data_dir = Path(tempfile.mkdtemp(prefix="conso-login-"))
    user_data_dir.mkdir(parents=True, exist_ok=True)

    launch_args = [
        f"--disable-extensions-except={ext_path}",
        f"--load-extension={ext_path}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    print(f"[login] launching Chromium (profile: {user_data_dir})")
    print(f"[login] extension: {ext_path}")
    print(
        "[login] Click the CONSO toolbar icon → 'Sign in with Google'. "
        "Waiting for a Supabase session to appear…"
    )

    session_captured: dict[str, Any] | None = None
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                args=launch_args,
                viewport={"width": 1200, "height": 800},
            )
            try:
                # Find the extension's service worker; it registers on load.
                sw_deadline = time.time() + 30
                sw = None
                while time.time() < sw_deadline and sw is None:
                    for candidate in ctx.service_workers:
                        if candidate.url.endswith("/background.js"):
                            sw = candidate
                            break
                    if sw is None:
                        time.sleep(0.5)
                if sw is None:
                    raise BrowserLoginError(
                        "extension service worker never registered"
                    )
                log.info("service worker ready: %s", sw.url)

                # Show a helper page so the operator has instructions.
                if ctx.pages:
                    page = ctx.pages[0]
                else:
                    page = ctx.new_page()
                page.set_content(
                    """
                    <html><head><title>Conso login helper</title>
                    <style>
                    body{font:16px system-ui, sans-serif; background:#111; color:#eee;
                         max-width:640px; margin:60px auto; padding:0 20px;}
                    h1{color:#6ec5ff;}
                    ol li{margin:6px 0;}
                    code{background:#222; padding:2px 5px; border-radius:4px;}
                    </style></head><body>
                    <h1>Conso — full-CLI login</h1>
                    <ol>
                      <li>Click the <b>CONSO</b> puzzle-piece icon in the toolbar
                          (or open <code>chrome://extensions</code> and pin it).</li>
                      <li>Click <b>Sign in with Google</b>. Complete the OAuth
                          flow in the popup that appears.</li>
                      <li>Once signed in, this window may close automatically —
                          the CLI captures the session from the extension.</li>
                    </ol>
                    </body></html>
                    """
                )
                session_captured = _read_session_from_sw(sw, timeout=timeout)
            finally:
                ctx.close()
    finally:
        if profile_owned and not keep_profile:
            shutil.rmtree(user_data_dir, ignore_errors=True)

    if session_captured is None:
        raise BrowserLoginError(
            "Timed out waiting for a Supabase session. Sign in inside the "
            "Chromium window before the timeout expires."
        )
    return _persist_account(settings, store, label=label, session=session_captured)


__all__ = ["BrowserLoginError", "BrowserLoginResult", "login_via_browser"]
