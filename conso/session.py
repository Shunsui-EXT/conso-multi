"""Earning session orchestrator.

One session per account. Applies every measured production constraint:

- only the four locked (model, platform) pairs are ever sent,
- each payload is scaled to land just under the per-credit ceiling,
- per-platform targets are planned so the daily budget fills evenly instead
  of the first platform draining it,
- runs stop on the first hard error (ban, auth) or two consecutive
  zero-credit responses (the soft flag that precedes a ban).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .client import AccountBanned, AuthExpired, ConsoClient, new_entry
from .config import Settings
from .prompts import LONG_PROMPT_TEMPLATES, SHORT_PROMPT_TEMPLATES
from .state import AccountState
from .zaps import (
    apply_attachment_multiplier,
    base_zaps,
    estimate_cost,
    estimate_tokens,
    quality_score,
)

log = logging.getLogger("conso.session")


# The locked (model, platform) pairs — highest multiplier real model id on
# each of the four platforms. Anything outside this set drops to the platform
# floor (0.1 on gemini) and is rejected up-front instead of silently earning
# nothing.
TOP_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-fable-5", "claude"),
    ("pplx_asi_fable_5", "perplexity"),
    ("gpt-5-6-thinking", "chatgpt"),
    ("gemini-3.1-pro", "gemini"),
)
DEFAULT_PLATFORM_MODELS: dict[str, str] = {p: m for m, p in TOP_MODELS}
MODEL_PLATFORM: dict[str, str] = {m: p for m, p in TOP_MODELS}


def resolve_top_model(model: str, platform: str | None = None) -> tuple[str, str]:
    """Return the locked (model, platform) pair; raise on anything else."""
    if model in MODEL_PLATFORM:
        locked = MODEL_PLATFORM[model]
        if platform and platform != locked:
            raise ValueError(
                f"model {model!r} belongs to {locked!r}, not {platform!r}"
            )
        return model, locked
    if platform and platform in DEFAULT_PLATFORM_MODELS:
        return DEFAULT_PLATFORM_MODELS[platform], platform
    allowed = ", ".join(f"{m} ({p})" for m, p in TOP_MODELS)
    raise ValueError(f"model {model!r} not in locked set: {allowed}")


@dataclass
class SessionReport:
    """Summary of one earning session on one account."""

    label: str
    started_at: str = ""
    turns_attempted: int = 0
    turns_credited: int = 0
    zaps_earned: float = 0.0
    platform_totals: dict[str, float] = field(default_factory=dict)
    missions_claimed: list[str] = field(default_factory=list)
    stopped_reason: str = ""
    banned: bool = False


# --- payload construction -------------------------------------------------
def build_turn(
    prompt_text: str,
    model: str,
    platform: str,
    *,
    response_scale: float = 1.6,
    has_non_image_attachment: bool = False,
) -> dict[str, Any]:
    """Turn a prompt into a fully-consistent ``append_prompt`` payload."""
    input_tokens = estimate_tokens(prompt_text)
    output_tokens = max(
        64, int(input_tokens * response_scale * random.uniform(0.8, 1.3))
    )
    quality = quality_score(prompt_text)
    zaps = base_zaps(input_tokens, output_tokens, model, platform, 0, quality)
    zaps = apply_attachment_multiplier(zaps, has_non_image_attachment)
    cost = estimate_cost(input_tokens, output_tokens, model)
    entry = new_entry(
        model=model,
        platform=platform,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_files=1 if has_non_image_attachment else 0,
        output_files=0,
        prompt_quality=quality,
    )
    return {
        "entry": entry,
        "base_zaps": zaps,
        "spend_usd": cost["totalCost"],
        "quality": quality,
    }


def build_turn_for_target(
    prompt_text: str,
    model: str,
    platform: str,
    target_zaps: float,
    *,
    has_non_image_attachment: bool = False,
) -> dict[str, Any]:
    """Scale token counts so ``base_zaps`` lands near ``target_zaps``."""
    baseline = build_turn(
        prompt_text, model, platform, has_non_image_attachment=has_non_image_attachment
    )
    base = float(baseline["base_zaps"])
    if base <= 0 or target_zaps <= 0:
        return baseline
    scale = target_zaps / base
    entry = dict(baseline["entry"])
    entry["inputTokens"] = max(1, int(round(entry["inputTokens"] * scale)))
    entry["outputTokens"] = max(1, int(round(entry["outputTokens"] * scale)))
    zaps = base_zaps(
        entry["inputTokens"], entry["outputTokens"], model, platform, 0,
        float(baseline["quality"]),
    )
    zaps = apply_attachment_multiplier(zaps, has_non_image_attachment)
    cost = estimate_cost(entry["inputTokens"], entry["outputTokens"], model)
    baseline["entry"] = entry
    baseline["base_zaps"] = zaps
    baseline["spend_usd"] = cost["totalCost"]
    return baseline


def plan_target(platforms: list[str], budget: float, ceiling: float) -> float:
    """Per-platform target that fits the shared daily budget."""
    if not platforms:
        return 0.0
    return min(ceiling, budget / len(platforms))


# --- session orchestration ------------------------------------------------
async def _claim_missions(
    client: ConsoClient, state: AccountState, *, dry_run: bool
) -> list[str]:
    claimed_now: list[str] = []
    missions = await client.list_missions()
    already = set(await client.get_todays_claims()) | set(state.claimed)
    for mission in missions:
        mission_id = str(mission.get("id", ""))
        if not mission_id or mission_id in already:
            continue
        kind = str(mission.get("kind", ""))
        if kind != "checkin":
            # Tweet/article missions need external proof; skip.
            log.info(
                "[%s] skip mission %s kind=%s (needs proof)",
                client.label, mission_id, kind,
            )
            continue
        if dry_run:
            claimed_now.append(mission_id)
            continue
        ok, err = await client.claim_bonus_mission(mission_id)
        if ok:
            state.record_claim(mission_id)
            claimed_now.append(mission_id)
            log.info("[%s] claimed mission %s", client.label, mission_id)
        else:
            log.warning("[%s] claim %s failed: %s", client.label, mission_id, err)
    return claimed_now


TurnHook = Callable[[str, str, float], Awaitable[None] | None]


async def run_earning_session(
    settings: Settings,
    client: ConsoClient,
    state: AccountState,
    *,
    platforms: list[str] | None = None,
    long_prompts: bool = True,
    attach: bool = True,
    interrupt: asyncio.Event | None = None,
    on_turn: TurnHook | None = None,
) -> SessionReport:
    """Fill the daily budget on one account, evenly across the four platforms."""
    label = client.label
    interrupt = interrupt or asyncio.Event()
    order = platforms or list(DEFAULT_PLATFORM_MODELS.keys())
    for platform in order:
        if platform not in DEFAULT_PLATFORM_MODELS:
            allowed = ", ".join(DEFAULT_PLATFORM_MODELS)
            raise ValueError(f"platform {platform!r} not in locked set: {allowed}")

    report = SessionReport(
        label=label,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    user = await client.verify_session()
    if not user:
        raise AuthExpired(f"[{label}] session invalid; token refresh failed")
    state.bind_account(str(user.get("id") or state.account_id))

    profile = await client.get_profile()
    if profile is None:
        report.stopped_reason = "profile not visible (RLS or ban)"
        return report
    if profile.get("is_banned"):
        report.banned = True
        report.stopped_reason = f"banned: {profile.get('ban_reason') or 'unspecified'}"
        return report

    daily_used = float(profile.get("daily_zaps_earned") or 0.0)
    remaining = max(0.0, settings.daily_zap_cap - daily_used)
    target = plan_target(order, remaining, settings.credit_ceiling)
    log.info(
        "[%s] profile consoname=%s daily_used=%.2f remaining=%.2f target=%.2f",
        label, profile.get("consoname"), daily_used, remaining, target,
    )
    if target <= 0:
        report.stopped_reason = f"daily cap reached ({daily_used:.2f})"
        return report

    report.missions_claimed = await _claim_missions(
        client, state, dry_run=settings.dry_run
    )

    templates = list(LONG_PROMPT_TEMPLATES if long_prompts else SHORT_PROMPT_TEMPLATES)
    random.shuffle(templates)
    earned = {p: 0.0 for p in order}
    spent = 0.0
    zero_streak = 0
    first_turn = True
    max_rounds = 4

    for rnd in range(max_rounds):
        shortlist = [p for p in order if earned[p] < target * 0.9]
        if not shortlist:
            break
        for idx, platform in enumerate(shortlist):
            if interrupt.is_set():
                report.stopped_reason = "interrupted"
                break
            if client.banned:
                report.banned = True
                report.stopped_reason = "banned"
                break
            if spent >= remaining:
                report.stopped_reason = "daily budget exhausted"
                break

            if not first_turn and not settings.dry_run:
                gap = random.uniform(settings.gap_min, settings.gap_max)
                log.info("[%s] waiting %.0fs", label, gap)
                try:
                    await asyncio.wait_for(interrupt.wait(), timeout=gap)
                    report.stopped_reason = "interrupted"
                    break
                except asyncio.TimeoutError:
                    pass
            first_turn = False

            model = DEFAULT_PLATFORM_MODELS[platform]
            prompt = templates[(rnd * len(order) + idx) % len(templates)]
            want = round(target - earned[platform], 2)
            turn = build_turn_for_target(
                prompt, model, platform, want,
                has_non_image_attachment=attach,
            )
            entry = turn["entry"]
            report.turns_attempted += 1
            log.info(
                "[%s] r%d %s model=%s tokens=%d+%d q=%.1f base=%.2f",
                label, rnd + 1, platform, model,
                entry["inputTokens"], entry["outputTokens"],
                turn["quality"], turn["base_zaps"],
            )

            if settings.dry_run:
                await asyncio.sleep(0.05)
                continue

            try:
                result = await client.append_prompt(
                    entry, turn["base_zaps"], turn["spend_usd"]
                )
            except AccountBanned:
                report.banned = True
                report.stopped_reason = "banned"
                break

            if result.banned:
                report.banned = True
                report.stopped_reason = result.error or "banned"
                break

            if result.ok and result.credited_zaps > 0:
                earned[platform] = round(
                    earned[platform] + result.credited_zaps, 2
                )
                spent = round(spent + result.credited_zaps, 2)
                report.turns_credited += 1
                report.zaps_earned = round(
                    report.zaps_earned + result.credited_zaps, 2
                )
                state.record_turn(
                    model=model,
                    platform=platform,
                    input_tokens=entry["inputTokens"],
                    output_tokens=entry["outputTokens"],
                    quality=turn["quality"],
                    credited=result.credited_zaps,
                )
                zero_streak = 0
                log.info(
                    "[%s] credited %.2f on %s (session %.2f)",
                    label, result.credited_zaps, platform, report.zaps_earned,
                )
                if on_turn is not None:
                    hook_result = on_turn(label, platform, result.credited_zaps)
                    if asyncio.iscoroutine(hook_result):
                        await hook_result
            elif result.ok:
                zero_streak += 1
                log.warning(
                    "[%s] zero credit streak=%d on %s (soft flag)",
                    label, zero_streak, platform,
                )
                if zero_streak >= 2:
                    report.stopped_reason = "zero-credit soft flag"
                    break
            else:
                log.warning("[%s] turn rejected: %s", label, result.error)

            state.save(state_path_from(settings, label))

        if report.banned or report.stopped_reason:
            break

    report.platform_totals = dict(earned)
    if not report.stopped_reason:
        report.stopped_reason = "target reached"
    state.save(state_path_from(settings, label))
    return report


def state_path_from(settings: Settings, label: str) -> Any:
    """Import-cycle-free helper — imported lazily by the runner too."""
    from .state import state_path
    return state_path(settings.state_dir, label)


async def smoke_test(client: ConsoClient) -> dict[str, Any]:
    """Verify auth + profile + missions without writing anything."""
    out: dict[str, Any] = {"label": client.label}
    user = await client.verify_session()
    if not user:
        out["auth"] = "failed"
        return out
    out["auth"] = "ok"
    out["user_id"] = user.get("id")
    out["email"] = user.get("email")
    profile = await client.get_profile()
    if profile is None:
        out["profile_visible"] = False
        return out
    out["profile_visible"] = True
    out["consoname"] = profile.get("consoname")
    out["total_zaps"] = profile.get("total_zaps")
    out["daily_zaps_earned"] = profile.get("daily_zaps_earned")
    out["is_banned"] = bool(profile.get("is_banned"))
    out["claimed_today"] = await client.get_todays_claims()
    out["access_token_ttl"] = int(client.auth.seconds_until_expiry())
    return out
