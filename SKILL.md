---
name: opencode-subtask
description: Run OpenCode as an external child-agent task runtime for Codex/Claude. Use `task` for native Task-like delegation with `--subagent-type`, `--task-id`, optional `--background`, text stdout, and stderr metadata; use `ask` for lower-level text-only foreground calls. Lifecycle commands provide local JSON, artifacts, progress, watch, cancellation, and task listing. Not for decomposition; use subtask-orchestrator first when planning is needed.
---

# OpenCode Subtask Adapter

`opencode-subtask` is a thin adapter around OpenCode. It should not force the child model to speak an adapter-specific result format. Give OpenCode a normal prompt, let it answer normally, and keep adapter JSON limited to local execution facts.

## Core Contract

### Surfaces

| Need | Command |
|---|---|
| Native Task-like call from Codex/Claude | `task` |
| Parent agent delegates one task and wants the child final answer | `ask` |
| Foreground run with execution metadata | `run` |
| Long/background run | `start` then `watch`, `wait`, or `status` |
| Stream compact progress notifications | `watch` |
| Resume/inspect external child task handles | `--task-id`, `list-tasks` |
| Stop a background run | `cancel` |
| Manage a per-project OpenCode server | `ensure-server`, `stop-server` |
| Clean old local run artifacts | `prune-cache` |

### Stdout

| Command | Stdout |
|---|---|
| `task` | Foreground: final assistant text on success. With `--background`: exactly one `opencode-subtask-start` JSON line. |
| `ask` | Final assistant text on success. No adapter JSON envelope. |
| `run` | Exactly one JSON line: `opencode-subtask-finish` or `opencode-subtask-error`. |
| `start` | Exactly one JSON line: `opencode-subtask-start` or `opencode-subtask-error`. |
| `status` | Exactly one JSON line: `opencode-subtask-status` or `opencode-subtask-finish`. |
| `wait` | Exactly one JSON line: `opencode-subtask-status` or `opencode-subtask-finish`. |
| `watch` | Exactly one JSON line on stdout, plus optional `OPENCODE_SUBTASK_PROGRESS {json}` lines on stderr. |
| `cancel` | Exactly one JSON line: `opencode-subtask-cancel`. |
| `list-tasks` | Exactly one JSON line listing adapter task handles for the workdir. |
| server/cache commands | Exactly one JSON line with command facts. |

`ask` writes concise human-readable failures to stderr. When available, it includes the `finish.json` path so the parent can inspect full execution details without polluting stdout.

`ask` is not a read-only review mode. It is the natural sub-agent execution path: the prompt may ask OpenCode to inspect, implement, edit files, run tests, or produce a report. Workspace writes are controlled by the OpenCode agent permissions and the prompt, not by an adapter-level result protocol.

By default, `ask` appends a short natural-language parent-agent boundary briefing. It is plain task guidance: execute directly, avoid unnecessary clarification loops, and keep the final report focused on outcome, changed files, verification, and blockers. This is designed to make Codex/OpenAI-style parent agents consume the child result with minimal context cost.

`ask` also caps stdout through `--ask-stdout-max-chars` (default: 12000). The full answer remains in `assistant.txt`; stdout receives a short truncation note with that path when the child produces an oversized final response.

When the parent needs traceability without polluting stdout, pass `--ask-metadata-to-stderr`. Successful `ask` then emits one `OPENCODE_SUBTASK_META {json}` line to stderr containing `runId`, `taskId`, `sessionId`, child role, changed files, and artifact paths. Stdout remains the final assistant text only. On failure, `--ask-error-json-to-stderr` can echo the control-plane envelope to stderr for programmatic callers. The `task` command enables both metadata flags by default for foreground calls.

### Child Roles

This adapter is not invoking OpenCode's internal Task tool. It launches OpenCode's primary agent as an external child process/session for Codex, Claude, or another parent agent. Still, it mirrors OpenCode child-agent ergonomics through adapter-level roles:

