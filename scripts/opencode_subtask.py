#!/usr/bin/env python3
"""
opencode_subtask.py

A Codex-friendly adapter around OpenCode that provides:
- Stable one-line JSON output (ASCII-only) with schemaVersion.
- Artifacts-first logging (events/stderr/assistant/payload/patch) to avoid caller context bloat.
- Job semantics: start -> wait (background) and run (foreground).
- Engine abstraction: HTTP server API preferred; CLI fallback.

Python: 3.10+
No third-party deps.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree, which
from typing import Any, Final, Iterable, NoReturn

# ============================
# Constants / schema
# ============================

ADAPTER_SCHEMA_VERSION: Final[int] = 2
ADAPTER_VERSION: Final[str] = "0.6.0"

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

DEFAULT_SERVER_HOSTNAME: Final[str] = "127.0.0.1"
DEFAULT_SERVER_PORT: Final[int] = 0  # 0 => pick a free port
DEFAULT_SERVER_WAIT_S: Final[float] = 10.0

SENTINEL_BEGIN: Final[str] = "BEGIN_OC_SUBTASK_JSON"
SENTINEL_END: Final[str] = "END_OC_SUBTASK_JSON"
PAYLOAD_PROTOCOL: Final[str] = "opencode-subtask-payload-v2"
OUTPUT_MODES: Final[set[str]] = {"machine", "text"}
DIAGNOSTICS_MODES: Final[set[str]] = {"never", "on-failure", "always"}
FINISH_OUTCOMES: Final[set[str]] = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "internal_error",
}
PAYLOAD_STATUSES: Final[set[str]] = {
    "validated",
    "not_requested",
    "missing",
    "malformed",
    "ambiguous",
    "persist_failed",
}
DECISION_STATUSES: Final[set[str]] = {
    "determinate",
    "abstained",
    "unavailable",
    "not_requested",
}
DECISION_ROUTES: Final[set[str]] = {"GO_NO_DELTA", "MANDATORY_DELTA"}
EXECUTION_PROFILES: Final[set[str]] = {"hybrid", "latency", "checkpoint"}
EXECUTION_ENGINE_SELECTED_VALUES: Final[set[str]] = {
    "cli",
    "http",
    "none",
    "watchdog",
    "cancel",
}
EXECUTION_ENGINE_FALLBACK_VALUES: Final[set[str]] = {"http"}
PAYLOAD_DECISIONS: Final[set[str]] = DECISION_ROUTES | {"UNDETERMINED"}
PAYLOAD_ERROR_CODES: Final[set[str]] = {
    "PAYLOAD_MISSING",
    "SENTINEL_MULTIPLE",
    "SENTINEL_TRAILING_TEXT",
    "NONCE_MISMATCH",
    "PAYLOAD_JSON_INVALID",
    "PAYLOAD_SCHEMA_INVALID",
    "DECISION_INVALID",
    "PAYLOAD_PERSIST_FAILED",
}
JUDGE_POLICIES: Final[set[str]] = {
    "execution-only",
    "require-determinate",
    "require-go-no-delta",
}
JUDGE_REASON: Final[dict[str, str]] = {
    "finish_not_found": "FINISH_NOT_FOUND",
    "finish_unreadable": "FINISH_UNREADABLE",
    "finish_invalid": "FINISH_INVALID",
    "unknown_policy": "UNKNOWN_POLICY",
    "payload_digest_mismatch": "PAYLOAD_DIGEST_MISMATCH",
    "execution_completed": "EXECUTION_COMPLETED",
    "execution_failed": "EXECUTION_FAILED",
    "execution_timed_out": "EXECUTION_TIMED_OUT",
    "execution_internal_error": "EXECUTION_INTERNAL_ERROR",
    "execution_cancelled": "EXECUTION_CANCELLED",
    "execution_unknown": "EXECUTION_UNKNOWN",
    "output_not_machine": "OUTPUT_NOT_MACHINE",
    "payload_missing": "PAYLOAD_MISSING",
    "payload_malformed": "PAYLOAD_MALFORMED",
    "payload_ambiguous": "PAYLOAD_AMBIGUOUS",
    "payload_persist_failed": "PAYLOAD_PERSIST_FAILED",
    "decision_go_no_delta": "DECISION_GO_NO_DELTA",
    "decision_mandatory_delta": "DECISION_MANDATORY_DELTA",
    "decision_undetermined": "DECISION_UNDETERMINED",
    "decision_unavailable": "DECISION_UNAVAILABLE",
}
JUDGE_EXECUTION_REASON_BY_OUTCOME: Final[dict[str, str]] = {
    "completed": JUDGE_REASON["execution_completed"],
    "failed": JUDGE_REASON["execution_failed"],
    "timed_out": JUDGE_REASON["execution_timed_out"],
    "internal_error": JUDGE_REASON["execution_internal_error"],
    "cancelled": JUDGE_REASON["execution_cancelled"],
}
JUDGE_PAYLOAD_REASON_BY_STATUS: Final[dict[str, str]] = {
    "missing": JUDGE_REASON["payload_missing"],
    "malformed": JUDGE_REASON["payload_malformed"],
    "ambiguous": JUDGE_REASON["payload_ambiguous"],
    "persist_failed": JUDGE_REASON["payload_persist_failed"],
}
GENERIC_SENTINEL_BEGIN_RE: Final[re.Pattern[str]] = re.compile(
    rf"{re.escape(SENTINEL_BEGIN)}_([A-Za-z0-9._-]+)"
)
GENERIC_SENTINEL_END_RE: Final[re.Pattern[str]] = re.compile(
    rf"{re.escape(SENTINEL_END)}_([A-Za-z0-9._-]+)"
)

JSON_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*({[\s\S]*?})\s*```", re.IGNORECASE
)

# ============================
# Small utilities
# ============================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_line(obj: dict[str, Any]) -> str:
    # ASCII-only JSON to survive GBK/CP1252 stdout encodings.
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _normalize_observed_sentinel_nonce(value: str) -> str:
    # Review models sometimes echo marker instructions inline and attach sentence
    # punctuation to the nonce token. Treat that punctuation as non-semantic when
    # diagnosing marker collisions.
    normalized = str(value).rstrip("`'\".,:;!?)]}")
    # Markdown separators like END...--- should not be treated as a distinct nonce.
    return re.sub(r"-{3,}$", "", normalized)


def _exit_with_error(error_name: str, message: str, exit_code: int = 1) -> NoReturn:
    """Print a JSON error to stdout and exit. Maintains stdout contract."""
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
    ArgumentParser that preserves adapter stdout JSON contract on CLI parse errors.
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


_RUN_ID_RE = re.compile(r"^[\w.\-]+$")  # alphanumeric, underscore, dot, hyphen


def _validate_run_id_for_path(rid: str) -> None:
    """Reject run_id values that could escape _runs_dir() via path traversal.

    Raises ValueError if the run_id contains path separators, ``..``
    components, or characters outside the safe whitelist.
    """
    if rid in (".", ".."):
        raise ValueError(f"run_id must not be a relative directory alias: {rid!r}")
    if ".." in rid.split("/") or ".." in rid.split("\\"):
        raise ValueError(f"run_id contains path traversal component: {rid!r}")
    if "/" in rid or "\\" in rid:
        raise ValueError(f"run_id contains path separator: {rid!r}")
    if not _RUN_ID_RE.match(rid):
        raise ValueError(f"run_id contains disallowed characters: {rid!r}")


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

    def create_session(self) -> dict[str, Any]:
        # Server docs: POST /session -> Session
        _, js = self._request_json("POST", "/session", {}, timeout_s=self.timeout_s)
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
        if model:
            body["model"] = model
        if variant:
            body["variant"] = variant
        if agent:
            body["agent"] = agent

        def _post_message(payload: dict[str, Any]) -> dict[str, Any]:
            # Server docs: POST /session/:id/message -> Message
            _, js = self._request_json(
                "POST", f"/session/{session_id}/message", payload, timeout_s=timeout_s
            )
            if not isinstance(js, dict):
                raise RuntimeError("Invalid /message response (expected JSON object)")
            return js

        try:
            return _post_message(body)
        except RuntimeError as e:
            # Back-compat: newer servers validate `model` as an object, older servers accept a string.
            # Retry only when the server clearly rejects the string type for `model`.
            if model and '"path":["model"]' in str(e) and "expected object" in str(e):
                body2 = dict(body)
                provider_id, sep, model_id = model.partition("/")
                if sep:
                    body2["model"] = {"providerID": provider_id, "modelID": model_id}
                else:
                    body2["model"] = {"providerID": "", "modelID": model}
                return _post_message(body2)
            raise

    def abort(self, session_id: str) -> None:
        # Server docs: POST /session/:id/abort
        try:
            self._request_json(
                "POST", f"/session/{session_id}/abort", {}, timeout_s=5.0
            )
        except Exception:
            pass

    def abort_checked(self, session_id: str, *, timeout_s: float = 5.0) -> None:
        # Same endpoint as abort(), but lets exceptions propagate for callers
        # that need a reliable success/failure signal.
        self._request_json(
            "POST", f"/session/{session_id}/abort", {}, timeout_s=timeout_s
        )

    def reply_permission(
        self,
        session_id: str,
        permission_id: str,
        *,
        response: str,
        remember: bool = False,
    ) -> None:
        # Server docs: POST /session/:id/permissions/:permissionID
        body = {"response": response, "remember": remember}
        try:
            self._request_json(
                "POST",
                f"/session/{session_id}/permissions/{permission_id}",
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
    if obj.get("type") != "opencode-subtask-finish":
        return "type must be opencode-subtask-finish"
    # V2 is a deliberate schema break. Lifecycle commands only trust strict V2
    # finish envelopes and quarantine anything older or malformed.
    if obj.get("schemaVersion") != ADAPTER_SCHEMA_VERSION:
        return f"schemaVersion must be {ADAPTER_SCHEMA_VERSION}"

    output_mode = obj.get("outputMode")
    if output_mode not in OUTPUT_MODES:
        return "outputMode must be machine or text"

    outcome = obj.get("outcome")
    if outcome not in FINISH_OUTCOMES:
        return "outcome is invalid"

    execution = obj.get("execution")
    payload = obj.get("payload")
    decision = obj.get("decision")
    workspace = obj.get("workspace")
    artifacts = obj.get("artifacts")
    if not isinstance(execution, dict):
        return "execution must be an object"
    if not isinstance(payload, dict):
        return "payload must be an object"
    if not isinstance(decision, dict):
        return "decision must be an object"
    if not isinstance(workspace, dict):
        return "workspace must be an object"
    if not isinstance(artifacts, dict):
        return "artifacts must be an object"

    execution_engine = execution.get("engine")
    if not isinstance(execution_engine, dict):
        return "execution.engine must be an object"
    if not isinstance(payload.get("artifact"), dict):
        return "payload.artifact must be an object"
    if execution.get("error") is not None and not isinstance(execution.get("error"), dict):
        return "execution.error must be an object or null"
    payload_errors = payload.get("errors")
    if not isinstance(payload_errors, list):
        return "payload.errors must be a list"

    engine_selected = execution_engine.get("selected")
    if engine_selected not in EXECUTION_ENGINE_SELECTED_VALUES:
        return "execution.engine.selected is invalid"
    engine_fallback = execution_engine.get("fallbackFrom")
    if engine_fallback is not None and engine_fallback not in EXECUTION_ENGINE_FALLBACK_VALUES:
        return "execution.engine.fallbackFrom is invalid"
    if engine_fallback is not None and engine_selected != "cli":
        return "execution.engine.fallbackFrom requires execution.engine.selected=cli"

    if payload.get("status") not in PAYLOAD_STATUSES:
        return "payload.status is invalid"
    decision_status = decision.get("status")
    if decision_status not in DECISION_STATUSES:
        return "decision.status is invalid"
    payload_status = payload.get("status")
    payload_schema = payload.get("schema")
    payload_artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    payload_path = payload_artifact.get("path")
    payload_digest = payload_artifact.get("digest")
    if payload_schema is not None and not isinstance(payload_schema, str):
        return "payload.schema must be a string or null"
    if payload_path is not None and not isinstance(payload_path, str):
        return "payload.artifact.path must be a string or null"
    if payload_digest is not None and not isinstance(payload_digest, str):
        return "payload.artifact.digest must be a string or null"
    for idx, entry in enumerate(payload_errors):
        if not isinstance(entry, dict):
            return f"payload.errors[{idx}] must be an object"
        if entry.get("code") not in PAYLOAD_ERROR_CODES:
            return f"payload.errors[{idx}].code is invalid"
        if not isinstance(entry.get("message"), str):
            return f"payload.errors[{idx}].message must be a string"

    if output_mode == "text":
        if payload_status != "not_requested":
            return "text outputMode requires payload.status=not_requested"
        if decision_status != "not_requested":
            return "text outputMode requires decision.status=not_requested"
        if payload_path is not None or payload_digest is not None:
            return "text outputMode requires payload.artifact.path/digest=null"
    elif payload_status == "not_requested":
        return "machine outputMode forbids payload.status=not_requested"
    elif payload_status != "validated" and decision_status != "unavailable":
        return "decision.status is invalid for non-validated payloads"
    elif payload_status == "validated":
        if payload_schema != PAYLOAD_PROTOCOL:
            return f"validated payload requires payload.schema={PAYLOAD_PROTOCOL}"
        if not isinstance(payload_path, str) or not payload_path:
            return "validated payload requires payload.artifact.path"
        if not isinstance(payload_digest, str) or not payload_digest:
            return "validated payload requires payload.artifact.digest"
        if decision_status not in {"determinate", "abstained"}:
            return "validated payload requires decision.status=determinate|abstained"

    if payload_status != "validated":
        if payload_path is not None or payload_digest is not None:
            return "non-validated payload requires payload.artifact.path/digest=null"

    if outcome == "completed" and execution.get("error") is not None:
        return "completed outcome requires execution.error=null"

    route = decision.get("route")
    if decision_status == "determinate":
        if route not in DECISION_ROUTES:
            return "decision.route must be a known route when determinate"
    elif route is not None:
        return "decision.route must be null unless decision.status is determinate"

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
        else "finish.json failed strict V2 validation"
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


def _extract_text_from_event(evt: dict[str, Any]) -> str | None:
    # Best-effort for OpenCode event shapes.
    for k in ("text", "delta", "content"):
        v = evt.get(k)
        if isinstance(v, str) and v:
            return v
    part = evt.get("part")
    if isinstance(part, dict):
        for k in ("text", "delta", "content"):
            v = part.get(k)
            if isinstance(v, str) and v:
                return v
    msg = evt.get("message")
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
            out = "".join(chunks).strip()
            return out or None
    data = evt.get("data")
    if isinstance(data, dict):
        for k in ("text", "delta", "content"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
    return None


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


@dataclass
class MachinePayloadExtraction:
    payload_status: str
    payload_schema: str | None
    decision_status: str
    decision_route: str | None
    payload_obj: dict[str, Any] | None
    errors: list[dict[str, str]]
    diagnostics: dict[str, Any]


# ============================
# Structured result extraction
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
    assistant text / payload signal.
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


def _payload_error(code: str, message: str) -> dict[str, str]:
    if code not in PAYLOAD_ERROR_CODES:
        code = "PAYLOAD_SCHEMA_INVALID"
    return {"code": str(code), "message": str(message)}


def _normalize_output_mode(mode: str | None) -> str:
    m = str(mode or "machine").strip().lower()
    return m if m in OUTPUT_MODES else "machine"


def _normalize_diagnostics_mode(mode: str | None) -> str:
    m = str(mode or "on-failure").strip().lower()
    return m if m in DIAGNOSTICS_MODES else "on-failure"


def _http_unsupported_options(args: argparse.Namespace) -> list[str]:
    unsupported: list[str] = []
    if bool(getattr(args, "continue_last", False)):
        unsupported.append("--continue")
    if str(getattr(args, "session", "") or "").strip():
        unsupported.append("--session")
    if str(getattr(args, "title", "") or "").strip():
        unsupported.append("--title")
    files = list(getattr(args, "file", []) or [])
    if files:
        unsupported.append("--file")
    return unsupported


def _dedupe_payload_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for v in errors:
        if not isinstance(v, dict):
            continue
        code = str(v.get("code") or "").strip()
        msg = str(v.get("message") or "").strip()
        if not code:
            continue
        key = (code, msg)
        if key in seen:
            continue
        seen.add(key)
        out.append({"code": code, "message": msg})
    return out


def _make_contract_nonce() -> str:
    return secrets.token_hex(8)


def _sentinel_markers(nonce: str) -> tuple[str, str]:
    token = str(nonce or "").strip()
    return f"{SENTINEL_BEGIN}_{token}", f"{SENTINEL_END}_{token}"


def _canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    return (
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _extract_heuristic_diagnostics(
    text: str, *, max_scan_chars: int = 80_000
) -> dict[str, Any]:
    fence_obj: dict[str, Any] | None = None
    blocks = JSON_FENCE_RE.findall(text or "")
    for blk in reversed(blocks):
        blk = blk.strip()
        if not blk:
            continue
        try:
            obj = json.loads(blk)
        except Exception:
            continue
        if isinstance(obj, dict):
            fence_obj = obj
            break

    backscan_obj: dict[str, Any] | None = None
    window = text[-max_scan_chars:] if len(text) > max_scan_chars else text
    for i in range(len(window) - 1, -1, -1):
        if window[i] != "{":
            continue
        candidate = window[i:].strip()
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict):
            backscan_obj = obj
            break

    return {
        "fence": fence_obj,
        "backscan": backscan_obj,
        "fenceCount": len(blocks),
    }


def _validate_payload_schema(obj: dict[str, Any] | None, nonce: str) -> list[dict[str, str]]:
    if not isinstance(obj, dict):
        return [_payload_error("PAYLOAD_JSON_INVALID", "payload must be a JSON object")]
    errs: list[str] = []
    if obj.get("protocol") != PAYLOAD_PROTOCOL:
        errs.append(f"protocol must equal {PAYLOAD_PROTOCOL!r}")
    if obj.get("nonce") != nonce:
        return [_payload_error("NONCE_MISMATCH", f"payload nonce must equal {nonce!r}")]

    raw_decision = obj.get("decision")
    if not isinstance(raw_decision, str):
        errs.append("decision must be string enum")
    else:
        d = raw_decision.strip().upper()
        if d not in PAYLOAD_DECISIONS:
            return [
                _payload_error(
                    "DECISION_INVALID",
                    f"decision must be one of {sorted(PAYLOAD_DECISIONS)}; got {raw_decision!r}",
                )
            ]
    if not isinstance(obj.get("summary"), str):
        errs.append("summary must be string")
    for key in ("evidence", "changes", "next_steps"):
        v = obj.get(key)
        if not isinstance(v, list):
            errs.append(f"{key} must be string[]")
            continue
        if any((not isinstance(x, str)) for x in v):
            errs.append(f"{key} must be string[]")
    return [_payload_error("PAYLOAD_SCHEMA_INVALID", se) for se in errs]


def _extract_machine_payload(
    *,
    text: str,
    nonce: str | None,
    output_mode: str,
) -> MachinePayloadExtraction:
    output_mode_n = _normalize_output_mode(output_mode)
    nonce_n = str(nonce or "").strip()
    diagnostics: dict[str, Any] = {
        "expectedNonce": nonce_n or None,
        "outputMode": output_mode_n,
        "heuristics": _extract_heuristic_diagnostics(text),
    }

    if output_mode_n == "text":
        diagnostics["machine"] = {"status": "not_requested"}
        return MachinePayloadExtraction(
            payload_status="not_requested",
            payload_schema=None,
            decision_status="not_requested",
            decision_route=None,
            payload_obj=None,
            errors=[],
            diagnostics=diagnostics,
        )

    if not nonce_n:
        err = _payload_error("NONCE_MISMATCH", "missing contract nonce")
        diagnostics["machine"] = {"status": "malformed"}
        return MachinePayloadExtraction(
            payload_status="malformed",
            payload_schema=None,
            decision_status="unavailable",
            decision_route=None,
            payload_obj=None,
            errors=[err],
            diagnostics=diagnostics,
        )

    begin_marker, end_marker = _sentinel_markers(nonce_n)
    exact_begin_positions = [m.start() for m in re.finditer(re.escape(begin_marker), text)]
    exact_end_positions = [m.start() for m in re.finditer(re.escape(end_marker), text)]
    exact_begin_count = len(exact_begin_positions)
    exact_end_count = len(exact_end_positions)
    generic_begins = [
        {"nonce": _normalize_observed_sentinel_nonce(m.group(1)), "index": m.start()}
        for m in GENERIC_SENTINEL_BEGIN_RE.finditer(text or "")
    ]
    generic_ends = [
        {"nonce": _normalize_observed_sentinel_nonce(m.group(1)), "index": m.start()}
        for m in GENERIC_SENTINEL_END_RE.finditer(text or "")
    ]
    generic_nonces = sorted(
        {
            *(str(item["nonce"]) for item in generic_begins),
            *(str(item["nonce"]) for item in generic_ends),
        }
    )
    diagnostics["machine"] = {
        "beginMarker": begin_marker,
        "endMarker": end_marker,
        "exactBeginCount": exact_begin_count,
        "exactEndCount": exact_end_count,
        "genericBeginCount": len(generic_begins),
        "genericEndCount": len(generic_ends),
        "genericNonces": generic_nonces,
    }

    if len(generic_nonces) > 1:
        err = _payload_error(
            "SENTINEL_MULTIPLE",
            "multiple sentinel nonces found; nonce binding is ambiguous",
        )
        return MachinePayloadExtraction(
            payload_status="ambiguous",
            payload_schema=None,
            decision_status="unavailable",
            decision_route=None,
            payload_obj=None,
            errors=[err],
            diagnostics=diagnostics,
        )

    if exact_begin_count == 0 and exact_end_count == 0:
        if generic_nonces and nonce_n not in generic_nonces:
            err = _payload_error(
                "NONCE_MISMATCH",
                f"found sentinel for nonce {generic_nonces[0]!r}, expected {nonce_n!r}",
            )
            return MachinePayloadExtraction(
                payload_status="malformed",
                payload_schema=None,
                decision_status="unavailable",
                decision_route=None,
                payload_obj=None,
                errors=[err],
                diagnostics=diagnostics,
            )
        err = _payload_error(
            "PAYLOAD_MISSING", "no authoritative payload sentinel found"
        )
        return MachinePayloadExtraction(
            payload_status="missing",
            payload_schema=None,
            decision_status="unavailable",
            decision_route=None,
            payload_obj=None,
            errors=[err],
            diagnostics=diagnostics,
        )

    candidate_results: list[dict[str, Any]] = []
    for begin_idx in exact_begin_positions:
        end_idx = text.find(end_marker, begin_idx + len(begin_marker))
        if end_idx == -1 or end_idx < begin_idx:
            continue
        payload_text = text[begin_idx + len(begin_marker) : end_idx].strip()
        trailing = text[end_idx + len(end_marker) :]
        candidate_errors: list[dict[str, str]] = []
        if trailing.strip():
            candidate_errors.append(
                _payload_error(
                    "SENTINEL_TRAILING_TEXT",
                    "only whitespace is allowed after the END sentinel",
                )
            )

        candidate_obj: dict[str, Any] | None = None
        try:
            parsed = json.loads(payload_text)
            if isinstance(parsed, dict):
                candidate_obj = parsed
            else:
                candidate_errors.append(
                    _payload_error(
                        "PAYLOAD_JSON_INVALID",
                        "payload must decode to a JSON object",
                    )
                )
        except Exception as ex:
            candidate_errors.append(
                _payload_error(
                    "PAYLOAD_JSON_INVALID", f"{type(ex).__name__}: {ex}"
                )
            )

        if isinstance(candidate_obj, dict):
            candidate_errors.extend(_validate_payload_schema(candidate_obj, nonce_n))

        candidate_results.append(
            {
                "begin": begin_idx,
                "end": end_idx,
                "payload_obj": candidate_obj,
                "errors": _dedupe_payload_errors(candidate_errors),
            }
        )

    diagnostics["machine"]["candidateCount"] = len(candidate_results)
    valid_candidates = [
        item
        for item in candidate_results
        if isinstance(item.get("payload_obj"), dict) and not item.get("errors")
    ]
    diagnostics["machine"]["validCandidateCount"] = len(valid_candidates)

    if len(valid_candidates) > 1:
        err = _payload_error(
            "SENTINEL_MULTIPLE",
            "multiple valid sentinel payloads found for the expected nonce",
        )
        return MachinePayloadExtraction(
            payload_status="ambiguous",
            payload_schema=None,
            decision_status="unavailable",
            decision_route=None,
            payload_obj=None,
            errors=[err],
            diagnostics=diagnostics,
        )

    if len(valid_candidates) == 1:
        obj = valid_candidates[0]["payload_obj"]
        assert isinstance(obj, dict)
        decision = str(obj["decision"]).strip().upper()
        if decision == "UNDETERMINED":
            decision_status = "abstained"
            decision_route = None
        else:
            decision_status = "determinate"
            decision_route = decision

        return MachinePayloadExtraction(
            payload_status="validated",
            payload_schema=PAYLOAD_PROTOCOL,
            decision_status=decision_status,
            decision_route=decision_route,
            payload_obj=obj,
            errors=[],
            diagnostics=diagnostics,
        )

    if not candidate_results:
        err = _payload_error(
            "PAYLOAD_MISSING",
            "could not locate a matching payload region between sentinel markers",
        )
        return MachinePayloadExtraction(
            payload_status="malformed",
            payload_schema=None,
            decision_status="unavailable",
            decision_route=None,
            payload_obj=None,
            errors=[err],
            diagnostics=diagnostics,
        )

    last_candidate = candidate_results[-1]
    obj = (
        last_candidate["payload_obj"]
        if isinstance(last_candidate.get("payload_obj"), dict)
        else None
    )
    deduped_errors = list(last_candidate["errors"])
    if deduped_errors:
        return MachinePayloadExtraction(
            payload_status="malformed",
            payload_schema=None,
            decision_status="unavailable",
            decision_route=None,
            payload_obj=obj,
            errors=deduped_errors,
            diagnostics=diagnostics,
        )

    assert isinstance(obj, dict)
    decision = str(obj["decision"]).strip().upper()
    if decision == "UNDETERMINED":
        decision_status = "abstained"
        decision_route = None
    else:
        decision_status = "determinate"
        decision_route = decision

    return MachinePayloadExtraction(
        payload_status="validated",
        payload_schema=PAYLOAD_PROTOCOL,
        decision_status=decision_status,
        decision_route=decision_route,
        payload_obj=obj,
        errors=[],
        diagnostics=diagnostics,
    )


def _default_contract_prompt(nonce: str) -> str:
    begin_marker, end_marker = _sentinel_markers(nonce)
    return (
        "\n\n"
        "SUBTASK OUTPUT CONTRACT (strict):\n"
        f"At the very end, output exactly:\n"
        f"{begin_marker}\n"
        "{...one JSON object...}\n"
        f"{end_marker}\n"
        "No markdown, no code fences, no extra text after the END marker.\n"
        "Do not quote or restate the BEGIN/END markers anywhere except the final block.\n"
        "Schema:\n"
        "{\n"
        f'  "protocol": "{PAYLOAD_PROTOCOL}",\n'
        f'  "nonce": "{nonce}",\n'
        '  "decision": "GO_NO_DELTA" | "MANDATORY_DELTA" | "UNDETERMINED",\n'
        '  "summary": string (<= 800 chars),\n'
        '  "evidence": string[] (each: "path:line - fact", <= 10 items),\n'
        '  "changes": string[] (<= 10 items),\n'
        '  "next_steps": string[] (<= 10 items)\n'
        "}\n"
        'If you cannot safely determine the route, use "decision":"UNDETERMINED".\n'
    )


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
        "payloadPath": None,
        "diagnosticsPath": None,
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
    patch_path: str | None = None,
    payload_path: Path | None = None,
    diagnostics_path: Path | None = None,
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
        "payloadPath": _name_if_exists(payload_path),
        "diagnosticsPath": _name_if_exists(diagnostics_path),
    }


def _finish_obj(
    *,
    run_id: str,
    workdir: Path,
    output_mode: str,
    outcome: str,
    exit_code: int,
    duration_ms: int,
    engine_selected: str,
    fallback_from: str | None,
    session_id: str | None,
    execution_error: dict[str, Any] | None,
    execution_warnings: list[dict[str, str]] | None,
    payload_status: str,
    payload_schema: str | None,
    payload_path: str | None,
    payload_digest: str | None,
    payload_errors: list[dict[str, str]] | None,
    decision_status: str,
    decision_route: str | None,
    changed_files: list[str],
    untracked_files: list[str],
    patch_path: str | None,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    route_out = decision_route if decision_status == "determinate" else None
    obj: dict[str, Any] = {
        "type": "opencode-subtask-finish",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "workdir": str(workdir),
        "outputMode": _normalize_output_mode(output_mode),
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
        "payload": {
            "status": payload_status,
            "schema": payload_schema,
            "artifact": {
                "path": payload_path,
                "digest": payload_digest,
            },
            "errors": payload_errors or [],
        },
        "decision": {
            "status": decision_status,
            "route": route_out,
        },
        "workspace": {
            "changedFiles": changed_files,
            "untrackedFiles": untracked_files,
            "patchPath": patch_path,
        },
        "artifacts": artifacts,
    }
    return obj


def _start_obj(
    *,
    run_id: str,
    pid: int,
    workdir: Path,
    artifacts_dir: Path,
    artifacts: dict[str, Any],
    output_mode: str,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "opencode-subtask-start",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": True,
        "warnings": warnings or [],
        "runId": run_id,
        "pid": pid,
        "workdir": str(workdir),
        "outputMode": _normalize_output_mode(output_mode),
        "artifacts": artifacts,
    }


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
) -> dict[str, Any]:
    warnings_out = warnings or []
    obj = {
        "ok": ok,
        "type": "opencode-subtask-status",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
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


def _maybe_write_diagnostics(
    *,
    mode: str,
    diagnostics_path: Path,
    run_id: str,
    output_mode: str,
    execution_failed: bool,
    extraction: MachinePayloadExtraction,
) -> Path | None:
    mode_n = _normalize_diagnostics_mode(mode)
    output_mode_n = _normalize_output_mode(output_mode)
    extraction_failed = (
        extraction.payload_status != "not_requested"
        if output_mode_n == "text"
        else extraction.payload_status != "validated"
    )
    should_write = mode_n == "always" or (
        mode_n == "on-failure" and (execution_failed or extraction_failed)
    )
    if not should_write:
        return None
    obj = {
        "type": "opencode-subtask-diagnostics",
        "schemaVersion": 1,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "outputMode": _normalize_output_mode(output_mode),
        "payloadStatus": extraction.payload_status,
        "decisionStatus": extraction.decision_status,
        "errors": extraction.errors,
        "diagnostics": extraction.diagnostics,
    }
    _write_json(diagnostics_path, obj)
    return diagnostics_path


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
    # Always attach the prompt as a file; avoid shell quoting issues.
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
    observed_session_id: str | None = session_id
    error_event: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None

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
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_fp,
            env=env,
            **popen_kwargs,
        )
    except Exception as e:
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
                    # Never write streaming events to stdout; stdout is reserved for the final one-line JSON.
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
            sid = evt.get("sessionID") or evt.get("sessionId")
            if isinstance(sid, str) and sid and sid != observed_session_id:
                observed_session_id = sid
                if on_session_id:
                    try:
                        on_session_id(observed_session_id)
                    except Exception:
                        pass
            if evt.get("type") == "error":
                error_event = evt
            # Metrics from step-finish-ish events (best-effort)
            if evt.get("type") in ("step-finish", "step_finish", "step-finished"):
                part = evt.get("part") if isinstance(evt.get("part"), dict) else evt
                if isinstance(part, dict):
                    tok = (
                        part.get("tokens")
                        if isinstance(part.get("tokens"), dict)
                        else None
                    )
                    metrics = {
                        "reason": part.get("reason"),
                        "cost": part.get("cost"),
                        "tokens": tok,
                    }
            # Collect assistant-ish text
            t = _extract_text_from_event(evt)
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

    th.join(timeout=5)
    supervisor.stop()

    if events_fp:
        events_fp.close()
    if assistant_fp:
        assistant_fp.close()
    stderr_fp.close()

    exit_code = proc.returncode if proc.returncode is not None else 1
    full_text = tail.get()

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
    # /message response is documented as Message, typically has parts[]
    parts = msg.get("parts")
    if isinstance(parts, list):
        out: list[str] = []
        for p in parts:
            if isinstance(p, dict):
                if p.get("type") == "text" and isinstance(p.get("text"), str):
                    out.append(p["text"])
                # tolerate other shapes
                elif isinstance(p.get("text"), str):
                    out.append(p["text"])
                elif isinstance(p.get("content"), str):
                    out.append(p["content"])
            elif isinstance(p, str):
                out.append(p)
        return "".join(out)
    # fallback
    if isinstance(msg.get("text"), str):
        return str(msg["text"])
    return json.dumps(msg, ensure_ascii=False)


def _run_http(
    *,
    server_url: str,
    workdir: Path,
    env: dict[str, str],
    prompt: str,
    agent: str | None,
    model: str | None,
    variant: str | None,
    timeout_s: float,
    save_events: bool,
    save_text: bool,
    max_artifact_bytes: int,
    events_path: Path | None,
    stderr_path: Path,
    assistant_path: Path | None,
    permission_mode: str,
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

    def _noninteractive_permission_response(evt: dict[str, Any]) -> str:
        """
        Best-effort: align HTTP permission handling with the CLI "noninteractive" preset.

        Goal: avoid hangs (no prompts) while denying a few high-risk classes when we can identify them
        from SSE payloads. If we cannot classify the request, default to "allow" to keep unattended
        runs moving (callers can always choose --permission-mode inherit for interactive safety).
        """
        perm_obj: dict[str, Any] | None = None
        perm = evt.get("permission")
        if isinstance(perm, dict):
            perm_obj = perm
        data = evt.get("data")
        if perm_obj is None and isinstance(data, dict):
            perm2 = data.get("permission")
            if isinstance(perm2, dict):
                perm_obj = perm2

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
        if perm_obj:
            for k in ("permission", "type", "name", "tool", "category", "action"):
                v = perm_obj.get(k)
                if isinstance(v, str) and v:
                    kind = v
                    break
        if not kind and isinstance(perm, str) and perm:
            kind = perm

        kind_l = kind.lower() if isinstance(kind, str) else ""
        if kind_l in ("task", "skill", "external_directory", "doom_loop"):
            return "deny"

        # Deny reads of .env-style secrets when we can detect them.
        # (The CLI preset denies *.env and *.env.* but allows *.env.example.)
        if kind_l.startswith("read") or kind_l == "read":
            texts = [s.lower() for s in _strings_from(perm_obj or {})]
            for s in texts:
                if s.endswith(".env.example"):
                    continue
                if s.endswith(".env") or ".env." in s:
                    return "deny"

        return "allow"

    def maybe_permission_id(evt: dict[str, Any]) -> str | None:
        # Heuristic extraction: permissionID / permissionId / permission.id / data.permission.id
        for k in ("permissionID", "permissionId"):
            v = evt.get(k)
            if isinstance(v, str) and v:
                return v
        perm = evt.get("permission")
        if isinstance(perm, dict):
            v = perm.get("id") or perm.get("permissionID") or perm.get("permissionId")
            if isinstance(v, str) and v:
                return v
        data = evt.get("data")
        if isinstance(data, dict):
            perm2 = data.get("permission")
            if isinstance(perm2, dict):
                v = (
                    perm2.get("id")
                    or perm2.get("permissionID")
                    or perm2.get("permissionId")
                )
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

                    # Filter by sessionID when present; keep server.connected even without sessionID.
                    sid = evt.get("sessionID") or evt.get("sessionId")
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

                    # Auto permission replies (best-effort)
                    if permission_mode in ("allow", "noninteractive"):
                        et = evt.get("type")
                        if isinstance(et, str) and "permission" in et:
                            pid = maybe_permission_id(evt)
                            if pid and pid not in responded_permissions:
                                responded_permissions.add(pid)
                                client.reply_permission(
                                    session_id,
                                    pid,
                                    response=(
                                        "allow"
                                        if permission_mode == "allow"
                                        else _noninteractive_permission_response(evt)
                                    ),
                                    remember=False,
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
        session = client.create_session()
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
        text = _extract_text_from_message_obj(msg_obj)
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


def cmd_run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    output_mode_n = _normalize_output_mode(getattr(args, "output_mode", "machine"))
    diagnostics_mode = _normalize_diagnostics_mode(getattr(args, "diagnostics", None))
    contract_nonce = (
        None
        if output_mode_n == "text"
        else str(getattr(args, "contract_nonce", None) or _make_contract_nonce())
    )
    run_timeout_s = float(args.run_timeout)
    run_started_ms = _now_ms()

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

    # Merge env early so profile thresholds can honor --env / --env-file overrides.
    env = _safe_merge_env(os.environ, set_vars=args.env, set_from_files=args.env_file)
    requested_engine = str(getattr(args, "engine", "auto"))
    profile_info = _apply_execution_profile(args, prompt, env)

    prompt = _apply_persona_policy(prompt, args.persona_mode, args.persona_line)

    if output_mode_n == "machine":
        assert contract_nonce is not None
        prompt = prompt + _default_contract_prompt(contract_nonce)

    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    # Artifacts paths
    job_path = artifacts_dir / "job.json"
    events_path = artifacts_dir / "events.ndjson" if args.save_events else None
    assistant_path = artifacts_dir / "assistant.txt" if args.save_text else None
    stderr_path = artifacts_dir / "stderr.log"
    payload_path = artifacts_dir / "payload.json"
    diagnostics_path = artifacts_dir / "diagnostics.json"
    # wrapper.log is only meaningful in start (background) mode;
    # in foreground run mode the file won't exist, so _artifacts_obj
    # will return null for wrapperLogPath.  But we point to the
    # canonical path so that if a start-mode worker reuses this code
    # path the artifact is correctly referenced when present.
    wrapper_log_path = artifacts_dir / "wrapper.log"

    # Job init
    job_obj = {
        "runId": run_id,
        "adapterVersion": ADAPTER_VERSION,
        "workdir": str(workdir),
        "state": "running",
        "createdAt": _now_ms(),
        "updatedAt": _now_ms(),
        "pid": os.getpid(),
        "engine": args.engine,
        "outputMode": output_mode_n,
        "diagnosticsMode": diagnostics_mode,
        "contractNonce": contract_nonce,
        "stopServerAfterRunMode": str(
            getattr(args, "stop_server_after_run", DEFAULT_STOP_SERVER_AFTER_RUN)
        ),
        "serverStartedNew": False,
        "httpAttempted": False,
        "orphanReaperEnabled": bool(getattr(args, "orphan_reaper", True)),
    }
    _write_job_locked(job_path, artifacts_dir, job_obj)

    def _update_job(fields: dict[str, Any]) -> None:
        _update_job_fields_locked(job_path, artifacts_dir, fields)

    # Env
    # Defensive defaults (no-op if ignored by OpenCode)
    env.setdefault("OPENCODE_CLIENT", "opencode-subtask")
    if args.disable_claude_code:
        env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE", "1")
    _apply_permission_mode(env, args.permission_mode)

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
                output_mode=output_mode_n,
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
                payload_status="not_requested" if output_mode_n == "text" else "missing",
                payload_schema=None,
                payload_path=None,
                payload_digest=None,
                payload_errors=(
                    []
                    if output_mode_n == "text"
                    else [
                        _payload_error(
                            "PAYLOAD_MISSING",
                            "execution failed before any authoritative payload was produced",
                        )
                    ]
                ),
                decision_status="not_requested"
                if output_mode_n == "text"
                else "unavailable",
                decision_route=None,
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
                    payload_path=None,
                    diagnostics_path=None,
                ),
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
            timeout_s=run_timeout_s,
            save_events=args.save_events,
            save_text=args.save_text,
            max_artifact_bytes=int(args.max_artifact_bytes),
            events_path=events_path,
            stderr_path=stderr_path,
            assistant_path=assistant_path,
            permission_mode=args.permission_mode,
            on_session_id=lambda sid: _update_job({"sessionId": sid}),
        )

    changed_files: list[str] = []
    untracked_files: list[str] = []
    patch_name: str | None = None
    extraction = _extract_machine_payload(
        text="",
        nonce=contract_nonce,
        output_mode=output_mode_n,
    )
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

    extraction = _extract_machine_payload(
        text=outcome.full_text,
        nonce=contract_nonce,
        output_mode=output_mode_n,
    )
    payload_file_for_finish: str | None = None
    payload_digest: str | None = None
    if extraction.payload_status == "validated" and isinstance(extraction.payload_obj, dict):
        try:
            _atomic_write_bytes(payload_path, _canonical_json_bytes(extraction.payload_obj))
            payload_digest = _sha256_file(payload_path)
            payload_file_for_finish = payload_path.name
        except Exception as ex:
            extraction = MachinePayloadExtraction(
                payload_status="persist_failed",
                payload_schema=None,
                decision_status="unavailable",
                decision_route=None,
                payload_obj=extraction.payload_obj,
                errors=_dedupe_payload_errors(
                    [
                        *extraction.errors,
                        _payload_error(
                            "PAYLOAD_PERSIST_FAILED",
                            f"{type(ex).__name__}: {ex}",
                        ),
                    ]
                ),
                diagnostics=extraction.diagnostics,
            )

    diagnostics_written_path: Path | None = None
    execution_error = outcome.error
    outcome_name = _execution_outcome_from_error(
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        error=execution_error,
    )
    try:
        diagnostics_written_path = _maybe_write_diagnostics(
            mode=diagnostics_mode,
            diagnostics_path=diagnostics_path,
            run_id=run_id,
            output_mode=output_mode_n,
            execution_failed=(outcome_name != "completed"),
            extraction=extraction,
        )
    except Exception:
        diagnostics_written_path = None

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
        output_mode=output_mode_n,
        outcome=outcome_name,
        exit_code=outcome.exit_code,
        duration_ms=max(0, _now_ms() - run_started_ms),
        engine_selected=outcome.engine,
        fallback_from=fallback_from,
        session_id=outcome.session_id,
        execution_error=execution_error,
        execution_warnings=execution_warnings,
        payload_status=extraction.payload_status,
        payload_schema=extraction.payload_schema,
        payload_path=payload_file_for_finish,
        payload_digest=payload_digest,
        payload_errors=extraction.errors,
        decision_status=extraction.decision_status,
        decision_route=extraction.decision_route,
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
            payload_path=(payload_path if payload_file_for_finish else None),
            diagnostics_path=diagnostics_written_path,
        ),
    )

    finish_written, finish_reason, existing_finish = _write_finish_once(
        artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
    )
    if (not finish_written) and isinstance(existing_finish, dict):
        err = _error_obj(
            error_name="FinishAlreadyPresent",
            message=_finish_already_present_message(finish_path),
        )
        sys.stdout.write(_json_line(err) + "\n")
        return 1
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
            "outputMode": output_mode_n,
            "serverStartedNew": server_started_new,
            "httpAttempted": http_was_attempted,
            "stopServerAfterRunMode": stop_server_mode,
            "emptyOutputDetected": empty_output_detected,
            "emptyOutputRetried": empty_output_retried,
            "emptyOutputRecovered": empty_output_recovered,
            "payloadStatus": extraction.payload_status,
            "decisionStatus": extraction.decision_status,
        }
        if outcome.session_id:
            fields["sessionId"] = outcome.session_id
        if attach_url:
            fields["serverUrl"] = attach_url
        _update_job_fields_locked(job_path, artifacts_dir, fields)

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
    output_mode_n = _normalize_output_mode(getattr(args, "output_mode", "machine"))
    diagnostics_mode = _normalize_diagnostics_mode(getattr(args, "diagnostics", None))
    contract_nonce = (
        None
        if output_mode_n == "text"
        else str(getattr(args, "contract_nonce", None) or _make_contract_nonce())
    )

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

    prompt = _apply_persona_policy(prompt, args.persona_mode, args.persona_line)
    # Note: the worker (`run`) appends the output contract when outputMode=machine.

    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    job_path = artifacts_dir / "job.json"
    wrapper_log_path = artifacts_dir / "wrapper.log"

    _write_job_locked(
        job_path,
        artifacts_dir,
        {
            "runId": run_id,
            "adapterVersion": ADAPTER_VERSION,
            "workdir": str(workdir),
            "state": "queued",
            "outputMode": output_mode_n,
            "diagnosticsMode": diagnostics_mode,
            "contractNonce": contract_nonce,
            "stopServerAfterRunMode": str(
                getattr(args, "stop_server_after_run", DEFAULT_STOP_SERVER_AFTER_RUN)
            ),
            "serverStartedNew": False,
            "orphanReaperEnabled": bool(getattr(args, "orphan_reaper", True)),
            "createdAt": _now_ms(),
            "updatedAt": _now_ms(),
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
        "--execution-profile",
        str(args.execution_profile),
        "--output-mode",
        output_mode_n,
        "--diagnostics",
        diagnostics_mode,
        "--stop-server-after-run",
        str(args.stop_server_after_run),
        "--orphan-reaper-idle-s",
        str(args.orphan_reaper_idle_s),
    ]

    # booleans
    worker_cmd.append("--quiet" if args.quiet else "--no-quiet")
    worker_cmd.append("--save-events" if args.save_events else "--no-save-events")
    worker_cmd.append("--save-text" if args.save_text else "--no-save-text")
    worker_cmd.append("--orphan-reaper" if args.orphan_reaper else "--no-orphan-reaper")
    worker_cmd.append(
        "--disable-claude-code"
        if args.disable_claude_code
        else "--no-disable-claude-code"
    )
    if contract_nonce:
        worker_cmd.extend(["--contract-nonce", contract_nonce])

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

    # passthrough
    if getattr(args, "continue_last", False):
        worker_cmd.append("--continue")
    if getattr(args, "session", None):
        worker_cmd.extend(["--session", str(getattr(args, "session"))])
    if getattr(args, "title", None):
        worker_cmd.extend(["--title", str(getattr(args, "title"))])
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

    out = _start_obj(
        run_id=run_id,
        pid=proc.pid,
        workdir=workdir,
        artifacts_dir=artifacts_dir,
        output_mode=output_mode_n,
        warnings=runtime_preflight_warnings,
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
            payload_path=(artifacts_dir / "payload.json"),
            diagnostics_path=(artifacts_dir / "diagnostics.json"),
        ),
    )
    sys.stdout.write(_json_line(out) + "\n")
    return 0


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
    output_mode = (
        _normalize_output_mode(str(job.get("outputMode")))
        if isinstance(job, dict) and job.get("outputMode")
        else "machine"
    )
    created_ms = _job_ms(job, "createdAt") if isinstance(job, dict) else 0
    duration_ms = (
        max(0, _now_ms() - created_ms) if created_ms > 0 else 0
    )
    out = _finish_obj(
        run_id=run_id,
        workdir=wd,
        output_mode=output_mode,
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
        payload_status="not_requested" if output_mode == "text" else "missing",
        payload_schema=None,
        payload_path=None,
        payload_digest=None,
        payload_errors=(
            []
            if output_mode == "text"
            else [_payload_error("PAYLOAD_MISSING", error_message)]
        ),
        decision_status="not_requested" if output_mode == "text" else "unavailable",
        decision_route=None,
        changed_files=[],
        untracked_files=[],
        patch_path=None,
        artifacts=_minimal_artifacts(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
        ),
    )
    finish_written, finish_reason, existing_finish = _write_finish_once(
        artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
    )
    if (not finish_written) and isinstance(existing_finish, dict):
        out = existing_finish
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
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id or --artifacts-dir", exit_code=2
        )

    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)
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
                payload_path=artifacts_dir / "payload.json",
                diagnostics_path=artifacts_dir / "diagnostics.json",
            ),
            progress=_progress_snapshot(artifacts_dir),
            warnings=runtime_warnings or None,
            error={"name": "JobNotFound", "message": "job.json not found"},
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    job = _load_json(job_path) or {}
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
            payload_path=artifacts_dir / "payload.json",
            diagnostics_path=artifacts_dir / "diagnostics.json",
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
    )
    sys.stdout.write(_json_line(out) + "\n")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id or --artifacts-dir", exit_code=2
        )

    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)

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
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 0

        time.sleep(poll_interval)


def cmd_cancel(args: argparse.Namespace) -> int:
    if not getattr(args, "run_id", None) and not getattr(args, "artifacts_dir", None):
        _exit_with_error(
            "MissingRunId", "Provide --run-id or --artifacts-dir", exit_code=2
        )

    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"

    job = _load_json(job_path) or {}
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
    output_mode = (
        _normalize_output_mode(str(job.get("outputMode")))
        if isinstance(job, dict) and job.get("outputMode")
        else "machine"
    )
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
            output_mode=output_mode,
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
            payload_status="not_requested" if output_mode == "text" else "missing",
            payload_schema=None,
            payload_path=None,
            payload_digest=None,
            payload_errors=(
                []
                if output_mode == "text"
                else [_payload_error("PAYLOAD_MISSING", task_error["message"])]
            ),
            decision_status="not_requested" if output_mode == "text" else "unavailable",
            decision_route=None,
            changed_files=[],
            untracked_files=[],
            patch_path=None,
            artifacts=_minimal_artifacts(
                dir_path=artifacts_dir,
                job_path=job_path,
                finish_path=finish_path,
            ),
        )
        cancel_fin_written, cancel_fin_reason, _ = _write_finish_once(
            artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
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
        # stays populated on ok=false (backward-compatible).
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


def _judgment_obj(
    *,
    policy: str,
    verdict: str,
    reason_code: str,
    route: str | None,
    retryable: bool,
) -> dict[str, Any]:
    return {
        "type": "opencode-subtask-judgment",
        "schemaVersion": 1,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "policy": policy,
        "verdict": verdict,
        "reasonCode": reason_code,
        "route": route if route in DECISION_ROUTES else None,
        "retryable": bool(retryable),
    }


def _judge_exit_code(verdict: str) -> int:
    return {
        "accept": 0,
        "reroute": 10,
        "retry": 11,
        "block": 12,
    }.get(str(verdict), 1)


def _judge_reason(name: str) -> str:
    code = JUDGE_REASON.get(name)
    if code is None:
        raise ValueError(f"unknown judge reason key: {name}")
    return code


def _payload_digest_matches(finish: dict[str, Any], finish_path: Path) -> bool:
    payload = finish.get("payload") if isinstance(finish.get("payload"), dict) else {}
    artifact = payload.get("artifact") if isinstance(payload, dict) else {}
    rel_path = artifact.get("path") if isinstance(artifact, dict) else None
    digest = artifact.get("digest") if isinstance(artifact, dict) else None
    if not isinstance(rel_path, str) or not rel_path or not isinstance(digest, str) or not digest:
        return False
    artifacts = finish.get("artifacts") if isinstance(finish.get("artifacts"), dict) else {}
    base_dir = Path(str(artifacts.get("dir"))) if artifacts and artifacts.get("dir") else finish_path.parent
    candidate = Path(rel_path)
    payload_path = candidate if candidate.is_absolute() else (base_dir / rel_path)
    if not payload_path.exists():
        return False
    try:
        return _sha256_file(payload_path) == digest
    except Exception:
        return False


def cmd_judge(args: argparse.Namespace) -> int:
    finish_path = Path(args.finish).expanduser().resolve()
    policy = str(args.policy).strip()
    if not finish_path.exists():
        out = _judgment_obj(
            policy=policy,
            verdict="retry",
            reason_code=_judge_reason("finish_not_found"),
            route=None,
            retryable=True,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])

    finish, finish_error = _read_finish_envelope(finish_path)
    if finish_error == "FINISH_UNREADABLE":
        out = _judgment_obj(
            policy=policy,
            verdict="retry",
            reason_code=_judge_reason("finish_unreadable"),
            route=None,
            retryable=True,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])
    if finish_error == "FINISH_INVALID" or not isinstance(finish, dict):
        out = _judgment_obj(
            policy=policy,
            verdict="retry",
            reason_code=_judge_reason("finish_invalid"),
            route=None,
            retryable=True,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])

    outcome = str(finish.get("outcome") or "")
    output_mode = str(finish.get("outputMode") or "machine")
    payload = finish.get("payload") if isinstance(finish.get("payload"), dict) else {}
    decision = finish.get("decision") if isinstance(finish.get("decision"), dict) else {}
    payload_status = str(payload.get("status") or "")
    decision_status = str(decision.get("status") or "")
    route = decision.get("route") if isinstance(decision.get("route"), str) else None

    if policy not in JUDGE_POLICIES:
        out = _judgment_obj(
            policy=policy,
            verdict="retry",
            reason_code=_judge_reason("unknown_policy"),
            route=route,
            retryable=True,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])

    if policy != "execution-only" and payload_status == "validated":
        if not _payload_digest_matches(finish, finish_path):
            out = _judgment_obj(
                policy=policy,
                verdict="retry",
                reason_code=_judge_reason("payload_digest_mismatch"),
                route=route,
                retryable=True,
            )
            sys.stdout.write(_json_line(out) + "\n")
            return _judge_exit_code(out["verdict"])

    if policy == "execution-only":
        if outcome == "completed":
            verdict, reason_code = "accept", _judge_reason("execution_completed")
        elif outcome in {"failed", "timed_out", "internal_error"}:
            verdict, reason_code = "retry", JUDGE_EXECUTION_REASON_BY_OUTCOME[outcome]
        elif outcome == "cancelled":
            verdict, reason_code = "block", _judge_reason("execution_cancelled")
        else:
            verdict, reason_code = "retry", _judge_reason("execution_unknown")
        out = _judgment_obj(
            policy=policy,
            verdict=verdict,
            reason_code=reason_code,
            route=route,
            retryable=(verdict == "retry"),
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])

    if outcome in {"failed", "timed_out", "internal_error"}:
        out = _judgment_obj(
            policy=policy,
            verdict="retry",
            reason_code=JUDGE_EXECUTION_REASON_BY_OUTCOME[outcome],
            route=route,
            retryable=True,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])
    if outcome == "cancelled":
        out = _judgment_obj(
            policy=policy,
            verdict="block",
            reason_code=_judge_reason("execution_cancelled"),
            route=route,
            retryable=False,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return _judge_exit_code(out["verdict"])

    if policy == "require-determinate":
        if output_mode == "text" or payload_status == "not_requested":
            verdict, reason_code = "block", _judge_reason("output_not_machine")
        elif payload_status in {"missing", "malformed", "ambiguous", "persist_failed"}:
            verdict, reason_code = "retry", JUDGE_PAYLOAD_REASON_BY_STATUS[payload_status]
        elif payload_status == "validated" and decision_status == "determinate" and route == "GO_NO_DELTA":
            verdict, reason_code = "accept", _judge_reason("decision_go_no_delta")
        elif payload_status == "validated" and decision_status == "determinate" and route == "MANDATORY_DELTA":
            verdict, reason_code = "reroute", _judge_reason("decision_mandatory_delta")
        elif payload_status == "validated" and decision_status == "abstained":
            verdict, reason_code = "reroute", _judge_reason("decision_undetermined")
        else:
            verdict, reason_code = "retry", _judge_reason("decision_unavailable")
    else:
        if output_mode == "text" or payload_status == "not_requested":
            verdict, reason_code = "block", _judge_reason("output_not_machine")
        elif payload_status in {"missing", "malformed", "ambiguous", "persist_failed"}:
            verdict, reason_code = "retry", JUDGE_PAYLOAD_REASON_BY_STATUS[payload_status]
        elif payload_status == "validated" and decision_status == "determinate" and route == "GO_NO_DELTA":
            verdict, reason_code = "accept", _judge_reason("decision_go_no_delta")
        elif payload_status == "validated" and decision_status == "determinate" and route == "MANDATORY_DELTA":
            verdict, reason_code = "block", _judge_reason("decision_mandatory_delta")
        elif decision_status == "abstained":
            verdict, reason_code = "block", _judge_reason("decision_undetermined")
        else:
            verdict, reason_code = "retry", _judge_reason("decision_unavailable")

    out = _judgment_obj(
        policy=policy,
        verdict=verdict,
        reason_code=reason_code,
        route=route,
        retryable=(verdict == "retry"),
    )
    sys.stdout.write(_json_line(out) + "\n")
    return _judge_exit_code(out["verdict"])


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

    # Session continuity (optional). These map to `opencode run` flags and only affect the CLI engine.
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
        help="(CLI engine) Continue a specific opencode session id.",
    )
    p.add_argument("--title", default=None, help="(CLI engine) Title for the session.")

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
        help="Quiet mode (stdout only final one-line JSON).",
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
        "--permission-mode",
        choices=["inherit", "allow", "noninteractive"],
        default="inherit",
        help="Permission handling. HTTP engine auto-replies via API when possible.",
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
        "--output-mode",
        choices=["machine", "text"],
        default="machine",
        help=(
            "machine: request authoritative nonce-bound JSON payload; "
            "text: freeform assistant text only."
        ),
    )
    p.add_argument(
        "--diagnostics",
        choices=["never", "on-failure", "always"],
        default="on-failure",
        help="Diagnostics sidecar emission policy.",
    )
    p.add_argument(
        "--contract-nonce",
        default=None,
        help=argparse.SUPPRESS,
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

        p_start = sub.add_parser("start", help="Start a subtask in background.")
        _add_common_run_flags(p_start)
        p_start.add_argument(
            "prompt",
            nargs=argparse.REMAINDER,
            help="Prompt args (use `--` before the prompt).",
        )
        p_start.set_defaults(func=cmd_start)

        p_wait = sub.add_parser("wait", help="Wait for a background job finish.json.")
        p_wait.add_argument("--run-id", required=False, default=None)
        p_wait.add_argument("--artifacts-dir", required=False, default=None)
        p_wait.add_argument(
            "--wait-timeout",
            type=float,
            default=DEFAULT_TIMEOUT_S,
            help="Wait window seconds for wait command.",
        )
        p_wait.add_argument("--poll-interval", type=float, default=0.5)
        p_wait.set_defaults(func=cmd_wait)

        p_status = sub.add_parser("status", help="Show job status/progress.")
        p_status.add_argument("--run-id", required=False, default=None)
        p_status.add_argument("--artifacts-dir", required=False, default=None)
        p_status.set_defaults(func=cmd_status)

        p_cancel = sub.add_parser(
            "cancel",
            help="Cancel a job by killing worker and aborting session if known.",
        )
        p_cancel.add_argument("--run-id", required=False, default=None)
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

        p_judge = sub.add_parser(
            "judge",
            help="Apply a built-in policy preset to a finish.json envelope.",
        )
        p_judge.add_argument("--finish", required=True, help="Path to finish.json")
        p_judge.add_argument(
            "--policy",
            choices=[
                "execution-only",
                "require-determinate",
                "require-go-no-delta",
            ],
            required=True,
        )
        p_judge.set_defaults(func=cmd_judge)

        p_es = sub.add_parser(
            "ensure-server", help="Ensure a per-project opencode server."
        )
        p_es.add_argument("--opencode", default="opencode")
        p_es.add_argument("--workdir", default=".")
        p_es.add_argument("--server-hostname", default=DEFAULT_SERVER_HOSTNAME)
        p_es.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
        p_es.add_argument("--server-wait", type=float, default=DEFAULT_SERVER_WAIT_S)
        p_es.add_argument("--env", action="append", default=[])
        p_es.add_argument("--env-file", action="append", default=[])
        p_es.set_defaults(func=cmd_ensure_server)

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
