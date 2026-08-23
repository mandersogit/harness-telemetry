# Cost — derived from tokens, not read from the API

Every dollar figure Claude Code shows you is an **estimate it computed itself, at list price**. The API never sent it a price. That is not a criticism of the estimate — it reproduces exactly, to the micro-dollar, from published rates — but it does mean the number answers "what would this have cost at sticker price?" and not "what will I be invoiced?". If you are on a subscription plan you are not billed per token at all; if you are on a negotiated contract, the discount is applied downstream of anything the client can see.

This project therefore treats **token counts as the ground truth** and derives cost itself, keeping the harness's own figure only as a cross-check. This document explains why, what the derived numbers mean, how to apply your own rate, and how to refresh the price table when Anthropic changes prices.

## The API delivers no price

Across every captured response body, the `usage` object contains token counts and nothing resembling money (*observed in captures*). The fields observed are `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` (plus a `cache_creation` breakdown into `ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`), `server_tool_use`, `service_tier`, `speed`, `inference_geo` and `iterations`. A search for any `cost` / `price` / `usd` / `discount` / `tier` key across all captured response bodies returns nothing.

So cost cannot be *read* from the API. It can only be *computed*, from tokens and a rate table — and whoever computes it chooses the rate table.

## Claude Code computes it client-side, at list price

Claude Code computes cost from the response's token counts against a list-price table, and the same computed value feeds every surface it shows (*observed behaviour of the Claude Code client, 2.1.196* — the cost function and its price table were read out of that build; the surfaces below were also seen carrying the same number in captures):

- the OpenTelemetry `api_request` event's `cost_usd` (per inference),
- the `claude_code.cost.usage` metric,
- the statusline's `cost.total_cost_usd` — which is just the running session sum, held in memory and never written to disk by Claude Code. (The statusline documentation says outright that the figure "may differ from your actual bill".)

The reproduction is exact. Taking the token counts observed in a session of the maintainer's (no content) for two Opus 4.8 inferences and multiplying by published rates — `$5` in, `$25` out, `$0.50` cache read, `$10` cache write at 1 h — matches the reported `cost_usd` to the last digit:

| input | output | cache read | cache write (1 h) | computed | reported `cost_usd` |
| --- | --- | --- | --- | --- | --- |
| 3108 | 145 | 14162 | 4779 | 0.074036 | 0.074036 |
| 2 | 20 | 18941 | 3262 | 0.042600 | 0.042600 |

That exactness is what makes the reported number useful as a **cross-check oracle**: any gap between it and a derivation means the two price tables have drifted apart, which is worth knowing.

## No discount, plan, or tier enters that path

There is no discount factor, contract multiplier, plan adjustment, or tier lookup anywhere in the computation (*observed behaviour of the Claude Code client, 2.1.196*). The only price variation is a `speed: "fast"` branch (below), and that is driven by a **server-returned** field describing the request's lane, not by anything about your account. Your plan *is* reported alongside as metadata (`subscription_type`), but it is not an input to the arithmetic.

Two consequences worth being explicit about:

- **On a subscription plan (Pro / Max), you are not billed per token.** The `cost_usd` you see is a notional list-price figure for work that carries no per-token charge. It is emitted anyway.
- **Under a negotiated discount, the figure overstates your actual invoiced cost** by roughly the discount. The discount is a billing-side term; the client never sees it, so it cannot apply it.

Claude Code does have exactly one runtime price source — `additional_model_costs`, delivered in an account/org-scoped bootstrap response and cached to disk — but it is consulted **only after** the built-in table (*observed behaviour of the Claude Code client, 2.1.196*). It can therefore price a custom or gateway model the table does not know, and it structurally **cannot override first-party Claude list prices**. Even that dynamic hook cannot express your contract.

## Fast (priority) mode is a separate rate

A `speed: "fast"` request prices against its own record, not a multiplier on the standard one. For Opus 4.8 that is `$10/$50` against `$5/$25` — 2×. Correct derivation therefore keys on **(model, speed)**, never on model alone; `speed` travels in the response `usage`, so it is captured, and `pricing.cost_usd()` reads it from there unless a caller passes `speed=` explicitly. Not every model has a fast tier: those that do not fall back to their standard record.

## What this project does instead

`src/harness_telemetry/normalize/pricing.py` derives cost from the response's token buckets:

```text
cost = input_tokens      /1e6 * input
     + output_tokens     /1e6 * output
     + cache_read_tokens /1e6 * cache_read
     + cache_write_1h    /1e6 * cache_write_1h
     + cache_write_5m    /1e6 * cache_write_5m
     + web_search_requests * web_search
   , all times (1 - discount)
```

Cache-creation tokens are split into the 1 h and 5 m buckets using the response's own `cache_creation` breakdown (clamped so the 1 h count can never exceed the total). `web_search` is a flat per-request charge.

