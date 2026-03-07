---
name: opencode-subtask
description: Run an OpenCode subtask as an isolated sub-agent executor and policy judge (run/start/status/wait/cancel/judge). Prints exactly one JSON line to stdout and writes full artifacts to disk; prefers HTTP server API with CLI fallback for non-timeout failures. Use when delegating a single well-scoped coding task to OpenCode. Not for task decomposition or orchestration (use subtask-orchestrator). 用 OpenCode 跑一个子任务并返回稳定 JSON。
---

# OpenCode Subtask Adapter

`opencode-subtask` is a facts-only executor plus a separate policy judge.

- `run/start/status/wait/cancel` produce execution facts and artifacts.
- `judge` consumes `finish.json` and returns a policy verdict.
- Public machine protocol trusts only the nonce-bound sentinel payload.
- Heuristic extraction is debug-only and stays in `diagnostics.json`.

## Core Contract

### Stdout contract

Exactly one JSON line is printed to stdout.

| Command | Stdout `type` |
|---|---|
| `run` | `opencode-subtask-finish` on normal terminal completion; `opencode-subtask-error` on preflight or finish-persistence failure |
| `start` | `opencode-subtask-start` on successful launch; `opencode-subtask-error` on preflight or launch failure |
| `status` | `opencode-subtask-status` or `opencode-subtask-finish` |
| `wait` | `opencode-subtask-status` or `opencode-subtask-finish` |
| `cancel` | `opencode-subtask-cancel` |
| `judge` | `opencode-subtask-judgment` |
| arg/command errors | `opencode-subtask-error` |

### `finish.json` shape

`finish.json` is the only authoritative terminal envelope.

```json
{
  "type": "opencode-subtask-finish",
  "schemaVersion": 2,
  "adapterVersion": "0.6.0",
  "timestamp": 0,
  "runId": "run_...",
  "workdir": "...",
  "outputMode": "machine|text",
  "outcome": "completed|failed|timed_out|cancelled|internal_error",
  "execution": {
    "exitCode": 0,
    "durationMs": 0,
    "engine": {"selected": "cli|http|none|watchdog|cancel", "fallbackFrom": "http|null"},
    "sessionId": null,
    "error": null,
    "warnings": []
  },
  "payload": {
    "status": "validated|not_requested|missing|malformed|ambiguous|persist_failed",
    "schema": "opencode-subtask-payload-v2|null",
    "artifact": {"path": "payload.json|null", "digest": "sha256|null"},
    "errors": []
  },
  "decision": {
    "status": "determinate|abstained|unavailable|not_requested",
    "route": "GO_NO_DELTA|MANDATORY_DELTA|null"
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
    "payloadPath": "payload.json|null",
    "diagnosticsPath": "diagnostics.json|null"
  }
}
```

### Writer invariants

- `outputMode="machine"` never emits `payload.status="not_requested"`.
- `outputMode="text"` requires `payload.status="not_requested"`, `decision.status="not_requested"`, and `payload.artifact.path/digest=null`.
- `payload.status="validated"` requires:
  - `payload.schema == "opencode-subtask-payload-v2"`
  - `payload.artifact.path != null`
  - `payload.artifact.digest != null`
  - `decision.status in {"determinate", "abstained"}`
- Non-validated payloads keep `payload.artifact.path/digest=null`.
- `decision.route` is non-null only when `decision.status="determinate"`.
- `outcome="completed"` requires `execution.error=null`.

### Frozen machine vocabulary

#### `execution.engine.selected`

| Value | Meaning |
|---|---|
| `cli` | Foreground/background worker used the OpenCode CLI engine. |
| `http` | Run completed on the OpenCode HTTP server path. |
| `none` | Adapter failed before any engine run could start. |
| `watchdog` | Terminal finish was synthesized by stale-run watchdog recovery. |
| `cancel` | Terminal finish was synthesized by `cancel`. |

#### `execution.engine.fallbackFrom`

| Value | Meaning |
|---|---|
| `null` | No engine fallback occurred. |
| `http` | Initial HTTP attempt failed and adapter reran on CLI. |

#### `payload.errors[].code`

