#!/usr/bin/env python3
"""opencode_subtask.py

A small, stable adapter around OpenCode's CLI for "subagent"-style delegation.

Design goals:
- Upstream agents (e.g., Codex) consume ONLY this adapter's stable JSON events.
- Hide OpenCode's NDJSON event stream and CLI quirks behind a versioned contract.
- Support both foreground (run) and background (start/wait) execution.
- Persist full artifacts (events, stderr, full text, patch) to disk, while returning
  only a short, bounded summary to stdout.

Python: 3.10+
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from shutil import which
from typing import Any, Final

# ============================
# Stable contract
# ============================

ADAPTER_SCHEMA_VERSION: Final[int] = 1

DEFAULT_SERVER_HOSTNAME: Final[str] = "127.0.0.1"
DEFAULT_SERVER_PORT: Final[int] = 0  # 0 => pick a free port
DEFAULT_SERVER_WAIT_S: Final[float] = 10.0

DEFAULT_RUN_TIMEOUT_S: Final[float] = 30 * 60.0  # 30 minutes
DEFAULT_WAIT_TIMEOUT_S: Final[float] = 60 * 60.0  # 60 minutes
DEFAULT_MAX_TEXT_CHARS: Final[int] = 1000

DEFAULT_POLL_INTERVAL_S: Final[float] = 0.25

DEFAULT_OPENCODE_BIN: Final[str] = "opencode.cmd" if os.name == "nt" else "opencode"


# ============================
# Small utilities
# ============================

def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_line(obj: dict[str, Any]) -> str:
    # Robust stdout across Windows codepages (e.g. GBK): emit ASCII-only JSON
    # (non-ASCII escaped as \\uXXXX). Full-fidelity UTF-8 is still written to artifacts.
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{_now_ms()}")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            with contextlib.suppress(Exception):
                os.fsync(f.fileno())
        if os.name == "nt":
            # On Windows, rename/replace can transiently fail due to AV scanners or
            # other readers briefly opening the target. Retry with a short backoff.
            for i in range(8):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    time.sleep(0.02 * (i + 1))
            else:
                os.replace(tmp, path)
        else:
            os.replace(tmp, path)
    finally:
        with contextlib.suppress(Exception):
            if tmp.exists():
                tmp.unlink()


def _write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    _atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_read_text(path))
    except Exception:
        return None


def _cache_root() -> Path:
    """User cache root for this adapter.

    - Linux/macOS: $XDG_CACHE_HOME or ~/.cache
    - Windows: %LOCALAPPDATA% or ~/AppData/Local
    """
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


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # Windows: tasklist check
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")
        return str(pid) in out
    except Exception:
        return False


def _pid_looks_like_opencode(pid: int) -> bool:
    """Best-effort guard against PID reuse when killing stale server processes."""
    if pid <= 0 or not _pid_running(pid):
        return False
    try:
        if os.name == "nt":
            cmd = (
                f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\"; "
                "if ($null -eq $p) { exit 1 }; "
                "$s = ($p.Name + \" \" + $p.CommandLine); "
                "Write-Output $s"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="replace")
            return "opencode" in out.lower()

        # POSIX best-effort: /proc is common but not universal.
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            data = proc_cmdline.read_bytes().decode("utf-8", errors="replace").replace("\x00", " ")
            return "opencode" in data.lower()
    except Exception:
        return False
    return False


def _kill_tree(pid: int) -> None:
    if pid <= 0:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    # POSIX
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def _merge_env(base: dict[str, str], set_vars: list[str], set_from_files: list[str]) -> dict[str, str]:
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
        env[k.strip()] = _read_text(Path(p).expanduser())
    # Adapter stability defaults (caller may override via --env).
    env.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "1")
    env.setdefault("OPENCODE_CLIENT", "opencode-subtask")
    return env



def _resolve_executable(exe: str) -> str | None:
    """Resolve an executable to an absolute path for subprocess.

    Motivation: on Windows, Node-installed CLIs are often .cmd shims. Resolving
    up-front avoids CreateProcess/PATHEXT edge cases and makes start/wait more
    reliable.
    """
    if not exe:
        return None

    # If it already looks like a path, respect it.
    if any(sep in exe for sep in (os.sep, "/", "\\")) or (os.name == "nt" and len(exe) >= 2 and exe[1] == ":"):
        p = Path(exe).expanduser()
        return str(p) if p.exists() else None

    found = which(exe)
    if found:
        return found

    # On Windows, try common shim extensions explicitly.
    if os.name == "nt" and not Path(exe).suffix:
        for ext in (".cmd", ".exe", ".bat", ".com", ".ps1"):
            found = which(exe + ext)
            if found:
                return found

    return None


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 64), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _read_tail(path: Path, max_bytes: int) -> str | None:
    if max_bytes <= 0:
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start, os.SEEK_SET)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _progress_snapshot(artifacts_dir: Path) -> dict[str, Any] | None:
    files: dict[str, Any] = {}
    last_activity_ms: int | None = None
    for name in ("events.ndjson", "assistant.txt", "stderr.log", "wrapper.log"):
        p = artifacts_dir / name
        if not p.exists():
            continue
        try:
            st = p.stat()
            m = int(st.st_mtime * 1000)
            files[name] = {"bytes": int(st.st_size), "mtimeMs": m}
            last_activity_ms = m if last_activity_ms is None else max(last_activity_ms, m)
        except Exception:
            continue

    if not files:
        return None

    now_ms = _now_ms()
    idle_s = max(0.0, (now_ms - (last_activity_ms or now_ms)) / 1000.0)
    return {"files": files, "lastActivityMs": last_activity_ms, "idleForSeconds": idle_s}

def _http_get_json(url: str, timeout_s: float = 1.0) -> dict[str, Any] | None:
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _server_health(url_base: str, *, attempts: int = 1, delay_s: float = 0.0) -> dict[str, Any] | None:
    attempts = max(1, int(attempts))
    for i in range(attempts):
        res = _http_get_json(f"{url_base}/global/health", timeout_s=1.0)
        if isinstance(res, dict):
            return res
        if i + 1 < attempts:
            time.sleep(max(0.0, float(delay_s)))
    return None


def _make_run_id() -> str:
    return f"run_{_now_ms()}_{os.getpid()}"


def _join_prompt(prompt_parts: list[str]) -> str:
    prompt = " ".join(prompt_parts).strip()
    if prompt:
        return prompt
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    raise SystemExit("Missing prompt. Provide as args or via stdin.")


def _truncate(s: str, max_chars: int) -> tuple[str, bool]:
    if max_chars < 0:
        return s, False
    if max_chars == 0:
        return "", bool(s)
    if len(s) <= max_chars:
        return s, False
    return s[:max_chars], True


# ============================
# OpenCode server management
# ============================

def ensure_server(
    *,
    opencode_bin: str,
    workdir: Path,
    hostname: str,
    port: int,
    wait_s: float,
    env: dict[str, str],
) -> dict[str, Any]:
    """Ensure a per-project opencode serve instance is running and healthy.

    Uses /global/health to validate that the port really hosts an OpenCode server.
    """
    st_path = _server_state_path(workdir)
    log_path = _server_log_path(workdir)
    st_path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = _servers_dir() / f"{_project_key(workdir)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    class _ServerLock:
        def __init__(self, path: Path, timeout_s: float) -> None:
            self.path = path
            self.timeout_s = timeout_s
            self.acquired = False

        def __enter__(self) -> "_ServerLock":
            deadline = time.monotonic() + max(0.0, float(self.timeout_s))
            while True:
                try:
                    fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, f"{os.getpid()} {_now_ms()}\n".encode("utf-8"))
                    finally:
                        os.close(fd)
                    self.acquired = True
                    return self
                except FileExistsError:
                    # Best-effort stale lock cleanup.
                    try:
                        age_s = time.time() - self.path.stat().st_mtime
                        if age_s > 300:
                            self.path.unlink()
                            continue
                    except Exception:
                        pass
                    if time.monotonic() >= deadline:
                        return self
                    time.sleep(0.05)

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            if self.acquired:
                with contextlib.suppress(Exception):
                    self.path.unlink()

    # Acquire a per-project lock so concurrent tasks don't race-start or clobber state.
    with _ServerLock(lock_path, timeout_s=max(0.5, float(wait_s))) as lock:
        if not lock.acquired:
            # Another task is likely starting or reusing the server. Poll state until healthy.
            deadline = time.monotonic() + max(0.1, float(wait_s))
            while time.monotonic() < deadline:
                st = _load_json(st_path) or {}
                if isinstance(st, dict) and isinstance(st.get("url"), str):
                    url = str(st["url"])
                    health = _server_health(url, attempts=3, delay_s=0.25)
                    if isinstance(health, dict) and health.get("healthy") is True:
                        st["version"] = health.get("version")
                        _write_json(st_path, st)
                        return st
                time.sleep(0.25)
            raise RuntimeError(f"opencode serve lock busy and no healthy server observed: {st_path}")

        # 1) Reuse if healthy.
        st = _load_json(st_path) or {}
        if isinstance(st, dict) and isinstance(st.get("url"), str):
            url = str(st["url"])
            health = _server_health(url, attempts=3, delay_s=0.25)
            if isinstance(health, dict) and health.get("healthy") is True:
                st["version"] = health.get("version")
                _write_json(st_path, st)
                return st

        # 2) If stale pid exists, try to kill.
        try:
            old_pid = int(st.get("pid") or 0)
            if old_pid and _pid_looks_like_opencode(old_pid):
                _kill_tree(old_pid)
        except Exception:
            pass

        # 3) Start new server (retry a few times when auto-picking a port).
        port_auto = port == 0
        attempts = 3 if port_auto else 1
        last_err: Exception | None = None
        proc: subprocess.Popen[Any] | None = None
        health: dict[str, Any] | None = None
        port_chosen: int | None = None

        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
            popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True

        for _ in range(attempts):
            port_chosen = _pick_free_port(hostname) if port_auto else int(port)
            cmd = [opencode_bin, "serve", "--hostname", hostname, "--port", str(port_chosen)]

            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_path, "ab", buffering=0)
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
            except Exception as e:
                last_err = e
                with contextlib.suppress(Exception):
                    log_fp.close()
                proc = None
                continue
            finally:
                # Close the parent's handle; the child keeps its own fd for logging.
                with contextlib.suppress(Exception):
                    log_fp.close()

            url = f"http://{hostname}:{port_chosen}"
            deadline = time.monotonic() + max(wait_s, 0.1)
            health = None
            while time.monotonic() < deadline:
                health = _server_health(url, attempts=1)
                if isinstance(health, dict) and health.get("healthy") is True:
                    break
                # If the child died quickly, don't wait the full timeout.
                if proc is not None and not _pid_running(int(proc.pid)):
                    break
                time.sleep(0.25)

            if isinstance(health, dict) and health.get("healthy") is True:
                break

            # Failed to become healthy; kill and retry on a new port (auto mode only).
            if proc is not None:
                with contextlib.suppress(Exception):
                    _kill_tree(int(proc.pid))
            proc = None
            health = None

        if not (isinstance(health, dict) and health.get("healthy") is True and proc is not None and port_chosen is not None):
            if last_err is not None:
                raise RuntimeError(f"opencode serve failed to start: {last_err}") from last_err
            raise RuntimeError("opencode serve failed health check")

        state = {
            "pid": proc.pid,
            "hostname": hostname,
            "port": port_chosen,
            "url": f"http://{hostname}:{port_chosen}",
            "startedAt": _now_ms(),
            "version": health.get("version"),
            "projectRoot": str(_find_git_root(workdir)),
            "logPath": str(log_path),
            "command": cmd,
        }
        _write_json(st_path, state)
        return state


# ============================
# NDJSON aggregation
# ============================

def _extract_text_from_message_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        joined = "".join(parts).strip()
        return joined or None
    return None


def _extract_text(event: dict[str, Any]) -> str | None:
    # Common direct fields.
    for key in ("text", "delta", "content"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val

    # part: {type, text/delta/content, role?}
    part = event.get("part")
    if isinstance(part, dict):
        for key in ("text", "delta", "content"):
            val = part.get(key)
            if isinstance(val, str) and val:
                return val

    # data payload.
    data = event.get("data")
    if isinstance(data, dict):
        for key in ("text", "delta", "content"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val

    # message object.
    msg = event.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        t = _extract_text_from_message_content(content)
        if t:
            return t

    return None


def _event_role(event: dict[str, Any]) -> str | None:
    # Prefer explicit role.
    role = event.get("role")
    if isinstance(role, str) and role:
        return role
    part = event.get("part")
    if isinstance(part, dict) and isinstance(part.get("role"), str):
        return str(part.get("role"))
    msg = event.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("role"), str):
        return str(msg.get("role"))
    return None


class _Aggregator:
    def __init__(self, *, text_sink: Any | None = None, tail_max_chars: int = 200_000) -> None:
        self.session_id: str | None = None
        self.error_event: dict[str, Any] | None = None
        self.last_step_finish: dict[str, Any] | None = None
        self.metrics: dict[str, Any] | None = None

        self._text_sink = text_sink
        self._tail_max_chars = max(1024, int(tail_max_chars))
        self._tail: str = ""
        self._lock = threading.Lock()

    def _append_text(self, t: str) -> None:
        if not t:
            return
        if self._text_sink is not None:
            try:
                self._text_sink.write(t)
                # Flush aggressively: this is a long-running subprocess and we want partial text persisted.
                self._text_sink.flush()
            except Exception:
                pass

        self._tail += t
        if len(self._tail) > self._tail_max_chars:
            self._tail = self._tail[-self._tail_max_chars :]

    def ingest(self, event: Any) -> None:
        if not isinstance(event, dict):
            return

        with self._lock:
            sid = event.get("sessionID") or event.get("sessionId")
            if isinstance(sid, str) and sid and not self.session_id:
                self.session_id = sid

            typ = event.get("type")
            if typ == "error":
                self.error_event = event
                return

            if typ in ("step-finish", "step_finish", "step-finished"):
                self.last_step_finish = event
                # best-effort metrics extraction
                payload: Any = event.get("part") if isinstance(event.get("part"), dict) else event
                if isinstance(payload, dict):
                    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else None
                    self.metrics = {
                        "reason": payload.get("reason"),
                        "cost": payload.get("cost"),
                        "tokens": tokens,
                    }

            # Collect assistant-facing text. Be conservative to avoid echoing prompt/tool logs.
            role = _event_role(event)
            if role and role != "assistant":
                return

            # Typical OpenCode NDJSON: type "text" with part.type "text".
            part = event.get("part")
            if isinstance(part, dict):
                ptyp = part.get("type")
                if ptyp in ("text", "delta"):
                    t = _extract_text(event)
                    if t:
                        self._append_text(t)
                    return

            if typ in ("text", "delta", "assistant", "assistant-message", "message"):
                t = _extract_text(event)
                if t:
                    self._append_text(t)

    def full_text(self) -> str:
        with self._lock:
            return self._tail


# ============================

# Structured result extraction
# ============================

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*({[\s\S]*?})\s*```", re.IGNORECASE)


def _extract_last_json_object(text: str, *, max_scan_chars: int = 50_000) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from assistant text.

    Strategy:
    1) Prefer the last fenced block (```json ... ```).
    2) Else, scan backwards for a '{' and attempt json.loads on slices.

    This deliberately avoids being too strict; upstream should rely on adapter schema,
    not the model's internal output.
    """
    if not text:
        return None

    # 1) Fenced blocks.
    blocks = _JSON_FENCE_RE.findall(text)
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

    # 2) Backward scan. Limit scan window.
    window = text[-max_scan_chars:] if len(text) > max_scan_chars else text
    # To avoid O(n^2) on huge strings, cap attempts.
    attempts = 0
    for i in range(len(window) - 1, -1, -1):
        if window[i] != "{":
            continue
        attempts += 1
        if attempts > 200:
            break
        candidate = window[i:].strip()
        if len(candidate) > 30_000:
            continue
        # Try trimming trailing junk by cutting at the last '}'
        j = candidate.rfind("}")
        if j == -1:
            continue
        candidate2 = candidate[: j + 1]
        try:
            obj = json.loads(candidate2)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    return None


