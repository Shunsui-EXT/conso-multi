"""Curses TUI dashboard.

Live multi-account overview: header with runtime settings, per-account table
with today/server/auth columns, activity log at the bottom, footer with
keybindings. All actions (earn, refresh, test) run on a background thread
event loop so the UI stays responsive.

Design goals:
- zero deps (stdlib curses),
- one visible truth: the same runner primitives the CLI uses,
- proactive refresh visible: auth TTL counts down in the account row.
"""

from __future__ import annotations

import asyncio
import curses
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .accounts import Account, AccountStore
from .client import ConsoClient
from .config import Settings
from .runner import (
    RunOutcome,
    run_wave,
    task_earn,
    task_refresh,
    task_smoke,
)
from .session import SessionReport
from .state import AccountState, state_path

REFRESH_INTERVAL = 1.0  # UI redraw cadence, seconds
POLL_INTERVAL = 30.0  # profile snapshot cadence, seconds
MAX_ACTIVITY = 200


# --- shared state ---------------------------------------------------------
@dataclass
class AccountView:
    """Everything the dashboard needs to render one row."""

    label: str
    enabled: bool
    consoname: str = ""
    total_zaps: float | None = None
    daily_zaps_earned: float | None = None
    is_banned: bool = False
    auth_ttl: int = 0
    state: str = "idle"  # idle | earn | refresh | test | error
    last_error: str = ""
    zaps_today_local: float = 0.0
    turns_today_local: int = 0
    last_updated: float = 0.0


@dataclass
class Activity:
    ts: float
    label: str
    channel: str
    message: str
    kind: str = "info"  # info | good | warn | bad


@dataclass
class UIModel:
    accounts: dict[str, AccountView] = field(default_factory=dict)
    activity: deque[Activity] = field(default_factory=lambda: deque(maxlen=MAX_ACTIVITY))
    selected_index: int = 0
    status_line: str = ""
    running_tasks: int = 0

    def order(self) -> list[str]:
        return list(self.accounts.keys())

    def add(self, label: str, level: str, channel: str, msg: str) -> None:
        self.activity.appendleft(
            Activity(ts=time.time(), label=label, channel=channel, message=msg, kind=level)
        )