| Code | Meaning |
|---|---|
| `PAYLOAD_MISSING` | No authoritative nonce-bound payload was produced. |
| `SENTINEL_MULTIPLE` | Multiple authoritative sentinel candidates were found. |
| `SENTINEL_TRAILING_TEXT` | Non-whitespace text appeared after the terminal sentinel. |
| `NONCE_MISMATCH` | Payload nonce did not match the run contract. |
| `PAYLOAD_JSON_INVALID` | Sentinel block did not parse into a JSON object. |
| `PAYLOAD_SCHEMA_INVALID` | JSON parsed, but payload fields failed schema validation. |
| `DECISION_INVALID` | `decision` field was present but outside the allowed enum. |
| `PAYLOAD_PERSIST_FAILED` | Canonical `payload.json` write failed after validation. |

### Authoritative machine payload

Machine mode requires a single nonce-bound terminal sentinel block:

```text
BEGIN_OC_SUBTASK_JSON_<nonce>
{"protocol":"opencode-subtask-payload-v2","nonce":"<nonce>",...}
END_OC_SUBTASK_JSON_<nonce>
```

Payload schema:

```json
{
  "protocol": "opencode-subtask-payload-v2",
  "nonce": "<nonce>",
  "decision": "GO_NO_DELTA|MANDATORY_DELTA|UNDETERMINED",
  "summary": "string",
  "evidence": ["string"],
  "changes": ["string"],
  "next_steps": ["string"]
}
```

`payload.json` is canonical JSON written by the adapter after validation. `assistant.txt` keeps the raw text trace. `diagnostics.json` is optional debug output and never part of the public decision contract.

## Machine Vocabulary

### `finish.outcome`

| Value | Meaning |
|---|---|
| `completed` | Execution completed without execution-layer error. |
| `failed` | Execution failed but was not a timeout. |
| `timed_out` | Execution timed out. |
| `cancelled` | Run was cancelled. |
| `internal_error` | Adapter synthesized or reported an internal failure. |

### `payload.status`

| Value | Meaning |
|---|---|
| `validated` | Authoritative payload validated and `payload.json` persisted. |
| `not_requested` | `outputMode=text`; no machine payload expected. |
| `missing` | No authoritative sentinel payload found. |
| `malformed` | Sentinel candidate found but JSON/schema/nonce was invalid. |
| `ambiguous` | Multiple candidates or ambiguous sentinel extraction. |
| `persist_failed` | Payload validated but `payload.json` could not be written. |

### `decision.status`

| Value | Meaning |
|---|---|
| `determinate` | Business route is available in `decision.route`. |
| `abstained` | Payload explicitly returned `UNDETERMINED`. |
| `unavailable` | No usable decision is available. |
| `not_requested` | `outputMode=text`; no decision requested. |

### `decision.route`

| Value | Meaning |
|---|---|
| `GO_NO_DELTA` | No code change required. |
| `MANDATORY_DELTA` | Code or artifact delta is required. |

### `payload.errors[].code`

| Code | Meaning |
|---|---|
| `PAYLOAD_MISSING` | No authoritative payload found. |
| `SENTINEL_MULTIPLE` | Multiple sentinel candidates were found. |
| `SENTINEL_TRAILING_TEXT` | Non-whitespace content followed the terminal sentinel. |
| `NONCE_MISMATCH` | Payload nonce did not match the expected contract nonce. |
| `PAYLOAD_JSON_INVALID` | Payload body was not valid JSON. |
| `PAYLOAD_SCHEMA_INVALID` | Payload JSON shape was invalid. |
| `DECISION_INVALID` | `decision` was a string, but outside the allowed enum. |
| `PAYLOAD_PERSIST_FAILED` | `payload.json` write failed after validation. |

### `judge.verdict`

| Value | Meaning |
|---|---|
| `accept` | Accept the run result. |
| `reroute` | Route elsewhere but do not retry the same run. |
| `retry` | Retry is appropriate. |
| `block` | Do not continue. |

### `judge.reasonCode` (frozen)

