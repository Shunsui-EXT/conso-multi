"""Per-account daily state.

Every account owns a small JSON file at ``data/state/<label>.json`` recording
turns credited today plus a rolling history. The state rolls over on a UTC
date change so a new day starts clean without operator intervention.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@dataclass
class AccountState:
    """Local checkpoint for one account."""

    label: str = ""
    account_id: str = ""
    date: str = field(default_factory=_today)
    zaps_today: float = 0.0
    credited_total: float = 0.0
    turns_completed: int = 0
    claimed: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    last_turn_at: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path, *, label: str) -> "AccountState":
        if not path.exists():
            return cls(label=label)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(label=label)

        state = cls(
            label=str(raw.get("label", label) or label),
            account_id=str(raw.get("account_id", "")),
            date=str(raw.get("date", _today())),
            zaps_today=float(raw.get("zaps_today", 0.0)),
            credited_total=float(raw.get("credited_total", 0.0)),
            turns_completed=int(raw.get("turns_completed", 0)),
            claimed=[str(x) for x in raw.get("claimed", [])],
            history=list(raw.get("history", []))[-200:],
            last_turn_at=str(raw.get("last_turn_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )
        if state.date != _today():
            state.date = _today()
            state.zaps_today = 0.0
            state.turns_completed = 0
            state.claimed = []
        return state

    def save(self, path: Path) -> None:
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)

    def bind_account(self, account_id: str) -> None:
        """Reset the checkpoint if the account behind the label changed."""
        if not account_id:
            return
        if self.account_id and self.account_id != account_id:
            self.account_id = account_id
            self.zaps_today = 0.0
            self.turns_completed = 0
            self.claimed = []
            self.history = []
            self.date = _today()
        else:
            self.account_id = account_id

    def record_turn(
        self,
        *,
        model: str,
        platform: str,
        input_tokens: int,
        output_tokens: int,
        quality: float,
        credited: float,
    ) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.turns_completed += 1
        self.zaps_today = round(self.zaps_today + credited, 2)
        self.credited_total = round(self.credited_total + credited, 2)
        self.last_turn_at = ts
        self.history.append(
            {
                "ts": ts,
                "model": model,
                "platform": platform,
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "quality": quality,
                "credited": credited,
            }
        )
        self.history = self.history[-200:]

    def record_claim(self, mission_id: str) -> None:
        if mission_id not in self.claimed:
            self.claimed.append(mission_id)

    def cap_reached(self, cap: float) -> bool:
        return cap > 0 and self.zaps_today >= cap


def state_path(state_dir: Path, label: str) -> Path:
    """Location of the state file for one account label."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
    return state_dir / f"{safe}.json"
