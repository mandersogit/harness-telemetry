# Claude Code hooks — the harness-state channel

Claude Code can run a shell command of your choosing at each point in its own lifecycle: a session opens, a prompt is submitted, a tool is about to run, context is about to be compacted, a sub-agent finishes, the session ends. Those points are **hook events**, configured under a `hooks` key in a settings file. Hooks exist so you can *intervene* — block a tool call, rewrite a prompt, add context. This project uses them for the opposite reason: a hook that does nothing but record the JSON blob it was handed turns Claude Code's internal state machine into a **capture channel**.

That channel is the fourth one this project ingests, alongside the on-disk transcript, the raw request/response bodies, and OpenTelemetry. It is the only one that reports the harness's *own* decisions rather than the model traffic. `harness-capture` registers a passive listener on every documented hook event by default; this document is what it registers, what you get back, and how to turn it off.

## Why a passive listener is a capture channel

The three other channels answer "what was sent to the model, and what came back". Hooks answer "what did the CLI do, and when".

| Channel | Centred on | Sole-source contribution |
| --- | --- | --- |
| Raw bodies | the API request/response | assembled system prompt, tool schemas, byte-exact context, cache breakpoints |
| Transcript | the message tree | the DAG (`uuid`/`parentUuid`), sub-agent structure and spawn links |
| OpenTelemetry | the API call | per-inference latency, the harness's own cost estimate, tool-decision events |
| **Hooks** | **the harness state machine** | **config / instruction / file / cwd *change* events, worktree events, UI notifications** |

Several hook events carry information that appears in no other channel at all: `PermissionRequest` / `PermissionDenied` (governance — what was asked for and what was refused), `PreCompact` / `PostCompact` (a boundary marker at the exact compaction instant, rather than a boundary inferred from a body diff), `ConfigChange` / `InstructionsLoaded` / `FileChanged` (context-engineering provenance — which instruction files were in effect, what changed mid-session), `StopFailure` (turns that ended in an API error), and `Notification` / `MessageDisplay` (things that only ever existed on screen).

Every payload also carries `transcript_path`, so even a listener that does nothing else gives you a timestamped pointer at the live transcript at each lifecycle moment — enough to snapshot or correlate at that instant.

## The event set

`harness-capture` registers a listener on **31 events** — every event the official hooks documentation lists. Thirty of the names were observed first-hand on Claude Code 2.1.195; the thirty-first, `DirectoryAdded`, comes from the documentation (see below). They live in one place in the code, the `HOOK_EVENTS` tuple in `src/harness_telemetry/capture.py`.

**Currency check (2026-08-21):** the official hooks documentation at `code.claude.com/docs/en/hooks` lists **31** events — the 30 observed on 2.1.195 plus **`DirectoryAdded`**, which the listener now registers. Two smaller drifts in the same docs: `SessionStart` has gained a `fork` matcher, and the documented common payload now includes `prompt_id`. Treat the event set as version-dependent — it has grown at least once since June 2026.

In the tables below, **matcher** is what the event's `"matcher"` field filters on (an empty matcher, which is what the listener uses, means "all"), and **blocks** is whether the event is blocking-capable per the official documentation. A passive listener never blocks regardless: it returns exit 0.

### Session lifecycle

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `SessionStart` | `startup`, `resume`, `clear`, `compact`, `fork` | no | authoritative session open + how it started; `transcript_path` from t0 |
| `Setup` | `init`, `maintenance` | no | setup/maintenance runs distinguished from real sessions |
| `SessionEnd` | `clear`, `resume`, `logout`, `prompt_input_exit`, `other` | no | authoritative close **and the reason** — a finalize trigger |

### Prompt boundaries

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `UserPromptSubmit` | (none) | yes | turn/prompt boundaries plus the prompt text, before processing |
| `UserPromptExpansion` | command names | yes | the *expanded* prompt behind a slash command (command provenance) |