| Code | Meaning |
|---|---|
| `FINISH_UNREADABLE` | The provided finish file could not be read or parsed. |
| `FINISH_INVALID` | The provided finish file failed V2 validation. |
| `UNKNOWN_POLICY` | Unsupported policy name. |
| `FINISH_NOT_FOUND` | The finish file does not exist at the provided path. |
| `PAYLOAD_DIGEST_MISMATCH` | `payload.json` digest does not match `finish.json`. |
| `EXECUTION_COMPLETED` | Execution-only policy accepted a completed run. |
| `EXECUTION_FAILED` | Execution failed. |
| `EXECUTION_TIMED_OUT` | Execution timed out. |
| `EXECUTION_INTERNAL_ERROR` | Adapter reported an internal error. |
| `EXECUTION_CANCELLED` | Execution was cancelled. |
| `EXECUTION_UNKNOWN` | Unknown execution state. |
| `OUTPUT_NOT_MACHINE` | Policy required machine output but run used text mode. |
| `PAYLOAD_MISSING` | Policy blocked/retried because payload was missing. |
| `PAYLOAD_MALFORMED` | Policy blocked/retried because payload was malformed. |
| `PAYLOAD_AMBIGUOUS` | Policy blocked/retried because payload was ambiguous. |
| `PAYLOAD_PERSIST_FAILED` | Policy blocked/retried because canonical payload write failed. |
| `DECISION_GO_NO_DELTA` | Validated decision accepted. |
| `DECISION_MANDATORY_DELTA` | Validated mandatory-delta decision. |
| `DECISION_UNDETERMINED` | Validated payload abstained. |
| `DECISION_UNAVAILABLE` | No usable decision available. |

## Operations

### Requirements

- Python 3.10+.
- OpenCode installed and reachable in `PATH`, or pass `--opencode <path>`.
- `git` is optional but recommended if you want `workspace.changedFiles`, `workspace.untrackedFiles`, and `changes.patch`.
- Windows users can call the wrapper at `scripts/opencode-subtask.cmd`; other platforms can call `python scripts/opencode_subtask.py`.

### Boundary

This skill is an executor plus policy judge, not a planner.

- Use it to run one well-scoped subtask and record stable artifacts.
- Use `judge` to turn a finished run into `accept|reroute|retry|block`.
- Do not use it to decompose goals, assign expert roles, or invent acceptance criteria for a large project. Feed it a single prepared prompt instead.

### Command selection

| Need | Command |
|---|---|
| Foreground subtask with immediate terminal result | `run` |
| Long-running review or refactor you want to poll | `start` + `status`/`wait` |
| Stop a running job and force a terminal record | `cancel` |
| Apply a routing policy to an existing finish artifact | `judge` |
| Pre-warm or explicitly manage a per-project HTTP server | `ensure-server` / `stop-server` |
| Clean old run artifacts | `prune-cache` |

### Minimal commands

```bash
python scripts/opencode_subtask.py run \
  --workdir . \
  --engine auto \
  --execution-profile hybrid \
  --output-mode machine \
  --diagnostics on-failure \
  --permission-mode allow \
  -- "Act as a senior software engineer. Review src/foo.py and return a machine payload."
```

```bash
python scripts/opencode_subtask.py start --workdir . --engine auto --execution-profile checkpoint --output-mode machine -- "Act as a senior software engineer. Review the diff."
python scripts/opencode_subtask.py status --run-id <runId>
python scripts/opencode_subtask.py wait --run-id <runId>
python scripts/opencode_subtask.py cancel --run-id <runId>
python scripts/opencode_subtask.py judge --finish <artifacts-dir>/finish.json --policy require-determinate
```

### Quick start

Windows wrapper from any directory:

```bat
%USERPROFILE%\.claude\skills\opencode-subtask\scripts\opencode-subtask.cmd run ^
  --workdir C:\path\to\project ^
  --engine auto ^
  --execution-profile hybrid ^
  --output-mode machine ^
  --permission-mode allow ^
  -- "Act as a senior software engineer. Review the diff and return a machine payload."
```

Background review with explicit wait window:

```bash
python scripts/opencode_subtask.py start --workdir . --engine auto --execution-profile checkpoint --output-mode machine --run-timeout 1800 -- \
  "Act as a senior software engineer. Review the current patch and return GO_NO_DELTA or MANDATORY_DELTA."

python scripts/opencode_subtask.py wait --run-id <runId> --wait-timeout 300
python scripts/opencode_subtask.py judge --finish <artifacts-dir>/finish.json --policy require-go-no-delta
```

Notes:

- `run` blocks until terminal state.
- `wait` returns `opencode-subtask-finish` if the job completes inside the wait window; otherwise it returns `opencode-subtask-status` with `waitExpired=true`.
- `judge` is the only public policy surface. Do not infer routing policy from raw `finish` fields alone.

### Key flags