| `--subtask-type` | Default OpenCode agent | Profile | Workspace policy | Typical use |
|---|---|---|---|---|
| `general` | OpenCode default | `hybrid` | follows OpenCode permissions | normal delegated task |
| `worker` | `build` | `checkpoint` | can edit/run tests | implementation or repair |
| `fast-worker` | `build` | `latency` | can edit/run focused checks | small patch or quick fix |
| `thinker` | `plan` | `checkpoint` | read-only envelope | design, risk analysis, review |
| `explore` | `plan` | `latency` | read-only envelope | fast codebase exploration |
| `scout` | `plan` | `latency` | read-only envelope | docs/dependency/API research |

Implementation-capable roles (`general`, `worker`, `fast-worker`) do not receive adapter-added deny rules by default. They need the full OpenCode permission surface to finish real edit/test work; project OpenCode config and `--permission-mode` still govern what is allowed. Read-only roles (`thinker`, `explore`, `scout`) add an adapter read-only envelope: deny `edit` and `bash`, and deny `todowrite`/`task` unless `--allow-child-todos` or `--allow-nested-subtasks` is explicitly set. `--subagent-type` is accepted as an alias for `--subtask-type`.

### External Task Handles

`--task-id` is the caller-facing external child-agent handle. This mirrors OpenCode Task-tool ergonomics, but it is implemented by the adapter because Codex/Claude are outside OpenCode's runtime. A task id records the latest `runId`, `sessionId`, artifacts, child role, progress file, notification file, and final outcome under the adapter cache.

Use explicit task ids for resumable or background work:

```bash
python scripts/opencode_subtask.py ask \
  --workdir . \
  --task-id task_auth_refactor \
  --subtask-type worker \
  --permission-mode allow \
  -- "Continue the auth refactor. Run the targeted tests and report blockers."
```

When a known task id is continued, the adapter reuses the recorded OpenCode `sessionId` unless `--session` is explicitly supplied. It also injects a bounded continuation-memory block derived from `progress.md` and task state, similar in spirit to MiMo-Code's task progress/checkpoint reconstruction. Disable with `--no-task-memory`; tune with `--task-memory-max-chars`.

`--task-id` is only the external adapter task handle. It no longer doubles as a raw OpenCode `ses_...` session shortcut; use explicit `--session` when you intentionally want to attach to a known OpenCode session. By default, starting the same live task id twice returns `TaskAlreadyRunning`; pass `--on-running-task start-new` only when duplicate work is intentional.

### No Model Result Protocol

Do not ask the child model to return adapter-owned JSON just so this wrapper can parse it. If the parent needs a structured final answer, say that naturally in the prompt, for example: "Return a short Markdown report with Findings and Next steps." The adapter will still treat the result as assistant text.

Local JSON is allowed only for deterministic control-plane facts produced by the adapter: process state, exit codes, engine selection, artifact paths, warnings, and workspace changes.

## `finish.json` V3

`finish.json` is the authoritative terminal record for lifecycle commands. It records execution facts, not model semantics.

```json
{
  "type": "opencode-subtask-finish",
  "schemaVersion": 3,
  "adapterVersion": "0.11.0",
  "timestamp": 0,
  "runId": "run_...",
  "taskId": "task_...",
  "workdir": "...",
  "outcome": "completed|failed|timed_out|cancelled|internal_error",
  "execution": {
    "exitCode": 0,
    "durationMs": 0,
    "engine": {"selected": "cli|http|none|watchdog|cancel", "fallbackFrom": "http|null"},
    "sessionId": null,
    "error": null,
    "warnings": []
  },
  "workspace": {
    "changedFiles": [],
    "untrackedFiles": [],
    "patchPath": null
  },
  "artifacts": {
    "dir": "...",
    "jobPath": "job.json",
    "finishPath": "finish.json",
    "promptPath": "prompt.txt|null",
    "stderrPath": "stderr.log|null",
    "assistantPath": "assistant.txt|null",
    "eventsPath": "events.ndjson|null",
    "wrapperLogPath": "wrapper.log|null",
    "progressPath": "progress.md|null",
    "notificationPath": "notification.json|null"
  },
  "subtask": {
    "taskId": "task_...",
    "type": "general|worker|fast-worker|thinker|explore|scout",
    "description": "...|null",
    "readOnly": false,
    "acceptance": [],
    "taskStatePath": "...",
    "taskProgressPath": ".../progress.md",
    "permissionRules": []
  }
}
```

