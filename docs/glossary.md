# Glossary

The vocabulary used across this repo's docs, the schema, and the normalizer. Terms are ordered roughly from the outside in: what we observe, what we store, what we check.

## capture surface

Everything a harness exposes about its own session, across every channel it offers, at whatever fidelity it offers it. The subject of this project: not "what did the agent do", but "what can be *observed* about what the agent did, and how completely".

## channel

One independent route by which a harness exposes session data. Five are catalogued here — transcript, OTEL, hooks, API proxy, statusline — plus the raw request/response **bodies**, which are the OTEL channel's file sink but are treated as a channel of their own; they are **additive** — each one that is present fills more of the picture, and a missing one thins the result rather than breaking it. Four are **ingested** (an adapter reads them and they contribute events to a normalized document): bodies, transcript, OTEL and hooks. Two are **catalogued only** (API proxy, statusline) — described here, understood, and deliberately not consumed.

- **transcript** *(ingested)* — the session log the harness writes to local disk on its own (for Claude Code, JSONL under `~/.claude/projects/<cwd>/<session-uuid>.jsonl`, plus a sibling tree for sub-agents). Sole source of the message tree/DAG.
- **OTEL** *(ingested)* — OpenTelemetry metrics and events the harness emits when telemetry is enabled. Sole source of per-inference latency, tool-decision provenance, and compaction/error events.
- **hooks** *(ingested)* — lifecycle callbacks the harness invokes at named points (tool pre/post, prompt submit, session start, compaction boundaries, …). Captured here by injecting a passive listener that records each payload verbatim. See [`hooks.md`](hooks.md).
- **proxy** *(catalogued only)* — interposing on the model HTTP traffic itself (via `ANTHROPIC_BASE_URL` or equivalent). An alternative route to full request bodies; not used by this project, since Claude Code can emit them natively.
- **statusline** *(catalogued only)* — the JSON snapshot Claude Code pipes to a configured `statusLine` command on each render. Carries the session-cumulative figures the harness displays (estimated cost, durations, lines changed, context-window totals), computed in memory and never written to disk — so a statusline command is the only way to observe them.

## bodies

*(ingested — the fourth ingested channel alongside transcript, OTEL and hooks.)* The raw, untruncated Messages API request and response JSON, exactly as assembled and received (`OTEL_LOG_RAW_API_BODIES=file:<dir>` writes them as `*.request.json` / `*.response.json`). Bodies are the only source of the assembled system prompt, the tool schemas, the byte-exact request, and the `cache_control` breakpoints — everything a transcript is too high-level to record. In the normalized document they are referenced by hash and kept in a separate audit ring, never inlined.

## enriched transcript

What a capture becomes: `sessions/<ts>/normalized.json` plus its projected `sessfs/` tree. "Enriched" relative to the harness's own transcript, which carries the message tree but not the assembled context, the token usage, the pairing, the per-inference cost, or the harness's own lifecycle events. The enriched transcript carries all of those in one document, reduced to the vocabulary in `schema/events.schema.json`. It is a **semantic re-encoding** of the capture, not a redacted or summarized view of it — everything sensitive in the capture is in it too.

## sessfs

The session-filesystem projection: `harness-project` renders a normalized document into a plain directory tree — `objects/` (one file per content-addressed block), `log/` (the event log), and `inferences/NNNN/{manifest.json, context/}` where the context entries are relative symlinks back into `objects/`. It is the shape an unreleased companion project (session-fs) models a session as, materialized with files and symlinks so it can be browsed, diffed, and verified without any special tooling. `--verify` round-trips the tree back to the document it came from.

## audit ring

The part of a normalized document that keeps the raw evidence separate from the interpreted story: `raw_bodies` is an index keyed by each body's sha-256, recording whether it was a request or a response, its length, its server request id, and which capture holds it — so every claim in the event log can be traced back to a specific file on disk, and conservation can be checked over the union of several captures. Bodies are referenced by hash from the events and never inlined into them; the ring is what makes "referenced by hash" resolvable rather than decorative.

## prelude

The stable, cache-oriented head of a request: the `system` blocks, the tool definitions, and the sampling parameters — as opposed to the conversation `messages[]` that grow turn by turn. Two calls with the same prelude are the same *kind* of call (main thread, a sub-agent of some type, a background call).

## lineage

A set of inferences that share a prelude — one conversational thread within a session. `lineage_id` is the prelude's hash, so it identifies the *kind* of thread, not a particular one. Note that this is a derived grouping, not something the harness reports: its own agent and sidechain flags under-report, and a prelude can churn mid-thread across harness versions, so the authoritative structure is the request chain (each request names its predecessor's response) rather than the hash alone.

## instance (chain component)

One concrete run of a lineage. Where `lineage_id` says *what kind of thread this is*, `instance_id` says *which one* — it is the identity of the connected component in the request chain, taken from its root call. Two same-type sub-agents running in parallel share a `lineage_id` and get distinct `instance_id`s; a session that is resumed keeps one `instance_id` across both captures, because it is one logical thread.

## conservation

The check that the output accounts for every input, run by `harness-normalize --check` against the capture directory: every request and response body file is present in the document, unmatched ones are named rather than silently dropped, references resolve, and the merged case is checked over the union of all captures. This is deliberately stronger than schema validation — a schema validates whatever story the normalizer chose to tell, so it cannot see a dropped input. See the method note in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## round-trip

The reconstruction oracle: `harness-validate --roundtrip` rebuilds every request and response **from the normalized document alone** — no access to the capture — and semantically diffs the result against the raw body it came from. It is what makes the content-addressed store trustworthy rather than merely plausible. What it proves is **semantic** equivalence, not byte equality: a short, documented list of differences is normalized away on both sides before the comparison (key order and whitespace, the request's `metadata`, the withheld thinking text, and a string-form system prompt versus the equivalent single text block). Anything else that differs is a real capture gap and fails the check.

## compaction

A context edit with provenance, never a deletion. When a session's context is compacted, the originals stay in the object store and the event log, and a `compaction` event records what was replaced by what — with the trigger (automatic or manual) and the pre/post token counts where the harness reports them.

## background call

An inference the harness makes on its own behalf rather than to answer the user: in interactive Claude Code sessions, conversation title generation and next-prompt suggestion. They are real, billable API calls that share the session, they are classified per call (a suggestion call carries the main thread's whole history), and a suggestion is routinely cancelled — leaving a request with no response, forever. Any honest per-session cost or call count has to include them.
