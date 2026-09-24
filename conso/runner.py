"""Multi-account runner.

Fan out over N accounts with a bounded concurrency semaphore. Each account
runs an isolated :class:`ConsoClient` + :class:`AccountState`; failures on one
account never abort the others.
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .accounts import Account, AccountStore
from .client import AuthExpired, ConsoClient, ConsoError
from .config import Settings
from .session import (
    SessionReport,
    run_earning_session,
    smoke_test,
    state_path_from,
)
from .state import AccountState

log = logging.getLogger("conso.runner")

RunOne = Callable[[ConsoClient, AccountState], Awaitable[Any]]


@dataclass
class RunOutcome:
    """One account's outcome from a runner wave."""

    label: str
    ok: bool
    error: str | None = None
    payload: Any = None


@dataclass
class RunSummary:
    """Aggregate result across every account in a wave."""

    outcomes: list[RunOutcome] = field(default_factory=list)

    def by_label(self, label: str) -> RunOutcome | None:
        for row in self.outcomes:
            if row.label == label:
                return row
        return None

    def ok_count(self) -> int:
        return sum(1 for r in self.outcomes if r.ok)


def install_signal_flag() -> asyncio.Event:
    """Return an event set on SIGINT/SIGTERM (best-effort on non-POSIX)."""
    flag = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _fire() -> None:
        flag.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _fire)
        except (NotImplementedError, RuntimeError):
            pass
    return flag


async def _run_one(
    *,
    settings: Settings,
    store: AccountStore,
    account: Account,
    task: RunOne,
    sem: asyncio.Semaphore,
    stagger: float,
    interrupt: asyncio.Event,
) -> RunOutcome:
    if stagger > 0 and not settings.dry_run:
        try:
            await asyncio.wait_for(interrupt.wait(), timeout=stagger)
            return RunOutcome(label=account.label, ok=False, error="interrupted")
        except asyncio.TimeoutError:
            pass
    async with sem:
        if interrupt.is_set():
            return RunOutcome(label=account.label, ok=False, error="interrupted")
        state_path = state_path_from(settings, account.label)
        state = AccountState.load(state_path, label=account.label)
        try:
            async with ConsoClient(settings, store, account) as client:
                payload = await task(client, state)
                # Persist any consoname change picked up by the task.
                try:
                    profile_data = payload if isinstance(payload, dict) else None
                    if profile_data and profile_data.get("consoname"):
                        store.update_profile(
                            account.label,
                            account_id=state.account_id,
                            consoname=str(profile_data["consoname"]),
                        )
                except KeyError:
                    pass
                return RunOutcome(label=account.label, ok=True, payload=payload)
        except AuthExpired as exc:
            log.error("[%s] auth failed: %s", account.label, exc)
            return RunOutcome(label=account.label, ok=False, error=f"auth: {exc}")
        except ConsoError as exc:
            log.error("[%s] error: %s", account.label, exc)
            return RunOutcome(label=account.label, ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - final defence per account
            log.exception("[%s] crashed: %s", account.label, exc)
            return RunOutcome(
                label=account.label, ok=False, error=f"crash: {exc.__class__.__name__}"
            )
        finally:
            try:
                state.save(state_path)
            except OSError as exc:
                log.warning("[%s] state save failed: %s", account.label, exc)


async def run_wave(
    settings: Settings,
    store: AccountStore,
    accounts: list[Account],
    task: RunOne,
    *,
    interrupt: asyncio.Event | None = None,
) -> RunSummary:
    """Run ``task`` for every account with bounded concurrency."""
    interrupt = interrupt or asyncio.Event()
    sem = asyncio.Semaphore(max(1, settings.max_parallel))
    # Randomise the account launch order so a repeat run does not always hit
    # the same account first — a small anti-fingerprint measure.
    shuffled = list(accounts)
    random.shuffle(shuffled)
    coros = [
        _run_one(
            settings=settings,
            store=store,
            account=account,
            task=task,
            sem=sem,
            stagger=(i * settings.account_stagger),
            interrupt=interrupt,
        )
        for i, account in enumerate(shuffled)
    ]
    outcomes = await asyncio.gather(*coros, return_exceptions=False)
    return RunSummary(outcomes=list(outcomes))


# --- concrete tasks -------------------------------------------------------
def task_smoke(_settings: Settings) -> RunOne:
    async def _task(client: ConsoClient, _state: AccountState) -> dict[str, Any]:
        return await smoke_test(client)
    return _task


def task_earn(
    settings: Settings,
    *,
    platforms: list[str] | None,
    long_prompts: bool,
    attach: bool,
    interrupt: asyncio.Event,
    on_turn: Callable[[str, str, float], Awaitable[None] | None] | None = None,
) -> RunOne:
    async def _task(client: ConsoClient, state: AccountState) -> SessionReport:
        return await run_earning_session(
            settings,
            client,
            state,
            platforms=platforms,
            long_prompts=long_prompts,
            attach=attach,
            interrupt=interrupt,
            on_turn=on_turn,
        )
    return _task


def task_status(_settings: Settings) -> RunOne:
    async def _task(client: ConsoClient, _state: AccountState) -> dict[str, Any]:
        profile = await client.get_profile()
        return {
            "label": client.label,
            "auth_ttl": int(client.auth.seconds_until_expiry()),
            "profile": profile,
        }
    return _task


def task_refresh(_settings: Settings) -> RunOne:
    async def _task(client: ConsoClient, _state: AccountState) -> dict[str, Any]:
        # ensure_fresh short-circuits if not near expiry; force one refresh.
        did = await client.auth.refresh()
        return {
            "label": client.label,
            "refreshed": did,
            "expires_in": int(client.auth.seconds_until_expiry()),
        }
    return _task