V3 intentionally has no model-semantic top-level fields. The assistant's answer is in `assistant.txt`, and `ask` is the command that prints it directly.

### Invariants

- `outcome="completed"` requires `execution.error=null`.
- `execution.engine.selected` is one of `cli`, `http`, `none`, `watchdog`, or `cancel`.
- `execution.engine.fallbackFrom` is either `null` or `http`, and only applies when the selected engine is `cli`.
- `workspace.patchPath` is the adapter-created `changes.patch` name when git detects a diff.
- `artifacts.dir` is the directory that contains all relative artifact paths.
- `subtask` records adapter-level child-role and task-handle metadata. It is execution metadata, not child-authored result content.
- `taskId` is stable across runs of the same external child task; `runId` is per invocation.

## Operations

### Requirements

- Python 3.10+.
- OpenCode installed and reachable in `PATH`, or pass `--opencode <path>`.
- `git` is optional but recommended if the caller wants `workspace.changedFiles`, `workspace.untrackedFiles`, and `changes.patch`.
- Windows users can call `scripts/opencode-subtask.cmd`; other platforms can call `python scripts/opencode_subtask.py`.

### Boundary

This skill is an executor, not a planner.

- Use it to run one well-scoped subtask and record stable artifacts.
- Use `subtask-orchestrator` first when the work still needs decomposition, role assignment, or acceptance criteria.
- Keep prompts explicit about the desired work, files, verification, and final answer style.

### Recommended Default

Use `task` for new Codex/Claude integrations. It mirrors OpenCode's Task-tool surface most closely: `--subagent-type`, `--task-id`, `--description`, optional `--background`, text-only foreground stdout, and metadata on stderr. `ask` remains the lower-level text-only foreground call when you do not want default metadata.

```bash
python scripts/opencode_subtask.py task \
  --workdir . \
  --task-id task_parser_fix \
  --subagent-type worker \
  --description "Fix parser regression" \
  --permission-mode allow \
  --permission-approval once \
  --acceptance "pytest tests/test_parser.py passes" \
  -- "Fix the failing parser test in tests/test_parser.py, make the smallest correct change, run the targeted test, and summarize changed files plus the test result."
```

Use `ask` when the parent specifically wants no default stderr metadata. This includes code-changing work; ask for implementation explicitly and choose the permission mode that matches the task.

```bash
python scripts/opencode_subtask.py ask \
  --workdir . \
  --engine auto \
  --subtask-type worker \
  --permission-mode allow \
  --permission-approval once \
  --ask-metadata-to-stderr \
  -- "Act as a senior software engineer. Fix the failing parser test in tests/test_parser.py, make the smallest correct change, run pytest tests/test_parser.py, and summarize changed files plus the test result."
```

`ask` still writes `job.json`, `finish.json`, `assistant.txt`, `stderr.log`, optional `events.ndjson`, and optional `changes.patch`. It just keeps stdout clean for the assistant's final text. For parent agents, the normal read path is: consume stdout first; if stdout reports truncation or failure, inspect `assistant.txt`, `finish.json`, and `changes.patch` by path.

### Foreground Lifecycle Run

Use `run` when a parent needs machine-readable execution facts, artifact paths, or the final outcome code.

```bash
python scripts/opencode_subtask.py run \
  --workdir . \
  --engine auto \
  --permission-mode allow \
  -- "Run the targeted tests and summarize failures."
```

`run` prints one JSON line. Read `artifacts.assistantPath` for the assistant text.

### Background Work

Use `task --background` or `start` for long work that should not occupy the parent context while running.

