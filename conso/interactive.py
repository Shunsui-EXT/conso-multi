"""Interactive menu-driven runner.

Zero-dependency stdlib TUI: a small menu loop that composes the same
:mod:`conso.runner` primitives the CLI uses. Every action returns to the
menu so a single ``python3 main.py`` session covers day-to-day operation
without remembering flags.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from typing import Callable

from .accounts import Account, AccountStore
from .config import Settings
from .runner import (
    RunOutcome,
    install_signal_flag,
    run_wave,
    task_earn,
    task_refresh,
    task_smoke,
    task_status,
)
from .session import DEFAULT_PLATFORM_MODELS, SessionReport, TOP_MODELS
from .zaps import MODEL_MULTIPLIER

# --- ANSI helpers (soft; disable by piping into a non-tty) ----------------
def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


_TTY = _isatty()


def _paint(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _TTY else text


def bold(text: str) -> str:
    return _paint("1", text)


def dim(text: str) -> str:
    return _paint("2", text)


def green(text: str) -> str:
    return _paint("32", text)


def yellow(text: str) -> str:
    return _paint("33", text)


def red(text: str) -> str:
    return _paint("31", text)


def cyan(text: str) -> str:
    return _paint("36", text)


def _print_header(settings: Settings) -> None:
    print()
    print(bold(cyan("Conso Multi-Account Runner")))
    print(
        dim(
            f"parallel={settings.max_parallel} stagger={settings.account_stagger:.0f}s "
            f"gap={settings.gap_min:.0f}-{settings.gap_max:.0f}s "
            f"cap={settings.daily_zap_cap:.0f} ceiling={settings.credit_ceiling:.2f} "
            f"refresh_leeway={settings.refresh_leeway:.0f}s "
            f"dry_run={'on' if settings.dry_run else 'off'}"
        )
    )


def _print_accounts(store: AccountStore) -> None:
    rows = store.all()
    if not rows:
        print(yellow("  (no accounts — add one from the menu)"))
        return
    for idx, a in enumerate(rows, start=1):
        state = green("enabled") if a.enabled else yellow("disabled")
        name = a.consoname or dim("?")
        note = f"  · {a.note}" if a.note else ""
        print(f"  {idx:>2}. {bold(a.label):<24} {state}  {dim(name)}{note}")


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return value or default


def _prompt_secret(msg: str) -> str:
    try:
        return getpass.getpass(f"{msg}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _prompt_bool(msg: str, default: bool = True) -> bool:
    default_str = "Y/n" if default else "y/N"
    raw = _prompt(f"{msg} ({default_str})").lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _pick_accounts(store: AccountStore, prompt: str = "accounts") -> list[Account]:
    """Prompt for a subset; empty = every enabled account."""
    rows = store.all()
    if not rows:
        print(red("no accounts configured"))
        return []
    print()
    print(bold("Select accounts:"))
    _print_accounts(store)
    print(
        dim(
            "  enter comma indices, labels, or blank for every enabled "
            "account (e.g. `1,3` or `primary,alt1`)"
        )
    )
    raw = _prompt(prompt)
    if not raw:
        return store.enabled()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    labels: list[str] = []
    by_label = {a.label: a for a in rows}
    for tok in tokens:
        if tok.isdigit():
            idx = int(tok)
            if 1 <= idx <= len(rows):
                labels.append(rows[idx - 1].label)
            else:
                print(red(f"  out-of-range index {tok}"))
        elif tok in by_label:
            labels.append(tok)
        else:
            print(red(f"  unknown account {tok!r}"))
    try:
        selection = store.select(labels)
    except KeyError as exc:
        print(red(str(exc)))
        return []
    if not selection:
        print(yellow("  no enabled account matched — nothing to do"))
    return selection


# --- outcome printers -----------------------------------------------------


def _summarise_test(outcomes: list[RunOutcome]) -> None:
    for o in outcomes:
        data = o.payload if isinstance(o.payload, dict) else {}
        auth = data.get("auth")
        head = bold(o.label)
        if not o.ok:
            print(f"  {head}: {red('crash')} {o.error}")
            continue
        if auth != "ok":
            print(f"  {head}: {red('auth failed')}")
            continue
        banned = data.get("is_banned")
        badge = red("BANNED") if banned else green("ok")
        ttl = data.get("access_token_ttl", 0)
        print(
            f"  {head}: {badge}  "
            f"consoname={data.get('consoname')} "
            f"total_zaps={data.get('total_zaps')} "
            f"daily={data.get('daily_zaps_earned')} "
            f"ttl={ttl}s"
        )


def _summarise_earn(outcomes: list[RunOutcome]) -> None:
    total = 0.0
    for o in outcomes:
        head = bold(o.label)
        if not o.ok:
            print(f"  {head}: {red('crash')} {o.error}")
            continue
        report: SessionReport | None = (
            o.payload if isinstance(o.payload, SessionReport) else None
        )
        if report is None:
            print(f"  {head}: {yellow('no report')}")
            continue
        total += report.zaps_earned
        badge = red("banned") if report.banned else green(f"+{report.zaps_earned:.2f}")
        parts = " ".join(
            f"{p}={v:.2f}" for p, v in sorted(report.platform_totals.items())
        )
        print(
            f"  {head}: {badge}  "
            f"turns={report.turns_credited}/{report.turns_attempted} "
            f"{parts}  ({dim(report.stopped_reason)})"
        )
    print(dim(f"  wave total: {total:.2f} zaps"))


def _summarise_status(outcomes: list[RunOutcome]) -> None:
    for o in outcomes:
        head = bold(o.label)
        if not o.ok:
            print(f"  {head}: {red('crash')} {o.error}")
            continue
        data = o.payload if isinstance(o.payload, dict) else {}
        profile = data.get("profile") or {}
        print(
            f"  {head}: {green('ok')}  "
            f"consoname={profile.get('consoname')} "
            f"total_zaps={profile.get('total_zaps')} "
            f"daily={profile.get('daily_zaps_earned')} "
            f"ttl={data.get('auth_ttl')}s"
        )


def _summarise_refresh(outcomes: list[RunOutcome]) -> None:
    for o in outcomes:
        head = bold(o.label)
        if not o.ok:
            print(f"  {head}: {red('crash')} {o.error}")
            continue
        data = o.payload if isinstance(o.payload, dict) else {}
        badge = green("refreshed") if data.get("refreshed") else yellow("skipped")
        print(f"  {head}: {badge}  ttl={data.get('expires_in')}s")


# --- async action dispatchers --------------------------------------------
async def _run(settings: Settings, store: AccountStore, task_factory: Callable, accounts: list[Account], **kwargs) -> list[RunOutcome]:
    interrupt = install_signal_flag()
    task = task_factory(settings, **kwargs) if kwargs else task_factory(settings)
    summary = await run_wave(
        settings, store, accounts, task, interrupt=interrupt,
    )
    return summary.outcomes


async def _action_test(settings: Settings, store: AccountStore) -> None:
    accounts = _pick_accounts(store, "test which accounts?")
    if not accounts:
        return
    outcomes = await _run(settings, store, task_smoke, accounts)
    print()
    _summarise_test(outcomes)


async def _action_status(settings: Settings, store: AccountStore) -> None:
    accounts = _pick_accounts(store, "status for which accounts?")
    if not accounts:
        return
    outcomes = await _run(settings, store, task_status, accounts)
    print()
    _summarise_status(outcomes)


async def _action_refresh(settings: Settings, store: AccountStore) -> None:
    accounts = _pick_accounts(store, "refresh which accounts?")
    if not accounts:
        return
    outcomes = await _run(settings, store, task_refresh, accounts)
    print()
    _summarise_refresh(outcomes)


async def _action_earn(settings: Settings, store: AccountStore) -> None:
    accounts = _pick_accounts(store, "earn on which accounts?")
    if not accounts:
        return

    all_platforms = list(DEFAULT_PLATFORM_MODELS.keys())
    print()
    print(bold("Platforms:"))
    for idx, p in enumerate(all_platforms, start=1):
        print(f"  {idx}. {p}  {dim(DEFAULT_PLATFORM_MODELS[p])}")
    print(dim("  blank = all four"))
    raw = _prompt("platforms")
    platforms: list[str] | None = None
    if raw:
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        chosen: list[str] = []
        for tok in tokens:
            if tok.isdigit() and 1 <= int(tok) <= len(all_platforms):
                chosen.append(all_platforms[int(tok) - 1])
            elif tok in DEFAULT_PLATFORM_MODELS:
                chosen.append(tok)
            else:
                print(red(f"  unknown platform {tok!r}"))
                return
        platforms = chosen or None

    long_prompts = _prompt_bool("use long-form corpus?", default=True)
    attach = _prompt_bool("mark non-image attachment (3x zaps)?", default=True)
    dry_run = _prompt_bool("dry-run only?", default=settings.dry_run)

    prior_dry_run = settings.dry_run
    settings.dry_run = dry_run
    interrupt = install_signal_flag()

    async def _on_turn(label: str, platform: str, credited: float) -> None:
        print(f"  {dim('·')} {bold(label)} {platform} {green(f'+{credited:.2f}')}")

    try:
        task = task_earn(
            settings,
            platforms=platforms,
            long_prompts=long_prompts,
            attach=attach,
            interrupt=interrupt,
            on_turn=_on_turn,
        )
        print()
        print(dim("  running… Ctrl-C to stop early"))
        summary = await run_wave(
            settings, store, accounts, task, interrupt=interrupt,
        )
    finally:
        settings.dry_run = prior_dry_run

    print()
    _summarise_earn(summary.outcomes)


# --- account CRUD (interactive) -------------------------------------------
def _action_accounts_add(store: AccountStore) -> None:
    label = _prompt("label (e.g. primary, alt1)")
    if not label:
        print(yellow("cancelled"))
        return
    access = _prompt_secret("access_token")
    if not access:
        print(yellow("cancelled"))
        return
    refresh = _prompt_secret("refresh_token")
    if not refresh:
        print(yellow("cancelled"))
        return
    note = _prompt("note (optional)")
    enabled = _prompt_bool("enable now?", default=True)
    account = store.upsert(
        label=label,
        access_token=access,
        refresh_token=refresh,
        enabled=enabled,
        note=note,
    )
    print(green(f"  saved {account.label}"))


def _action_accounts_remove(store: AccountStore) -> None:
    accounts = _pick_accounts(store, "remove which account?")
    if not accounts:
        return
    if not _prompt_bool(f"delete {len(accounts)} account(s)?", default=False):
        print(yellow("cancelled"))
        return
    for a in accounts:
        store.remove(a.label)
        print(green(f"  removed {a.label}"))


def _action_accounts_toggle(store: AccountStore, *, enable: bool) -> None:
    accounts = _pick_accounts(
        store, f"{'enable' if enable else 'disable'} which account?"
    )
    if not accounts:
        return
    for a in accounts:
        store.set_enabled(a.label, enable)
        print(
            green(f"  enabled {a.label}") if enable else yellow(f"  disabled {a.label}")
        )


def _action_platforms() -> None:
    print()
    for model, platform in TOP_MODELS:
        print(
            f"  {bold(platform):<12}  {model:<24}  "
            f"mult={MODEL_MULTIPLIER.get(model)}"
        )


# --- main loop -----------------------------------------------------------
MENU: tuple[tuple[str, str], ...] = (
    ("1", "list accounts"),
    ("2", "add / update account"),
    ("3", "enable account"),
    ("4", "disable account"),
    ("5", "remove account"),
    ("t", "test (auth + profile pre-flight)"),
    ("s", "status"),
    ("r", "refresh tokens"),
    ("e", "earn"),
    ("p", "platforms (locked set)"),
    ("q", "quit"),
)


def _render_menu() -> None:
    print()
    print(bold("Menu:"))
    for key, label in MENU:
        print(f"  [{bold(key)}] {label}")


def run(settings: Settings, store: AccountStore) -> int:
    """Blocking menu loop; returns a process exit code."""
    while True:
        _print_header(settings)
        print()
        print(bold("Accounts:"))
        _print_accounts(store)
        _render_menu()
        choice = _prompt("choose").lower()
        if not choice:
            continue
        try:
            if choice == "q":
                print(dim("bye"))
                return 0
            if choice == "1":
                # already printed on every loop
                continue
            if choice == "2":
                _action_accounts_add(store)
                continue
            if choice == "3":
                _action_accounts_toggle(store, enable=True)
                continue
            if choice == "4":
                _action_accounts_toggle(store, enable=False)
                continue
            if choice == "5":
                _action_accounts_remove(store)
                continue
            if choice == "t":
                asyncio.run(_action_test(settings, store))
                continue
            if choice == "s":
                asyncio.run(_action_status(settings, store))
                continue
            if choice == "r":
                asyncio.run(_action_refresh(settings, store))
                continue
            if choice == "e":
                asyncio.run(_action_earn(settings, store))
                continue
            if choice == "p":
                _action_platforms()
                continue
            print(red(f"unknown choice {choice!r}"))
        except KeyboardInterrupt:
            print()
            print(yellow("interrupted"))
        except Exception as exc:  # noqa: BLE001 - keep menu alive on any error
            print(red(f"error: {exc.__class__.__name__}: {exc}"))