In the normalized document that lands as two distinct fields on every inference:

| Field | Meaning |
| --- | --- |
| `cost_usd` | **Derived** here, from tokens × the table × `(1 - discount)`. Null when the model is unpriced. |
| `cost_usd_reported` | Claude Code's own OTEL figure, kept verbatim as a cross-check. Null without the OTEL channel. |

`harness-normalize --check` prints `total_cost_usd` (the sum of derived costs) and `cost_xcheck_max_delta` (the largest absolute gap between the two fields across inferences). At zero discount that delta should be `0.0` — it was, on a capture carrying bodies, transcript and OTEL together, confirming the derivation reproduces the client-side math. A non-zero delta at zero discount means the tables have drifted and `pricing.py` needs a refresh.

The practical payoff of deriving rather than ingesting: cost no longer depends on the OTEL channel at all. A capture with no collector attached still gets a per-inference cost, because the response bodies carry the tokens.

**Unpriced models yield null, never a guess.** A model id the table does not recognise — a non-Claude model routed through Claude Code, for example — makes `cost_usd()` return `None`, and the inference carries `cost_usd: null`. Nothing is fabricated, and the null is visible in `inferences_with_cost`.

## The price table

Reproduced from `src/harness_telemetry/normalize/pricing.py`, which is the source of truth — if these disagree with the code, the code is right. Rates are **USD per million tokens**, from Anthropic's published pricing page at `platform.claude.com` (*official docs*). The dates below are the "checked on" dates recorded in the code's own comments; they are not a guarantee that the published rates have not moved since.

For every Claude model the cache rates follow one formula: **cache read = 0.1 × input, 5-minute write = 1.25 × input, 1-hour write = 2 × input**. The table spells them out rather than computing them at runtime, so drift is visible in a diff.

**Standard rates** (checked 2026-06-30; the Claude 5 rows checked 2026-07-02):

| Pricing key | Matches model ids containing | input | output | cache read | write 5m | write 1h |
| --- | --- | --- | --- | --- | --- | --- |
| `fable-5` | `fable-5` | 10 | 50 | 1 | 12.5 | 20 |
| `mythos-5` | `mythos-5` | 10 | 50 | 1 | 12.5 | 20 |
| `opus-4-8` | `opus-4-8` | 5 | 25 | 0.5 | 6.25 | 10 |
| `opus-4-7` | `opus-4-7` | 5 | 25 | 0.5 | 6.25 | 10 |
| `opus-4-6` | `opus-4-6` | 5 | 25 | 0.5 | 6.25 | 10 |
| `opus-4-5` | `opus-4-5` | 5 | 25 | 0.5 | 6.25 | 10 |
| `opus` | any other `opus` (4.0 / 4.1) | 15 | 75 | 1.5 | 18.75 | 30 |
| `sonnet` | any `sonnet` | 3 | 15 | 0.3 | 3.75 | 6 |
| `haiku-4-5` | `haiku-4-5` | 1 | 5 | 0.1 | 1.25 | 2 |
| `haiku` | any other `haiku` (3 / 3.5) | 0.8 | 4 | 0.08 | 1 | 1.6 |

**Fast (priority-lane) rates**, used only when the response reports `speed: "fast"`:

| Pricing key | input | output | cache read | write 5m | write 1h |
| --- | --- | --- | --- | --- | --- |
| `opus-4-8` | 10 | 50 | 1 | 12.5 | 20 |
| `opus-4-7` | 30 | 150 | 3 | 37.5 | 60 |
| `opus-4-6` | 30 | 150 | 3 | 37.5 | 60 |

No other model has a fast record; any other model at `speed: "fast"` prices at its standard rate. `web_search` is `$0.01` per request for every model in both tables.

Model ids are matched by substring, most specific first, so a dated or vendor-prefixed id (`claude-opus-4-8-20260101`, say) resolves to the same record as the bare name. Anything that matches none of the keys is unpriced.

Two notes carried from the code, both of which the cross-check will arbitrate on real data:

- The Claude 5 rows were taken from the published documentation rather than from the harness, because Claude Code 2.1.198 no longer ships inline price literals for these models — its price records appear to be fed at runtime (*observed behaviour of the Claude Code client, 2.1.198*). The first real Claude 5 capture will show a `cost_xcheck_max_delta` of `0.0` if they agree.
- `claude-sonnet-5` resolves to the generic `sonnet` record at `$3/$15`, its sticker list rate. A promotional rate would be a billing-side term and would surface, loudly, as a cross-check delta.

## Applying your own rate: `HARNESS_COST_DISCOUNT`

If you have a negotiated rate, you can make the derived numbers reflect it. The discount is a **fraction in `[0, 1)`** applied as a final multiplier `(1 - discount)` to the whole derived dollar amount — so `0.10` means "bill at 90% of list".