| Flag | Default | Notes |
|---|---|---|
| `--workdir` | `.` | Project root to execute in. |
| `--engine` | `auto` | `auto` prefers HTTP when available and falls back to CLI on non-timeout failures. |
| `--attach` | unset | Explicit server URL. |
| `--attach-server` | `true` | Attach to or ensure a per-project server where applicable. |
| `--model` / `--variant` | provider default | Passed through to OpenCode. |
| `--permission-mode` | `inherit` | Use `allow` for unattended reviews; `noninteractive` auto-responds with a conservative policy. |
| `--persona-mode` | `require` | Prompt hygiene policy. `require` rejects prompts whose literal first line is not `Act as ...` (no leading blank lines); use `prepend`, `warn`, or `off` to relax it. |
| `--persona-line` | `Act as a senior software engineer.` | Persona line used by `--persona-mode prepend`, and a good default first line when writing prompts manually. |
| `--execution-profile` | `hybrid` | `hybrid`: short tasks prefer HTTP; `latency`: prefer HTTP + lighter artifacts; `checkpoint`: prefer CLI + full artifacts. |
| `--output-mode` | `machine` | `machine` expects authoritative sentinel JSON; `text` disables payload/decision extraction. |
| `--diagnostics` | `on-failure` | `never|on-failure|always` for `diagnostics.json`. |
| `--run-timeout` | `600` | Worker runtime timeout in seconds. |
| `--wait-timeout` | `600` | Wait window for `wait` in seconds. |
| `--max-artifact-bytes` | `20000000` | Hard cap across watched artifacts; breach becomes `OutputTooLarge`. |
| `--retry-empty-output` | `true` | Retries one empty successful run when no tracked changes were made. |
| `--continue` / `--session` | unset | CLI session reuse only; increases context bleed risk. |

### Permission modes

| Mode | Behavior |
|---|---|
| `inherit` | Leave permission handling to the surrounding OpenCode environment. |
| `allow` | Maximize forward progress for unattended runs; HTTP auto-replies where possible and CLI uses permissive `OPENCODE_PERMISSION`. |
| `noninteractive` | Avoid hangs on permission prompts with a conservative deterministic allow/deny preset. Prefer this over `allow` when you need unattended execution but still want some guardrails. |

### Engine and profile guidance

Engine selection:

| Setting | Use when | Notes |
|---|---|---|
| `--engine auto` | Default choice for most callers | Prefers HTTP when a server is available and falls back to CLI on non-timeout HTTP failures. |
| `--engine cli` | Isolation, session reuse, or full artifact trace matter most | No shared server state; best for long audits and reproducible transcripts. |
| `--engine http` | You explicitly want server-backed execution | Requires a server via `--attach` or `ensure-server`; no CLI fallback on HTTP timeout. |

Execution profiles:

| Profile | Use when | Behavior |
|---|---|---|
| `hybrid` | Mixed workloads or unknown task shape | Short tasks prefer HTTP + lighter artifacts; longer/heavier tasks prefer CLI + fuller artifacts. |
| `latency` | Quick probes, connectivity checks, or disposable asks | Bias toward HTTP and lighter artifact collection. |
| `checkpoint` | Long-chain reviews, auditable runs, or anything you may need to resume/debug | Bias toward CLI and full artifact retention. |

Guidance:

- `auto` + `hybrid` is the general default.
- `checkpoint` is the safer choice for long Opus review lanes or anything you may need to audit later.
- `latency` is appropriate for tiny probes, not for evidence-heavy review work.
- `auto` fallback does not occur on HTTP timeouts; if timeout sensitivity matters, set `--run-timeout` explicitly and choose `checkpoint`/`cli` when needed.

### Runtime guidance

- `run --run-timeout` and `start --run-timeout` are hard runtime caps for the worker.
- `wait --wait-timeout` only caps how long the caller waits; it does not extend worker runtime.
- The adapter does not auto-extend runtime based on heartbeats or artifact growth.
- The empty-output guard is real: by default the adapter retries one “successful but empty” run when no tracked changes were produced.
- Use `status.progress.idleForSeconds` together with artifact growth (`events.ndjson`, `assistant.txt`, `stderr.log`) before deciding a run is stuck.
- `--max-artifact-bytes` is enforced by a supervisor across watched artifacts. If you see `OutputTooLarge`, either reduce output volume or raise the cap deliberately.

### Session reuse

Session reuse is CLI-only and trades isolation for continuity.

Use it when:

- You are doing a review -> fix -> verify chain on the same narrow task.
- Later prompts need the model to remember earlier findings.
- You want to avoid reloading the same code context repeatedly.

Avoid it when:

- The task should be isolated from prior model state.
- You are running parallel subtasks.
- You need the strongest auditability or reproducibility.

Practical rules:

