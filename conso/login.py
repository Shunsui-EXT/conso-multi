"""Login pipeline reversed from the Conso extension.

The extension uses Google OAuth 2.0 id-token implicit flow (client_id is an
extension client — `redirect_uri` is hard-locked to
`chrome.identity.getRedirectURL()`), then feeds the id-token into
Supabase's ``signInWithIdToken`` RPC to obtain the persistent session.

Two integrations here:

1. :func:`supabase_signin_with_id_token` — port of the Supabase call. Anyone
   holding a fresh Google ID token minted for the extension client can pass
   it in and receive the same ``{access_token, refresh_token, expires_at}``
   the extension would.

2. :func:`serve_paste_login` — a one-shot local HTTP server that walks a
   human through pasting the extension's persisted session JSON (or a raw
   ``Authorization`` bearer plus refresh token) and stores it as an account.
   No third-party dependency, no browser automation.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .accounts import AccountStore
from .config import Settings

log = logging.getLogger("conso.login")

# The extension's Google OAuth client id — reverse-engineered from
# background.js.
GOOGLE_CLIENT_ID = "77519766304-hfrhb5gc5dp1kqano1ghsgroa2rf3ei1.apps.googleusercontent.com"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


@dataclass
class SignInResult:
    access_token: str
    refresh_token: str
    expires_in: int
    expires_at: int
    user_id: str
    email: str


# --- Supabase signInWithIdToken port -------------------------------------
async def supabase_signin_with_id_token(
    settings: Settings, *, id_token: str, nonce: str
) -> SignInResult:
    """POST /auth/v1/token?grant_type=id_token — Supabase's ID-token grant.

    ``nonce`` MUST be the RAW value (not the SHA-256 hash sent to Google).
    """
    url = f"{settings.supabase_url}/auth/v1/token"
    body = {"provider": "google", "id_token": id_token, "nonce": nonce}
    headers = {
        "apikey": settings.supabase_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
        resp = await http.post(
            url, params={"grant_type": "id_token"}, headers=headers, json=body
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Supabase signInWithIdToken failed: HTTP {resp.status_code} "
            f"{resp.text[:400]}"
        )
    data: dict[str, Any] = resp.json()
    access = str(data.get("access_token") or "")
    refresh = str(data.get("refresh_token") or "")
    if not access or not refresh:
        raise RuntimeError(f"missing tokens in response: {list(data)}")
    expires_in = int(data.get("expires_in") or 0)
    expires_at = int(data.get("expires_at") or (int(time.time()) + expires_in))
    user = data.get("user") or {}
    return SignInResult(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        expires_at=expires_at,
        user_id=str(user.get("id") or ""),
        email=str(user.get("email") or ""),
    )


# --- Paste-based login server --------------------------------------------
_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Conso login helper</title>
<style>
 body { font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
        margin: 40px auto; color: #eee; background: #111; padding: 0 16px; }
 h1 { color: #6ec5ff; margin-bottom: 4px; }
 p { line-height: 1.5; }
 code, pre { background: #222; color: #b6f2b6; padding: 2px 6px; border-radius: 4px; }
 pre { padding: 12px; overflow: auto; }
 textarea { width: 100%; height: 220px; background: #1b1b1b; color: #eee;
            border: 1px solid #444; border-radius: 6px; padding: 10px;
            font: 13px/1.4 monospace; }
 input[type=text] { width: 100%; padding: 10px; border-radius: 6px;
                    border: 1px solid #444; background: #1b1b1b; color: #eee;
                    font: 14px monospace; box-sizing: border-box; }
 button { background: #2c74dc; color: #fff; padding: 10px 18px;
          border: 0; border-radius: 6px; cursor: pointer; margin-top: 12px; }
 button:hover { background: #3a86ff; }
 .row { margin: 16px 0; }
 .ok { color: #7ee787; }
 .bad { color: #ff8080; }
 .dim { color: #888; }
 .card { background: #191919; padding: 16px 20px; border-radius: 10px;
         border: 1px solid #303030; margin: 20px 0; }
</style>
</head>
<body>
<h1>Conso login helper</h1>
<p class="dim">This page is served locally by the automation. Paste the extension's
Supabase session below.</p>

<div class="card">
<p><b>Label</b> — a short identifier for this account (e.g. <code>primary</code>).</p>
<div class="row"><input id="label" type="text" placeholder="primary" value="__LABEL__" /></div>

<p><b>Paste one of the following:</b></p>
<ol>
  <li>The full <code>chrome.storage.local["sb-jzxlayjrsdbyzykuiqns-auth-token"]</code> JSON, or</li>
  <li>A JSON like <code>{"access_token":"…","refresh_token":"…"}</code>.</li>
</ol>
<div class="row"><textarea id="session" placeholder='{"access_token":"eyJhbG…","refresh_token":"…","expires_at":…}'></textarea></div>
<button id="submit">Save account</button>
<div id="status" class="row"></div>
</div>

<div class="card">
<p><b>How to grab the session</b></p>
<ol>
  <li>Open <code>chrome://extensions</code>, enable Developer mode, click
      the <b>service worker</b> link on the Conso extension.</li>
  <li>In the opened DevTools console, run:
      <pre>chrome.storage.local.get("sb-jzxlayjrsdbyzykuiqns-auth-token", console.log)</pre></li>
  <li>Copy the JSON value into the box above.</li>
</ol>
<p class="dim">Alternative: open <code>https://www.conso.xyz</code>,
DevTools → Application → Local Storage → same key.</p>
</div>

<script>
async function submit() {
  const label = document.getElementById('label').value.trim();
  const session = document.getElementById('session').value.trim();
  const status = document.getElementById('status');
  status.className = 'row dim';
  status.textContent = 'verifying…';
  const resp = await fetch('/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, session }),
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.ok && data.ok) {
    status.className = 'row ok';
    status.textContent = 'saved: ' + (data.consoname || data.label || label) +
                         ' — you can close this tab.';
  } else {
    status.className = 'row bad';
    status.textContent = 'error: ' + (data.error || resp.statusText);
  }
}
document.getElementById('submit').addEventListener('click', submit);
</script>
</body>
</html>
"""


