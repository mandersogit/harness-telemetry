# codex-cli — session-data exposure

First-hand observations from parsing `~/.codex` on a local development machine (codex-cli 0.118–0.125). **Not yet a complete picture** — the OTEL/telemetry story is unverified; this covers the on-disk transcript channel.

## Channel 1 — on-disk transcript (rollout JSONL)

Location: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO8601>-<uuid>.jsonl`. One file per run (including non-interactive `codex_exec` runs spawned by other tools).

Each line is a JSON record with a top-level `type`. Observed record types:

- **`session_meta`** — first record. `payload` carries `{id, timestamp, cwd, originator (e.g. "codex_exec"), cli_version, source ("exec"), model_provider, base_instructions{text}}`. This is where you read the working directory, model provider, and CLI version.
- **`event_msg`** — runtime events, e.g. `payload.type = "task_started"` with `{turn_id, started_at, model_context_window, collaboration_mode_kind}`.
- **`response_item`** — the conversation content. `payload.type = "message"` with `role` ∈ {`developer`, `user`, `assistant`} and `content[]` blocks (e.g. `{type:"input_text", text:…}`). The first `user` record is typically an `<environment_context>` block (cwd/shell/date/timezone); a `developer` record carries the permissions/instructions preamble.

### Known differences vs Claude Code transcript

- Rollout records are **flat by `type`** (`session_meta`/`event_msg`/`response_item`), not Claude Code's `user`/`assistant` + content-block shape.
- Roles include **`developer`** (not present in Claude Code), used for the instructions/permissions preamble.
- Date-partitioned directory tree vs Claude Code's per-project-per-session layout.

### TODO / unverified

- **Tool calls / shell exec**: codex represents tool use as `response_item` items (likely `function_call` / `local_shell_call` / `function_call_output`) — exact shape **not yet inspected**. Needed for the normalizer's `tool_call`/`tool_result` mapping.
- **Reasoning/thinking** content representation.
- **Compaction / context management** events — whether and how codex logs them.
- **OTEL**: does codex-cli emit OpenTelemetry at all? Unknown. Channel-2 section to be filled in.
- **Parent/DAG linkage**: is there a per-record parent pointer (codex equivalent of `parentUuid`) for forks/sub-agents?
