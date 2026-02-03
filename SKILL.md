---
name: opencode-subtask
description: Run an OpenCode subtask as an isolated sub-agent executor (run/start/status/wait/cancel). Prints exactly one JSON line to stdout and writes full artifacts to disk; prefers HTTP server API with CLI fallback. 用 OpenCode 跑一个子任务并返回稳定 JSON。
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
2. **Artifacts-first**: Large outputs (NDJSON, transcript, stderr) written to disk
3. **Protocol shielding**: Callers depend only on this adapter's schema, not OpenCode internals
4. **Engine abstraction**: HTTP server API preferred, CLI fallback on failure

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
  -- "Summarize the repo structure (3 bullets)."
```

```bash
# Unix/macOS - using Python directly
python ~/.claude/skills/opencode-subtask/scripts/opencode_subtask.py run \
  --workdir /path/to/your/project \
  --engine auto \
  --model google/antigravity-gemini-3-flash --variant minimal \
  --permission-mode allow \
  -- "Summarize the repo structure (3 bullets)."
```

**Option B: Relative path (from skill directory)**

```bash
cd ~/.claude/skills/opencode-subtask  # or %USERPROFILE%\.claude\skills\opencode-subtask on Windows
python scripts/opencode_subtask.py run --workdir /path/to/project --engine auto -- "Your prompt"
```

### Background job (recommended for long-running tasks)

```bash
# 1) Start (returns immediately with runId)
python scripts/opencode_subtask.py start --workdir . --engine auto --permission-mode allow -- \
  "Review src/foo.py exception handling; propose a minimal fix with file:line evidence."

# 2) Poll status (optional)
python scripts/opencode_subtask.py status --run-id <runId>

# 3) Wait for completion (blocks until done)
python scripts/opencode_subtask.py wait --run-id <runId>
```

### Foreground (debug/quick tasks)

```bash
python scripts/opencode_subtask.py run --workdir . --engine auto --no-quiet -- \
  "Explain why tests fail on Windows; point to exact file:line."
```

## Basic flags (most callers only need these)

| Flag | Default | Notes |
|------|---------|------|
| `--workdir` | `.` | Target project directory (not the skill directory). |
| `--engine` | `auto` | `auto` uses HTTP only if a server URL is available (via `--attach` or `ensure-server`), otherwise CLI. |
| `--model` | (OpenCode default) | Prefer setting defaults in `opencode.json`. |
| `--variant` | (none) | Pass as `--variant <name>`; do **not** use `model:suffix`. |
| `--permission-mode` | `inherit` | Use `allow` for unattended runs; use `inherit` for interactive safety. |
| `--timeout` | (varies) | Increase for long-running reasoning models. |

## Advanced flags (opt-in)

### Session reuse (CLI engine only; reduces isolation)

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
  -- "Analyze src/foo.py for potential bugs. List them with file:line.")

# Extract sessionId from JSON output
SESSION_ID=$(echo "$RESULT1" | python -c "import sys,json; print(json.load(sys.stdin).get('sessionId',''))")
echo "Session: $SESSION_ID"

# Step 2: Continue same session for follow-up (has context from step 1)
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --session "$SESSION_ID" \
  --model google/antigravity-gemini-3-flash --variant low \
  --permission-mode allow \
  -- "Now fix the first bug you identified. Show the diff."

# Step 3: Continue for verification
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --session "$SESSION_ID" \
  --model google/antigravity-gemini-3-flash --variant low \
  --permission-mode allow \
  -- "Verify the fix is correct. Any remaining issues?"
```

**Example 2: Quick Q&A chain (--continue shorthand)**

```bash
# Step 1: Ask about file structure
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --model siliconflow-cn/Pro/zai-org/GLM-4.7 \
  --permission-mode allow \
  -- "Count how many lines are in SKILL.md."

# Step 2: Follow-up (uses --continue to auto-resume last session)
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --continue \
  --model siliconflow-cn/Pro/zai-org/GLM-4.7 \
  --permission-mode allow \
  -- "What file did you just count? How many lines?"

# Step 3: Another follow-up
python scripts/opencode_subtask.py run --workdir . --engine cli \
  --continue \
  --model siliconflow-cn/Pro/zai-org/GLM-4.7 \
  --permission-mode allow \
  -- "What was the first section heading in that file?"
```

