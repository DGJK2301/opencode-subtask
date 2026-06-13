# opencode-subtask

A thin adapter that runs [OpenCode](https://opencode.ai) as an **external child-agent task runtime** for Codex, Claude, or any parent agent that needs delegated execution.

> **Not a planner.** When you need task decomposition or role assignment, run a dedicated orchestration layer first. `opencode-subtask` is a precise executor that records stable artifacts and keeps parent context clean.

---

## Key Concepts

`opencode-subtask` wraps OpenCode's CLI or HTTP server. It does **not** invoke OpenCode's internal Task tool—it launches OpenCode's primary agent as an external child process/session. The adapter then mirrors OpenCode child-agent ergonomics through its own role presets and lifecycle commands.

The central design principle is that **the child model should not speak an adapter-specific result format**. Give OpenCode a normal prompt, let it answer normally, and keep adapter JSON limited to local execution facts (process state, exit codes, artifact paths, workspace diffs).

---

## Installation & Requirements

- **Python 3.10+**
- **OpenCode** installed and reachable in `PATH`, or pass `--opencode <path>`
- `git` (optional, but required for `workspace.changedFiles`, `workspace.untrackedFiles`, and `changes.patch`)

**Windows:**
```cmd
scripts\opencode-subtask.cmd [args]
```

**macOS / Linux:**
```bash
python scripts/opencode_subtask.py [command] [args]
```

---

## Quick Start

### `task` — Recommended for Codex / Claude integrations

`task` is the recommended entry point. It mirrors OpenCode's Task-tool surface most closely: supports `--subagent-type`, `--task-id`, `--description`, optional `--background`, text-only foreground stdout, and metadata on stderr.

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

### `ask` — Lower-level text-only foreground call

Use `ask` when you want no default stderr metadata and a clean stdout for the assistant's final answer.

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

---

## Command Reference

| Command | Stdout | Typical Use |
|---|---|---|
| `task` | Foreground: final assistant text. `--background`: one `opencode-subtask-start` JSON line. | Native Task-like delegation from Codex/Claude |
| `ask` | Final assistant text. No adapter JSON envelope. | Parent delegates one task and wants the child final answer |
| `run` | One JSON line: `opencode-subtask-finish` or `opencode-subtask-error` | Foreground run with machine-readable execution facts |
| `start` | One JSON line: `opencode-subtask-start` or `opencode-subtask-error` | Launch a background run |
| `status` | One JSON line: `opencode-subtask-status` or `opencode-subtask-finish` | Poll a background run |
| `wait` | One JSON line: `opencode-subtask-status` or `opencode-subtask-finish` | Block until a background run ends |
| `watch` | One JSON line on stdout + optional progress lines on stderr | Stream compact progress notifications |
| `cancel` | One JSON line: `opencode-subtask-cancel` | Stop a background run |
| `list-tasks` | One JSON line listing adapter task handles for the workdir | Inspect known child task handles |
| `ensure-server` / `stop-server` | One JSON line with command facts | Manage a per-project OpenCode HTTP server |
| `prune-cache` | One JSON line with command facts | Clean old local run artifacts |

---

## Child Roles (`--subtask-type`)

| Role | Default Agent | Profile | Workspace Policy | Typical Use |
|---|---|---|---|---|
| `general` | OpenCode default | `hybrid` | Follows OpenCode permissions | Normal delegated task |
| `worker` | `build` | `checkpoint` | Can edit / run tests | Implementation or repair |
| `fast-worker` | `build` | `latency` | Can edit / run focused checks | Small patch or quick fix |
| `thinker` | `plan` | `checkpoint` | **Read-only envelope** | Design, risk analysis, code review |
| `explore` | `plan` | `latency` | **Read-only envelope** | Fast codebase exploration |
| `scout` | `plan` | `latency` | **Read-only envelope** | Docs / dependency / API research |

Implementation-capable roles (`general`, `worker`, `fast-worker`) do not receive adapter-added deny rules by default. Read-only roles (`thinker`, `explore`, `scout`) automatically add deny rules for `edit`, `bash`, and `todowrite`/`task` unless `--allow-child-todos` or `--allow-nested-subtasks` is set.

`--subagent-type` is accepted as an alias for `--subtask-type`.

---

## Background Runs & Task IDs

Use `task --background` or `start` for long work. Use `--task-id` to assign a stable, resumable handle:

```bash
# Start in background
python scripts/opencode_subtask.py task --background \
  --workdir . \
  --task-id task_large_refactor \
  --subagent-type worker \
  --engine auto \
  --permission-mode allow \
  -- "Implement the requested change and run the relevant tests."

# Poll / stream progress
python scripts/opencode_subtask.py watch  --task-id task_large_refactor --wait-timeout 600

# Cancel if needed
python scripts/opencode_subtask.py cancel --task-id task_large_refactor
```

When a known `--task-id` is continued, the adapter reuses the recorded OpenCode `sessionId` and injects a bounded continuation-memory block from `progress.md`. Disable with `--no-task-memory`.

---

## Engine Behavior

`--engine auto` is the normal setting. It prefers an existing OpenCode HTTP server and falls back to the CLI on non-timeout failures. Timeouts do **not** trigger a fallback to avoid duplicating running work.

| Engine | Notes |
|---|---|
| `auto` | HTTP if server is healthy, CLI otherwise |
| `http` | Always use the OpenCode HTTP server |
| `cli` | Always use `opencode run --format json` |

**HTTP mode:** Checks health via `/global/health`, creates sessions via `POST /session`, continues sessions via `POST /session/:id/message`. Re-run HTTP schema tests after upgrading OpenCode.

**CLI mode:** Uses `opencode run --format json`. Required for `--file` and `--continue`. Prompt delivered via stdin by default (`--cli-prompt-transport stdin`).

---

## Permissions

| Mode | Behavior |
|---|---|
| `inherit` | Leaves OpenCode permission handling untouched |
| `allow` | Auto-approves HTTP permission requests (`once` by default; `always` with `--permission-approval always`) |
| `noninteractive` | Unattended but less permissive: rejects high-risk classes (broad delegation, external-directory access, secret reads) |

Use `--engine cli` for the most faithful OpenCode permission behavior when relying on native TUI/CLI prompts or project-specific configuration.

---

## Artifacts

All artifacts are written to a per-run directory under the adapter cache.

| File | Purpose |
|---|---|
| `assistant.txt` | Full assistant text from OpenCode |
| `finish.json` | Terminal execution record (schema V3) |
| `changes.patch` | Git diff captured after the run |
| `prompt.txt` | Exact prompt sent to OpenCode |
| `events.ndjson` | Optional raw OpenCode event stream for diagnostics |
| `stderr.log` | OpenCode process / server error text |
| `job.json` | Non-terminal local job state |
| `wrapper.log` | Background worker stdout/stderr (for `start`) |
| `progress.md` | Adapter-generated checkpoint notes for `--task-id` continuation |
| `notification.json` | Terminal compact notification summary |

**Normal read path for parent agents:** consume stdout first; if stdout reports truncation or failure, read `assistant.txt`, `finish.json`, and `changes.patch` by the paths in the JSON envelope.

---

## `finish.json` Schema (V3)

```json
{
  "type": "opencode-subtask-finish",
  "schemaVersion": 3,
  "adapterVersion": "0.11.0",
  "runId": "run_...",
  "taskId": "task_...",
  "workdir": "...",
  "outcome": "completed|failed|timed_out|cancelled|internal_error",
  "execution": {
    "exitCode": 0,
    "durationMs": 0,
    "engine": { "selected": "cli|http|none|watchdog|cancel", "fallbackFrom": "http|null" },
    "sessionId": null,
    "error": null,
    "warnings": []
  },
  "workspace": {
    "changedFiles": [],
    "untrackedFiles": [],
    "patchPath": null
  },
  "artifacts": { "dir": "...", "assistantPath": "assistant.txt", "finishPath": "finish.json" },
  "subtask": {
    "taskId": "task_...",
    "type": "general|worker|fast-worker|thinker|explore|scout",
    "readOnly": false,
    "acceptance": []
  }
}
```

V3 has no model-semantic top-level fields. The assistant's answer lives in `assistant.txt`; `ask` prints it directly to stdout.

---

## Exit Codes

| Outcome | Code |
|---|---|
| `completed` | `0` |
| `internal_error` | `1` |
| argument / config error | `2` |
| `failed` | `3` |
| `timed_out` | `124` |
| `cancelled` | `130` |

`ask` exits `0` only when it can print non-empty assistant text from a completed run.

---

## Key Flags

| Flag | Default | Description |
|---|---|---|
| `--subagent-type` / `--subtask-type` | `general` | Child role preset |
| `--task-id` | — | Stable external child task handle; enables session reuse and continuation memory |
| `--engine` | `auto` | Engine selection: `auto`, `http`, `cli` |
| `--permission-mode` | `inherit` | Permission handling: `inherit`, `allow`, `noninteractive` |
| `--permission-approval` | `once` | Reply mode for `allow`: `once` or `always` |
| `--background` | `false` | Run `task` as a background child (returns start JSON immediately) |
| `--acceptance` | — | Repeatable stop conditions appended to child briefing |
| `--ask-metadata-to-stderr` | `false` | Emit one `OPENCODE_SUBTASK_META` JSON line to stderr, keeping stdout text-only |
| `--ask-stdout-max-chars` | `12000` | Truncate stdout; full answer is always in `assistant.txt` |
| `--final-answer-budget-chars` | `1600` | Tell the child how compact the final report should be |
| `--on-running-task` | `fail` | Action when the same live task id is started twice: `fail` or `start-new` |
| `--no-task-memory` | — | Disable continuation-memory injection for `--task-id` resumption |

---

## Prompt Guidance

Good prompts are ordinary OpenCode prompts. A practical template:

```text
Act as a senior software engineer.
Task: Fix the failing parser test in tests/test_parser.py.
Constraints: Make the smallest correct change. Do not modify unrelated files.
Verification: Run pytest tests/test_parser.py.
Final answer: Summarize the cause, changed files, and test result in Markdown.
```

If you need structured output, say so naturally in the prompt—do not ask the child to wrap results in adapter JSON.

---

## License

See [LICENSE](LICENSE) for details.
