import argparse
import contextlib
import http.server
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import unittest.mock
from pathlib import Path


class TestCancelTerminalFinish(unittest.TestCase):
    @staticmethod
    def _load_adapter_module(repo_root: Path):
        script = repo_root / "scripts" / "opencode_subtask.py"
        module_name = "opencode_subtask_module"
        spec = importlib.util.spec_from_file_location(module_name, script)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return mod

    @staticmethod
    def _build_run_args(mod, repo_root: Path, artifacts_dir: Path, run_id: str):
        p = argparse.ArgumentParser(prog="run-test")
        mod._add_common_run_flags(p)
        p.add_argument("prompt", nargs=argparse.REMAINDER)
        return p.parse_args(
            [
                "--workdir",
                str(repo_root),
                "--engine",
                "cli",
                "--run-id",
                run_id,
                "--artifacts-dir",
                str(artifacts_dir),
                "--opencode",
                "__dummy_opencode__",
                "--prompt",
                "Act as a senior software engineer.",
                "--include-debug",
                "--retry-empty-output",
                "--empty-output-retries",
                "1",
            ]
        )

    def test_cancel_writes_finish_when_worker_dead_and_abort_fails(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"
        self.assertTrue(script.exists(), f"missing: {script}")

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_cancel_") as td:
            artifacts_dir = Path(td)
            job_path = artifacts_dir / "job.json"
            finish_path = artifacts_dir / "finish.json"

            # Deterministic reproduction of the state-machine gap:
            # - serverUrl + sessionId present so abort is attempted
            # - worker PID is dead so no normal finish.json can appear
            # - abort fails (connection refused) so ok remains false
            job = {
                "runId": "test-run",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
                "pid": 2147483647,  # extremely unlikely to exist
                # Use an invalid port to force a deterministic abort failure without
                # depending on network conditions.
                "serverUrl": "http://127.0.0.1:99999",
                "sessionId": "ses_test",
                "httpAttempted": True,
                "serverStartedNew": False,
                "stopServerAfterRunMode": "never",
            }
            job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            # Dead worker means "already terminated" for cancel semantics:
            # cancel should return success and still write terminal finish.json.
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(
                finish_path.exists(),
                f"finish.json was not written; stdout={proc.stdout!r}",
            )
            fin = json.loads(finish_path.read_text(encoding="utf-8"))
            self.assertEqual(fin.get("type"), "opencode-subtask-finish")
            self.assertIsInstance(fin.get("error"), dict)
            self.assertEqual(fin["error"].get("name"), "Canceled")

    def test_status_fail_fast_when_finish_unreadable(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_unreadable_status_"
        ) as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            finish_path.write_text("{not valid json", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "status",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            out = json.loads(proc.stdout)
            self.assertEqual(out.get("type"), "opencode-subtask-status")
            self.assertEqual((out.get("error") or {}).get("name"), "FinishUnreadable")

    def test_wait_fail_fast_when_finish_unreadable(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_unreadable_wait_"
        ) as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            finish_path.write_text("{still not valid json", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "wait",
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--timeout",
                    "60",
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            out = json.loads(proc.stdout)
            self.assertEqual(out.get("type"), "opencode-subtask-status")
            self.assertEqual((out.get("error") or {}).get("name"), "FinishUnreadable")

    def test_wait_timeout_override_takes_precedence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_wait_timeout_override_"
        ) as td:
            artifacts_dir = Path(td)
            job = {
                "runId": "test-run-wait-timeout",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
            }
            (artifacts_dir / "job.json").write_text(
                json.dumps(job, indent=2), encoding="utf-8"
            )

            orig_finalize = mod._maybe_finalize_stale_running_job
            buf = io.StringIO()
            try:
                mod._maybe_finalize_stale_running_job = lambda **kwargs: None
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_wait(
                        argparse.Namespace(
                            run_id=None,
                            artifacts_dir=str(artifacts_dir),
                            timeout=60.0,
                            wait_timeout=0.05,
                            poll_interval=0.01,
                        )
                    )
            finally:
                mod._maybe_finalize_stale_running_job = orig_finalize

            self.assertNotEqual(rc, 0)
            out = json.loads(buf.getvalue().strip())
            self.assertEqual((out.get("error") or {}).get("name"), "WaitTimeout")
            self.assertIn("timeout=0.05s", (out.get("error") or {}).get("message", ""))

    def test_cancel_succeeds_when_abort_succeeds_with_stale_pid(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        class _AbortHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/session/ses_test/abort":
                    body = b"{}"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        srv = http.server.HTTPServer(("127.0.0.1", 0), _AbortHandler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            with tempfile.TemporaryDirectory(prefix="ocsubtask_test_abort_ok_") as td:
                artifacts_dir = Path(td)
                job_path = artifacts_dir / "job.json"
                finish_path = artifacts_dir / "finish.json"
                job = {
                    "runId": "test-run-abort-ok",
                    "workdir": str(repo_root),
                    "state": "running",
                    "createdAt": 1,
                    "updatedAt": 1,
                    # Positive, live PID that is not this adapter worker.
                    "pid": os.getpid(),
                    "serverUrl": f"http://127.0.0.1:{srv.server_port}",
                    "sessionId": "ses_test",
                    "httpAttempted": True,
                    "serverStartedNew": False,
                    "stopServerAfterRunMode": "never",
                }
                job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

                proc = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "cancel",
                        "--artifacts-dir",
                        str(artifacts_dir),
                    ],
                    cwd=str(repo_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout)
                out = json.loads(proc.stdout)
                self.assertTrue(out.get("ok"))
                self.assertTrue(
                    finish_path.exists(),
                    f"finish.json was not written; stdout={proc.stdout!r}",
                )
                fin = json.loads(finish_path.read_text(encoding="utf-8"))
                self.assertEqual(fin.get("type"), "opencode-subtask-finish")
                self.assertEqual((fin.get("error") or {}).get("name"), "Canceled")
        finally:
            srv.shutdown()
            th.join(timeout=5)
            srv.server_close()

    def test_cancel_writes_finish_when_live_pid_and_abort_unreachable(self) -> None:
        """Live PID (not the worker) + unreachable server → cancel detects
        ownership mismatch, refuses to kill (safety), returns ok=false,
        and does NOT write finish.json (cannot confirm worker is dead)."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_live_unreach_") as td:
            artifacts_dir = Path(td)
            job_path = artifacts_dir / "job.json"
            finish_path = artifacts_dir / "finish.json"

            job = {
                "runId": "test-live-pid-unreach",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
                "pid": os.getpid(),  # live but not the worker
                "serverUrl": "http://127.0.0.1:1",  # unreachable
                "sessionId": "ses_test",
                "httpAttempted": True,
                "serverStartedNew": False,
                "stopServerAfterRunMode": "never",
            }
            job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            # Cancel returns ok=false (ownership mismatch → refuses to kill)
            stdout_obj = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(stdout_obj["ok"])
            self.assertEqual(
                stdout_obj.get("workerOwnership"),
                "mismatch",
                "cancel should detect PID ownership mismatch",
            )
            self.assertFalse(stdout_obj.get("killSignalDelivered", True))
            # finish.json must NOT be written — can't mark a job terminal
            # when the worker couldn't be confirmed dead
            self.assertFalse(
                finish_path.exists(),
                "finish.json should NOT be written on ownership mismatch",
            )

    def test_cancel_writes_finish_when_live_pid_and_no_server(self) -> None:
        """Live PID (not the worker) + no serverUrl/sessionId → cancel detects
        ownership mismatch, refuses to kill, returns ok=false,
        and does NOT write finish.json (kill-only path, safety)."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_live_nosrv_") as td:
            artifacts_dir = Path(td)
            job_path = artifacts_dir / "job.json"
            finish_path = artifacts_dir / "finish.json"

            job = {
                "runId": "test-live-pid-nosrv",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
                "pid": os.getpid(),  # live but not the worker
                "httpAttempted": False,
                "serverStartedNew": False,
                "stopServerAfterRunMode": "never",
            }
            job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            # Cancel returns ok=false (ownership mismatch → refuses to kill)
            stdout_obj = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(stdout_obj["ok"])
            self.assertEqual(
                stdout_obj.get("workerOwnership"),
                "mismatch",
                "cancel should detect PID ownership mismatch",
            )
            self.assertFalse(stdout_obj.get("killSignalDelivered", True))
            # finish.json must NOT be written
            self.assertFalse(
                finish_path.exists(),
                "finish.json should NOT be written on ownership mismatch",
            )
            # Cancel returns ok=false (ownership mismatch → refuses to kill)
            stdout_obj = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(stdout_obj["ok"])
            self.assertEqual(
                stdout_obj.get("workerOwnership"),
                "mismatch",
                "cancel should detect PID ownership mismatch",
            )
            # finish.json must still be written to mark job terminal
            self.assertTrue(
                finish_path.exists(),
                f"finish.json was not written; stdout={proc.stdout!r}",
            )

    def test_cancel_writes_finish_when_live_pid_and_no_server(self) -> None:
        """Live PID (not the worker) + no serverUrl/sessionId → cancel detects
        ownership mismatch, refuses to kill, returns ok=false,
        but must still write finish.json (kill-only path)."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_live_nosrv_") as td:
            artifacts_dir = Path(td)
            job_path = artifacts_dir / "job.json"
            finish_path = artifacts_dir / "finish.json"

            job = {
                "runId": "test-live-pid-nosrv",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
                "pid": os.getpid(),  # live but not the worker
                "httpAttempted": False,
                "serverStartedNew": False,
                "stopServerAfterRunMode": "never",
            }
            job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            # Cancel returns ok=false (ownership mismatch → refuses to kill)
            stdout_obj = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(stdout_obj["ok"])
            self.assertEqual(
                stdout_obj.get("workerOwnership"),
                "mismatch",
                "cancel should detect PID ownership mismatch",
            )
            self.assertFalse(stdout_obj.get("killSignalDelivered", True))
            # finish.json must NOT be written on ownership mismatch
            self.assertFalse(
                finish_path.exists(),
                "finish.json should NOT be written on ownership mismatch",
            )

    def test_abort_client_handles_connection_refused(self) -> None:
        """OpencodeHttpClient.abort to unreachable server must not crash.
        Connection refused is caught internally and handled gracefully."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        client = mod.OpencodeHttpClient("http://127.0.0.1:1")
        # abort() catches connection errors internally; must not raise
        try:
            client.abort("ses_nonexistent")
        except Exception as exc:
            self.fail(
                f"abort() should handle connection refused gracefully, but raised: {exc}"
            )

    def test_cancel_does_not_overwrite_existing_finish(self) -> None:
        """Cancel must not overwrite an already-written finish.json."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_no_overwrite_") as td:
            artifacts_dir = Path(td)
            job_path = artifacts_dir / "job.json"
            finish_path = artifacts_dir / "finish.json"

            original = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": True,
                "runId": "existing",
                "summary": "existing-finish",
            }
            finish_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
            before = finish_path.read_text(encoding="utf-8")

            job = {
                "runId": "test-run-no-overwrite",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
                "pid": 2147483647,
            }
            job_path.write_text(json.dumps(job, indent=2), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0)
            after = finish_path.read_text(encoding="utf-8")
            self.assertEqual(after, before)

    def test_run_reuses_existing_finish_when_opencode_not_found(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_run_reuse_finish_"
        ) as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            existing = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": True,
                "runId": "reuse-run",
                "summary": "already-finished",
            }
            finish_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            before = finish_path.read_text(encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "run",
                    "--workdir",
                    str(repo_root),
                    "--engine",
                    "cli",
                    "--run-id",
                    "reuse-run",
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--opencode",
                    "__definitely_missing_opencode_bin__",
                    "--prompt",
                    "Act as a senior software engineer.",
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            out = json.loads(proc.stdout)
            self.assertEqual(out.get("type"), "opencode-subtask-finish")
            self.assertEqual(out.get("runId"), "reuse-run")
            self.assertTrue(out.get("ok"))
            after = finish_path.read_text(encoding="utf-8")
            self.assertEqual(after, before)

    def test_server_lock_timeout_scales_with_wait(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        base = float(mod.DEFAULT_FILE_LOCK_TIMEOUT_S)
        self.assertEqual(mod._server_lock_timeout_s(None), base)
        self.assertGreaterEqual(mod._server_lock_timeout_s(60.0), 65.0)
        self.assertEqual(mod._server_lock_timeout_s(-5.0), base)

    def test_cancel_treats_kill_signal_as_success_when_probe_inconclusive(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_probe_unknown_") as td:
            artifacts_dir = Path(td)
            (artifacts_dir / "job.json").write_text(
                json.dumps(
                    {
                        "runId": "probe-unknown",
                        "workdir": str(repo_root),
                        "state": "running",
                        "createdAt": 1,
                        "updatedAt": 1,
                        "pid": 12345,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            call_idx = {"n": 0}

            def fake_pid_running_state(pid: int):
                call_idx["n"] += 1
                if call_idx["n"] == 1:
                    return (True, True)  # alive before cancel
                return (False, False)  # probe inconclusive after kill

            orig_pid_running_state = mod._pid_running_state
            orig_pid_owner = mod._pid_subtask_worker_ownership_status
            orig_kill_tree = mod._kill_tree
            orig_wait_dead = mod._wait_for_pid_dead
            try:
                mod._pid_running_state = fake_pid_running_state
                mod._pid_subtask_worker_ownership_status = (
                    lambda pid, run_id, require_run_id=False: "verified"
                )
                mod._kill_tree = lambda pid, sig=None: True
                mod._wait_for_pid_dead = lambda pid, timeout_s, poll_s=0.1: False

                rc = mod.cmd_cancel(
                    argparse.Namespace(
                        run_id=None,
                        artifacts_dir=str(artifacts_dir),
                        env=[],
                        env_file=[],
                    )
                )
                self.assertEqual(rc, 0)
                out = json.loads(
                    (artifacts_dir / "job.json").read_text(encoding="utf-8")
                )
                self.assertTrue(bool(out.get("cancelUnverified")))
                fin = json.loads(
                    (artifacts_dir / "finish.json").read_text(encoding="utf-8")
                )
                self.assertEqual((fin.get("error") or {}).get("name"), "Canceled")
                self.assertIn(
                    "termination not confirmed",
                    (fin.get("error") or {}).get("message", ""),
                )
            finally:
                mod._pid_running_state = orig_pid_running_state
                mod._pid_subtask_worker_ownership_status = orig_pid_owner
                mod._kill_tree = orig_kill_tree
                mod._wait_for_pid_dead = orig_wait_dead

    def test_empty_output_retry_once_then_fail(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_empty_output_fail_"
        ) as td:
            artifacts_dir = Path(td)
            args = self._build_run_args(mod, repo_root, artifacts_dir, "empty-fail")
            calls = {"n": 0}

            def fake_run_cli(**kwargs):
                calls["n"] += 1
                return mod.RunOutcome(
                    ok=True,
                    exit_code=0,
                    timed_out=False,
                    engine="cli",
                    fallback_from=None,
                    session_id="ses_test",
                    full_text="",
                    metrics={"tokens": {"output": 0}},
                    error=None,
                )

            orig_resolve = mod._resolve_executable_for_workdir
            orig_run_cli = mod._run_cli
            orig_git_status = mod._git_status
            orig_git_patch = mod._git_patch
            try:
                mod._resolve_executable_for_workdir = (
                    lambda cmd, wd: "__dummy_opencode__"
                )
                mod._run_cli = fake_run_cli
                mod._git_status = lambda wd: ([], [])
                mod._git_patch = lambda wd, ad: None

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_run(args)
                self.assertEqual(calls["n"], 2)
                self.assertNotEqual(rc, 0)
                fin = json.loads(buf.getvalue().strip())
                self.assertEqual(
                    (fin.get("error") or {}).get("name"), "EmptyModelOutput"
                )
                dbg = fin.get("debug") or {}
                self.assertTrue(bool(dbg.get("emptyOutputDetected")))
                self.assertTrue(bool(dbg.get("emptyOutputRetried")))
                self.assertFalse(bool(dbg.get("emptyOutputRecovered")))
            finally:
                mod._resolve_executable_for_workdir = orig_resolve
                mod._run_cli = orig_run_cli
                mod._git_status = orig_git_status
                mod._git_patch = orig_git_patch

    def test_empty_output_retry_recovers(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_empty_output_recover_"
        ) as td:
            artifacts_dir = Path(td)
            args = self._build_run_args(mod, repo_root, artifacts_dir, "empty-recover")
            calls = {"n": 0}

            def fake_run_cli(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    return mod.RunOutcome(
                        ok=True,
                        exit_code=0,
                        timed_out=False,
                        engine="cli",
                        fallback_from=None,
                        session_id="ses_test",
                        full_text="",
                        metrics={"tokens": {"output": 0}},
                        error=None,
                    )
                return mod.RunOutcome(
                    ok=True,
                    exit_code=0,
                    timed_out=False,
                    engine="cli",
                    fallback_from=None,
                    session_id="ses_test",
                    full_text="No major issues found.",
                    metrics={"tokens": {"output": 12}},
                    error=None,
                )

            orig_resolve = mod._resolve_executable_for_workdir
            orig_run_cli = mod._run_cli
            orig_git_status = mod._git_status
            orig_git_patch = mod._git_patch
            try:
                mod._resolve_executable_for_workdir = (
                    lambda cmd, wd: "__dummy_opencode__"
                )
                mod._run_cli = fake_run_cli
                mod._git_status = lambda wd: ([], [])
                mod._git_patch = lambda wd, ad: None

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_run(args)
                self.assertEqual(calls["n"], 2)
                self.assertEqual(rc, 0)
                fin = json.loads(buf.getvalue().strip())
                self.assertTrue(bool(fin.get("ok")))
                dbg = fin.get("debug") or {}
                self.assertTrue(bool(dbg.get("emptyOutputDetected")))
                self.assertTrue(bool(dbg.get("emptyOutputRetried")))
                self.assertTrue(bool(dbg.get("emptyOutputRecovered")))
            finally:
                mod._resolve_executable_for_workdir = orig_resolve
                mod._run_cli = orig_run_cli
                mod._git_status = orig_git_status
                mod._git_patch = orig_git_patch

    def test_status_uses_canonical_run_id_from_job_json(self) -> None:
        """When status is invoked with only --artifacts-dir (no --run-id),
        and finish.json does NOT exist, the output runId must match
        job.json's recorded runId, not a freshly generated one.
        This tests the _canonical_run_id path that actually uses run_id.

        NOTE: state="finished" prevents _maybe_finalize_stale_running_job
        from triggering (it only acts on running/queued), ensuring we test
        the primary cmd_status canonicalization, not the defensive one
        inside the stale finalizer."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_canonical_rid_") as td:
            artifacts_dir = Path(td)
            real_run_id = "run_canonical_test_12345"
            now_ms = int(time.time() * 1000)
            job = {
                "runId": real_run_id,
                "workdir": str(repo_root),
                "state": "finished",
                "createdAt": now_ms,
                "updatedAt": now_ms,
                "pid": 0,
            }
            (artifacts_dir / "job.json").write_text(
                json.dumps(job, indent=2), encoding="utf-8"
            )
            # NOTE: no finish.json — forces status through the active-job
            # path where _canonical_run_id actually determines the output runId.

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "status",
                    "--artifacts-dir",
                    str(artifacts_dir),
                    # NOTE: no --run-id provided
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            # The runId in stdout must be the canonical one from job.json,
            # not a freshly generated run_XXXX_YYYY.
            self.assertEqual(out.get("runId"), real_run_id)

    def test_cancel_uses_canonical_run_id_from_job_json(self) -> None:
        """When cancel is invoked with only --artifacts-dir, the output
        and finish.json must use job.json's recorded runId."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_cancel_canonical_"
        ) as td:
            artifacts_dir = Path(td)
            real_run_id = "run_cancel_canonical_67890"
            job = {
                "runId": real_run_id,
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": 1,
                "updatedAt": 1,
                "pid": 2147483647,
            }
            (artifacts_dir / "job.json").write_text(
                json.dumps(job, indent=2), encoding="utf-8"
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                    # NOTE: no --run-id provided
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            out = json.loads(proc.stdout)
            self.assertEqual(out.get("runId"), real_run_id)

            fin = json.loads(
                (artifacts_dir / "finish.json").read_text(encoding="utf-8")
            )
            self.assertEqual(fin.get("runId"), real_run_id)

    def test_write_finish_once_recovers_corrupt_finish(self) -> None:
        """When finish.json exists but is corrupt, _write_finish_once
        should rename the corrupt file and write a new valid finish."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_corrupt_recover_"
        ) as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            corrupt_content = "{this is not valid json!!!"
            finish_path.write_text(corrupt_content, encoding="utf-8")

            new_finish = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": True,
                "runId": "recover-test",
                "summary": "recovered",
            }
            written, reason, _ = mod._write_finish_once(
                artifacts_dir=artifacts_dir,
                finish_path=finish_path,
                finish_obj=new_finish,
            )
            self.assertTrue(written)
            self.assertEqual(reason, "recovered")

            # New finish.json must be valid and match what we wrote.
            fin = json.loads(finish_path.read_text(encoding="utf-8"))
            self.assertEqual(fin.get("runId"), "recover-test")
            self.assertTrue(fin.get("ok"))

            # Corrupt file must be preserved as finish.corrupt.<ts>.json
            corrupt_files = list(artifacts_dir.glob("finish.corrupt.*.json"))
            self.assertEqual(len(corrupt_files), 1)
            self.assertEqual(
                corrupt_files[0].read_text(encoding="utf-8"), corrupt_content
            )

    def test_run_wrapper_log_not_created_in_foreground_mode(self) -> None:
        """In foreground run mode, wrapper.log should not be referenced
        in the finish JSON (wrapperLogPath should be null)."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_no_wrapper_") as td:
            artifacts_dir = Path(td)
            args = self._build_run_args(mod, repo_root, artifacts_dir, "no-wrapper")
            calls = {"n": 0}

            def fake_run_cli(**kwargs):
                calls["n"] += 1
                return mod.RunOutcome(
                    ok=True,
                    exit_code=0,
                    timed_out=False,
                    engine="cli",
                    fallback_from=None,
                    session_id="ses_test",
                    full_text="All good.",
                    metrics={"tokens": {"output": 5}},
                    error=None,
                )

            orig_resolve = mod._resolve_executable_for_workdir
            orig_run_cli = mod._run_cli
            orig_git_status = mod._git_status
            orig_git_patch = mod._git_patch
            try:
                mod._resolve_executable_for_workdir = lambda cmd, wd: "__dummy__"
                mod._run_cli = fake_run_cli
                mod._git_status = lambda wd: ([], [])
                mod._git_patch = lambda wd, ad: None

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_run(args)
                self.assertEqual(rc, 0)
                fin = json.loads(buf.getvalue().strip())
                artifacts = fin.get("artifacts") or {}
                # wrapperLogPath should be absent or null in run mode
                self.assertIsNone(
                    artifacts.get("wrapperLogPath"),
                    f"wrapperLogPath should be null in run mode, got: {artifacts.get('wrapperLogPath')}",
                )
                # wrapper.log file should NOT exist on disk
                self.assertFalse(
                    (artifacts_dir / "wrapper.log").exists(),
                    "wrapper.log should not exist in foreground run mode",
                )
            finally:
                mod._resolve_executable_for_workdir = orig_resolve
                mod._run_cli = orig_run_cli
                mod._git_status = orig_git_status
                mod._git_patch = orig_git_patch

    def test_run_wrapper_log_referenced_when_present(self) -> None:
        """Fix D: when wrapper.log exists on disk (start-mode worker reusing
        cmd_run code path), finish JSON should reference it via wrapperLogPath."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_wrapper_ref_") as td:
            artifacts_dir = Path(td)
            # Pre-create wrapper.log to simulate start-mode scenario
            (artifacts_dir / "wrapper.log").write_text(
                "wrapper boot log line\n", encoding="utf-8"
            )
            args = self._build_run_args(mod, repo_root, artifacts_dir, "wrapper-ref")

            def fake_run_cli(**kwargs):
                return mod.RunOutcome(
                    ok=True,
                    exit_code=0,
                    timed_out=False,
                    engine="cli",
                    fallback_from=None,
                    session_id="ses_wrapper",
                    full_text="Done.",
                    metrics={"tokens": {"output": 3}},
                    error=None,
                )

            orig_resolve = mod._resolve_executable_for_workdir
            orig_run_cli = mod._run_cli
            orig_git_status = mod._git_status
            orig_git_patch = mod._git_patch
            try:
                mod._resolve_executable_for_workdir = lambda cmd, wd: "__dummy__"
                mod._run_cli = fake_run_cli
                mod._git_status = lambda wd: ([], [])
                mod._git_patch = lambda wd, ad: None

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_run(args)
                self.assertEqual(rc, 0)
                fin = json.loads(buf.getvalue().strip())
                artifacts = fin.get("artifacts") or {}
                self.assertEqual(
                    artifacts.get("wrapperLogPath"),
                    "wrapper.log",
                    "wrapperLogPath should reference wrapper.log when it exists on disk",
                )
            finally:
                mod._resolve_executable_for_workdir = orig_resolve
                mod._run_cli = orig_run_cli
                mod._git_status = orig_git_status
                mod._git_patch = orig_git_patch

    # ------------------------------------------------------------------ #
    # Degradation & edge-case tests (added per expert review feedback)   #
    # ------------------------------------------------------------------ #

    def test_canonical_run_id_malformed_inputs(self) -> None:
        """_canonical_run_id must gracefully handle missing, empty, and
        non-string runId values without crashing."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        fallback = "run_fallback_999"

        # job is None (job.json missing)
        self.assertEqual(mod._canonical_run_id(fallback, None), fallback)

        # job is not a dict
        self.assertEqual(mod._canonical_run_id(fallback, "garbage"), fallback)
        self.assertEqual(mod._canonical_run_id(fallback, [1, 2]), fallback)

        # job dict without runId key
        self.assertEqual(mod._canonical_run_id(fallback, {}), fallback)

        # runId is empty string (falsy)
        self.assertEqual(mod._canonical_run_id(fallback, {"runId": ""}), fallback)

        # runId is None
        self.assertEqual(mod._canonical_run_id(fallback, {"runId": None}), fallback)

        # runId is 0 (falsy int)
        self.assertEqual(mod._canonical_run_id(fallback, {"runId": 0}), fallback)

        # runId is a valid string
        self.assertEqual(
            mod._canonical_run_id(fallback, {"runId": "run_real_123"}),
            "run_real_123",
        )

        # runId is an int (truthy non-string) — should str() it
        self.assertEqual(
            mod._canonical_run_id(fallback, {"runId": 42}),
            "42",
        )

    def test_write_finish_once_replace_fails_unlink_succeeds(self) -> None:
        """When os.replace fails for the rename but unlink succeeds,
        _write_finish_once should still write the new finish (recovered)."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        import unittest.mock as mock

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_replace_fail_") as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            finish_path.write_text("NOT JSON!!!", encoding="utf-8")

            new_finish = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": True,
                "runId": "replace-fail-test",
                "summary": "recovered after replace fail",
            }

            orig_replace = os.replace
            call_count = {"n": 0}

            def selective_replace(src, dst):
                """Fail only the first call (rename corrupt → backup),
                allow subsequent calls (_atomic_write_bytes)."""
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise PermissionError("AV lock simulated")
                return orig_replace(src, dst)

            with mock.patch("os.replace", side_effect=selective_replace):
                written, reason, _ = mod._write_finish_once(
                    artifacts_dir=artifacts_dir,
                    finish_path=finish_path,
                    finish_obj=new_finish,
                )

            self.assertTrue(written)
            self.assertEqual(reason, "recovered")
            fin = json.loads(finish_path.read_text(encoding="utf-8"))
            self.assertEqual(fin["runId"], "replace-fail-test")

    def test_write_finish_once_replace_and_unlink_both_fail(self) -> None:
        """When both os.replace and unlink fail, _write_finish_once should
        return (False, 'unreadable', None) without crashing."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        import unittest.mock as mock

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_both_fail_") as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            finish_path.write_text("CORRUPT!!!", encoding="utf-8")

            new_finish = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": True,
                "runId": "both-fail-test",
            }

            with (
                mock.patch("os.replace", side_effect=PermissionError("locked")),
                mock.patch.object(
                    Path, "unlink", side_effect=PermissionError("also locked")
                ),
            ):
                written, reason, existing = mod._write_finish_once(
                    artifacts_dir=artifacts_dir,
                    finish_path=finish_path,
                    finish_obj=new_finish,
                )

            self.assertFalse(written)
            self.assertEqual(reason, "unreadable")
            self.assertIsNone(existing)
            # Original corrupt file must still be there
            self.assertEqual(finish_path.read_text(encoding="utf-8"), "CORRUPT!!!")

    def test_write_finish_once_double_corrupt_recovery(self) -> None:
        """After recovering from corrupt finish.json, a second call with
        a different finish should return (False, 'exists', first_finish)."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_double_recover_") as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            finish_path.write_text("{bad json", encoding="utf-8")

            first_finish = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": True,
                "runId": "first-recovery",
                "summary": "first",
            }
            written1, reason1, _ = mod._write_finish_once(
                artifacts_dir=artifacts_dir,
                finish_path=finish_path,
                finish_obj=first_finish,
            )
            self.assertTrue(written1)
            self.assertEqual(reason1, "recovered")

            # Second call: finish.json now has valid content from first recovery
            second_finish = {
                "type": "opencode-subtask-finish",
                "schemaVersion": 1,
                "ok": False,
                "runId": "second-attempt",
                "summary": "second",
            }
            written2, reason2, existing2 = mod._write_finish_once(
                artifacts_dir=artifacts_dir,
                finish_path=finish_path,
                finish_obj=second_finish,
            )
            self.assertFalse(written2)
            self.assertEqual(reason2, "exists")
            self.assertIsInstance(existing2, dict)
            self.assertEqual(existing2["runId"], "first-recovery")

    # ── PR #10 regression tests ────────────────────────────────────────

    def test_write_finish_once_not_exists_write_failure(self):
        """P0: _write_finish_once must catch _write_json failure on the
        'not exists' path and return (False, 'write_failed', None)."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        with tempfile.TemporaryDirectory() as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            finish_obj = {"ok": True, "runId": "wf-test"}
            # Make artifacts_dir read-only so _write_json fails
            # (on Windows, patch _write_json directly instead)
            original_write_json = mod._write_json
            call_count = 0

            def _failing_write_json(path, obj):
                nonlocal call_count
                call_count += 1
                raise OSError("disk full")

            with unittest.mock.patch.object(mod, "_write_json", _failing_write_json):
                written, reason, existing = mod._write_finish_once(
                    artifacts_dir=artifacts_dir,
                    finish_path=finish_path,
                    finish_obj=finish_obj,
                )
            self.assertFalse(written)
            self.assertEqual(reason, "write_failed")
            self.assertIsNone(existing)
            self.assertEqual(call_count, 1)
            self.assertFalse(finish_path.exists())

    def test_canonical_run_id_strip_and_control_char_rejection(self):
        """P3: _canonical_run_id strips whitespace and rejects control chars."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        fallback = "safe-uuid-1234"

        # Strip whitespace from job runId
        result = mod._canonical_run_id(fallback, {"runId": "  abc-123  "})
        self.assertEqual(result, "abc-123")

        # Strip whitespace from fallback when job has no runId
        result = mod._canonical_run_id("  fallback-id  ", {})
        self.assertEqual(result, "fallback-id")

        # Reject newline in job runId → fall back to stripped run_id
        result = mod._canonical_run_id(fallback, {"runId": "bad\nid"})
        self.assertEqual(result, fallback)

        # Reject tab in job runId
        result = mod._canonical_run_id(fallback, {"runId": "bad\tid"})
        self.assertEqual(result, fallback)

        # Reject NUL in job runId
        result = mod._canonical_run_id(fallback, {"runId": "bad\x00id"})
        self.assertEqual(result, fallback)

        # Reject DEL (0x7f) in job runId
        result = mod._canonical_run_id(fallback, {"runId": "bad\x7fid"})
        self.assertEqual(result, fallback)

        # Clean job runId passes through
        result = mod._canonical_run_id(fallback, {"runId": "clean-id-99"})
        self.assertEqual(result, "clean-id-99")

    def test_artifacts_obj_nonexistent_files_are_null(self):
        """P2: _artifacts_obj returns null for optional paths that don't exist."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            job_path = d / "job.json"
            finish_path = d / "finish.json"
            prompt_path = d / "prompt.md"
            # Create only mandatory files
            for p in (job_path, finish_path, prompt_path):
                p.write_text("{}")
            # Optional paths that do NOT exist on disk
            events_path = d / "events.ndjson"
            stderr_path = d / "stderr.log"
            assistant_path = d / "assistant.txt"
            wrapper_log_path = d / "wrapper.log"
            result_path = d / "result.json"

            arts = mod._artifacts_obj(
                dir_path=d,
                job_path=job_path,
                finish_path=finish_path,
                prompt_path=prompt_path,
                events_path=events_path,
                stderr_path=stderr_path,
                assistant_path=assistant_path,
                wrapper_log_path=wrapper_log_path,
                result_path=result_path,
                patch_path=None,
            )
            # Non-existent optional files should be null
            self.assertIsNone(arts["eventsPath"])
            self.assertIsNone(arts["stderrPath"])
            self.assertIsNone(arts["assistantPath"])
            self.assertIsNone(arts["wrapperLogPath"])
            self.assertIsNone(arts["resultPath"])
            # Mandatory files should still have names
            self.assertEqual(arts["jobPath"], "job.json")
            self.assertEqual(arts["finishPath"], "finish.json")

    def test_artifacts_obj_existing_files_have_names(self):
        """P2: _artifacts_obj returns file names for paths that DO exist."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            job_path = d / "job.json"
            finish_path = d / "finish.json"
            prompt_path = d / "prompt.md"
            events_path = d / "events.ndjson"
            stderr_path = d / "stderr.log"
            # Create all files
            for p in (job_path, finish_path, prompt_path, events_path, stderr_path):
                p.write_text("{}")

            arts = mod._artifacts_obj(
                dir_path=d,
                job_path=job_path,
                finish_path=finish_path,
                prompt_path=prompt_path,
                events_path=events_path,
                stderr_path=stderr_path,
                assistant_path=None,
                wrapper_log_path=None,
                result_path=None,
                patch_path=None,
            )
            self.assertEqual(arts["eventsPath"], "events.ndjson")
            self.assertEqual(arts["stderrPath"], "stderr.log")
            # None paths stay None
            self.assertIsNone(arts["assistantPath"])
            self.assertIsNone(arts["wrapperLogPath"])
            self.assertIsNone(arts["resultPath"])

    def test_cancel_idempotent_when_finish_exists(self):
        """P1: cmd_cancel returns early with alreadyFinished=true when
        finish.json already contains a valid terminal state."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"
        with tempfile.TemporaryDirectory() as td:
            artifacts_dir = Path(td)
            # Write a valid finish.json (successful completion)
            finish = {
                "ok": True,
                "exitCode": 0,
                "runId": "already-done",
                "summary": "completed successfully",
            }
            (artifacts_dir / "finish.json").write_text(
                json.dumps(finish), encoding="utf-8"
            )
            # Write a job.json with a PID that doesn't exist
            job = {
                "runId": "already-done",
                "workdir": str(repo_root),
                "state": "finished",
                "createdAt": int(time.time() * 1000),
                "updatedAt": int(time.time() * 1000),
                "pid": 0,
            }
            (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
            out = json.loads(result.stdout.strip())
            self.assertTrue(out.get("ok"))
            self.assertTrue(out.get("alreadyFinished"))
            self.assertEqual(out["existingFinish"]["runId"], "already-done")
            # job.json state should NOT have been changed to 'canceled'
            job_after = json.loads(
                (artifacts_dir / "job.json").read_text(encoding="utf-8")
            )
            self.assertEqual(job_after.get("state"), "finished")

    def test_cancel_telemetry_no_state_overwrite_when_finish_exists(self):
        """P1b: When _write_finish_once returns 'exists' during cancel,
        telemetry must NOT overwrite job.state to 'canceled'."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        with tempfile.TemporaryDirectory() as td:
            artifacts_dir = Path(td)
            finish_path = artifacts_dir / "finish.json"
            job_path = artifacts_dir / "job.json"

            # Pre-existing successful finish
            finish = {"ok": True, "exitCode": 0, "runId": "success-run"}
            finish_path.write_text(json.dumps(finish), encoding="utf-8")

            # Job in 'finished' state
            job = {
                "runId": "success-run",
                "workdir": str(repo_root),
                "state": "finished",
                "createdAt": int(time.time() * 1000),
                "updatedAt": int(time.time() * 1000),
                "pid": 0,
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")

            # Try to write a cancel finish — should return 'exists'
            cancel_finish = {
                "ok": False,
                "exitCode": 130,
                "runId": "success-run",
                "engine": "cancel",
            }
            written, reason, existing = mod._write_finish_once(
                artifacts_dir=artifacts_dir,
                finish_path=finish_path,
                finish_obj=cancel_finish,
            )
            self.assertFalse(written)
            self.assertEqual(reason, "exists")
            self.assertIsInstance(existing, dict)
            self.assertTrue(existing["ok"])  # original success preserved

    def test_emit_synthesized_finish_guards_job_state_on_write_failure(self):
        """P0: _emit_synthesized_missing_finish must NOT set job state='failed'
        when _write_finish_once itself fails (write_failed reason)."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        with tempfile.TemporaryDirectory() as td:
            artifacts_dir = Path(td)
            job_path = artifacts_dir / "job.json"
            finish_path = artifacts_dir / "finish.json"

            # Job in 'running' state
            job = {
                "runId": "synth-test",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": int(time.time() * 1000),
                "updatedAt": int(time.time() * 1000),
                "pid": 0,
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")

            # Patch _write_json to fail ONLY for finish.json
            original_write_json = mod._write_json

            def _selective_failing_write_json(path, obj):
                if Path(path).name == "finish.json":
                    raise OSError("disk full")
                return original_write_json(path, obj)

            with unittest.mock.patch.object(
                mod, "_write_json", _selective_failing_write_json
            ):
                out = mod._emit_synthesized_missing_finish(
                    artifacts_dir=artifacts_dir,
                    job_path=job_path,
                    finish_path=finish_path,
                    job=job,
                    run_id="synth-test",
                    error_name="ProcessVanished",
                    error_message="test vanish",
                )

            # Output should still be the synthesized failure
            self.assertFalse(out.get("ok"))
            # Job state should NOT have been set to 'failed' —
            # instead lastError should record the write failure
            job_after = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertNotEqual(
                job_after.get("state"),
                "failed",
                "job.state must not transition to 'failed' when finish.json "
                "write itself failed",
            )
            self.assertIn("FinishWriteFailed", str(job_after.get("lastError", {})))

    def test_cancel_idempotency_rejects_empty_finish(self):
        """Codex P1a: cancel idempotency guard must NOT trigger for an empty
        finish.json ({}) — only for a structurally complete finish with 'ok'."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"
        with tempfile.TemporaryDirectory() as td:
            artifacts_dir = Path(td)
            # Write an EMPTY finish.json (structurally incomplete)
            (artifacts_dir / "finish.json").write_text(json.dumps({}), encoding="utf-8")
            # Write a job.json with pid=0
            job = {
                "runId": "empty-finish-test",
                "workdir": str(repo_root),
                "state": "running",
                "createdAt": int(time.time() * 1000),
                "updatedAt": int(time.time() * 1000),
                "pid": 0,
            }
            (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = json.loads(result.stdout.strip())
            # Should NOT have alreadyFinished — the empty finish is not valid
            self.assertFalse(
                out.get("alreadyFinished", False),
                "cancel must not treat an empty {} finish.json as a valid "
                "terminal state",
            )

    def test_cancel_idempotency_accepts_complete_finish(self):
        """Codex P1a: cancel idempotency guard triggers for finish.json
        that contains the 'ok' key (structurally complete)."""
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"
        with tempfile.TemporaryDirectory() as td:
            artifacts_dir = Path(td)
            # Write a COMPLETE finish.json
            finish = {"ok": False, "exitCode": 1, "runId": "complete-test"}
            (artifacts_dir / "finish.json").write_text(
                json.dumps(finish), encoding="utf-8"
            )
            job = {
                "runId": "complete-test",
                "workdir": str(repo_root),
                "state": "failed",
                "createdAt": int(time.time() * 1000),
                "updatedAt": int(time.time() * 1000),
                "pid": 0,
            }
            (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "cancel",
                    "--artifacts-dir",
                    str(artifacts_dir),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout.strip())
            self.assertTrue(out.get("alreadyFinished"))
            self.assertTrue(out.get("ok"))
            # Job state must remain unchanged
            job_after = json.loads(
                (artifacts_dir / "job.json").read_text(encoding="utf-8")
            )
            self.assertEqual(job_after.get("state"), "failed")

    # ── PR #12 regression tests ────────────────────────────────────────

    def test_run_cli_posix_sets_start_new_session(self):
        """Fix B: _run_cli must set start_new_session=True on POSIX so
        _kill_tree's os.killpg targets only the subtask process group."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)
        captured_kwargs: dict = {}

        def _capture_popen(*a, **kw):
            captured_kwargs.update(kw)
            raise OSError("intentional — just capturing kwargs")

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_run_cli_posix_") as td:
            td_path = Path(td)
            workdir = td_path
            prompt_path = td_path / "prompt.txt"
            prompt_path.write_text(
                "Act as a senior software engineer.\n", encoding="utf-8"
            )
            stderr_path = td_path / "stderr.log"

            with unittest.mock.patch.object(mod.subprocess, "Popen", _capture_popen):
                try:
                    mod._run_cli(
                        opencode_bin="__dummy_opencode__",
                        workdir=workdir,
                        env=dict(os.environ),
                        attach_url=None,
                        prompt_path=prompt_path,
                        continue_last=False,
                        session_id=None,
                        title=None,
                        agent=None,
                        model=None,
                        variant=None,
                        files=[],
                        timeout_s=1.0,
                        quiet=True,
                        save_events=False,
                        save_text=False,
                        max_artifact_bytes=0,
                        events_path=None,
                        stderr_path=stderr_path,
                        assistant_path=None,
                        on_session_id=None,
                    )
                except Exception:
                    pass  # expected

        self.assertTrue(
            captured_kwargs.get("start_new_session"),
            "_run_cli must set start_new_session=True on POSIX",
        )

    def test_cmd_start_execution_profile_uses_run_timeout_for_short_class(self):
        """Fix A: cmd_start should apply execution profile using the effective
        worker runtime timeout (run_timeout_s), not the legacy args.timeout."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        p = argparse.ArgumentParser(prog="start-test")
        mod._add_common_run_flags(p)
        p.add_argument("prompt", nargs=argparse.REMAINDER)

        captured_cmd: list[str] | None = None

        def _fake_popen(cmd, *a, **kw):
            nonlocal captured_cmd
            captured_cmd = list(cmd)
            # Close wrapper log so TemporaryDirectory cleanup works on Windows.
            out = kw.get("stdout")
            err = kw.get("stderr")
            try:
                if out is not None and hasattr(out, "close"):
                    out.close()
            except Exception:
                pass
            try:
                if err is not None and err is not out and hasattr(err, "close"):
                    err.close()
            except Exception:
                pass

            class _Proc:
                pid = 12345

            return _Proc()

        with tempfile.TemporaryDirectory(
            prefix="ocsubtask_test_cmd_start_profile_"
        ) as td:
            artifacts_dir = Path(td) / "artifacts"
            args = p.parse_args(
                [
                    "--workdir",
                    str(repo_root),
                    "--engine",
                    "auto",
                    "--execution-profile",
                    "hybrid",
                    "--run-id",
                    "profile-short-test",
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--opencode",
                    "__dummy_opencode__",
                    "--prompt",
                    "Act as a senior software engineer.",
                    "--run-timeout",
                    "30",
                    # If cmd_start incorrectly uses legacy args.timeout(600),
                    # this threshold would classify the job as long.
                    "--hybrid-short-timeout-s",
                    "100",
                ]
            )

            with unittest.mock.patch.object(mod.subprocess, "Popen", _fake_popen):
                rc = mod.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertIsNotNone(captured_cmd)
        assert captured_cmd is not None
        engine = captured_cmd[captured_cmd.index("--engine") + 1]
        self.assertEqual(engine, "http")
        self.assertIn("--no-save-events", captured_cmd)
        self.assertIn("--no-save-text", captured_cmd)

    def test_cmd_start_execution_profile_merges_env_thresholds(self):
        """Fix A: cmd_start should merge --env/--env-file into the env passed to
        _apply_execution_profile so hybrid thresholds are honored."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        p = argparse.ArgumentParser(prog="start-test-env")
        mod._add_common_run_flags(p)
        p.add_argument("prompt", nargs=argparse.REMAINDER)

        captured_cmd: list[str] | None = None

        def _fake_popen(cmd, *a, **kw):
            nonlocal captured_cmd
            captured_cmd = list(cmd)
            out = kw.get("stdout")
            err = kw.get("stderr")
            try:
                if out is not None and hasattr(out, "close"):
                    out.close()
            except Exception:
                pass
            try:
                if err is not None and err is not out and hasattr(err, "close"):
                    err.close()
            except Exception:
                pass

            class _Proc:
                pid = 23456

            return _Proc()

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_cmd_start_env_") as td:
            artifacts_dir = Path(td) / "artifacts"
            args = p.parse_args(
                [
                    "--workdir",
                    str(repo_root),
                    "--engine",
                    "auto",
                    "--execution-profile",
                    "hybrid",
                    "--run-id",
                    "profile-env-test",
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--opencode",
                    "__dummy_opencode__",
                    "--prompt",
                    "Act as a senior software engineer.",
                    "--run-timeout",
                    "30",
                    # If --env is merged, threshold=10 => classify as long (CLI + full artifacts)
                    "--env",
                    "OPENCODE_SUBTASK_HYBRID_SHORT_TIMEOUT_S=10",
                ]
            )

            with unittest.mock.patch.object(mod.subprocess, "Popen", _fake_popen):
                rc = mod.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertIsNotNone(captured_cmd)
        assert captured_cmd is not None
        engine = captured_cmd[captured_cmd.index("--engine") + 1]
        self.assertEqual(engine, "cli")
        self.assertIn("--save-events", captured_cmd)
        self.assertIn("--save-text", captured_cmd)

    # ── PR #17 regression tests (review10) ─────────────────────────────

    def test_status_returns_0_for_failed_task(self) -> None:
        """Exit code semantics: status observing a failed task should return 0
        (observation succeeded), not 1.  Task outcome lives in stdout JSON."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_exitcode_") as td:
            artifacts_dir = Path(td)
            finish = {
                "type": "opencode-subtask-finish",
                "ok": False,
                "exitCode": 1,
                "runId": "exitcode-test",
                "error": {"name": "Timeout", "message": "timed out"},
            }
            (artifacts_dir / "finish.json").write_text(
                json.dumps(finish), encoding="utf-8"
            )

            p = argparse.ArgumentParser()
            p.add_argument("--run-id", default="exitcode-test")
            p.add_argument("--artifacts-dir", default=str(artifacts_dir))
            p.add_argument("--timeout", default="600")
            args = p.parse_args([])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_status(args)

            self.assertEqual(rc, 0, "status should return 0 when observation succeeds")
            out = json.loads(buf.getvalue().strip())
            self.assertFalse(out["ok"], "stdout JSON should still report ok=false")

    def test_wait_returns_0_for_failed_task(self) -> None:
        """Exit code semantics: wait observing a failed task should return 0."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_wait_exitcode_") as td:
            artifacts_dir = Path(td)
            finish = {
                "type": "opencode-subtask-finish",
                "ok": False,
                "exitCode": 1,
                "runId": "wait-exitcode-test",
                "error": {"name": "EmptyModelOutput", "message": "empty"},
            }
            (artifacts_dir / "finish.json").write_text(
                json.dumps(finish), encoding="utf-8"
            )
            (artifacts_dir / "job.json").write_text(
                json.dumps({"runId": "wait-exitcode-test", "pid": 99999}),
                encoding="utf-8",
            )

            p = argparse.ArgumentParser()
            p.add_argument("--run-id", default="wait-exitcode-test")
            p.add_argument("--artifacts-dir", default=str(artifacts_dir))
            p.add_argument("--timeout", default="600")
            p.add_argument("--wait-timeout", default="5")
            p.add_argument("--poll-interval", default="0.1")
            args = p.parse_args([])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_wait(args)

            self.assertEqual(rc, 0, "wait should return 0 when observation succeeds")
            out = json.loads(buf.getvalue().strip())
            self.assertFalse(out["ok"])

    def test_run_id_path_traversal_rejected(self) -> None:
        """run_id with path traversal components must be rejected."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        bad_ids = [
            "../escape",
            "..\\escape",
            "foo/../bar",
            "foo/bar",
            "foo\\bar",
            "hello world",  # space
            "run_123;rm -rf /",  # semicolon
        ]
        for bad_id in bad_ids:
            with self.assertRaises(ValueError, msg=f"should reject run_id={bad_id!r}"):
                mod._validate_run_id_for_path(bad_id)

    def test_safe_resolve_artifacts_dir_emits_bad_run_id(self) -> None:
        """_safe_resolve_artifacts_dir must exit with BadRunId (exit 2)."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                mod._safe_resolve_artifacts_dir("../escape", None)
        self.assertEqual(cm.exception.code, 2)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["name"], "BadRunId")

    def test_safe_merge_env_emits_bad_args(self) -> None:
        """_safe_merge_env must exit with BadArgs (exit 2) for malformed env."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                mod._safe_merge_env({}, ["NO_EQUALS_SIGN"], [])
        self.assertEqual(cm.exception.code, 2)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["name"], "BadArgs")

    def test_safe_merge_env_oserror_emits_bad_args(self) -> None:
        """_safe_merge_env must exit with BadArgs (exit 2) for missing env-file."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                mod._safe_merge_env({}, [], ["MY_VAR=/nonexistent/path/to/file.txt"])
        self.assertEqual(cm.exception.code, 2)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["name"], "BadArgs")

    def test_run_id_valid_patterns_accepted(self) -> None:
        """Normal run_id patterns must be accepted."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        good_ids = [
            "run_1234567890_12345",
            "my-custom-run.v2",
            "test_run_001",
            "a",
        ]
        for good_id in good_ids:
            # Should not raise
            mod._validate_run_id_for_path(good_id)

    # ── PR #19 regression tests (review12) ─────────────────────────────

    def test_resolve_artifacts_dir_containment(self) -> None:
        """_resolve_artifacts_dir must reject run_id that resolves outside
        _runs_dir even if the regex whitelist somehow passes."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        # Normal valid run_id should work fine
        rid, ad = mod._resolve_artifacts_dir("test_run_001", None)
        self.assertEqual(rid, "test_run_001")
        self.assertTrue(str(ad).endswith("test_run_001"))

        # Explicit --artifacts-dir bypasses containment (by design)
        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_ad_") as td:
            rid2, ad2 = mod._resolve_artifacts_dir("anything", td)
            self.assertEqual(str(ad2), str(Path(td).resolve()))

    def test_scan_running_job_skips_finished_jobs(self) -> None:
        """_scan_running_job_server_urls must skip jobs with ANY readable
        finish.json, not just canceled ones.  A finished (ok=true or ok=false)
        job should never contribute crashed-owner evidence."""
        repo_root = Path(__file__).resolve().parents[1]
        mod = self._load_adapter_module(repo_root)

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_reaper_") as td:
            runs_dir = Path(td)

            # Job 1: running, no finish → should contribute active/crashed
            run1 = runs_dir / "run_active"
            run1.mkdir()
            (run1 / "job.json").write_text(
                json.dumps(
                    {
                        "runId": "run_active",
                        "state": "running",
                        "pid": 99999999,  # non-existent PID
                        "serverUrl": "http://localhost:11111",
                        "serverStartedNew": True,
                        "workdir": str(repo_root),
                    }
                ),
                encoding="utf-8",
            )

            # Job 2: running state in job.json BUT has finish.json (ok=true)
            run2 = runs_dir / "run_finished_ok"
            run2.mkdir()
            (run2 / "job.json").write_text(
                json.dumps(
                    {
                        "runId": "run_finished_ok",
                        "state": "running",
                        "pid": 99999998,
                        "serverUrl": "http://localhost:22222",
                        "serverStartedNew": True,
                        "workdir": str(repo_root),
                    }
                ),
                encoding="utf-8",
            )
            (run2 / "finish.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "type": "opencode-subtask-finish",
                    }
                ),
                encoding="utf-8",
            )

            # Job 3: running state BUT has finish.json (ok=false, non-cancel)
            run3 = runs_dir / "run_finished_fail"
            run3.mkdir()
            (run3 / "job.json").write_text(
                json.dumps(
                    {
                        "runId": "run_finished_fail",
                        "state": "running",
                        "pid": 99999997,
                        "serverUrl": "http://localhost:33333",
                        "serverStartedNew": True,
                        "workdir": str(repo_root),
                    }
                ),
                encoding="utf-8",
            )
            (run3 / "finish.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "type": "opencode-subtask-finish",
                        "error": {"name": "Timeout", "message": "timed out"},
                    }
                ),
                encoding="utf-8",
            )

            # Patch _runs_dir to use our temp directory
            original_runs_dir = mod._runs_dir
            mod._runs_dir = lambda: runs_dir
            try:
                _active, crashed = mod._scan_running_job_server_urls(
                    current_project_key=mod._project_key(repo_root),
                )
            finally:
                mod._runs_dir = original_runs_dir

            # Job 1 (no finish) → crashed-owner (PID doesn't exist)
            self.assertIn(
                "http://localhost:11111",
                crashed,
                "job without finish.json should be in crashed_owner_urls",
            )
            # Job 2 (finish ok=true) → skipped entirely
            self.assertNotIn(
                "http://localhost:22222",
                crashed,
                "finished ok=true job should NOT be in crashed_owner_urls",
            )
            # Job 3 (finish ok=false) → skipped entirely
            self.assertNotIn(
                "http://localhost:33333",
                crashed,
                "finished ok=false job should NOT be in crashed_owner_urls",
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
