---
name: opencode-subtask
description: Run an OpenCode subtask as an isolated sub-agent executor (run/start/status/wait/cancel). Prints exactly one JSON line to stdout and writes full artifacts to disk; prefers HTTP server API with CLI fallback for non-timeout failures. Use when delegating a single well-scoped coding task to OpenCode. Not for task decomposition or orchestration (use subtask-orchestrator). 用 OpenCode 跑一个子任务并返回稳定 JSON。
---

# OpenCode Subtask Adapter

This skill turns OpenCode into a reliable "subagent primitive" for upstream agents (Codex CLI, Claude Code, etc.):

## Core Design

```
+-------------+     +------------------+     +------------------+
|  Upstream   | --> | opencode-subtask | --> |    OpenCode      |
|   Agent     |     |   (adapter)      |     | (HTTP or CLI)    |
+-------------+     +------------------+     +------------------+
       ^                    |
       |                    v
       +--- finish.json ----+  (stable contract)
```

**Key invariants:**
1. **Stdout stability**: Exactly ONE JSON line to stdout; `type` varies by subcommand:
   - `run`, `wait` (completed) → `opencode-subtask-finish`
   - `start` → `opencode-subtask-start`
   - `status` → `opencode-subtask-status` (or `-finish` if completed)
   - `cancel` → `opencode-subtask-cancel`
   - `ensure-server` → `opencode-subtask-server`
   - `stop-server` → `opencode-subtask-stop-server`
   - CLI argument errors → `opencode-subtask-error` (with `ok: false`)
  - `cancel` includes cleanup telemetry fields: `stopServerAttempted`, `stopServerOk`, `workerOwnership`, `allowUnknownOwnershipKill`, `probeInconclusiveAfterKill`
2. **Artifacts-first**: Large outputs (NDJSON, transcript, stderr) written to disk
3. **Protocol shielding**: Callers depend only on this adapter's schema, not OpenCode internals
4. **Engine abstraction**: HTTP server API preferred, CLI fallback on non-timeout failures

## Prerequisites

- **Python**: 3.10+ (no third-party dependencies)
- **OpenCode**: installed and in PATH (or specify via `--opencode <path>`). The adapter resolves `.exe`/`.cmd` shims automatically on Windows.
- **git**: optional; used for `changedFiles`/`untrackedFiles` and `changes.patch` generation

## Quick Start

### Run from anywhere (recommended)

This skill ships a Python script plus a Windows `.cmd` wrapper:

- Script: `scripts/opencode_subtask.py`
- Windows wrapper: `scripts/opencode-subtask.cmd` (calls the script next to it)

**Option A: Absolute path (invoke from any directory)**

```bat
REM Windows - using wrapper
%USERPROFILE%\.claude\skills\opencode-subtask\scripts\opencode-subtask.cmd run ^
  --workdir C:\path\to\your\project ^
  --engine auto ^
  --model google/antigravity-gemini-3-flash --variant minimal ^
  --permission-mode allow ^
  -- "Act as a senior software engineer. Summarize the repo structure (3 bullets)."
```

**Non‑negotiable prompt hygiene (default):**
- Make the **first line** of every subtask prompt a simple persona: `Act as a [profession]...` (no leading blank lines)
- This adapter enforces it by default via `--persona-mode require` (fail fast if missing).
- If you want auto-injection for convenience, use `--persona-mode prepend`.
- If you hit `PersonaMissing`, either add the persona line yourself or switch to `--persona-mode prepend` (auto-inject) / `off` (disable).
  - If you pass prompts via `--prompt-file` / piping / generated files, the **first non-empty line of the effective prompt** must still be the persona. Avoid leading BOM/whitespace/headers before `Act as ...`.
  - If you're using an orchestrator/planner that emits a prompt template, pass that template **verbatim** as the prompt input; do not prepend titles or metadata that would push the persona off line 1.

