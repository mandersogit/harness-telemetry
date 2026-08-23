# harness-telemetry — project guide

Comparative study of how agentic coding harnesses (Claude Code, codex-cli, pi, …) expose their own session internals — the *capture surface*: what each emits, through which channels, with what fidelity. The concrete artifact is a normalizer that turns a captured session into the event vocabulary in `schema/events.schema.json`. See `README.md` for scope and `docs/` for the per-harness write-ups.

## How to work here (most important)

- **Verify, don't infer — especially for Claude/Anthropic API and Claude Code internals.** This repo's findings have repeatedly contradicted plausible-sounding priors. Ground every claim in observed behaviour of the Claude Code client — the captured bodies (`bin/captures` + `jq`), the official documentation at `code.claude.com` / `platform.claude.com`, and, where a claim says so, inspection of the client. Tag which of the three a behavioural claim rests on, and name the client version it was observed on. When you must infer, say so — label a claim inferred vs. verified.
- For any model, provider, pricing, or thinking-related question, consult the official docs rather than answering from memory; list prices in particular are dated in `docs/cost.md` and go stale.

## Conventions

- **`bin/` scripts:** keep portable to **bash 3.2 + BSD userland** (stock macOS) — no `mapfile`/`readarray`, no GNU-only coreutils flags.
- **Markdown:** do not line-wrap paragraph text (one paragraph = one line); keep table rows under 150 columns; run `make lint-md` before calling a doc done.
- **Python:** `make all` must be green — ruff, mypy strict, pyright, pytest. Bootstrap the venv with `PYTHON=python3.11 make install`.

## Sensitivity

`captures/` and `sessions/` hold everything the model saw and said — prompts, file contents, tool output, and any secrets that were in scope. Both are gitignored; treat them like credentials, never commit or paste their contents, and never quote them in docs or commit messages. `SECURITY.md` has the full policy.

## Pointers

- `docs/` — per-harness capture surfaces, the hook channel, cost derivation, glossary.
- `src/harness_telemetry/normalize/README.md` — the pipeline, the channels, the CLI entry points.
