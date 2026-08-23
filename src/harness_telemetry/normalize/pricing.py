#!/usr/bin/env python3
"""
pricing.py — derive per-inference cost from token usage.

WHY WE COMPUTE COST OURSELVES (don't trust the harness's number)
----------------------------------------------------------------
Claude Code reports a `cost_usd` (OTEL `api_request` event) and a session
`total_cost_usd` (statusline), but both are computed CLIENT-SIDE from a
list-price table — the Anthropic Messages API response carries ONLY token counts,
never a price (verified: the response `usage` object has no cost/price/discount/
tier field; and CC's number reproduces to the micro-dollar from list price x token
buckets — see docs/cost.md). Consequences:

  - On a subscription plan (Pro/Max) you are NOT billed per token; the reported
    cost is a notional list-price figure, not real spend.
  - Any billing-side term that reduces the invoiced rate (a negotiated rate, a
    promo, a plan credit) is invisible to the client (verified: no discount/tier
    factor exists in CC's cost path, and its one runtime price hook cannot
    override first-party Claude prices). So CC's cost OVERSTATES actual invoiced
    cost by whatever that term is worth — which is why the adjustment here is a
    user-supplied discount fraction, not something the capture can discover.

Therefore we treat token counts (which the API really does deliver, and which we
capture in bodies/transcript/OTEL) as ground truth and DERIVE cost here, keyed on
(model, speed), times an explicit, user-supplied discount factor. CC's reported
number is kept only as a cross-check (inference.cost_usd_reported).

THE TABLE
---------
Rates are USD per MILLION tokens: Anthropic's published list prices (the pricing
page at platform.claude.com; checked 2026-06-30). Claude Code's own client-side
cost estimate reproduces from these same rates to the micro-dollar, which is what
makes its reported number a usable cross-check oracle (`cost_usd_reported`). For
every Claude model the cache rates follow input x {0.1 read, 1.25 write-5m, 2.0
write-1h}. A `speed: "fast"` (priority-lane) request prices at a SEPARATE, higher
record — for Opus 4.8 that is $10/$50 (2x standard), so cost must key on speed,
not model alone. `web_search` is USD per request. Unknown / non-Claude models
(e.g. GLM via OpenRouter) are unpriced -> cost_usd() returns None rather than a
fabricated number.

stdlib only; no I/O apart from reading one env var. `discount` is a user-supplied
discount fraction in [0, 1), applied as (1 - discount): 0.10 => bill x0.90. It is
overridable per call, via --discount, or via the HARNESS_COST_DISCOUNT env var;
parse_discount() is the single rule all three go through.
"""

import os
from typing import Any

# USD per million tokens, per published list-price record (input, output,
# cache_read, cache_write_5m, cache_write_1h, web_search-per-request).
#
# Claude 5 family (checked 2026-07-02): published list prices — $10/$50 for
# fable-5/mythos-5, cache = input x {0.1, 1.25, 2.0} like every Claude model;
# NO fast tier (fast mode is Opus 4.8/4.7 only). Claude Code 2.1.198 no longer
# ships inline price literals for these models (its price records are fed at
# runtime), so the cross-check oracle (cost_usd vs the reported cost -> 0.0)
# arbitrates on the first real Claude-5 capture.
# claude-sonnet-5: $2/$10 (checked 2026-08-23 against the published pricing
# page — the launch "intro through 2026-08-31" rate was made the standard price;
# the scheduled increase to $3/$15 was cancelled). Cache = input x {0.1, 1.25,
# 2.0} as usual. Sonnet <= 4.6 stays on the generic "sonnet" record at $3/$15.
_STANDARD: dict[str, dict[str, float]] = {
    "fable-5": {
        "input": 10,
        "output": 50,
        "cache_read": 1,
        "cache_write_5m": 12.5,
        "cache_write_1h": 20,
        "web_search": 0.01,
    },
    "mythos-5": {
        "input": 10,
        "output": 50,
        "cache_read": 1,
        "cache_write_5m": 12.5,
        "cache_write_1h": 20,
        "web_search": 0.01,
    },
    "opus-4-8": {
        "input": 5,
        "output": 25,
        "cache_read": 0.5,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10,
        "web_search": 0.01,
    },
    "opus-4-7": {
        "input": 5,
        "output": 25,
        "cache_read": 0.5,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10,
        "web_search": 0.01,
    },
    "opus-4-6": {
        "input": 5,
        "output": 25,
        "cache_read": 0.5,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10,
        "web_search": 0.01,
    },
    "opus-4-5": {
        "input": 5,
        "output": 25,
        "cache_read": 0.5,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10,
        "web_search": 0.01,
    },
    "opus": {
        "input": 15,
        "output": 75,
        "cache_read": 1.5,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30,
        "web_search": 0.01,
    },
    "sonnet-5": {
        "input": 2,
        "output": 10,
        "cache_read": 0.2,
        "cache_write_5m": 2.5,
        "cache_write_1h": 4,
        "web_search": 0.01,
    },
    "sonnet": {
        "input": 3,
        "output": 15,
        "cache_read": 0.3,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6,
        "web_search": 0.01,
    },
    "haiku-4-5": {
        "input": 1,
        "output": 5,
        "cache_read": 0.1,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2,
        "web_search": 0.01,
    },
    "haiku": {
        "input": 0.8,
        "output": 4,
        "cache_read": 0.08,
        "cache_write_5m": 1,
        "cache_write_1h": 1.6,
        "web_search": 0.01,
    },
}
# `speed: "fast"` priority-lane records — the published priority-tier list prices.
# Only these models have a fast tier; any other model at speed=fast falls back to
# its standard rate.
_FAST: dict[str, dict[str, float]] = {
    "opus-4-8": {
        "input": 10,
        "output": 50,
        "cache_read": 1,
        "cache_write_5m": 12.5,
        "cache_write_1h": 20,
        "web_search": 0.01,
    },
    "opus-4-7": {
        "input": 30,
        "output": 150,
        "cache_read": 3,
        "cache_write_5m": 37.5,
        "cache_write_1h": 60,
        "web_search": 0.01,
    },
    "opus-4-6": {
        "input": 30,
        "output": 150,
        "cache_read": 3,
        "cache_write_5m": 37.5,
        "cache_write_1h": 60,
        "web_search": 0.01,
    },
}


