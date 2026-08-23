"""pricing.py — key mapping, the exact CC cost reproduction, tiers, discount."""

import pytest

from harness_telemetry.normalize import pricing


def test_key_mapping() -> None:
    assert pricing._key("claude-opus-4-8") == "opus-4-8"
    assert pricing._key("claude-opus-4-5-20260101") == "opus-4-5"
    assert pricing._key("claude-opus-4-1") == "opus"
    assert pricing._key("claude-sonnet-5") == "sonnet"  # sticker $3/$15 via the generic record
    assert pricing._key("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert pricing._key("claude-3-5-haiku") == "haiku"
    # Claude 5 family: rates from Anthropic's published list prices. Claude Code
    # ships no inline price literals for them, so the cost_usd vs
    # cost_usd_reported cross-check arbitrates once a capture of one exists.
    assert pricing._key("claude-fable-5") == "fable-5"
    assert pricing._key("claude-mythos-5") == "mythos-5"
    # Unpriced: non-Claude models stay None (never fabricate a cost).
    assert pricing._key("z-ai/glm-5.2") is None
    assert pricing._key(None) is None


def test_fable_5_rates() -> None:
    # $10/$50 per MTok; cache = input x {0.1 read, 1.25 write-5m, 2.0 write-1h};
    # NO fast tier for the Claude 5 family (fast mode is Opus 4.8/4.7 only ->
    # speed=fast falls back to the standard record).
    r = pricing.rates_for("claude-fable-5")
    assert r == {
        "input": 10,
        "output": 50,
        "cache_read": 1,
        "cache_write_5m": 12.5,
        "cache_write_1h": 20,
        "web_search": 0.01,
    }
    assert pricing.rates_for("claude-fable-5", speed="fast") == r
    c = pricing.cost_usd(
        "claude-fable-5", {"input_tokens": 1_000_000, "output_tokens": 100_000}, discount=0.0
    )
    assert c is not None and c == pytest.approx(15.0)  # 10 + 5


def test_reproduces_cc_cost_to_the_microdollar() -> None:
    # Two token buckets observed in a session of the maintainer's (no content),
    # against Claude Code's own cost estimate (Opus 4.8, 1h cache TTL; the rows
    # written up in docs/cost.md): derived cost must equal the cost_usd CC
    # reported for them, to the micro-dollar.
    u1 = {
        "input_tokens": 3108,
        "output_tokens": 145,
        "cache_read_input_tokens": 14162,
        "cache_creation_input_tokens": 4779,
        "cache_creation": {"ephemeral_1h_input_tokens": 4779, "ephemeral_5m_input_tokens": 0},
    }
    c1 = pricing.cost_usd("claude-opus-4-8", u1, discount=0.0)
    assert c1 is not None and round(c1, 6) == 0.074036

    u2 = {
        "input_tokens": 2,
        "output_tokens": 20,
        "cache_read_input_tokens": 18941,
        "cache_creation_input_tokens": 3262,
        "cache_creation": {"ephemeral_1h_input_tokens": 3262, "ephemeral_5m_input_tokens": 0},
    }
    c2 = pricing.cost_usd("claude-opus-4-8", u2, discount=0.0)
    assert c2 is not None and round(c2, 6) == 0.042600


def test_cache_write_ttl_split() -> None:
    # 1h tokens price at 2x input; the remainder of cache_creation prices at 1.25x.
    u = {
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 400_000},
    }
    c = pricing.cost_usd("claude-opus-4-8", u, discount=0.0)
    assert c == pytest.approx(0.4 * 10 + 0.6 * 6.25)


def test_fast_tier_keys_on_speed() -> None:
    u = {"input_tokens": 1_000_000, "speed": "fast"}
    assert pricing.cost_usd("claude-opus-4-8", u, discount=0.0) == pytest.approx(10.0)
    # speed passed explicitly wins over usage
    assert pricing.cost_usd("claude-opus-4-8", u, speed="standard", discount=0.0) == pytest.approx(
        5.0
    )
    # models without a fast record fall back to standard
    assert pricing.cost_usd("claude-haiku-4-5", u, discount=0.0) == pytest.approx(1.0)


def test_unpriced_returns_none_not_zero() -> None:
    u = {"input_tokens": 1000, "output_tokens": 1000}
    assert pricing.cost_usd("z-ai/glm-5.2", u) is None
    assert pricing.cost_usd("claude-opus-4-8", "not-a-dict") is None


def test_discount_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    u = {"input_tokens": 1_000_000}
    monkeypatch.setenv("HARNESS_COST_DISCOUNT", "0.15")
    assert pricing.cost_usd("claude-opus-4-8", u) == pytest.approx(5.0 * 0.85)
    # explicit discount overrides the env
    assert pricing.cost_usd("claude-opus-4-8", u, discount=0.0) == pytest.approx(5.0)
    # junk / out-of-range env values fall back to list price
    monkeypatch.setenv("HARNESS_COST_DISCOUNT", "1.5")
    assert pricing.cost_usd("claude-opus-4-8", u) == pytest.approx(5.0)
    monkeypatch.setenv("HARNESS_COST_DISCOUNT", "nope")
    assert pricing.cost_usd("claude-opus-4-8", u) == pytest.approx(5.0)


def test_web_search_priced_per_request() -> None:
    u = {"server_tool_use": {"web_search_requests": 3}}
    assert pricing.cost_usd("claude-sonnet-5", u, discount=0.0) == pytest.approx(0.03)