# ============================
# Git patch capture
# ============================

def _git_patch(workdir: Path, artifacts_dir: Path) -> tuple[str | None, list[str]]:
    if which("git") is None:
        return None, []

    try:
        inside = (
            subprocess.check_output(
                ["git", "-C", str(workdir), "rev-parse", "--is-inside-work-tree"],
                stderr=subprocess.DEVNULL,
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
        if inside != "true":
            return None, []
    except Exception:
        return None, []

    changed: list[str] = []
    try:
        names = subprocess.check_output(
            ["git", "-C", str(workdir), "diff", "--name-only"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")
        changed = [x.strip() for x in names.splitlines() if x.strip()]
    except Exception:
        pass

    try:
        diff = subprocess.check_output(
            ["git", "-C", str(workdir), "diff"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")
        if diff.strip():
            p = artifacts_dir / "changes.patch"
            _write_text(p, diff)
            return str(p), changed
    except Exception:
        pass

    return None, changed


# ============================
# Permission presets (optional)
# ============================


def _permission_noninteractive_preset() -> dict[str, Any]:
    """A safe-ish preset that avoids interactive 'ask' deadlocks.

    Based on OpenCode docs: default permissions are permissive, but external_directory
    and doom_loop often require confirmation. In non-interactive mode, we prefer
    deterministic deny instead of hanging.

    Note: This is a best-effort adapter policy; users may override.
    """
    return {
        "edit": "allow",
        "bash": "allow",
        "webfetch": "allow",
        "mcp": "allow",
        "write": "allow",
        "task": "deny",
        "skill": "deny",
        "read": {
            "*": "allow",
            "*.env": "deny",
            "*.env.*": "deny",
            "*.env.example": "allow",
        },
        "external_directory": "deny",
        "doom_loop": "deny",
    }


def _apply_permission_mode(env: dict[str, str], mode: str) -> None:
    if mode == "inherit":
        return
    if mode == "allow":
        env.setdefault("OPENCODE_PERMISSION", json.dumps("allow"))
        return
    if mode == "noninteractive":
        env.setdefault("OPENCODE_PERMISSION", json.dumps(_permission_noninteractive_preset()))
        return
    raise ValueError(f"Unknown permission mode: {mode}")


# ============================
# Stable output builders
# ============================


def _artifacts_obj(**paths: str | None) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for k, v in paths.items():
        obj[k] = v
    return obj


def _finish_obj(
    *,
    ok: bool,
    exit_code: int,
    timed_out: bool,
    run_id: str,
    workdir: Path,
    session_id: str | None,
    summary: str,
    truncated: bool,
    result: dict[str, Any] | None,
    changed_files: list[str],
    artifacts_dir: Path,
    artifacts: dict[str, Any],
    server: dict[str, Any] | None,
    metrics: dict[str, Any] | None,
    error: dict[str, Any] | None,
    include_debug: bool,
    debug: dict[str, Any] | None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "type": "opencode-subtask-finish",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "timestamp": _now_ms(),
        "ok": ok,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "runId": run_id,
        "workdir": str(workdir),
        "sessionId": session_id,
        "summary": summary,
        "summaryTruncated": truncated,
        "result": result,
        "changedFiles": changed_files,
        "artifacts": {"dir": str(artifacts_dir), **artifacts},
        "server": (
            {
                "url": (server or {}).get("url"),
                "version": (server or {}).get("version"),
                "logPath": (server or {}).get("logPath"),
            }
            if server
            else None
        ),
        "metrics": metrics,
        "error": error,
    }
    if include_debug and isinstance(debug, dict):
        obj["debug"] = debug
    return obj


def _status_obj(
    *,
    run_id: str,
    status: str,
    workdir: Path | None,
    pid: int | None,
    artifacts_dir: Path,
    artifacts: dict[str, Any],
    progress: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "type": "opencode-subtask-status",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "status": status,
        "pid": pid,
        "workdir": str(workdir) if workdir else None,
        "artifacts": {"dir": str(artifacts_dir), **artifacts},
        "progress": progress,
        "error": error,
    }


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
        "timestamp": _now_ms(),
        "runId": run_id,
        "pid": pid,
        "workdir": str(workdir),
        "artifacts": {"dir": str(artifacts_dir), **artifacts},
    }


# ============================
# Commands
# ============================


def cmd_ensure_server(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    opencode_bin = _resolve_executable(args.opencode)
    if not opencode_bin:
        out = {
            "type": "opencode-subtask-server",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "timestamp": _now_ms(),
            "ok": False,
            "error": {
                "name": "OpencodeNotFound",
                "message": f"Could not find opencode executable: {args.opencode}",
            },
        }
        sys.stdout.write(_json_line(out) + "\n")
        return 127

    env = _merge_env(os.environ, set_vars=args.env, set_from_files=args.env_file)
    if args.disable_claude_code:
        env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE", "1")

    try:
        st = ensure_server(
            opencode_bin=opencode_bin,
            workdir=workdir,
            hostname=args.server_hostname,
            port=args.server_port,
            wait_s=args.server_wait,
            env=env,
        )
        out = {
            "type": "opencode-subtask-server",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "timestamp": _now_ms(),
            "ok": True,
            "server": {
                "url": st.get("url"),
                "version": st.get("version"),
                "pid": st.get("pid"),
                "logPath": st.get("logPath"),
            },
        }
        sys.stdout.write(_json_line(out) + "\n")
        return 0
    except Exception as e:
        out = {
            "type": "opencode-subtask-server",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "timestamp": _now_ms(),
            "ok": False,
            "error": {"name": type(e).__name__, "message": str(e)},
        }
        sys.stdout.write(_json_line(out) + "\n")
        return 1


def cmd_stop_server(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    st_path = _server_state_path(workdir)
    st = _load_json(st_path) or {}

    pid = int(st.get("pid") or 0) if isinstance(st, dict) else 0
    ok = False
    if pid and _pid_running(pid):
        try:
            _kill_tree(pid)
            ok = True
        except Exception:
            ok = False

    try:
        if st_path.exists():
            st_path.unlink()
    except Exception:
        pass

    out = {
        "type": "opencode-subtask-server-stop",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "timestamp": _now_ms(),
        "ok": ok,
        "pid": pid or None,
    }
    sys.stdout.write(_json_line(out) + "\n")
    return 0 if ok else 1


def _resolve_artifacts_dir(run_id: str | None, artifacts_dir: str | None) -> tuple[str, Path]:
    if artifacts_dir:
        ad = Path(artifacts_dir).expanduser().resolve()
        rid = run_id or ad.name
        return rid, ad
    rid = run_id or _make_run_id()
    ad = _runs_dir() / rid
    return rid, ad


def _write_job_state(job_path: Path, state: str, extra: dict[str, Any] | None = None) -> None:
    obj = _load_json(job_path) or {}
    if not isinstance(obj, dict):
        obj = {}
    obj["state"] = state
    obj["updatedAt"] = _now_ms()
    if isinstance(extra, dict):
        obj.update(extra)
    _write_json(job_path, obj)


def _read_job_pid(job_path: Path) -> int | None:
    obj = _load_json(job_path)
    if not isinstance(obj, dict):
        return None
    pid = obj.get("pid")
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str) and pid.isdigit():
        return int(pid)
    return None


def _default_contract_prompt() -> str:
    # Keep it short and mechanical.
    return (
        "\n\n"
        "SUBTASK OUTPUT CONTRACT (strict):\n"
        "Return your FINAL answer as a single JSON object (no markdown, no code fences).\n"
        "Schema:\n"
        "{\n"
        "  \"summary\": string (<= 800 chars),\n"
        "  \"evidence\": string[] (each item: \"path:line - fact\", <= 10 items),\n"
        "  \"changes\": string[] (what you changed, <= 10 items),\n"
        "  \"next_steps\": string[] (<= 10 items)\n"
        "}\n"
        "If you cannot comply, output {\"summary\": \"...\", \"evidence\": [], \"changes\": [], \"next_steps\": []}.\n"
        "Keep it concise.\n"
    )


def cmd_run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()

    # Preflight: opencode binary (resolve to a concrete path; important on Windows where opencode is often a .cmd shim)
    opencode_bin = _resolve_executable(args.opencode)
    if not opencode_bin:
        run_id, artifacts_dir = _resolve_artifacts_dir(args.run_id, args.artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        job_path = artifacts_dir / "job.json"
        finish_path = artifacts_dir / "finish.json"

        _write_json(
            job_path,
            {
                "runId": run_id,
                "workdir": str(workdir),
                "state": "failed",
                "createdAt": _now_ms(),
                "updatedAt": _now_ms(),
                "pid": os.getpid(),
            },
        )

        out = _finish_obj(
            ok=False,
            exit_code=127,
            timed_out=False,
            run_id=run_id,
            workdir=workdir,
            session_id=None,
            summary="",
            truncated=False,
            result=None,
            changed_files=[],
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name),
            server=None,
            metrics=None,
            error={
                "name": "OpencodeNotFound",
                "message": f"Could not find opencode executable: {args.opencode}",
            },
            include_debug=args.include_debug,
            debug=None,
        )
        _write_json(finish_path, out)
        sys.stdout.write(_json_line(out) + "\n")
        return 127

    # Prepare prompt.
    prompt: str
    if args.prompt_file:
        prompt = _read_text(Path(args.prompt_file).expanduser().resolve())
    elif getattr(args, "prompt_text", None) is not None:
        if args.prompt:
            raise SystemExit("Use either --prompt or positional prompt args, not both.")
        prompt = str(args.prompt_text)
    else:
        prompt = _join_prompt(args.prompt)

    if not args.no_contract:
        prompt = prompt + _default_contract_prompt()

    # Environment.
    env = _merge_env(os.environ, set_vars=args.env, set_from_files=args.env_file)
    if args.disable_claude_code:
        env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE", "1")
    _apply_permission_mode(env, args.permission_mode)

    # Run identity.
    run_id, artifacts_dir = _resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Persist the effective prompt (including any appended contract) for reproducibility.
    prompt_path = artifacts_dir / "prompt.txt"
    try:
        _write_text(prompt_path, prompt)
    except Exception as e:
        job_path = artifacts_dir / "job.json"
        finish_path = artifacts_dir / "finish.json"
        _write_json(
            job_path,
            {
                "runId": run_id,
                "workdir": str(workdir),
                "state": "failed",
                "createdAt": _now_ms(),
                "updatedAt": _now_ms(),
                "pid": os.getpid(),
            },
        )
        out = _finish_obj(
            ok=False,
            exit_code=1,
            timed_out=False,
            run_id=run_id,
            workdir=workdir,
            session_id=None,
            summary="",
            truncated=False,
            result=None,
            changed_files=[],
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name),
            server=None,
            metrics=None,
            error={"name": "PromptWriteError", "message": str(e)},
            include_debug=args.include_debug,
            debug=None,
        )
        _write_json(finish_path, out)
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    # Artifacts paths.
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    events_path = artifacts_dir / "events.ndjson" if args.save_events else None
    assistant_path = artifacts_dir / "assistant.txt" if args.save_text else None
    stderr_path = artifacts_dir / "stderr.log"
    wrapper_log_path = artifacts_dir / "wrapper.log" if args.wrapper_log else None

    # Initialize job record.
    _write_json(
        job_path,
        {
            "runId": run_id,
            "workdir": str(workdir),
            "state": "running",
            "createdAt": _now_ms(),
            "updatedAt": _now_ms(),
            "pid": os.getpid(),
        },
    )

    # Ensure/attach server (best-effort).
    server_state: dict[str, Any] | None = None
    attach_url = args.attach
    server_error: dict[str, Any] | None = None
    # opencode v1.1.25 has a known crash path for `opencode run --attach ... --agent <name>`
    # ("No context found for instance"). Prefer standalone mode when an explicit agent is requested.
    attach_server = bool(args.attach_server and not args.agent)
    if not attach_url and attach_server:
        try:
            server_state = ensure_server(
                opencode_bin=opencode_bin,
                workdir=workdir,
                hostname=args.server_hostname,
                port=args.server_port,
                wait_s=args.server_wait,
                env=env,
            )
            attach_url = str(server_state.get("url"))
        except Exception as e:
            # Don't fail the whole task; fall back to standalone run.
            server_error = {"name": type(e).__name__, "message": str(e)}
            server_state = None
            attach_url = None

    # Build opencode command.
    cmd: list[str] = [opencode_bin, "run", "--format", "json"]
    if getattr(args, "opencode_print_logs", False):
        cmd.append("--print-logs")
    if getattr(args, "opencode_log_level", None):
        cmd.extend(["--log-level", str(args.opencode_log_level)])
    if attach_url:
        cmd.extend(["--attach", attach_url])
    if args.agent:
        cmd.extend(["--agent", args.agent])
    if args.model:
        cmd.extend(["--model", args.model])
    for f in args.file:
        cmd.extend(["--file", f])
    # Always attach the prompt as a file to avoid shell quoting and Windows command-line limits.
    cmd.extend(["--file", str(prompt_path)])
    # Yargs-style parsers may treat trailing args as additional values for an array option.
    # Use `--` to terminate options so the message is not mis-parsed as another --file.
    cmd.append("--")
    cmd.append("Follow the instructions in the attached prompt.txt.")

    assistant_fp = None
    if assistant_path:
        try:
            assistant_fp = open(assistant_path, "w", encoding="utf-8")
        except Exception:
            assistant_fp = None

    agg = _Aggregator(text_sink=assistant_fp)
    timed_out = False

    events_fp = None
    stderr_fp = None
    try:
        events_fp = open(events_path, "ab", buffering=0) if events_path else None
        stderr_fp = open(stderr_path, "ab", buffering=0)
    except Exception as e:
        with contextlib.suppress(Exception):
            if events_fp:
                events_fp.close()
        with contextlib.suppress(Exception):
            if stderr_fp:
                stderr_fp.close()
        with contextlib.suppress(Exception):
            if assistant_fp:
                assistant_fp.close()
        _write_job_state(job_path, "failed", {"error": {"name": type(e).__name__, "message": str(e)}})
        out = _finish_obj(
            ok=False,
            exit_code=1,
            timed_out=False,
            run_id=run_id,
            workdir=workdir,
            session_id=None,
            summary="",
            truncated=False,
            result=None,
            changed_files=[],
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(
                jobPath=job_path.name,
                finishPath=finish_path.name,
                eventsPath=events_path.name if events_path else None,
                assistantPath=assistant_path.name if assistant_path else None,
                stderrPath=stderr_path.name,
                wrapperLogPath=wrapper_log_path.name if wrapper_log_path else None,
                patchPath=None,
                promptPath=prompt_path.name if prompt_path else None,
            ),
            server=server_state,
            metrics=None,
            error={"name": "ArtifactOpenError", "message": str(e)},
            include_debug=args.include_debug,
            debug={"opencodeCommand": cmd, "serverError": server_error},
        )
        _write_json(finish_path, out)
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    # wrapper.log is for background worker stdout/stderr redirection; in foreground we don't need it.
    if wrapper_log_path:
        _write_text(wrapper_log_path, "")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_fp,
            env=env,
        )
        _write_job_state(job_path, "running", {"opencodePid": proc.pid})
    except Exception as e:
        with contextlib.suppress(Exception):
            if events_fp:
                events_fp.close()
        with contextlib.suppress(Exception):
            if stderr_fp:
                stderr_fp.close()
        with contextlib.suppress(Exception):
            if assistant_fp:
                assistant_fp.close()
        _write_job_state(job_path, "failed", {"error": {"name": type(e).__name__, "message": str(e)}})
        out = _finish_obj(
            ok=False,
            exit_code=127,
            timed_out=False,
            run_id=run_id,
            workdir=workdir,
            session_id=None,
            summary="",
            truncated=False,
            result=None,
            changed_files=[],
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(
                jobPath=job_path.name,
                finishPath=finish_path.name,
                eventsPath=events_path.name if events_path else None,
                assistantPath=assistant_path.name if assistant_path else None,
                stderrPath=stderr_path.name,
                wrapperLogPath=wrapper_log_path.name if wrapper_log_path else None,
                patchPath=None,
                promptPath=prompt_path.name if prompt_path else None,
            ),
            server=server_state,
            metrics=None,
            error={"name": type(e).__name__, "message": str(e)},
            include_debug=args.include_debug,
            debug={"opencodeCommand": cmd, "serverError": server_error},
        )
        _write_json(finish_path, out)
        sys.stdout.write(_json_line(out) + "\n")
        return 127

    def reader() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            if not raw:
                break
            if events_fp:
                try:
                    events_fp.write(raw)
                except Exception:
                    pass
            if not args.quiet:
                # debugging only: forward raw NDJSON
                try:
                    sys.stderr.buffer.write(raw)
                    sys.stderr.buffer.flush()
                except Exception:
                    pass
            try:
                evt = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            agg.ingest(evt)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        proc.wait(timeout=float(args.timeout))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            _kill_tree(proc.pid) if os.name == "nt" else proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    t.join(timeout=5)

    if events_fp:
        events_fp.close()
    stderr_fp.close()
    if assistant_fp:
        try:
            assistant_fp.close()
        except Exception:
            pass

    exit_code = int(proc.returncode or 0)

    # Assistant tail (full transcript, if enabled, is streamed to assistant.txt).
    full_text = agg.full_text()
    if args.save_text and assistant_path and assistant_fp is None:
        try:
            _write_text(assistant_path, full_text)
        except Exception:
            assistant_path = None

    # Extract structured result if present.
    result_obj = _extract_last_json_object(full_text)

    # Choose summary.
    summary_source: str = ""
    if isinstance(result_obj, dict) and isinstance(result_obj.get("summary"), str):
        summary_source = str(result_obj.get("summary"))
    else:
        summary_source = full_text

    summary, truncated = _truncate(summary_source.strip(), int(args.max_text_chars))

    # Capture patch (best-effort).
    patch_path, changed_files = _git_patch(workdir, artifacts_dir)

    # Persist structured result to result.json (adapter writes the file; model only prints JSON).
    result_path: Path | None = artifacts_dir / "result.json"
    result_digest: str | None = None
    result_record: Any
    if isinstance(result_obj, dict):
        result_record = dict(result_obj)
        # Enrich with changed_files if the model didn't include it.
        if "changed_files" not in result_record and "changedFiles" not in result_record:
            result_record["changed_files"] = changed_files
    else:
        result_record = {
            "summary": summary,
            "evidence": [],
            "changed_files": changed_files,
            "next_steps": [],
            "raw": result_obj,
        }
    try:
        _write_json(result_path, result_record)
        result_digest = _sha256_file(result_path)
    except Exception:
        result_path = None
        result_digest = None

    ok = (not timed_out) and exit_code == 0 and agg.error_event is None

    error_obj: dict[str, Any] | None = None
    if not ok:
        if timed_out:
            error_obj = {"name": "Timeout", "message": f"opencode run exceeded timeout={args.timeout}s"}
        elif agg.error_event is not None:
            error_obj = {"name": "OpenCodeErrorEvent", "message": "error event observed", "event": agg.error_event}
        elif exit_code != 0:
            error_obj = {"name": "NonZeroExit", "message": f"opencode exited with code {exit_code}"}

    if error_obj is not None and stderr_path is not None:
        tail = _read_tail(stderr_path, 4096)
        if tail:
            tail_s, _ = _truncate(tail.strip(), 2000)
            if tail_s:
                error_obj.setdefault("stderrTail", tail_s)

    # Update job state.
    _write_job_state(job_path, "finished" if ok else "failed", {"exitCode": exit_code, "timedOut": timed_out})

    out = _finish_obj(
        ok=ok,
        exit_code=exit_code,
        timed_out=timed_out,
        run_id=run_id,
        workdir=workdir,
        session_id=agg.session_id,
        summary=summary,
        truncated=truncated,
        result=(result_record if args.inline_result else None),
        changed_files=changed_files,
        artifacts_dir=artifacts_dir,
        artifacts=_artifacts_obj(
            jobPath=job_path.name,
            finishPath=finish_path.name,
            eventsPath=events_path.name if events_path else None,
            assistantPath=assistant_path.name if assistant_path else None,
            stderrPath=stderr_path.name,
            wrapperLogPath=wrapper_log_path.name if wrapper_log_path else None,
            patchPath=Path(patch_path).name if patch_path else None,
            promptPath=prompt_path.name if prompt_path else None,
            resultPath=result_path.name if result_path else None,
            resultDigest=result_digest,
        ),
        server=server_state,
        metrics=agg.metrics,
        error=error_obj,
        include_debug=args.include_debug,
        debug={
            "opencodeCommand": cmd,
            "serverError": server_error,
            "exitCode": exit_code,
            "timedOut": timed_out,
        },
    )

    _write_json(finish_path, out)
    sys.stdout.write(_json_line(out) + "\n")

    return 0 if ok else 1


def cmd_start(args: argparse.Namespace) -> int:
    """Start a subtask in the background and return a handle (runId + artifacts dir).

    The worker is the same script's `run` command with explicit --run-id/--artifacts-dir,
    so start/wait never loses the pointer.
    """
    workdir = Path(args.workdir).expanduser().resolve()

    # Preflight: opencode binary (resolve to a concrete path; important on Windows where opencode is often a .cmd shim)
    opencode_bin = _resolve_executable(args.opencode)
    if not opencode_bin:
        out = {
            "type": "opencode-subtask-start",
            "schemaVersion": ADAPTER_SCHEMA_VERSION,
            "timestamp": _now_ms(),
            "ok": False,
            "error": {"name": "FileNotFoundError", "message": f"opencode not found: {args.opencode}"},
        }
        sys.stdout.write(_json_line(out) + "\n")
        return 127

    run_id, artifacts_dir = _resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Write prompt to file to avoid shell quoting issues.
    if getattr(args, "prompt_text", None) is not None:
        if args.prompt:
            raise SystemExit("Use either --prompt or positional prompt args, not both.")
        prompt = str(args.prompt_text)
    else:
        prompt = _join_prompt(args.prompt)
    prompt_path = artifacts_dir / "prompt.txt"
    _write_text(prompt_path, prompt)

    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    wrapper_log_path = artifacts_dir / "wrapper.log"

    # Init job.
    _write_json(
        job_path,
        {
            "runId": run_id,
            "workdir": str(workdir),
            "state": "starting",
            "createdAt": _now_ms(),
            "updatedAt": _now_ms(),
        },
    )

    # Build worker command.
    script_path = Path(__file__).resolve()

    worker_cmd: list[str] = [
        sys.executable,
        str(script_path),
        "run",
        "--opencode",
        str(opencode_bin),
        "--workdir",
        str(workdir),
        "--run-id",
        run_id,
        "--artifacts-dir",
        str(artifacts_dir),
        "--prompt-file",
        str(prompt_path),
        "--timeout",
        str(args.timeout),
        "--max-text-chars",
        str(args.max_text_chars),
        "--permission-mode",
        args.permission_mode,
    ]
    attach_server = bool(args.attach_server and not args.agent)

    # Booleans.
    if args.quiet:
        worker_cmd.append("--quiet")
    else:
        worker_cmd.append("--no-quiet")

    if args.save_events:
        worker_cmd.append("--save-events")
    else:
        worker_cmd.append("--no-save-events")

    if args.save_text:
        worker_cmd.append("--save-text")
    else:
        worker_cmd.append("--no-save-text")

    if args.disable_claude_code:
        worker_cmd.append("--disable-claude-code")
    else:
        worker_cmd.append("--no-disable-claude-code")

    if args.include_debug:
        worker_cmd.append("--include-debug")

    if getattr(args, "inline_result", False):
        worker_cmd.append("--inline-result")

    if args.no_contract:
        worker_cmd.append("--no-contract")

    # Attach settings.
    if args.attach:
        worker_cmd.extend(["--attach", args.attach])
    if not attach_server:
        worker_cmd.append("--no-attach-server")

    worker_cmd.extend(["--server-hostname", args.server_hostname])
    worker_cmd.extend(["--server-port", str(args.server_port)])
    worker_cmd.extend(["--server-wait", str(args.server_wait)])

    # passthroughs.
    if args.agent:
        worker_cmd.extend(["--agent", args.agent])
    if args.model:
        worker_cmd.extend(["--model", args.model])
    if getattr(args, "opencode_print_logs", False):
        worker_cmd.append("--opencode-print-logs")
    if getattr(args, "opencode_log_level", None):
        worker_cmd.extend(["--opencode-log-level", str(args.opencode_log_level)])
    for f in args.file:
        worker_cmd.extend(["--file", f])

    for ev in args.env:
        worker_cmd.extend(["--env", ev])
    for evf in args.env_file:
        worker_cmd.extend(["--env-file", evf])

    # Ensure wrapper.log exists; redirect worker stdout/stderr there.
    _write_text(wrapper_log_path, "")
    log_fp = open(wrapper_log_path, "ab", buffering=0)

    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
        popen_kwargs["creationflags"] = creationflags
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

    _write_job_state(job_path, "running", {"pid": proc.pid, "finishPath": str(finish_path)})

    out = _start_obj(
        run_id=run_id,
        pid=proc.pid,
        workdir=workdir,
        artifacts_dir=artifacts_dir,
        artifacts=_artifacts_obj(
            jobPath=job_path.name,
            finishPath=finish_path.name,
            promptPath=prompt_path.name,
            wrapperLogPath=wrapper_log_path.name,
        ),
    )

    sys.stdout.write(_json_line(out) + "\n")
    return 0


def _load_finish_if_exists(finish_path: Path) -> dict[str, Any] | None:
    if not finish_path.exists():
        return None
    obj = _load_json(finish_path)
    if isinstance(obj, dict):
        return obj
    return None


def cmd_status(args: argparse.Namespace) -> int:
    run_id, artifacts_dir = _resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    wrapper_log_path = artifacts_dir / "wrapper.log"
    progress = _progress_snapshot(artifacts_dir)

    if finish_path.exists():
        fin = _load_json(finish_path)
        if isinstance(fin, dict):
            # Return finish as-is for convenience.
            sys.stdout.write(_json_line(fin) + "\n")
            return 0
        out = _status_obj(
            run_id=run_id,
            status="failed",
            workdir=None,
            pid=None,
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name, wrapperLogPath=wrapper_log_path.name),
            progress=progress,
            error={
                "name": "FinishUnreadable",
                "message": "finish.json exists but is not valid JSON (possibly being written).",
            },
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    pid = _read_job_pid(job_path)
    state_obj = _load_json(job_path)
    workdir = None
    if isinstance(state_obj, dict) and isinstance(state_obj.get("workdir"), str):
        workdir = Path(str(state_obj.get("workdir"))).expanduser().resolve()

    if pid and _pid_running(pid):
        out = _status_obj(
            run_id=run_id,
            status="running",
            workdir=workdir,
            pid=pid,
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name, wrapperLogPath=wrapper_log_path.name),
            progress=progress,
            error=None,
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 0

    # Not finished, pid not running.
    out = _status_obj(
        run_id=run_id,
        status="failed",
        workdir=workdir,
        pid=pid,
        artifacts_dir=artifacts_dir,
        artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name, wrapperLogPath=wrapper_log_path.name),
        progress=progress,
        error={
            "name": "WorkerNotRunning",
            "message": "worker process is not running and finish.json does not exist",
        },
    )
    sys.stdout.write(_json_line(out) + "\n")
    return 1


def cmd_wait(args: argparse.Namespace) -> int:
    run_id, artifacts_dir = _resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"
    progress = _progress_snapshot(artifacts_dir)
    job_obj = _load_json(job_path) or {}
    workdir = None
    if isinstance(job_obj, dict) and isinstance(job_obj.get("workdir"), str):
        workdir = Path(str(job_obj.get("workdir"))).expanduser().resolve()

    # Fast-fail on unknown jobs.
    if not job_path.exists() and not finish_path.exists():
        out = _status_obj(
            run_id=run_id,
            status="missing",
            workdir=None,
            pid=None,
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name),
            progress=progress,
            error={
                "name": "JobNotFound",
                "message": "job.json and finish.json not found for this runId",
            },
        )
        sys.stdout.write(_json_line(out) + "\n")
        return 1

    deadline = time.monotonic() + float(args.timeout)
    while True:
        if finish_path.exists():
            fin = _load_json(finish_path)
            if isinstance(fin, dict):
                sys.stdout.write(_json_line(fin) + "\n")
                return 0 if fin.get("ok") is True else 1
            # finish.json exists but isn't parseable yet; retry until timeout.
            if time.monotonic() >= deadline:
                out = _status_obj(
                    run_id=run_id,
                    status="failed",
                    workdir=None,
                    pid=_read_job_pid(job_path),
                    artifacts_dir=artifacts_dir,
                    artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name),
                    progress=progress,
                    error={
                        "name": "FinishUnreadable",
                        "message": "timeout waiting for a readable finish.json",
                    },
                )
                sys.stdout.write(_json_line(out) + "\n")
                return 1
            time.sleep(float(args.poll_interval))
            continue

        pid = _read_job_pid(job_path)
        if pid and not _pid_running(pid):
            out = _status_obj(
                run_id=run_id,
                status="failed",
                workdir=None,
                pid=pid,
                artifacts_dir=artifacts_dir,
                artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name, wrapperLogPath="wrapper.log"),
                progress=progress,
                error={
                    "name": "WorkerNotRunning",
                    "message": "worker process is not running and finish.json does not exist",
                },
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 1

        if time.monotonic() >= deadline:
            out = _status_obj(
                run_id=run_id,
                status="running",
                workdir=workdir,
                pid=pid,
                artifacts_dir=artifacts_dir,
                artifacts=_artifacts_obj(jobPath=job_path.name, finishPath=finish_path.name),
                progress=_progress_snapshot(artifacts_dir),
                error={
                    "name": "WaitTimeout",
                    "message": f"timeout waiting for finish.json (timeout={args.timeout}s)",
                },
            )
            sys.stdout.write(_json_line(out) + "\n")
            return 0

        time.sleep(float(args.poll_interval))


def cmd_cancel(args: argparse.Namespace) -> int:
    run_id, artifacts_dir = _resolve_artifacts_dir(args.run_id, args.artifacts_dir)
    job_path = artifacts_dir / "job.json"
    finish_path = artifacts_dir / "finish.json"

    pid = _read_job_pid(job_path)
    ok = False
    if pid and _pid_running(pid):
        try:
            _kill_tree(pid)
            ok = True
        except Exception:
            ok = False

    # If no finish exists, write a minimal one so wait won't hang.
    if not finish_path.exists():
        out = _finish_obj(
            ok=False,
            exit_code=130,
            timed_out=False,
            run_id=run_id,
            workdir=Path(_load_json(job_path).get("workdir", str(Path.cwd()))) if isinstance(_load_json(job_path), dict) else Path.cwd(),
            session_id=None,
            summary="Canceled",
            truncated=False,
            result=None,
            changed_files=[],
            artifacts_dir=artifacts_dir,
            artifacts=_artifacts_obj(
                jobPath=job_path.name,
                finishPath=finish_path.name,
                stderrPath=None,
                eventsPath=None,
                assistantPath=None,
                wrapperLogPath="wrapper.log",
                patchPath=None,
                promptPath="prompt.txt",
            ),
            server=None,
            metrics=None,
            error={"name": "Canceled", "message": "job canceled by adapter"},
            include_debug=False,
            debug=None,
        )
        _write_json(finish_path, out)

    out2 = {
        "type": "opencode-subtask-cancel",
        "schemaVersion": ADAPTER_SCHEMA_VERSION,
        "timestamp": _now_ms(),
        "runId": run_id,
        "ok": ok,
        "pid": pid,
    }
    sys.stdout.write(_json_line(out2) + "\n")
    return 0 if ok else 1


# ============================
# CLI
# ============================


def _add_common_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--opencode", default=DEFAULT_OPENCODE_BIN, help="Path to opencode binary")
    p.add_argument("--workdir", default=".", help="Working directory / project root")

    p.add_argument("--attach", default=None, help="Attach to an existing opencode server URL")
    p.add_argument(
        "--attach-server",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse a per-project opencode serve via --attach (recommended for speed)",
    )
    p.add_argument(
        "--no-attach",
        dest="attach_server",
        action="store_false",
        help="(deprecated) Alias for --no-attach-server",
    )

    p.add_argument("--server-hostname", default=DEFAULT_SERVER_HOSTNAME)
    p.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    p.add_argument("--server-wait", type=float, default=DEFAULT_SERVER_WAIT_S)

    p.add_argument("--agent", default=None, help="OpenCode agent name")
    p.add_argument("--model", default=None, help="Model in provider/model form")
    p.add_argument("--file", action="append", default=[], help="File(s) to attach to the message")
    p.add_argument(
        "--opencode-print-logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Pass --print-logs to opencode (captured in artifacts/stderr.log)",
    )
    p.add_argument(
        "--opencode-log-level",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        default=None,
        help="Pass --log-level to opencode (captured in artifacts/stderr.log)",
    )

    p.add_argument("--timeout", type=float, default=DEFAULT_RUN_TIMEOUT_S, help="Timeout for opencode run")
    p.add_argument(
        "--quiet",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If true, suppress passthrough of OpenCode NDJSON to stdout (recommended)",
    )

    p.add_argument(
        "--save-events",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save full NDJSON stream to artifacts/events.ndjson",
    )
    p.add_argument(
        "--save-text",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save aggregated assistant text to artifacts/assistant.txt",
    )
    p.add_argument(
        "--max-text-chars",
        type=int,
        default=DEFAULT_MAX_TEXT_CHARS,
        help="Max characters of summary returned in finish JSON (full text is still saved)",
    )
    p.add_argument(
        "--inline-result",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Inline parsed result JSON into the finish event (otherwise only write artifacts/result.json and return resultPath/digest)",
    )
    p.add_argument(
        "--include-debug",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Include debug field in finish JSON (may include unstable fields)",
    )

    p.add_argument(
        "--disable-claude-code",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Set OPENCODE_DISABLE_CLAUDE_CODE=1 for isolation (recommended when running under other agents)",
    )

    p.add_argument(
        "--permission-mode",
        choices=["inherit", "allow", "noninteractive"],
        default="inherit",
        help="Permission policy override via OPENCODE_PERMISSION (use noninteractive to avoid ask deadlocks)",
    )

    p.add_argument("--env", action="append", default=[], help="Extra env var KEY=VALUE")
    p.add_argument("--env-file", action="append", default=[], help="Extra env var from file KEY=PATH")

    p.add_argument(
        "--no-contract",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Do not append the adapter's compact JSON output contract to the prompt",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenCode subtask adapter: stable JSON boundary + artifacts + background start/wait.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_srv = sub.add_parser("ensure-server", help="Start/reuse a per-project opencode serve")
    p_srv.add_argument("--opencode", default=DEFAULT_OPENCODE_BIN)
    p_srv.add_argument("--workdir", default=".")
    p_srv.add_argument("--server-hostname", default=DEFAULT_SERVER_HOSTNAME)
    p_srv.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    p_srv.add_argument("--server-wait", type=float, default=DEFAULT_SERVER_WAIT_S)
    p_srv.add_argument(
        "--disable-claude-code",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    p_srv.add_argument("--env", action="append", default=[])
    p_srv.add_argument("--env-file", action="append", default=[])
    p_srv.set_defaults(func=cmd_ensure_server)

    p_stop = sub.add_parser("stop-server", help="Stop the per-project opencode serve")
    p_stop.add_argument("--workdir", default=".")
    p_stop.set_defaults(func=cmd_stop_server)

    p_run = sub.add_parser("run", help="Run a subtask (foreground, returns finish JSON)")
    _add_common_run_flags(p_run)
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--artifacts-dir", default=None)
    p_run.add_argument("--prompt-file", default=None, help="Read prompt from a file")
    p_run.add_argument("--prompt", dest="prompt_text", help="Prompt text (alternative to positional args or stdin)")
    p_run.add_argument(
        "--wrapper-log",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="(internal) If true, create artifacts/wrapper.log placeholder",
    )
    p_run.add_argument("prompt", nargs="*", help="Prompt to send to OpenCode")
    p_run.set_defaults(func=cmd_run)

    p_start = sub.add_parser("start", help="Start a subtask (background), returns runId handle")
    _add_common_run_flags(p_start)
    p_start.add_argument("--run-id", default=None)
    p_start.add_argument("--artifacts-dir", default=None)
    p_start.add_argument("--prompt", dest="prompt_text", default=None, help="Prompt string (alternative to positional args or stdin).")
    p_start.add_argument("prompt", nargs="*", help="Prompt to send to OpenCode")
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status", help="Check status for a background run")
    p_status.add_argument("--run-id", default=None, required=False)
    p_status.add_argument("--artifacts-dir", default=None, required=False)
    p_status.set_defaults(func=cmd_status)

    p_wait = sub.add_parser("wait", help="Wait for finish.json; returns finish or running status")
    p_wait.add_argument("--run-id", default=None, required=False)
    p_wait.add_argument("--artifacts-dir", default=None, required=False)
    p_wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_S)
    p_wait.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)
    p_wait.set_defaults(func=cmd_wait)

    p_cancel = sub.add_parser("cancel", help="Cancel a background run")
    p_cancel.add_argument("--run-id", default=None, required=False)
    p_cancel.add_argument("--artifacts-dir", default=None, required=False)
    p_cancel.set_defaults(func=cmd_cancel)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
