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
- `--max-artifact-bytes N`:
  - Default: 50MB per artifact file.
  - If any artifact file grows past `N` bytes, the wrapper kills the run and returns `error.name="OutputTooLarge"`.
  - Use `--max-artifact-bytes 0` to disable this guardrail.
- `--no-attach-server`: do not reuse/attach to a shared `opencode serve` instance (slower, but avoids shared-daemon edge cases). (`--no-attach` is accepted as a deprecated alias.)
- `--disable-claude-code / --no-disable-claude-code`:
  - Default: `--disable-claude-code` (sets `OPENCODE_DISABLE_CLAUDE_CODE=1` for isolation when running OpenCode under another agent/executor).
  - Use `--no-disable-claude-code` if you explicitly want OpenCode’s built-in Claude Code integrations enabled.
- `--permission-mode allow`: sets `OPENCODE_PERMISSION={"*":"allow"}` (no prompts; maximum capability).
- `--permission-mode noninteractive`: sets `OPENCODE_PERMISSION` to avoid headless hangs (denies `doom_loop`, `external_directory`, nested `task`, and nested `skill`).
- Default: `--permission-mode allow` (override with `--permission-mode inherit` to respect your existing OpenCode permissions).
- `--opencode-print-logs` / `--opencode-log-level {DEBUG,INFO,WARN,ERROR}`: pass through OpenCode logging to help diagnose long hangs/retries (captured in `artifacts/stderr.log`).
- On Windows, the default `--opencode` is `opencode.cmd`.


## Finish JSON schema

All commands return a single JSON object to stdout.

For `run` / `wait`, the output includes:

- `ok`: boolean
- `exitCode`: integer
- `timedOut`: boolean
- `summary`: string (truncated, bounded)
- `changedFiles`: list of file paths (tracked changes from `git status --porcelain -z`, best-effort)
- `untrackedFiles`: list of untracked file paths (best-effort; not included in `patchPath`)
- `result`: object OR null
  - null by default (see `--inline-result`)
- `error`: object OR null
  - may include `stderrTail` (bounded) for fast diagnosis
  - `error.name` may be `Timeout`, `OutputTooLarge`, `Blocked`, `NonZeroExit`, etc.
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
- Model choice (Antigravity / Google):
  - Quick probes / connectivity checks / shallow triage (strictly for connection confirmation):
    Use `google/antigravity-gemini-3-flash` (variants: `minimal` | `low` | `medium` | `high`).
  - Routine analysis and code review (moderate complexity, limited edits):
    Use `google/antigravity-claude-sonnet-4-5-thinking` (variants: `low` | `max`).
  - Complex analysis + correctness-critical code changes (multi-step refactors, tricky debugging):
    Use `google/antigravity-claude-opus-4-5-thinking` (variants: `low` | `max`).
  - Pure formatting / rewriting with minimal reasoning:
    Use `google/antigravity-claude-sonnet-4-5` (no thinking; no variants).
  - Simple, isolated tasks with short context (basic queries, straightforward code snippets):
	Use `google/antigravity-gemini-3-pro` (variants: `low` | `high`).
	⚠️ Avoid for: complex reasoning, long context (>50k tokens), multi-step logic, or mission-critical tasks.
- For “is it stuck?” checks, use `status`/`wait` output `progress.idleForSeconds` plus the artifact files (`events.ndjson`, `assistant.txt`, `stderr.log`, `wrapper.log`) to see if anything is still advancing.
- For writing reliable subtask prompts (role profiles, Facts/Hypotheses/Constraints/Acceptance capsule), see the `subtask-orchestrator` skill templates.
- Result extraction prefers the strict sentinel-wrapped JSON block (`BEGIN_OC_SUBTASK_JSON` / `END_OC_SUBTASK_JSON`) and falls back to fenced/heuristic JSON extraction if needed.
- Note: On OpenCode `1.1.25`, `opencode run --attach ... --agent <name>` can crash with “No context found for instance”. The wrapper therefore skips server attach when `--agent` is set (unless you pass an explicit `--attach` URL), and you can always force standalone mode with `--no-attach-server`.
- If an auto-attached server run returns `BLOCKED` due to ruleset validation errors (e.g. invalid `action` values), the wrapper retries once in standalone mode and preserves the first attempt artifacts as `*.attempt1` plus `attempt1.json`.
- Prompt hygiene (prevents “wandering” + reduces debug time): avoid unverified claims like “flag X exists” or “path Y exists”. Prefer a short capsule: **Facts** (verified), **Hypotheses** (need checking), **Constraints** (what must not change), **Acceptance** (how to validate; prefer small filtered tests before full gates).
- Typical states:
  - **Finished**: `finish.json` exists and `status` returns `type=opencode-subtask-finish` with `ok/exitCode/timedOut`.
  - **Running**: `wait --timeout <small>` returns `type=opencode-subtask-status` with `status=running` and a live `progress` snapshot.
  - **Stuck/slow**: `status=running` but `progress.idleForSeconds` keeps increasing and artifact sizes stop changing; check `stderr.log` (and enable `--opencode-print-logs` for retry details).
- This wrapper sets `OPENCODE_CLIENT=opencode-subtask` by default (override with `--env OPENCODE_CLIENT=...`). It does not disable OpenCode auto-update; control updates via `opencode.json`.
- For deterministic model selection, set `model` in `opencode.json` (or pass `--model` explicitly).