### Turn and response

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `Stop` | (none) | yes | per-turn end markers without parsing the transcript |
| `StopFailure` | error types | no | turns that ended in an API error, which other channels may not record |

### Tool use and permissions

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `PreToolUse` | tool names | yes | the intended call and its full `tool_input` at decision time |
| `PostToolUse` | tool names | no | tool results as they complete |
| `PostToolUseFailure` | tool names | no | tool errors |
| `PostToolBatch` | (none) | yes | parallelism boundaries — which tools ran concurrently |
| `PermissionRequest` | tool names | no | the permission gate and what was asked for |
| `PermissionDenied` | tool names | no | what was blocked (governance / audit) |

### Sub-agents, tasks, teams

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `SubagentStart` | agent types | no | sub-agent open and the spawn linkage |
| `SubagentStop` | agent types | yes | sub-agent close — the moment its transcript file is complete |
| `TaskCreated` | (none) | yes | the agent's task plan as it forms |
| `TaskCompleted` | (none) | yes | task closure |
| `TeammateIdle` | (none) | yes | agent-team coordination state (niche: agent teams only) |

### Compaction

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `PreCompact` | `manual`, `auto` | yes | a marker at the exact pre-compaction instant, and why it fired |
| `PostCompact` | (none) | no | the post-compaction state to pair with it — what compaction changed |

### Environment and config

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `InstructionsLoaded` | load reasons | no | which instruction files were in effect (`file_path`, `load_reason`, `memory_type`) |
| `ConfigChange` | config sources | yes | settings mutations that change behaviour mid-session |
| `CwdChanged` | (none) | no | cwd provenance for subsequent tool calls |
| `DirectoryAdded` | `slash_command`, `register_repo_root` | no | a working dir added mid-session (`/add-dir`, SDK `register_repo_root`) |
| `FileChanged` | literal filenames | no | external file mutations the agent reacted to |

`DirectoryAdded` is the one row above that is documentation-sourced rather than observed first-hand (added to `HOOK_EVENTS` 2026-08-21); everything the listener records for it is therefore unverified until a capture shows it.

### Worktrees

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `WorktreeCreate` | (none) | yes | isolation boundaries for parallel work |
| `WorktreeRemove` | (none) | no | teardown boundary |

### MCP elicitation

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `Elicitation` | MCP server names | yes | an MCP server asking the user for input |
| `ElicitationResult` | MCP server names | yes | the user's answer to it |

### UI and notifications

| Event | Matcher filters on | Blocks | A passive listener gets |
| --- | --- | --- | --- |
| `Notification` | notification types | no | idle / auth / permission-prompt surfaces that exist nowhere else |
| `MessageDisplay` | (none) | no | a display-time signal while assistant text streams (10 s hook timeout) |

## The common payload

Every hook is handed a JSON object on stdin. Four fields were observed first-hand in captured payloads on Claude Code 2.1.198: `session_id`, `transcript_path` (the path to the transcript *at that instant*), `cwd`, and `hook_event_name`. The official documentation additionally lists `prompt_id` (absent until the first user input) and, in sub-agent context, `agent_id` and `agent_type`.