**Verified behavior (tested with GLM-4.7):**

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
for /f "delims=" %%i in ('python scripts\opencode_subtask.py run --workdir . --engine cli --model google/antigravity-gemini-3-flash --permission-mode allow -- "List all TODO comments in src/"') do set RESULT=%%i

REM Extract sessionId using Python
for /f %%s in ('echo !RESULT! ^| python -c "import sys,json; print(json.load(sys.stdin).get('sessionId',''))"') do set SESSION_ID=%%s

echo Session: %SESSION_ID%

REM Step 2: Continue
python scripts\opencode_subtask.py run --workdir . --engine cli ^
  --session "%SESSION_ID%" ^
  --model google/antigravity-gemini-3-flash ^
  --permission-mode allow ^
  -- "Pick the most important TODO and implement it."
```

**Note:** The `sessionId` is returned in the finish JSON (`finish.sessionId`). The session belongs to OpenCode CLI, not to this adapter. HTTP engine uses different session mechanics (server-managed sessions).

### Output control / debugging

| Flag | Notes |
|------|------|
| `--no-quiet` | Allow streaming OpenCode events to stderr (stdout still stays one JSON line). |
| `--max-text-chars <n>` | Bound the `summary` size in the finish JSON. |
| `--max-artifact-bytes <n>` | Hard cap per artifact file (0 disables). |
| `--include-debug` | Include extra debug fields in the finish JSON. |

### Prompt / input control

| Flag | Notes |
|------|------|
| `--prompt <text>` | Prompt as a single string (alternative to positional args after `--`). |
| `--prompt-file <path>` | Read prompt from a file (useful for complex prompts or Windows escaping issues). |
| `-f` / `--file <path>` | Extra files to include (CLI engine only). |
| `--no-contract` | Don't append the default output contract to the prompt. |

### Environment / executable

| Flag | Notes |
|------|------|
| `--opencode <path>` | Path to opencode executable (if not in PATH). |
| `--agent <name>` | OpenCode agent name to use. |
| `--env KEY=VALUE` | Set environment variable for the OpenCode process. |
| `--env-file KEY=PATH` | Set environment variable from file contents. |

### Server attachment

| Flag | Notes |
|------|------|
| `--attach <url>` | Attach to a specific `opencode serve` URL. |
| `--no-attach-server` | Don’t attach to a per-project server automatically. (`--engine http` will still require `--attach` or `ensure-server`.) |

## Engine Selection (`--engine`)

| Mode | Behavior |
|------|----------|
| `auto` (default) | **With `--attach-server` (default true):** tries to attach to an existing per-project server first. If a server URL is available (via `--attach` or attached server), uses HTTP; otherwise falls back to CLI. If HTTP fails, falls back to CLI. |
| `http` | HTTP only (requires server via `--attach` or `ensure-server`) |
| `cli` | CLI only (`opencode run --format json`) |

**Note:** `--attach-server` defaults to `true`, meaning `auto` mode will attempt to reuse an existing server if one is running for the project. Use `--no-attach-server` to force CLI mode without checking for servers.

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
    "stderrPath": "stderr.log",
    "assistantPath": "assistant.txt",
    "wrapperLogPath": "wrapper.log",
    "resultPath": "result.json",
    "patchPath": "changes.patch"
  },
  "error": null
}
```

**Note:** `resultDigest` is a raw SHA-256 hex string (no `sha256:` prefix).

## Artifacts (always on disk)