```bash
python scripts/opencode_subtask.py task --background \
  --workdir . \
  --task-id task_large_refactor \
  --subagent-type worker \
  --description "Large refactor" \
  --engine auto \
  --permission-mode allow \
  -- "Implement the requested change and run the relevant tests."
```

Then poll, watch, or block. Prefer `--task-id` for parent-agent orchestration because it survives per-run artifact rotation:

```bash
python scripts/opencode_subtask.py status --task-id <taskId>
python scripts/opencode_subtask.py watch  --task-id <taskId> --wait-timeout 600
python scripts/opencode_subtask.py wait   --task-id <taskId> --wait-timeout 600
```

`watch` is the closest external equivalent to native task notifications. Stdout remains a single terminal JSON object, while stderr can stream compact `OPENCODE_SUBTASK_PROGRESS {json}` lines containing `taskId`, `runId`, phase, last event/tool, idle time, and artifacts directory. Use it when Codex/Claude wants progress without ingesting `events.ndjson`.

`status.progress.summary` provides a lightweight phase snapshot such as `initializing`, `thinking`, `editing`, `testing`, `waiting_permission`, `answering`, `completed_like`, or `error`, derived from recent OpenCode events and artifact state.

List known child handles for the current project:

```bash
python scripts/opencode_subtask.py list-tasks --workdir .
```

Cancel if needed:

```bash
python scripts/opencode_subtask.py cancel --task-id <taskId>
```

## Engine Behavior

`--engine auto` is the normal setting. It prefers an existing OpenCode HTTP server when useful and falls back to the CLI on non-timeout failures. Timeouts do not fall back because retrying a possibly still-running task can duplicate work.

HTTP mode targets the current OpenCode server API surface as verified against local OpenCode 1.17.4 `/doc`: health is checked through `/global/health`, sessions are created through `POST /session`, existing sessions can be continued with `--session` or `--task-id` through `POST /session/:id/message`, and messages are sent as normal text parts. `--title` and `--description` are used when creating a new HTTP session. HTTP session creation also passes `agent`, session-scoped model defaults, and role-derived permission rules when any exist. `--http-deny-interactive-tools` controls deny rules for `question`, `plan_enter`, and `plan_exit`; by default it is enabled only for read-only roles and disabled for implementation-capable roles. Re-run the HTTP schema tests after upgrading OpenCode because server APIs may evolve.

HTTP model shape follows OpenCode's current boundary: session creation uses `{ providerID, id, variant? }`, while message sending uses `{ providerID, modelID }` plus a top-level `variant` when supplied. The user-facing flags remain `--model provider/model` and `--variant <name>`.

CLI mode uses `opencode run --format json`. It is still the correct path for `--file` and `--continue`, because those are native `opencode run` flags and are not equivalent to the HTTP message endpoint.

For CLI mode, the main prompt is passed through stdin by default (`--cli-prompt-transport stdin`). `prompt.txt` is still written as an adapter artifact, but it is not attached as a model-visible OpenCode `--file`. This keeps the prompt transport close to `opencode run`'s native non-interactive input path and avoids wasting child-agent context on an artificial prompt attachment. Use `--cli-prompt-transport file` only when stdin transport is unsuitable for a specific OpenCode CLI invocation.

Assistant text extraction follows current OpenCode event flow. The adapter listens for `message.updated` / `message.part.updated`-style events, tracks assistant message ids, ignores user echoes, reasoning/tool/file/patch parts, and de-duplicates cumulative text snapshots. HTTP mode can also use the SSE stream as a final-text fallback when the synchronous message response has no extracted assistant text. This keeps `assistant.txt` and `ask` stdout aligned with the child's final assistant answer instead of the transport stream.

### Permissions

`--permission-mode inherit` leaves OpenCode permission handling untouched.

`--permission-mode allow` auto-replies to HTTP permission requests. By default it replies `once`; pass `--permission-approval always` only when you want OpenCode's server to approve matching future requests as well.