def _key(model: str | None) -> str | None:
    """Normalize a model id to a pricing key, or None if unpriced (non-Claude)."""
    m = (model or "").lower()
    for k in (
        "fable-5",
        "mythos-5",
        "sonnet-5",
        "opus-4-8",
        "opus-4-7",
        "opus-4-6",
        "opus-4-5",
        "haiku-4-5",
    ):
        if k in m:
            return k
    if "opus" in m:
        return "opus"  # 4.0 / 4.1 ($15/$75)
    if "sonnet" in m:
        return "sonnet"  # <= 4.6 ($3/$15)
    if "haiku" in m:
        return "haiku"  # 3 / 3.5 ($0.80/$4)
    return None


def rates_for(model: str | None, speed: str | None = None) -> dict[str, float] | None:
    """Per-MTok rate record for (model, speed), or None if the model is unpriced."""
    k = _key(model)
    if k is None:
        return None
    if speed == "fast" and k in _FAST:
        return _FAST[k]
    return _STANDARD.get(k)


def parse_discount(value: str) -> float:
    """The ONE discount rule: a float in [0, 1), applied as (1 - discount).

    Raises ValueError on anything else — junk, a negative fraction, or >= 1
    (which would zero or invert every cost). Callers that must not fail (the env
    var default) catch it; callers that should reject loudly (the --discount CLI
    flag) let it surface.
    """
    d = float(value)  # ValueError on junk
    if not (0.0 <= d < 1.0):
        raise ValueError(
            f"discount must be a fraction in [0, 1) (e.g. 0.10 = bill x0.90), got {value!r}"
        )
    return d


def _discount_default() -> float:
    """HARNESS_COST_DISCOUNT, or 0 (list price) when unset/invalid."""
    raw = os.environ.get("HARNESS_COST_DISCOUNT", "")
    if not raw:
        return 0.0
    try:
        return parse_discount(raw)
    except ValueError:
        return 0.0


def cost_usd(
    model: str | None,
    usage: Any,
    *,
    speed: str | None = None,
    discount: float | None = None,
) -> float | None:
    """Derive cost in USD from an API `usage` dict (raw API field names).

    Reproduces Claude Code's client-side computation: tokens/1e6 x per-MTok rate,
    splitting cache-creation tokens into 1h vs 5m by usage.cache_creation. Returns
    None when the model is unpriced (so callers don't fabricate a cost). `discount`
    is a user-supplied fraction in [0, 1) applied as (1 - discount); defaults to
    the HARNESS_COST_DISCOUNT env var, else 0 (list price).
    """
    if not isinstance(usage, dict):
        return None
    rates = rates_for(model, speed if speed is not None else usage.get("speed"))
    if rates is None:
        return None
    if discount is None:
        discount = _discount_default()

    def g(k: str) -> float:
        v: float = usage.get(k) or 0
        return v

    cw = g("cache_creation_input_tokens")
    cw1h_raw: float = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    cw1h = min(cw1h_raw, cw)
    cw5m = cw - cw1h
    web: float = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    dollars = (
        g("input_tokens") / 1e6 * rates["input"]
        + g("output_tokens") / 1e6 * rates["output"]
        + g("cache_read_input_tokens") / 1e6 * rates["cache_read"]
        + cw1h / 1e6 * rates["cache_write_1h"]
        + cw5m / 1e6 * rates["cache_write_5m"]
        + web * rates["web_search"]
    )
    return dollars * (1.0 - discount)
