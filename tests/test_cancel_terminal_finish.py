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
import threading
import unittest
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
                finish_path.exists(), f"finish.json was not written; stdout={proc.stdout!r}"
            )
            fin = json.loads(finish_path.read_text(encoding="utf-8"))
            self.assertEqual(fin.get("type"), "opencode-subtask-finish")
            self.assertIsInstance(fin.get("error"), dict)
            self.assertEqual(fin["error"].get("name"), "Canceled")

    def test_status_fail_fast_when_finish_unreadable(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "opencode_subtask.py"

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_unreadable_status_") as td:
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

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_unreadable_wait_") as td:
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

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_wait_timeout_override_") as td:
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

    def test_cancel_does_not_overwrite_existing_finish(self) -> None:
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

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_run_reuse_finish_") as td:
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
                out = json.loads((artifacts_dir / "job.json").read_text(encoding="utf-8"))
                self.assertTrue(bool(out.get("cancelUnverified")))
                fin = json.loads((artifacts_dir / "finish.json").read_text(encoding="utf-8"))
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

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_empty_output_fail_") as td:
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
                mod._resolve_executable_for_workdir = lambda cmd, wd: "__dummy_opencode__"
                mod._run_cli = fake_run_cli
                mod._git_status = lambda wd: ([], [])
                mod._git_patch = lambda wd, ad: None

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mod.cmd_run(args)
                self.assertEqual(calls["n"], 2)
                self.assertNotEqual(rc, 0)
                fin = json.loads(buf.getvalue().strip())
                self.assertEqual((fin.get("error") or {}).get("name"), "EmptyModelOutput")
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

        with tempfile.TemporaryDirectory(prefix="ocsubtask_test_empty_output_recover_") as td:
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
                mod._resolve_executable_for_workdir = lambda cmd, wd: "__dummy_opencode__"
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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