**`permission_mode` is per-event, not universal.** The documentation describes it as a common field; captured payloads contradict that. It was present on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolBatch` and `Stop`, and absent on `SessionStart`, `InstructionsLoaded`, `MessageDisplay` and `SessionEnd`. Consumers must treat it as optional.

Event-specific fields ride along: `PreToolUse` carries the full `tool_input`; `InstructionsLoaded` carries `file_path`, `load_reason` and `memory_type`. The recorder keeps whatever arrives, verbatim, so a payload growing new fields costs nothing.

## Headless (`-p`) behaviour

The documentation in June 2026 marked four events as interactive-only — `Notification`, `MessageDisplay`, `Elicitation`, `ElicitationResult` — implying `-p` captures the other 26. One of those is wrong: **`MessageDisplay` does fire in `-p`**, observed in a headless single-tool session on 2.1.198. `Notification`, `Elicitation` and `ElicitationResult` have not been exercised either way first-hand; they are inherently interactive (a user-facing prompt, an MCP server asking a question), so the documented behaviour is plausible, but treat it as unconfirmed.

The current documentation no longer states per-event `-p` exclusions at all. The one explicit statement it makes is about `Setup`: it fires with `--init-only`, or with `--init` / `--maintenance` in `-p`, for one-time preparation in CI or scripts. It also notes that subagent frontmatter hooks run only after the workspace trust dialog is accepted, and that a `-p` session does not count as accepting it — that constraint is about hooks declared in subagent frontmatter, not about the listener described here.

A representative headless capture (one prompt, one Bash tool call) produced 10 records: `SessionStart`, `InstructionsLoaded` ×2, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolBatch`, `MessageDisplay`, `Stop`, `SessionEnd`.

## What the launcher injects, and how to opt out

`harness-capture` takes no flags of its own — everything after the command name is passed to `claude` verbatim, and configuration is by environment variable. Hook injection follows one policy, implemented in `src/harness_telemetry/capture.py`:

1. **Scan argv read-only.** If the user passed their own `--settings` (in either `--settings X` or `--settings=X` form), **skip injection entirely** and rely on the other channels. Two `--settings` flags do not merge — the last one wins — so injecting would silently discard the user's file. Losing the hook channel is the cheaper failure.
2. **Otherwise generate the settings file into the capture directory** as `captures/<ts>/hook-settings.json`, and append `--settings <that path>` to the child's argv. It is generated per capture rather than kept as a fixed file in this repo, so each capture records exactly what was injected.
3. **`HARNESS_NO_HOOKS=1`** (also `true` / `yes`) skips injection unconditionally, whatever argv looks like.

