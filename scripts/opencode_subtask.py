#!/usr/bin/env python3
"""
opencode_subtask.py

A Codex-friendly adapter around OpenCode that provides:
- Stable one-line JSON output (ASCII-only) with schemaVersion.
- Artifacts-first logging (events/stderr/assistant/result/patch) to avoid caller context bloat.
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
from typing import Any, Final, Iterable, Never, NoReturn

# ============================
# Constants / schema
# ============================

ADAPTER_SCHEMA_VERSION: Final[int] = 1
ADAPTER_VERSION: Final[str] = "0.5.16"

DEFAULT_TIMEOUT_S: Final[float] = 600.0
DEFAULT_MAX_TEXT_CHARS: Final[int] = 1000
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


def _exit_with_error(error_name: str, message: str, exit_code: int = 1) -> Never:
    """Print a JSON error to stdout and exit. Maintains stdout contract."""
    obj = {
        "type": "opencode-subtask-error",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "ok": False,
        "error": {"name": error_name, "message": message},
    }
    sys.stdout.write(_json_line(obj) + "\n")
    sys.exit(exit_code)


class _JsonArgumentParser(argparse.ArgumentParser):
    """
    ArgumentParser that preserves adapter stdout JSON contract on CLI parse errors.
    """

    def error(self, message: str) -> Never:  # pragma: no cover - thin adapter
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


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", True
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars - 1] + "…", True


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
    if profile not in ("legacy", "hybrid", "latency", "checkpoint"):
        profile = DEFAULT_EXECUTION_PROFILE

    prompt_chars = len(prompt)
    timeout_s = float(getattr(args, "timeout", DEFAULT_TIMEOUT_S))
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

    # Preserve old behavior.
    if profile == "legacy":
        return {
            "profile": profile,
            "taskClass": "legacy",
            "promptChars": prompt_chars,
            "timeoutS": timeout_s,
            "hybridShortTimeoutS": hybrid_short_timeout_s,
            "hybridShortPromptChars": hybrid_short_prompt_chars,
            "hybridThresholdSource": {
                "timeoutS": hybrid_timeout_source,
                "promptChars": hybrid_prompt_source,
            },
        }

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
            existing = _load_json(finish_path)
            if isinstance(existing, dict):
                return False, "exists", existing
            # Corrupt/unreadable: rename to preserve forensic evidence,
            # then write the new finish so the system converges to a
            # terminal state instead of being stuck forever.
            corrupt_name = f"finish.corrupt.{_now_ms()}.json"
            corrupt_path = artifacts_dir / corrupt_name
            try:
                os.replace(finish_path, corrupt_path)
            except Exception:
                try:
                    finish_path.unlink()
                except Exception:
                    return False, "unreadable", None
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


def _finish_unreadable_error() -> dict[str, str]:
    return {
        "name": "FinishUnreadable",
        "message": "finish.json exists but is not readable JSON",
    }


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


def _is_empty_model_output(
    outcome: RunOutcome, result_obj: dict[str, Any] | None, summary_source: str
) -> bool:
    """
    Detect model-completed-but-empty responses so callers don't treat them as success.
    Conservative gating: only triggers on successful, non-timeout outcomes with no
    assistant text / structured payload / summary signal.
    """
    if (not outcome.ok) or outcome.timed_out or (outcome.error is not None):
        return False
    if (outcome.full_text or "").strip():
        return False
    if (summary_source or "").strip():
        return False

    out_tokens = _metric_output_tokens(outcome.metrics)
    if out_tokens is not None and out_tokens > 0:
        return False

    if isinstance(result_obj, dict):
        for k, v in result_obj.items():
            if k in ("changed_files", "untracked_files"):
                continue
            if isinstance(v, str) and v.strip():
                return False
            if isinstance(v, (list, dict)) and len(v) > 0:
                return False
            if isinstance(v, (int, float)) and v != 0:
                return False
            if isinstance(v, bool) and v:
                return False
            if v is None:
                continue
    return True


def _extract_structured_json(
    text: str, *, max_scan_chars: int = 80_000
) -> dict[str, Any] | None:
    """
    Extraction order:
    1) Sentinel block between BEGIN_OC_SUBTASK_JSON and END_OC_SUBTASK_JSON.
    2) Last fenced JSON block.
    3) Backward scan for a JSON object.
    """
    if not text:
        return None

    # 1) Sentinel.
    b = text.rfind(SENTINEL_BEGIN)
    if b != -1:
        e = text.find(SENTINEL_END, b + len(SENTINEL_BEGIN))
        if e != -1:
            payload = text[b + len(SENTINEL_BEGIN) : e].strip()
            try:
                obj = json.loads(payload)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    # 2) Fenced blocks.
    blocks = JSON_FENCE_RE.findall(text)
    for blk in reversed(blocks):
        blk = blk.strip()
        if not blk:
            continue
        try:
            obj = json.loads(blk)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    # 3) Backward scan.
    window = text[-max_scan_chars:] if len(text) > max_scan_chars else text
    # scan back for '{'
    for i in range(len(window) - 1, -1, -1):
        if window[i] != "{":
            continue
        candidate = window[i:]
        # trim trailing noise
        candidate = candidate.strip()
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _default_contract_prompt() -> str:
    return (
        "\n\n"
        "SUBTASK OUTPUT CONTRACT (strict):\n"
        f"At the very end, output exactly:\n"
        f"{SENTINEL_BEGIN}\n"
        "{...one JSON object...}\n"
        f"{SENTINEL_END}\n"
        "No markdown, no code fences, no extra text after the END marker.\n"
        "Schema:\n"
        "{\n"
        '  "summary": string (<= 800 chars),\n'
        '  "evidence": string[] (each: "path:line - fact", <= 10 items),\n'
        '  "changes": string[] (<= 10 items),\n'
        '  "next_steps": string[] (<= 10 items)\n'
        "}\n"
        "If you cannot comply, output an object with empty arrays.\n"
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
    """Artifact dict for edge/error paths where optional file paths are unavailable.

    Returns the same key set as _artifacts_obj so consumers see a stable shape.
    """
    return {
        "dir": str(dir_path),
        "jobPath": job_path.name,
        "finishPath": finish_path.name,
        "promptPath": None,
        "eventsPath": None,
        "stderrPath": None,
        "assistantPath": None,
        "wrapperLogPath": None,
        "resultPath": None,
        "patchPath": None,
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
    result_path: Path | None,
    patch_path: str | None,
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
        "resultPath": _name_if_exists(result_path),
        "patchPath": patch_path,
    }


def _finish_obj(
    *,
    ok: bool,
    exit_code: int,
    timed_out: bool,
    run_id: str,
    workdir: Path,
    engine: str,
    fallback_from: str | None,
    server: dict[str, Any] | None,
    session_id: str | None,
    summary: str,
    summary_truncated: bool,
    result_digest: str | None,
    changed_files: list[str],
    untracked_files: list[str],
    warnings: list[dict[str, str]] | None = None,
    artifacts_dir: Path,
    artifacts: dict[str, Any],
    metrics: dict[str, Any] | None,
    error: dict[str, Any] | None,
    include_debug: bool,
    debug: dict[str, Any] | None,
) -> dict[str, Any]:
    warnings_out = warnings or []
    obj: dict[str, Any] = {
        "type": "opencode-subtask-finish",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": ok,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "runId": run_id,
        "workdir": str(workdir),
        "engine": {"selected": engine, "fallbackFrom": fallback_from},
        "sessionId": session_id,
        "summary": summary,
        "summaryTruncated": summary_truncated,
        "resultDigest": result_digest,
        "changedFiles": changed_files,
        "untrackedFiles": untracked_files,
        "warnings": warnings_out,
        "artifacts": artifacts,
        "server": {
            "url": (server or {}).get("url"),
            "version": (server or {}).get("version"),
            "logPath": (server or {}).get("logPath"),
        }
        if server
        else None,
        "metrics": metrics,
        "error": error,
    }
    if include_debug and isinstance(debug, dict):
        obj["debug"] = debug
    return obj


def _start_obj(
    *,
    run_id: str,
    pid: int,
    workdir: Path,
    artifacts_dir: Path,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "opencode-subtask-start",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "adapterVersion": ADAPTER_VERSION,
        "timestamp": _now_ms(),
        "ok": True,
        "runId": run_id,
        "pid": pid,
        "workdir": str(workdir),
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
) -> dict[str, Any]:
    warnings_out = warnings or []
    return {
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


def _enforce_artifact_cap(
    paths: Iterable[Path], max_bytes: int
) -> tuple[bool, str | None]:
    if max_bytes <= 0:
        return True, None
    for p in paths:
        try:
            if p.exists() and p.stat().st_size > max_bytes:
                return False, p.name
        except Exception:
            continue
    return True, None


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
    session_id: str | None = None
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

    def reader() -> None:
        nonlocal session_id, error_event, metrics, killed_for_size, killed_file
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
            if isinstance(sid, str) and sid and not session_id:
                session_id = sid
                if on_session_id:
                    try:
                        on_session_id(session_id)
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
            # Artifact cap: check during streaming (events/stderr/assistant)
            ok_cap, which_file = _enforce_artifact_cap(
                [p for p in [events_path, stderr_path, assistant_path] if p],
                max_artifact_bytes,
            )
            if not ok_cap and which_file:
                killed_for_size = True
                killed_file = which_file
                try:
                    sent = _kill_tree(
                        proc.pid, sig=(signal.SIGKILL if os.name != "nt" else None)
                    )
                    if (not sent) and proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                break

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
            err = {"name": "NonZeroExit", "message": f"opencode exit_code={exit_code}"}

    return RunOutcome(
        ok=ok,
        exit_code=exit_code,
        timed_out=timed_out,
        engine="cli",
        fallback_from=None,
        session_id=session_id,
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
    include_debug: bool,
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

                    # Artifact cap
                    ok_cap, which_file = _enforce_artifact_cap(
                        [p for p in [events_path, stderr_path, assistant_path] if p],
                        max_artifact_bytes,
                    )
                    if not ok_cap and which_file:
                        try:
                            stderr_fp.write(
                                f"OutputTooLarge: {which_file}\n".encode("utf-8")
                            )
                            stderr_fp.flush()
                        except Exception:
                            pass
                        try:
                            client.abort(session_id)
                        except Exception:
                            pass
                        stop_evt.set()
                        break

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
        if on_session_id:
            try:
                on_session_id(session_id)
            except Exception:
                pass
    except Exception as e:
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

    if err is None and isinstance(msg_obj, dict):
        text = _extract_text_from_message_obj(msg_obj)
        if assistant_fp:
            try:
                assistant_fp.write(text.encode("utf-8", errors="replace"))
            except Exception:
                pass
        # Artifact cap: also check response size indirectly (assistant file size)
        ok_cap, which_file = _enforce_artifact_cap(
            [p for p in [events_path, stderr_path, assistant_path] if p],
            max_artifact_bytes,
        )
        if not ok_cap and which_file:
            client.abort(session_id)
            err = {
                "name": "OutputTooLarge",
                "message": f"artifact {which_file} exceeded max-artifact-bytes={max_artifact_bytes}",
            }
        full_text = text
    else:
        full_text = ""

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
    run_timeout_s = (
        float(args.run_timeout)
        if getattr(args, "run_timeout", None) is not None
        else float(args.timeout)
    )

    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Prompt
    prompt = _resolve_prompt_input(args)

    # Merge env early so profile thresholds can honor --env / --env-file overrides.
    env = _safe_merge_env(os.environ, set_vars=args.env, set_from_files=args.env_file)
    # Keep profile short-task classification aligned with effective runtime timeout.
    args.timeout = run_timeout_s
    requested_engine = str(getattr(args, "engine", "auto"))
    profile_info = _apply_execution_profile(args, prompt, env)

    prompt = _apply_persona_policy(prompt, args.persona_mode, args.persona_line)

    if not args.no_contract:
        prompt = prompt + _default_contract_prompt()

    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    # Artifacts paths
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
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
    job_obj = {
        "runId": run_id,
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
                ok=False,
                exit_code=127,
                timed_out=False,
                run_id=run_id,
                workdir=workdir,
                engine="none",
                fallback_from=None,
                server=None,
                session_id=None,
                summary="",
                summary_truncated=False,
                result_digest=None,
                changed_files=[],
                untracked_files=[],
                artifacts_dir=artifacts_dir,
                artifacts=_artifacts_obj(
                    dir_path=artifacts_dir,
                    job_path=job_path,
                    finish_path=finish_path,
                    prompt_path=prompt_path,
                    events_path=events_path,
                    stderr_path=stderr_path,
                    assistant_path=assistant_path,
                    wrapper_log_path=wrapper_log_path,
                    result_path=None,
                    patch_path=None,
                ),
                metrics=None,
                error={
                    "name": "OpencodeNotFound",
                    "message": f"Could not find opencode executable: {default_cmd}",
                },
                include_debug=args.include_debug,
                debug=None,
            )
            finish_written, _, existing_finish = _write_finish_once(
                artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
            )
            if (not finish_written) and isinstance(existing_finish, dict):
                out = existing_finish
                final_ok = bool(out.get("ok") is True)
                sys.stdout.write(_json_line(out) + "\n")
                return 0 if final_ok else 1
            sys.stdout.write(_json_line(out) + "\n")
            return 1  # OpencodeNotFound is an internal/runtime error

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
            include_debug=args.include_debug,
            on_session_id=lambda sid: _update_job({"sessionId": sid}),
        )

    changed_files: list[str] = []
    untracked_files: list[str] = []
    patch_name: str | None = None
    result_obj: dict[str, Any] | None = None
    summary_source = ""
    summary = ""
    truncated = False
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

        result_obj = _extract_structured_json(outcome.full_text)
        if isinstance(result_obj, dict) and isinstance(result_obj.get("summary"), str):
            summary_source = str(result_obj.get("summary"))
        else:
            summary_source = outcome.full_text
        summary, truncated = _truncate(summary_source.strip(), int(args.max_text_chars))
        changed_files, untracked_files = _git_status(workdir)
        patch_name = _git_patch(workdir, artifacts_dir)

        empty_now = _is_empty_model_output(outcome, result_obj, summary_source)
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

    # Persist structured result to result.json (always adapter-written)
    result_path = artifacts_dir / "result.json"
    result_digest: str | None = None
    try:
        record: dict[str, Any]
        if isinstance(result_obj, dict):
            record = dict(result_obj)
        else:
            record = {
                "summary": summary,
                "evidence": [],
                "changes": [],
                "next_steps": [],
                "raw": result_obj,
            }
        # Enrich
        record.setdefault("changed_files", changed_files)
        record.setdefault("untracked_files", untracked_files)
        _write_json(result_path, record)
        result_digest = _sha256_file(result_path)
    except Exception:
        result_path = None  # type: ignore[assignment]
        result_digest = None

    # Final ok
    ok = outcome.ok and (outcome.error is None)

    # Construct finish
    debug: dict[str, Any] | None = None
    if args.include_debug:
        debug = {
            "engineSelected": outcome.engine,
            "fallbackFrom": fallback_from,
            "httpFallbackError": http_fallback_error,
            "serverError": server_error,
            "serverUrl": attach_url,
            "serverStartedNew": server_started_new,
            "stopServerAfterRunMode": stop_server_mode,
            "executionProfile": profile_info,
            "orphanReaper": reaper_info,
            "emptyOutputDetected": empty_output_detected,
            "emptyOutputRetried": empty_output_retried,
            "emptyOutputRecovered": empty_output_recovered,
            "emptyOutputRetryAttempts": empty_output_attempts,
            "emptyOutputRetryMax": max_empty_output_retries,
            "serverHealth": (
                OpencodeHttpClient(
                    attach_url, auth=_get_http_auth_from_env(env)
                ).health()
                if attach_url
                else None
            ),
        }

    out = _finish_obj(
        ok=ok,
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        run_id=run_id,
        workdir=workdir,
        engine=outcome.engine,
        fallback_from=fallback_from,
        server=server_state,
        session_id=outcome.session_id,
        summary=summary,
        summary_truncated=truncated,
        result_digest=result_digest,
        changed_files=changed_files,
        untracked_files=untracked_files,
        artifacts_dir=artifacts_dir,
        artifacts=_artifacts_obj(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
            prompt_path=prompt_path,
            events_path=events_path,
            stderr_path=stderr_path,
            assistant_path=assistant_path,
            wrapper_log_path=wrapper_log_path,
            result_path=result_path if isinstance(result_path, Path) else None,
            patch_path=patch_name,
        ),
        metrics=outcome.metrics,
        error=outcome.error,
        include_debug=args.include_debug,
        debug=debug,
    )

    finish_written, finish_reason, existing_finish = _write_finish_once(
        artifacts_dir=artifacts_dir, finish_path=finish_path, finish_obj=out
    )
    if (not finish_written) and isinstance(existing_finish, dict):
        out = existing_finish
    elif (not finish_written) and existing_finish is None:
        # write_failed / unreadable — disk has no finish.json; degrade stdout
        out["ok"] = False
        if not out.get("error"):
            out["error"] = {
                "name": "FinishWriteFailed",
                "message": (
                    f"finish.json could not be persisted (reason={finish_reason})"
                ),
            }
    # Update job state only if this worker won the terminal finish write.
    if finish_written:
        fields: dict[str, Any] = {
            "state": "finished",
            "ok": ok,
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

    final_ok = bool(out.get("ok") is True) if isinstance(out, dict) else bool(ok)
    sys.stdout.write(_json_line(out) + "\n")
    return 0 if final_ok else 1


def cmd_start(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()

    run_id, artifacts_dir = _safe_resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Prompt
    prompt = _resolve_prompt_input(args)

    prompt = _apply_persona_policy(prompt, args.persona_mode, args.persona_line)
    # Note: the worker (`run`) will append the output contract unless --no-contract is set.

    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    wrapper_log_path = artifacts_dir / "wrapper.log"

    _write_job_locked(
        job_path,
        artifacts_dir,
        {
            "runId": run_id,
            "adapterVersion": ADAPTER_VERSION,
            "workdir": str(workdir),
            "state": "queued",
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
    run_timeout_s = (
        float(args.run_timeout)
        if getattr(args, "run_timeout", None) is not None
        else float(args.timeout)
    )

    # Apply execution profile so that worker_cmd flags (--engine,
    # --save-events, --save-text) and the start stdout metadata match what
    # the worker will actually compute in cmd_run.  The function mutates
    # args in place and is idempotent, so the worker calling it again is
    # harmless.
    # Important: _apply_execution_profile() reads args.timeout, so align it
    # with the actual worker runtime timeout (run_timeout_s) before applying.
    args.timeout = float(run_timeout_s)
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
        "--timeout",
        str(run_timeout_s),
        "--max-text-chars",
        str(args.max_text_chars),
        "--max-artifact-bytes",
        str(args.max_artifact_bytes),
        "--permission-mode",
        args.permission_mode,
        "--execution-profile",
        str(args.execution_profile),
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
    if args.include_debug:
        worker_cmd.append("--include-debug")
    if args.no_contract:
        worker_cmd.append("--no-contract")

    # prompt persona policy (keep worker behavior consistent with start/run)
    worker_cmd.extend(
        [
            "--persona-mode",
            str(args.persona_mode),
            "--persona-line",
            str(args.persona_line),
        ]
    )
    if args.wrapper_log:
        worker_cmd.append("--wrapper-log")
    if args.workaround_agent_attach:
        worker_cmd.append("--workaround-agent-attach")
    else:
        worker_cmd.append("--no-workaround-agent-attach")
    worker_cmd.append(
        "--retry-empty-output" if args.retry_empty_output else "--no-retry-empty-output"
    )
    worker_cmd.extend(["--empty-output-retries", str(args.empty_output_retries)])
    if getattr(args, "run_timeout", None) is not None:
        worker_cmd.extend(["--run-timeout", str(args.run_timeout)])

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
            result_path=(artifacts_dir / "result.json"),
            patch_path=None,
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
    out = _finish_obj(
        ok=False,
        exit_code=1,
        timed_out=False,
        run_id=run_id,
        workdir=wd,
        engine="watchdog",
        fallback_from=None,
        server=None,
        session_id=(
            str(job.get("sessionId"))
            if isinstance(job, dict) and job.get("sessionId")
            else None
        ),
        summary="",
        summary_truncated=False,
        result_digest=None,
        changed_files=[],
        untracked_files=[],
        artifacts_dir=artifacts_dir,
        artifacts=_minimal_artifacts(
            dir_path=artifacts_dir,
            job_path=job_path,
            finish_path=finish_path,
        ),
        metrics=None,
        error={"name": error_name, "message": error_message},
        include_debug=False,
        debug=None,
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

    if finish_path.exists():
        fin = _load_json(finish_path)
        if isinstance(fin, dict):
            sys.stdout.write(_json_line(fin) + "\n")
            # Exit 0: status successfully observed terminal state.
            # Task success/failure is in the JSON (ok/error), not the exit code.
            return 0
        out = _status_obj(
            ok=False,
            run_id=run_id,
            status="failed",
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
                result_path=artifacts_dir / "result.json",
                patch_path=None,
            ),
            progress=_progress_snapshot(artifacts_dir),
            error=_finish_unreadable_error(),
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

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
                result_path=artifacts_dir / "result.json",
                patch_path=None,
            ),
            progress=_progress_snapshot(artifacts_dir),
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
            return 1
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
            result_path=artifacts_dir / "result.json",
            patch_path=None,
        ),
        progress=progress,
        warnings=(
            [
                {
                    "name": "WorkerMissingPending",
                    "message": "pid is not running; waiting stale-window before synthesizing finish",
                }
            ]
            if _worker_missing
            else None
        ),
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

    if not job_path.exists() and not finish_path.exists():
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
            error={
                "name": "JobNotFound",
                "message": "job.json and finish.json not found",
            },
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    wait_timeout_s = (
        float(args.wait_timeout)
        if getattr(args, "wait_timeout", None) is not None
        else float(args.timeout)
    )
    deadline = time.monotonic() + wait_timeout_s
    while True:
        if finish_path.exists():
            fin = _load_json(finish_path)
            if isinstance(fin, dict):
                sys.stdout.write(_json_line(fin) + "\n")
                # Exit 0: wait successfully observed terminal state.
                # Task success/failure is in the JSON (ok/error), not the exit code.
                return 0
            out = _status_obj(
                ok=False,
                run_id=run_id,
                status="failed",
                pid=None,
                workdir=None,
                artifacts_dir=artifacts_dir,
                artifacts=_minimal_artifacts(
                    dir_path=artifacts_dir,
                    job_path=job_path,
                    finish_path=finish_path,
                ),
                error=_finish_unreadable_error(),
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 1

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
                return 1

        if time.monotonic() >= deadline:
            out = _status_obj(
                ok=False,
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
                error={
                    "name": "WaitTimeout",
                    "message": f"timeout waiting for finish.json (timeout={wait_timeout_s}s)",
                },
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 1

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

    # ── Idempotency guard ─────────────────────────────────────────────
    # If a valid finish.json already exists the subtask has already
    # reached a terminal state.  Killing the (possibly recycled) PID or
    # overwriting job.state would be harmful.  Return early with
    # ``alreadyFinished=true`` so the caller knows cancel was a no-op.
    #
    # We require the finish to contain at least an ``ok`` key — an empty
    # or structurally incomplete dict (e.g. ``{}``) is treated as corrupt
    # and the cancel proceeds normally so the process is still cleaned up.
    existing_finish = _load_json(finish_path)
    if isinstance(existing_finish, dict) and "ok" in existing_finish:
        out_idem = {
            "type": "opencode-subtask-cancel",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "timestamp": _now_ms(),
            "runId": run_id,
            "ok": True,
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
            ok=False,
            exit_code=130 if ok else 1,
            timed_out=False,
            run_id=run_id,
            workdir=Path(str(job.get("workdir")))
            if isinstance(job, dict) and job.get("workdir")
            else Path.cwd(),
            engine="cancel",
            fallback_from=None,
            server=None,
            session_id=session_id,
            summary="Canceled",
            summary_truncated=False,
            result_digest=None,
            changed_files=[],
            untracked_files=[],
            artifacts_dir=artifacts_dir,
            artifacts=_minimal_artifacts(
                dir_path=artifacts_dir,
                job_path=job_path,
                finish_path=finish_path,
            ),
            metrics=None,
            error=task_error,
            include_debug=False,
            debug=None,
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
        # Present when a cancel finish was generated, null otherwise.
        "taskError": task_error,
    }
    # error: cancel-command failure.  Invariant: ok=true => error absent.
    if not ok and cancel_error:
        out2["error"] = cancel_error
    sys.stdout.write(_json_line(out2) + "\n")
    return 0 if ok else 1


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
    # Deprecated alias: --no-attach
    p.add_argument(
        "--no-attach",
        dest="no_attach_deprecated",
        action="store_true",
        help=argparse.SUPPRESS,
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
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="Legacy timeout seconds (used when --run-timeout / --wait-timeout is not set).",
    )
    p.add_argument(
        "--run-timeout",
        type=float,
        default=None,
        help="Runtime timeout seconds for run/start worker execution (overrides --timeout when set).",
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
        "--wrapper-log",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retained for compatibility; wrapper.log is always created in start mode.",
    )

    p.add_argument(
        "--permission-mode",
        choices=["inherit", "allow", "noninteractive"],
        default="inherit",
        help="Permission handling. HTTP engine auto-replies via API when possible.",
    )
    p.add_argument(
        "--execution-profile",
        choices=["legacy", "hybrid", "latency", "checkpoint"],
        default=DEFAULT_EXECUTION_PROFILE,
        help=(
            "Routing policy for engine/artifacts. "
            "hybrid(default): short->HTTP+lighter artifacts, long->CLI+full artifacts; "
            "latency: prefer HTTP+lighter artifacts; checkpoint: prefer CLI+full artifacts; "
            "legacy: keep pre-policy behavior."
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
        "--max-text-chars",
        type=int,
        default=DEFAULT_MAX_TEXT_CHARS,
        help="Max chars returned in finish.summary.",
    )
    p.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=DEFAULT_MAX_ARTIFACT_BYTES,
        help="Hard cap per artifact file (0 disables).",
    )
    p.add_argument(
        "--include-debug",
        action="store_true",
        help="Include debug info under finish.debug (may be noisy).",
    )
    p.add_argument(
        "--workaround-agent-attach",
        dest="workaround_agent_attach",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Workaround: avoid CLI --attach when --agent is set unless attach is explicit.",
    )

    p.add_argument(
        "--no-contract",
        action="store_true",
        help="Do not append output contract to the prompt.",
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
        p_wait.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
        p_wait.add_argument(
            "--wait-timeout",
            type=float,
            default=None,
            help="Wait window seconds for wait command (overrides --timeout when set).",
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

        # Apply deprecated alias: --no-attach => --no-attach-server
        if hasattr(args, "no_attach_deprecated") and getattr(
            args, "no_attach_deprecated"
        ):
            setattr(args, "attach_server", False)

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
            "ok": False,
            "error": {"name": "UnhandledException", "message": msg},
        }
        sys.stdout.write(_json_line(obj) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