**Boundary:** This is an executor, not a planner — see [Boundary (planning vs execution)](#boundary-planning-vs-execution) below.

**Practical tip:**
- Prefer writing the persona line yourself as the first line (it avoids accidentally injecting a generic default persona).
- Keep personas boring and specific: a clear job title beats role-play.

```bash
# Unix/macOS - using Python directly
python ~/.claude/skills/opencode-subtask/scripts/opencode_subtask.py run \
  --workdir /path/to/your/project \
  --engine auto \
  --model google/antigravity-gemini-3-flash --variant minimal \
  --permission-mode allow \
  -- "Act as a senior software engineer. Summarize the repo structure (3 bullets)."
```

**Option B: Relative path (from skill directory)**

```bash
cd ~/.claude/skills/opencode-subtask  # or %USERPROFILE%\.claude\skills\opencode-subtask on Windows
python scripts/opencode_subtask.py run --workdir /path/to/project --engine auto -- "Act as a senior software engineer. <your prompt>"
```

### Background job (recommended for long-running tasks)

```bash
# 1) Start (returns immediately with runId)
python scripts/opencode_subtask.py start --workdir . --engine auto --permission-mode allow --execution-profile checkpoint -- \
  "Act as a senior software engineer. Review src/foo.py exception handling; propose a minimal fix with file:line evidence."

# 2) Poll status (optional)
python scripts/opencode_subtask.py status --run-id <runId>

# 3) Wait for completion (blocks until done)
python scripts/opencode_subtask.py wait --run-id <runId>
```

Behavior note:
- `run` is foreground and returns automatically when the subtask completes (one final JSON line).
- `start` only returns a launch record; completion is observed via `wait`/`status` reading `finish.json`.
- There is no push callback mode in this adapter today.

### Foreground (debug/quick tasks)

```bash
python scripts/opencode_subtask.py run --workdir . --engine auto --execution-profile latency --no-quiet -- \
  "Act as a senior software engineer. Explain why tests fail on Windows; point to exact file:line."
```

## Basic flags (most callers only need these)

| Flag | Default | Notes |
|------|---------|------|
| `--workdir` | `.` | Target project directory (not the skill directory). |
| `--engine` | `auto` | `auto` uses HTTP only if a server URL is available (via `--attach` or `ensure-server`), otherwise CLI. |
| `--model` | (OpenCode default) | Prefer setting defaults in `opencode.json`. |
| `--variant` | (none) | Pass as `--variant <name>`; do **not** use `model:suffix`. |
| `--permission-mode` | `inherit` | Use `allow` for unattended runs; use `inherit` for interactive safety. |
| `--execution-profile` | `hybrid` | Policy switch for engine/artifact behavior: `hybrid`/`latency`/`checkpoint`/`legacy`. |
| `--hybrid-short-timeout-s` | (unset) | `hybrid` short-task timeout threshold override (seconds). Precedence: flag > env > default `240`. |
| `--hybrid-short-prompt-chars` | (unset) | `hybrid` short-task prompt-length threshold override (chars). Precedence: flag > env > default `1600`. |
| `--orphan-reaper` | `true` | On run start, reap orphan per-project servers left by crashed/hard-killed workers. Use `--no-orphan-reaper` to disable. |
| `--orphan-reaper-idle-s` | `1800` | Fallback idle timeout for reaping healthy but unreferenced per-project servers. Set `0` to disable idle-timeout reaping. |
| `--timeout` | (varies) | Legacy timeout input. Used when `--run-timeout` / `--wait-timeout` is not set. |
| `--run-timeout` | (unset) | Runtime timeout for `run` / `start` worker execution; overrides `--timeout` when set. |
| `--wait-timeout` | (unset) | Wait window for `wait`; overrides `--timeout` when set. |
| `--retry-empty-output` | `true` | Retry once when model returns an empty successful response and no tracked file changes are detected. |
| `--empty-output-retries` | `1` | Max retries for empty-output recovery. |

## Timeout semantics (important)

- `run --run-timeout` / `start --run-timeout`: hard cap for worker runtime.
- `wait --wait-timeout`: wait-window for the caller, independent from worker runtime timeout.
- Backward compatibility: if `--run-timeout` / `--wait-timeout` is not provided, adapter falls back to `--timeout`.
- The adapter does not currently auto-extend runtime timeout based on heartbeat (`events.ndjson` / `assistant.txt` growth).
- Empty-success guard: if a model returns success with empty assistant output, adapter marks `EmptyModelOutput` and (by default) retries once only when no tracked changes were produced.
- For heavy reasoning models (especially `opus-4.6-thinking`), prefer larger runtime timeout windows (commonly 1200-1800s for complex reviews).

## Persona-first prompts (default behavior)

For best results (especially with Gemini), every subtask prompt should start with a simple, explicit persona **on the first non-empty line**:

```text
Act as a [profession]...
```

Examples:

```text
Act as a senior software engineer. Review src/foo.py and propose a minimal fix with file:line evidence.
```

```text
Act as a MATLAB runtime capture engineer. Design a Frida hook plan; list exact functions and stop conditions.
```

This adapter supports a lightweight persona policy and enables it by default:

| Flag | Default | Notes |
|------|---------|------|
| `--persona-mode` | `require` | `off`/`warn`/`require`/`prepend`. In `require`, the adapter fails fast if the prompt doesn't start with `Act as ...`. In `prepend`, it injects a persona line **only if** missing. |
| `--persona-line` | `Act as a senior software engineer.` | Used by `prepend` (and can be used as a required prefix). Prefer a simple job title. |

## Boundary (planning vs execution)

This skill is an **execution adapter**: it runs a *single* OpenCode subtask and returns a stable JSON result + on-disk artifacts.

It deliberately does **not** decide:
- how to decompose a big goal into subtasks,
- which expert roles to assign,
- or what acceptance criteria should be.

If you need role allocation, decomposition, and auditable acceptance criteria, use a separate **planner/orchestrator** and feed the resulting per-subtask prompt into `opencode-subtask`.

## Session reuse (CLI engine only; reduces isolation)

Session reuse can reduce cost/time by continuing an existing OpenCode session, avoiding the overhead of starting fresh. However, it increases the risk of "context bleed" across subtasks.

**When to use session reuse:**
- Multi-step analysis where later steps need context from earlier steps
- Iterative refinement tasks (review → fix → review again)
- Cost optimization for related subtasks

**When NOT to use:**
- Isolated, independent subtasks (default behavior is safer)
- Tasks requiring clean context

| Flag | Notes |
|------|------|
| `--continue` / `-c` | Continue the last OpenCode session. |
| `--session <id>` / `-s <id>` | Continue a specific OpenCode session. |
| `--title <text>` | Set/update session title. |

**Practical scenarios:**

| Scenario | Approach | Benefit |
|----------|----------|---------|
| Code review → Fix → Verify | `--session <id>` chain | Model remembers identified issues across steps |
| Incremental refactoring | `--continue` for each step | Maintains refactoring context and decisions |
| Q&A about a codebase | `--continue` conversation | Avoids re-reading files, uses cached context |
| Parallel independent tasks | No session reuse (default) | Clean isolation, no cross-contamination |

**Example 1: Multi-step code review (explicit session ID)**

```bash
# Step 1: Initial analysis (starts new session)
RESULT1=$(python scripts/opencode_subtask.py run --workdir . --engine cli \
  --model google/antigravity-gemini-3-flash --variant low \
  --permission-mode allow \
  -- "Act as a senior software engineer. Analyze src/foo.py for potential bugs. List them with file:line.")

# Extract sessionId from JSON output
SESSION_ID=$(echo "$RESULT1" | python -c "import sys,json; print(json.load(sys.stdin).get('sessionId',''))")
echo "Session: $SESSION_ID"

# Step 2: Continue same session for follow-up (has context from step 1)
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --session "$SESSION_ID" \
  --model google/antigravity-gemini-3-flash --variant low \
  --permission-mode allow \
  -- "Act as a senior software engineer. Now fix the first bug you identified. Show the diff."

# Step 3: Continue for verification
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --session "$SESSION_ID" \
  --model google/antigravity-gemini-3-flash --variant low \
  --permission-mode allow \
  -- "Act as a senior software engineer. Verify the fix is correct. Any remaining issues?"
```

**Example 2: Quick Q&A chain (--continue shorthand)**

```bash
# Step 1: Ask about file structure
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --model siliconflow-cn/Pro/moonshotai/Kimi-K2.5 \
  --permission-mode allow \
  -- "Act as a senior software engineer. Count how many lines are in SKILL.md."

# Step 2: Follow-up (uses --continue to auto-resume last session)
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --continue \
  --model siliconflow-cn/Pro/moonshotai/Kimi-K2.5 \
  --permission-mode allow \
  -- "Act as a senior software engineer. What file did you just count? How many lines?"

# Step 3: Another follow-up
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --continue \
  --model siliconflow-cn/Pro/moonshotai/Kimi-K2.5 \
  --permission-mode allow \
  -- "Act as a senior software engineer. What was the first section heading in that file?"
```

**Verified behavior (tested with GLM-5):**

| Step | Input tokens | Cache read | Observation |
|------|--------------|------------|-------------|
| Step 1 (new session) | 5042 | 10240 | Fresh context, reads file |
| Step 2 (`--session`) | 380 | 15232 | 92% fewer input tokens, remembers "SKILL.md had 318 lines" |
| Step 3 (`--continue`) | 713 | 15232 | Remembers file, answers from memory |

**Example 3: Windows batch script**

```bat
@echo off
setlocal EnableDelayedExpansion

REM Step 1: Analyze
for /f "delims=" %%i in ('python scripts\opencode_subtask.py run --workdir . --engine cli --model google/antigravity-gemini-3-flash --permission-mode allow -- "Act as a senior software engineer. List all TODO comments in src/."') do set RESULT=%%i

REM Extract sessionId using Python
for /f %%s in ('echo !RESULT! ^| python -c "import sys,json; print(json.load(sys.stdin).get('sessionId',''))"') do set SESSION_ID=%%s

echo Session: %SESSION_ID%

REM Step 2: Continue
python scripts\opencode_subtask.py run --workdir . --engine cli ^
  --session "%SESSION_ID%" ^
  --model google/antigravity-gemini-3-flash ^
  --permission-mode allow ^
  -- "Act as a senior software engineer. Pick the most important TODO and implement it."
```

**Note:** The `sessionId` is returned in the finish JSON (`finish.sessionId`). The session belongs to OpenCode CLI, not to this adapter. HTTP engine uses different session mechanics (server-managed sessions).

## Output control / debugging

| Flag | Notes |
|------|------|
| `--no-quiet` | Allow streaming OpenCode events to stderr (stdout still stays one JSON line). |
| `--max-text-chars <n>` | Bound the `summary` size in the finish JSON. |
| `--max-artifact-bytes <n>` | Hard cap per artifact file (0 disables). |
| `--include-debug` | Include extra debug fields in the finish JSON. |
| `--retry-empty-output` | Enable/disable empty-output auto-retry safety net. |
| `--empty-output-retries <n>` | Configure empty-output retry count (default 1). |

## Prompt / input control

| Flag | Notes |
|------|------|
| `--prompt <text>` | Prompt as a single string (alternative to positional args after `--`). |
| `--prompt-file <path>` | Read prompt from a file (useful for complex prompts or Windows escaping issues). |
| `-f` / `--file <path>` | Extra files to include (CLI engine only). |
| `--no-contract` | Don't append the default output contract to the prompt. |

## Environment / executable

| Flag | Notes |
|------|------|
| `--opencode <path>` | Path to opencode executable (if not in PATH). |
| `--agent <name>` | OpenCode agent name to use. |
| `--env KEY=VALUE` | Set environment variable for the OpenCode process. |
| `--env-file KEY=PATH` | Set environment variable from file contents. |
| `OPENCODE_SUBTASK_CANCEL_ALLOW_UNKNOWN_KILL` | `1/true/yes/on` enables kill on unknown worker ownership (default is safe-off, i.e. unknown ownership is not killed). |

## Server attachment

| Flag | Notes |
|------|------|
| `--attach <url>` | Attach to a specific `opencode serve` URL. |
| `--no-attach-server` | Don’t attach to a per-project server automatically. (`--engine http` will still require `--attach` or `ensure-server`.) |
| `--stop-server-after-run <mode>` | HTTP server shutdown policy: `if-started` (default, only stop if this run started the server), `always`, or `never`. |
| `--orphan-reaper` / `--no-orphan-reaper` | Enable/disable startup orphan-server cleanup before attach/ensure logic. |
| `--orphan-reaper-idle-s <seconds>` | Idle fallback threshold used by the startup reaper. |

Shutdown note:
- In `--engine auto`, if HTTP is attempted first and then falls back to CLI (non-timeout failures), shutdown policy is still evaluated for that attempted HTTP path.
- `if-started` only stops when this invocation created the per-project server; `always` stops regardless of who started it.
- Safety gate: `always` / `if-started` only stop the currently tracked **local** per-project server URL (prevents stopping unrelated or remote `--attach` targets).
- `cancel` applies the same policy from persisted job state (`httpAttempted`, `serverStartedNew`, `stopServerAfterRunMode`) to reduce orphaned per-project servers.
- `cancel` is **idempotent**: if `finish.json` already contains a valid terminal state (has an `ok` key), cancel returns immediately with `alreadyFinished=true` and `ok=true` — no PID kill, no job.state overwrite. An empty or structurally incomplete `finish.json` (e.g. `{}`) is NOT treated as a valid terminal state and cancel proceeds normally.
- `cancel` success (`ok=true`) is recognized when any of the following holds:
  - local worker termination is confirmed,
  - remote `abort` succeeds, or
  - kill signal is delivered and liveness probe is inconclusive.
- When `ok=true` comes from inconclusive liveness probing, finish error message explicitly marks cancellation as unverified.
- `cancel` persists `job.json.state=canceled` (plus `canceledAt`) only when the cancel finish write actually wins (i.e. no prior `finish.json` existed). If `finish.json` was already written by the worker or watchdog, job.state is not overwritten.
- If `cancel` cannot persist `finish.json` to disk (`write_failed` / `unreadable`), it degrades `ok` to `false` and attaches a `CancelFinishWriteFailed` error so the caller knows wait/status may not see a terminal state.
- Default safety posture: unknown worker ownership does not trigger kill. Override only when needed via `OPENCODE_SUBTASK_CANCEL_ALLOW_UNKNOWN_KILL`.
- Startup/attach server lock timeout is aligned with `--server-wait` (instead of fixed 20s) to reduce false concurrent-start failures.
- In early `OpencodeNotFound` paths, if `finish.json` already exists, adapter reuses existing finish for stdout/exit consistency.
- Startup reaper uses recent `job.json` evidence and server health: it reaps dead/unhealthy servers immediately, reaps strong crashed-owner orphans, and optionally reaps idle healthy servers after `--orphan-reaper-idle-s`.
- Startup reaper treats failed health probes (including auth mismatch) as unknown evidence and does not kill solely on probe failure.

## Execution Profile (`--execution-profile`)

`--execution-profile` controls routing and artifact density. This is orthogonal to model selection.

| Mode | Intended use | Behavior |
|------|--------------|----------|
| `hybrid` (default) | General-purpose mixed workloads | Heuristic routing: short tasks prefer HTTP and lighter artifacts (`--no-save-events --no-save-text`), long tasks prefer CLI and full artifacts (`--save-events --save-text`). Default short-task thresholds are `timeout <= 240s` and prompt length `<= 1600` chars, configurable via flags/env (below). |
| `latency` | Fast interactive / probe calls | Prefer HTTP when possible and use lighter artifacts. Best for short, disposable subtasks. |
| `checkpoint` | Long-chain / auditable / recoverable workflows | Prefer CLI path and keep full artifacts for resume/debug/audit. Recommended for multi-agent chains. |
| `legacy` | Backward compatibility | Keep pre-policy behavior; use when migrating existing orchestration scripts. |

Policy guidance:
- For long chains, default to `checkpoint`.
- For quick probes and low-latency asks, use `latency`.
- Keep `hybrid` as project default when task mix is unknown.
- If caller input was `--engine auto`, profile-based HTTP preference preserves HTTP-failure fallback to CLI except HTTP timeout (timeout returns directly, no CLI retry).

Hybrid threshold configuration:
- Flags: `--hybrid-short-timeout-s`, `--hybrid-short-prompt-chars`
- Env: `OPENCODE_SUBTASK_HYBRID_SHORT_TIMEOUT_S`, `OPENCODE_SUBTASK_HYBRID_SHORT_PROMPT_CHARS`
- Precedence: CLI flag > env var > built-in default
- Example (flags): `python scripts/opencode_subtask.py run --execution-profile hybrid --hybrid-short-timeout-s 120 --hybrid-short-prompt-chars 1200 --run-timeout 90 --prompt "..."`
- Example (env): set `OPENCODE_SUBTASK_HYBRID_SHORT_TIMEOUT_S=120` and `OPENCODE_SUBTASK_HYBRID_SHORT_PROMPT_CHARS=1200`, then run with `--execution-profile hybrid`

## Engine Selection (`--engine`)

| Mode | Behavior |
|------|----------|
| `auto` (default) | **With `--attach-server` (default true):** tries to attach to an existing per-project server first. If a server URL is available (via `--attach` or attached server), uses HTTP; otherwise falls back to CLI. If HTTP fails without timeout, falls back to CLI. |
| `http` | HTTP only (requires server via `--attach` or `ensure-server`) |
| `cli` | CLI only (`opencode run --format json`) |

**Note:** `--attach-server` defaults to `true`, meaning `auto` mode will attempt to reuse an existing server if one is running for the project. Use `--no-attach-server` to force CLI mode without checking for servers.

**Note (memory):** HTTP engine sessions live inside the per-project `opencode serve` process and are not automatically deleted. If you run many subtasks against a long-lived server, memory can grow over time. Options: run with `--stop-server-after-run if-started` (or `always`), periodically run `stop-server`, or use CLI-only mode (`--engine cli` or `--engine auto --no-attach-server`).

**HTTP path** (preferred):
- Uses OpenCode Server API: `/global/health`, `/session`, `/session/:id/message`
- SSE event stream for diagnostics and auto-permission replies
- Lower latency when server is warm

**CLI path** (fallback):
- Uses `opencode run --format json`
- NDJSON event stream parsed and written to `events.ndjson`
- More isolated, no shared server state

## Finish JSON Schema

All commands return a single JSON object to stdout (note: `type` varies by subcommand, see Key invariants above):

```json
{
  "type": "opencode-subtask-finish",
  "schemaVersion": 1,
  "ok": true,
  "exitCode": 0,
  "timedOut": false,
  "runId": "run_1234567890_12345",
  "workdir": "/path/to/project",
  "engine": {"selected": "http", "fallbackFrom": null},
  "sessionId": "session-abc123",
  "summary": "Fixed auth bug in login.py:42...",
  "summaryTruncated": false,
  "resultDigest": "abc123def456...",
  "changedFiles": ["src/login.py"],
  "untrackedFiles": [],
  "artifacts": {
    "dir": "/path/to/artifacts",
    "jobPath": "job.json",
    "finishPath": "finish.json",
    "promptPath": "prompt.txt",
    "eventsPath": "events.ndjson",
    "stderrPath": null,
    "assistantPath": null,
    "wrapperLogPath": null,
    "resultPath": "result.json",
    "patchPath": "changes.patch"
  },
  "error": null
}
```

**Note:** `resultDigest` is a raw SHA-256 hex string (no `sha256:` prefix).

**Note:** Optional artifact paths (`eventsPath`, `stderrPath`, `assistantPath`, `wrapperLogPath`, `resultPath`) are `null` in JSON when the corresponding file does not exist on disk. Only paths whose files have actually been created are populated with file names. `patchPath` follows its own logic (non-null only when git tracked changes exist).

## Artifacts (on disk)

| File | Condition | Description |
|------|-----------|-------------|
| `prompt.txt` | always | Effective prompt (with contract appended) |
| `job.json` | always | Job state for status/wait/cancel |
| `finish.json` | always | Final stable result (same as stdout) |
| `stderr.log` | always | CLI stderr or HTTP errors |
| `result.json` | always | Adapter-extracted structured result |
| `events.ndjson` | `--save-events` (profile-dependent) | Full event stream (CLI NDJSON or HTTP SSE→NDJSON) |
| `assistant.txt` | `--save-text` (profile-dependent) | Full assistant transcript |
| `changes.patch` | when git tracked changes exist | git diff for tracked changes |
| `wrapper.log` | `start` mode only | Background worker output |

## Permission Modes

| Mode | HTTP Behavior | CLI Behavior |
|------|---------------|--------------|
| `inherit` | No auto-reply | Use existing OPENCODE_PERMISSION |
| `allow` | Auto-allow via API | OPENCODE_PERMISSION=`{\"*\":\"allow\"}` |
| `noninteractive` | Auto-reply via API (conservative allow/deny; denies `external_directory`/`doom_loop`/nested agents and `*.env` reads when detectable) | OPENCODE_PERMISSION=<no-ask preset JSON (deny `external_directory`/`doom_loop`/nested agents; deny `*.env` reads)> |

## Model Selection

| Use Case | Recommended Model |
|----------|-------------------|
| Quick probes, connectivity checks | `google/antigravity-gemini-3-flash` (variants: `minimal`, `low`, `medium`, `high`) |
| Routine analysis, code review | `google/antigravity-claude-sonnet-4-6-thinking` (variants: `low`, `max`) |
| Complex analysis, multi-step refactors | `google/antigravity-claude-opus-4-6-thinking` (variants: `low`, `max`) |
| Pure formatting, minimal reasoning | `google/antigravity-claude-sonnet-4-6` (no thinking) |
| Simple isolated tasks | `google/antigravity-gemini-3.1-pro` (variants: `low`, `high`) |
| Cost-effective alternative (SiliconFlow) |  `siliconflow-cn/Pro/moonshotai/Kimi-K2.5` (comparable to sonnet-4.5, lower cost) |
| High-capability alternative (SiliconFlow) | `siliconflow-cn/Pro/zai-org/GLM-5` |

> **Note:** The models above are from specific providers. For other environments or additional models, consult your `opencode.json` configuration.

**Variant selection:** pass variants with `--variant <name>` (e.g. `--model google/antigravity-gemini-3-flash --variant low`). Do not use `model:suffix` strings.

## Server Management

```bash
# Ensure a per-project server is running
python scripts/opencode_subtask.py ensure-server --workdir .

# Stop server
python scripts/opencode_subtask.py stop-server --workdir .

# One-shot runs (start server if needed, then stop it after completion)
python scripts/opencode_subtask.py run --engine http --stop-server-after-run if-started --workdir . --prompt "..."
```

## Operational Notes

- **Stuck detection**: Use `status` output's `progress.idleForSeconds` + artifact sizes
- **Prompt hygiene**: Use Facts/Hypotheses/Constraints/Acceptance capsule (see `subtask-orchestrator`)
- **Result extraction**: Prefers sentinel-wrapped JSON (`BEGIN_OC_SUBTASK_JSON`/`END_OC_SUBTASK_JSON`)
- **Windows**: Default executable is `opencode` (the wrapper prefers `opencode.exe` if available and falls back to `opencode.cmd`); uses `taskkill /T /F` for process cleanup
- **Fallback logging**: When HTTP→CLI fallback occurs, `engine.fallbackFrom` is set in finish JSON
- **Orphan reaper telemetry**: run debug/job state may include `orphanReaper` details; cancel output includes `stopServerAttempted` / `stopServerOk` and ownership/probe fields (`workerOwnership`, `allowUnknownOwnershipKill`, `probeInconclusiveAfterKill`)

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `ok=false`, `error.name=ServerUnhealthy` | Server not running/healthy. If you want HTTP, run `ensure-server` (or pass `--attach`). Otherwise use CLI (`--engine cli`). |
| `ok=false`, `error.name=OpencodeNotFound` | OpenCode not installed or not in PATH. Install OpenCode or use `--opencode <path>` to specify the executable path. |
| `ok=false`, `error.name=MissingPrompt` | No prompt provided. Pass prompt after `--` or use `--prompt` / `--prompt-file`. |
| `ok=false`, `error.name=PromptConflict` | Multiple prompt sources were provided. Use exactly one of: positional args after `--`, `--prompt`, or `--prompt-file`. |
| `ok=false`, `error.name=MissingRunId` | `status`/`wait`/`cancel` requires `--run-id` or `--artifacts-dir`. |
| `ok=false`, `error.name=JobNotFound` | The specified run-id/artifacts-dir doesn't have a `job.json`. The job may not have started. |
| `ok=false`, `error.name=WorkerNotRunning` | Background worker process not running and `finish.json` missing. The job may have crashed. Check `wrapper.log` and `stderr.log`. |
| A `opencode serve` / Node console window pops up | You are starting/ensuring a server (`ensure-server` or `--engine http`). Use CLI-only mode (`--engine cli` or `--engine auto --no-attach-server`) to avoid starting a server. |
| `ok=false`, `error.name=SseUnavailable` | SSE endpoint blocked; try `--engine cli` |
| `ok=false`, `error.name=Timeout` or `WaitTimeout` | Increase `--run-timeout` / `--wait-timeout` (or legacy `--timeout`); check model complexity |
| `opus-4.6` reviews often timeout before completion | This adapter uses hard runtime timeout; increase `run/start --run-timeout` (commonly 1200-1800s). `wait --wait-timeout` does not extend worker runtime. |
| `ok=false`, `error.name=EmptyModelOutput` | Model returned an empty successful response. Retry manually, or tune `--retry-empty-output` / `--empty-output-retries`. |
| `ok=false`, `error.name=OutputTooLarge` | Reduce output or increase `--max-artifact-bytes` |
| `ok=false`, `error.name=FinishWriteFailed` | `finish.json` could not be written to disk (permissions, disk full, etc.). The stdout JSON is degraded to `ok=false`. Check disk space and directory permissions for the artifacts dir. |
| `ok=false`, `error.name=CancelFinishWriteFailed` | `cancel` attempted to terminate the subtask but could not persist `finish.json` to disk (the process may or may not have been killed — e.g. `pid<=0` takes a no-signal path). Subsequent `wait`/`status` may not see a terminal state. Check disk space and artifacts dir permissions. |
| `cancel` returns `alreadyFinished=true` | The job already has a valid `finish.json`. Cancel is a no-op — no PID kill, no state overwrite. This is normal for finally-cleanup patterns. |
| `progress.idleForSeconds` keeps growing | Model stuck; check `stderr.log` for retry loops |
