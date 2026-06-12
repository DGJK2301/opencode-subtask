#!/usr/bin/env python3
"""
opencode_subtask.py

A Codex-friendly adapter around OpenCode that provides:
- A natural child-agent call: ask -> final assistant text on stdout.
- Control-plane JSON for local lifecycle commands.
- Artifacts-first logging (events/stderr/assistant/patch) to avoid caller context bloat.
- Job semantics: start -> wait (background) and run (foreground lifecycle record).
- Engine abstraction: HTTP server API preferred; CLI fallback.

Python: 3.10+
No third-party deps.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree, which
from typing import Any, Final, Iterable, NoReturn

# ============================
# Constants / schema
# ============================

ADAPTER_SCHEMA_VERSION: Final[int] = 3
ADAPTER_VERSION: Final[str] = "0.11.0"

DEFAULT_TIMEOUT_S: Final[float] = 600.0
DEFAULT_GIT_TIMEOUT_S: Final[float] = 20.0
DEFAULT_STALE_IDLE_S: Final[float] = 600.0
DEFAULT_DEAD_WORKER_GRACE_S: Final[float] = 180.0
DEFAULT_CANCEL_STUCK_GRACE_S: Final[float] = 180.0
DEFAULT_EXECUTION_PROFILE: Final[str] = "hybrid"
HYBRID_SHORT_TIMEOUT_S: Final[float] = 240.0
HYBRID_SHORT_PROMPT_CHARS: Final[int] = 1600
DEFAULT_STOP_SERVER_AFTER_RUN: Final[str] = "if-started"
DEFAULT_ORPHAN_REAPER_IDLE_S: Final[float] = 1800.0
DEFAULT_CANCEL_TERM_GRACE_S: Final[float] = 2.0
DEFAULT_CANCEL_KILL_GRACE_S: Final[float] = 2.0
DEFAULT_CANCEL_ALLOW_UNKNOWN_KILL: Final[bool] = False
DEFAULT_FILE_LOCK_TIMEOUT_S: Final[float] = 20.0
DEFAULT_FILE_LOCK_POLL_S: Final[float] = 0.05
# 0 => no cap (scan all run dirs) to avoid missing active long-lived workers.
ORPHAN_REAPER_SCAN_LIMIT: Final[int] = 0

# 0 means "no hard cap"
DEFAULT_MAX_ARTIFACT_BYTES: Final[int] = 20_000_000

# Parent-agent hygiene: keep child final reports useful but compact.
# The full assistant text remains in assistant.txt; ask stdout can be capped to
# protect the caller's context window from accidental log/diff dumps.
DEFAULT_FINAL_ANSWER_BUDGET_CHARS: Final[int] = 1600
DEFAULT_ASK_STDOUT_MAX_CHARS: Final[int] = 12_000
DEFAULT_CLI_PROMPT_TRANSPORT: Final[str] = "stdin"

# External task runtime: stable caller-facing task ids, compact continuation
# memory, and progress notification files.  These are adapter-side equivalents
# of OpenCode Task tool handles/background notifications, not model contracts.
DEFAULT_TASK_MEMORY_MAX_CHARS: Final[int] = 2400
DEFAULT_WATCH_PROGRESS_INTERVAL_S: Final[float] = 5.0

# Adapter-level child-agent roles.  These mirror OpenCode Task-tool ergonomics
# (subagent_type + focused permission envelope) while still launching the
# OpenCode main agent as an external child process/session for Codex/Claude.
DEFAULT_SUBTASK_TYPE: Final[str] = "general"
SUBTASK_TYPES: Final[set[str]] = {
    "general",
    "worker",
    "fast-worker",
    "thinker",
    "explore",
    "scout",
}
SUBTASK_TYPE_ALIASES: Final[dict[str, str]] = {
    "fast_worker": "fast-worker",
    "fastworker": "fast-worker",
    "fast": "fast-worker",
    "research": "explore",
    "review": "thinker",
}
SUBTASK_TYPE_PRESETS: Final[dict[str, dict[str, Any]]] = {
    "general": {
        "agent": None,
        "executionProfile": None,
        "brief": "general-purpose external child agent",
        "readOnly": False,
    },
    "worker": {
        "agent": "build",
        "executionProfile": "checkpoint",
        "brief": "workspace-changing implementation child",
        "readOnly": False,
    },
    "fast-worker": {
        "agent": "build",
        "executionProfile": "latency",
        "brief": "small low-latency implementation child",
        "readOnly": False,
    },
    "thinker": {
        "agent": "plan",
        "executionProfile": "checkpoint",
        "brief": "read-only analysis/planning child",
        "readOnly": True,
    },
    "explore": {
        "agent": "plan",
        "executionProfile": "latency",
        "brief": "fast read-only codebase exploration child",
        "readOnly": True,
    },
    "scout": {
        "agent": "plan",
        "executionProfile": "latency",
        "brief": "read-only docs/dependency research child",
        "readOnly": True,
    },
}

DEFAULT_SERVER_HOSTNAME: Final[str] = "127.0.0.1"
DEFAULT_SERVER_PORT: Final[int] = 0  # 0 => pick a free port
DEFAULT_SERVER_WAIT_S: Final[float] = 10.0

FINISH_OUTCOMES: Final[set[str]] = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "internal_error",
}
EXECUTION_PROFILES: Final[set[str]] = {"hybrid", "latency", "checkpoint"}
EXECUTION_ENGINE_SELECTED_VALUES: Final[set[str]] = {
    "cli",
    "http",
    "none",
    "watchdog",
    "cancel",
}
EXECUTION_ENGINE_FALLBACK_VALUES: Final[set[str]] = {"http"}

# ============================
# Small utilities
# ============================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_line(obj: dict[str, Any]) -> str:
    # ASCII-only JSON to survive GBK/CP1252 stdout encodings.
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _exit_with_error(error_name: str, message: str, exit_code: int = 1) -> NoReturn:
    """Print a control-plane JSON error to stdout and exit."""
    obj = _error_obj(error_name=error_name, message=message)
    sys.stdout.write(_json_line(obj) + "\n")
    sys.exit(exit_code)


def _error_obj(
    *, error_name: str, message: str, warnings: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "type": "opencode-subtask-error",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": False,
        "warnings": warnings or [],
        "error": {"name": error_name, "message": message},
    }


def _finish_already_present_message(finish_path: Path) -> str:
    return (
        "artifacts dir already contains a terminal finish.json; "
        f"refusing to reuse prior terminal state at {finish_path}"
    )


class _JsonArgumentParser(argparse.ArgumentParser):
    """
    ArgumentParser that preserves lifecycle stdout JSON on CLI parse errors.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:  # pragma: no cover - thin adapter
        _exit_with_error("BadArgs", message, exit_code=2)


def _first_nonempty_line(s: str) -> str:
    # Be robust against BOM/zero-width characters that can appear when prompts
    # come from files/CLIs. These can otherwise break the "Act as ..." detector.
    # Include common bidi/directional markers as well (often introduced by copy/paste).
    strip_prefix = (
        "\ufeff"  # BOM
        "\u200b\u200c\u200d\u2060"  # zero-width
        "\u200e\u200f"  # LRM/RLM
        "\u202a\u202b\u202c\u202d\u202e"  # LRE/RLE/PDF/LRO/RLO
        "\u2066\u2067\u2068\u2069"  # LRI/RLI/FSI/PDI
        "\ufffd"  # replacement char (decode artifacts)
    )
    for ln in (s or "").splitlines():
        t = ln.strip()
        if t:
            return t.lstrip(strip_prefix)
    return ""


def _first_line_stripped(s: str) -> str:
    """
    Return the first line with BOM/zero-width prefixes stripped.
    This is stricter than _first_nonempty_line(): it does NOT skip blank lines.
    """
    strip_prefix = (
        "\ufeff"  # BOM
        "\u200b\u200c\u200d\u2060"  # zero-width
        "\u200e\u200f"  # LRM/RLM
        "\u202a\u202b\u202c\u202d\u202e"  # LRE/RLE/PDF/LRO/RLO
        "\u2066\u2067\u2068\u2069"  # LRI/RLI/FSI/PDI
        "\ufffd"  # replacement char (decode artifacts)
    )
    if not s:
        return ""
    lines = (s or "").splitlines()
    if not lines:
        return ""
    return lines[0].lstrip(strip_prefix).lstrip()


def _apply_persona_policy(prompt: str, persona_mode: str, persona_line: str) -> str:
    """
    Prompt hygiene: require or inject a first-line persona (e.g. "Act as a [profession]...").

    Rationale:
    - Helps Gemini and other models respond more consistently.
    - Makes automated subagent prompts less ambiguous.
    """
    mode = (persona_mode or "off").strip().lower()
    if mode == "off":
        return prompt

    first = _first_line_stripped(prompt)
    if first.lower().startswith("act as "):
        return prompt

    persona = (persona_line or "").strip()
    if not persona:
        persona = "Act as a senior software engineer."
    if not persona.lower().startswith("act as "):
        persona = "Act as " + persona
    if not persona.rstrip().endswith("."):
        persona = persona.rstrip() + "."

    if mode == "warn":
        sys.stderr.write(
            "[opencode-subtask] WARN: prompt does not start with an 'Act as ...' persona line on the FIRST line.\n"
        )
        return prompt
    if mode == "require":
        _exit_with_error(
            "PersonaMissing",
            "Prompt must start with a persona line on the FIRST line (no leading blank lines): "
            "'Act as a [profession]...'. Either add it as line 1, or set "
            "--persona-mode prepend (auto-inject) / off (disable).",
            exit_code=2,
        )
        return prompt  # unreachable
    if mode == "prepend":
        sys.stderr.write(
            "[opencode-subtask] NOTE: injected missing persona line. "
            'Prefer starting prompts with: "Act as a [profession]...".\n'
        )
        return persona + "\n" + (prompt or "")

    _exit_with_error(
        "BadPersonaMode", f"Unknown --persona-mode: {persona_mode!r}", exit_code=2
    )
    return prompt  # unreachable



def _apply_subagent_briefing(
    prompt: str,
    *,
    enabled: bool,
    final_answer_budget_chars: int,
) -> str:
    """Append a small natural-language child-agent briefing.

    This is deliberately NOT a model-output protocol.  It is a boundary hint:
    do real work in the workspace, keep the parent-facing report compact, and
    avoid dumping large diffs/logs into the parent context.  The caller can
    disable it for tasks that truly require a long final answer.
    """
    if not enabled:
        return prompt
    if "Parent-agent boundary:" in (prompt or ""):
        return prompt
    try:
        budget = int(final_answer_budget_chars)
    except Exception:
        budget = DEFAULT_FINAL_ANSWER_BUDGET_CHARS
    if budget < 0:
        budget = 0
    budget_line = (
        f" Keep the final answer under about {budget} characters unless the parent explicitly requested a longer artifact."
        if budget > 0
        else " Keep the final answer compact unless the parent explicitly requested a long artifact."
    )
    footer = (
        "\n\nParent-agent boundary:\n"
        "- Execute the task directly in the workspace when edits/tests are requested; do not turn implementation work into a planning-only reply.\n"
        "- Do not ask clarification questions unless the task is impossible or unsafe; make a reasonable engineering assumption and state it briefly.\n"
        "- Final answer: report outcome, changed files, verification commands/results, and blockers only."
        + budget_line
        + "\n- Do not paste full diffs, large logs, or long code blocks unless explicitly requested; those details belong in files/artifacts.\n"
        "- No adapter JSON is required.\n"
    )
    return (prompt or "").rstrip() + footer



def _normalize_subtask_type(raw: str | None) -> str:
    value = str(raw or DEFAULT_SUBTASK_TYPE).strip().lower()
    value = SUBTASK_TYPE_ALIASES.get(value, value)
    if value not in SUBTASK_TYPES:
        _exit_with_error(
            "BadConfig",
            "--subtask-type must be one of: " + ", ".join(sorted(SUBTASK_TYPES)),
            exit_code=2,
        )
    return value


def _subtask_default_permission_rules(
    subtask_type: str,
    *,
    allow_nested_subtasks: bool = False,
    allow_child_todos: bool = False,
) -> list[dict[str, str]]:
    """Return adapter-level child-session permission rules.

    Implementation-capable external children should keep the full OpenCode
    permission surface by default; they need it to complete edits/tests.  Only
    read-only presets get adapter deny rules, matching their role contract.
    """
    t = _normalize_subtask_type(subtask_type)
    rules: list[dict[str, str]] = []
    if not bool(SUBTASK_TYPE_PRESETS[t].get("readOnly")):
        return rules
    if not allow_child_todos:
        rules.append({"permission": "todowrite", "action": "deny", "pattern": "*"})
    if not allow_nested_subtasks:
        rules.append({"permission": "task", "action": "deny", "pattern": "*"})
    rules.extend(
        [
            {"permission": "edit", "action": "deny", "pattern": "*"},
            {"permission": "bash", "action": "deny", "pattern": "*"},
        ]
    )
    return rules


