# Contributing

Thanks for looking. This is a small, pre-alpha research project; issues and pull requests are welcome, and so is a note that something in `docs/` no longer matches the harness you are running.

## Dev setup

Requires CPython **≥ 3.11**.

```sh
PYTHON=python3.11 make install   # or: make install PYTHON=/path/to/python3.11
make all
```

`PYTHON` is required and has no fallback chain: `make install` bootstraps `local.venv/` with exactly the interpreter you name, and fails loudly rather than guessing which of several installed Pythons you meant. It is consulted only when `local.venv/` does not exist yet — every other target runs `local.venv/bin/*`, so `PYTHON` is a one-time bootstrap input, not a standing requirement. The `≥ 3.11` floor is enforced by `pyproject.toml` at install time.

## Quality gates

| Command | What it runs |
| --- | --- |
| `make all` | `ruff` (lint) + `ruff format --check` + `mypy --strict` + `pyright` + `pytest` |
| `make lint-md` | `markdownlint-cli2` over `docs/` and the top-level `*.md`, plus the table-width check |
| `make help` | every target |

`make lint-md` needs a Node-based tool that is not a Python dependency: `npm install -g markdownlint-cli2`.

All five gates in `make all` must be green before a pull request is ready. Both type checkers are deliberately present — they disagree often enough to be worth it. `mypy` runs in **strict** mode; `pyright` runs at its **basic** type-checking mode (`pyproject.toml` sets `typeCheckingMode = "basic"`), so it is a second opinion rather than a second strict gate.

## Markdown conventions

- **Do not line-wrap paragraph text.** One paragraph is one long line; break only at blank lines, list items, and headings. This keeps diffs meaningful.
- **Table rows stay under 150 columns.** Split a wide table into narrower ones, shorten headers, or drop the table.
- Lint every file you touch (`make lint-md`) and fix until clean.

## Sensitivity

Working on this project means generating captures, and **a capture contains everything the model saw and said** — your prompts, the contents of files it read, tool output, and any secrets that were in scope at the time. `captures/` and `sessions/` are gitignored; treat them like credentials. Never attach a capture (or a `normalized.json`) to an issue or a pull request, and never paste a fragment of one into a doc, a test fixture, or a commit message. If you need a fixture, build a synthetic one — see `tests/conftest.py`. Full policy: [`SECURITY.md`](SECURITY.md).

## Testing

Tests under `tests/` are **synthetic**: inputs are built inline or under `tmp_path`, so they run anywhere and are safe to commit. Synthetic does not mean shallow — the suite exercises the real invariants against those inputs: conservation against a capture directory, hook-line conservation, the pricing derivation, and the reconstruct primitives.

Two oracles cannot be exercised that way, because they need a genuine session: a **capture-vs-document round trip** against real raw bodies, and the **`cost_usd` vs `cost_usd_reported` cross-check** against the harness's own estimate. Those run only against the maintainer's local captures, which are not distributed because they cannot be scrubbed cheaply, so nothing under `tests/` covers them. That is a known gap, not a design choice: committable, redacted scenario fixtures are the plan.

New behavior needs a synthetic test. If the behavior can only be demonstrated against a real capture, say so in the pull request and describe what you observed.

## Method: conserve, don't just validate

One lesson from this project's own history is worth stating up front, because it is easy to get wrong. It is tempting to verify a normalizer by checking its *output*: the document is schema-valid, the references resolve, the numbers are internally consistent. That is necessary and not sufficient — the schema validates whatever story the normalizer chose to tell, so it cannot see a dropped input, a mislabeled provenance, or a coerced value.

**Every normalizer claim needs a conservation check against its inputs, not just consistency checks over its outputs.** Concretely: `harness-normalize --check` compares the output against the capture directory it came from (every body file accounted for, unmatched ones named), and a claim about a fixture should cite that output rather than an adjective. Hold new adapters to the same bar.

## How to add a harness adapter

The contract is `schema/events.schema.json` — the normalized event vocabulary. An adapter's job is to read one harness's own output and emit a document that validates against it and conserves its inputs.

1. Read [`src/harness_telemetry/normalize/claude_code.py`](src/harness_telemetry/normalize/claude_code.py) as the reference implementation. It is the only complete adapter and shows the shape: discover the channels, build the content-addressed object store block by block, emit one `inference` event per API call, splice in the transcript's message DAG, and attach the OTEL-only fields where they exist.
2. Read `schema/events.schema.json` for the contract — the event types, the object kinds, the ordering rules, and what belongs in hashed content versus per-request annotation.
3. Document the harness's capture surface first, in `docs/<harness>.md`, before writing code. Which channels exist, what each is the sole source of, and what is version-dependent. The adapter falls out of that.
4. Implement `--check` conservation for your inputs from the start, and degrade gracefully: a missing channel should thin the document, not fail it.

## Commits and pull requests

- One concern per commit. A refactor and a behavior change are two commits.
- Say **why** in the message body, not just what — the diff already says what.
- No AI-attribution trailers.
- Reference the doc you changed alongside the code, so `docs/` does not drift.

For what it is worth, the maintainer prepares commits with a public helper skill, [`commit-plans`](https://github.com/mandersogit-skills/commit-plans). That is information, not a requirement — plain `git commit` is fine.
