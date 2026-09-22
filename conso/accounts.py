"""Account store — persisted list of accounts with credentials.

Multi-account is a first-class concept: every operation targets a labelled
account, and credentials are refreshed in place so an operator never has to
paste a new access token by hand.

Data lives in ``data/accounts.json``. The file is written atomically on every
mutation, so a crash mid-refresh never leaves half-written credentials.
"""

from __future__ import annotations

import json
import stat
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Account:
    """One Supabase-authenticated Conso session."""

    label: str
    access_token: str
    refresh_token: str
    enabled: bool = True
    note: str = ""
    account_id: str = ""
    consoname: str = ""
    updated_at: str = ""
    last_refresh_at: str = ""
    expires_at: int = 0  # unix seconds; 0 = unknown

    def sanitized(self) -> dict[str, Any]:
        """A safe-for-log view (tokens redacted, ids kept)."""
        return {
            "label": self.label,
            "enabled": self.enabled,
            "note": self.note,
            "account_id": self.account_id,
            "consoname": self.consoname,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "last_refresh_at": self.last_refresh_at,
            "access_token": _redact(self.access_token),
            "refresh_token": _redact(self.refresh_token),
        }


def _redact(token: str) -> str:
    if not token:
        return ""
    if len(token) < 12:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


class AccountStore:
    """Thread-safe JSON-backed account list.

    Operations serialise through an internal lock so the async runner can
    write refreshed tokens without racing the CLI mutating the same file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._loaded = False

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._accounts = list(self._read())
            self._loaded = True

    def _read(self) -> Iterable[Account]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        out: list[Account] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label", "")).strip()
            if not label:
                continue
            out.append(
                Account(
                    label=label,
                    access_token=str(row.get("access_token", "")),
                    refresh_token=str(row.get("refresh_token", "")),
                    enabled=bool(row.get("enabled", True)),
                    note=str(row.get("note", "")),
                    account_id=str(row.get("account_id", "")),
                    consoname=str(row.get("consoname", "")),
                    updated_at=str(row.get("updated_at", "")),
                    last_refresh_at=str(row.get("last_refresh_at", "")),
                    expires_at=int(row.get("expires_at", 0) or 0),
                )
            )
        return out

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps([asdict(a) for a in self._accounts], indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            try:
                self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    # -- lookup ------------------------------------------------------------
    def all(self) -> list[Account]:
        with self._lock:
            if not self._loaded:
                self.load()
            return list(self._accounts)

    def enabled(self) -> list[Account]:
        return [a for a in self.all() if a.enabled and a.access_token]

    def get(self, label: str) -> Account | None:
        for account in self.all():
            if account.label == label:
                return account
        return None

    def select(self, labels: list[str] | None) -> list[Account]:
        """Resolve a label filter to Account rows, keeping order.

        Empty list = every enabled account. Unknown labels raise.
        """
        rows = self.enabled()
        if not labels:
            return rows
        by_label = {a.label: a for a in self.all()}
        missing = [label for label in labels if label not in by_label]
        if missing:
            raise KeyError(f"unknown account label(s): {', '.join(missing)}")
        return [by_label[label] for label in labels if by_label[label].enabled]

    # -- mutation ---------------------------------------------------------
    def upsert(
        self,
        *,
        label: str,
        access_token: str,
        refresh_token: str,
        enabled: bool = True,
        note: str = "",
    ) -> Account:
        with self._lock:
            self.load() if not self._loaded else None
            existing = next((a for a in self._accounts if a.label == label), None)
            if existing:
                existing.access_token = access_token
                existing.refresh_token = refresh_token
                existing.enabled = enabled
                existing.note = note or existing.note
                existing.updated_at = _now()
                existing.expires_at = 0  # invalidate stale exp
                account = existing
            else:
                account = Account(
                    label=label,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    enabled=enabled,
                    note=note,
                    updated_at=_now(),
                )
                self._accounts.append(account)
            self.save()
            return account

    def remove(self, label: str) -> bool:
        with self._lock:
            self.load() if not self._loaded else None
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.label != label]
            if len(self._accounts) == before:
                return False
            self.save()
            return True

    def set_enabled(self, label: str, enabled: bool) -> bool:
        with self._lock:
            self.load() if not self._loaded else None
            for account in self._accounts:
                if account.label == label:
                    account.enabled = enabled
                    account.updated_at = _now()
                    self.save()
                    return True
            return False

    def update_tokens(
        self,
        label: str,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_at: int,
    ) -> None:
        """Persist a refreshed session for one account."""
        with self._lock:
            self.load() if not self._loaded else None
            for account in self._accounts:
                if account.label == label:
                    account.access_token = access_token
                    if refresh_token:
                        account.refresh_token = refresh_token
                    account.expires_at = expires_at
                    account.last_refresh_at = _now()
                    account.updated_at = _now()
                    self.save()
                    return
            raise KeyError(label)

    def update_profile(
        self,
        label: str,
        *,
        account_id: str | None = None,
        consoname: str | None = None,
    ) -> None:
        with self._lock:
            self.load() if not self._loaded else None
            for account in self._accounts:
                if account.label == label:
                    if account_id:
                        account.account_id = account_id
                    if consoname is not None:
                        account.consoname = consoname
                    account.updated_at = _now()
                    self.save()
                    return
            raise KeyError(label)