# --- background worker: runs asyncio loop off the main thread -------------
class BackgroundLoop:
    """One asyncio loop pinned to a worker thread; scheduling is thread-safe."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._runner, name="conso-dashboard-loop", daemon=True
        )
        self._thread.start()

    def _runner(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


# --- action tasks (run on the background loop) ---------------------------
async def _snapshot_all(
    settings: Settings, store: AccountStore, accounts: list[Account]
) -> list[tuple[str, dict[str, Any] | None, str | None]]:
    """One profile + auth-ttl fetch per account, gathered."""
    async def _one(acc: Account):
        try:
            async with ConsoClient(settings, store, acc) as client:
                user = await client.verify_session()
                if user is None:
                    return acc.label, None, "auth failed"
                profile = await client.get_profile()
                ttl = int(client.auth.seconds_until_expiry())
                return acc.label, {"profile": profile, "auth_ttl": ttl}, None
        except Exception as exc:  # noqa: BLE001 - dashboard must not crash
            return acc.label, None, f"{exc.__class__.__name__}: {exc}"
    return await asyncio.gather(*(_one(a) for a in accounts))


class Dashboard:
    """Owns the UIModel + curses screen + background loop."""

    def __init__(self, settings: Settings, store: AccountStore) -> None:
        self.settings = settings
        self.store = store
        self.model = UIModel()
        self.bg = BackgroundLoop()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._interrupts: dict[str, asyncio.Event] = {}
        # Seed views from store + local state.
        for account in store.all():
            self._ensure_view(account)

    # -- model mutation (thread-safe) ------------------------------------
    def _ensure_view(self, account: Account) -> AccountView:
        with self._lock:
            view = self.model.accounts.get(account.label)
            if view is None:
                view = AccountView(label=account.label, enabled=account.enabled)
                self.model.accounts[account.label] = view
            view.enabled = account.enabled
            view.consoname = account.consoname or view.consoname
            sp = state_path(self.settings.state_dir, account.label)
            if sp.exists():
                st = AccountState.load(sp, label=account.label)
                view.zaps_today_local = st.zaps_today
                view.turns_today_local = st.turns_completed
            return view

    def _touch_view(
        self, label: str, **updates: Any
    ) -> None:
        with self._lock:
            view = self.model.accounts.get(label)
            if view is None:
                return
            for key, value in updates.items():
                setattr(view, key, value)
            view.last_updated = time.time()

    def _log(self, label: str, channel: str, msg: str, level: str = "info") -> None:
        with self._lock:
            self.model.add(label, level, channel, msg)

    def _set_status(self, msg: str) -> None:
        with self._lock:
            self.model.status_line = msg

    # -- background actions ----------------------------------------------
    def refresh_profiles(self, labels: list[str] | None = None) -> None:
        accounts = self.store.enabled()
        if labels:
            wanted = set(labels)
            accounts = [a for a in accounts if a.label in wanted]
        if not accounts:
            return
        for a in accounts:
            self._touch_view(a.label, state="refresh")
        future = self.bg.submit(_snapshot_all(self.settings, self.store, accounts))

        def _done(fut) -> None:
            try:
                rows = fut.result()
            except Exception as exc:  # noqa: BLE001
                self._log("*", "poll", f"crash: {exc}", "bad")
                return
            for label, payload, err in rows:
                if err:
                    self._touch_view(label, state="error", last_error=err)
                    self._log(label, "poll", err, "bad")
                    continue
                profile = (payload or {}).get("profile") or {}
                ttl = int((payload or {}).get("auth_ttl") or 0)
                self._touch_view(
                    label,
                    state="idle",
                    last_error="",
                    consoname=str(profile.get("consoname") or ""),
                    total_zaps=(
                        float(profile.get("total_zaps") or 0.0)
                        if profile.get("total_zaps") is not None
                        else None
                    ),
                    daily_zaps_earned=(
                        float(profile.get("daily_zaps_earned") or 0.0)
                        if profile.get("daily_zaps_earned") is not None
                        else None
                    ),
                    is_banned=bool(profile.get("is_banned")),
                    auth_ttl=ttl,
                )
        future.add_done_callback(_done)

    def start_earn(self, labels: list[str] | None = None) -> None:
        accounts = self.store.enabled()
        if labels:
            wanted = set(labels)
            accounts = [a for a in accounts if a.label in wanted]
        if not accounts:
            self._log("*", "earn", "no enabled accounts to run", "warn")
            return

        interrupt = asyncio.Event()
        # Registering per-label lets `stop_all` cancel every active wave.
        for a in accounts:
            self._interrupts[a.label] = interrupt
            self._touch_view(a.label, state="earn")
            self._log(a.label, "earn", "session started", "info")

        with self._lock:
            self.model.running_tasks += 1

        def _on_turn(label: str, platform: str, credited: float) -> None:
            self._log(label, platform, f"credited +{credited:.2f}", "good")
            # Refresh local state figures for this label.
            sp = state_path(self.settings.state_dir, label)
            if sp.exists():
                st = AccountState.load(sp, label=label)
                self._touch_view(
                    label,
                    zaps_today_local=st.zaps_today,
                    turns_today_local=st.turns_completed,
                )

        async def _hook(label: str, platform: str, credited: float) -> None:
            _on_turn(label, platform, credited)

        task = task_earn(
            self.settings,
            platforms=None,
            long_prompts=True,
            attach=True,
            interrupt=interrupt,
            on_turn=_hook,
        )
        future = self.bg.submit(
            run_wave(self.settings, self.store, accounts, task, interrupt=interrupt)
        )

        def _done(fut) -> None:
            with self._lock:
                self.model.running_tasks = max(0, self.model.running_tasks - 1)
            for a in accounts:
                self._interrupts.pop(a.label, None)
            try:
                summary = fut.result()
            except Exception as exc:  # noqa: BLE001
                self._log("*", "earn", f"crash: {exc}", "bad")
                for a in accounts:
                    self._touch_view(a.label, state="error", last_error=str(exc))
                return
            for outcome in summary.outcomes:
                self._on_outcome(outcome)
            # After the wave, refresh so server counters catch up.
            self.refresh_profiles([o.label for o in summary.outcomes])

        future.add_done_callback(_done)

    def _on_outcome(self, outcome: RunOutcome) -> None:
        if not outcome.ok:
            self._touch_view(outcome.label, state="error", last_error=outcome.error or "")
            self._log(outcome.label, "earn", outcome.error or "error", "bad")
            return
        report = outcome.payload if isinstance(outcome.payload, SessionReport) else None
        if report is None:
            self._touch_view(outcome.label, state="idle")
            return
        badge = "banned" if report.banned else f"+{report.zaps_earned:.2f}"
        parts = " ".join(f"{p}={v:.2f}" for p, v in report.platform_totals.items())
        level = "bad" if report.banned else ("good" if report.zaps_earned > 0 else "warn")
        self._touch_view(
            outcome.label,
            state="banned" if report.banned else "idle",
            last_error=report.stopped_reason if report.banned else "",
        )
        self._log(
            outcome.label,
            "earn",
            f"{badge} turns {report.turns_credited}/{report.turns_attempted}  {parts}  ({report.stopped_reason})",
            level,
        )

    def refresh_tokens(self, labels: list[str] | None = None) -> None:
        accounts = self.store.enabled()
        if labels:
            wanted = set(labels)
            accounts = [a for a in accounts if a.label in wanted]
        if not accounts:
            return
        for a in accounts:
            self._touch_view(a.label, state="refresh")
        future = self.bg.submit(
            run_wave(self.settings, self.store, accounts, task_refresh(self.settings))
        )

        def _done(fut) -> None:
            try:
                summary = fut.result()
            except Exception as exc:  # noqa: BLE001
                self._log("*", "refresh", f"crash: {exc}", "bad")
                return
            for outcome in summary.outcomes:
                data = outcome.payload if isinstance(outcome.payload, dict) else {}
                if outcome.ok:
                    did = bool(data.get("refreshed"))
                    ttl = int(data.get("expires_in") or 0)
                    self._touch_view(outcome.label, state="idle", auth_ttl=ttl)
                    self._log(
                        outcome.label,
                        "auth",
                        f"{'refreshed' if did else 'skipped'} ttl={ttl}s",
                        "good" if did else "info",
                    )
                else:
                    self._touch_view(
                        outcome.label, state="error", last_error=outcome.error or ""
                    )
                    self._log(outcome.label, "auth", outcome.error or "", "bad")

        future.add_done_callback(_done)

    def test_all(self, labels: list[str] | None = None) -> None:
        accounts = self.store.enabled()
        if labels:
            wanted = set(labels)
            accounts = [a for a in accounts if a.label in wanted]
        if not accounts:
            return
        for a in accounts:
            self._touch_view(a.label, state="test")
        future = self.bg.submit(
            run_wave(self.settings, self.store, accounts, task_smoke(self.settings))
        )

        def _done(fut) -> None:
            try:
                summary = fut.result()
            except Exception as exc:  # noqa: BLE001
                self._log("*", "test", f"crash: {exc}", "bad")
                return
            for outcome in summary.outcomes:
                data = outcome.payload if isinstance(outcome.payload, dict) else {}
                if not outcome.ok:
                    self._touch_view(outcome.label, state="error", last_error=outcome.error or "")
                    self._log(outcome.label, "test", outcome.error or "", "bad")
                    continue
                auth = data.get("auth")
                banned = bool(data.get("is_banned"))
                self._touch_view(
                    outcome.label,
                    state="banned" if banned else "idle",
                    consoname=str(data.get("consoname") or ""),
                    total_zaps=(
                        float(data.get("total_zaps") or 0.0)
                        if data.get("total_zaps") is not None else None
                    ),
                    daily_zaps_earned=(
                        float(data.get("daily_zaps_earned") or 0.0)
                        if data.get("daily_zaps_earned") is not None else None
                    ),
                    is_banned=banned,
                    auth_ttl=int(data.get("access_token_ttl") or 0),
                )
                self._log(
                    outcome.label, "test",
                    f"auth={auth} banned={banned} daily={data.get('daily_zaps_earned')}",
                    "good" if auth == "ok" and not banned else "warn",
                )

        future.add_done_callback(_done)

    def stop_all(self) -> None:
        for event in self._interrupts.values():
            self.settings  # noqa - keep reference
            # Set the event on the loop thread so waiters wake immediately.
            self.bg.loop.call_soon_threadsafe(event.set)
        self._log("*", "runner", "stop signalled", "warn")

    # -- toggle helpers ---------------------------------------------------
    def toggle_selected(self) -> None:
        with self._lock:
            order = self.model.order()
            if not order:
                return
            label = order[self.model.selected_index % len(order)]
        account = self.store.get(label)
        if not account:
            return
        was_enabled = account.enabled
        self.store.set_enabled(label, not was_enabled)
        refreshed = self.store.get(label)
        if refreshed is not None:
            self._ensure_view(refreshed)
        self._log(label, "cfg", "disabled" if was_enabled else "enabled", "info")

    def move_selection(self, delta: int) -> None:
        with self._lock:
            order = self.model.order()
            if not order:
                self.model.selected_index = 0
                return
            self.model.selected_index = (
                self.model.selected_index + delta
            ) % len(order)

    def selected_label(self) -> str | None:
        with self._lock:
            order = self.model.order()
            if not order:
                return None
            return order[self.model.selected_index % len(order)]

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._stop.set()
        self.stop_all()
        self.bg.stop()


# --- curses rendering -----------------------------------------------------
COLOR_HEADER = 1
COLOR_ROW = 2
COLOR_ROW_SEL = 3
COLOR_GOOD = 4
COLOR_WARN = 5
COLOR_BAD = 6
COLOR_DIM = 7
COLOR_ACCENT = 8


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(COLOR_ROW, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_ROW_SEL, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(COLOR_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_BAD, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_DIM, curses.COLOR_BLUE, -1)
    curses.init_pair(COLOR_ACCENT, curses.COLOR_MAGENTA, -1)


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Draw without ever crashing on width overflow."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    trimmed = text[: max_x - x - 1]
    try:
        win.addnstr(y, x, trimmed, max(0, max_x - x - 1), attr)
    except curses.error:
        pass


def _fmt_ttl(ttl: int) -> str:
    if ttl <= 0:
        return "expired"
    if ttl < 60:
        return f"{ttl}s"
    if ttl < 3600:
        return f"{ttl // 60}m{ttl % 60:02d}"
    h = ttl // 3600
    m = (ttl % 3600) // 60
    return f"{h}h{m:02d}m"


def _state_color(state: str) -> int:
    return {
        "earn": COLOR_ACCENT,
        "refresh": COLOR_DIM,
        "test": COLOR_DIM,
        "banned": COLOR_BAD,
        "error": COLOR_BAD,
        "idle": COLOR_ROW,
    }.get(state, COLOR_ROW)


def _kind_color(kind: str) -> int:
    return {
        "good": COLOR_GOOD,
        "warn": COLOR_WARN,
        "bad": COLOR_BAD,
    }.get(kind, COLOR_ROW)


def _draw(stdscr, dash: Dashboard) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    # Header bar (row 0-2)
    header_line = " Conso Multi-Account Dashboard "
    stats = dash.model
    enabled_count = sum(1 for v in stats.accounts.values() if v.enabled)
    banned_count = sum(1 for v in stats.accounts.values() if v.is_banned)
    total_server = sum(
        v.total_zaps or 0.0 for v in stats.accounts.values() if v.total_zaps is not None
    )
    total_daily = sum(
        v.daily_zaps_earned or 0.0 for v in stats.accounts.values()
        if v.daily_zaps_earned is not None
    )
    header_right = time.strftime("%H:%M:%S UTC", time.gmtime())

    stdscr.attron(curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
    _safe_addstr(stdscr, 0, 0, " " * (max_x - 1))
    _safe_addstr(stdscr, 0, 1, header_line)
    _safe_addstr(stdscr, 0, max_x - len(header_right) - 2, header_right)
    stdscr.attroff(curses.color_pair(COLOR_HEADER) | curses.A_BOLD)

    settings_line = (
        f" accounts={len(stats.accounts)}  enabled={enabled_count}  "
        f"running={stats.running_tasks}  banned={banned_count}  "
        f"cap={dash.settings.daily_zap_cap:.0f}  "
        f"ceiling={dash.settings.credit_ceiling:.2f}  "
        f"gap={dash.settings.gap_min:.0f}-{dash.settings.gap_max:.0f}s  "
        f"parallel={dash.settings.max_parallel}  "
        f"dry_run={'on' if dash.settings.dry_run else 'off'}"
    )
    _safe_addstr(stdscr, 1, 0, settings_line, curses.color_pair(COLOR_DIM))
    totals_line = (
        f" total server zaps: {total_server:.2f}   today (server): {total_daily:.2f}"
    )
    _safe_addstr(stdscr, 2, 0, totals_line, curses.color_pair(COLOR_ACCENT))

    # Accounts table
    table_top = 4
    headers = [
        (" ", 2),
        ("LABEL", 12),
        ("CONSONAME", 14),
        ("STATE", 8),
        ("LOCAL TODAY", 14),
        ("SERVER DAILY", 14),
        ("SERVER TOTAL", 14),
        ("AUTH TTL", 10),
    ]

    # Header row
    x = 0
    stdscr.attron(curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
    _safe_addstr(stdscr, table_top, 0, " " * (max_x - 1))
    for label, width in headers:
        _safe_addstr(stdscr, table_top, x, label.ljust(width))
        x += width
    stdscr.attroff(curses.color_pair(COLOR_HEADER) | curses.A_BOLD)

    with dash._lock:  # snapshot in a stable order
        order = dash.model.order()
        selected = dash.model.selected_index % max(1, len(order))
        views = [dash.model.accounts[label] for label in order]

    for idx, view in enumerate(views):
        row_y = table_top + 1 + idx
        if row_y >= max_y - 6:
            break
        selected_row = idx == selected
        base_attr = curses.color_pair(COLOR_ROW_SEL) if selected_row else curses.color_pair(_state_color(view.state))
        if selected_row:
            _safe_addstr(stdscr, row_y, 0, " " * (max_x - 1), base_attr)

        marker = ">" if selected_row else (" " if view.enabled else "·")
        cells = [
            (marker, 2),
            (view.label[:11], 12),
            ((view.consoname or "?")[:13], 14),
            (view.state[:7], 8),
            (
                f"{view.zaps_today_local:>5.2f} / {int(dash.settings.daily_zap_cap):<3}  ",
                14,
            ),
            (
                (
                    "-".rjust(12)
                    if view.daily_zaps_earned is None
                    else f"{view.daily_zaps_earned:>5.2f}/{int(dash.settings.daily_zap_cap):<3}"
                ),
                14,
            ),
            (
                "-".rjust(12) if view.total_zaps is None else f"{view.total_zaps:>10.2f}",
                14,
            ),
            (_fmt_ttl(view.auth_ttl).rjust(8), 10),
        ]
        x = 0
        for text, width in cells:
            attr = base_attr
            if not selected_row:
                # Colour ttl if getting close
                if width == 10:  # ttl column
                    if view.auth_ttl and view.auth_ttl < dash.settings.refresh_leeway:
                        attr = curses.color_pair(COLOR_WARN)
                if width == 14 and text.strip().endswith(f"/{int(dash.settings.daily_zap_cap)}"):
                    val = view.daily_zaps_earned or 0
                    if val >= dash.settings.daily_zap_cap * 0.9:
                        attr = curses.color_pair(COLOR_WARN)
            _safe_addstr(stdscr, row_y, x, text.ljust(width), attr)
            x += width

        # Error tag if any
        if view.last_error and not selected_row:
            err_x = x
            _safe_addstr(
                stdscr, row_y, err_x, f"  {view.last_error[: max_x - err_x - 3]}",
                curses.color_pair(COLOR_BAD),
            )

    # Activity feed
    log_top = table_top + 2 + len(views)
    log_top = min(log_top, max_y - 8)
    _safe_addstr(
        stdscr, log_top, 0,
        "── Activity ────────────────────────────────────────────────────────────────────────────",
        curses.color_pair(COLOR_DIM),
    )
    with dash._lock:
        rows = list(dash.model.activity)
    log_body_top = log_top + 1
    log_lines = max(0, max_y - log_body_top - 2)
    for i, act in enumerate(rows[:log_lines]):
        ts = time.strftime("%H:%M:%S", time.localtime(act.ts))
        line = (
            f"{ts}  {act.label:<10} {act.channel:<11}  {act.message}"
        )
        _safe_addstr(stdscr, log_body_top + i, 0, line, curses.color_pair(_kind_color(act.kind)))

    # Footer
    footer_y = max_y - 1
    footer = (
        " [e] earn all  [E] earn selected  [r] refresh  [t] test  "
        "[↑/↓] select  [space] toggle  [x] stop  [q] quit "
    )
    stdscr.attron(curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
    _safe_addstr(stdscr, footer_y, 0, " " * (max_x - 1))
    _safe_addstr(stdscr, footer_y, 0, footer)
    stdscr.attroff(curses.color_pair(COLOR_HEADER) | curses.A_BOLD)

    # Status line above footer
    with dash._lock:
        status = dash.model.status_line
    if status:
        _safe_addstr(stdscr, footer_y - 1, 0, status.ljust(max_x - 1), curses.color_pair(COLOR_WARN))

    stdscr.refresh()


def _tick_ttls(dash: Dashboard) -> None:
    """Decrement auth_ttl every second so the countdown feels live."""
    with dash._lock:
        for view in dash.model.accounts.values():
            if view.auth_ttl > 0:
                view.auth_ttl -= 1




def _curses_main(stdscr, dash: Dashboard) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.timeout(int(REFRESH_INTERVAL * 1000))
    _init_colors()

    last_poll = 0.0
    dash._log("*", "boot", "dashboard online", "info")
    dash.refresh_profiles()

    while True:
        _draw(stdscr, dash)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            break
        now = time.time()

        if ch != -1:
            if ch in (ord("q"), 27):  # ESC
                break
            if ch in (curses.KEY_UP, ord("k")):
                dash.move_selection(-1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                dash.move_selection(1)
            elif ch == ord(" "):
                dash.toggle_selected()
            elif ch == ord("e"):
                dash.start_earn()
            elif ch == ord("E"):
                label = dash.selected_label()
                if label:
                    dash.start_earn([label])
            elif ch == ord("r"):
                dash.refresh_tokens()
            elif ch == ord("t"):
                dash.test_all()
            elif ch == ord("x"):
                dash.stop_all()
            elif ch == ord("p"):
                dash.refresh_profiles()
            elif ch == curses.KEY_RESIZE:
                pass

        _tick_ttls(dash)
        if now - last_poll >= POLL_INTERVAL:
            dash.refresh_profiles()
            last_poll = now


def run(settings: Settings, store: AccountStore) -> int:
    dash = Dashboard(settings, store)
    try:
        curses.wrapper(_curses_main, dash)
    finally:
        dash.close()
    return 0