The generated document registers the same passive entry on all 31 events:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "", "hooks": [{ "type": "command", "command": "\"$HARNESS_HOOK_PY\" -I \"$HARNESS_HOOK_SCRIPT\"" }] }
    ]
  }
}
```

Two details of that command line are load-bearing:

- **Both paths travel in the environment.** Hook `command` fields are shell-executed and environment variables expand inside them, so the launcher sets `HARNESS_HOOK_PY` to the running venv's interpreter and `HARNESS_HOOK_SCRIPT` to the absolute path of the installed `hookrec.py` (resolved with `importlib.util.find_spec`; if it cannot be located, injection is skipped with a message rather than guessed at). Neither depends on `PATH` or on a console script, and the generated settings file holds no absolute paths of its own.
- **`-I` plus the recorder's absolute path, never `-m`.** A hook command runs with the *session's* working directory — the user's project — as its cwd. Resolving the recorder as a module (`python -m harness_telemetry.hookrec`) would search that directory, so a `harness_telemetry/` tree sitting in the project, or anything on `PYTHONPATH`, would execute as the capturing user on every single hook firing. Naming the installed file by absolute path removes the search; `-I` (isolated mode) additionally ignores `PYTHONPATH`, the other `PYTHON*` variables and the user site directory, and — via the `-P` it implies — keeps the script's own directory off `sys.path`. Never use a relative path in a hook command for the same reason: it resolves against a directory that belongs to the user, not to this repo.

`--settings` hooks execute as documented; since the flag is user-supplied, no trust gate applies, so injection is dependable for capture.

### The recorder

`src/harness_telemetry/hookrec.py` reads the payload from stdin and appends one line to `$HARNESS_HOOK_DIR/hooks.jsonl` — i.e. `captures/<ts>/hooks/hooks.jsonl`:

```json
{"received_at": "2026-01-01T00:00:00.000Z", "event": "PreToolUse", "payload": {"...": "verbatim"}}
```

Its contract is that it must never cost the user anything:

- **Exit 0 unconditionally.** A missing env var, unparseable stdin, or any I/O failure is swallowed. The capture may lose one record; the session loses nothing.
- **Verbatim payload.** No filtering, no reshaping. Stdin that is not JSON is still kept, as `{"raw": "<bytes>"}` with a null event name.
- **Locked appends.** Each write holds an exclusive `flock`, so parallel tool calls and concurrent sub-agents cannot interleave partial lines.
- **Private by default.** `hooks/` is created `0700` and `hooks.jsonl` `0600`, matching the rest of the capture directory. The recorder sets its own umask for the moment it creates the file, since it is a short-lived process of its own and cannot inherit one from the launcher — the launcher deliberately does not set a process-wide umask, which `claude` and every tool it spawns would otherwise inherit.

### Ingestion

`harness-normalize` reads `hooks/hooks.jsonl` as a first-class channel. Each record becomes one event of `type: "hook"` with `source: "hook"`, `subtype` = the hook event name (null if the payload did not parse), `ts` = when the recorder received it, and `payload` = the blob as delivered. `session.captured_with.hooks` means **records present** — at least one hook record was ingested — not "the listener was configured". Injection can succeed and still yield nothing (a managed policy that blocks hooks, a session that ends before any event fires), and that reads as `false`, correctly: the document has no hook evidence in it. Hook events merge across capture directories the same way the other channels do, so a session and its resume continuations produce one ordered stream.

Conservation is checked, not assumed: `harness-normalize --check` counts non-empty lines in `hooks.jsonl` against `hook` events in the document and fails loudly on a mismatch, so a torn or unparseable line can never disappear silently. `harness-watch` fingerprints the `hooks/` directory too, so the channel stays current while the session runs.

## Settings mechanics you need to know

- **Hooks merge across the settings hierarchy.** User, project, local, managed, plugin and `--settings` layers all contribute; a hook in one layer does not replace a hook in another, and multiple hooks on the same event all fire. Verified empirically (a project `settings.json` hook and a `--settings` hook both fired on one event) and stated in the official configuration documentation. Consequence: the injected listener runs *alongside* your own hooks rather than displacing them.
- **Two `--settings` flags are last-wins.** Only the final one takes effect; they do not merge. Verified by test. This is the whole reason the launcher yields when you pass your own.
- **A malformed `--settings` file is silently ignored** (per the CLI's own `--help`).
- **Standard locations hot-reload; `--settings` is read once at startup.** The documentation says the standard settings files reload mid-session (and that `ConfigChange` fires when they do), while `model` and `outputStyle` need a restart. That `--settings` is startup-only is **inferred**, not documented: there is no `ConfigChange` matcher for it and the CLI reference describes it as applying "for this session". Nothing in this project depends on the inference.
- **`disableAllHooks`** turns hooks off globally when set in settings, and **`allowManagedHooksOnly`** lets an administrator restrict execution to managed hooks — in which case user, project, local and plugin hooks (the injected listener among them) do not run at all. Both are documented settings; in a managed environment they, not `HARNESS_NO_HOOKS`, decide whether this channel exists.

### If you need hooks alongside your own `--settings`

The launcher's answer is "don't" — it declines. Three strategies exist, ranked as they were evaluated:

1. **Skip injection and rely on the other channels** (what ships). No collision, no dependence on the startup-only inference, no residue. Most of what hooks provide has an OTEL or transcript equivalent; the change events do not.
2. **Inject via `.claude/settings.local.json`** — a separate standard location that merges with your `--settings` and leaves it untouched. The cost is a persistent file in someone's project: it must be deep-merged with any existing one, backed up, and guaranteed to be cleaned up, or a crash leaves stale hooks that fire on the next unrelated session.
3. **Deep-merge your `--settings` with the hook document into one combined temp file.** Workable, but it rests on the startup-only inference and rewrites a file the user owns. Least preferred.

## What this channel cannot give you

A passive listener is deliberately observational, and that bounds it:

- **No model traffic.** Prompts appear (via `UserPromptSubmit`) and tool inputs appear (via `PreToolUse`), but assembled system prompts, tool schemas, cache breakpoints and response content come from the raw bodies. Token counts come from the response bodies (or OTEL). Per-inference latency and the harness's own cost estimate come from OTEL only.
- **No message DAG.** Hook payloads point at the transcript; they do not contain the `uuid`/`parentUuid` tree, sub-agent structure, or spawn links.
- **No intervention.** Blocking, modifying and injecting are exactly what hooks are for, and exactly what this listener declines to do. Measurement must not change what it measures.
- **Nothing when injection is skipped.** Pass your own `--settings`, set `HARNESS_NO_HOOKS=1`, or run in an environment where `disableAllHooks` / `allowManagedHooksOnly` applies, and the channel is simply absent — `captured_with.hooks` is false and the rest of the pipeline degrades gracefully.

## Caveats

- **The event set is version-dependent.** 30 events observed in 2.1.195; 31 documented on 2026-08-21, all 31 registered. Re-check `HOOK_EVENTS` against the published list when you upgrade Claude Code.
- **Per-event payload schemas are mostly documentation-sourced.** The fields confirmed first-hand are listed above; everything else comes from the official reference and should be treated as such until a capture shows it.
- **Hook records are as sensitive as the rest of a capture.** Payloads carry prompt text, full tool inputs, file paths and working directories. `captures/` is gitignored and should be treated like credentials.
- **A listener costs a process per event.** It is one short-lived Python process per firing, exiting immediately; `MessageDisplay` in particular can fire often, and its hook timeout is 10 seconds by default rather than the usual 600.
- **Recording is best-effort by design.** The recorder swallows its own failures, which is why conservation counting at ingestion time exists — the mismatch, not the silence, is what tells you a record was lost.

## Verified / inferred

**Verified first-hand:**

- 30 of the 31 event names, observed in Claude Code 2.1.195 (2026-06-28) and cross-checked against the official hooks documentation. (`DirectoryAdded`, the 31st, is documentation-sourced — see below.)
- Hooks merge across the settings hierarchy (a project `settings.json` hook and a `--settings` hook both fired on one event, 2026-06-28); two `--settings` flags are last-wins (2026-06-28).
- Hook `command` fields are shell-executed and `$ENV` expands within them; `--settings` hooks execute as documented, in headless and interactive mode alike (2026-07-02).
- Live capture on Claude Code 2.1.198 (2026-07-02): 10 verbatim payloads from a headless single-tool session; `session_id`, `transcript_path`, `cwd`, `hook_event_name` in every payload; `PreToolUse` carrying full `tool_input`; `InstructionsLoaded` carrying `file_path` / `load_reason` / `memory_type`. Two corrections to the documented behaviour came out of it: **`MessageDisplay` fires in `-p`**, and **`permission_mode` is per-event, not universal**.
- Conservation between recorded lines and ingested events, and byte-level agreement between the watcher's output and the launcher's finalize output (2026-07-02).

**Documentation-sourced (`code.claude.com/docs/en/hooks`, read 2026-08-21):** the matcher and blocking columns; the 31st event `DirectoryAdded` (registered since 2026-08-21, not yet seen fire); the `fork` matcher on `SessionStart`; `prompt_id` in the common payload; `agent_id` / `agent_type` in sub-agent context; the `MessageDisplay` 10 s timeout; `disableAllHooks` and `allowManagedHooksOnly`; `Setup`'s `-p` conditions.

**Inferred, not verified:** that `--settings` is read once at startup and does not hot-reload (no `ConfigChange` matcher for it; the CLI reference calls it session-scoped). That `Notification`, `Elicitation` and `ElicitationResult` do not fire in `-p` — documented in June 2026, not exercised first-hand, and no longer stated in the current documentation.
