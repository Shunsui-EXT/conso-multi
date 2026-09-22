"""Zap formula and prompt-quality scorer.

A faithful port of the extension bundle's client-side computation. The
Supabase RPC validates `p_base_zaps` against `p_entry`, so the caller MUST
compute the figure with these functions for the same entry.
"""

from __future__ import annotations

import math
import re
from typing import Any

# --- scalar constants lifted from the extension bundle --------------------
TOKENS_PER_BLOCK = 10_000
OUTPUT_WEIGHT = 2.5
MEDIA_WEIGHT = 0.5
DEFAULT_MULTIPLIER = 0.25
ATTACHMENT_MULTIPLIER = 3
CHARS_PER_TOKEN = 4

# Model quality multipliers.
MODEL_MULTIPLIER: dict[str, float] = {
    "auto": 0.3,
    "gpt-5-5-instant": 0.6,
    "gpt-5-5-thinking": 0.7,
    "gpt-5-6": 0.6,
    "gpt-5-6-thinking": 0.7,
    "claude-3-opus": 0.5,
    "claude-opus-4-6": 0.6,
    "claude-opus-4-7": 0.7,
    "claude-opus-4-8": 0.7,
    "claude-haiku-4-5": 0.3,
    "claude-sonnet-4-6": 0.4,
    "claude-sonnet-5": 0.7,
    "claude-opus-5": 0.7,
    "claude-fable-5": 1.4,
    "gemini-3-flash": 0.25,
    "gemini-3-pro": 0.56,
    "gemini-3.5-flash-lite": 0.1,
    "gemini-3.6-flash": 0.25,
    "gemini-3.6-thinking": 0.6,
    "gemini-3.1-pro": 0.7,
    "turbo": 0.4,
    "gpt56_terra": 0.6,
    "experimental": 0.3,
    "gemini37flash": 0.3,
    "claude50sonnet": 0.7,
    "kimik3thinking": 0.56,
    "glm_5_2": 0.7,
    "grok46low": 0.65,
    "nv_nemotron_3_ultra": 0.63,
    "pplx_asi": 0.5,
    "pplx_asi_opus": 0.7,
    "pplx_asi_glm": 0.7,
    "pplx_asi_deepseek_v4_pro": 0.67,
    "pplx_asi_kimi_k3": 0.56,
    "pplx_asi_grok_46": 0.63,
    "pplx_asi_fable_5": 1.4,
    "pplx_asi_gpt_56_sol": 0.67,
    "pplx_asi_sonnet": 0.7,
    "pplx_alpha": 0.45,
}

PLATFORM_MULTIPLIER: dict[str, float] = {
    "chatgpt": 0.3,
    "claude": 0.3,
    "gemini": 0.1,
    "perplexity": 0.3,
}

PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4": {"inputPerMillion": 5.0, "outputPerMillion": 25.0},
    "claude-sonnet-5": {"inputPerMillion": 3.0, "outputPerMillion": 15.0},
    "claude-haiku-4": {"inputPerMillion": 1.0, "outputPerMillion": 5.0},
    "gpt-5": {"inputPerMillion": 1.25, "outputPerMillion": 10.0},
    "gpt-4.1": {"inputPerMillion": 2.0, "outputPerMillion": 8.0},
    "gpt-4o": {"inputPerMillion": 2.5, "outputPerMillion": 10.0},
    "gpt-4o-mini": {"inputPerMillion": 0.15, "outputPerMillion": 0.6},
}
DEFAULT_PRICING = {"inputPerMillion": 3.0, "outputPerMillion": 15.0}

STRUCTURE_SIGNALS: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"\n"), 0.4, "multi_line"),
    (
        re.compile(r"\b(json|xml|csv|markdown|table|list)\b", re.IGNORECASE),
        0.6,
        "output_format",
    ),
)

_WORD_RE = re.compile(r"\b[a-z']+\b")


# --- primitives -----------------------------------------------------------
def round2(value: float) -> float:
    return round(value * 100) / 100


def resolve_multiplier(model: str, platform: str) -> float:
    """Model → platform → default (0.25)."""
    if model in MODEL_MULTIPLIER:
        return MODEL_MULTIPLIER[model]
    return PLATFORM_MULTIPLIER.get(platform, DEFAULT_MULTIPLIER)


def base_zaps(
    input_tokens: int,
    output_tokens: int,
    model: str,
    platform: str,
    media_tokens: int = 0,
    quality: float = 1.0,
) -> float:
    """Extension's base-zap formula (before attachment multiplier)."""
    mult = resolve_multiplier(model, platform)
    text = (input_tokens + output_tokens - media_tokens) / TOKENS_PER_BLOCK
    media = media_tokens / TOKENS_PER_BLOCK
    raw = text * quality * mult * OUTPUT_WEIGHT + media * quality * mult * MEDIA_WEIGHT
    return round2(raw)


def apply_attachment_multiplier(zaps: float, has_non_image_attachment: bool) -> float:
    """Non-image attachments triple the credited zaps."""
    return round2(zaps * ATTACHMENT_MULTIPLIER) if has_non_image_attachment else zaps


def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> dict[str, float]:
    pricing = PRICING.get(model, DEFAULT_PRICING)
    input_cost = input_tokens / 1e6 * pricing["inputPerMillion"]
    output_cost = output_tokens / 1e6 * pricing["outputPerMillion"]
    return {
        "inputCost": input_cost,
        "outputCost": output_cost,
        "totalCost": input_cost + output_cost,
    }


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


# --- quality scorer -------------------------------------------------------
def _tokenize_words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _length_score(words: list[str]) -> float:
    n = len(words)
    bands = (
        (3, 0.0),
        (6, 0.03),
        (12, 0.1),
        (20, 0.25),
        (35, 0.45),
        (60, 0.65),
        (100, 0.8),
        (150, 0.91),
        (250, 0.97),
    )
    for limit, score in bands:
        if n < limit:
            return score
    return 1.0


def _lexical_score(words: list[str]) -> float:
    if not words:
        return 0.0
    ttr = len(set(words)) / len(words)
    if ttr < 0.25:
        return 0.2
    if ttr < 0.4:
        return 0.5
    if ttr < 0.55:
        return 0.75
    if ttr < 0.7:
        return 0.9
    return 1.0


def _structure_score(text: str) -> float:
    total = 0.0
    for pattern, value, _label in STRUCTURE_SIGNALS:
        if pattern.search(text):
            total += value
    return min(total, 1.0)


def quality_score(prompt_text: str) -> float:
    """0.0–5.0 quality multiplier, mirroring the extension."""
    words = _tokenize_words(prompt_text)
    blended = (
        _length_score(words) * 0.6
        + _structure_score(prompt_text) * 0.3
        + _lexical_score(words) * 0.1
    )
    return round(min(1 + blended**0.45 * 4, 5), 1)


def describe_quality(prompt_text: str) -> dict[str, Any]:
    words = _tokenize_words(prompt_text)
    return {
        "length": _length_score(words),
        "structure": _structure_score(prompt_text),
        "lexical": _lexical_score(words),
        "quality": quality_score(prompt_text),
        "words": len(words),
    }
