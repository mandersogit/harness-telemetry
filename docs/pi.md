# pi — session-data exposure

> **Unverified stub.** Nothing on this page has been checked first-hand for this project. It is a second-hand summary, assembled from an earlier internals note and from pi's own documentation, kept here so the cross-harness comparison has a starting point. There is **no pi adapter** in this repo, no pi capture has been taken, and every claim below should be treated as a lead to verify rather than a finding. Corrections welcome.

Sources for what follows:

- a pi-internals note (2026-05-14) in the public, MIT-licensed [`libharness`](https://github.com/mandersogit/libharness/blob/main/dev-notes/2026-05-14-pi-internals-notes.md) repository;
- pi's own docs — `docs/session-format.md`, `docs/sessions.md`, `docs/compaction.md`, `docs/rpc.md` — cited by filename, since they are upstream documents and not reproduced here.

## Channel 1 — on-disk transcript (JSONL)

- Each session is its own `<timestamp>_<uuid>.jsonl` under `sessions/<encoded-cwd>/`; sessions do not share files. Default base `~/.pi/agent/sessions/<encoded-cwd>/` (overridable via `session_dir`). *(Second-hand, from the internals note; not verified here.)*
- pi has first-class **compaction** and **session forking / tree** concepts (`session_tree` / `session_before_tree` events were noted in the internals-note event analysis) — promising for the manifest/DAG mapping, and worth contrasting with Claude Code's `parentUuid` tree. *(Second-hand; not verified here.)*

## TODO

- First-hand parse of a real pi session JSONL → record/event schema. Everything above stays unverified until this happens.
- Map pi's compaction + session-tree to the common event vocabulary (pi may be the *richest* native source for compaction-as-event provenance).
- **OTEL**: does pi emit OpenTelemetry? Unknown — likely not; its observability is the RPC event stream. Document that channel instead.
