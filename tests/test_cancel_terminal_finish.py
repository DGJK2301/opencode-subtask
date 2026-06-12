import argparse
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(REPO_ROOT / "scripts" / "opencode_subtask.py")


class TestOpencodeSubtaskV3(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = cls._load_adapter_module(REPO_ROOT)

    @staticmethod
    def _load_adapter_module(repo_root: Path):
        script = repo_root / "scripts" / "opencode_subtask.py"
        module_name = "opencode_subtask_v3_test_module"
        spec = importlib.util.spec_from_file_location(module_name, script)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod

    def setUp(self) -> None:
        self._tmp_paths: list[Path] = []

    def tearDown(self) -> None:
        for path in reversed(self._tmp_paths):
            shutil.rmtree(path, ignore_errors=True)

    def _mktempdir(self, prefix: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self._tmp_paths.append(path)
        return path

    def _build_run_args(
        self,
        *,
        artifacts_dir: Path,
        run_id: str,
        engine: str = "cli",
        extra_args: list[str] | None = None,
    ):
        p = argparse.ArgumentParser(prog="run-test")
        self._mod._add_common_run_flags(p)
        p.add_argument("prompt", nargs=argparse.REMAINDER)
        argv = [
            "--workdir",
            str(REPO_ROOT),
            "--engine",
            engine,
            "--run-id",
            run_id,
            "--artifacts-dir",
            str(artifacts_dir),
            "--opencode",
            "__dummy_opencode__",
            "--prompt",
            "Act as a senior software engineer.",
            "--retry-empty-output",
            "--empty-output-retries",
            "1",
        ]
        if extra_args:
            argv.extend(extra_args)
        return p.parse_args(argv)

    def _make_finish(
        self,
        *,
        artifacts_dir: Path,
        run_id: str = "finish-test",
        outcome: str = "completed",
        exit_code: int = 0,
        engine_selected: str = "cli",
        fallback_from: str | None = None,
        execution_error: dict | None = None,
    ) -> dict:
        job_path = artifacts_dir / "job.json"
        finish_path = artifacts_dir / "finish.json"
        return self._mod._finish_obj(
            run_id=run_id,
            workdir=REPO_ROOT,
            outcome=outcome,
            exit_code=exit_code,
            duration_ms=1,
            engine_selected=engine_selected,
            fallback_from=fallback_from,
            session_id=None,
            execution_error=execution_error,
            execution_warnings=[],
            changed_files=[],
            untracked_files=[],
            patch_path=None,
            artifacts=self._mod._minimal_artifacts(
                dir_path=artifacts_dir,
                job_path=job_path,
                finish_path=finish_path,
            ),
        )

    def _write_finish(self, artifacts_dir: Path, finish: dict) -> Path:
        finish_path = artifacts_dir / "finish.json"
        finish_path.write_text(json.dumps(finish), encoding="utf-8")
        return finish_path

    def _assert_v3_finish_shape(self, obj: dict) -> None:
        self.assertLessEqual(
            set(obj),
            {
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
            },
        )
        artifacts = obj.get("artifacts")
        self.assertIsInstance(artifacts, dict)
        self.assertLessEqual(
            set(artifacts),
            {
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
            },
        )

    def _mocked_run(
        self,
        *,
        text: str = "assistant text",
        run_outcome=None,
        changed_files: list[str] | None = None,
        untracked_files: list[str] | None = None,
        patch_name: str | None = None,
    ) -> tuple[int, dict, Path]:
        artifacts_dir = self._mktempdir("ocsubtask_v3_run_")
        args = self._build_run_args(
            artifacts_dir=artifacts_dir,
            run_id=f"run-{artifacts_dir.name}",
        )
        outcome = run_outcome or self._mod.RunOutcome(
            ok=True,
            exit_code=0,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id="ses_test",
            full_text=text,
            metrics={"tokens": {"output": 1}},
            error=None,
        )

        buf = io.StringIO()
        with (
            unittest.mock.patch.object(
                self._mod,
                "_resolve_executable_for_workdir",
                return_value="__dummy_opencode__",
            ),
            unittest.mock.patch.object(
                self._mod,
                "_apply_execution_profile",
                side_effect=lambda args, prompt, env: {"profile": args.execution_profile},
            ),
            unittest.mock.patch.object(self._mod, "_run_cli", return_value=outcome),
            unittest.mock.patch.object(
                self._mod,
                "_git_status",
                return_value=(changed_files or [], untracked_files or []),
            ),
            unittest.mock.patch.object(self._mod, "_git_patch", return_value=patch_name),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_run(args)
        obj = json.loads(buf.getvalue().strip().splitlines()[-1])
        return rc, obj, artifacts_dir

    def _run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )

    def test_validate_finish_accepts_v3_shape(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v3_finish_")
        finish = self._make_finish(artifacts_dir=artifacts_dir)

        self.assertIsNone(self._mod._validate_finish_envelope(finish))
        self.assertEqual(finish["schemaVersion"], 3)
        self._assert_v3_finish_shape(finish)

    def test_validate_finish_rejects_unknown_top_level_fields(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v3_unknown_top_")
        finish = self._make_finish(artifacts_dir=artifacts_dir)
        finish["unexpectedField"] = "not-schema-v3"

        err = self._mod._validate_finish_envelope(finish)
        self.assertIsInstance(err, str)
        self.assertIn("unexpectedField", err)

    def test_validate_finish_rejects_unknown_artifact_fields(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v3_unknown_artifacts_")
        finish = self._make_finish(artifacts_dir=artifacts_dir)
        finish["artifacts"]["unexpectedArtifact"] = "x.json"

        err = self._mod._validate_finish_envelope(finish)
        self.assertIsInstance(err, str)
        self.assertIn("unexpectedArtifact", err)

    def test_run_writes_v3_finish_shape(self) -> None:
        rc, out, artifacts_dir = self._mocked_run(text="done")

        self.assertEqual(rc, 0)
        self.assertEqual(out["type"], "opencode-subtask-finish")
        self.assertEqual(out["schemaVersion"], 3)
        self.assertEqual(out["outcome"], "completed")
        self._assert_v3_finish_shape(out)

        persisted = json.loads((artifacts_dir / "finish.json").read_text(encoding="utf-8"))
        self._assert_v3_finish_shape(persisted)

    def test_run_empty_output_is_failed_with_v3_shape(self) -> None:
        outcome = self._mod.RunOutcome(
            ok=True,
            exit_code=0,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id="ses_empty",
            full_text="",
            metrics=None,
            error=None,
        )
        rc, out, _ = self._mocked_run(text="", run_outcome=outcome)

        self.assertEqual(rc, 3)
        self.assertEqual(out["outcome"], "failed")
        self.assertEqual(out["execution"]["error"]["name"], "EmptyModelOutput")
        self._assert_v3_finish_shape(out)

    def test_status_returns_terminal_finish_unchanged(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v3_status_")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        finish = self._make_finish(artifacts_dir=artifacts_dir, run_id="status-run")
        self._write_finish(artifacts_dir, finish)

        args = argparse.Namespace(run_id="status-run", artifacts_dir=str(artifacts_dir))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._mod.cmd_status(args)

        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["runId"], "status-run")
        self._assert_v3_finish_shape(out)

    def test_wait_uses_finish_outcome_exit_code(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v3_wait_")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            run_id="wait-run",
            outcome="timed_out",
            exit_code=124,
            execution_error={"name": "Timeout", "message": "timeout"},
        )
        self._write_finish(artifacts_dir, finish)

        args = argparse.Namespace(
            run_id="wait-run",
            artifacts_dir=str(artifacts_dir),
            poll_interval=0.01,
            wait_timeout=0.01,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._mod.cmd_wait(args)

        self.assertEqual(rc, 124)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["outcome"], "timed_out")
        self._assert_v3_finish_shape(out)

    def test_cancel_no_active_worker_writes_v3_finish(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v3_cancel_")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        job_path = artifacts_dir / "job.json"
        finish_path = artifacts_dir / "finish.json"
        job_path.write_text(
            json.dumps(
                {
                    "runId": "cancel-run",
                    "adapterVersion": self._mod.ADAPTER_VERSION,
                    "workdir": str(REPO_ROOT),
                    "state": "running",
                    "pid": 0,
                    "createdAt": self._mod._now_ms(),
                    "updatedAt": self._mod._now_ms(),
                }
            ),
            encoding="utf-8",
        )

        args = argparse.Namespace(
            run_id="cancel-run",
            artifacts_dir=str(artifacts_dir),
            env=[],
            env_file=[],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self._mod.cmd_cancel(args)

        self.assertEqual(rc, 1)
        cancel_out = json.loads(buf.getvalue())
        self.assertEqual(cancel_out["type"], "opencode-subtask-cancel")
        self.assertFalse(cancel_out["ok"])
        finish = json.loads(finish_path.read_text(encoding="utf-8"))
        self.assertEqual(finish["type"], "opencode-subtask-finish")
        self.assertEqual(finish["schemaVersion"], 3)
        self.assertEqual(finish["outcome"], "internal_error")
        self._assert_v3_finish_shape(finish)

    def test_unknown_public_flags_are_rejected(self) -> None:
        proc = self._run_script(
            "run",
            "--unknown-adapter-flag",
            "--prompt",
            "Act as a senior software engineer.",
        )

        self.assertEqual(proc.returncode, 2)
        out = json.loads(proc.stdout)
        self.assertEqual(out["error"]["name"], "BadArgs")

        proc = self._run_script(
            "ask",
            "--unknown-ask-flag",
            "--prompt",
            "plain request",
        )

        self.assertEqual(proc.returncode, 2)
        out = json.loads(proc.stdout)
        self.assertEqual(out["error"]["name"], "BadArgs")

    def test_unknown_command_is_rejected(self) -> None:
        proc = self._run_script("unknown-command", "--finish", "finish.json")

        self.assertEqual(proc.returncode, 2)
        out = json.loads(proc.stdout)
        self.assertEqual(out["error"]["name"], "BadArgs")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