| File | Description |
|------|-------------|
| `prompt.txt` | Effective prompt (with contract appended) |
| `events.ndjson` | Full event stream (CLI NDJSON or HTTP SSE→NDJSON) |
| `assistant.txt` | Full assistant transcript |
| `stderr.log` | CLI stderr or HTTP errors |
| `result.json` | Adapter-extracted structured result |
| `changes.patch` | git diff for tracked changes |
| `job.json` | Job state for status/wait/cancel |
| `finish.json` | Final stable result (same as stdout) |
| `wrapper.log` | Background worker output (start mode) |

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
| Routine analysis, code review | `google/antigravity-claude-sonnet-4-5-thinking` (variants: `low`, `max`) |
| Complex analysis, multi-step refactors | `google/antigravity-claude-opus-4-5-thinking` (variants: `low`, `max`) |
| Pure formatting, minimal reasoning | `google/antigravity-claude-sonnet-4-5` (no thinking) |
| Simple isolated tasks | `google/antigravity-gemini-3-pro` (variants: `low`, `high`) |
| Cost-effective alternative (SiliconFlow) |  `siliconflow-cn/Pro/moonshotai/Kimi-K2.5` (comparable to sonnet-4.5, lower cost) |
| High-capability alternative (SiliconFlow) | `siliconflow-cn/Pro/zai-org/GLM-4.7` |

> **Note:** The models above are from specific providers. For other environments or additional models, consult your `opencode.json` configuration.

**Variant selection:** pass variants with `--variant <name>` (e.g. `--model google/antigravity-gemini-3-flash --variant low`). Do not use `model:suffix` strings.

## Server Management

```bash
# Ensure a per-project server is running
python scripts/opencode_subtask.py ensure-server --workdir .

# Stop server
python scripts/opencode_subtask.py stop-server --workdir .
```

## Operational Notes

- **Stuck detection**: Use `status` output's `progress.idleForSeconds` + artifact sizes
- **Prompt hygiene**: Use Facts/Hypotheses/Constraints/Acceptance capsule (see `subtask-orchestrator`)
- **Result extraction**: Prefers sentinel-wrapped JSON (`BEGIN_OC_SUBTASK_JSON`/`END_OC_SUBTASK_JSON`)
- **Windows**: Default executable is `opencode` (the wrapper prefers `opencode.exe` if available and falls back to `opencode.cmd`); uses `taskkill /T /F` for process cleanup
- **Fallback logging**: When HTTP→CLI fallback occurs, `engine.fallbackFrom` is set in finish JSON

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `ok=false`, `error.name=ServerUnhealthy` | Server not running/healthy. If you want HTTP, run `ensure-server` (or pass `--attach`). Otherwise use CLI (`--engine cli`). |
| `ok=false`, `error.name=OpencodeNotFound` | OpenCode not installed or not in PATH. Install OpenCode or use `--opencode <path>` to specify the executable path. |
| `ok=false`, `error.name=MissingPrompt` | No prompt provided. Pass prompt after `--` or use `--prompt` / `--prompt-file`. |
| `ok=false`, `error.name=MissingRunId` | `status`/`wait`/`cancel` requires `--run-id` or `--artifacts-dir`. |
| `ok=false`, `error.name=JobNotFound` | The specified run-id/artifacts-dir doesn't have a `job.json`. The job may not have started. |
| `ok=false`, `error.name=WorkerNotRunning` | Background worker process not running and `finish.json` missing. The job may have crashed. Check `wrapper.log` and `stderr.log`. |
| A `opencode serve` / Node console window pops up | You are starting/ensuring a server (`ensure-server` or `--engine http`). Use CLI-only mode (`--engine cli` or `--engine auto --no-attach-server`) to avoid starting a server. |
| `ok=false`, `error.name=SseUnavailable` | SSE endpoint blocked; try `--engine cli` |
| `ok=false`, `error.name=Timeout` or `WaitTimeout` | Increase `--timeout`; check model complexity |
| `ok=false`, `error.name=OutputTooLarge` | Reduce output or increase `--max-artifact-bytes` |
| `progress.idleForSeconds` keeps growing | Model stuck; check `stderr.log` for retry loops |
