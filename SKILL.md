---
name: opencode-subtask
description: Run an isolated OpenCode subtask headlessly (start/wait/run). Writes NDJSON/logs/results to artifacts, and prints exactly one stable JSON line to stdout.
---

This Skill wraps the `opencode` CLI with a stable, automation-friendly interface:

- `start` launches a background worker.
- `wait` blocks (with timeout) for completion.
- `run` executes synchronously.

Critical invariants for agent callers:

1) Stdout stability: the wrapper prints exactly ONE JSON object (single line) to stdout.
   Any streaming NDJSON or debug output is written to stderr and/or artifacts.
   Note: stdout JSON is ASCII-only (`\\uXXXX` escapes) to avoid Windows codepage/GBK encoding crashes; use `artifacts/assistant.txt` for full UTF-8 text.

2) Artifact-first: all large outputs (NDJSON stream, assistant transcript, stderr, patch, full result) are written to disk. The finish JSON returns paths + digests.

3) Protocol shielding: callers only depend on the wrapper schema, not OpenCode's internal NDJSON event format.


## Usage

Synchronous run:

  python scripts/opencode_subtask.py run --workdir . --prompt "Summarize the repo structure" \
    --model google/antigravity-claude-opus-4-5-thinking

Background job:

  python scripts/opencode_subtask.py start --workdir . --prompt "Fix failing tests" \
    --model google/antigravity-claude-opus-4-5-thinking

  python scripts/opencode_subtask.py wait --run-id <RUN_ID>


## Important flags

- `--prompt "..."` (preferred): prompt string.
- Positional prompt: still supported (remaining args joined with spaces).
- `--inline-result / --no-inline-result`:
  - Default: `--no-inline-result` (finish JSON returns pointers + summary only).
  - Enable `--inline-result` for debugging or when the caller explicitly wants the parsed JSON in-band.
- `--no-attach`: do not reuse/attach to a shared `opencode serve` instance.
- `--permission-mode noninteractive`: sets `OPENCODE_PERMISSION` to avoid headless hangs (denies `doom_loop`, `external_directory`, nested `task`, and nested `skill`).
- `--opencode-print-logs` / `--opencode-log-level {DEBUG,INFO,WARN,ERROR}`: pass through OpenCode logging to help diagnose long hangs/retries (captured in `artifacts/stderr.log`).
- On Windows, the default `--opencode` is `opencode.cmd`.


## Finish JSON schema

All commands return a single JSON object to stdout.

For `run` / `wait`, the output includes:

- `ok`: boolean
- `exitCode`: integer
- `timedOut`: boolean
- `summary`: string (truncated, bounded)
- `changedFiles`: list of file paths (from `git diff --name-only`, best-effort)
- `result`: object OR null
  - null by default (see `--inline-result`)
- `error`: object OR null
  - may include `stderrTail` (bounded) for fast diagnosis
- `artifacts`: object with paths (relative to artifactsDir)

Artifact fields (typical):
- `eventsPath`: NDJSON stream from OpenCode (if enabled)
- `stderrPath`: OpenCode stderr
- `assistantPath`: assistant transcript (streamed)
- `promptPath`: prompt+contract as written to disk
- `resultPath`: full structured result JSON written by the wrapper
- `resultDigest`: sha256 digest of `resultPath`
- `patchPath`: git diff patch (best-effort)
- `finishPath`: the finish JSON persisted to disk


## Operational notes

- Prefer `start/wait` for long-running reasoning models.
- For “is it stuck?” checks, use `status`/`wait` output `progress.idleForSeconds` plus the artifact files (`events.ndjson`, `assistant.txt`, `stderr.log`, `wrapper.log`) to see if anything is still advancing.
- Typical states:
  - **Finished**: `finish.json` exists and `status` returns `type=opencode-subtask-finish` with `ok/exitCode/timedOut`.
  - **Running**: `wait --timeout <small>` returns `type=opencode-subtask-status` with `status=running` and a live `progress` snapshot.
  - **Stuck/slow**: `status=running` but `progress.idleForSeconds` keeps increasing and artifact sizes stop changing; check `stderr.log` (and enable `--opencode-print-logs` for retry details).
- For automation stability, consider setting `autoupdate` to `"notify"` (or `false`) in `opencode.json` so OpenCode doesn't change underneath the adapter unexpectedly.
- For deterministic model selection, set `model` in `opencode.json` (or pass `--model` explicitly).
