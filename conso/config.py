"""Runtime settings, loaded from `.env`.

Every secret lives in `data/accounts.json`; this file only carries tuning
knobs. `AccountStore` (accounts.py) reads credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"
LOGS_DIR = ROOT / "logs"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"


def _load_dotenv() -> None:
    """Minimal `.env` loader — no third-party dep."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Resolved runtime settings — no secrets here."""

    supabase_url: str = "https://jzxlayjrsdbyzykuiqns.supabase.co"
    supabase_key: str = "sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd"

    daily_zap_cap: float = 21.0
    credit_ceiling: float = 3.2
    gap_min: float = 60.0
    gap_max: float = 180.0
    account_stagger: float = 15.0
    max_parallel: int = 4

    request_timeout: float = 30.0
    max_retries: int = 3
    refresh_leeway: float = 90.0

    dry_run: bool = False

    accounts_file: Path = field(default_factory=lambda: ACCOUNTS_FILE)
    state_dir: Path = field(default_factory=lambda: STATE_DIR)
    log_dir: Path = field(default_factory=lambda: LOGS_DIR)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            supabase_url=os.environ.get("CONSO_SUPABASE_URL", cls.supabase_url),
            supabase_key=os.environ.get("CONSO_SUPABASE_KEY", cls.supabase_key),
            daily_zap_cap=_float("CONSO_DAILY_ZAP_CAP", 21.0),
            credit_ceiling=_float("CONSO_CREDIT_CEILING", 3.2),
            gap_min=_float("CONSO_GAP_MIN", 60.0),
            gap_max=_float("CONSO_GAP_MAX", 180.0),
            account_stagger=_float("CONSO_ACCOUNT_STAGGER", 15.0),
            max_parallel=max(1, _int("CONSO_MAX_PARALLEL", 4)),
            request_timeout=_float("CONSO_REQUEST_TIMEOUT", 30.0),
            max_retries=_int("CONSO_MAX_RETRIES", 3),
            refresh_leeway=_float("CONSO_REFRESH_LEEWAY", 90.0),
            dry_run=_bool("CONSO_DRY_RUN", False),
        )

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