def _parse_session_blob(raw: str) -> dict[str, Any]:
    """Accept both the raw ``chrome.storage.local`` shape and a flat one.

    The extension stores the session as ``{ "currentSession": {...}, ... }`` on
    older versions and as a plain session dict on newer ones.
    """
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("session must be a JSON object")
    # Some Supabase versions nest under `currentSession`.
    if "currentSession" in obj and isinstance(obj["currentSession"], dict):
        obj = obj["currentSession"]
    if "access_token" not in obj or "refresh_token" not in obj:
        raise ValueError(
            "session must contain access_token and refresh_token"
        )
    return obj


async def _verify_session(settings: Settings, access_token: str) -> dict[str, Any] | None:
    """Fetch the current user with the pasted access token."""
    url = f"{settings.supabase_url}/auth/v1/user"
    async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
        resp = await http.get(
            url,
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {access_token}",
            },
        )
    if resp.status_code != 200:
        return None
    return resp.json()


async def _fetch_consoname(settings: Settings, access_token: str) -> str:
    """Best-effort consoname lookup after sign-in."""
    url = f"{settings.supabase_url}/rest/v1/consousers"
    async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
        resp = await http.get(
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


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LoginHandle:
    """Async handle for a running paste-login server."""

    url: str
    label: str
    done: threading.Event
    _server: http.server.ThreadingHTTPServer
    _thread: threading.Thread
    _saved: dict[str, Any]

    def cancel(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2)

    def wait(self, timeout: float | None = None) -> str | None:
        finished = self.done.wait(timeout=timeout)
        self.cancel()
        if not finished:
            return None
        return self._saved.get("label")

    @property
    def result(self) -> dict[str, Any] | None:
        return dict(self._saved) if self._saved.get("label") else None


def start_paste_login(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
    on_save: "Callable[[dict[str, Any]], None] | None" = None,
) -> LoginHandle:
    """Spawn the paste-login server without blocking.

    ``on_save(payload)`` fires from the server thread when a save succeeds;
    ``payload = {"label", "consoname", "email", "user_id"}``.
    """
    import asyncio

    resolved_port = port or _free_port()
    token = secrets.token_urlsafe(16)
    done = threading.Event()
    saved: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _write(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                q = urllib.parse.parse_qs(parsed.query)
                if q.get("t", [""])[0] != token:
                    self._write(403, "text/plain", b"forbidden")
                    return
                html = _PAGE.replace("__LABEL__", label)
                self._write(200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            self._write(404, "text/plain", b"not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/save":
                self._write(404, "text/plain", b"not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                self._write(400, "application/json", b'{"ok":false,"error":"invalid json"}')
                return
            new_label = str(payload.get("label") or label).strip() or label
            try:
                session = _parse_session_blob(str(payload.get("session") or ""))
            except ValueError as exc:
                self._write(
                    400, "application/json",
                    json.dumps({"ok": False, "error": str(exc)}).encode(),
                )
                return

            loop = asyncio.new_event_loop()
            try:
                user = loop.run_until_complete(
                    _verify_session(settings, session["access_token"])
                )
                if not user:
                    self._write(
                        401, "application/json",
                        b'{"ok":false,"error":"access_token rejected by Supabase"}',
                    )
                    return
                consoname = loop.run_until_complete(
                    _fetch_consoname(settings, session["access_token"])
                )
            finally:
                loop.close()

            store.upsert(
                label=new_label,
                access_token=str(session["access_token"]),
                refresh_token=str(session["refresh_token"]),
                enabled=True,
                note=str(user.get("email") or ""),
            )
            store.update_profile(
                new_label,
                account_id=str(user.get("id") or ""),
                consoname=consoname,
            )
            expires_at = int(session.get("expires_at") or 0)
            if expires_at:
                store.update_tokens(
                    new_label,
                    access_token=str(session["access_token"]),
                    refresh_token=str(session["refresh_token"]),
                    expires_at=expires_at,
                )
            saved.update(
                label=new_label,
                consoname=consoname,
                email=str(user.get("email") or ""),
                user_id=str(user.get("id") or ""),
            )
            self._write(
                200, "application/json",
                json.dumps(
                    {"ok": True, "label": new_label, "consoname": consoname}
                ).encode(),
            )
            if on_save is not None:
                try:
                    on_save(dict(saved))
                except Exception:  # noqa: BLE001
                    log.exception("on_save callback failed")
            done.set()

    server = http.server.ThreadingHTTPServer((host, resolved_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{host}:{resolved_port}/?t={token}"
    log.info("login helper listening on %s", url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            log.warning("could not open browser automatically; visit %s", url)
    return LoginHandle(
        url=url, label=label, done=done, _server=server, _thread=thread, _saved=saved,
    )


def serve_paste_login(
    settings: Settings,
    store: AccountStore,
    *,
    label: str,
    open_browser: bool = True,
    host: str = "127.0.0.1",
    port: int | None = None,
    timeout: float = 600.0,
) -> str:
    """Blocking variant of :func:`start_paste_login`.

    Prints the URL to stdout and blocks until a save or timeout.
    """
    handle = start_paste_login(
        settings, store,
        label=label, host=host, port=port, open_browser=open_browser,
    )
    print(f"[login] paste helper: {handle.url}")
    saved = handle.wait(timeout=timeout)
    if saved is None:
        raise TimeoutError("login helper timed out; nothing saved")
    return saved
