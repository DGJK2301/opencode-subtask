---
name: opencode-subtask
description: Run an isolated OpenCode subtask with HTTP server API (preferred) or CLI fallback. Returns one stable JSON line to stdout; writes full artifacts to disk.
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
1. **Stdout stability**: Exactly ONE JSON line to stdout (`type=opencode-subtask-finish`)
2. **Artifacts-first**: Large outputs (NDJSON, transcript, stderr) written to disk
3. **Protocol shielding**: Callers depend only on this adapter's schema, not OpenCode internals
4. **Engine abstraction**: HTTP server API preferred, CLI fallback on failure

## Quick Start

### Run from anywhere (recommended)

This skill ships a Python script plus a Windows `.cmd` wrapper:

- Script: `scripts/opencode_subtask.py`
- Windows wrapper: `scripts/opencode-subtask.cmd` (calls the script next to it)

On Windows, prefer invoking the wrapper by absolute path so you don't depend on your current directory:

```bat
REM Run from any directory; set --workdir to the target project directory
%USERPROFILE%\.codex\skills\opencode-subtask\scripts\opencode-subtask.cmd run ^
  --workdir C:\path\to\your\project ^
  --engine auto ^
  --model google/antigravity-gemini-3-flash --variant minimal ^
  --permission-mode allow ^
  -- "Summarize the repo structure (3 bullets)."
```

### Background job (recommended for long-running tasks)

```bash
# 1) Start
python scripts/opencode_subtask.py start --workdir . --engine auto --permission-mode allow -- \
  "Review src/foo.py exception handling; propose a minimal fix with file:line evidence."

# 2) Poll status (optional)
python scripts/opencode_subtask.py status --run-id <runId>

# 3) Wait for completion
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
| `--engine` | `auto` | `auto` tries HTTP first → CLI fallback on failure. |
| `--model` | (OpenCode default) | Prefer setting defaults in `opencode.json`. |
| `--variant` | (none) | Pass as `--variant <name>`; do **not** use `model:suffix`. |
| `--permission-mode` | `inherit` | Use `allow` for unattended runs; use `inherit` for interactive safety. |
| `--timeout` | (varies) | Increase for long-running reasoning models. |

## Advanced flags (opt-in)

### Session reuse (CLI engine only; reduces isolation)

Session reuse can reduce cost/time, but it also increases the risk of “context bleed” across subtasks. Use only when you intentionally want continuity.

| Flag | Notes |
|------|------|
| `--continue` / `-c` | Continue the last OpenCode session. |
| `--session <id>` / `-s <id>` | Continue a specific OpenCode session. |
| `--title <text>` | Set/update session title. |

### Output control / debugging

| Flag | Notes |
|------|------|
| `--no-quiet` | Allow streaming progress logs (still keeps stdout as one JSON line). |
| `--max-text-chars <n>` | Bound the `summary` size in the finish JSON. |
| `--max-artifact-bytes <n>` | Hard cap per artifact file (0 disables). |
| `--include-debug` | Include extra debug fields in the finish JSON. |

### Server attachment

| Flag | Notes |
|------|------|
| `--attach <url>` | Attach to a specific `opencode serve` URL. |
| `--no-attach-server` | Don’t attach to a per-project server automatically. (`--engine http` will still require `--attach` or `ensure-server`.) |

## Engine Selection (`--engine`)

| Mode | Behavior |
|------|----------|
| `auto` (default) | Use HTTP if a server URL is available (via `--attach` or `ensure-server`) → otherwise run CLI. If HTTP fails, fall back to CLI. |
| `http` | HTTP only (requires server) |
| `cli` | CLI only (`opencode run --format json`) |

**HTTP path** (preferred):
- Uses OpenCode Server API: `/global/health`, `/session`, `/session/:id/message`
- SSE event stream for diagnostics and auto-permission replies
- Lower latency when server is warm

**CLI path** (fallback):
- Uses `opencode run --format json`
- NDJSON event stream parsed and written to `events.ndjson`
- More isolated, no shared server state

## Finish JSON Schema

All commands return a single JSON object to stdout:

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
  "resultDigest": "sha256:abc123...",
  "changedFiles": ["src/login.py"],
  "untrackedFiles": [],
  "artifacts": {
    "dir": "/path/to/artifacts",
    "eventsPath": "events.ndjson",
    "assistantPath": "assistant.txt",
    "resultPath": "result.json",
    "patchPath": "changes.patch"
  },
  "error": null
}
```

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
| `noninteractive` | Auto-deny via API | OPENCODE_PERMISSION=<no-ask preset JSON (deny `external_directory`/`doom_loop`/nested agents; deny `*.env` reads)> |

## Model Selection

| Use Case | Recommended Model |
|----------|-------------------|
| Quick probes, connectivity checks | `google/antigravity-gemini-3-flash` (variants: `minimal`, `low`, `medium`, `high`) |
| Routine analysis, code review | `google/antigravity-claude-sonnet-4-5-thinking` (variants: `low`, `max`) |
| Complex analysis, multi-step refactors | `google/antigravity-claude-opus-4-5-thinking` (variants: `low`, `max`) |
| Pure formatting, minimal reasoning | `google/antigravity-claude-sonnet-4-5` (no thinking) |
| Simple isolated tasks | `google/antigravity-gemini-3-pro` (variants: `low`, `high`) |

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
- **Windows**: Default executable is `opencode.cmd`; uses `taskkill /T /F` for process cleanup
- **Fallback logging**: When HTTP→CLI fallback occurs, `engine.fallbackFrom` is set in finish JSON

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `ok=false`, `error.name=ServerUnhealthy` | Server not running/healthy. If you want HTTP, run `ensure-server` (or pass `--attach`). Otherwise use CLI (`--engine cli`). |
| `ok=false`, `error.name=SseUnavailable` | SSE endpoint blocked; try `--engine cli` |
| `ok=false`, `error.name=Timeout` | Increase `--timeout`; check model complexity |
| `ok=false`, `error.name=OutputTooLarge` | Reduce output or increase `--max-artifact-bytes` |
| `progress.idleForSeconds` keeps growing | Model stuck; check `stderr.log` for retry loops |