- Prefer `--session <id>` when you want explicit control.
- Use `--continue` only for short local chains where “last session” ambiguity is acceptable.
- In V2 `sessionId` lives under `finish.execution.sessionId`, not at the top level.

### Utility commands

```bash
python scripts/opencode_subtask.py ensure-server --workdir .
python scripts/opencode_subtask.py stop-server --workdir .
python scripts/opencode_subtask.py prune-cache --keep-last 200
python scripts/opencode_subtask.py prune-cache --keep-last 200 --apply
```

Use these when:

- `ensure-server`: you want a per-project HTTP server running before a batch of HTTP or `auto` tasks.
- `stop-server`: you want to explicitly tear down the per-project server and clear its in-memory session state.
- `prune-cache`: you want to inspect or reclaim old artifacts. It is dry-run by default; add `--apply` to delete.

### `judge` policies

| Policy | Accepts | Other outcomes |
|---|---|---|
| `execution-only` | `outcome=completed` | `retry` on `failed/timed_out/internal_error`; `block` on `cancelled` |
| `require-determinate` | validated `GO_NO_DELTA` | `reroute` on `MANDATORY_DELTA` or `UNDETERMINED`; `retry` on invalid payload/execution failure; `block` on text output or cancel |
| `require-go-no-delta` | validated `GO_NO_DELTA` | `block` on `MANDATORY_DELTA`, `UNDETERMINED`, text output, or cancel; `retry` on invalid payload/execution failure |

### Exit codes

#### `run` and terminal `wait`

| Outcome | Exit code |
|---|---|
| `completed` | `0` |
| `failed` | `3` |
| `timed_out` | `124` |
| `cancelled` | `130` |
| `internal_error` | `1` |

If `finish.json` cannot be written, stdout is `opencode-subtask-error` with `error.name="FinishWriteFailed"` and the process exits `1`.

#### `judge`

| Verdict | Exit code |
|---|---|
| `accept` | `0` |
| `reroute` | `10` |
| `retry` | `11` |
| `block` | `12` |

## Practical Notes

- Prompt hygiene is enforced by default. `--persona-mode require` rejects prompts unless the literal first line is a persona such as `Act as a senior software engineer.` Leading blank lines are not tolerated. Use `--persona-mode off` only if you explicitly want to disable that guard.
- `start` always creates `wrapper.log` as an internal background-worker log artifact. It is not a public policy input.
- `status`, `wait`, `cancel`, and `judge` only trust strict V2 `finish.json`. Invalid finish files are quarantined and ignored by lifecycle commands.
- `auto` engine fallback does not occur on HTTP timeouts.
- Provider/runtime failures should surface as classified execution errors, not protocol redesign. Treat Gemini instability as an operations risk unless the adapter is misclassifying it.
- `--env KEY=VALUE` values are visible in process arguments. Use `--env-file KEY=PATH` for secrets.
- If you attach WIP files or diffs for review, attach them explicitly; do not assume a clean review worktree can see uncommitted local files.

### Troubleshooting

| Symptom | Check |
|---|---|
| `opencode-subtask-error` with `MissingPrompt` / `PromptConflict` / `PromptFileReadError` | Fix prompt input shape: use exactly one of positional prompt, `--prompt`, or `--prompt-file`. |
| `PersonaMissing` | The literal first line was not `Act as ...`. Remove leading blank lines or switch to `--persona-mode prepend` / `off`. |
| `wait` returns status instead of finish | The wait window expired. Inspect `waitExpired`, then keep polling with `status`/`wait` or `cancel` if the run is no longer making progress. |
| `payload.status` is `missing`, `malformed`, or `ambiguous` | Check `diagnostics.json`, `assistant.txt`, and the contract prompt; the run did not produce a usable authoritative machine payload. |
| `judge` returns `FINISH_INVALID` / `FINISH_UNREADABLE` | The provided `finish.json` is not a valid V2 terminal envelope. Use the lifecycle commands to regenerate or quarantine it instead of hand-editing. |
| `execution.error.name=OutputTooLarge` | Lower output volume or increase `--max-artifact-bytes`. |
| `status.progress.idleForSeconds` keeps growing and artifacts stop changing | Treat it as a stuck run candidate. Confirm lack of growth in `assistant.txt`, `events.ndjson`, and `stderr.log`, then `cancel`. |
| HTTP runs keep failing before useful work starts | Try `--engine cli` or `--execution-profile checkpoint`, or pre-warm with `ensure-server` and inspect server health separately. |