`--permission-mode noninteractive` is unattended but less permissive: it replies `once` for ordinary requests and `reject` for detected high-risk classes such as broad task/skill delegation, external-directory access, doom-loop requests, or `.env`-style secret reads.

HTTP auto-permission uses the latest server reply shape: `POST /permission/:requestID/reply` with `{ "reply": "once" | "always" | "reject" }`. It does not use the old session-scoped permission endpoint.

Use `--engine cli` for the most faithful OpenCode permission behavior when you rely on OpenCode's local TUI/CLI prompts or project-specific permission configuration.

## Artifact Policy

Artifacts keep detail out of parent context.

| Artifact | Purpose |
|---|---|
| `prompt.txt` | Exact prompt sent to OpenCode. |
| `assistant.txt` | Assistant text collected from OpenCode events or HTTP response. |
| `events.ndjson` | Optional raw OpenCode event stream for diagnostics. |
| `stderr.log` | OpenCode process/server error text. |
| `changes.patch` | Git diff captured after the run, when present. |
| `job.json` | Non-terminal local job state. |
| `finish.json` | Terminal execution record. |
| `wrapper.log` | Background worker stdout/stderr for `start`. |
| `progress.md` | Adapter-generated task progress/checkpoint notes for `--task-id` continuation. |
| `notification.json` | Terminal compact notification summary for parent agents/background watchers. |

Prefer reading only `assistant.txt`, `finish.json`, and `changes.patch` unless deeper debugging is necessary.

## Prompt Guidance

Good prompts are ordinary OpenCode prompts.

```text
Act as a senior software engineer.
Task: Fix the failing parser test in tests/test_parser.py.
Constraints: Make the smallest correct change. Do not modify unrelated files.
Verification: Run pytest tests/test_parser.py.
Final answer: Summarize the cause, changed files, and test result in Markdown.
```

For stricter child stopping behavior, pass repeatable acceptance criteria. These are appended to the child briefing, not parsed from model output:

```bash
python scripts/opencode_subtask.py ask \
  --workdir . \
  --subtask-type worker \
  --acceptance "pytest tests/test_parser.py passes" \
  --acceptance "No unrelated files changed" \
  -- "Fix the parser regression."
```

For `ask`, `--persona-mode` defaults to `off` so parent agents can pass terse delegation prompts. For `run` and `start`, the default persona policy still requires a first-line `Act as ...` prompt unless you override it.

Useful child-agent tuning flags:

| Flag | Default | Use |
|---|---:|---|
| `task --background` | false | Use the Task-like command as a background external child task. |
| `--subagent-briefing` | on for `ask`, off for `run/start` | Append the compact parent-agent boundary briefing. |
| `--final-answer-budget-chars` | 1600 | Tell the child how compact the final report should be. |
| `--ask-stdout-max-chars` | 12000 | Keep parent stdout small while preserving full `assistant.txt`. |
| `--cli-prompt-transport stdin|file` | `stdin` | Use stdin as the native prompt channel; `file` attaches `prompt.txt` as fallback. |
| `--http-deny-interactive-tools` | role-based | Deny `question`, `plan_enter`, `plan_exit` for HTTP-created sessions; default on for read-only roles, off for implementation roles. |
| `--subtask-type` | `general` | Select adapter child role/preset. |
| `--task-id` | null | Stable external child task handle; resumes recorded OpenCode session and bounded memory when known. |
| `--on-running-task fail|start-new` | `fail` | Prevent duplicate live child work for the same task id. |
| `--task-memory` | true | Inject compact task state/progress on continuation. |
| `--acceptance` | repeatable | Add stop conditions to the child-agent briefing. |
| `--ask-metadata-to-stderr` | false | Emit one parseable metadata line on stderr while preserving text-only stdout. |

## Exit Codes

| Outcome | Exit code |
|---|---:|
| `completed` | 0 |
| `failed` | 3 |
| `timed_out` | 124 |
| `cancelled` | 130 |
| `internal_error` | 1 |
| argument/config error | 2 |

`ask` exits 0 only when it can print non-empty assistant text from a completed run.