def _dedupe_permission_rules(rules: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for rule in rules:
        perm = str(rule.get("permission") or "").strip()
        action = str(rule.get("action") or "").strip()
        pattern = str(rule.get("pattern") or "*").strip() or "*"
        if not perm or not action:
            continue
        key = (perm, pattern, action)
        if key in seen:
            continue
        seen.add(key)
        out.append({"permission": perm, "action": action, "pattern": pattern})
    return out


def _permission_rules_to_config(rules: Iterable[dict[str, str]]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for rule in rules:
        perm = str(rule.get("permission") or "").strip()
        action = str(rule.get("action") or "").strip()
        pattern = str(rule.get("pattern") or "*").strip() or "*"
        if not perm or action not in {"allow", "ask", "deny"}:
            continue
        if pattern == "*":
            cfg[perm] = action
            continue
        cur = cfg.get(perm)
        if isinstance(cur, dict):
            cur[pattern] = action
        elif isinstance(cur, str):
            cfg[perm] = {"*": cur, pattern: action}
        else:
            cfg[perm] = {pattern: action}
    return cfg


def _merge_permission_rule_config(base: dict[str, Any], rules: Iterable[dict[str, str]]) -> dict[str, Any]:
    merged = dict(base)
    for perm, value in _permission_rules_to_config(rules).items():
        if isinstance(value, dict) and isinstance(merged.get(perm), dict):
            cur = dict(merged[perm])
            cur.update(value)
            merged[perm] = cur
        else:
            merged[perm] = value
    return merged


def _apply_subtask_permission_rules_to_env(env: dict[str, str], rules: list[dict[str, str]]) -> None:
    if not rules:
        return
    raw = env.get("OPENCODE_PERMISSION")
    base: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                base = parsed
            elif isinstance(parsed, str) and parsed in {"allow", "ask", "deny"}:
                base = {"*": parsed}
        except Exception:
            if raw.strip() in {"allow", "ask", "deny"}:
                base = {"*": raw.strip()}
    env["OPENCODE_PERMISSION"] = json.dumps(
        _merge_permission_rule_config(base, rules), ensure_ascii=False
    )


def _apply_subtask_preset(args: argparse.Namespace) -> dict[str, Any]:
    """Apply adapter-level role preset and return stable subtask metadata."""
    subtask_type = _normalize_subtask_type(getattr(args, "subtask_type", None))
    setattr(args, "subtask_type", subtask_type)
    preset = SUBTASK_TYPE_PRESETS[subtask_type]

    explicit_agent = bool(str(getattr(args, "agent", "") or "").strip())
    preset_agent = preset.get("agent") if isinstance(preset.get("agent"), str) else None
    if (not explicit_agent) and preset_agent:
        setattr(args, "agent", preset_agent)

    explicit_profile = str(
        getattr(args, "execution_profile", DEFAULT_EXECUTION_PROFILE)
        or DEFAULT_EXECUTION_PROFILE
    ).strip().lower() != DEFAULT_EXECUTION_PROFILE
    preset_profile = preset.get("executionProfile")
    if (not explicit_profile) and isinstance(preset_profile, str) and preset_profile:
        setattr(args, "execution_profile", preset_profile)

    description = str(getattr(args, "description", "") or "").strip()
    if description and not str(getattr(args, "title", "") or "").strip():
        title = f"{description} (@opencode-subtask/{subtask_type})"
        setattr(args, "title", title[:160])

    rules = _subtask_default_permission_rules(
        subtask_type,
        allow_nested_subtasks=bool(getattr(args, "allow_nested_subtasks", False)),
        allow_child_todos=bool(getattr(args, "allow_child_todos", False)),
    )
    acceptance = list(getattr(args, "acceptance", []) or [])
    return {
        "type": subtask_type,
        "taskId": getattr(args, "_adapter_task_id", None),
        "description": description or None,
        "brief": str(preset.get("brief") or ""),
        "agent": getattr(args, "agent", None),
        "agentSource": "explicit" if explicit_agent else ("preset" if preset_agent else "default"),
        "executionProfile": getattr(args, "execution_profile", None),
        "readOnly": bool(preset.get("readOnly")),
        "permissionRules": rules,
        "acceptance": acceptance,
        "allowNestedSubtasks": bool(getattr(args, "allow_nested_subtasks", False)),
        "allowChildTodos": bool(getattr(args, "allow_child_todos", False)),
    }


def _effective_http_deny_interactive_tools(
    args: argparse.Namespace, subtask_info: dict[str, Any]
) -> bool:
    raw = getattr(args, "http_deny_interactive_tools", None)
    if raw is None:
        return bool(subtask_info.get("readOnly"))
    return bool(raw)


def _apply_subtask_briefing(
    prompt: str,
    *,
    subtask_info: dict[str, Any] | None,
) -> str:
    if not isinstance(subtask_info, dict):
        return prompt
    t = str(subtask_info.get("type") or DEFAULT_SUBTASK_TYPE)
    if "OpenCode child role:" in (prompt or ""):
        return prompt
    read_only = bool(subtask_info.get("readOnly"))
    task_id = str(subtask_info.get("taskId") or "").strip()
    prefix = f"\n\nOpenCode external task_id: {task_id}." if task_id else "\n\n"
    if read_only:
        role_line = (
            prefix
            + f" OpenCode child role: {t}. This is a read-only delegated task; inspect and report, "
            "but do not edit files or run mutating shell commands."
        )
    elif t in {"worker", "fast-worker"}:
        role_line = (
            prefix
            + f" OpenCode child role: {t}. Implement directly when the task asks for changes, "
            "run the most relevant verification, and return only the useful final report."
        )
    else:
        role_line = (
            prefix
            + f" OpenCode child role: {t}. Treat this as one delegated child-agent task with fresh context; "
            "do not re-delegate the same task to another agent."
        )
    acceptance = list(subtask_info.get("acceptance") or [])
    if acceptance:
        items = [str(x).strip() for x in acceptance if str(x).strip()]
        if items:
            role_line += "\nAcceptance criteria / stop condition:\n" + "\n".join(f"- {x}" for x in items)
    return (prompt or "").rstrip() + role_line + "\n"

def _ask_metadata_obj(finish_obj: dict[str, Any]) -> dict[str, Any]:
    execution = finish_obj.get("execution") if isinstance(finish_obj.get("execution"), dict) else {}
    artifacts = finish_obj.get("artifacts") if isinstance(finish_obj.get("artifacts"), dict) else {}
    workspace = finish_obj.get("workspace") if isinstance(finish_obj.get("workspace"), dict) else {}
    artifact_dir = artifacts.get("dir") if isinstance(artifacts, dict) else None

    def artifact_abs(name_key: str) -> str | None:
        if not isinstance(artifacts, dict) or not isinstance(artifact_dir, str):
            return None
        name = artifacts.get(name_key)
        if not isinstance(name, str) or not name:
            return None
        return str(Path(artifact_dir).expanduser() / name)

    def workspace_patch_abs() -> str | None:
        if not isinstance(workspace, dict) or not isinstance(artifact_dir, str):
            return None
        patch_name = workspace.get("patchPath")
        if not isinstance(patch_name, str) or not patch_name:
            return None
        patch_path = Path(patch_name).expanduser()
        if patch_path.is_absolute():
            return str(patch_path)
        return str(Path(artifact_dir).expanduser() / patch_path)

    session_id = execution.get("sessionId") if isinstance(execution, dict) else None
    run_id = finish_obj.get("runId")
    subtask = finish_obj.get("subtask") if isinstance(finish_obj.get("subtask"), dict) else {}
    adapter_task_id = subtask.get("taskId") if isinstance(subtask, dict) else None
    return {
        "type": "opencode-subtask-ask-metadata",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": finish_obj.get("outcome") == "completed",
        "runId": run_id,
        "taskId": adapter_task_id or session_id or run_id,
        "sessionId": session_id,
        "outcome": finish_obj.get("outcome"),
        "subtask": finish_obj.get("subtask"),
        "engine": execution.get("engine") if isinstance(execution, dict) else None,
        "changedFiles": workspace.get("changedFiles") if isinstance(workspace, dict) else None,
        "artifacts": {
            "dir": artifact_dir,
            "finishPath": artifact_abs("finishPath"),
            "assistantPath": artifact_abs("assistantPath"),
            "patchPath": workspace_patch_abs(),
            "taskStatePath": subtask.get("taskStatePath") if isinstance(subtask, dict) else None,
            "taskProgressPath": subtask.get("taskProgressPath") if isinstance(subtask, dict) else None,
        },
    }


def _truncate_for_ask_stdout(
    text: str,
    *,
    max_chars: int,
    assistant_path: Path | None,
) -> tuple[str, bool]:
    """Cap ask stdout while preserving the full answer in assistant.txt."""
    try:
        cap = int(max_chars)
    except Exception:
        cap = DEFAULT_ASK_STDOUT_MAX_CHARS
    if cap <= 0 or len(text) <= cap:
        return text, False
    note = "\n\n[opencode-subtask: final answer truncated at " + str(cap) + " chars"
    if assistant_path:
        note += "; full assistant text is in " + str(assistant_path)
    note += "]"
    # Reserve room for the note when possible.
    keep = max(0, cap - len(note))
    return text[:keep].rstrip() + note, True

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _atomic_write_bytes(
    path: Path, data: bytes, *, retries: int = 5, sleep_s: float = 0.05
) -> None:
    """
    Atomic-ish write: write temp then os.replace.
    On Windows, os.replace may fail transiently due to AV/indexers locking files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{_now_ms()}")
    tmp.write_bytes(data)
    last_err: Exception | None = None
    for i in range(max(retries, 1)):
        try:
            os.replace(tmp, path)
            return
        except Exception as e:  # pragma: no cover
            last_err = e
            time.sleep(sleep_s * (i + 1))
    # final attempt without swallowing
    if last_err:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[attr-defined]
        except Exception:
            pass
        raise last_err


def _write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(path, data)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_read_text(path))
    except Exception:
        return None


def _tail_bytes(path: Path, max_bytes: int = 2048) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(size - max_bytes, 0))
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _stderr_failure_error(stderr_path: Path, exit_code: int) -> dict[str, str]:
    tail = _tail_bytes(stderr_path, max_bytes=4096).strip()
    if tail:
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        condensed = " | ".join(lines[-3:])
        condensed = " ".join(condensed.split())
        if (
            "oh no: Bun has crashed" in tail
            or "panic(main thread):" in tail
            or "Segmentation fault" in tail
        ):
            return {
                "name": "EngineCrash",
                "message": (
                    f"opencode runtime crashed before producing a stable result "
                    f"(exit_code={exit_code}); stderr={condensed[:1000]}"
                ),
            }
        return {
            "name": "NonZeroExit",
            "message": f"opencode exit_code={exit_code}; stderr={condensed[:1000]}",
        }
    return {"name": "NonZeroExit", "message": f"opencode exit_code={exit_code}"}


def _join_prompt(args_prompt: list[str]) -> str:
    parts = list(args_prompt or [])
    # argparse + nargs=REMAINDER keeps a literal `--` in the remainder list.
    # Treat it as a separator and drop it to match the CLI help text.
    while parts and parts[0] == "--":
        parts = parts[1:]
    prompt = " ".join(parts).strip()
    if prompt:
        return prompt
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data
    _exit_with_error(
        "MissingPrompt",
        "Missing prompt. Pass after `--`, via --prompt/--prompt-file, or via stdin.",
        exit_code=2,
    )


def _resolve_prompt_input(args: argparse.Namespace) -> str:
    has_file = bool(getattr(args, "prompt_file", None))
    has_text = getattr(args, "prompt_text", None) is not None
    prompt_parts = list(getattr(args, "prompt", []) or [])
    prompt_probe = list(prompt_parts)
    while prompt_probe and prompt_probe[0] == "--":
        prompt_probe = prompt_probe[1:]
    has_positional = bool(" ".join(prompt_probe).strip())

    chosen = int(has_file) + int(has_text) + int(has_positional)
    if chosen > 1:
        _exit_with_error(
            "PromptConflict",
            "Use exactly one prompt source: --prompt-file, --prompt, or positional args after `--`.",
            exit_code=2,
        )

    if has_file:
        prompt_file_path = Path(str(args.prompt_file)).expanduser().resolve()
        try:
            return _read_text(prompt_file_path)
        except SystemExit:
            raise
        except Exception as e:
            _exit_with_error(
                "PromptFileReadError",
                f"Could not read --prompt-file {prompt_file_path}: {type(e).__name__}: {e}",
                exit_code=2,
            )
    if has_text:
        return str(args.prompt_text)
    return _join_prompt(prompt_parts)


def _read_env_float(env: dict[str, str], key: str) -> float | None:
    raw = env.get(key)
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        return float(txt)
    except Exception:
        _exit_with_error(
            "BadConfig", f"{key} must be a float, got: {raw!r}", exit_code=2
        )


def _read_env_int(env: dict[str, str], key: str) -> int | None:
    raw = env.get(key)
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        return int(txt)
    except Exception:
        _exit_with_error(
            "BadConfig", f"{key} must be an int, got: {raw!r}", exit_code=2
        )


def _resolve_hybrid_thresholds(
    args: argparse.Namespace, env: dict[str, str]
) -> tuple[float, str, int, str]:
    timeout_s = HYBRID_SHORT_TIMEOUT_S
    timeout_source = "default"
    prompt_chars = HYBRID_SHORT_PROMPT_CHARS
    prompt_source = "default"

    env_timeout = _read_env_float(env, "OPENCODE_SUBTASK_HYBRID_SHORT_TIMEOUT_S")
    if env_timeout is not None:
        if env_timeout <= 0:
            _exit_with_error(
                "BadConfig",
                f"OPENCODE_SUBTASK_HYBRID_SHORT_TIMEOUT_S must be > 0, got: {env_timeout}",
                exit_code=2,
            )
        timeout_s = float(env_timeout)
        timeout_source = "env"

    env_prompt_chars = _read_env_int(env, "OPENCODE_SUBTASK_HYBRID_SHORT_PROMPT_CHARS")
    if env_prompt_chars is not None:
        if env_prompt_chars <= 0:
            _exit_with_error(
                "BadConfig",
                f"OPENCODE_SUBTASK_HYBRID_SHORT_PROMPT_CHARS must be > 0, got: {env_prompt_chars}",
                exit_code=2,
            )
        prompt_chars = int(env_prompt_chars)
        prompt_source = "env"

    flag_timeout = getattr(args, "hybrid_short_timeout_s", None)
    if flag_timeout is not None:
        if float(flag_timeout) <= 0:
            _exit_with_error(
                "BadConfig",
                f"--hybrid-short-timeout-s must be > 0, got: {flag_timeout}",
                exit_code=2,
            )
        timeout_s = float(flag_timeout)
        timeout_source = "flag"

    flag_prompt_chars = getattr(args, "hybrid_short_prompt_chars", None)
    if flag_prompt_chars is not None:
        if int(flag_prompt_chars) <= 0:
            _exit_with_error(
                "BadConfig",
                f"--hybrid-short-prompt-chars must be > 0, got: {flag_prompt_chars}",
                exit_code=2,
            )
        prompt_chars = int(flag_prompt_chars)
        prompt_source = "flag"

    return timeout_s, timeout_source, prompt_chars, prompt_source


def _apply_execution_profile(
    args: argparse.Namespace, prompt: str, env: dict[str, str]
) -> dict[str, Any]:
    """
    Hybrid routing policy for engine/artifact behavior.
    - short tasks: prefer HTTP + lighter artifacts
    - long tasks: prefer CLI + full artifacts
    """
    raw_profile = str(
        getattr(args, "execution_profile", DEFAULT_EXECUTION_PROFILE)
        or DEFAULT_EXECUTION_PROFILE
    )
    profile = raw_profile.strip().lower()
    if profile not in EXECUTION_PROFILES:
        profile = DEFAULT_EXECUTION_PROFILE

    prompt_chars = len(prompt)
    timeout_s = float(getattr(args, "run_timeout", DEFAULT_TIMEOUT_S))
    (
        hybrid_short_timeout_s,
        hybrid_timeout_source,
        hybrid_short_prompt_chars,
        hybrid_prompt_source,
    ) = _resolve_hybrid_thresholds(args, env)
    short_task = (
        timeout_s <= hybrid_short_timeout_s
        and prompt_chars <= hybrid_short_prompt_chars
    )

    # Explicit modes.
    if profile == "latency":
        if getattr(args, "engine", "auto") == "auto":
            if getattr(args, "attach", None) or bool(
                getattr(args, "attach_server", True)
            ):
                args.engine = "http"
            else:
                args.engine = "cli"
        args.save_events = False
        args.save_text = False
        return {
            "profile": profile,
            "taskClass": "short",
            "promptChars": prompt_chars,
            "timeoutS": timeout_s,
            "hybridShortTimeoutS": hybrid_short_timeout_s,
            "hybridShortPromptChars": hybrid_short_prompt_chars,
            "hybridThresholdSource": {
                "timeoutS": hybrid_timeout_source,
                "promptChars": hybrid_prompt_source,
            },
        }

    if profile == "checkpoint":
        if getattr(args, "engine", "auto") == "auto":
            args.engine = "cli"
        args.save_events = True
        args.save_text = True
        return {
            "profile": profile,
            "taskClass": "long",
            "promptChars": prompt_chars,
            "timeoutS": timeout_s,
            "hybridShortTimeoutS": hybrid_short_timeout_s,
            "hybridShortPromptChars": hybrid_short_prompt_chars,
            "hybridThresholdSource": {
                "timeoutS": hybrid_timeout_source,
                "promptChars": hybrid_prompt_source,
            },
        }

    # hybrid
    if short_task:
        if getattr(args, "engine", "auto") == "auto":
            if getattr(args, "attach", None) or bool(
                getattr(args, "attach_server", True)
            ):
                args.engine = "http"
            else:
                args.engine = "cli"
        args.save_events = False
        args.save_text = False
        task_class = "short"
    else:
        if getattr(args, "engine", "auto") == "auto":
            args.engine = "cli"
        args.save_events = True
        args.save_text = True
        task_class = "long"

    return {
        "profile": profile,
        "taskClass": task_class,
        "promptChars": prompt_chars,
        "timeoutS": timeout_s,
        "hybridShortTimeoutS": hybrid_short_timeout_s,
        "hybridShortPromptChars": hybrid_short_prompt_chars,
        "hybridThresholdSource": {
            "timeoutS": hybrid_timeout_source,
            "promptChars": hybrid_prompt_source,
        },
    }


def _merge_env(
    base: dict[str, str], set_vars: list[str], set_from_files: list[str]
) -> dict[str, str]:
    env = dict(base)
    for item in set_vars:
        if "=" not in item:
            raise ValueError(f"--env expects KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        env[k.strip()] = v
    for item in set_from_files:
        if "=" not in item:
            raise ValueError(f"--env-file expects KEY=PATH, got: {item}")
        k, p = item.split("=", 1)
        env[k.strip()] = _read_text(Path(p).expanduser().resolve())
    return env


def _safe_merge_env(
    base: dict[str, str], set_vars: list[str], set_from_files: list[str]
) -> dict[str, str]:
    """Command-entry wrapper: converts ValueError/OSError → BadArgs (exit 2).

    OSError covers env-file not found / permission denied.
    UnicodeDecodeError is a ValueError subclass, already caught.
    """
    try:
        return _merge_env(base, set_vars, set_from_files)
    except (ValueError, OSError) as exc:
        _exit_with_error("BadArgs", str(exc), exit_code=2)


# ============================
# Paths
# ============================


def _cache_root() -> Path:
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
    return base / "opencode-subtask"


def _runs_dir() -> Path:
    return _cache_root() / "runs"


def _servers_dir() -> Path:
    return _cache_root() / "servers"


def _tasks_dir() -> Path:
    return _cache_root() / "tasks"


def _task_project_dir(workdir: Path) -> Path:
    return _tasks_dir() / _project_key(workdir)


def _task_state_path(workdir: Path, task_id: str) -> Path:
    _validate_safe_id(task_id, label="task_id")
    return _task_project_dir(workdir) / f"{task_id}.json"


def _task_lock_path(workdir: Path, task_id: str) -> Path:
    return _task_state_path(workdir, task_id).with_suffix(".lock")


def _task_progress_path(workdir: Path, task_id: str) -> Path:
    _validate_safe_id(task_id, label="task_id")
    return _task_project_dir(workdir) / f"{task_id}.progress.md"


def _prune_run_artifacts(*, keep_last: int, dry_run: bool) -> dict[str, Any]:
    """
    Prune local on-disk run artifacts under the cache root.

    This addresses cache growth (disk), not a memory leak.

    Policy:
    - Keep the most-recent `keep_last` run directories by mtime; delete the rest.
    - Safe-by-default: `dry_run=True` does not delete.
    """
    keep_last = int(keep_last)
    if keep_last < 0:
        keep_last = 0

    runs_dir = _runs_dir()
    if not runs_dir.exists():
        return {
            "runsDir": str(runs_dir),
            "total": 0,
            "kept": 0,
            "candidates": 0,
            "deleted": 0,
            "dryRun": dry_run,
            "errors": [],
        }

    items: list[tuple[float, Path]] = []
    for p in runs_dir.iterdir():
        if not p.is_dir():
            continue
        try:
            st = p.stat()
        except Exception:
            continue
        items.append((float(st.st_mtime), p))

    items.sort(key=lambda t: t[0], reverse=True)
    candidates = [p for _, p in items[keep_last:]]

    errors: list[dict[str, str]] = []
    deleted = 0
    if not dry_run:
        for p in candidates:
            try:
                rmtree(p, ignore_errors=False)
                deleted += 1
            except Exception as e:
                errors.append({"path": str(p), "error": f"{type(e).__name__}: {e}"})

    return {
        "runsDir": str(runs_dir),
        "total": len(items),
        "kept": min(len(items), keep_last),
        "candidates": len(candidates),
        "deleted": deleted,
        "dryRun": dry_run,
        "errors": errors,
    }


def _find_git_root(start: Path) -> Path:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return cur


def _project_key(workdir: Path) -> str:
    root = _find_git_root(workdir)
    h = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return h[:12]


def _server_state_path(workdir: Path) -> Path:
    return _servers_dir() / f"{_project_key(workdir)}.json"


def _server_log_path(workdir: Path) -> Path:
    return _servers_dir() / f"{_project_key(workdir)}.log"


def _server_lock_path(workdir: Path) -> Path:
    return _servers_dir() / f"{_project_key(workdir)}.lock"


def _state_lock_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "state.lock"


def _finish_lock_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / "finish.lock"


def _win_hide_popen_kwargs(*, detached: bool) -> dict[str, Any]:
    """
    Best-effort to prevent Windows console windows from flashing open.

    - For background processes (server/worker): detached=True.
    - For CLI runs (need stdout pipes): detached=False.
    """
    if os.name != "nt":
        return {}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached:
        creationflags |= (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )

    kw: dict[str, Any] = {"creationflags": creationflags}
    try:
        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kw["startupinfo"] = si
    except Exception:
        pass
    return kw


def _make_run_id() -> str:
    return f"run_{_now_ms()}_{os.getpid()}"


def _make_task_id() -> str:
    return f"task_{_now_ms()}_{os.getpid()}"


_RUN_ID_RE = re.compile(r"^[\w.\-]+$")  # alphanumeric, underscore, dot, hyphen


def _validate_safe_id(value: str, *, label: str) -> None:
    """Reject ids that could escape cache directories via path traversal."""
    if value in (".", ".."):
        raise ValueError(f"{label} must not be a relative directory alias: {value!r}")
    if ".." in value.split("/") or ".." in value.split("\\"):
        raise ValueError(f"{label} contains path traversal component: {value!r}")
    if "/" in value or "\\" in value:
        raise ValueError(f"{label} contains path separator: {value!r}")
    if not _RUN_ID_RE.match(value):
        raise ValueError(f"{label} contains disallowed characters: {value!r}")


def _validate_run_id_for_path(rid: str) -> None:
    """Reject run_id values that could escape _runs_dir() via path traversal.

    Raises ValueError if the run_id contains path separators, ``..``
    components, or characters outside the safe whitelist.
    """
    _validate_safe_id(rid, label="run_id")


def _resolve_artifacts_dir(
    run_id: str | None, artifacts_dir: str | None
) -> tuple[str, Path]:
    rid = run_id or _make_run_id()
    if artifacts_dir:
        ad = Path(artifacts_dir).expanduser().resolve()
    else:
        _validate_run_id_for_path(rid)
        runs = _runs_dir().resolve()
        ad = (runs / rid).resolve()
        # Belt-and-suspenders: ensure resolved path stays under runs_dir
        # even if symlinks or OS-level tricks bypass the regex whitelist.
        try:
            ad.relative_to(runs)
        except ValueError:
            raise ValueError(f"run_id resolves outside runs directory: {rid!r} -> {ad}")
    return rid, ad


def _safe_resolve_artifacts_dir(
    run_id: str | None, artifacts_dir: str | None
) -> tuple[str, Path]:
    """Command-entry wrapper: converts ValueError → BadRunId (exit 2)."""
    try:
        return _resolve_artifacts_dir(run_id, artifacts_dir)
    except ValueError as exc:
        _exit_with_error("BadRunId", str(exc), exit_code=2)



@dataclass
class TaskResolution:
    task_id: str
    state_path: Path
    progress_path: Path
    prior_state: dict[str, Any] | None
    created_new: bool
    is_external: bool


def _load_task_state(workdir: Path, task_id: str) -> dict[str, Any] | None:
    try:
        p = _task_state_path(workdir, task_id)
    except ValueError:
        return None
    obj = _load_json(p)
    return obj if isinstance(obj, dict) else None


def _resolve_task_id_for_run(args: argparse.Namespace, workdir: Path) -> TaskResolution:
    """Resolve adapter task identity and optionally hydrate OpenCode session continuation.

    `--task-id` is now the caller-facing handle. If it maps to a known adapter
    task, the latest OpenCode session id is reused unless --session explicitly
    overrides it. Unknown safe ids create a new adapter task handle. Raw
    OpenCode session continuation must use --session explicitly.
    """
    raw = str(getattr(args, "task_id", "") or "").strip()
    prior: dict[str, Any] | None = None
    is_external = False
    created_new = False

    if raw:
        try:
            _validate_safe_id(raw, label="task_id")
        except ValueError as exc:
            _exit_with_error("BadTaskId", str(exc), exit_code=2)
        prior = _load_task_state(workdir, raw)
        task_id = raw
        is_external = True
        if prior is None:
            created_new = True
    else:
        task_id = _make_task_id()
        created_new = True
        is_external = True

    state_path = _task_state_path(workdir, task_id)
    progress_path = _task_progress_path(workdir, task_id)

    if prior:
        session_id = str(prior.get("sessionId") or "").strip()
        if session_id and not str(getattr(args, "session", "") or "").strip():
            setattr(args, "session", session_id)
        sub = prior.get("subtask") if isinstance(prior.get("subtask"), dict) else {}
        # Preserve prior role on continuation unless caller selected something non-default.
        if (
            str(getattr(args, "subtask_type", DEFAULT_SUBTASK_TYPE) or DEFAULT_SUBTASK_TYPE)
            == DEFAULT_SUBTASK_TYPE
            and isinstance(sub.get("type"), str)
            and sub.get("type") in SUBTASK_TYPES
        ):
            setattr(args, "subtask_type", sub.get("type"))
        for field in ("agent", "model", "variant"):
            cur = str(getattr(args, field, "") or "").strip()
            val = prior.get(field)
            if not cur and isinstance(val, str) and val:
                setattr(args, field, val)

    return TaskResolution(
        task_id=task_id,
        state_path=state_path,
        progress_path=progress_path,
        prior_state=prior,
        created_new=created_new,
        is_external=is_external,
    )


def _task_is_running(state: dict[str, Any] | None) -> bool:
    """Best-effort duplicate guard for a caller-facing adapter task id.

    A task state can be left as ``running`` if the parent process is hard-killed.
    Treat a task as actively running only when its latest job still has a live
    worker pid or an HTTP session without a terminal finish.  This mirrors native
    task handles: avoid double-starting live work, but do not permanently lock a
    recoverable task id after a crash.
    """
    if not isinstance(state, dict):
        return False
    state_name = str(state.get("state") or "").lower()
    artifacts = str(state.get("latestArtifactsDir") or "").strip()
    if artifacts:
        ad = Path(artifacts)
        if (ad / "finish.json").exists():
            return False
        job = _load_json(ad / "job.json")
        if isinstance(job, dict):
            pid = _safe_int(job.get("pid")) or 0
            job_state = str(job.get("state") or "").lower()
            if pid > 0 and _pid_running(pid) and job_state in {"queued", "running", "cancel_requested"}:
                return True
            if job.get("sessionId") and job_state in {"queued", "running", "cancel_requested"}:
                return True
    return state_name in {"starting"} and not artifacts


def _safe_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _read_tail_text(path: Path, max_chars: int) -> str:
    try:
        if max_chars <= 0 or not path.exists():
            return ""
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_chars), os.SEEK_SET)
            except Exception:
                pass
            data = f.read(max_chars + 1024)
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text.strip()
    except Exception:
        return ""


def _task_memory_block(res: TaskResolution, *, max_chars: int) -> str:
    """Build a compact MiMo-style continuation memory block for task resumption."""
    st = res.prior_state
    if not isinstance(st, dict):
        return ""
    try:
        cap = max(0, int(max_chars))
    except Exception:
        cap = DEFAULT_TASK_MEMORY_MAX_CHARS
    if cap <= 0:
        return ""
    pieces: list[str] = []
    pieces.append(f"task_id: {res.task_id}")
    for key in ("state", "outcome", "sessionId", "latestRunId"):
        val = st.get(key)
        if val:
            pieces.append(f"{key}: {val}")
    changed = st.get("changedFiles")
    if isinstance(changed, list) and changed:
        pieces.append("changedFiles: " + ", ".join(str(x) for x in changed[:20]))
    last_progress = _read_tail_text(res.progress_path, max(800, min(cap, 4000)))
    if last_progress:
        pieces.append("recent progress:\n" + last_progress)
    block = "\n".join(pieces).strip()
    if len(block) > cap:
        block = block[-cap:].lstrip()
        block = "[truncated task memory]\n" + block
    return (
        "\n\nExternal task continuation memory (adapter-generated, bounded):\n"
        "```text\n" + block + "\n```\n"
        "Use this only to avoid re-discovering prior state; the current user task remains authoritative.\n"
    )


def _append_task_progress(path: Path, *, event: str, fields: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} {event}"]
        for k, v in fields.items():
            if v is None or v == [] or v == {}:
                continue
            if isinstance(v, (dict, list)):
                val = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            else:
                val = str(v)
            if len(val) > 1200:
                val = val[:1200].rstrip() + "..."
            lines.append(f"- {k}: {val}")
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def _write_task_state_locked(
    *,
    workdir: Path,
    task_id: str,
    update: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        state_path = _task_state_path(workdir, task_id)
        lock_path = _task_lock_path(workdir, task_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with _FileLock(lock_path):
            cur = _load_json(state_path)
            if not isinstance(cur, dict):
                cur = {
                    "type": "opencode-subtask-task-state",
                    "schemaVersion": ADAPTER_SCHEMA_VERSION,
                    "adapterVersion": ADAPTER_VERSION,
                    "taskId": task_id,
                    "projectKey": _project_key(workdir),
                    "workdir": str(workdir),
                    "createdAt": _now_ms(),
                }
            cur.update(update)
            cur["adapterVersion"] = ADAPTER_VERSION
            cur["updatedAt"] = _now_ms()
            _write_json(state_path, cur)
            return cur
    except Exception:
        return None


def _resolve_task_artifacts(
    *,
    workdir: Path,
    task_id: str | None,
    run_id: str | None,
    artifacts_dir: str | None,
) -> tuple[str, Path, dict[str, Any] | None]:
    if task_id:
        try:
            _validate_safe_id(task_id, label="task_id")
        except ValueError as exc:
            _exit_with_error("BadTaskId", str(exc), exit_code=2)
        st = _load_task_state(workdir, task_id)
        if not isinstance(st, dict):
            _exit_with_error("TaskNotFound", f"No adapter task state for task_id={task_id!r}", exit_code=2)
        ad = st.get("latestArtifactsDir")
        rid = st.get("latestRunId")
        if not isinstance(ad, str) or not ad:
            _exit_with_error("TaskNoRun", f"Task {task_id!r} has no recorded run artifacts", exit_code=2)
        return str(rid or task_id), Path(ad).expanduser().resolve(), st
    rid, ad = _safe_resolve_artifacts_dir(run_id, artifacts_dir)
    return rid, ad, None


def _write_notification(
    *,
    artifacts_dir: Path,
    finish_obj: dict[str, Any],
    task_state_path: Path | None,
) -> Path | None:
    try:
        assistant_path = artifacts_dir / "assistant.txt"
        preview = _read_tail_text(assistant_path, 4000) if assistant_path.exists() else ""
        obj = {
            "type": "opencode-subtask-notification",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "timestamp": _now_ms(),
            "taskId": (finish_obj.get("subtask") or {}).get("taskId") if isinstance(finish_obj.get("subtask"), dict) else None,
            "runId": finish_obj.get("runId"),
            "sessionId": (finish_obj.get("execution") or {}).get("sessionId") if isinstance(finish_obj.get("execution"), dict) else None,
            "outcome": finish_obj.get("outcome"),
            "workdir": finish_obj.get("workdir"),
            "assistantPreview": preview,
            "artifactsDir": str(artifacts_dir),
            "finishPath": str(artifacts_dir / "finish.json"),
            "assistantPath": str(assistant_path) if assistant_path.exists() else None,
            "taskStatePath": str(task_state_path) if task_state_path else None,
        }
        p = artifacts_dir / "notification.json"
        _write_json(p, obj)
        return p
    except Exception:
        return None

def _canonical_run_id(run_id: str, job: Any) -> str:
    """Return the authoritative run_id: prefer job.json's recorded runId.

    When status/wait/cancel are invoked with only ``--artifacts-dir`` (no
    ``--run-id``), :func:`_resolve_artifacts_dir` generates a fresh run_id
    that won't match the real worker's ID stored in job.json.  This helper
    resolves the discrepancy by preferring the job-recorded value.

    The result is stripped of surrounding whitespace and rejected if it
    contains control characters (newlines, tabs, NUL, etc.) to prevent
    identity-field injection via a crafted job.json.
    """
    candidate = run_id
    if isinstance(job, dict) and job.get("runId"):
        candidate = str(job["runId"])
    candidate = candidate.strip()
    # Reject control characters (< 0x20 except nothing, plus DEL 0x7f).
    if any(c < " " or c == "\x7f" for c in candidate):
        # Fall back to the original (already generated) run_id stripped,
        # which is a safe UUID produced by _resolve_artifacts_dir.
        return run_id.strip()
    return candidate


# ============================
# Process helpers
# ============================


def _pid_running_state(pid: int) -> tuple[bool, bool]:
    """
    Return (is_running, probe_known). probe_known=False means the probe itself
    was inconclusive (for example, command timeout).
    """
    if pid <= 0:
        return (False, True)
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return (True, True)
        except OSError:
            return (False, True)
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        ).decode("utf-8", errors="replace")
        pid_txt = re.escape(str(pid))
        for raw_line in out.splitlines():
            line = raw_line.strip()
            if not line or line.upper().startswith("INFO:"):
                continue
            if re.search(rf'^"[^"]*","{pid_txt}"(?:,|$)', line):
                return (True, True)
        return (False, True)
    except subprocess.TimeoutExpired:
        return (False, False)
    except Exception:
        return (False, False)


def _pid_running(pid: int) -> bool:
    alive, known = _pid_running_state(pid)
    if known:
        return alive
    if os.name == "nt":
        # Global conservative default for uncertain Windows probes. Some
        # call sites (such as cancel latch) handle probe-known separately.
        return True
    return alive


def _kill_tree(pid: int, *, sig: int | None = None) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
    signal_to_send = int(sig) if sig is not None else int(signal.SIGTERM)
    try:
        os.killpg(pid, signal_to_send)
        return True
    except Exception:
        try:
            os.kill(pid, signal_to_send)
            return True
        except Exception:
            return False


def _proc_cmdline(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name != "nt":
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            try:
                raw = proc_cmdline.read_bytes()
                return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
            except Exception:
                pass
        # macOS/BSD fallback where /proc may be unavailable.
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            ).decode("utf-8", errors="replace")
            return out.strip()
        except Exception:
            return ""
    # Windows: wmic is deprecated but still common; fall back to tasklist if needed.
    try:
        out = subprocess.check_output(
            [
                "wmic",
                "process",
                "where",
                f"processid={pid}",
                "get",
                "CommandLine",
                "/VALUE",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        ).decode("utf-8", errors="replace")
        if out and ("CommandLine" in out):
            return out
    except Exception:
        pass
    # Windows 11+ fallback where wmic may be unavailable.
    try:
        ps_cmd = (
            f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
            "| Select-Object -First 1 -ExpandProperty CommandLine; "
            "if ($p) { [Console]::Out.Write($p) }"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        ).decode("utf-8", errors="replace")
        return out.strip()
    except Exception:
        return ""


def _extract_server_port_from_url(url: str) -> int | None:
    m = re.match(
        r"^[a-zA-Z][a-zA-Z0-9+.\-]*://(?:\[[^\]]+\]|[^:/?#]+):(\d+)(?:[/?#]|$)",
        str(url or "").strip(),
    )
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _split_cmdline_tokens(cmdline: str) -> list[str]:
    txt = str(cmdline or "").strip()
    if not txt:
        return []
    try:
        return shlex.split(txt, posix=(os.name != "nt"))
    except Exception:
        return re.findall(r'"[^"]*"|\'[^\']*\'|\S+', txt)


def _normalize_cmd_token(tok: str) -> str:
    t = str(tok or "").strip().strip("\"'").lower()
    if t.startswith("commandline="):
        t = t[len("commandline=") :]
    return t


def _extract_flag_value(cmdline: str, flag: str) -> str | None:
    """
    Extract a CLI flag value from command line tokens.
    Supports both: `--flag value` and `--flag=value`.
    Returns normalized lowercase value when present, otherwise None.
    """
    tokens = _split_cmdline_tokens(cmdline)
    f = str(flag or "").strip().lower()
    i = 0
    while i < len(tokens):
        t = _normalize_cmd_token(tokens[i])
        if not t:
            i += 1
            continue
        if t == f:
            if i + 1 < len(tokens):
                return _normalize_cmd_token(tokens[i + 1])
            return ""
        if t.startswith(f + "="):
            return _normalize_cmd_token(t[len(f) + 1 :])
        i += 1
    return None


def _has_serve_token(cmdline: str) -> bool:
    tokens = _split_cmdline_tokens(cmdline)
    for tok in tokens:
        t = _normalize_cmd_token(tok)
        if t == "serve":
            return True
    return False


def _looks_like_opencode_identity(tok: str) -> bool:
    n = Path(str(tok or "")).name.lower()
    if not n:
        return False
    return bool(re.search(r"(^|[^a-z0-9])opencode([^a-z0-9]|$)", n))


def _expected_server_exec_token(st: dict[str, Any] | None) -> str | None:
    if not isinstance(st, dict):
        return None
    cmd = st.get("command")
    raw = ""
    if isinstance(cmd, list) and cmd:
        raw = str(cmd[0] or "").strip()
    elif isinstance(cmd, str) and cmd.strip():
        toks = _split_cmdline_tokens(cmd)
        if toks:
            raw = str(toks[0] or "").strip()
    if not raw:
        return None
    return Path(raw).name.strip().lower() or None


def _has_server_identity_token(cmdline: str, expected_exec_token: str | None) -> bool:
    tokens = _split_cmdline_tokens(cmdline)
    expected = str(expected_exec_token or "").strip().lower()
    generic_exec = {
        "python",
        "python.exe",
        "python3",
        "python3.exe",
        "py",
        "py.exe",
        "node",
        "node.exe",
        "bash",
        "sh",
        "zsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }
    expected_matched = False
    opencode_matched = False
    for tok in tokens:
        t = _normalize_cmd_token(tok)
        if not t:
            continue
        t_name = Path(t).name.lower()
        if expected and (t == expected or t_name == expected):
            expected_matched = True
        if _looks_like_opencode_identity(t) or _looks_like_opencode_identity(t_name):
            opencode_matched = True
    if opencode_matched:
        return True
    if expected and expected_matched and (expected not in generic_exec):
        return True
    return False


def _pid_matches_server_url(
    pid: int, url: str, expected_exec_token: str | None = None
) -> bool:
    if pid <= 0:
        return False
    expected_port = _extract_server_port_from_url(url)
    if expected_port is None:
        return False
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return False
    cmdline_lc = cmdline.lower()
    if not _has_serve_token(cmdline_lc):
        return False
    if not _has_server_identity_token(cmdline_lc, expected_exec_token):
        return False
    port_txt = str(expected_port)
    return bool(
        re.search(rf"--port\s*=\s*{re.escape(port_txt)}\b", cmdline_lc)
        or re.search(rf"--port\s+{re.escape(port_txt)}\b", cmdline_lc)
    )


def _server_pid_ownership_status(
    pid: int, url: str, expected_exec_token: str | None = None
) -> str:
    """
    Classify ownership check result for a tracked server PID.
    Returns: "verified" | "mismatch" | "unknown"
    - unknown: cannot read cmdline or cannot infer expected port safely
    """
    if pid <= 0:
        return "mismatch"
    expected_port = _extract_server_port_from_url(url)
    if expected_port is None:
        return "unknown"
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return "unknown"
    cmdline_lc = cmdline.lower()
    if not _has_serve_token(cmdline_lc):
        return "mismatch"
    if not _has_server_identity_token(cmdline_lc, expected_exec_token):
        return "mismatch"
    port_txt = str(expected_port)
    if re.search(rf"--port\s*=\s*{re.escape(port_txt)}\b", cmdline_lc) or re.search(
        rf"--port\s+{re.escape(port_txt)}\b", cmdline_lc
    ):
        return "verified"
    return "mismatch"


def _cmdline_matches_subtask_worker(
    cmdline: str, run_id: str | None = None, *, require_run_id: bool = False
) -> bool:
    cmdline_lc = cmdline.lower()
    if "opencode_subtask.py" not in cmdline_lc:
        return False
    if not re.search(r"(^|\s)run(\s|$)", cmdline_lc):
        return False
    rid = str(run_id or "").strip().lower()
    if rid:
        rid_arg = _extract_flag_value(cmdline_lc, "--run-id")
        if require_run_id:
            if rid_arg != rid:
                return False
        else:
            # Foreground `run` may auto-generate runId internally and argv may not
            # contain it; only enforce runId when argv explicitly carries --run-id.
            if (rid_arg is not None) and (rid_arg != rid):
                return False
    return True


def _pid_matches_subtask_worker(
    pid: int, run_id: str | None = None, *, require_run_id: bool = False
) -> bool:
    return (
        _pid_subtask_worker_ownership_status(pid, run_id, require_run_id=require_run_id)
        == "verified"
    )


def _pid_subtask_worker_ownership_status(
    pid: int, run_id: str | None = None, *, require_run_id: bool = False
) -> str:
    """
    Classify ownership for a tracked subtask worker PID.
    Returns: "verified" | "mismatch" | "unknown"
    - unknown: command line cannot be read
    """
    if pid <= 0:
        return "mismatch"
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return "unknown"
    if _cmdline_matches_subtask_worker(cmdline, run_id, require_run_id=require_run_id):
        return "verified"
    return "mismatch"


def _should_count_job_pid_as_active_worker(pid: int, run_id: str | None) -> bool:
    """
    Determine whether a running PID should be treated as an active opencode-subtask worker.
    Conservative behavior: if command line cannot be read, treat as active to avoid unsafe reaping.
    """
    if pid <= 0:
        return False
    if not _pid_running(pid):
        return False
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return True
    return _cmdline_matches_subtask_worker(cmdline, run_id)


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


# ============================
# Executable resolution (Windows-safe)
# ============================


def _resolve_executable(cmd: str) -> str | None:
    """
    Resolve a runnable executable path.
    On Windows, `opencode` is often a .cmd shim; passing "opencode" to CreateProcess may fail.
    """
    # If it's a path already.
    p = Path(cmd)
    if p.is_file():
        resolved = p.resolve()
        if os.name == "nt" and resolved.suffix.lower() in (".cmd", ".bat"):
            # If a sibling .exe exists, prefer it (avoids cmd.exe windows/popups).
            sibling_exe = resolved.with_suffix(".exe")
            if sibling_exe.is_file():
                return str(sibling_exe.resolve())
        return str(resolved)

    # Try direct which lookup.
    found = which(cmd)
    if found:
        resolved = Path(found).resolve()
        if os.name == "nt" and resolved.suffix.lower() in (".cmd", ".bat"):
            sibling_exe = resolved.with_suffix(".exe")
            if sibling_exe.is_file():
                return str(sibling_exe.resolve())
        return str(resolved)

    # Windows: try common suffixes explicitly.
    if os.name == "nt":
        # Prefer .exe first to avoid cmd.exe spawning and extra console noise.
        for candidate in [cmd + ".exe", cmd + ".cmd", cmd + ".bat"]:
            found2 = which(candidate)
            if found2:
                return str(Path(found2).resolve())
    return None


def _resolve_executable_for_workdir(cmd: str, workdir: Path) -> str | None:
    resolved = _resolve_executable(cmd)
    if resolved:
        return resolved
    p = Path(cmd)
    if not p.is_absolute():
        candidate = (workdir / p).resolve()
        if candidate.is_file():
            return str(candidate)
    return None


# ============================
# HTTP client (stdlib)
# ============================


@dataclass(frozen=True)
class HttpAuth:
    username: str
    password: str


class OpencodeHttpClient:
    def __init__(
        self, base_url: str, auth: HttpAuth | None = None, timeout_s: float = 10.0
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth:
            token = f"{self.auth.username}:{self.auth.password}".encode("utf-8")
            h["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
        if extra:
            h.update(extra)
        return h

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        url = self.base_url + path
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(
                req, timeout=float(timeout_s or self.timeout_s)
            ) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, None
                return resp.status, json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            # Try to capture server error body.
            try:
                raw = e.read()
                msg = raw.decode("utf-8", errors="replace")
            except Exception:
                msg = ""
            raise RuntimeError(
                f"HTTP {e.code} {e.reason} for {path}: {msg[:500]}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"HTTP request failed for {path}: {e}") from e

    def health(self) -> dict[str, Any] | None:
        try:
            _, js = self._request_json("GET", "/global/health", None, timeout_s=2.0)
            return js
        except Exception:
            return None

    def create_session(
        self,
        *,
        title: str | None = None,
        parent_id: str | None = None,
        agent: str | None = None,
        model: dict[str, Any] | None = None,
        permission: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # Latest OpenCode clients create sessions with title plus optional
        # execution defaults such as agent/model/permission rules.  Unknown
        # omitted fields stay absent; no adapter-owned model-output protocol is
        # involved.
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if parent_id:
            body["parentID"] = parent_id
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        if permission:
            body["permission"] = permission
        _, js = self._request_json("POST", "/session", body, timeout_s=self.timeout_s)
        if not isinstance(js, dict):
            raise RuntimeError("Invalid /session response (expected JSON object)")
        return js

    def send_message_sync(
        self,
        session_id: str,
        *,
        prompt: str,
        model: str | None,
        variant: str | None,
        agent: str | None,
        timeout_s: float,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        msg_model = _model_arg_to_http_message_model(model)
        if msg_model:
            body["model"] = msg_model
        if variant:
            body["variant"] = variant
        if agent:
            body["agent"] = agent

        # Server docs: POST /session/:id/message -> { info, parts }
        _, js = self._request_json(
            "POST",
            f"/session/{urllib.parse.quote(session_id, safe='')}/message",
            body,
            timeout_s=timeout_s,
        )
        if not isinstance(js, dict):
            raise RuntimeError("Invalid /message response (expected JSON object)")
        return js

    def abort(self, session_id: str) -> None:
        # Server docs: POST /session/:id/abort
        try:
            self._request_json(
                "POST",
                f"/session/{urllib.parse.quote(session_id, safe='')}/abort",
                {},
                timeout_s=5.0,
            )
        except Exception:
            pass

    def abort_checked(self, session_id: str, *, timeout_s: float = 5.0) -> None:
        # Same endpoint as abort(), but lets exceptions propagate for callers
        # that need a reliable success/failure signal.
        self._request_json(
            "POST",
            f"/session/{urllib.parse.quote(session_id, safe='')}/abort",
            {},
            timeout_s=timeout_s,
        )

    def reply_permission(self, request_id: str, *, reply: str) -> None:
        # Latest server permission API: POST /permission/:requestID/reply
        # with reply in {"once", "always", "reject"}.  This is deliberately
        # not the session-scoped permissions endpoint.
        if reply not in {"once", "always", "reject"}:
            raise ValueError(f"invalid permission reply: {reply!r}")
        request_id_q = urllib.parse.quote(request_id, safe="")
        body = {"reply": reply}
        try:
            self._request_json(
                "POST",
                f"/permission/{request_id_q}/reply",
                body,
                timeout_s=5.0,
            )
        except Exception:
            pass

    def open_sse(
        self, path: str, *, timeout_s: float = 30.0
    ) -> urllib.request.addinfourl:
        """
        Open an SSE stream endpoint (returns a file-like HTTP response).
        Note: stdlib doesn't have native SSE parsing; we do manual line parsing.
        """
        url = self.base_url + path
        req = urllib.request.Request(
            url, method="GET", headers=self._headers({"Accept": "text/event-stream"})
        )
        return urllib.request.urlopen(req, timeout=timeout_s)  # type: ignore[return-value]


# ============================
# Server lifecycle
# ============================


def _server_health_probe(url_base: str, auth: HttpAuth | None) -> dict[str, Any]:
    """
    Probe /global/health with coarse failure classification.

    status:
    - healthy: endpoint reachable and reports healthy=true
    - unhealthy: endpoint reachable but healthy!=true
    - auth-error: HTTP 401/403 (credentials mismatch / missing)
    - unknown: transport/protocol failures (timeout, refused, parse, etc.)
    """
    c = OpencodeHttpClient(url_base, auth=auth, timeout_s=2.0)
    try:
        _, js = c._request_json("GET", "/global/health", None, timeout_s=2.0)
    except RuntimeError as e:
        msg = str(e)
        if ("HTTP 401" in msg) or ("HTTP 403" in msg):
            return {"status": "auth-error", "error": msg}
        return {"status": "unknown", "error": msg}
    except Exception as e:
        return {"status": "unknown", "error": f"{type(e).__name__}: {e}"}

    if isinstance(js, dict) and js.get("healthy") is True:
        return {"status": "healthy", "payload": js}
    if isinstance(js, dict):
        return {"status": "unhealthy", "payload": js}
    return {"status": "unknown", "error": "invalid health payload"}


def _server_health(url_base: str, auth: HttpAuth | None) -> dict[str, Any] | None:
    probe = _server_health_probe(url_base, auth)
    payload = probe.get("payload")
    if str(probe.get("status")) == "healthy" and isinstance(payload, dict):
        return payload
    return None


def _scan_running_job_server_urls(
    *,
    current_project_key: str | None = None,
    limit: int = ORPHAN_REAPER_SCAN_LIMIT,
) -> tuple[set[str], set[str]]:
    """
    Scan recent run job.json files and return:
    - active_urls: server URLs currently referenced by live running workers
    - crashed_owner_urls: server URLs referenced by running jobs whose worker PID is dead,
      serverStartedNew=true, and (when current_project_key is set) belong to the same project.
    """
    active_urls: set[str] = set()
    crashed_owner_urls: set[str] = set()
    runs_dir = _runs_dir()
    if not runs_dir.exists():
        return active_urls, crashed_owner_urls

    entries: list[tuple[float, Path]] = []
    try:
        for p in runs_dir.iterdir():
            if not p.is_dir():
                continue
            try:
                mtime = float(p.stat().st_mtime)
            except Exception:
                continue
            entries.append((mtime, p))
    except Exception:
        return active_urls, crashed_owner_urls

    entries.sort(key=lambda t: t[0], reverse=True)
    if limit > 0:
        entries = entries[:limit]

    for _, run_dir in entries:
        job = _load_json(run_dir / "job.json")
        if not isinstance(job, dict):
            continue
        url = job.get("serverUrl")
        if not isinstance(url, str) or not url.strip():
            continue
        finish = _load_json(run_dir / "finish.json")
        if isinstance(finish, dict):
            # Any readable finish.json means the job has reached a terminal
            # state — it should not contribute crashed-owner evidence.
            # Previously only canceled finishes were skipped; a successful or
            # failed finish is equally terminal.
            continue
        state = str(job.get("state") or "").strip().lower()
        if state != "running":
            continue

        job_project_key: str | None = None
        if current_project_key is not None:
            try:
                workdir_val = job.get("workdir")
                if isinstance(workdir_val, str) and workdir_val.strip():
                    job_project_key = _project_key(Path(workdir_val))
            except Exception:
                job_project_key = None

        pid = 0
        try:
            raw = job.get("pid")
            if isinstance(raw, (int, str)):
                pid = int(raw or 0)
        except Exception:
            pid = 0

        job_run_id: str | None = None
        raw_run_id = job.get("runId")
        if isinstance(raw_run_id, (str, int)):
            txt_run_id = str(raw_run_id).strip()
            if txt_run_id:
                job_run_id = txt_run_id

        if _should_count_job_pid_as_active_worker(pid, job_run_id):
            active_urls.add(url)
        elif bool(job.get("serverStartedNew")):
            if (current_project_key is None) or (
                job_project_key == current_project_key
            ):
                crashed_owner_urls.add(url)

    return active_urls, crashed_owner_urls


def reap_orphan_server_for_project(
    *,
    workdir: Path,
    auth: HttpAuth | None,
    idle_s: float,
) -> dict[str, Any]:
    """
    Startup reaper for per-project server state.

    Rules:
    - Reap immediately when state PID is dead or server is unhealthy.
    - Reap immediately when we find a crashed owner job (running state + dead PID + serverStartedNew=true).
    - Optional fallback: reap healthy idle server when no live worker references it and age >= idle_s.
    """
    st_path = _server_state_path(workdir)
    lock_path = _server_lock_path(workdir)

    with _FileLock(lock_path):
        st = _load_json(st_path) or {}
        if not (isinstance(st, dict) and isinstance(st.get("url"), str)):
            return {
                "checked": False,
                "reaped": False,
                "reason": "no-state",
                "statePath": str(st_path),
            }

        url = str(st["url"])
        expected_exec_token = _expected_server_exec_token(st)
        pid = 0
        try:
            raw_pid = st.get("pid")
            if isinstance(raw_pid, (int, str)):
                pid = int(raw_pid or 0)
        except Exception:
            pid = 0

        def _clear_state() -> None:
            try:
                st_path.unlink(missing_ok=True)  # type: ignore[attr-defined]
            except Exception:
                pass

        if pid <= 0:
            _clear_state()
            return {
                "checked": True,
                "reaped": True,
                "reason": "invalid-pid",
                "url": url,
                "pid": pid,
                "statePath": str(st_path),
            }

        if not _pid_running(pid):
            _clear_state()
            return {
                "checked": True,
                "reaped": True,
                "reason": "dead-pid",
                "url": url,
                "pid": pid,
                "statePath": str(st_path),
            }

        health_probe = _server_health_probe(url, auth)
        health_status = str(health_probe.get("status") or "").strip().lower()
        if health_status == "unhealthy":
            if not _pid_matches_server_url(pid, url, expected_exec_token):
                return {
                    "checked": True,
                    "reaped": False,
                    "reason": "unhealthy-owner-unverified",
                    "url": url,
                    "pid": pid,
                    "healthStatus": health_status,
                    "statePath": str(st_path),
                }
            if _kill_tree(pid):
                _clear_state()
                return {
                    "checked": True,
                    "reaped": True,
                    "reason": "unhealthy-server",
                    "url": url,
                    "pid": pid,
                    "healthStatus": health_status,
                    "statePath": str(st_path),
                }
            return {
                "checked": True,
                "reaped": False,
                "reason": "unhealthy-kill-failed",
                "url": url,
                "pid": pid,
                "healthStatus": health_status,
                "statePath": str(st_path),
            }
        if health_status != "healthy":
            # Unknown probe result (including auth mismatch) is not sufficient
            # evidence to kill a potentially healthy server process.
            return {
                "checked": True,
                "reaped": False,
                "reason": "health-unknown",
                "url": url,
                "pid": pid,
                "healthStatus": health_status,
                "healthError": health_probe.get("error"),
                "statePath": str(st_path),
            }

        active_urls, crashed_owner_urls = _scan_running_job_server_urls(
            current_project_key=_project_key(workdir)
        )
        if url in active_urls:
            return {
                "checked": True,
                "reaped": False,
                "reason": "active-worker",
                "url": url,
                "pid": pid,
                "statePath": str(st_path),
            }

        started_at_ms = 0
        try:
            raw_started = st.get("startedAt")
            if isinstance(raw_started, (int, str)):
                started_at_ms = int(raw_started or 0)
        except Exception:
            started_at_ms = 0
        age_s = (
            max(0.0, (float(_now_ms()) - float(started_at_ms)) / 1000.0)
            if started_at_ms > 0
            else None
        )

        if url in crashed_owner_urls:
            if not _pid_matches_server_url(pid, url, expected_exec_token):
                return {
                    "checked": True,
                    "reaped": False,
                    "reason": "crashed-owner-unverified",
                    "url": url,
                    "pid": pid,
                    "ageS": age_s,
                    "statePath": str(st_path),
                }
            if _kill_tree(pid):
                _clear_state()
                return {
                    "checked": True,
                    "reaped": True,
                    "reason": "crashed-owner",
                    "url": url,
                    "pid": pid,
                    "ageS": age_s,
                    "statePath": str(st_path),
                }
            return {
                "checked": True,
                "reaped": False,
                "reason": "crashed-owner-kill-failed",
                "url": url,
                "pid": pid,
                "ageS": age_s,
                "statePath": str(st_path),
            }

        if idle_s > 0 and age_s is not None and age_s >= idle_s:
            if not _pid_matches_server_url(pid, url, expected_exec_token):
                return {
                    "checked": True,
                    "reaped": False,
                    "reason": "idle-timeout-unverified",
                    "url": url,
                    "pid": pid,
                    "ageS": age_s,
                    "statePath": str(st_path),
                }
            if _kill_tree(pid):
                _clear_state()
                return {
                    "checked": True,
                    "reaped": True,
                    "reason": "idle-timeout",
                    "url": url,
                    "pid": pid,
                    "ageS": age_s,
                    "statePath": str(st_path),
                }
            return {
                "checked": True,
                "reaped": False,
                "reason": "idle-timeout-kill-failed",
                "url": url,
                "pid": pid,
                "ageS": age_s,
                "statePath": str(st_path),
            }

        return {
            "checked": True,
            "reaped": False,
            "reason": "healthy-kept",
            "url": url,
            "pid": pid,
            "ageS": age_s,
            "statePath": str(st_path),
        }


class _FileLock:
    """
    Minimal cross-platform advisory lock on a file.
    - Unix: fcntl.flock (non-blocking + bounded retry)
    - Windows: msvcrt.locking (non-blocking + bounded retry)
    """

    def __init__(
        self,
        path: Path,
        timeout_s: float = DEFAULT_FILE_LOCK_TIMEOUT_S,
        poll_s: float = DEFAULT_FILE_LOCK_POLL_S,
    ):
        self.path = path
        self.fp = None
        self.timeout_s = max(0.0, float(timeout_s))
        self.poll_s = max(0.01, float(poll_s))

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout_s
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore

                while True:
                    self.fp.seek(0)
                    try:
                        msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"lock timeout: {self.path}")
                        time.sleep(
                            min(
                                self.poll_s,
                                max(0.01, deadline - time.monotonic()),
                            )
                        )
            else:
                import fcntl  # type: ignore

                while True:
                    try:
                        fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"lock timeout: {self.path}")
                        time.sleep(
                            min(
                                self.poll_s,
                                max(0.01, deadline - time.monotonic()),
                            )
                        )
        except Exception:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.fp:
            return
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore

                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # type: ignore

                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None


def _write_job_locked(job_path: Path, artifacts_dir: Path, obj: dict[str, Any]) -> None:
    with _FileLock(_state_lock_path(artifacts_dir)):
        _write_json(job_path, obj)


def _update_job_fields_locked(
    job_path: Path, artifacts_dir: Path, fields: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        with _FileLock(_state_lock_path(artifacts_dir)):
            job = _load_json(job_path) or {}
            if not isinstance(job, dict):
                job = {}
            job.update(fields)
            job["updatedAt"] = _now_ms()
            _write_json(job_path, job)
            return job
    except Exception:
        return None


def _write_finish_once(
    *,
    artifacts_dir: Path,
    finish_path: Path,
    finish_obj: dict[str, Any],
) -> tuple[bool, str, dict[str, Any] | None]:
    with _FileLock(_finish_lock_path(artifacts_dir)):
        if finish_path.exists():
            existing, existing_reason = _read_finish_envelope(finish_path)
            if existing_reason is None and isinstance(existing, dict):
                return False, "exists", existing
            quarantined_path = _quarantine_invalid_finish(finish_path)
            if quarantined_path is None:
                try:
                    finish_path.unlink(missing_ok=True)  # type: ignore[call-arg]
                except Exception:
                    return False, str(existing_reason or "unreadable").lower(), None
            try:
                _write_json(finish_path, finish_obj)
            except Exception:
                return False, "write_failed", None
            return True, "recovered", None
        try:
            _write_json(finish_path, finish_obj)
        except Exception:
            return False, "write_failed", None
        return True, "written", None


def _wait_for_pid_dead(pid: int, timeout_s: float, poll_s: float = 0.1) -> bool:
    if pid <= 0:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        alive, known = _pid_running_state(pid)
        if known and not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(max(poll_s, 0.01), max(0.01, deadline - time.monotonic())))


def _validate_finish_envelope(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return "root must be an object"
    allowed_top = {
        "type",
        "schemaVersion",
        "adapterVersion",
        "timestamp",
        "runId",
        "taskId",
        "workdir",
        "outcome",
        "execution",
        "workspace",
        "artifacts",
        "subtask",
    }
    extra_top = sorted(set(obj) - allowed_top)
    if extra_top:
        return "unexpected top-level field: " + extra_top[0]
    if obj.get("type") != "opencode-subtask-finish":
        return "type must be opencode-subtask-finish"
    # V3 is a deliberate schema break. Lifecycle commands only trust strict V3
    # finish envelopes and quarantine anything older or malformed.
    if obj.get("schemaVersion") != ADAPTER_SCHEMA_VERSION:
        return f"schemaVersion must be {ADAPTER_SCHEMA_VERSION}"

    outcome = obj.get("outcome")
    if outcome not in FINISH_OUTCOMES:
        return "outcome is invalid"

    execution = obj.get("execution")
    workspace = obj.get("workspace")
    artifacts = obj.get("artifacts")
    if not isinstance(execution, dict):
        return "execution must be an object"
    if not isinstance(workspace, dict):
        return "workspace must be an object"
    if not isinstance(artifacts, dict):
        return "artifacts must be an object"
    allowed_artifacts = {
        "dir",
        "jobPath",
        "finishPath",
        "promptPath",
        "eventsPath",
        "stderrPath",
        "assistantPath",
        "wrapperLogPath",
        "progressPath",
        "notificationPath",
    }
    extra_artifacts = sorted(set(artifacts) - allowed_artifacts)
    if extra_artifacts:
        return "unexpected artifact field: " + extra_artifacts[0]

    execution_engine = execution.get("engine")
    if not isinstance(execution_engine, dict):
        return "execution.engine must be an object"
    if execution.get("error") is not None and not isinstance(execution.get("error"), dict):
        return "execution.error must be an object or null"

    engine_selected = execution_engine.get("selected")
    if engine_selected not in EXECUTION_ENGINE_SELECTED_VALUES:
        return "execution.engine.selected is invalid"
    engine_fallback = execution_engine.get("fallbackFrom")
    if engine_fallback is not None and engine_fallback not in EXECUTION_ENGINE_FALLBACK_VALUES:
        return "execution.engine.fallbackFrom is invalid"
    if engine_fallback is not None and engine_selected != "cli":
        return "execution.engine.fallbackFrom requires execution.engine.selected=cli"

    if outcome == "completed" and execution.get("error") is not None:
        return "completed outcome requires execution.error=null"

    return None


def _read_finish_envelope(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = json.loads(_read_text(path))
    except Exception:
        return None, "FINISH_UNREADABLE"
    if not isinstance(raw, dict):
        return None, "FINISH_INVALID"
    validation_error = _validate_finish_envelope(raw)
    if validation_error is not None:
        return None, "FINISH_INVALID"
    return raw, None


def _quarantine_invalid_finish(path: Path) -> Path | None:
    for attempt in range(5):
        suffix = "" if attempt == 0 else f".{attempt}"
        target = path.with_name(f"finish.invalid.{_now_ms()}{suffix}.json")
        try:
            os.replace(path, target)
            return target
        except FileNotFoundError:
            return None
        except Exception:
            time.sleep(0.01)
    return None


def _load_runtime_finish_envelope(
    finish_path: Path,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    if not finish_path.exists():
        return None, []

    finish, reason_code = _read_finish_envelope(finish_path)
    if finish is not None:
        return finish, []

    quarantined_path = _quarantine_invalid_finish(finish_path)
    reason_text = (
        "finish.json is not readable JSON"
        if reason_code == "FINISH_UNREADABLE"
        else "finish.json failed strict V3 validation"
    )
    if quarantined_path is not None:
        reason_text += f"; moved to {quarantined_path.name}"
    else:
        removed = False
        try:
            finish_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            removed = True
        except Exception:
            removed = False
        if removed:
            reason_text += "; quarantine rename failed, finish.json removed"
        else:
            reason_text += "; quarantine rename failed"
    return None, [_warning("FinishInvalidQuarantined", reason_text)]


def _with_execution_warnings(
    finish: dict[str, Any], warnings: list[dict[str, str]]
) -> dict[str, Any]:
    if not warnings:
        return finish
    out = json.loads(json.dumps(finish))
    execution = out.get("execution")
    if not isinstance(execution, dict):
        return out
    existing = execution.get("warnings")
    warning_list = list(existing) if isinstance(existing, list) else []
    warning_list.extend(warnings)
    execution["warnings"] = warning_list
    return out


def _server_lock_timeout_s(wait_s: float | None = None) -> float:
    base = float(DEFAULT_FILE_LOCK_TIMEOUT_S)
    if wait_s is None:
        return base
    try:
        w = float(wait_s)
    except Exception:
        return base
    if w < 0:
        w = 0.0
    # Keep lock timeout aligned with startup health wait to avoid false timeout
    # failures during legitimate concurrent ensure/attach calls.
    return max(base, w + 5.0)


def ensure_server(
    *,
    opencode_bin: str,
    workdir: Path,
    hostname: str,
    port: int,
    wait_s: float,
    env: dict[str, str],
    auth: HttpAuth | None,
    cors_origins: list[str] | None = None,
) -> dict[str, Any]:
    st_path = _server_state_path(workdir)
    log_path = _server_log_path(workdir)
    lock_path = _server_lock_path(workdir)

    with _FileLock(lock_path, timeout_s=_server_lock_timeout_s(wait_s)):
        st = _load_json(st_path) or {}
        if isinstance(st, dict) and isinstance(st.get("url"), str):
            url = str(st["url"])
            health = _server_health(url, auth)
            if health:
                st["version"] = health.get("version")
                _write_json(st_path, st)
                out = dict(st)
                out["startedNew"] = False
                return out

            # If we have a recorded PID and it is still alive, do not spawn another server.
            # Starting multiple servers for the same project is noisy on Windows and can confuse callers.
            pid = (
                int(st.get("pid") or 0) if isinstance(st.get("pid"), (int, str)) else 0
            )
            if pid and _pid_running(pid):
                raise RuntimeError(
                    f"opencode serve appears to be running but unhealthy: pid={pid} url={url}. "
                    "Use `stop-server` (or fix server auth/config) before retrying."
                )

        # Do NOT auto-kill a stale PID here.
        # OpenCode may be running other work; killing aggressively can disrupt unrelated tasks.

        if port == 0:
            port = _pick_free_port(hostname)

        cmd = [opencode_bin, "serve", "--hostname", hostname, "--port", str(port)]
        for origin in cors_origins or []:
            if origin:
                cmd.extend(["--cors", origin])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_path, "ab", buffering=0)

        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs.update(_win_hide_popen_kwargs(detached=True))
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=log_fp,
                cwd=str(workdir),
                env=env,
                **popen_kwargs,
            )
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

        url = f"http://{hostname}:{port}"
        deadline = time.monotonic() + max(wait_s, 0.1)
        health: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            health = _server_health(url, auth)
            if health:
                break
            time.sleep(0.25)

        if not health:
            try:
                _kill_tree(proc.pid)
            except Exception:
                pass
            raise RuntimeError(f"opencode serve failed health check: {url}")

        state = {
            "pid": proc.pid,
            "hostname": hostname,
            "port": port,
            "url": url,
            "startedAt": _now_ms(),
            "version": health.get("version"),
            "projectRoot": str(_find_git_root(workdir)),
            "logPath": str(log_path),
            "command": cmd,
            "corsOrigins": list(cors_origins or []),
            "managedBy": "opencode-subtask",
        }
        _write_json(st_path, state)
        out = dict(state)
        out["startedNew"] = True
        return out


def attach_existing_server(
    *,
    workdir: Path,
    auth: HttpAuth | None,
    wait_s: float | None = None,
) -> dict[str, Any] | None:
    """
    Attach to an already-running per-project server if we have state and it is healthy.
    Never starts or kills processes.
    """
    st_path = _server_state_path(workdir)
    lock_path = _server_lock_path(workdir)
    with _FileLock(lock_path, timeout_s=_server_lock_timeout_s(wait_s)):
        st = _load_json(st_path) or {}
        if not (isinstance(st, dict) and isinstance(st.get("url"), str)):
            return None
        url = str(st["url"])
        health = _server_health(url, auth)
        if not health:
            return None
        st["version"] = health.get("version")
        _write_json(st_path, st)
        return st


def stop_server(workdir: Path) -> dict[str, Any]:
    st_path = _server_state_path(workdir)
    lock_path = _server_lock_path(workdir)
    with _FileLock(lock_path):
        st = _load_json(st_path) or {}
        url = str(st.get("url") or "") if isinstance(st, dict) else ""
        expected_exec_token = _expected_server_exec_token(
            st if isinstance(st, dict) else None
        )
        pid = int(st.get("pid") or 0) if isinstance(st, dict) else 0
        ok = False
        kept_state = False
        reason = "no-pid"
        if pid and _pid_running(pid):
            own = _server_pid_ownership_status(pid, url, expected_exec_token)
            if own == "verified":
                ok = _kill_tree(pid)
                if ok:
                    reason = "killed"
                else:
                    kept_state = True
                    reason = "kill-failed-state-kept"
            elif own == "unknown":
                reason = "owner-unverified-state-cleared"
            else:
                reason = "owner-mismatch-state-cleared"
        elif pid:
            reason = "pid-not-running"
        if not kept_state:
            try:
                st_path.unlink(missing_ok=True)  # type: ignore[attr-defined]
            except Exception:
                pass
        out = {
            "ok": ok,
            "pid": pid,
            "statePath": str(st_path),
            "keptState": kept_state,
            "reason": reason,
        }
        if not ok:
            out["error"] = {
                "name": "StopServerFailed",
                "message": f"stop-server did not terminate server (reason={reason})",
            }
        return out


# ============================
# Event aggregation (CLI NDJSON)
# ============================


class _TailText:
    def __init__(self, max_chars: int = 200_000):
        self._max = max_chars
        self._buf = ""
        self._lock = threading.Lock()

    def append(self, t: str) -> None:
        if not t:
            return
        with self._lock:
            self._buf += t
            if len(self._buf) > self._max:
                self._buf = self._buf[-self._max :]

    def get(self) -> str:
        with self._lock:
            return self._buf


def _event_payload(evt: dict[str, Any]) -> dict[str, Any]:
    payload = evt.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload
    return evt


def _event_properties(evt: dict[str, Any]) -> dict[str, Any]:
    props = evt.get("properties")
    return props if isinstance(props, dict) else {}


def _nested_event_dicts(evt: dict[str, Any]) -> list[dict[str, Any]]:
    """Return likely event sub-objects that may carry ids/roles.

    OpenCode CLI JSON and server SSE can arrive either as the raw bus event or
    as a GlobalEvent wrapper: { directory, payload: Event }.  Current message
    events put useful fields under properties.info / properties.part, while
    some older/raw event emitters use top-level fields.  This helper is a
    shallow scanner, not a schema contract.
    """
    evt = _event_payload(evt)
    props = _event_properties(evt)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(obj: Any) -> None:
        if isinstance(obj, dict) and id(obj) not in seen:
            seen.add(id(obj))
            out.append(obj)

    add(evt)
    add(props)
    for base in list(out):
        for key in (
            "info",
            "part",
            "message",
            "permission",
            "request",
            "data",
            "session",
            "properties",
        ):
            add(base.get(key))
    return out


def _event_session_id(evt: dict[str, Any]) -> str | None:
    for src in _nested_event_dicts(evt):
        sid = src.get("sessionID") or src.get("sessionId") or src.get("session")
        if isinstance(sid, str) and sid:
            return sid
    return None


def _event_message_id(evt: dict[str, Any]) -> str | None:
    for src in _nested_event_dicts(evt):
        mid = src.get("messageID") or src.get("messageId") or src.get("message")
        if isinstance(mid, str) and mid:
            return mid
        mid = src.get("id")
        if isinstance(mid, str) and mid and str(src.get("role") or "") in (
            "assistant",
            "user",
        ):
            return mid
    return None


def _event_role(evt: dict[str, Any]) -> str | None:
    for src in _nested_event_dicts(evt):
        role = src.get("role")
        if isinstance(role, str) and role:
            return role.lower()
    return None


def _first_nested_dict(evt: dict[str, Any], key: str) -> dict[str, Any] | None:
    evt = _event_payload(evt)
    props = _event_properties(evt)
    for src in (props, evt):
        v = src.get(key)
        if isinstance(v, dict):
            return v
    for src in _nested_event_dicts(evt):
        v = src.get(key)
        if isinstance(v, dict):
            return v
    return None


def _first_nested_str(evt: dict[str, Any], keys: Iterable[str]) -> str | None:
    for src in _nested_event_dicts(evt):
        for key in keys:
            v = src.get(key)
            if isinstance(v, str) and v:
                return v
    return None


class _AssistantTextAccumulator:
    """Accumulate assistant text from OpenCode events without custom JSON.

    The important invariant is: assistant.txt should contain the model's final
    natural-language output, not echoed user prompts, reasoning/tool metadata,
    or duplicate cumulative part snapshots.
    """

    _TEXT_TYPES: Final[set[str]] = {"text", "assistant_text", "output_text"}
    _NON_TEXT_TYPES: Final[set[str]] = {
        "reasoning",
        "tool",
        "tool_call",
        "tool_result",
        "file",
        "patch",
        "step",
        "snapshot",
    }

    def __init__(self) -> None:
        self._assistant_message_ids: set[str] = set()
        self._user_message_ids: set[str] = set()
        self._part_full_text: dict[str, str] = {}

    def _observe_message_info(self, evt: dict[str, Any]) -> None:
        info = _first_nested_dict(evt, "info") or _first_nested_dict(evt, "message")
        if not isinstance(info, dict):
            return
        role = info.get("role")
        mid = info.get("id") or info.get("messageID") or info.get("messageId")
        if isinstance(mid, str) and mid:
            if role == "assistant":
                self._assistant_message_ids.add(mid)
            elif role == "user":
                self._user_message_ids.add(mid)

    def _part_id(self, part: dict[str, Any]) -> str:
        for key in ("id", "partID", "partId"):
            v = part.get(key)
            if isinstance(v, str) and v:
                return v
        mid = part.get("messageID") or part.get("messageId")
        if isinstance(mid, str) and mid:
            return mid + ":text"
        return "__single_text_part__"

    def _is_assistant_part(self, part: dict[str, Any], evt: dict[str, Any]) -> bool:
        mid = part.get("messageID") or part.get("messageId")
        if isinstance(mid, str) and mid in self._user_message_ids:
            return False
        if isinstance(mid, str) and mid and self._assistant_message_ids:
            return mid in self._assistant_message_ids
        role = _event_role(evt)
        if role and role != "assistant":
            return False
        # When OpenCode emits only a part event before the message info event,
        # there is no role yet.  Accept text-only parts; reject known non-text
        # types below.
        return True

    def _delta_from_full_text(self, part: dict[str, Any], text: str) -> str | None:
        part_id = self._part_id(part)
        prev = self._part_full_text.get(part_id, "")
        self._part_full_text[part_id] = text
        if not prev:
            return text or None
        if text == prev:
            return None
        if text.startswith(prev):
            return text[len(prev) :] or None
        # Snapshot reset or server-side compaction.  Prefer returning the new
        # value over losing output; this rare path may duplicate but stays
        # diagnosable through events.ndjson.
        return text or None

    def _consume_part(self, evt: dict[str, Any]) -> str | None:
        part = _first_nested_dict(evt, "part")
        if not isinstance(part, dict):
            return None
        part_type = str(part.get("type") or "").lower()
        if part_type in self._NON_TEXT_TYPES:
            return None
        if part_type and part_type not in self._TEXT_TYPES:
            return None
        if not self._is_assistant_part(part, evt):
            return None

        props = _event_properties(evt)
        delta = props.get("delta") or evt.get("delta")
        if isinstance(delta, str) and delta:
            part_id = self._part_id(part)
            self._part_full_text[part_id] = self._part_full_text.get(part_id, "") + delta
            return delta

        for key in ("text", "content"):
            v = part.get(key)
            if isinstance(v, str) and v:
                return self._delta_from_full_text(part, v)
        return None

    def _consume_direct_message_text(self, evt: dict[str, Any]) -> str | None:
        evt = _event_payload(evt)
        et = str(evt.get("type") or "").lower()
        role = _event_role(evt)
        if role and role != "assistant":
            return None
        if et and not (
            et in {"message", "assistant", "response", "text", "message.text"}
            or et.startswith("assistant")
        ):
            return None
        for src in (evt, _event_properties(evt)):
            for key in ("text", "delta", "content"):
                v = src.get(key)
                if isinstance(v, str) and v:
                    return v
        msg = _first_nested_dict(evt, "message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                chunks: list[str] = []
                for item in content:
                    if isinstance(item, str):
                        chunks.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("text"), str):
                        chunks.append(str(item["text"]))
                out = "".join(chunks)
                return out or None
        return None

    def consume(self, raw_evt: dict[str, Any]) -> str | None:
        evt = _event_payload(raw_evt)
        if not isinstance(evt, dict):
            return None
        self._observe_message_info(evt)
        text = self._consume_part(evt)
        if text:
            return text
        return self._consume_direct_message_text(evt)


def _extract_text_from_event(evt: dict[str, Any]) -> str | None:
    # Backward-free, best-effort helper for tests/simple emitters.  Streaming
    # paths should reuse a single _AssistantTextAccumulator instance.
    return _AssistantTextAccumulator().consume(evt)


@dataclass
class RunOutcome:
    ok: bool
    exit_code: int
    timed_out: bool
    engine: str
    fallback_from: str | None
    session_id: str | None
    full_text: str
    metrics: dict[str, Any] | None
    error: dict[str, Any] | None


# ============================
# Result validation
# ============================


def _metric_output_tokens(metrics: dict[str, Any] | None) -> int | None:
    if not isinstance(metrics, dict):
        return None
    tokens = metrics.get("tokens")
    if not isinstance(tokens, dict):
        return None
    v = tokens.get("output")
    try:
        return int(v)
    except Exception:
        return None


def _is_empty_model_output(outcome: RunOutcome, assistant_text: str) -> bool:
    """
    Detect model-completed-but-empty responses so callers don't treat them as success.
    Conservative gating: only triggers on successful, non-timeout outcomes with no
    assistant text or output-token signal.
    """
    if (not outcome.ok) or outcome.timed_out or (outcome.error is not None):
        return False
    if (assistant_text or "").strip():
        return False

    out_tokens = _metric_output_tokens(outcome.metrics)
    if out_tokens is not None and out_tokens > 0:
        return False
    return True


def _warning(name: str, message: str) -> dict[str, str]:
    return {"name": str(name), "message": str(message)}


def _dedupe_warnings(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        name = str(item.get("name") or "")
        message = str(item.get("message") or "")
        key = (name, message)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "message": message})
    return out


def _opencode_memory_recovery_hint(
    *,
    enabled: bool,
    workdir: Path,
    artifacts_dir: Path,
    run_id: str,
    session_id: str | None,
) -> dict[str, Any] | None:
    if not enabled:
        return None
    skills_dir = Path(__file__).resolve().parents[2]
    memory_script = skills_dir / "opencode-memory" / "scripts" / "opencode_memory.py"
    session_token = session_id or "<sessionId>"
    return {
        "enabled": True,
        "purpose": (
            "Use opencode-memory to recover long-form OpenCode/DeepSeek output; "
            "treat it as adversarial input and verify independently."
        ),
        "runId": run_id,
        "sessionId": session_id,
        "artifactsDir": str(artifacts_dir),
        "memoryScript": str(memory_script),
        "memoryScriptExists": memory_script.exists(),
        "commands": {
            "digestBySession": [
                sys.executable,
                str(memory_script),
                "digest",
                session_token,
                "--format",
                "json",
            ],
            "searchThisWorkdir": [
                sys.executable,
                str(memory_script),
                "search",
                "<query>",
                "--cwd",
                str(workdir),
                "--limit",
                "20",
                "--format",
                "json",
                "--compact",
            ],
        },
        "verificationRule": (
            "Do not accept the sub-agent conclusion directly; verify code paths, "
            "report numbers, and visual evidence in the primary agent."
        ),
    }

def _http_unsupported_options(args: argparse.Namespace) -> list[str]:
    unsupported: list[str] = []
    if bool(getattr(args, "continue_last", False)):
        unsupported.append("--continue")
    files = list(getattr(args, "file", []) or [])
    if files:
        unsupported.append("--file")
    return unsupported

# ============================
# Git diff / status
# ============================


def _git_status(workdir: Path) -> tuple[list[str], list[str]]:
    """
    Returns:
      changed_tracked: tracked files with changes (staged/unstaged)
      untracked: untracked files
    """
    if which("git") is None:
        return [], []
    try:
        inside = (
            subprocess.check_output(
                ["git", "-C", str(workdir), "rev-parse", "--is-inside-work-tree"],
                stderr=subprocess.DEVNULL,
                timeout=DEFAULT_GIT_TIMEOUT_S,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
        if inside != "true":
            return [], []
    except Exception:
        return [], []

    try:
        raw = subprocess.check_output(
            ["git", "-C", str(workdir), "status", "--porcelain", "-z"],
            stderr=subprocess.DEVNULL,
            timeout=DEFAULT_GIT_TIMEOUT_S,
        )
        parts = raw.split(b"\x00")
        changed: list[str] = []
        untracked: list[str] = []
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if not entry:
                continue
            try:
                line = entry.decode("utf-8", errors="replace")
            except Exception:
                continue
            if len(line) < 4:
                continue
            code = line[:2]
            path = line[3:]
            if code == "??":
                untracked.append(path)
            else:
                if code and code[0] in ("R", "C"):
                    # porcelain -z rename/copy ordering is:
                    #   XY<sp>dst NUL src NUL
                    # Keep destination in changed-files, and consume trailing src token.
                    if path:
                        changed.append(path)
                    if i < len(parts):
                        i += 1
                    continue
                changed.append(path)
        return sorted(set(changed)), sorted(set(untracked))
    except Exception:
        # fallback
        try:
            names = subprocess.check_output(
                ["git", "-C", str(workdir), "diff", "--name-only"],
                stderr=subprocess.DEVNULL,
                timeout=DEFAULT_GIT_TIMEOUT_S,
            ).decode("utf-8", errors="replace")
            changed = [x.strip() for x in names.splitlines() if x.strip()]
            return sorted(set(changed)), []
        except Exception:
            return [], []


def _git_patch(workdir: Path, artifacts_dir: Path) -> str | None:
    if which("git") is None:
        return None
    try:
        diff = subprocess.check_output(
            ["git", "-C", str(workdir), "diff"],
            stderr=subprocess.DEVNULL,
            timeout=DEFAULT_GIT_TIMEOUT_S,
        ).decode("utf-8", errors="replace")
        if not diff.strip():
            return None
        p = artifacts_dir / "changes.patch"
        _write_text(p, diff)
        return p.name
    except Exception:
        return None


# ============================
# Output objects (stable contract)
# ============================


def _minimal_artifacts(
    *,
    dir_path: Path,
    job_path: Path,
    finish_path: Path,
) -> dict[str, Any]:
    return {
        "dir": str(dir_path),
        "jobPath": job_path.name,
        "finishPath": finish_path.name,
        "promptPath": None,
        "eventsPath": None,
        "stderrPath": None,
        "assistantPath": None,
        "wrapperLogPath": None,
    }


def _artifacts_obj(
    *,
    dir_path: Path,
    job_path: Path,
    finish_path: Path,
    prompt_path: Path,
    events_path: Path | None,
    stderr_path: Path | None,
    assistant_path: Path | None,
    wrapper_log_path: Path | None,
    progress_path: Path | None = None,
    notification_path: Path | None = None,
    patch_path: str | None = None,
) -> dict[str, Any]:
    def _name_if_exists(p: Path | None) -> str | None:
        return p.name if p and p.exists() else None

    return {
        "dir": str(dir_path),
        "jobPath": job_path.name,
        "finishPath": finish_path.name,
        "promptPath": prompt_path.name,
        "eventsPath": _name_if_exists(events_path),
        "stderrPath": _name_if_exists(stderr_path),
        "assistantPath": _name_if_exists(assistant_path),
        "wrapperLogPath": _name_if_exists(wrapper_log_path),
        "progressPath": _name_if_exists(progress_path),
        "notificationPath": (notification_path.name if notification_path else None),
    }


def _finish_obj(
    *,
    run_id: str,
    workdir: Path,
    outcome: str,
    exit_code: int,
    duration_ms: int,
    engine_selected: str,
    fallback_from: str | None,
    session_id: str | None,
    execution_error: dict[str, Any] | None,
    execution_warnings: list[dict[str, str]] | None,
    changed_files: list[str],
    untracked_files: list[str],
    patch_path: str | None,
    artifacts: dict[str, Any],
    subtask: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "type": "opencode-subtask-finish",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "taskId": ((subtask or {}).get("taskId") if isinstance(subtask, dict) else None) or task_id,
        "workdir": str(workdir),
        "outcome": outcome,
        "execution": {
            "exitCode": int(exit_code),
            "durationMs": max(0, int(duration_ms)),
            "engine": {
                "selected": engine_selected,
                "fallbackFrom": fallback_from,
            },
            "sessionId": session_id,
            "error": execution_error,
            "warnings": execution_warnings or [],
        },
        "workspace": {
            "changedFiles": changed_files,
            "untrackedFiles": untracked_files,
            "patchPath": patch_path,
        },
        "artifacts": artifacts,
    }
    if subtask is not None:
        obj["subtask"] = subtask
    return obj


def _start_obj(
    *,
    run_id: str,
    task_id: str | None,
    pid: int,
    workdir: Path,
    artifacts_dir: Path,
    artifacts: dict[str, Any],
    memory_recovery: dict[str, Any] | None = None,
    warnings: list[dict[str, str]] | None = None,
    subtask: dict[str, Any] | None = None,
) -> dict[str, Any]:
    obj = {
        "type": "opencode-subtask-start",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": True,
        "warnings": warnings or [],
        "runId": run_id,
        "taskId": task_id or run_id,
        "pid": pid,
        "workdir": str(workdir),
        "artifacts": artifacts,
    }
    if memory_recovery is not None:
        obj["memoryRecovery"] = memory_recovery
    if subtask is not None:
        obj["subtask"] = subtask
    return obj


def _status_obj(
    *,
    ok: bool = True,
    run_id: str,
    status: str,
    pid: int | None,
    workdir: str | None,
    artifacts_dir: Path,
    artifacts: dict[str, Any],
    progress: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    warnings: list[dict[str, str]] | None = None,
    wait_expired: bool | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    warnings_out = warnings or []
    obj = {
        "ok": ok,
        "type": "opencode-subtask-status",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "taskId": task_id,
        "status": status,
        "pid": pid,
        "workdir": workdir,
        "artifacts": artifacts,
        "progress": progress,
        "error": error,
        "warnings": warnings_out,
    }
    if wait_expired is not None:
        obj["waitExpired"] = bool(wait_expired)
    return obj


def _outcome_exit_code(outcome: str) -> int:
    return {
        "completed": 0,
        "failed": 3,
        "timed_out": 124,
        "cancelled": 130,
        "internal_error": 1,
    }.get(str(outcome), 1)


def _execution_outcome_from_error(
    *,
    exit_code: int,
    timed_out: bool,
    error: dict[str, Any] | None,
) -> str:
    if timed_out:
        return "timed_out"
    if not error:
        return "completed"
    name = str(error.get("name") or "").strip()
    if name in {
        "OpencodeNotFound",
        "NoServer",
        "ServerUnhealthy",
        "FinishWriteFailed",
        "WorkerNotRunning",
        "StuckAfterCancel",
        "UnhandledException",
    }:
        return "internal_error"
    if name == "Canceled":
        return "cancelled"
    if int(exit_code) == 130:
        return "cancelled"
    return "failed"


# ============================
# Permission mode
# ============================


def _apply_permission_mode(env: dict[str, str], mode: str) -> None:
    """
    CLI env mode:
      - inherit: no override
      - allow: OPENCODE_PERMISSION={"*":"allow"}
      - noninteractive: OPENCODE_PERMISSION=<no-ask preset> (deny some risky classes; avoid hangs)
    """
    if mode == "inherit":
        return
    if mode == "allow":
        env["OPENCODE_PERMISSION"] = json.dumps({"*": "allow"})
    elif mode == "noninteractive":
        # "noninteractive" means: do not hang on ask prompts; prefer deterministic allow/deny.
        # Keep this preset conservative but usable.
        env["OPENCODE_PERMISSION"] = json.dumps(
            {
                "*": "allow",
                "task": "deny",
                "skill": "deny",
                "external_directory": "deny",
                "doom_loop": "deny",
                "read": {
                    "*": "allow",
                    "*.env": "deny",
                    "*.env.*": "deny",
                    "*.env.example": "allow",
                },
            }
        )
    else:
        env["OPENCODE_PERMISSION"] = mode


def _get_http_auth_from_env(env: dict[str, str]) -> HttpAuth | None:
    # Server docs: OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD for Basic Auth.
    u = env.get("OPENCODE_SERVER_USERNAME")
    p = env.get("OPENCODE_SERVER_PASSWORD")
    if u and p:
        return HttpAuth(username=u, password=p)
    return None



def _model_arg_to_http_message_model(model: str | None) -> dict[str, str] | None:
    """Convert provider/model CLI syntax to the HTTP message body shape."""
    if not model:
        return None
    provider_id, sep, model_id = str(model).partition("/")
    if not sep or not provider_id or not model_id:
        raise RuntimeError("HTTP model must use provider/model form")
    return {"providerID": provider_id, "modelID": model_id}


def _model_arg_to_http_session_model(
    model: str | None, variant: str | None = None
) -> dict[str, str] | None:
    """Convert provider/model to the current session-create model shape.

    OpenCode's own run client uses `{ providerID, id, variant? }` while the
    message endpoint accepts `{ providerID, modelID }`.  We pass session model
    only at creation time and keep the message body shape unchanged.
    """
    if not model:
        return None
    provider_id, sep, model_id = str(model).partition("/")
    if not sep or not provider_id or not model_id:
        raise RuntimeError("HTTP model must use provider/model form")
    out = {"providerID": provider_id, "id": model_id}
    if variant:
        out["variant"] = variant
    return out


def _noninteractive_session_permission_rules(enabled: bool) -> list[dict[str, str]]:
    """Rules borrowed from OpenCode's non-interactive run path.

    These prevent a delegated child run from blocking on question/plan tools.
    Tool/file permissions still remain under OpenCode's normal permission system
    and this adapter's --permission-mode handling.
    """
    if not enabled:
        return []
    return [
        {"permission": "question", "action": "deny", "pattern": "*"},
        {"permission": "plan_enter", "action": "deny", "pattern": "*"},
        {"permission": "plan_exit", "action": "deny", "pattern": "*"},
    ]

# ============================
# Engines
# ============================


class ArtifactBudgetSupervisor:
    def __init__(
        self,
        *,
        watched_paths: Iterable[Path],
        max_bytes: int,
        on_breach: Any,
        poll_interval_s: float = 0.1,
    ) -> None:
        self._watched_paths = [p for p in watched_paths if isinstance(p, Path)]
        self._max_bytes = int(max_bytes)
        self._on_breach = on_breach
        self._poll_interval_s = max(0.01, float(poll_interval_s))
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._breached_path: Path | None = None

    @property
    def breached_path(self) -> Path | None:
        with self._lock:
            return self._breached_path

    def start(self) -> None:
        if self._max_bytes <= 0 or not self._watched_paths or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._poll_interval_s * 4))

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            breached = self._first_breached_path()
            if breached is not None:
                with self._lock:
                    if self._breached_path is None:
                        self._breached_path = breached
                try:
                    self._on_breach(breached)
                except Exception:
                    pass
                return
            self._stop_evt.wait(self._poll_interval_s)

    def _first_breached_path(self) -> Path | None:
        return _first_breached_artifact_path(self._watched_paths, self._max_bytes)


def _first_breached_artifact_path(
    watched_paths: Iterable[Path], max_bytes: int
) -> Path | None:
    max_bytes_n = int(max_bytes)
    if max_bytes_n <= 0:
        return None
    for path in watched_paths:
        try:
            if isinstance(path, Path) and path.exists() and path.stat().st_size > max_bytes_n:
                return path
        except Exception:
            continue
    return None


def _run_cli(
    *,
    opencode_bin: str,
    workdir: Path,
    env: dict[str, str],
    attach_url: str | None,
    prompt_path: Path,
    prompt_text: str,
    cli_prompt_transport: str,
    continue_last: bool,
    session_id: str | None,
    title: str | None,
    agent: str | None,
    model: str | None,
    variant: str | None,
    files: list[str],
    timeout_s: float,
    quiet: bool,
    save_events: bool,
    save_text: bool,
    max_artifact_bytes: int,
    events_path: Path | None,
    stderr_path: Path,
    assistant_path: Path | None,
    on_session_id: Any | None,
) -> RunOutcome:
    cmd: list[str] = [opencode_bin, "run", "--format", "json"]
    if attach_url:
        cmd.extend(["--attach", attach_url])
    if continue_last:
        cmd.append("--continue")
    if session_id:
        cmd.extend(["--session", session_id])
    if title:
        cmd.extend(["--title", title])
    if agent:
        cmd.extend(["--agent", agent])
    if model:
        cmd.extend(["--model", model])
    if variant:
        cmd.extend(["--variant", variant])
    for f in files:
        cmd.extend(["--file", f])

    prompt_transport = (cli_prompt_transport or DEFAULT_CLI_PROMPT_TRANSPORT).strip().lower()
    if prompt_transport not in {"stdin", "file"}:
        prompt_transport = DEFAULT_CLI_PROMPT_TRANSPORT

    # Latest `opencode run` resolves piped stdin as the message input.  Prefer
    # stdin so prompt.txt remains an adapter artifact instead of a model-visible
    # file attachment.  The file transport is kept as an explicit fallback.
    use_stdin_prompt = prompt_transport == "stdin"
    if not use_stdin_prompt:
        cmd.extend(["--file", str(prompt_path)])
        cmd.append("--")
        cmd.append("Follow the instructions in the attached prompt.txt.")

    events_fp = (
        open(events_path, "ab", buffering=0) if (save_events and events_path) else None
    )
    assistant_fp = (
        open(assistant_path, "ab", buffering=0)
        if (save_text and assistant_path)
        else None
    )
    stderr_fp = open(stderr_path, "ab", buffering=0)

    tail = _TailText()
    text_acc = _AssistantTextAccumulator()
    observed_session_id: str | None = session_id
    error_event: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None

    stdin_source: Any = subprocess.DEVNULL
    stdin_file: Any | None = None
    if use_stdin_prompt:
        # Feed the prompt through process stdin from the already-written
        # prompt artifact. This keeps prompt.txt out of OpenCode's model-visible
        # --file attachments while avoiding pipe-writer deadlocks on long prompts.
        stdin_file = open(prompt_path, "rb")
        stdin_source = stdin_file

    try:
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs.update(_win_hide_popen_kwargs(detached=False))
        else:
            # Place the child in its own process group so _kill_tree's
            # os.killpg(pid, sig) targets only the subtask tree, not the
            # adapter's own process group.
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdin=stdin_source,
            stdout=subprocess.PIPE,
            stderr=stderr_fp,
            env=env,
            **popen_kwargs,
        )
    except Exception as e:
        if stdin_file is not None:
            try:
                stdin_file.close()
            except Exception:
                pass
        if events_fp:
            events_fp.close()
        if assistant_fp:
            assistant_fp.close()
        stderr_fp.close()
        return RunOutcome(
            ok=False,
            exit_code=127,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id=None,
            full_text="",
            metrics=None,
            error={"name": type(e).__name__, "message": str(e)},
        )

    if stdin_file is not None:
        try:
            stdin_file.close()
        except Exception:
            pass

    timed_out = False
    killed_for_size = False
    killed_file: str | None = None
    kill_lock = threading.Lock()

    def on_artifact_breach(path: Path) -> None:
        nonlocal killed_for_size, killed_file
        with kill_lock:
            if killed_for_size:
                return
            killed_for_size = True
            killed_file = path.name
        try:
            sent = _kill_tree(proc.pid, sig=(signal.SIGKILL if os.name != "nt" else None))
            if (not sent) and proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    watched_artifact_paths = [p for p in [stderr_path, events_path, assistant_path] if p]
    supervisor = ArtifactBudgetSupervisor(
        watched_paths=watched_artifact_paths,
        max_bytes=max_artifact_bytes,
        on_breach=on_artifact_breach,
    )
    supervisor.start()

    def reader() -> None:
        nonlocal observed_session_id, error_event, metrics
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            if not raw:
                break
            # Write events log
            if events_fp:
                try:
                    events_fp.write(raw)
                except Exception:
                    pass
            if not quiet:
                try:
                    # Never write streaming events to stdout; stdout is reserved for the command result.
                    sys.stderr.buffer.write(raw)
                    sys.stderr.buffer.flush()
                except Exception:
                    pass
            # Parse JSON event
            try:
                evt = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            evt_payload = _event_payload(evt)
            sid = _event_session_id(evt_payload)
            if isinstance(sid, str) and sid and sid != observed_session_id:
                observed_session_id = sid
                if on_session_id:
                    try:
                        on_session_id(observed_session_id)
                    except Exception:
                        pass
            et = evt_payload.get("type")
            if et == "error" or (isinstance(et, str) and et.endswith(".error")):
                error_event = evt_payload
            # Metrics from step-finish-ish events (best-effort)
            if et in ("step-finish", "step_finish", "step-finished", "message.finish", "message.finished"):
                part = _first_nested_dict(evt_payload, "part") or evt_payload
                if isinstance(part, dict):
                    tok = (
                        part.get("tokens")
                        if isinstance(part.get("tokens"), dict)
                        else None
                    )
                    metrics = {
                        "reason": part.get("reason") or part.get("finish"),
                        "cost": part.get("cost"),
                        "tokens": tok,
                    }
            # Collect final assistant text only; avoid user echoes, reasoning/tool
            # metadata, and duplicate cumulative snapshots.
            t = text_acc.consume(evt_payload)
            if isinstance(t, str) and t:
                tail.append(t)
                if assistant_fp:
                    try:
                        assistant_fp.write(t.encode("utf-8", errors="replace"))
                    except Exception:
                        pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            term_sent = _kill_tree(
                proc.pid, sig=(signal.SIGTERM if os.name != "nt" else None)
            )
            if (not term_sent) and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                kill_sent = _kill_tree(
                    proc.pid, sig=(signal.SIGKILL if os.name != "nt" else None)
                )
                if (not kill_sent) and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    # Let the reader drain buffered JSON events after process exit. Closing the
    # pipe first can race and truncate final message.part.updated events.
    th.join(timeout=5)
    if th.is_alive():
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        th.join(timeout=1)
    supervisor.stop()

    if events_fp:
        events_fp.close()
    if assistant_fp:
        assistant_fp.close()
    stderr_fp.close()

    breached_after_exit = supervisor.breached_path or _first_breached_artifact_path(
        watched_artifact_paths, max_artifact_bytes
    )
    if breached_after_exit is not None:
        killed_for_size = True
        killed_file = breached_after_exit.name

    exit_code = proc.returncode if proc.returncode is not None else 1
    full_text = tail.get()
    if killed_for_size:
        # Oversized artifacts are an execution failure; do not let a retained
        # text tail make the run look successful.
        full_text = ""

    ok = (
        (not timed_out)
        and (not killed_for_size)
        and exit_code == 0
        and error_event is None
    )

    err: dict[str, Any] | None = None
    if not ok:
        if killed_for_size:
            err = {
                "name": "OutputTooLarge",
                "message": f"artifact {killed_file} exceeded max-artifact-bytes={max_artifact_bytes}",
            }
        elif timed_out:
            err = {
                "name": "Timeout",
                "message": f"opencode run exceeded timeout={timeout_s}s",
            }
        elif error_event:
            err = {
                "name": "OpencodeErrorEvent",
                "message": json.dumps(error_event, ensure_ascii=False)[:5000],
            }
        else:
            err = _stderr_failure_error(stderr_path, exit_code)

    return RunOutcome(
        ok=ok,
        exit_code=exit_code,
        timed_out=timed_out,
        engine="cli",
        fallback_from=None,
        session_id=observed_session_id,
        full_text=full_text,
        metrics=metrics,
        error=err,
    )


def _extract_text_from_message_obj(msg: dict[str, Any]) -> str:
    # /message response is documented as { info: Message, parts: Part[] }.
    # Only text-like assistant parts are user-visible child-agent output.  Do
    # not dump the full JSON response as text; that turns transport metadata
    # into false success.
    parts = msg.get("parts")
    if isinstance(parts, list):
        out: list[str] = []
        for p in parts:
            if isinstance(p, dict):
                p_type = str(p.get("type") or "").lower()
                if p_type and p_type not in {"text", "assistant_text", "output_text"}:
                    continue
                if isinstance(p.get("text"), str):
                    out.append(p["text"])
                elif isinstance(p.get("content"), str):
                    out.append(p["content"])
            elif isinstance(p, str):
                out.append(p)
        return "".join(out)

    for src in (msg, msg.get("info") if isinstance(msg.get("info"), dict) else None):
        if not isinstance(src, dict):
            continue
        for key in ("text", "content", "output"):
            v = src.get(key)
            if isinstance(v, str):
                return v
        v = src.get("structured_output") or src.get("structuredOutput")
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
    return ""


def _extract_error_from_message_obj(msg: dict[str, Any]) -> dict[str, Any] | None:
    for src in (msg, msg.get("info") if isinstance(msg.get("info"), dict) else None):
        if not isinstance(src, dict):
            continue
        err = src.get("error")
        if isinstance(err, dict):
            return {"name": str(err.get("name") or "OpencodeMessageError"), "message": json.dumps(err, ensure_ascii=False)[:5000]}
        if isinstance(err, str) and err:
            return {"name": "OpencodeMessageError", "message": err[:5000]}
    return None



def _run_http(
    *,
    server_url: str,
    workdir: Path,
    env: dict[str, str],
    prompt: str,
    agent: str | None,
    model: str | None,
    variant: str | None,
    session_id_seed: str | None,
    title: str | None,
    timeout_s: float,
    save_events: bool,
    save_text: bool,
    max_artifact_bytes: int,
    events_path: Path | None,
    stderr_path: Path,
    assistant_path: Path | None,
    permission_mode: str,
    permission_approval: str,
    http_deny_interactive_tools: bool,
    subtask_permission_rules: list[dict[str, str]] | None,
    on_session_id: Any | None,
) -> RunOutcome:
    auth = _get_http_auth_from_env(env)
    client = OpencodeHttpClient(server_url, auth=auth, timeout_s=10.0)

    # Verify server health (fast) but don't fail hard; we'll report.
    health = client.health()
    if not (isinstance(health, dict) and health.get("healthy") is True):
        return RunOutcome(
            ok=False,
            exit_code=2,
            timed_out=False,
            engine="http",
            fallback_from=None,
            session_id=None,
            full_text="",
            metrics=None,
            error={
                "name": "ServerUnhealthy",
                "message": f"/global/health not healthy for {server_url}",
            },
        )

    events_fp = (
        open(events_path, "ab", buffering=0) if (save_events and events_path) else None
    )
    assistant_fp = (
        open(assistant_path, "ab", buffering=0)
        if (save_text and assistant_path)
        else None
    )
    stderr_fp = open(stderr_path, "ab", buffering=0)

    responded_permissions: set[str] = set()
    stop_evt = threading.Event()
    sse_connected = threading.Event()
    sse_open_error: list[str] = []
    sse_tail = _TailText()
    sse_text_acc = _AssistantTextAccumulator()
    sse_error_event: list[dict[str, Any]] = []
    session_box: dict[str, str | None] = {"id": None}

    def _output_too_large_error(path: Path) -> dict[str, str]:
        return {
            "name": "OutputTooLarge",
            "message": (
                f"artifact {path.name} exceeded "
                f"max-artifact-bytes={max_artifact_bytes}"
            ),
        }

    def on_artifact_breach(path: Path) -> None:
        stop_evt.set()
        session_id_local = session_box.get("id")
        if session_id_local:
            try:
                client.abort(session_id_local)
            except Exception:
                pass

    watched_artifact_paths = [p for p in [stderr_path, events_path, assistant_path] if p]
    supervisor = ArtifactBudgetSupervisor(
        watched_paths=watched_artifact_paths,
        max_bytes=max_artifact_bytes,
        on_breach=on_artifact_breach,
    )
    supervisor.start()

    def _permission_object(evt: dict[str, Any]) -> dict[str, Any] | None:
        evt = _event_payload(evt)
        props = _event_properties(evt)
        for src in (props, evt):
            for key in ("permission", "request", "data"):
                v = src.get(key)
                if isinstance(v, dict):
                    # Current permission.asked events often place the request
                    # directly in properties; data/permission may wrap it.
                    if key == "data":
                        for nested_key in ("permission", "request"):
                            nested = v.get(nested_key)
                            if isinstance(nested, dict):
                                return nested
                    return v
        return props if props else None

    def _noninteractive_permission_reply(evt: dict[str, Any]) -> str:
        """
        Best-effort unattended policy for latest permission replies.

        reply values are OpenCode's server-side contract: once / always / reject.
        Unknown low-risk requests default to once so HTTP runs do not hang, while
        broad delegation and .env-style secret reads are rejected when detected.
        """
        perm_obj = _permission_object(evt) or {}

        def _strings_from(obj: Any) -> list[str]:
            out: list[str] = []
            if isinstance(obj, str):
                out.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    out.extend(_strings_from(v))
            elif isinstance(obj, list):
                for v in obj:
                    out.extend(_strings_from(v))
            return out

        kind = None
        for k in ("permission", "type", "name", "tool", "category", "action"):
            v = perm_obj.get(k)
            if isinstance(v, str) and v:
                kind = v
                break
        kind_l = kind.lower() if isinstance(kind, str) else ""
        if kind_l in ("task", "skill", "external_directory", "doom_loop"):
            return "reject"

        # Deny reads of .env-style secrets when we can detect them.
        # (The config-level preset handles most cases; this protects HTTP auto-reply.)
        if kind_l.startswith("read") or kind_l == "read":
            texts = [s.lower() for s in _strings_from(perm_obj)]
            for s in texts:
                if s.endswith(".env.example"):
                    continue
                if s.endswith(".env") or ".env." in s:
                    return "reject"

        return "once"

    def _permission_reply_for_event(evt: dict[str, Any]) -> str:
        if permission_mode == "allow":
            return permission_approval
        return _noninteractive_permission_reply(evt)

    def maybe_permission_request_id(evt: dict[str, Any]) -> str | None:
        # Latest shape: requestID.  Be liberal inside the latest contract because
        # SSE may wrap the request under properties/data/permission.
        evt = _event_payload(evt)
        for src in _nested_event_dicts(evt):
            for key in ("requestID", "requestId", "id"):
                v = src.get(key)
                if isinstance(v, str) and v:
                    return v
        return None

    def sse_worker(session_id: str) -> None:
        nonlocal responded_permissions
        # Prefer /event; if unavailable, try /global/event.
        paths = ["/event", "/global/event"]
        resp = None
        last_open_err: str | None = None

        for p in paths:
            try:
                # Use a small socket timeout so we can periodically check stop_evt.
                resp = client.open_sse(p, timeout_s=2.0)
                sse_connected.set()
                break
            except Exception as e:
                last_open_err = str(e)
                resp = None

        if resp is None:
            if last_open_err:
                sse_open_error.append(last_open_err)
            return

        data_lines: list[str] = []
        try:
            while not stop_evt.is_set():
                try:
                    line = resp.readline()
                except socket.timeout:
                    continue
                except Exception:
                    break

                if not line:
                    break  # EOF

                try:
                    s = line.decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    continue

                if s.startswith(":"):
                    # comment/keepalive
                    continue

                if s == "":
                    # end of event
                    if not data_lines:
                        continue
                    payload = "\n".join(data_lines).strip()
                    data_lines.clear()
                    if not payload:
                        continue
                    try:
                        evt = json.loads(payload)
                    except Exception:
                        continue
                    if not isinstance(evt, dict):
                        continue

                    evt = _event_payload(evt)
                    if not isinstance(evt, dict):
                        continue

                    # Filter by sessionID when present; keep server.connected even without sessionID.
                    sid = _event_session_id(evt)
                    if isinstance(sid, str) and sid and sid != session_id:
                        continue
                    if not sid and evt.get("type") not in (
                        "server.connected",
                        "server_connected",
                    ):
                        continue

                    # Write NDJSON
                    if events_fp:
                        try:
                            events_fp.write(
                                (json.dumps(evt, ensure_ascii=False) + "\n").encode(
                                    "utf-8"
                                )
                            )
                        except Exception:
                            pass

                    et = evt.get("type")
                    if et == "session.error" or (isinstance(et, str) and et.endswith(".error")):
                        sse_error_event.append(evt)

                    # Keep a text fallback from the same event stream used by
                    # official clients.  We do not write it to assistant.txt
                    # here to avoid duplicating the synchronous /message
                    # response; it is used only if the response object has no
                    # extractable text.
                    t = sse_text_acc.consume(evt)
                    if isinstance(t, str) and t:
                        sse_tail.append(t)

                    # Auto permission replies (best-effort, latest server contract).
                    if permission_mode in ("allow", "noninteractive"):
                        if isinstance(et, str) and "permission" in et:
                            request_id = maybe_permission_request_id(evt)
                            if request_id and request_id not in responded_permissions:
                                responded_permissions.add(request_id)
                                client.reply_permission(
                                    request_id,
                                    reply=_permission_reply_for_event(evt),
                                )

                    continue  # done processing one event

                if s.startswith("data:"):
                    data_lines.append(s[5:].lstrip())
                else:
                    # ignore other SSE fields (event:, id:, retry:)
                    continue
        finally:
            try:
                resp.close()
            except Exception:
                pass

    session: dict[str, Any] | None = None
    session_id: str | None = None
    try:
        if session_id_seed:
            session_id = session_id_seed
        else:
            session = client.create_session(
                title=title,
                agent=agent,
                model=_model_arg_to_http_session_model(model, variant),
                permission=_dedupe_permission_rules(
                    [
                        *_noninteractive_session_permission_rules(
                            bool(http_deny_interactive_tools)
                        ),
                        *(subtask_permission_rules or []),
                    ]
                ),
            )
            sid = session.get("id") if isinstance(session.get("id"), str) else None
            if not sid:
                raise RuntimeError("Session created but missing id")
            session_id = sid
        session_box["id"] = session_id
        if on_session_id:
            try:
                on_session_id(session_id)
            except Exception:
                pass
        breached_path = supervisor.breached_path
        if stop_evt.is_set() and breached_path is not None:
            try:
                client.abort(session_id)
            except Exception:
                pass
            supervisor.stop()
            if events_fp:
                events_fp.close()
            if assistant_fp:
                assistant_fp.close()
            stderr_fp.close()
            return RunOutcome(
                ok=False,
                exit_code=1,
                timed_out=False,
                engine="http",
                fallback_from=None,
                session_id=session_id,
                full_text="",
                metrics=None,
                error=_output_too_large_error(breached_path),
            )
    except Exception as e:
        supervisor.stop()
        if events_fp:
            events_fp.close()
        if assistant_fp:
            assistant_fp.close()
        stderr_fp.write((str(e) + "\n").encode("utf-8", errors="replace"))
        stderr_fp.close()
        return RunOutcome(
            ok=False,
            exit_code=2,
            timed_out=False,
            engine="http",
            fallback_from=None,
            session_id=None,
            full_text="",
            metrics=None,
            error={"name": "SessionCreateFailed", "message": str(e)},
        )

    # Start SSE thread (for diagnostics and auto-permissions).
    t = threading.Thread(target=sse_worker, args=(session_id,), daemon=True)
    t.start()
    # Require SSE when saving events and/or when we must auto-handle permissions.
    if not sse_connected.wait(timeout=2.0):
        if save_events or permission_mode in ("allow", "noninteractive"):
            client.abort(session_id)
            stop_evt.set()
            supervisor.stop()
            if events_fp:
                events_fp.close()
            if assistant_fp:
                assistant_fp.close()
            stderr_fp.write(
                (
                    "SSE unavailable; cannot stream events for diagnostics/permissions.\n"
                ).encode("utf-8")
            )
            stderr_fp.close()
            return RunOutcome(
                ok=False,
                exit_code=2,
                timed_out=False,
                engine="http",
                fallback_from=None,
                session_id=session_id,
                full_text="",
                metrics=None,
                error={
                    "name": "SseUnavailable",
                    "message": (
                        sse_open_error[-1]
                        if sse_open_error
                        else "SSE stream not connected"
                    ),
                },
            )

    timed_out = False
    err: dict[str, Any] | None = None
    msg_obj: dict[str, Any] | None = None

    try:
        if supervisor.breached_path is not None:
            err = _output_too_large_error(supervisor.breached_path)
        else:
            msg_obj = client.send_message_sync(
                session_id,
                prompt=prompt,
                model=model,
                variant=variant,
                agent=agent,
                timeout_s=timeout_s,
            )
    except Exception as e:
        # If it's a timeout at HTTP layer, abort session.
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            timed_out = True
            client.abort(session_id)
            err = {
                "name": "Timeout",
                "message": f"HTTP message exceeded timeout={timeout_s}s",
            }
        else:
            err = {"name": "HttpError", "message": str(e)}
    finally:
        stop_evt.set()
        # best-effort join
        t.join(timeout=2.0)
        supervisor.stop()

    if err is None and isinstance(msg_obj, dict):
        msg_error = _extract_error_from_message_obj(msg_obj)
        if msg_error is not None:
            err = msg_error
            full_text = ""
        else:
            text = _extract_text_from_message_obj(msg_obj)
            if not text:
                # Some server/client combinations surface the complete text in
                # events but return an empty message body.  Fall back to the
                # subscribed event stream rather than forcing a duplicate CLI run.
                text = sse_tail.get()
            if (not text) and sse_error_event:
                err = {
                    "name": "OpencodeSseErrorEvent",
                    "message": json.dumps(sse_error_event[-1], ensure_ascii=False)[:5000],
                }
                full_text = ""
            else:
                if assistant_fp:
                    try:
                        assistant_fp.write(text.encode("utf-8", errors="replace"))
                    except Exception:
                        pass
                breached_after_write = supervisor.breached_path or _first_breached_artifact_path(
                    watched_artifact_paths, max_artifact_bytes
                )
                if breached_after_write is not None:
                    err = _output_too_large_error(breached_after_write)
                    full_text = ""
                else:
                    full_text = text
    else:
        full_text = ""

    if supervisor.breached_path is not None:
        timed_out = False
        err = _output_too_large_error(supervisor.breached_path)

    if events_fp:
        events_fp.close()
    if assistant_fp:
        assistant_fp.close()
    stderr_fp.close()

    ok = (err is None) and (not timed_out)

    return RunOutcome(
        ok=ok,
        exit_code=0 if ok else (124 if timed_out else 1),
        timed_out=timed_out,
        engine="http",
        fallback_from=None,
        session_id=session_id,
        full_text=full_text,
        metrics=None,
        error=err,
    )


# ============================
# Commands
# ============================


def _assistant_text_from_finish_for_ask(
    finish_obj: dict[str, Any],
) -> tuple[str, Path | None]:
    """Return assistant final text for the direct `ask` command."""
    artifacts = finish_obj.get("artifacts")
    if not isinstance(artifacts, dict):
        return "", None
    raw_dir = artifacts.get("dir")
    raw_assistant = artifacts.get("assistantPath")
    if not isinstance(raw_dir, str) or not raw_dir:
        return "", None
    if not isinstance(raw_assistant, str) or not raw_assistant:
        return "", None
    assistant_path = Path(raw_dir).expanduser() / raw_assistant
    try:
        return _read_text(assistant_path), assistant_path
    except Exception:
        return "", assistant_path


def _finish_path_from_finish_for_ask(finish_obj: dict[str, Any]) -> Path | None:
    artifacts = finish_obj.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    raw_dir = artifacts.get("dir")
    raw_finish = artifacts.get("finishPath")
    if not isinstance(raw_dir, str) or not raw_dir:
        return None
    if not isinstance(raw_finish, str) or not raw_finish:
        return None
    return Path(raw_dir).expanduser() / raw_finish


def _ask_error_message(obj: dict[str, Any] | None, raw_stdout: str) -> str:
    if isinstance(obj, dict) and obj.get("type") == "opencode-subtask-finish":
        outcome = str(obj.get("outcome") or "unknown")
        execution = obj.get("execution") if isinstance(obj.get("execution"), dict) else {}
        err = execution.get("error") if isinstance(execution, dict) else None
        if isinstance(err, dict):
            name = str(err.get("name") or "ExecutionError")
            msg = str(err.get("message") or "")
        else:
            name = "ExecutionError"
            msg = f"opencode finished with outcome={outcome}"
        finish_path = _finish_path_from_finish_for_ask(obj)
        suffix = f"; finish={finish_path}" if finish_path else ""
        return f"opencode-subtask ask failed: outcome={outcome}; {name}: {msg}{suffix}"

    if isinstance(obj, dict) and obj.get("type") == "opencode-subtask-error":
        err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
        name = str(err.get("name") or "AdapterError") if isinstance(err, dict) else "AdapterError"
        msg = str(err.get("message") or "") if isinstance(err, dict) else ""
        return f"opencode-subtask ask failed before execution: {name}: {msg}"

    raw = " ".join((raw_stdout or "").split())
    if len(raw) > 1000:
        raw = raw[:997] + "..."
    return (
        "opencode-subtask ask failed: adapter produced no parseable finish envelope; "
        f"raw_stdout={raw!r}"
    )


def cmd_ask(args: argparse.Namespace) -> int:
    """Run a subtask and print only the assistant's final text to stdout."""
    # `ask` is the natural-language subagent surface. Always preserve assistant.txt;
    # stdout from cmd_run stays internal and is translated into final assistant text.
    args.save_text = True
    args.quiet = True
    args._force_save_text = True

    buf = io.StringIO()
    rc = 1
    try:
        with contextlib.redirect_stdout(buf):
            rc = int(cmd_run(args))
    except SystemExit as exc:
        try:
            rc = int(exc.code) if exc.code is not None else 1
        except Exception:
            rc = 1

    raw_stdout = buf.getvalue().strip()
    obj: dict[str, Any] | None = None
    if raw_stdout:
        first_line = raw_stdout.splitlines()[0]
        try:
            parsed = json.loads(first_line)
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            obj = None

    if rc == 0 and isinstance(obj, dict) and obj.get("type") == "opencode-subtask-finish":
        if bool(getattr(args, "ask_metadata_to_stderr", False)):
            sys.stderr.write(
                "OPENCODE_SUBTASK_META " + _json_line(_ask_metadata_obj(obj)) + "\n"
            )
        text, assistant_path = _assistant_text_from_finish_for_ask(obj)
        if text:
            out_text, truncated = _truncate_for_ask_stdout(
                text,
                max_chars=int(
                    getattr(args, "ask_stdout_max_chars", DEFAULT_ASK_STDOUT_MAX_CHARS)
                ),
                assistant_path=assistant_path,
            )
            sys.stdout.write(out_text)
            if truncated:
                sys.stderr.write(
                    "[opencode-subtask] NOTE: ask stdout was truncated; full assistant text is in "
                    + (str(assistant_path) if assistant_path else "assistant.txt")
                    + "\n"
                )
            return 0
        finish_path = _finish_path_from_finish_for_ask(obj)
        suffix = f"; finish={finish_path}" if finish_path else ""
        if assistant_path:
            suffix += f"; assistant={assistant_path}"
        sys.stderr.write(
            "opencode-subtask ask failed: completed run had no assistant text"
            + suffix
            + "\n"
        )
        return 1

    if bool(getattr(args, "ask_error_json_to_stderr", False)) and isinstance(obj, dict):
        sys.stderr.write("OPENCODE_SUBTASK_ERROR_JSON " + _json_line(obj) + "\n")
    sys.stderr.write(_ask_error_message(obj, raw_stdout) + "\n")
    return rc if rc != 0 else 1


def cmd_task(args: argparse.Namespace) -> int:
    """OpenCode Task-like entrypoint for external parent agents."""
    if bool(getattr(args, "background", False)):
        return cmd_start(args)
    return cmd_ask(args)


def cmd_run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    run_timeout_s = float(args.run_timeout)
    run_started_ms = _now_ms()
    task_res = _resolve_task_id_for_run(args, workdir)
    setattr(args, "_adapter_task_id", task_res.task_id)
    if _task_is_running(task_res.prior_state) and str(getattr(args, "on_running_task", "fail")) != "start-new":
        obj = _error_obj(
            error_name="TaskAlreadyRunning",
            message=(
                f"task_id {task_res.task_id!r} already has a running OpenCode child. "
                "Use status/wait/watch with --task-id, or pass --on-running-task start-new to override."
            ),
        )
        obj["task"] = task_res.prior_state
        sys.stdout.write(_json_line(obj) + "\n")
        return 2

    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    finish_path = artifacts_dir / "finish.json"
    runtime_preflight_warnings: list[dict[str, str]] = []
    existing_finish, finish_warnings = _load_runtime_finish_envelope(finish_path)
    runtime_preflight_warnings = _dedupe_warnings(
        [*runtime_preflight_warnings, *finish_warnings]
    )
    if isinstance(existing_finish, dict):
        err = _error_obj(
            error_name="FinishAlreadyPresent",
            message=_finish_already_present_message(finish_path),
            warnings=runtime_preflight_warnings,
        )
        sys.stdout.write(_json_line(err) + "\n")
        return 1

    # Prompt
    prompt = _resolve_prompt_input(args)
    if bool(getattr(args, "task_memory", True)):
        prompt = prompt.rstrip() + _task_memory_block(
            task_res,
            max_chars=int(getattr(args, "task_memory_max_chars", DEFAULT_TASK_MEMORY_MAX_CHARS)),
        )

    # Merge env early so profile thresholds can honor --env / --env-file overrides.
    env = _safe_merge_env(os.environ, set_vars=args.env, set_from_files=args.env_file)
    subtask_info = _apply_subtask_preset(args)
    subtask_info["taskStatePath"] = str(task_res.state_path)
    subtask_info["taskProgressPath"] = str(task_res.progress_path)
    subtask_info["continued"] = bool(task_res.prior_state)
    http_deny_interactive_tools = _effective_http_deny_interactive_tools(
        args, subtask_info
    )
    requested_engine = str(getattr(args, "engine", "auto"))
    profile_info = _apply_execution_profile(args, prompt, env)
    subtask_info["executionProfile"] = getattr(args, "execution_profile", None)
    if bool(getattr(args, "_force_save_text", False)):
        args.save_text = True

    prompt = _apply_persona_policy(prompt, args.persona_mode, args.persona_line)
    prompt = _apply_subagent_briefing(
        prompt,
        enabled=bool(getattr(args, "subagent_briefing", False)),
        final_answer_budget_chars=int(
            getattr(args, "final_answer_budget_chars", DEFAULT_FINAL_ANSWER_BUDGET_CHARS)
        ),
    )
    prompt = _apply_subtask_briefing(prompt, subtask_info=subtask_info)

    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    # Artifacts paths
    job_path = artifacts_dir / "job.json"
    events_path = artifacts_dir / "events.ndjson" if args.save_events else None
    assistant_path = artifacts_dir / "assistant.txt" if args.save_text else None
    stderr_path = artifacts_dir / "stderr.log"
    # wrapper.log is only meaningful in start (background) mode;
    # in foreground run mode the file won't exist, so _artifacts_obj
    # will return null for wrapperLogPath.  But we point to the
    # canonical path so that if a start-mode worker reuses this code
    # path the artifact is correctly referenced when present.
    wrapper_log_path = artifacts_dir / "wrapper.log"

    # Job init
    seed_session_id = str(getattr(args, "session", "") or "").strip() or None
    memory_recovery = _opencode_memory_recovery_hint(
        enabled=bool(getattr(args, "memory_friendly", False)),
        workdir=workdir,
        artifacts_dir=artifacts_dir,
        run_id=run_id,
        session_id=seed_session_id,
    )
    job_obj = {
        "runId": run_id,
        "taskId": task_res.task_id,
        "adapterVersion": ADAPTER_VERSION,
        "workdir": str(workdir),
        "state": "running",
        "createdAt": _now_ms(),
        "updatedAt": _now_ms(),
        "pid": os.getpid(),
        "engine": args.engine,
        "stopServerAfterRunMode": str(
            getattr(args, "stop_server_after_run", DEFAULT_STOP_SERVER_AFTER_RUN)
        ),
        "serverStartedNew": False,
        "httpAttempted": False,
        "orphanReaperEnabled": bool(getattr(args, "orphan_reaper", True)),
        "sessionId": seed_session_id,
        "memoryFriendly": bool(getattr(args, "memory_friendly", False)),
        "memoryRecovery": memory_recovery,
        "subtask": subtask_info,
    }
    _write_job_locked(job_path, artifacts_dir, job_obj)
    _write_task_state_locked(
        workdir=workdir,
        task_id=task_res.task_id,
        update={
            "state": "running",
            "latestRunId": run_id,
            "latestArtifactsDir": str(artifacts_dir),
            "sessionId": seed_session_id,
            "subtask": subtask_info,
            "agent": getattr(args, "agent", None),
            "model": getattr(args, "model", None),
            "variant": getattr(args, "variant", None),
            "title": getattr(args, "title", None),
        },
    )
    _append_task_progress(
        task_res.progress_path,
        event="started",
        fields={
            "runId": run_id,
            "sessionId": seed_session_id,
            "subtaskType": subtask_info.get("type"),
            "agent": getattr(args, "agent", None),
            "engine": getattr(args, "engine", None),
            "artifactsDir": str(artifacts_dir),
        },
    )

    def _update_job(fields: dict[str, Any]) -> None:
        _update_job_fields_locked(job_path, artifacts_dir, fields)
        task_update = {"latestRunId": run_id, "latestArtifactsDir": str(artifacts_dir)}
        if "sessionId" in fields:
            task_update["sessionId"] = fields.get("sessionId")
            _append_task_progress(task_res.progress_path, event="session", fields={"sessionId": fields.get("sessionId")})
        _write_task_state_locked(workdir=workdir, task_id=task_res.task_id, update=task_update)

    # Env
    # Defensive defaults (no-op if ignored by OpenCode)
    env.setdefault("OPENCODE_CLIENT", "opencode-subtask")
    if args.disable_claude_code:
        env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE", "1")
    _apply_permission_mode(env, args.permission_mode)
    _apply_subtask_permission_rules_to_env(
        env, list(subtask_info.get("permissionRules") or [])
    )

    auth = _get_http_auth_from_env(env)
    reaper_info: dict[str, Any] | None = None
    effective_engine = str(getattr(args, "engine", requested_engine)).strip().lower()
    reaper_idle_s = float(
        getattr(args, "orphan_reaper_idle_s", DEFAULT_ORPHAN_REAPER_IDLE_S)
    )
    if reaper_idle_s < 0:
        _exit_with_error(
            "BadConfig",
            f"--orphan-reaper-idle-s must be >= 0, got: {reaper_idle_s}",
            exit_code=2,
        )
    if (
        bool(getattr(args, "orphan_reaper", True))
        and (effective_engine in ("http", "auto"))
        and (args.attach is None)
        and bool(args.attach_server)
    ):
        try:
            reaper_info = reap_orphan_server_for_project(
                workdir=workdir,
                auth=auth,
                idle_s=reaper_idle_s,
            )
        except Exception as e:
            reaper_info = {
                "checked": True,
                "reaped": False,
                "reason": "reaper-error",
                "error": f"{type(e).__name__}: {e}",
            }
        _update_job({"orphanReaper": reaper_info})

    # Server attach/ensure
    server_state: dict[str, Any] | None = None
    attach_url = args.attach
    server_error: dict[str, Any] | None = None
    server_started_new = False
    stop_server_mode = (
        str(getattr(args, "stop_server_after_run", DEFAULT_STOP_SERVER_AFTER_RUN))
        .strip()
        .lower()
    )

    # Need opencode binary if we might call CLI engine OR we need to start a server.
    opencode_bin: str | None = None
    need_opencode_bin = (args.engine == "cli") or (
        (args.engine == "http") and (not attach_url) and args.attach_server
    )
    if need_opencode_bin:
        default_cmd = args.opencode
        opencode_bin = _resolve_executable_for_workdir(default_cmd, workdir)
        if not opencode_bin:
            out = _finish_obj(
                run_id=run_id,
                workdir=workdir,
                outcome="internal_error",
                exit_code=127,
                duration_ms=max(0, _now_ms() - run_started_ms),
                engine_selected="none",
                fallback_from=None,
                session_id=None,
                execution_error={
                    "name": "OpencodeNotFound",
                    "message": f"Could not find opencode executable: {default_cmd}",
                },
                execution_warnings=[],
                changed_files=[],
                untracked_files=[],
                patch_path=None,
                artifacts=_artifacts_obj(
                    dir_path=artifacts_dir,
                    job_path=job_path,
                    finish_path=finish_path,
                    prompt_path=prompt_path,
                    events_path=events_path,
                    stderr_path=stderr_path,
                    assistant_path=assistant_path,
                    wrapper_log_path=wrapper_log_path,
                    progress_path=task_res.progress_path,
                    notification_path=artifacts_dir / "notification.json",
                ),
                subtask=subtask_info,
            )
            finish_written, _, existing_finish = _write_finish_once(
                artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
            )
            if (not finish_written) and isinstance(existing_finish, dict):
                out = existing_finish
                sys.stdout.write(_json_line(out) + "\n")
                return _outcome_exit_code(str(out.get("outcome") or "internal_error"))
            sys.stdout.write(_json_line(out) + "\n")
            return _outcome_exit_code("internal_error")

    if not attach_url and args.attach_server:
        # Engine policy:
        # - http: ensure/start a server if needed.
        # - auto: attach to an already-running server if present; do not start a new one.
        # - cli: ignore attach_server unless user provided an explicit --attach.
        if args.engine == "http":
            try:
                assert opencode_bin is not None
                # Prefer reusing an already-running healthy server (if any) to avoid
                # spawning multiple per-project servers and to make "stop-after-run"
                # safe (we only stop servers that we started in this invocation).
                server_state = attach_existing_server(
                    workdir=workdir, auth=auth, wait_s=float(args.server_wait)
                )
                if server_state:
                    attach_url = str(server_state.get("url"))
                    server_started_new = False
                else:
                    server_state = ensure_server(
                        opencode_bin=opencode_bin,
                        workdir=workdir,
                        hostname=args.server_hostname,
                        port=args.server_port,
                        wait_s=args.server_wait,
                        env=env,
                        auth=auth,
                        cors_origins=list(getattr(args, "server_cors", []) or []),
                    )
                    server_started_new = (
                        bool(server_state.get("startedNew"))
                        if isinstance(server_state, dict)
                        else False
                    )
                    attach_url = str(server_state.get("url"))
            except Exception as e:
                server_error = {"name": type(e).__name__, "message": str(e)}
                server_state = None
                attach_url = None
        elif args.engine == "auto":
            try:
                server_state = attach_existing_server(
                    workdir=workdir, auth=auth, wait_s=float(args.server_wait)
                )
                if server_state:
                    attach_url = str(server_state.get("url"))
                    server_started_new = False
            except Exception as e:
                server_error = {"name": type(e).__name__, "message": str(e)}
                server_state = None
                attach_url = None

    _update_job({"serverStartedNew": server_started_new})
    if attach_url:
        _update_job({"serverUrl": attach_url, "serverStartedNew": server_started_new})

    # Decide engine
    chosen = args.engine
    fallback_from: str | None = None
    outcome: RunOutcome | None = None
    retry_empty_output = bool(getattr(args, "retry_empty_output", True))
    try:
        max_empty_output_retries = max(0, int(getattr(args, "empty_output_retries", 1)))
    except Exception:
        max_empty_output_retries = 1
    empty_output_detected = False
    empty_output_retried = False
    empty_output_recovered = False
    empty_output_attempts = 0

    # CLI workaround: in some OpenCode versions, "opencode run --attach ... --agent <name>" can fail.
    # If attach was auto-created (not user-provided) and agent is set, prefer standalone CLI (no attach).
    cli_attach_url = attach_url
    if (
        args.agent
        and cli_attach_url
        and (args.attach is None)
        and args.workaround_agent_attach
    ):
        cli_attach_url = None

    http_unsupported = _http_unsupported_options(args)
    auto_http_skip_warning: dict[str, str] | None = None
    if http_unsupported and args.engine == "auto":
        chosen = "cli"
        auto_http_skip_warning = _warning(
            "HttpEngineSkipped",
            "auto mode selected CLI because HTTP does not support "
            + ", ".join(http_unsupported),
        )

    def _ensure_opencode_bin_for_cli() -> str | None:
        nonlocal opencode_bin
        if opencode_bin:
            return opencode_bin
        resolved = _resolve_executable_for_workdir(str(args.opencode), workdir)
        if not resolved:
            return None
        opencode_bin = resolved
        return opencode_bin

    def run_cli() -> RunOutcome:
        cli_bin = _ensure_opencode_bin_for_cli()
        if not cli_bin:
            return RunOutcome(
                ok=False,
                exit_code=127,
                timed_out=False,
                engine="cli",
                fallback_from=None,
                session_id=None,
                full_text="",
                metrics=None,
                error={
                    "name": "OpencodeNotFound",
                    "message": f"Could not find opencode executable: {args.opencode}",
                },
            )
        return _run_cli(
            opencode_bin=cli_bin,
            workdir=workdir,
            env=env,
            attach_url=cli_attach_url,
            prompt_path=prompt_path,
            prompt_text=prompt,
            cli_prompt_transport=str(
                getattr(args, "cli_prompt_transport", DEFAULT_CLI_PROMPT_TRANSPORT)
            ),
            continue_last=bool(getattr(args, "continue_last", False)),
            session_id=getattr(args, "session", None),
            title=getattr(args, "title", None),
            agent=args.agent,
            model=args.model,
            variant=getattr(args, "variant", None),
            files=list(args.file),
            timeout_s=run_timeout_s,
            quiet=args.quiet,
            save_events=args.save_events,
            save_text=args.save_text,
            max_artifact_bytes=int(args.max_artifact_bytes),
            events_path=events_path,
            stderr_path=stderr_path,
            assistant_path=assistant_path,
            on_session_id=lambda sid: _update_job({"sessionId": sid}),
        )

    def run_http() -> RunOutcome:
        if http_unsupported:
            return RunOutcome(
                ok=False,
                exit_code=2,
                timed_out=False,
                engine="http",
                fallback_from=None,
                session_id=None,
                full_text="",
                metrics=None,
                error={
                    "name": "UnsupportedHttpOptions",
                    "message": "HTTP engine does not support "
                    + ", ".join(http_unsupported),
                },
            )
        if not attach_url:
            return RunOutcome(
                ok=False,
                exit_code=2,
                timed_out=False,
                engine="http",
                fallback_from=None,
                session_id=None,
                full_text="",
                metrics=None,
                error={
                    "name": "NoServer",
                    "message": "HTTP engine requires --attach or --attach-server (default)",
                },
            )
        return _run_http(
            server_url=attach_url,
            workdir=workdir,
            env=env,
            prompt=prompt,
            agent=args.agent,
            model=args.model,
            variant=getattr(args, "variant", None),
            session_id_seed=getattr(args, "session", None),
            title=getattr(args, "title", None),
            timeout_s=run_timeout_s,
            save_events=args.save_events,
            save_text=args.save_text,
            max_artifact_bytes=int(args.max_artifact_bytes),
            events_path=events_path,
            stderr_path=stderr_path,
            assistant_path=assistant_path,
            permission_mode=args.permission_mode,
            permission_approval=args.permission_approval,
            http_deny_interactive_tools=http_deny_interactive_tools,
            subtask_permission_rules=list(subtask_info.get("permissionRules") or []),
            on_session_id=lambda sid: _update_job({"sessionId": sid}),
        )

    changed_files: list[str] = []
    untracked_files: list[str] = []
    patch_name: str | None = None
    baseline_untracked = set(_git_status(workdir)[1])
    while True:
        http_was_attempted = False
        fallback_from = None
        http_fallback_error: dict[str, Any] | None = None
        if chosen == "cli":
            outcome = run_cli()
        elif chosen == "http":
            http_was_attempted = True
            _update_job({"httpAttempted": True})
            o1 = run_http()
            if o1.ok:
                outcome = o1
            elif requested_engine == "auto" and (not o1.timed_out):
                fallback_from = "http"
                http_fallback_error = o1.error if hasattr(o1, "error") else None
                outcome = run_cli()
                outcome.fallback_from = "http"  # type: ignore[attr-defined]
            else:
                outcome = o1
        else:
            # auto: prefer http if we have a server URL; otherwise cli.
            if attach_url:
                http_was_attempted = True
                _update_job({"httpAttempted": True})
                o1 = run_http()
                if o1.ok:
                    outcome = o1
                elif o1.timed_out:
                    outcome = o1
                else:
                    fallback_from = "http"
                    http_fallback_error = o1.error if hasattr(o1, "error") else None
                    outcome = run_cli()
                    outcome.fallback_from = "http"  # type: ignore[attr-defined]
            else:
                outcome = run_cli()

        assert outcome is not None

        changed_files, untracked_files = _git_status(workdir)
        patch_name = _git_patch(workdir, artifacts_dir)

        assistant_text = (outcome.full_text or "").strip()
        empty_now = _is_empty_model_output(outcome, assistant_text)
        if empty_now:
            empty_output_detected = True
        new_untracked = set(untracked_files) - baseline_untracked
        can_retry_empty = bool(
            retry_empty_output
            and empty_now
            and (empty_output_attempts < max_empty_output_retries)
            and (len(changed_files) == 0)
            and (len(new_untracked) == 0)
            and (patch_name is None)
        )
        if can_retry_empty:
            empty_output_retried = True
            empty_output_attempts += 1
            retries_left = max(0, max_empty_output_retries - empty_output_attempts)
            sys.stderr.write(
                f"[opencode-subtask] WARN: empty model output detected; retrying ({empty_output_attempts}/{max_empty_output_retries}, left={retries_left}).\n"
            )
            continue
        if empty_output_retried and (not empty_now):
            empty_output_recovered = True
        if empty_now:
            msg = f"model completed with empty output after {empty_output_attempts + 1} attempt(s)"
            outcome.error = {"name": "EmptyModelOutput", "message": msg}
            if outcome.exit_code == 0:
                outcome.exit_code = 1
            outcome.ok = False
        break

    execution_error = outcome.error
    outcome_name = _execution_outcome_from_error(
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        error=execution_error,
    )
    execution_warnings: list[dict[str, str]] = list(runtime_preflight_warnings)
    if auto_http_skip_warning:
        execution_warnings.append(auto_http_skip_warning)
    if fallback_from:
        execution_warnings.append(
            _warning(
                "EngineFallback",
                f"execution fell back from {fallback_from} to {outcome.engine}",
            )
        )
    if empty_output_recovered:
        execution_warnings.append(
            _warning(
                "EmptyOutputRecovered",
                f"empty model output recovered after {empty_output_attempts} retry attempt(s)",
            )
        )
    if empty_output_detected and not empty_output_recovered and outcome.error:
        execution_warnings.append(
            _warning(
                "EmptyOutputDetected",
                "model completed without assistant output or workspace changes",
            )
        )

    out = _finish_obj(
        run_id=run_id,
        workdir=workdir,
        outcome=outcome_name,
        exit_code=outcome.exit_code,
        duration_ms=max(0, _now_ms() - run_started_ms),
        engine_selected=outcome.engine,
        fallback_from=fallback_from,
        session_id=outcome.session_id,
        execution_error=execution_error,
        execution_warnings=execution_warnings,
        changed_files=changed_files,
        untracked_files=untracked_files,
        patch_path=patch_name,
        artifacts=_artifacts_obj(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
            prompt_path=prompt_path,
            events_path=events_path,
            stderr_path=stderr_path,
            assistant_path=assistant_path,
            wrapper_log_path=wrapper_log_path,
            progress_path=task_res.progress_path,
            notification_path=artifacts_dir / "notification.json",
        ),
        subtask=subtask_info,
    )

    finish_written, finish_reason, existing_finish = _write_finish_once(
        artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
    )
    if (not finish_written) and isinstance(existing_finish, dict):
        out = existing_finish
    elif (not finish_written) and existing_finish is None:
        err = _error_obj(
            error_name="FinishWriteFailed",
            message=f"finish.json could not be persisted (reason={finish_reason})",
        )
        sys.stdout.write(_json_line(err) + "\n")
        return 1
    # Update job state only if this worker won the terminal finish write.
    if finish_written:
        fields: dict[str, Any] = {
            "state": "finished",
            "outcome": outcome_name,
            "serverStartedNew": server_started_new,
            "httpAttempted": http_was_attempted,
            "stopServerAfterRunMode": stop_server_mode,
            "emptyOutputDetected": empty_output_detected,
            "emptyOutputRetried": empty_output_retried,
            "emptyOutputRecovered": empty_output_recovered,
        }
        if outcome.session_id:
            fields["sessionId"] = outcome.session_id
        if attach_url:
            fields["serverUrl"] = attach_url
        _update_job_fields_locked(job_path, artifacts_dir, fields)

    notification_path = _write_notification(
        artifacts_dir=artifacts_dir,
        finish_obj=out,
        task_state_path=task_res.state_path,
    )
    _append_task_progress(
        task_res.progress_path,
        event="finished",
        fields={
            "runId": run_id,
            "sessionId": outcome.session_id,
            "outcome": outcome_name,
            "changedFiles": changed_files,
            "untrackedFiles": untracked_files,
            "patchPath": patch_name,
            "notificationPath": str(notification_path) if notification_path else None,
        },
    )
    _write_task_state_locked(
        workdir=workdir,
        task_id=task_res.task_id,
        update={
            "state": "finished",
            "outcome": outcome_name,
            "latestRunId": run_id,
            "latestArtifactsDir": str(artifacts_dir),
            "sessionId": outcome.session_id or seed_session_id,
            "changedFiles": changed_files,
            "untrackedFiles": untracked_files,
            "patchPath": patch_name,
            "notificationPath": str(notification_path) if notification_path else None,
            "finishPath": str(finish_path),
            "assistantPath": str(assistant_path) if assistant_path else None,
            "subtask": subtask_info,
        },
    )

    # Optional: stop the per-project server after completion.
    should_stop_server = False
    if http_was_attempted:
        attached_server_url = (
            str(server_state.get("url"))
            if isinstance(server_state, dict) and server_state.get("url")
            else None
        )
        if (not attached_server_url) and attach_url:
            # Support explicit --attach local per-project server URLs where
            # server_state may be unset.
            st_local = _load_json(_server_state_path(workdir)) or {}
            if isinstance(st_local, dict) and st_local.get("url"):
                attached_server_url = str(st_local.get("url"))
        # Safety gate: only stop the exact local per-project server selected
        # for this invocation. This avoids affecting unrelated/remote attach targets.
        is_local_selected_server = bool(
            attach_url
            and attached_server_url
            and str(attach_url) == str(attached_server_url)
        )
        if stop_server_mode == "always":
            should_stop_server = is_local_selected_server
        elif stop_server_mode == "if-started":
            # Safety gate: only stop the exact local server this invocation started.
            should_stop_server = bool(server_started_new and is_local_selected_server)
        elif stop_server_mode == "never":
            should_stop_server = False
        else:
            _exit_with_error(
                "BadConfig",
                "Invalid --stop-server-after-run mode. Expected one of: if-started, always, never.",
                exit_code=2,
            )

    if should_stop_server:
        try:
            st = stop_server(workdir)
            sys.stderr.write(
                f"[opencode-subtask] NOTE: stop-server-after-run({stop_server_mode}): ok={st.get('ok')} pid={st.get('pid')}\n"
            )
        except Exception as e:
            sys.stderr.write(
                f"[opencode-subtask] WARN: stop-server-after-run({stop_server_mode}) failed: {type(e).__name__}: {e}\n"
            )

    sys.stdout.write(_json_line(out) + "\n")
    return _outcome_exit_code(str(out.get("outcome") or outcome_name))


def cmd_start(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    task_res = _resolve_task_id_for_run(args, workdir)
    setattr(args, "_adapter_task_id", task_res.task_id)
    if _task_is_running(task_res.prior_state) and str(getattr(args, "on_running_task", "fail")) != "start-new":
        obj = _error_obj(
            error_name="TaskAlreadyRunning",
            message=(
                f"task_id {task_res.task_id!r} already has a running OpenCode child. "
                "Use status/wait/watch with --task-id, or pass --on-running-task start-new to override."
            ),
        )
        obj["task"] = task_res.prior_state
        sys.stdout.write(_json_line(obj) + "\n")
        return 2
    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    finish_path = artifacts_dir / "finish.json"
    runtime_preflight_warnings: list[dict[str, str]] = []
    existing_finish, finish_warnings = _load_runtime_finish_envelope(finish_path)
    runtime_preflight_warnings = _dedupe_warnings(
        [*runtime_preflight_warnings, *finish_warnings]
    )
    if isinstance(existing_finish, dict):
        err = _error_obj(
            error_name="FinishAlreadyPresent",
            message=_finish_already_present_message(finish_path),
            warnings=runtime_preflight_warnings,
        )
        sys.stdout.write(_json_line(err) + "\n")
        return 1

    # Prompt
    prompt = _resolve_prompt_input(args)
    if bool(getattr(args, "task_memory", True)):
        prompt = prompt.rstrip() + _task_memory_block(
            task_res,
            max_chars=int(getattr(args, "task_memory_max_chars", DEFAULT_TASK_MEMORY_MAX_CHARS)),
        )
    subtask_info = _apply_subtask_preset(args)
    subtask_info["taskStatePath"] = str(task_res.state_path)
    subtask_info["taskProgressPath"] = str(task_res.progress_path)
    subtask_info["continued"] = bool(task_res.prior_state)

    prompt = _apply_persona_policy(prompt, args.persona_mode, args.persona_line)
    prompt = _apply_subagent_briefing(
        prompt,
        enabled=bool(getattr(args, "subagent_briefing", False)),
        final_answer_budget_chars=int(
            getattr(args, "final_answer_budget_chars", DEFAULT_FINAL_ANSWER_BUDGET_CHARS)
        ),
    )
    prompt = _apply_subtask_briefing(prompt, subtask_info=subtask_info)

    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    job_path = artifacts_dir / "job.json"
    wrapper_log_path = artifacts_dir / "wrapper.log"
    seed_session_id = str(getattr(args, "session", "") or "").strip() or None
    memory_recovery = _opencode_memory_recovery_hint(
        enabled=bool(getattr(args, "memory_friendly", False)),
        workdir=workdir,
        artifacts_dir=artifacts_dir,
        run_id=run_id,
        session_id=seed_session_id,
    )

    _write_job_locked(
        job_path,
        artifacts_dir,
        {
            "runId": run_id,
            "taskId": task_res.task_id,
            "adapterVersion": ADAPTER_VERSION,
            "workdir": str(workdir),
            "state": "queued",
            "stopServerAfterRunMode": str(
                getattr(args, "stop_server_after_run", DEFAULT_STOP_SERVER_AFTER_RUN)
            ),
            "serverStartedNew": False,
            "orphanReaperEnabled": bool(getattr(args, "orphan_reaper", True)),
            "sessionId": seed_session_id,
            "memoryFriendly": bool(getattr(args, "memory_friendly", False)),
            "memoryRecovery": memory_recovery,
            "subtask": subtask_info,
            "createdAt": _now_ms(),
            "updatedAt": _now_ms(),
        },
    )
    _write_task_state_locked(
        workdir=workdir,
        task_id=task_res.task_id,
        update={
            "state": "running",
            "latestRunId": run_id,
            "latestArtifactsDir": str(artifacts_dir),
            "sessionId": seed_session_id,
            "subtask": subtask_info,
            "agent": getattr(args, "agent", None),
            "model": getattr(args, "model", None),
            "variant": getattr(args, "variant", None),
            "title": getattr(args, "title", None),
        },
    )
    _append_task_progress(
        task_res.progress_path,
        event="started-background",
        fields={
            "runId": run_id,
            "sessionId": seed_session_id,
            "subtaskType": subtask_info.get("type"),
            "agent": getattr(args, "agent", None),
            "engine": getattr(args, "engine", None),
            "artifactsDir": str(artifacts_dir),
        },
    )
    _write_text(wrapper_log_path, "")

    opencode_arg = _resolve_executable_for_workdir(str(args.opencode), workdir) or str(
        args.opencode
    )
    run_timeout_s = float(args.run_timeout)

    # Apply execution profile so that worker_cmd flags (--engine,
    # --save-events, --save-text) and the start stdout metadata match what
    # the worker will actually compute in cmd_run. The function mutates
    # args in place and is idempotent, so the worker calling it again is
    # harmless.
    env = _safe_merge_env(dict(os.environ), args.env, args.env_file)
    _apply_execution_profile(args, prompt, env)
    subtask_info["executionProfile"] = getattr(args, "execution_profile", None)

    # Build worker command (explicitly pass run_id/artifacts_dir/opencode path and flags).
    py = sys.executable
    worker_cmd: list[str] = [
        py,
        str(Path(__file__).resolve()),
        "run",
        "--workdir",
        str(workdir),
        "--run-id",
        run_id,
        "--artifacts-dir",
        str(artifacts_dir),
        "--opencode",
        opencode_arg,
        "--engine",
        args.engine,
        "--run-timeout",
        str(run_timeout_s),
        "--max-artifact-bytes",
        str(args.max_artifact_bytes),
        "--permission-mode",
        args.permission_mode,
        "--permission-approval",
        args.permission_approval,
        "--execution-profile",
        str(args.execution_profile),
        "--subtask-type",
        str(getattr(args, "subtask_type", DEFAULT_SUBTASK_TYPE)),
        "--task-id",
        task_res.task_id,
        "--on-running-task",
        "start-new",
        "--no-task-memory",
        "--final-answer-budget-chars",
        str(getattr(args, "final_answer_budget_chars", DEFAULT_FINAL_ANSWER_BUDGET_CHARS)),
        "--cli-prompt-transport",
        str(getattr(args, "cli_prompt_transport", DEFAULT_CLI_PROMPT_TRANSPORT)),
        "--stop-server-after-run",
        str(args.stop_server_after_run),
        "--orphan-reaper-idle-s",
        str(args.orphan_reaper_idle_s),
    ]

    # booleans
    worker_cmd.append("--quiet" if args.quiet else "--no-quiet")
    worker_cmd.append("--save-events" if args.save_events else "--no-save-events")
    worker_cmd.append("--save-text" if args.save_text else "--no-save-text")
    worker_cmd.append(
        "--memory-friendly" if args.memory_friendly else "--no-memory-friendly"
    )
    worker_cmd.append("--orphan-reaper" if args.orphan_reaper else "--no-orphan-reaper")
    worker_cmd.append(
        "--disable-claude-code"
        if args.disable_claude_code
        else "--no-disable-claude-code"
    )
    worker_cmd.append(
        "--subagent-briefing"
        if getattr(args, "subagent_briefing", False)
        else "--no-subagent-briefing"
    )
    http_deny_interactive_arg = getattr(args, "http_deny_interactive_tools", None)
    if http_deny_interactive_arg is not None:
        worker_cmd.append(
            "--http-deny-interactive-tools"
            if http_deny_interactive_arg
            else "--no-http-deny-interactive-tools"
        )
    worker_cmd.append(
        "--allow-nested-subtasks"
        if getattr(args, "allow_nested_subtasks", False)
        else "--no-allow-nested-subtasks"
    )
    worker_cmd.append(
        "--allow-child-todos"
        if getattr(args, "allow_child_todos", False)
        else "--no-allow-child-todos"
    )

    # prompt persona policy (keep worker behavior consistent with start/run)
    worker_cmd.extend(
        [
            "--persona-mode",
            str(args.persona_mode),
            "--persona-line",
            str(args.persona_line),
        ]
    )
    if args.workaround_agent_attach:
        worker_cmd.append("--workaround-agent-attach")
    else:
        worker_cmd.append("--no-workaround-agent-attach")
    worker_cmd.append(
        "--retry-empty-output" if args.retry_empty_output else "--no-retry-empty-output"
    )
    worker_cmd.extend(["--empty-output-retries", str(args.empty_output_retries)])

    # attach settings
    if args.attach:
        worker_cmd.extend(["--attach", args.attach])
    if args.attach_server:
        worker_cmd.append("--attach-server")
    else:
        worker_cmd.append("--no-attach-server")
    if getattr(args, "hybrid_short_timeout_s", None) is not None:
        worker_cmd.extend(
            ["--hybrid-short-timeout-s", str(getattr(args, "hybrid_short_timeout_s"))]
        )
    if getattr(args, "hybrid_short_prompt_chars", None) is not None:
        worker_cmd.extend(
            [
                "--hybrid-short-prompt-chars",
                str(getattr(args, "hybrid_short_prompt_chars")),
            ]
        )
    worker_cmd.extend(
        [
            "--server-hostname",
            args.server_hostname,
            "--server-port",
            str(args.server_port),
            "--server-wait",
            str(args.server_wait),
        ]
    )
    for origin in getattr(args, "server_cors", []) or []:
        worker_cmd.extend(["--server-cors", str(origin)])

    # passthrough
    if getattr(args, "continue_last", False):
        worker_cmd.append("--continue")
    if getattr(args, "session", None):
        worker_cmd.extend(["--session", str(getattr(args, "session"))])
    if getattr(args, "title", None):
        worker_cmd.extend(["--title", str(getattr(args, "title"))])
    if getattr(args, "description", None):
        worker_cmd.extend(["--description", str(getattr(args, "description"))])
    if args.agent:
        worker_cmd.extend(["--agent", args.agent])
    if args.model:
        worker_cmd.extend(["--model", args.model])
    if getattr(args, "variant", None):
        worker_cmd.extend(["--variant", str(getattr(args, "variant"))])
    for f in args.file:
        worker_cmd.extend(["--file", f])
    for ev in args.env:
        worker_cmd.extend(["--env", ev])
    for evf in args.env_file:
        worker_cmd.extend(["--env-file", evf])
    for item in getattr(args, "acceptance", []) or []:
        worker_cmd.extend(["--acceptance", str(item)])

    # prompt
    worker_cmd.extend(["--prompt-file", str(prompt_path)])

    log_fp = open(wrapper_log_path, "ab", buffering=0)
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs.update(_win_hide_popen_kwargs(detached=True))
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        worker_cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        cwd=str(workdir),
        **popen_kwargs,
    )

    # update job
    _update_job_fields_locked(
        job_path,
        artifacts_dir,
        {
            "state": "running",
            "pid": proc.pid,
        },
    )
    _write_task_state_locked(
        workdir=workdir,
        task_id=task_res.task_id,
        update={"state": "running", "pid": proc.pid, "latestRunId": run_id, "latestArtifactsDir": str(artifacts_dir)},
    )

    out = _start_obj(
        run_id=run_id,
        pid=proc.pid,
        workdir=workdir,
        artifacts_dir=artifacts_dir,
        memory_recovery=memory_recovery,
        warnings=runtime_preflight_warnings,
        subtask=subtask_info,
        task_id=task_res.task_id,
        artifacts=_artifacts_obj(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
            prompt_path=prompt_path,
            events_path=(artifacts_dir / "events.ndjson") if args.save_events else None,
            stderr_path=(artifacts_dir / "stderr.log"),
            assistant_path=(artifacts_dir / "assistant.txt")
            if args.save_text
            else None,
            wrapper_log_path=wrapper_log_path,
            progress_path=task_res.progress_path,
        ),
    )
    sys.stdout.write(_json_line(out) + "\n")
    return 0


def _tail_text_lines(path: Path, *, max_bytes: int = 65536) -> list[str]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fp:
            if size > max_bytes:
                fp.seek(max(0, size - max_bytes))
                # discard partial first line
                fp.readline()
            data = fp.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def _progress_summary_from_events(
    *, events_path: Path, assistant_path: Path, finish_path: Path
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "phase": "completed_like" if finish_path.exists() else "initializing",
        "lastEventType": None,
        "lastTool": None,
        "lastToolStatus": None,
        "source": None,
    }

    test_markers = (
        "pytest", "unittest", "npm test", "pnpm test", "yarn test",
        "cargo test", "go test", "mvn test", "gradle test", "ctest",
        "jest", "vitest", "ruff", "mypy", "tsc",
    )
    edit_markers = ("edit", "write", "patch", "apply_patch", "multiedit")

    def set_phase(phase: str, *, source: str) -> None:
        if summary.get("phase") != "completed_like":
            summary["phase"] = phase
            summary["source"] = source

    def str_from_part(part: dict[str, Any], keys: tuple[str, ...]) -> str:
        for k in keys:
            v = part.get(k)
            if isinstance(v, str) and v:
                return v
        return ""

    for ln in _tail_text_lines(events_path):
        try:
            raw = json.loads(ln)
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        evt = _event_payload(raw)
        if not isinstance(evt, dict):
            continue
        et = evt.get("type")
        if isinstance(et, str):
            summary["lastEventType"] = et
        props = _event_properties(evt)

        if et == "permission.asked":
            set_phase("waiting_permission", source="event")
            continue
        if isinstance(et, str) and et.endswith(".error"):
            set_phase("error", source="event")
            continue
        if et == "session.status":
            status = props.get("status") or evt.get("status")
            if isinstance(status, dict):
                status = status.get("type") or status.get("status")
            if isinstance(status, str):
                st = status.lower()
                if st in {"idle", "ready"}:
                    set_phase("completed_like", source="session.status")
                elif st in {"running", "busy"}:
                    set_phase("thinking", source="session.status")
            continue

        part = _first_nested_dict(evt, "part")
        if not isinstance(part, dict):
            # Some events expose tool-ish information directly under properties.
            part = props if isinstance(props, dict) else {}
        part_type = str(part.get("type") or "").lower()
        if et == "message.updated":
            set_phase("thinking", source="message.updated")
        if et == "message.part.updated" or part_type:
            if part_type in {"reasoning", "reasoning_text", "thinking"}:
                set_phase("thinking", source="part")
                continue
            if part_type in {"text", "assistant_text", "output_text"}:
                set_phase("answering", source="part")
                continue
            tool = str_from_part(part, ("tool", "name", "toolName", "id"))
            if tool:
                summary["lastTool"] = tool
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = state.get("status") if isinstance(state, dict) else part.get("status")
            if isinstance(status, str):
                summary["lastToolStatus"] = status
            haystack = " ".join(
                str(v).lower()
                for v in [
                    tool,
                    part.get("command"),
                    part.get("title"),
                    part.get("description"),
                    state.get("command") if isinstance(state, dict) else None,
                ]
                if v is not None
            )
            if any(m in haystack for m in test_markers):
                set_phase("testing", source="tool")
            elif any(m in haystack for m in edit_markers):
                set_phase("editing", source="tool")
            elif tool or part_type in {"tool", "tool_call"}:
                set_phase("tooling", source="tool")

    if summary.get("phase") == "initializing" and assistant_path.exists():
        try:
            if assistant_path.stat().st_size > 0:
                summary["phase"] = "answering"
                summary["source"] = "assistant"
        except Exception:
            pass
    return summary


def _progress_snapshot(artifacts_dir: Path) -> dict[str, Any]:
    files = {
        "events": artifacts_dir / "events.ndjson",
        "assistant": artifacts_dir / "assistant.txt",
        "stderr": artifacts_dir / "stderr.log",
        "wrapper": artifacts_dir / "wrapper.log",
        "finish": artifacts_dir / "finish.json",
    }
    now = time.time()
    prog: dict[str, Any] = {"files": {}, "idleForSeconds": None}
    last_mtime = 0.0
    for k, p in files.items():
        if not p.exists():
            continue
        try:
            st = p.stat()
            prog["files"][k] = {"bytes": st.st_size, "mtime": st.st_mtime}
            last_mtime = max(last_mtime, st.st_mtime)
        except Exception:
            continue
    if last_mtime > 0:
        prog["idleForSeconds"] = max(0.0, now - last_mtime)
    prog["summary"] = _progress_summary_from_events(
        events_path=files["events"],
        assistant_path=files["assistant"],
        finish_path=files["finish"],
    )
    return prog


def _job_ms(job: dict[str, Any], key: str) -> int:
    raw = job.get(key)
    if isinstance(raw, (int, str)):
        try:
            return int(raw)
        except Exception:
            return 0
    return 0


def _emit_synthesized_missing_finish(
    *,
    run_id: str,
    artifacts_dir: Path,
    finish_path: Path,
    job_path: Path,
    job: dict[str, Any],
    error_name: str,
    error_message: str,
) -> dict[str, Any]:
    wd = (
        Path(str(job.get("workdir")))
        if isinstance(job, dict) and job.get("workdir")
        else Path.cwd()
    )
    created_ms = _job_ms(job, "createdAt") if isinstance(job, dict) else 0
    duration_ms = (
        max(0, _now_ms() - created_ms) if created_ms > 0 else 0
    )
    out = _finish_obj(
        run_id=run_id,
        workdir=wd,
        outcome="internal_error",
        exit_code=1,
        duration_ms=duration_ms,
        engine_selected="watchdog",
        fallback_from=None,
        session_id=(
            str(job.get("sessionId"))
            if isinstance(job, dict) and job.get("sessionId")
            else None
        ),
        execution_error={"name": error_name, "message": error_message},
        execution_warnings=[],
        changed_files=[],
        untracked_files=[],
        patch_path=None,
        artifacts=_minimal_artifacts(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
        ),
        subtask=job.get("subtask") if isinstance(job.get("subtask"), dict) else None,
    )
    finish_written, finish_reason, existing_finish = _write_finish_once(
        artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
    )
    if (not finish_written) and isinstance(existing_finish, dict):
        out = existing_finish
    task_id = str(job.get("taskId") or "").strip() if isinstance(job, dict) else ""
    if task_id and (finish_written or isinstance(existing_finish, dict)):
        note_path = _write_notification(
            artifacts_dir=artifacts_dir,
            finish_obj=out,
            task_state_path=_task_state_path(wd, task_id),
        )
        _append_task_progress(
            _task_progress_path(wd, task_id),
            event="watchdog-finished",
            fields={"runId": run_id, "outcome": "internal_error", "error": error_name},
        )
        _write_task_state_locked(
            workdir=wd,
            task_id=task_id,
            update={
                "state": "error",
                "outcome": "internal_error",
                "latestRunId": run_id,
                "latestArtifactsDir": str(artifacts_dir),
                "finishPath": str(finish_path),
                "notificationPath": str(note_path) if note_path else None,
                "lastError": {"name": error_name, "message": error_message},
            },
        )
    # Only update job state when we successfully persisted a terminal finish
    # (or an existing finish already covers it).
    if finish_written or isinstance(existing_finish, dict):
        _update_job_fields_locked(
            job_path,
            artifacts_dir,
            {
                "state": "failed",
                "failedAt": _now_ms(),
                "lastError": {"name": error_name, "message": error_message},
            },
        )
    else:
        # write_failed / unreadable — persist only the error, not the state
        # transition, to avoid a state=failed with no finish.json on disk.
        _update_job_fields_locked(
            job_path,
            artifacts_dir,
            {
                "lastError": {
                    "name": "FinishWriteFailed",
                    "message": (
                        f"watchdog finish.json write failed (reason={finish_reason})"
                    ),
                },
            },
        )
    return out


def _maybe_finalize_stale_running_job(
    *,
    run_id: str,
    artifacts_dir: Path,
    finish_path: Path,
    job_path: Path,
    job: dict[str, Any],
    pid: int | None,
    progress: dict[str, Any],
    stale_idle_s: float,
    dead_worker_grace_s: float,
    cancel_stuck_grace_s: float,
) -> dict[str, Any] | None:
    if finish_path.exists():
        return None
    # Defensive: canonicalize run_id from job in case callers forgot.
    run_id = _canonical_run_id(run_id, job)
    state = str(job.get("state") or "").strip().lower()
    if state and state not in ("running", "queued", "canceled", "cancelled", "failed"):
        return None
    idle_for = progress.get("idleForSeconds")
    idle_s = float(idle_for) if isinstance(idle_for, (int, float)) else None
    now_ms = _now_ms()

    last_touch_ms = max(
        _job_ms(job, "updatedAt"),
        _job_ms(job, "createdAt"),
        _job_ms(job, "cancelAttemptedAt"),
    )
    age_since_touch_s = (
        max(0.0, (float(now_ms) - float(last_touch_ms)) / 1000.0)
        if last_touch_ms > 0
        else 0.0
    )

    pid_alive = bool(pid and _pid_running(pid))
    if pid_alive and pid is not None:
        # Use the run_id recorded in job.json (the real worker's id), not
        # the caller-supplied run_id which may be a freshly generated one
        # when status/wait was invoked with only --artifacts-dir.
        job_run_id = job.get("runId") if isinstance(job, dict) else None
        ownership = _pid_subtask_worker_ownership_status(
            pid,
            job_run_id or run_id,
            require_run_id=False,
        )
        if ownership == "mismatch":
            pid_alive = False
    if not pid_alive:
        if age_since_touch_s < dead_worker_grace_s:
            return None
        return _emit_synthesized_missing_finish(
            run_id=run_id,
            artifacts_dir=artifacts_dir,
            finish_path=finish_path,
            job_path=job_path,
            job=job,
            error_name="WorkerNotRunning",
            error_message="worker process not running and finish.json missing",
        )

    cancel_attempted_ms = _job_ms(job, "cancelAttemptedAt")
    if cancel_attempted_ms <= 0:
        return None
    # If no progress files exist yet, use job touch age as a conservative
    # silence signal so canceled runs can still converge to a terminal finish.
    silence_s = idle_s if idle_s is not None else age_since_touch_s
    if silence_s < stale_idle_s:
        return None
    cancel_age_s = max(0.0, (float(now_ms) - float(cancel_attempted_ms)) / 1000.0)
    if cancel_age_s < cancel_stuck_grace_s:
        return None
    return _emit_synthesized_missing_finish(
        run_id=run_id,
        artifacts_dir=artifacts_dir,
        finish_path=finish_path,
        job_path=job_path,
        job=job,
        error_name="StuckAfterCancel",
        error_message="no heartbeat progress after cancel attempt and finish.json missing",
    )


def cmd_status(args: argparse.Namespace) -> int:
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None) and not getattr(args, "task_id", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id, --artifacts-dir, or --task-id", exit_code=2
        )

    workdir = Path(getattr(args, "workdir", ".") or ".").expanduser().resolve()
    run_id, artifacts_dir, task_state = _resolve_task_artifacts(
        workdir=workdir,
        task_id=getattr(args, "task_id", None),
        run_id=getattr(args, "run_id", None),
        artifacts_dir=getattr(args, "artifacts_dir", None),
    )
    task_id = str(task_state.get("taskId")) if isinstance(task_state, dict) and task_state.get("taskId") else None
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    prompt_path = artifacts_dir / "prompt.txt"

    # Canonicalize run_id: prefer the real runId recorded in job.json
    # over the potentially freshly-generated one from _resolve_artifacts_dir.
    _pre_job = _load_json(job_path)
    run_id = _canonical_run_id(run_id, _pre_job)
    runtime_warnings: list[dict[str, str]] = []

    fin, finish_warnings = _load_runtime_finish_envelope(finish_path)
    runtime_warnings = _dedupe_warnings([*runtime_warnings, *finish_warnings])
    if isinstance(fin, dict):
        sys.stdout.write(_json_line(fin) + "\n")
        # Exit 0: status successfully observed terminal state.
        return 0

    if not job_path.exists():
        out = _status_obj(
            ok=False,
            run_id=run_id,
            status="missing",
            pid=None,
            workdir=None,
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(
                dir_path=artifacts_dir,
                job_path=job_path,
                finish_path=finish_path,
                prompt_path=prompt_path,
                events_path=artifacts_dir / "events.ndjson",
                stderr_path=artifacts_dir / "stderr.log",
                assistant_path=artifacts_dir / "assistant.txt",
                wrapper_log_path=artifacts_dir / "wrapper.log",
            ),
            progress=_progress_snapshot(artifacts_dir),
            warnings=runtime_warnings or None,
            error={"name": "JobNotFound", "message": "job.json not found"},
            task_id=task_id,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    job = _load_json(job_path) or {}
    if (not task_id) and isinstance(job, dict) and job.get("taskId"):
        task_id = str(job.get("taskId"))
    pid = int(job.get("pid") or 0) if isinstance(job, dict) else 0
    status = str(job.get("state") or "running") if isinstance(job, dict) else "running"
    progress = _progress_snapshot(artifacts_dir)
    if isinstance(job, dict):
        synthesized = _maybe_finalize_stale_running_job(
            run_id=run_id,
            artifacts_dir=artifacts_dir,
            finish_path=finish_path,
            job_path=job_path,
            job=job,
            pid=pid if pid else None,
            progress=progress,
            stale_idle_s=DEFAULT_STALE_IDLE_S,
            dead_worker_grace_s=DEFAULT_DEAD_WORKER_GRACE_S,
            cancel_stuck_grace_s=DEFAULT_CANCEL_STUCK_GRACE_S,
        )
        if isinstance(synthesized, dict):
            sys.stdout.write(_json_line(synthesized) + "\n")
            return 0
    if (
        pid
        and not _pid_running(pid)
        and not finish_path.exists()
        and status != "finished"
    ):
        if status in ("running", "queued"):
            status = "failed"

    _worker_missing = pid and not _pid_running(pid) and not finish_path.exists()
    out = _status_obj(
        run_id=run_id,
        status=status,
        pid=pid if pid else None,
        workdir=str(job.get("workdir")) if isinstance(job, dict) else None,
        artifacts_dir=artifacts_dir,
        artifacts=_artifacts_obj(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
            prompt_path=prompt_path,
            events_path=artifacts_dir / "events.ndjson",
            stderr_path=artifacts_dir / "stderr.log",
            assistant_path=artifacts_dir / "assistant.txt",
            wrapper_log_path=artifacts_dir / "wrapper.log",
        ),
        progress=progress,
        warnings=(
            runtime_warnings
            + (
                [
                    {
                        "name": "WorkerMissingPending",
                        "message": "pid is not running; waiting stale-window before synthesizing finish",
                    }
                ]
                if _worker_missing
                else []
            )
        )
        or None,
        task_id=task_id,
    )
    sys.stdout.write(_json_line(out) + "\n")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None) and not getattr(args, "task_id", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id, --artifacts-dir, or --task-id", exit_code=2
        )

    workdir = Path(getattr(args, "workdir", ".") or ".").expanduser().resolve()
    run_id, artifacts_dir, task_state = _resolve_task_artifacts(
        workdir=workdir,
        task_id=getattr(args, "task_id", None),
        run_id=getattr(args, "run_id", None),
        artifacts_dir=getattr(args, "artifacts_dir", None),
    )
    task_id = str(task_state.get("taskId")) if isinstance(task_state, dict) and task_state.get("taskId") else None

    poll_interval = float(args.poll_interval)
    if poll_interval <= 0:
        _exit_with_error(
            "BadConfig",
            f"--poll-interval must be > 0, got: {poll_interval}",
            exit_code=2,
        )

    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"

    # Canonicalize run_id from job.json (see _canonical_run_id).
    _pre_job = _load_json(job_path)
    run_id = _canonical_run_id(run_id, _pre_job)
    runtime_warnings: list[dict[str, str]] = []

    fin, finish_warnings = _load_runtime_finish_envelope(finish_path)
    runtime_warnings = _dedupe_warnings([*runtime_warnings, *finish_warnings])
    if isinstance(fin, dict):
        sys.stdout.write(_json_line(fin) + "\n")
        return _outcome_exit_code(str(fin.get("outcome") or "internal_error"))

    if not job_path.exists() and fin is None:
        out = _status_obj(
            ok=False,
            run_id=run_id,
            status="missing",
            pid=None,
            workdir=None,
            artifacts_dir=artifacts_dir,
            artifacts=_minimal_artifacts(
                dir_path=artifacts_dir,
                job_path=job_path,
                finish_path=finish_path,
            ),
            warnings=runtime_warnings or None,
            error={
                "name": "JobNotFound",
                "message": "job.json and finish.json not found",
            },
            task_id=task_id,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    wait_timeout_s = float(args.wait_timeout)
    deadline = time.monotonic() + wait_timeout_s
    while True:
        fin, finish_warnings = _load_runtime_finish_envelope(finish_path)
        runtime_warnings = _dedupe_warnings([*runtime_warnings, *finish_warnings])
        if isinstance(fin, dict):
            sys.stdout.write(_json_line(_with_execution_warnings(fin, runtime_warnings)) + "\n")
            return _outcome_exit_code(str(fin.get("outcome") or "internal_error"))

        pid = None
        job = _load_json(job_path) or {}
        if (not task_id) and isinstance(job, dict) and job.get("taskId"):
            task_id = str(job.get("taskId"))
        if job_path.exists():
            if isinstance(job, dict) and job.get("pid"):
                try:
                    pid = int(job.get("pid"))
                except Exception:
                    pid = None
        progress = _progress_snapshot(artifacts_dir)
        if isinstance(job, dict):
            synthesized = _maybe_finalize_stale_running_job(
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                finish_path=finish_path,
                job_path=job_path,
                job=job,
                pid=pid,
                progress=progress,
                stale_idle_s=DEFAULT_STALE_IDLE_S,
                dead_worker_grace_s=DEFAULT_DEAD_WORKER_GRACE_S,
                cancel_stuck_grace_s=DEFAULT_CANCEL_STUCK_GRACE_S,
            )
            if isinstance(synthesized, dict):
                sys.stdout.write(_json_line(synthesized) + "\n")
                return _outcome_exit_code(
                    str(synthesized.get("outcome") or "internal_error")
                )

        if time.monotonic() >= deadline:
            out = _status_obj(
                ok=True,
                run_id=run_id,
                status="running",
                pid=pid,
                workdir=None,
                artifacts_dir=artifacts_dir,
                artifacts=_minimal_artifacts(
                    dir_path=artifacts_dir,
                    job_path=job_path,
                    finish_path=finish_path,
                ),
                progress=progress,
                warnings=runtime_warnings or None,
                wait_expired=True,
                task_id=task_id,
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 0

        time.sleep(poll_interval)


def cmd_cancel(args: argparse.Namespace) -> int:
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None) and not getattr(args, "task_id", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id, --artifacts-dir, or --task-id", exit_code=2
        )

    workdir = Path(getattr(args, "workdir", ".") or ".").expanduser().resolve()
    run_id, artifacts_dir, task_state = _resolve_task_artifacts(
        workdir=workdir,
        task_id=getattr(args, "task_id", None),
        run_id=getattr(args, "run_id", None),
        artifacts_dir=getattr(args, "artifacts_dir", None),
    )
    task_id = str(task_state.get("taskId")) if isinstance(task_state, dict) and task_state.get("taskId") else None
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"

    job = _load_json(job_path) or {}
    if (not task_id) and isinstance(job, dict) and job.get("taskId"):
        task_id = str(job.get("taskId"))
    run_id = _canonical_run_id(run_id, job)
    runtime_warnings: list[dict[str, str]] = []

    # ── Idempotency guard ─────────────────────────────────────────────
    # If a valid finish.json already exists the subtask has already
    # reached a terminal state.  Killing the (possibly recycled) PID or
    # overwriting job.state would be harmful.  Return early with
    # ``alreadyFinished=true`` so the caller knows cancel was a no-op.
    #
    # Require a recognizable finish envelope; otherwise continue cancel flow.
    existing_finish, finish_warnings = _load_runtime_finish_envelope(finish_path)
    runtime_warnings = _dedupe_warnings([*runtime_warnings, *finish_warnings])
    if isinstance(existing_finish, dict):
        out_idem = {
            "type": "opencode-subtask-cancel",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "timestamp": _now_ms(),
            "runId": run_id,
            "taskId": task_id,
            "ok": True,
            "warnings": runtime_warnings,
            "alreadyFinished": True,
            "existingFinish": existing_finish,
            "taskError": None,
        }
        sys.stdout.write(_json_line(out_idem) + "\n")
        return 0

    pid = int(job.get("pid") or 0) if isinstance(job, dict) else 0
    server_url = (
        str(job.get("serverUrl"))
        if isinstance(job, dict) and job.get("serverUrl")
        else None
    )
    session_id = (
        str(job.get("sessionId"))
        if isinstance(job, dict) and job.get("sessionId")
        else None
    )
    http_attempted = bool(job.get("httpAttempted")) if isinstance(job, dict) else False
    stop_server_mode = (
        str(job.get("stopServerAfterRunMode", DEFAULT_STOP_SERVER_AFTER_RUN))
        .strip()
        .lower()
        if isinstance(job, dict)
        else DEFAULT_STOP_SERVER_AFTER_RUN
    )
    server_started_new = (
        bool(job.get("serverStartedNew")) if isinstance(job, dict) else False
    )
    stop_attempted = False
    stop_ok: bool | None = None
    # run_id is already canonical (set via _canonical_run_id above).
    job_run_id = run_id

    kill_attempted = False
    kill_signal_delivered = False
    probe_inconclusive_after_kill = False
    termination_confirmed = False
    termination_evidence = "unknown"
    worker_ownership = "not_checked"
    allow_unknown_kill = str(
        os.environ.get("OPENCODE_SUBTASK_CANCEL_ALLOW_UNKNOWN_KILL", "")
    ).strip().lower() in ("1", "true", "yes", "on")
    if not allow_unknown_kill:
        allow_unknown_kill = DEFAULT_CANCEL_ALLOW_UNKNOWN_KILL
    cancel_phase = "none"
    if pid <= 0:
        termination_evidence = "no_pid"
        worker_ownership = "no_pid"
    else:
        alive_before, known_before = _pid_running_state(pid)
        if known_before and not alive_before:
            termination_confirmed = True
            termination_evidence = "pid_dead"
            worker_ownership = "dead"
        else:
            worker_ownership = _pid_subtask_worker_ownership_status(
                pid, job_run_id, require_run_id=False
            )
            if worker_ownership == "verified" or (
                worker_ownership == "unknown" and allow_unknown_kill
            ):
                kill_attempted = True
                cancel_phase = "term"
                term_sent = _kill_tree(
                    pid, sig=(signal.SIGTERM if os.name != "nt" else None)
                )
                kill_signal_delivered = term_sent
                if term_sent and _wait_for_pid_dead(pid, DEFAULT_CANCEL_TERM_GRACE_S):
                    termination_confirmed = True
                    termination_evidence = "pid_dead"
                else:
                    cancel_phase = "kill"
                    kill_sent = _kill_tree(
                        pid, sig=(signal.SIGKILL if os.name != "nt" else None)
                    )
                    kill_signal_delivered = kill_signal_delivered or kill_sent
                    if kill_sent and _wait_for_pid_dead(
                        pid, DEFAULT_CANCEL_KILL_GRACE_S
                    ):
                        termination_confirmed = True
                        termination_evidence = "pid_dead"
                if kill_attempted and (not termination_confirmed):
                    alive_after, known_after = _pid_running_state(pid)
                    if known_after:
                        if not alive_after:
                            termination_confirmed = True
                            termination_evidence = "pid_dead"
                    else:
                        probe_inconclusive_after_kill = True
                        if termination_evidence == "unknown":
                            termination_evidence = "probe_unknown_after_kill"
            else:
                if worker_ownership == "unknown":
                    termination_evidence = "owner_unknown_no_kill"
                else:
                    termination_evidence = "owner_mismatch"

    # Best-effort abort session if recorded.
    abort_ok = False
    abort_error: str | None = None
    if server_url and session_id:
        env = _safe_merge_env(
            os.environ, set_vars=args.env, set_from_files=args.env_file
        )
        auth = _get_http_auth_from_env(env)
        try:
            OpencodeHttpClient(server_url, auth=auth, timeout_s=5.0).abort_checked(
                session_id, timeout_s=5.0
            )
            abort_ok = True
        except Exception as e:
            abort_error = f"{type(e).__name__}: {e}"
            abort_error = " ".join(str(abort_error).split())
            if len(abort_error) > 500:
                abort_error = abort_error[:497] + "..."
    if pid > 0:
        # Treat either local worker termination OR successful remote session abort
        # as a successful cancel outcome.
        ok = bool(
            termination_confirmed
            or abort_ok
            or (
                kill_attempted
                and kill_signal_delivered
                and probe_inconclusive_after_kill
            )
        )
    else:
        ok = bool(abort_ok)
    cancel_unverified = bool(
        ok
        and (not termination_confirmed)
        and (not abort_ok)
        and kill_attempted
        and kill_signal_delivered
        and probe_inconclusive_after_kill
    )

    # If worker was canceled before normal completion, best-effort apply
    # stop-server-after-run policy to avoid orphaning a freshly-started server.
    should_stop_server = False
    wd = (
        Path(str(job.get("workdir")))
        if isinstance(job, dict) and job.get("workdir")
        else Path.cwd()
    )
    if http_attempted or server_started_new:
        st_local = _load_json(_server_state_path(wd)) or {}
        local_server_url = (
            str(st_local.get("url"))
            if isinstance(st_local, dict) and st_local.get("url")
            else None
        )
        # Safety gate: only stop the currently tracked local per-project server.
        # For the "started but not yet attempted HTTP" timing window, serverUrl
        # may still be missing in job state; allow local match via startedNew.
        is_local_selected_server = bool(
            local_server_url
            and (
                (server_url and str(server_url) == str(local_server_url))
                or (server_started_new and not server_url)
            )
        )
        if stop_server_mode == "always":
            should_stop_server = bool(
                is_local_selected_server and (http_attempted or server_started_new)
            )
        elif stop_server_mode == "if-started":
            should_stop_server = bool(server_started_new and is_local_selected_server)
        elif stop_server_mode == "never":
            should_stop_server = False

    if should_stop_server:
        stop_attempted = True
        try:
            st = stop_server(wd)
            stop_ok = bool(st.get("ok")) if isinstance(st, dict) else False
        except Exception:
            stop_ok = False

    # Write terminal cancel finish when cancel is confirmed or there is no local
    # worker signal path left.
    cancel_fin_written = False
    cancel_fin_reason = "skipped"
    # task_error: describes why the task ended (written to finish.json,
    # surfaced as additive ``taskError`` in stdout).
    # cancel_error: describes cancel-command failure (stdout ``error``,
    # only when ok=false).  Enforces invariant: ok=true => error=null.
    task_error: dict | None = None
    cancel_error: dict | None = None
    no_signal_path = bool((pid <= 0) or termination_confirmed)
    if ok or no_signal_path:
        if ok:
            if cancel_unverified:
                task_error = {
                    "name": "Canceled",
                    "message": "cancel signal delivered; termination not confirmed (liveness probe inconclusive)",
                }
            else:
                task_error = {
                    "name": "Canceled",
                    "message": "job canceled by adapter",
                }
        elif abort_error:
            task_error = {
                "name": "CancelAbortFailed",
                "message": f"worker not running; session abort failed: {abort_error}",
            }
        else:
            task_error = {
                "name": "CancelNoActiveWorker",
                "message": "cancel requested but no active worker process remained",
            }
        out = _finish_obj(
            run_id=run_id,
            workdir=Path(str(job.get("workdir")))
            if isinstance(job, dict) and job.get("workdir")
            else Path.cwd(),
            outcome="cancelled" if ok else "internal_error",
            exit_code=130 if ok else 1,
            duration_ms=(
                max(0, _now_ms() - _job_ms(job, "createdAt"))
                if _job_ms(job, "createdAt") > 0
                else 0
            ),
            engine_selected="cancel",
            fallback_from=None,
            session_id=session_id,
            execution_error=task_error,
            execution_warnings=[],
            changed_files=[],
            untracked_files=[],
            patch_path=None,
            artifacts=_minimal_artifacts(
                dir_path=artifacts_dir,
                job_path=job_path,
                finish_path=finish_path,
            ),
            subtask=job.get("subtask") if isinstance(job.get("subtask"), dict) else None,
        )
        cancel_fin_written, cancel_fin_reason, _ = _write_finish_once(
            artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
        )
        if task_id:
            notification_path = None
            if cancel_fin_written:
                notification_path = _write_notification(
                    artifacts_dir=artifacts_dir,
                    finish_obj=out,
                    task_state_path=_task_state_path(wd, task_id),
                )
            _append_task_progress(
                _task_progress_path(wd, task_id),
                event="cancelled" if ok else "cancel-failed",
                fields={
                    "runId": run_id,
                    "sessionId": session_id,
                    "outcome": "cancelled" if ok else "internal_error",
                    "terminationEvidence": termination_evidence,
                    "workerOwnership": worker_ownership,
                    "finishWritten": cancel_fin_written,
                },
            )
            _write_task_state_locked(
                workdir=wd,
                task_id=task_id,
                update={
                    "state": "cancelled" if ok else "error",
                    "outcome": "cancelled" if ok else "internal_error",
                    "latestRunId": run_id,
                    "sessionId": session_id,
                    "latestArtifactsDir": str(artifacts_dir),
                    "finishPath": str(finish_path) if cancel_fin_written else None,
                    "notificationPath": str(notification_path) if notification_path else None,
                    "cancelledAt": _now_ms(),
                },
            )
        # If finish.json could not be persisted AND no prior finish exists,
        # downstream wait/status will never see a terminal state.  Degrade
        # the cancel result so the caller knows the kill may have worked but
        # the on-disk record is missing.
        if (not cancel_fin_written) and cancel_fin_reason in (
            "write_failed",
            "unreadable",
        ):
            ok = False
            cancel_error = {
                "name": "CancelFinishWriteFailed",
                "message": (
                    f"cancel finish.json could not be persisted "
                    f"(reason={cancel_fin_reason}); "
                    f"wait/status may not see a terminal state"
                ),
            }

        # For failed cancels where no explicit command-error was set,
        # promote the task_error to cancel_error so stdout ``error``
        # stays populated on ok=false.
        if not ok and cancel_error is None and task_error is not None:
            cancel_error = task_error

    # If cancel did not confirm termination and we did not write a terminal
    # finish.json, ensure stdout still includes a cancel-command error.
    if not ok and cancel_error is None:
        if worker_ownership == "mismatch":
            msg = (
                "refusing to kill pid due to ownership mismatch "
                f"(pid={pid}, workerOwnership={worker_ownership})"
            )
            if abort_error:
                msg += f"; abort_error={abort_error}"
            cancel_error = {"name": "CancelOwnershipMismatch", "message": msg}
        elif worker_ownership == "unknown" and (not allow_unknown_kill):
            msg = (
                "refusing to kill pid due to unknown ownership "
                f"(pid={pid}, workerOwnership={worker_ownership}, "
                f"allowUnknownOwnershipKill={allow_unknown_kill})"
            )
            if abort_error:
                msg += f"; abort_error={abort_error}"
            cancel_error = {"name": "CancelOwnershipUnknown", "message": msg}
        elif kill_attempted and (not kill_signal_delivered):
            cancel_error = {
                "name": "CancelSignalFailed",
                "message": f"failed to deliver termination signal(s) (pid={pid})",
            }
        else:
            msg = (
                "cancel did not confirm worker termination and session abort did not succeed "
                f"(pid={pid}, terminationEvidence={termination_evidence}, "
                f"workerOwnership={worker_ownership})"
            )
            if abort_error:
                msg += f"; abort_error={abort_error}"
            cancel_error = {"name": "CancelFailed", "message": msg}

    # Persist cancel telemetry.
    if isinstance(job, dict):
        now_ms = _now_ms()
        fields: dict[str, Any] = {
            "cancelAttemptedAt": now_ms,
            "ok": ok,
            "stopServerAttempted": stop_attempted,
            "stopServerOk": stop_ok,
            "terminationConfirmed": termination_confirmed,
            "terminationEvidence": termination_evidence,
            "workerOwnership": worker_ownership,
            "allowUnknownOwnershipKill": allow_unknown_kill,
            "cancelPhase": cancel_phase,
            "killSignalDelivered": kill_signal_delivered,
            "probeInconclusiveAfterKill": probe_inconclusive_after_kill,
            "cancelUnverified": cancel_unverified,
            "cancelFinWritten": cancel_fin_written,
            "cancelFinReason": cancel_fin_reason,
        }
        # Only transition job state when *this* cancel actually won the
        # terminal finish write.  If _write_finish_once returned 'exists',
        # an earlier run/watchdog already finalised the job — overwriting
        # state would corrupt the record (e.g. success → canceled).
        if cancel_fin_written:
            if ok:
                fields["state"] = "canceled"
                fields["canceledAt"] = now_ms
            elif no_signal_path:
                fields["state"] = "failed"
                fields["failedAt"] = now_ms
        _update_job_fields_locked(job_path, artifacts_dir, fields)

    out2 = {
        "type": "opencode-subtask-cancel",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "taskId": task_id,
        "ok": ok,
        "warnings": runtime_warnings,
        "pid": pid or None,
        "sessionId": session_id,
        "terminationConfirmed": termination_confirmed,
        "terminationEvidence": termination_evidence,
        "workerOwnership": worker_ownership,
        "allowUnknownOwnershipKill": allow_unknown_kill,
        "cancelPhase": cancel_phase,
        "killSignalDelivered": kill_signal_delivered,
        "probeInconclusiveAfterKill": probe_inconclusive_after_kill,
        "cancelUnverified": cancel_unverified,
        "stopServerAttempted": stop_attempted,
        "stopServerOk": stop_ok,
        # taskError: describes why the task ended (mirrors finish.json error).
        # Present only when a cancel finish was successfully persisted.
        "taskError": task_error if cancel_fin_written else None,
    }
    # error: cancel-command failure.  Invariant: ok=true => error absent.
    if not ok and cancel_error:
        out2["error"] = cancel_error
    sys.stdout.write(_json_line(out2) + "\n")
    return 0 if ok else 1



def _progress_event_line(*, task_id: str | None, run_id: str, progress: dict[str, Any], artifacts_dir: Path) -> dict[str, Any]:
    summary = progress.get("summary") if isinstance(progress, dict) else None
    if not isinstance(summary, dict):
        summary = {}
    return {
        "type": "opencode-subtask-progress",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "taskId": task_id,
        "runId": run_id,
        "phase": summary.get("phase"),
        "lastEvent": summary.get("lastEvent"),
        "lastTool": summary.get("lastTool"),
        "idleForSeconds": progress.get("idleForSeconds") if isinstance(progress, dict) else None,
        "artifactsDir": str(artifacts_dir),
    }


def cmd_watch(args: argparse.Namespace) -> int:
    """Wait with stderr progress notifications and a single JSON terminal stdout.

    This is the adapter-side analogue of host task notifications: stdout remains
    one control-plane object, while stderr emits compact OPENCODE_SUBTASK_PROGRESS
    lines that callers may stream without reading events.ndjson.
    """
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None) and not getattr(args, "task_id", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id, --artifacts-dir, or --task-id", exit_code=2
        )
    workdir = Path(getattr(args, "workdir", ".") or ".").expanduser().resolve()
    run_id, artifacts_dir, task_state = _resolve_task_artifacts(
        workdir=workdir,
        task_id=getattr(args, "task_id", None),
        run_id=getattr(args, "run_id", None),
        artifacts_dir=getattr(args, "artifacts_dir", None),
    )
    task_id = str(task_state.get("taskId")) if isinstance(task_state, dict) and task_state.get("taskId") else None
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    job = _load_json(job_path) or {}
    if (not task_id) and isinstance(job, dict) and job.get("taskId"):
        task_id = str(job.get("taskId"))
    run_id = _canonical_run_id(run_id, job)

    poll_interval = float(getattr(args, "poll_interval", 0.5) or 0.5)
    progress_interval = float(getattr(args, "progress_interval", DEFAULT_WATCH_PROGRESS_INTERVAL_S) or DEFAULT_WATCH_PROGRESS_INTERVAL_S)
    if poll_interval <= 0 or progress_interval <= 0:
        _exit_with_error("BadConfig", "--poll-interval and --progress-interval must be > 0", exit_code=2)
    timeout_s = float(getattr(args, "wait_timeout", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    deadline = time.monotonic() + timeout_s
    last_emit = 0.0
    last_signature = None
    runtime_warnings: list[dict[str, str]] = []

    while True:
        fin, finish_warnings = _load_runtime_finish_envelope(finish_path)
        runtime_warnings = _dedupe_warnings([*runtime_warnings, *finish_warnings])
        if isinstance(fin, dict):
            sys.stdout.write(_json_line(_with_execution_warnings(fin, runtime_warnings)) + "\n")
            return _outcome_exit_code(str(fin.get("outcome") or "internal_error"))

        job = _load_json(job_path) or {}
        if (not task_id) and isinstance(job, dict) and job.get("taskId"):
            task_id = str(job.get("taskId"))
        pid = _safe_int(job.get("pid")) if isinstance(job, dict) else None
        progress = _progress_snapshot(artifacts_dir)
        if isinstance(job, dict):
            synthesized = _maybe_finalize_stale_running_job(
                run_id=run_id,
                artifacts_dir=artifacts_dir,
                finish_path=finish_path,
                job_path=job_path,
                job=job,
                pid=pid,
                progress=progress,
                stale_idle_s=DEFAULT_STALE_IDLE_S,
                dead_worker_grace_s=DEFAULT_DEAD_WORKER_GRACE_S,
                cancel_stuck_grace_s=DEFAULT_CANCEL_STUCK_GRACE_S,
            )
            if isinstance(synthesized, dict):
                sys.stdout.write(_json_line(synthesized) + "\n")
                return _outcome_exit_code(str(synthesized.get("outcome") or "internal_error"))

        now = time.monotonic()
        summary = progress.get("summary") if isinstance(progress, dict) else {}
        sig = None
        if isinstance(summary, dict):
            sig = (summary.get("phase"), summary.get("lastEvent"), summary.get("lastTool"))
        if now - last_emit >= progress_interval or sig != last_signature:
            sys.stderr.write("OPENCODE_SUBTASK_PROGRESS " + _json_line(_progress_event_line(task_id=task_id, run_id=run_id, progress=progress, artifacts_dir=artifacts_dir)) + "\n")
            sys.stderr.flush()
            last_emit = now
            last_signature = sig

        if now >= deadline:
            out = _status_obj(
                ok=True,
                run_id=run_id,
                task_id=task_id,
                status="running",
                pid=pid,
                workdir=str(job.get("workdir")) if isinstance(job, dict) and job.get("workdir") else None,
                artifacts_dir=artifacts_dir,
                artifacts=_minimal_artifacts(dir_path=artifacts_dir, job_path=job_path, finish_path=finish_path),
                progress=progress,
                warnings=runtime_warnings or None,
                wait_expired=True,
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 0
        time.sleep(poll_interval)


def cmd_list_tasks(args: argparse.Namespace) -> int:
    workdir = Path(getattr(args, "workdir", ".") or ".").expanduser().resolve()
    limit = int(getattr(args, "limit", 50) or 50)
    if limit <= 0:
        limit = 50
    task_dir = _task_project_dir(workdir)
    items: list[dict[str, Any]] = []
    if task_dir.exists():
        for path in task_dir.glob("*.json"):
            if path.name.endswith(".lock.json"):
                continue
            st = _load_json(path)
            if not isinstance(st, dict) or not st.get("taskId"):
                continue
            items.append({
                "taskId": st.get("taskId"),
                "state": st.get("state"),
                "outcome": st.get("outcome"),
                "sessionId": st.get("sessionId"),
                "latestRunId": st.get("latestRunId"),
                "latestArtifactsDir": st.get("latestArtifactsDir"),
                "description": st.get("description"),
                "subtask": st.get("subtask"),
                "updatedAt": st.get("updatedAt"),
                "createdAt": st.get("createdAt"),
                "finishPath": st.get("finishPath"),
                "notificationPath": st.get("notificationPath"),
            })
    items.sort(key=lambda x: int(x.get("updatedAt") or x.get("createdAt") or 0), reverse=True)
    out = {
        "type": "opencode-subtask-task-list",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": True,
        "warnings": [],
        "workdir": str(workdir),
        "count": min(len(items), limit),
        "tasks": items[:limit],
    }
    sys.stdout.write(_json_line(out) + "\n")
    return 0

def cmd_ensure_server(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    env = _safe_merge_env(os.environ, set_vars=args.env, set_from_files=args.env_file)
    auth = _get_http_auth_from_env(env)
    opencode_bin = _resolve_executable_for_workdir(args.opencode, workdir)
    if not opencode_bin:
        sys.stdout.write(
            _json_line(
                {
                    "type": "opencode-subtask-server",
                    "schemaVersion": ADAPTER_SCHEMA_VERSION,
                    "adapterVersion": ADAPTER_VERSION,
                    "timestamp": _now_ms(),
                    "ok": False,
                    "warnings": [],
                    "error": {"name": "OpencodeNotFound", "message": args.opencode},
                }
            )
            + "\n"
        )
        return 1
    try:
        st = ensure_server(
            opencode_bin=opencode_bin,
            workdir=workdir,
            hostname=args.server_hostname,
            port=args.server_port,
            wait_s=args.server_wait,
            env=env,
            auth=auth,
            cors_origins=list(getattr(args, "server_cors", []) or []),
        )
        out = {
            "type": "opencode-subtask-server",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "timestamp": _now_ms(),
            "ok": True,
            "warnings": [],
            "server": st,
        }
        sys.stdout.write(_json_line(out) + "\n")
        return 0
    except Exception as e:
        out = {
            "type": "opencode-subtask-server",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "timestamp": _now_ms(),
            "ok": False,
            "warnings": [],
            "error": {"name": type(e).__name__, "message": str(e)},
        }
        sys.stdout.write(_json_line(out) + "\n")
        return 1


def cmd_stop_server(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    out = stop_server(workdir)
    out2 = {
        "type": "opencode-subtask-stop-server",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "warnings": [],
        **out,
    }
    sys.stdout.write(_json_line(out2) + "\n")
    return 0 if out.get("ok") else 1


def cmd_prune_cache(args: argparse.Namespace) -> int:
    rep = _prune_run_artifacts(keep_last=int(args.keep_last), dry_run=(not args.apply))
    ok = len(rep.get("errors", [])) == 0
    err = None
    if not ok:
        n = len(rep.get("errors", []) or [])
        err = {
            "name": "PruneFailed",
            "message": f"{n} run artifact dir(s) could not be deleted",
        }
    sys.stdout.write(
        _json_line(
            {
                "type": "opencode-subtask-prune-cache",
                "schemaVersion": ADAPTER_SCHEMA_VERSION,
                "adapterVersion": ADAPTER_VERSION,
                "timestamp": _now_ms(),
                "ok": ok,
                "warnings": [],
                **({"error": err} if err else {}),
                "applied": bool(args.apply),
                "report": rep,
            }
        )
        + "\n"
    )
    return 0 if ok else 1


# ============================
# CLI
# ============================


def _add_common_run_flags(p: argparse.ArgumentParser) -> None:
    default_opencode = "opencode"
    p.add_argument(
        "--opencode",
        default=default_opencode,
        help="Path to opencode executable (Windows: prefer opencode.exe if available).",
    )
    p.add_argument("--workdir", default=".", help="Working directory (project root).")

    p.add_argument(
        "--run-id",
        default=None,
        help="Existing run id (advanced; used by worker/start).",
    )
    p.add_argument(
        "--artifacts-dir",
        default=None,
        help="Explicit artifacts directory (advanced; used by worker/start).",
    )

    p.add_argument(
        "--engine",
        choices=["auto", "http", "cli"],
        default="auto",
        help="Execution engine. auto prefers HTTP then falls back to CLI on non-timeout failures.",
    )
    p.add_argument(
        "--attach",
        default=None,
        help="Attach/connect to an existing OpenCode server URL (e.g., http://127.0.0.1:4096).",
    )

    # New naming: attach-server / no-attach-server (default: attach)
    p.add_argument(
        "--attach-server",
        dest="attach_server",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attach to a per-project opencode server when available (auto), or ensure one is running (http). (default: true)",
    )
    p.add_argument(
        "--server-hostname",
        default=DEFAULT_SERVER_HOSTNAME,
        help="opencode serve hostname",
    )
    p.add_argument(
        "--server-port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help="opencode serve port (0 => auto)",
    )
    p.add_argument(
        "--server-wait",
        type=float,
        default=DEFAULT_SERVER_WAIT_S,
        help="Seconds to wait for server health",
    )
    p.add_argument(
        "--server-cors",
        action="append",
        default=[],
        help="Extra CORS origin passed to `opencode serve --cors`; repeatable.",
    )
    p.add_argument(
        "--stop-server-after-run",
        choices=["if-started", "always", "never"],
        default=DEFAULT_STOP_SERVER_AFTER_RUN,
        help=(
            "(HTTP engine) Auto-stop policy after run. "
            "if-started: stop only if this invocation started a new per-project server; "
            "always: stop whenever HTTP engine was used; "
            "never: never auto-stop."
        ),
    )
    p.add_argument(
        "--orphan-reaper",
        dest="orphan_reaper",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "On run start, reap orphan per-project servers left by crashed/hard-killed workers. "
            "(default: true)"
        ),
    )
    p.add_argument(
        "--orphan-reaper-idle-s",
        type=float,
        default=DEFAULT_ORPHAN_REAPER_IDLE_S,
        help=(
            "Fallback idle timeout for reaping healthy but unreferenced per-project servers. "
            "Set 0 to disable idle-timeout reaping."
        ),
    )

    # Session continuity (optional). --session maps to HTTP /session/:id/message and CLI --session.
    # --continue remains CLI-only because the HTTP API requires an explicit session id.
    p.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="(CLI engine) Continue the last opencode session.",
    )
    p.add_argument(
        "-s",
        "--session",
        default=None,
        help="Continue a specific opencode session id (HTTP or CLI).",
    )
    p.add_argument(
        "--task-id",
        dest="task_id",
        default=None,
        help=(
            "Caller-facing external task handle. Existing adapter task ids resume their "
            "recorded OpenCode session and inject bounded task memory. Use --session "
            "explicitly for raw OpenCode ses_* continuation."
        ),
    )
    p.add_argument(
        "--on-running-task",
        choices=["fail", "start-new"],
        default="fail",
        help="When --task-id points at an already-running child: fail (default) or start-new.",
    )
    p.add_argument(
        "--task-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Inject bounded adapter task memory/progress when continuing a known --task-id. (default: true)",
    )
    p.add_argument(
        "--task-memory-max-chars",
        type=int,
        default=DEFAULT_TASK_MEMORY_MAX_CHARS,
        help="Character budget for continuation memory injected into the child prompt.",
    )
    p.add_argument("--title", default=None, help="Session title for new sessions (HTTP) or CLI --title.")
    p.add_argument(
        "--description",
        default=None,
        help="Short delegated-task label; used for session title and ask metadata when --title is absent.",
    )
    p.add_argument(
        "--acceptance",
        action="append",
        default=[],
        help="Acceptance criterion / stop condition appended to the child-agent briefing. Repeatable.",
    )
    p.add_argument(
        "--subtask-type",
        default=DEFAULT_SUBTASK_TYPE,
        help="Adapter child role: general|worker|fast-worker|thinker|explore|scout.",
    )
    p.add_argument(
        "--subagent-type",
        dest="subtask_type",
        help="Alias for --subtask-type, matching OpenCode Task-tool terminology.",
    )
    p.add_argument(
        "--allow-nested-subtasks",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="For read-only roles, permit OpenCode task/subagent delegation instead of adding the task deny rule.",
    )
    p.add_argument(
        "--allow-child-todos",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="For read-only roles, permit child todo bookkeeping instead of adding the todowrite deny rule.",
    )

    p.add_argument("--agent", default=None, help="OpenCode agent name")
    p.add_argument("-m", "--model", default=None, help="Model id provider/model")
    p.add_argument(
        "--variant",
        default=None,
        help="Model variant (provider-specific). Equivalent to `opencode run --variant`.",
    )
    p.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        help="Extra files to include (CLI engine only).",
    )

    p.add_argument(
        "--run-timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="Runtime timeout seconds for run/start worker execution.",
    )
    p.add_argument(
        "--retry-empty-output",
        dest="retry_empty_output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retry when model returns an empty success with no tracked/untracked changes (count controlled by --empty-output-retries).",
    )
    p.add_argument(
        "--empty-output-retries",
        type=int,
        default=1,
        help="Max retries for empty model output (only when retry-empty-output is enabled).",
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="wait/status poll interval seconds",
    )
    p.add_argument(
        "--quiet",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Quiet mode (stdout only command result; streaming events stay in artifacts/stderr).",
    )
    p.add_argument(
        "--save-events",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save events.ndjson.",
    )
    p.add_argument(
        "--save-text",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save assistant.txt.",
    )
    p.add_argument(
        "--subagent-briefing",
        dest="subagent_briefing",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Append a small natural-language parent-agent boundary briefing "
            "that asks the child to execute directly and keep its final report compact."
        ),
    )
    p.add_argument(
        "--final-answer-budget-chars",
        type=int,
        default=DEFAULT_FINAL_ANSWER_BUDGET_CHARS,
        help="Target final-answer budget used by --subagent-briefing (0 => compact but no numeric budget).",
    )
    p.add_argument(
        "--cli-prompt-transport",
        choices=["stdin", "file"],
        default=DEFAULT_CLI_PROMPT_TRANSPORT,
        help=(
            "How CLI engine passes the main prompt to `opencode run`. "
            "stdin avoids attaching prompt.txt as a model-visible file; file is a fallback."
        ),
    )

    p.add_argument(
        "--permission-mode",
        choices=["inherit", "allow", "noninteractive"],
        default="inherit",
        help="Permission handling. HTTP engine auto-replies via API when possible.",
    )
    p.add_argument(
        "--permission-approval",
        choices=["once", "always"],
        default="once",
        help=(
            "HTTP auto-reply used with --permission-mode allow. "
            "noninteractive still rejects identified high-risk requests."
        ),
    )
    p.add_argument(
        "--http-deny-interactive-tools",
        dest="http_deny_interactive_tools",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "For HTTP-created sessions, deny OpenCode question/plan_enter/plan_exit tools "
            "so delegated runs do not stall waiting for an interactive parent. "
            "Default: enabled for read-only roles, disabled for implementation roles."
        ),
    )
    p.add_argument(
        "--execution-profile",
        choices=sorted(EXECUTION_PROFILES),
        default=DEFAULT_EXECUTION_PROFILE,
        help=(
            "Routing policy for engine/artifacts. "
            "hybrid(default): short->HTTP+lighter artifacts, long->CLI+full artifacts; "
            "latency: prefer HTTP+lighter artifacts; checkpoint: prefer CLI+full artifacts."
        ),
    )
    p.add_argument(
        "--hybrid-short-timeout-s",
        type=float,
        default=None,
        help=(
            "hybrid profile threshold: classify as short only if effective runtime timeout <= this value. "
            "Precedence: flag > env(OPENCODE_SUBTASK_HYBRID_SHORT_TIMEOUT_S) > default."
        ),
    )
    p.add_argument(
        "--hybrid-short-prompt-chars",
        type=int,
        default=None,
        help=(
            "hybrid profile threshold: classify as short only if prompt chars <= this value. "
            "Precedence: flag > env(OPENCODE_SUBTASK_HYBRID_SHORT_PROMPT_CHARS) > default."
        ),
    )
    p.add_argument(
        "--disable-claude-code",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Set OPENCODE_DISABLE_CLAUDE_CODE=1 (defensive).",
    )

    p.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
        help="Hard cap per artifact file (0 disables).",
    )
    p.add_argument(
        "--workaround-agent-attach",
        dest="workaround_agent_attach",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Workaround: avoid CLI --attach when --agent is set unless attach is explicit.",
    )

    p.add_argument(
        "--memory-friendly",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Record opencode-memory recovery hints for long audits.",
    )

    p.add_argument(
        "--persona-mode",
        choices=["off", "warn", "require", "prepend"],
        default="require",
        help="Prompt hygiene: ensure FIRST line is 'Act as ...' persona (no leading blank lines).",
    )
    p.add_argument(
        "--persona-line",
        default="Act as a senior software engineer.",
        help="Persona line used by --persona-mode prepend (and as the suggested default).",
    )
    p.add_argument("--prompt-file", default=None, help="Read prompt from file.")
    p.add_argument(
        "--prompt",
        dest="prompt_text",
        default=None,
        help="Prompt as a single string (alternative to positional prompt args).",
    )

    p.add_argument(
        "--env",
        action="append",
        default=[],
        help="Set env KEY=VALUE for opencode process/server.",
    )
    p.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="Set env KEY=PATH (file contents as value).",
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        parser = _JsonArgumentParser(prog="opencode_subtask.py")
        sub = parser.add_subparsers(dest="cmd", required=True)

        p_run = sub.add_parser("run", help="Run a subtask (foreground).")
        _add_common_run_flags(p_run)
        p_run.add_argument(
            "prompt",
            nargs=argparse.REMAINDER,
            help="Prompt args (use `--` before the prompt).",
        )
        p_run.set_defaults(func=cmd_run)

        p_task = sub.add_parser(
            "task",
            help="OpenCode Task-like external child-agent call; foreground prints final text, --background starts a task.",
        )
        _add_common_run_flags(p_task)
        p_task.add_argument(
            "--background",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="Start in background and return task/run metadata instead of final text.",
        )
        p_task.add_argument(
            "--ask-stdout-max-chars",
            type=int,
            default=DEFAULT_ASK_STDOUT_MAX_CHARS,
            help=(
                "Maximum characters printed by foreground task on stdout before adding a short "
                "truncation note. 0 disables the cap; full text remains in assistant.txt."
            ),
        )
        p_task.add_argument(
            "--ask-metadata-to-stderr",
            dest="ask_metadata_to_stderr",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=(
                "On successful foreground task, write one OPENCODE_SUBTASK_META JSON line "
                "to stderr with runId/taskId/sessionId/artifact paths. Stdout remains final text only."
            ),
        )
        p_task.add_argument(
            "--ask-error-json-to-stderr",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="On foreground task failure, echo the control-plane JSON envelope to stderr for programmatic callers.",
        )
        p_task.add_argument(
            "prompt",
            nargs=argparse.REMAINDER,
            help="Prompt args (use `--` before the prompt).",
        )
        p_task.set_defaults(
            func=cmd_task,
            persona_mode="off",
            save_text=True,
            quiet=True,
            subagent_briefing=True,
        )

        p_ask = sub.add_parser(
            "ask",
            help="Run a subtask and print only the final assistant text to stdout.",
        )
        _add_common_run_flags(p_ask)
        p_ask.add_argument(
            "--ask-stdout-max-chars",
            type=int,
            default=DEFAULT_ASK_STDOUT_MAX_CHARS,
            help=(
                "Maximum characters printed by ask on stdout before adding a short "
                "truncation note. 0 disables the cap; full text remains in assistant.txt."
            ),
        )
        p_ask.add_argument(
            "--ask-metadata-to-stderr",
            dest="ask_metadata_to_stderr",
            action=argparse.BooleanOptionalAction,
            default=False,
            help=(
                "On successful ask, write one OPENCODE_SUBTASK_META JSON line to stderr "
                "with runId/taskId/sessionId/artifact paths. Stdout remains final text only."
            ),
        )
        p_ask.add_argument(
            "--ask-error-json-to-stderr",
            action=argparse.BooleanOptionalAction,
            default=False,
            help="On ask failure, echo the control-plane JSON envelope to stderr for programmatic callers.",
        )
        p_ask.add_argument(
            "prompt",
            nargs=argparse.REMAINDER,
            help="Prompt args (use `--` before the prompt).",
        )
        p_ask.set_defaults(
            func=cmd_ask,
            persona_mode="off",
            save_text=True,
            quiet=True,
            subagent_briefing=True,
        )

        p_start = sub.add_parser("start", help="Start a subtask in background.")
        _add_common_run_flags(p_start)
        p_start.add_argument(
            "prompt",
            nargs=argparse.REMAINDER,
            help="Prompt args (use `--` before the prompt).",
        )
        p_start.set_defaults(func=cmd_start)

        p_wait = sub.add_parser("wait", help="Wait for a background job finish.json.")
        p_wait.add_argument("--workdir", default=".")
        p_wait.add_argument("--run-id", required=False, default=None)
        p_wait.add_argument("--task-id", required=False, default=None)
        p_wait.add_argument("--artifacts-dir", required=False, default=None)
        p_wait.add_argument(
            "--wait-timeout",
            type=float,
            default=DEFAULT_TIMEOUT_S,
            help="Wait window seconds for wait command.",
        )
        p_wait.add_argument("--poll-interval", type=float, default=0.5)
        p_wait.set_defaults(func=cmd_wait)

        p_watch = sub.add_parser(
            "watch",
            help="Wait for a job while emitting compact progress notifications to stderr.",
        )
        p_watch.add_argument("--workdir", default=".")
        p_watch.add_argument("--run-id", required=False, default=None)
        p_watch.add_argument("--task-id", required=False, default=None)
        p_watch.add_argument("--artifacts-dir", required=False, default=None)
        p_watch.add_argument("--wait-timeout", type=float, default=DEFAULT_TIMEOUT_S)
        p_watch.add_argument("--poll-interval", type=float, default=0.5)
        p_watch.add_argument(
            "--progress-interval",
            type=float,
            default=DEFAULT_WATCH_PROGRESS_INTERVAL_S,
            help="Seconds between stderr OPENCODE_SUBTASK_PROGRESS lines when phase is unchanged.",
        )
        p_watch.set_defaults(func=cmd_watch)

        p_status = sub.add_parser("status", help="Show job status/progress.")
        p_status.add_argument("--workdir", default=".")
        p_status.add_argument("--run-id", required=False, default=None)
        p_status.add_argument("--task-id", required=False, default=None)
        p_status.add_argument("--artifacts-dir", required=False, default=None)
        p_status.set_defaults(func=cmd_status)

        p_cancel = sub.add_parser(
            "cancel",
            help="Cancel a job by killing worker and aborting session if known.",
        )
        p_cancel.add_argument("--workdir", default=".")
        p_cancel.add_argument("--run-id", required=False, default=None)
        p_cancel.add_argument("--task-id", required=False, default=None)
        p_cancel.add_argument("--artifacts-dir", required=False, default=None)
        p_cancel.add_argument(
            "--env",
            action="append",
            default=[],
            help="For abort auth: OPENCODE_SERVER_USERNAME/PASSWORD via env.",
        )
        p_cancel.add_argument(
            "--env-file",
            action="append",
            default=[],
            help="For abort auth: OPENCODE_SERVER_USERNAME/PASSWORD via env-file.",
        )
        p_cancel.set_defaults(func=cmd_cancel)

        p_es = sub.add_parser(
            "ensure-server", help="Ensure a per-project opencode server."
        )
        p_es.add_argument("--opencode", default="opencode")
        p_es.add_argument("--workdir", default=".")
        p_es.add_argument("--server-hostname", default=DEFAULT_SERVER_HOSTNAME)
        p_es.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
        p_es.add_argument("--server-wait", type=float, default=DEFAULT_SERVER_WAIT_S)
        p_es.add_argument("--server-cors", action="append", default=[])
        p_es.add_argument("--env", action="append", default=[])
        p_es.add_argument("--env-file", action="append", default=[])
        p_es.set_defaults(func=cmd_ensure_server)

        p_lt = sub.add_parser("list-tasks", help="List adapter task handles for a workdir.")
        p_lt.add_argument("--workdir", default=".")
        p_lt.add_argument("--limit", type=int, default=50)
        p_lt.set_defaults(func=cmd_list_tasks)

        p_ss = sub.add_parser(
            "stop-server", help="Stop the per-project opencode server."
        )
        p_ss.add_argument("--workdir", default=".")
        p_ss.set_defaults(func=cmd_stop_server)

        p_pc = sub.add_parser(
            "prune-cache",
            help="Prune local run artifacts cache (disk). Safe-by-default: dry-run unless --apply.",
        )
        p_pc.add_argument(
            "--keep-last",
            type=int,
            default=200,
            help="Keep the most-recent N run artifact directories (by mtime).",
        )
        p_pc.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete; otherwise report-only (dry-run).",
        )
        p_pc.set_defaults(func=cmd_prune_cache)

        args = parser.parse_args(argv)

        return int(args.func(args))  # type: ignore[misc]
    except SystemExit:
        raise
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        msg = " ".join(str(msg).split())
        if len(msg) > 1000:
            msg = msg[:997] + "..."
        obj = {
            "type": "opencode-subtask-error",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "timestamp": _now_ms(),
            "ok": False,
            "warnings": [],
            "error": {"name": "UnhandledException", "message": msg},
        }
        sys.stdout.write(_json_line(obj) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
