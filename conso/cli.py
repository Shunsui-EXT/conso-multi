"""Command-line interface.

All commands are multi-account. Filter with ``--label`` (comma-separated).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from dataclasses import asdict
from typing import Any

from .accounts import AccountStore
from .config import Settings
from .runner import (
    install_signal_flag,
    run_wave,
    task_earn,
    task_refresh,
    task_smoke,
    task_status,
)
from .session import DEFAULT_PLATFORM_MODELS, TOP_MODELS
from .zaps import MODEL_MULTIPLIER


# --- helpers --------------------------------------------------------------
def _setup_logging(verbose: bool, log_path: str) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=level, format=fmt, datefmt="%H:%M:%S", handlers=handlers, force=True,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _parse_labels(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else []


def _resolve_accounts(store: AccountStore, labels: str) -> list:
    parsed = _parse_labels(labels)
    try:
        selection = store.select(parsed)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    if not selection:
        print(
            "no accounts to run — add one with `python3 main.py accounts add …`",
            file=sys.stderr,
        )
        sys.exit(2)
    return selection


# --- accounts subcommands -------------------------------------------------
def _cmd_accounts_list(store: AccountStore) -> int:
    rows = [account.sanitized() for account in store.all()]
    _print(rows)
    return 0


def _read_token(name: str, provided: str | None) -> str:
    if provided is not None:
        return provided.strip()
    return getpass.getpass(f"{name}: ").strip()


def _cmd_accounts_add(store: AccountStore, args: argparse.Namespace) -> int:
    access = _read_token("access_token", args.access_token)
    refresh = _read_token("refresh_token", args.refresh_token)
    if not access or not refresh:
        print("both access_token and refresh_token are required", file=sys.stderr)
        return 2
    account = store.upsert(
        label=args.label,
        access_token=access,
        refresh_token=refresh,
        enabled=not args.disabled,
        note=args.note or "",
    )
    _print(account.sanitized())
    return 0


def _cmd_accounts_remove(store: AccountStore, args: argparse.Namespace) -> int:
    if not store.remove(args.label):
        print(f"no account labelled {args.label!r}", file=sys.stderr)
        return 2
    print(f"removed {args.label}")
    return 0


def _cmd_accounts_toggle(
    store: AccountStore, args: argparse.Namespace, *, enabled: bool
) -> int:
    if not store.set_enabled(args.label, enabled):
        print(f"no account labelled {args.label!r}", file=sys.stderr)
        return 2
    print(f"{'enabled' if enabled else 'disabled'} {args.label}")
    return 0


# --- runner subcommands ---------------------------------------------------
async def _cmd_test(settings: Settings, store: AccountStore, args: argparse.Namespace) -> int:
    interrupt = install_signal_flag()
    accounts = _resolve_accounts(store, args.label)
    summary = await run_wave(
        settings, store, accounts, task_smoke(settings), interrupt=interrupt,
    )
    _print(
        [
            {"label": o.label, "ok": o.ok, "error": o.error, "result": o.payload}
            for o in summary.outcomes
        ]
    )
    def _payload(outcome):
        return outcome.payload if isinstance(outcome.payload, dict) else {}

    banned = [o for o in summary.outcomes if _payload(o).get("is_banned")]
    auth_failed = [o for o in summary.outcomes if _payload(o).get("auth") != "ok"]
    transport_failed = [o for o in summary.outcomes if not o.ok]
    if banned:
        return 3
    if auth_failed or transport_failed:
        return 1
    return 0


async def _cmd_earn(settings: Settings, store: AccountStore, args: argparse.Namespace) -> int:
    interrupt = install_signal_flag()
    accounts = _resolve_accounts(store, args.label)
    platforms = _parse_labels(args.platforms)
    summary = await run_wave(
        settings,
        store,
        accounts,
        task_earn(
            settings,
            platforms=platforms or None,
            long_prompts=not args.short,
            attach=not args.no_attach,
            interrupt=interrupt,
        ),
        interrupt=interrupt,
    )
    _print(
        [
            {
                "label": o.label,
                "ok": o.ok,
                "error": o.error,
                "report": asdict(o.payload) if o.ok and o.payload is not None else None,
            }
            for o in summary.outcomes
        ]
    )
    if any(o.ok and getattr(o.payload, "banned", False) for o in summary.outcomes):
        return 3
    if any(not o.ok for o in summary.outcomes):
        return 1
    return 0


async def _cmd_status(settings: Settings, store: AccountStore, args: argparse.Namespace) -> int:
    interrupt = install_signal_flag()
    accounts = _resolve_accounts(store, args.label)
    summary = await run_wave(
        settings, store, accounts, task_status(settings), interrupt=interrupt,
    )
    _print(
        [
            {"label": o.label, "ok": o.ok, "error": o.error, "result": o.payload}
            for o in summary.outcomes
        ]
    )
    return 0 if summary.ok_count() == len(summary.outcomes) else 1


async def _cmd_refresh(settings: Settings, store: AccountStore, args: argparse.Namespace) -> int:
    interrupt = install_signal_flag()
    accounts = _resolve_accounts(store, args.label)
    summary = await run_wave(
        settings, store, accounts, task_refresh(settings), interrupt=interrupt,
    )
    _print(
        [
            {"label": o.label, "ok": o.ok, "error": o.error, "result": o.payload}
            for o in summary.outcomes
        ]
    )
    return 0 if summary.ok_count() == len(summary.outcomes) else 1


async def _cmd_bootstrap(settings: Settings, store: AccountStore, args: argparse.Namespace) -> int:
    from .runner import task_bootstrap
    interrupt = install_signal_flag()
    accounts = _resolve_accounts(store, args.label)
    summary = await run_wave(
        settings, store, accounts, task_bootstrap(settings), interrupt=interrupt,
    )
    _print(
        [
            {"label": o.label, "ok": o.ok, "error": o.error, "result": o.payload}
            for o in summary.outcomes
        ]
    )
    banned = any(
        (isinstance(o.payload, dict) and o.payload.get("is_banned"))
        for o in summary.outcomes if o.ok
    )
    if banned:
        return 3
    return 0 if summary.ok_count() == len(summary.outcomes) else 1


def _cmd_platforms() -> int:
    rows = []
    for model, platform in TOP_MODELS:
        rows.append(
            {
                "platform": platform,
                "model": model,
                "multiplier": MODEL_MULTIPLIER.get(model),
                "default_for_platform": DEFAULT_PLATFORM_MODELS.get(platform) == model,
            }
        )
    _print(rows)
    return 0


# --- parser ---------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conso",
        description="Conso multi-account zap automation.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    # accounts
    accounts_parser = sub.add_parser("accounts", help="manage accounts")
    accounts_sub = accounts_parser.add_subparsers(dest="accounts_command", required=True)

    accounts_sub.add_parser("list", help="list every configured account")

    p_add = accounts_sub.add_parser("add", help="add or update an account")
    p_add.add_argument("label", help="short identifier (e.g. 'primary', 'alt1')")
    p_add.add_argument("--access-token", help="if omitted, prompted securely")
    p_add.add_argument("--refresh-token", help="if omitted, prompted securely")
    p_add.add_argument("--note", default="", help="optional memo")
    p_add.add_argument("--disabled", action="store_true", help="add but skip in runs")

    p_rm = accounts_sub.add_parser("remove", help="delete an account by label")
    p_rm.add_argument("label")

    p_en = accounts_sub.add_parser("enable", help="mark an account enabled")
    p_en.add_argument("label")

    p_di = accounts_sub.add_parser("disable", help="skip account in runs")
    p_di.add_argument("label")

    # runners
    for name, help_text in (
        ("test", "auth + profile pre-flight (no writes)"),
        ("status", "profile snapshot + auth TTL"),
        ("refresh", "force token refresh and persist"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--label", default="", help="comma list; default = every enabled")

    p_earn = sub.add_parser("earn", help="earn today's daily budget on every account")
    p_earn.add_argument("--label", default="", help="comma list; default = every enabled")
    p_earn.add_argument(
        "--platforms",
        default="",
        help=f"comma list; default = all four ({','.join(DEFAULT_PLATFORM_MODELS)})",
    )
    p_earn.add_argument(
        "--short", action="store_true", help="use the short corpus instead of long-form",
    )
    p_earn.add_argument(
        "--no-attach", action="store_true", help="do not send the attachment marker",
    )
    p_earn.add_argument("--dry-run", action="store_true", help="build payloads, post nothing")

    sub.add_parser("platforms", help="show the locked (model, platform) set")
    sub.add_parser(
        "interactive",
        aliases=["menu"],
        help="interactive menu (default when no subcommand is given)",
    )
    sub.add_parser(
        "dashboard",
        aliases=["tui"],
        help="live curses dashboard: header, account table, activity log",
    )
    p_login = sub.add_parser(
        "login",
        help="full-CLI login: launch Chromium with the Conso extension and "
             "capture the Supabase session automatically",
    )
    p_login.add_argument(
        "label", nargs="?", default="primary",
        help="account label to save under (default: primary)",
    )
    p_login.add_argument(
        "--mode",
        choices=("browser", "paste", "email", "otp"),
        default="browser",
        help="'browser' launches Chromium with the Conso extension "
             "(default); 'paste' expects a hand-pasted extension session; "
             "'email' signs in with email+password via the captcha-solver "
             "sidecar; 'otp' uses email one-time codes",
    )
    p_login.add_argument("--email", type=str, default="", help="email address (email/otp modes)")
    p_login.add_argument(
        "--password", type=str, default="",
        help="password (email mode); prompted securely if omitted",
    )
    p_login.add_argument(
        "--sitekey", type=str, default="",
        help="hCaptcha sitekey; falls back to $CONSO_HCAPTCHA_SITEKEY",
    )
    p_login.add_argument(
        "--captcha-url", type=str, default="https://www.conso.xyz",
        help="page the sitekey lives on (default https://www.conso.xyz)",
    )
    p_login.add_argument(
        "--otp-code", type=str, default="",
        help="pre-supply the 6-digit code (otp mode); prompted if omitted",
    )
    p_login.add_argument(
        "--create-user", action="store_true",
        help="allow signup when the email is not known (otp mode)",
    )
    p_login.add_argument(
        "--extension", type=str, default="",
        help="path to the unpacked Conso extension folder (browser mode; "
             "auto-detected if omitted)",
    )
    p_login.add_argument(
        "--profile", type=str, default="",
        help="reuse a Chromium user-data dir (browser mode); implies --keep-profile",
    )
    p_login.add_argument(
        "--keep-profile", action="store_true",
        help="keep the Chromium profile after login (browser mode)",
    )
    p_login.add_argument("--port", type=int, default=0,
                         help="local port for paste mode (0 = auto)")
    p_login.add_argument(
        "--no-open", action="store_true",
        help="paste mode only: do not auto-open the browser",
    )
    p_login.add_argument(
        "--timeout", type=float, default=600.0,
        help="seconds to wait for the login (default 600)",
    )
    p_boot = sub.add_parser(
        "bootstrap",
        help="first-time setup: verify auth, auto-claim daily checkin, "
             "print X-connect link if missing",
    )
    p_boot.add_argument("--label", default="", help="comma list; default = every enabled")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if getattr(args, "dry_run", False):
        settings.dry_run = True
    settings.ensure_dirs()
    _setup_logging(args.verbose, str(settings.log_dir / "app.log"))

    store = AccountStore(settings.accounts_file)
    store.load()

    cmd = args.command or "interactive"
    if cmd == "accounts":
        sub = args.accounts_command
        if sub == "list":
            return _cmd_accounts_list(store)
        if sub == "add":
            return _cmd_accounts_add(store, args)
        if sub == "remove":
            return _cmd_accounts_remove(store, args)
        if sub == "enable":
            return _cmd_accounts_toggle(store, args, enabled=True)
        if sub == "disable":
            return _cmd_accounts_toggle(store, args, enabled=False)
        parser.error(f"unknown accounts subcommand {sub}")

    if cmd == "test":
        return asyncio.run(_cmd_test(settings, store, args))
    if cmd == "status":
        return asyncio.run(_cmd_status(settings, store, args))
    if cmd == "refresh":
        return asyncio.run(_cmd_refresh(settings, store, args))
    if cmd == "earn":
        return asyncio.run(_cmd_earn(settings, store, args))
    if cmd == "bootstrap":
        return asyncio.run(_cmd_bootstrap(settings, store, args))
    if cmd == "platforms":
        return _cmd_platforms()
    if cmd in ("interactive", "menu"):
        from .interactive import run as run_interactive
        return run_interactive(settings, store)
    if cmd in ("dashboard", "tui"):
        from .dashboard import run as run_dashboard
        return run_dashboard(settings, store)
    if cmd == "login":
        if args.mode == "browser":
            from pathlib import Path
            from .login_browser import BrowserLoginError, login_via_browser
            try:
                result = login_via_browser(
                    settings, store,
                    label=args.label,
                    extension_dir=Path(args.extension) if args.extension else None,
                    user_data_dir=Path(args.profile) if args.profile else None,
                    keep_profile=args.keep_profile or bool(args.profile),
                    timeout=args.timeout,
                )
            except BrowserLoginError as exc:
                print(f"login failed: {exc}", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                print("cancelled", file=sys.stderr)
                return 2
            _print(
                {
                    "label": result.label,
                    "consoname": result.consoname,
                    "email": result.email,
                    "user_id": result.user_id,
                    "expires_at": result.expires_at,
                }
            )
            return 0
        if args.mode in ("email", "otp"):
            from .login_email import (
                EmailLoginError,
                login_via_email_otp,
                login_via_email_password,
            )
            if not args.email:
                print("email is required for --mode email/otp", file=sys.stderr)
                return 2
            try:
                if args.mode == "email":
                    result = login_via_email_password(
                        settings, store,
                        label=args.label,
                        email=args.email,
                        password=args.password or None,
                        sitekey=args.sitekey,
                        captcha_url=args.captcha_url,
                    )
                else:
                    result = login_via_email_otp(
                        settings, store,
                        label=args.label,
                        email=args.email,
                        code=args.otp_code or None,
                        sitekey=args.sitekey,
                        captcha_url=args.captcha_url,
                        create_user=args.create_user,
                    )
            except EmailLoginError as exc:
                print(f"login failed: {exc}", file=sys.stderr)
                return 1
            _print(
                {
                    "label": result.label,
                    "consoname": result.consoname,
                    "email": result.email,
                    "user_id": result.user_id,
                    "expires_at": result.expires_at,
                }
            )
            return 0
        # paste fallback
        from .login import serve_paste_login
        try:
            label = serve_paste_login(
                settings,
                store,
                label=args.label,
                open_browser=not args.no_open,
                port=args.port or None,
                timeout=args.timeout,
            )
        except TimeoutError as exc:
            print(f"login timed out: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"login failed: {exc}", file=sys.stderr)
            return 1
        print(f"saved account {label!r}")
        return 0

    parser.error(f"unknown command {cmd}")