Two ways to set it, with a strict precedence:

| Mechanism | Applies to | Notes |
| --- | --- | --- |
| `--discount 0.10` | `harness-normalize`, `harness-watch` | Explicit per-run value; wins over the environment variable. |
| `HARNESS_COST_DISCOUNT=0.10` | every derivation, incl. `harness-capture`'s finalize | The default when no `--discount` was passed. |

Exact semantics, as implemented:

- **The two mechanisms both reject a bad value, but differently.** `HARNESS_COST_DISCOUNT` is parsed as a float and a value that does not parse, or that falls outside `[0, 1)` — negative, or `1.0` and above — is **silently clamped to `0.0`**, i.e. list price: there is no error and no warning, so check your value if the totals look unchanged. `--discount` is **validated on the command line and rejects an out-of-range or unparseable value with an error** rather than running at list price behind your back.
- Passing `--discount` explicitly overrides the environment variable, including `--discount 0`, which forces list price even when `HARNESS_COST_DISCOUNT` is set.
- The discount applies **only to the derived `cost_usd`** (and therefore to `total_cost_usd`). `cost_usd_reported` is always the harness's untouched list-price figure.
- `harness-capture` has no flags of its own — everything after the command name goes to `claude` verbatim — so the environment variable is the only way to set a discount for its automatic finalize step.

A consequence worth anticipating: **with a discount set, `cost_xcheck_max_delta` is no longer zero**, and should not be. The gap between derived and reported cost *is* the discount, made explicit on the largest single inference. To verify the derivation itself, run the check once with `--discount 0`.

```sh
# derive at list price and confirm the check is exact
harness-normalize captures/<id> --check --discount 0

# then apply your contracted rate
HARNESS_COST_DISCOUNT=0.10 harness-normalize captures/<id> --out sessions/<id>/normalized.json
```

What a discount cannot do is make the number an invoice. It rescales list prices by a flat factor; real agreements can vary by model, by term, by commitment tier, and can include credits and minimums. Treat the output as "list-price-equivalent usage, scaled" — a good relative measure across sessions and models, and a poor substitute for billing.

## Refreshing the rates when prices change

The table is deliberately a plain literal, so keeping it current is a small, auditable diff:

1. Read the current rates from Anthropic's published pricing page (`platform.claude.com`). Take input and output; the cache rates follow the 0.1 / 1.25 / 2.0 formula unless the page says otherwise.
2. Edit `_STANDARD` (and `_FAST`, if a priority-lane rate changed) in `src/harness_telemetry/normalize/pricing.py`, and update the "checked" date in the comment above the block you touched. Add a new key only when a model does not already resolve by substring to a correct record.
3. Run the unit tests: `local.venv/bin/pytest tests/test_pricing.py`. They pin the key-resolution order, the fast tier, the cache split, the discount semantics, and one exact reproduction.
4. Cross-check against a fresh capture on the affected model:

   ```sh
   harness-capture -p "hello" --model <the model>
   harness-normalize captures/<id> --check --discount 0
   ```

   `cost_xcheck_max_delta: 0.0` means your table and the harness's agree. A non-zero delta means one of them is stale — and since the harness's figure is its own client-side estimate, check the published page to decide which.

Re-run the check whenever you upgrade Claude Code, too: the harness's estimate is the oracle here, and it moves when the harness does.

## Verified / inferred

**Verified first-hand (2026-06-29 / 2026-06-30, Claude Code 2.1.195–2.1.198):**

- The response `usage` object carries token counts only — no price, cost, discount or tier field — across every captured fixture.
- Claude Code's reported `cost_usd` reproduces from the captured `usage` and published list prices **to the micro-dollar**, including the 1 h vs 5 m cache-write split.
- No discount, plan or tier factor exists in that computation; the only price variation is the server-reported `speed` lane. `additional_model_costs` is consulted only after the built-in table and cannot override first-party Claude prices.
- Deriving cost from tokens reproduces the harness's figure exactly at zero discount (`cost_xcheck_max_delta: 0.0` on a capture carrying bodies, transcript and OTEL together), yields per-inference cost on a capture with no OTEL channel at all, and returns null for an unpriced non-Claude model rather than inventing one.
- With a discount applied, the total scales by exactly `(1 - discount)` and the cross-check delta becomes the discount gap.

**Documentation-sourced:** the published per-MTok rates themselves (checked on the dates recorded in `pricing.py`: 2026-06-30, and 2026-07-02 for the Claude 5 family); the statusline payload's `cost.total_cost_usd` and its "may differ from your actual bill" caveat.

**Inferred:** that a promotional or introductory rate, if the harness's estimate used one, would surface as a cross-check delta — that is the mechanism working as designed, not an observation. The Claude 5 rows have not yet been arbitrated against a real capture on those models.
