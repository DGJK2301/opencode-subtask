import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(REPO_ROOT / "scripts" / "opencode_subtask.py")


class TestOpencodeSubtaskV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = cls._load_adapter_module(REPO_ROOT)

    @staticmethod
    def _load_adapter_module(repo_root: Path):
        script = repo_root / "scripts" / "opencode_subtask.py"
        module_name = "opencode_subtask_v2_test_module"
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
        output_mode: str,
        diagnostics: str = "on-failure",
        nonce: str | None = None,
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
            "--output-mode",
            output_mode,
            "--diagnostics",
            diagnostics,
            "--retry-empty-output",
            "--empty-output-retries",
            "1",
        ]
        if nonce:
            argv.extend(["--contract-nonce", nonce])
        if extra_args:
            argv.extend(extra_args)
        return p.parse_args(argv)

    def _build_payload_text(
        self, *, nonce: str, decision: str, **overrides: object
    ) -> str:
        begin, end = self._mod._sentinel_markers(nonce)
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": nonce,
            "decision": decision,
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload.update(overrides)
        return f"{begin}\n{json.dumps(payload)}\n{end}\n"

    def _make_finish(
        self,
        *,
        artifacts_dir: Path,
        run_id: str = "finish-test",
        output_mode: str = "machine",
        outcome: str = "completed",
        exit_code: int = 0,
        fallback_from: str | None = None,
        execution_error: dict | None = None,
        execution_warnings: list[dict] | None = None,
        payload_status: str = "missing",
        payload_schema: str | None = None,
        payload_artifact_path: str | None = None,
        payload_digest: str | None = None,
        payload_errors: list[dict] | None = None,
        decision_status: str = "unavailable",
        decision_route: str | None = None,
    ) -> dict:
        return {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": run_id,
            "workdir": str(REPO_ROOT),
            "outputMode": output_mode,
            "outcome": outcome,
            "execution": {
                "exitCode": exit_code,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": fallback_from},
                "sessionId": None,
                "error": execution_error,
                "warnings": execution_warnings or [],
            },
            "payload": {
                "status": payload_status,
                "schema": payload_schema,
                "artifact": {
                    "path": payload_artifact_path,
                    "digest": payload_digest,
                },
                "errors": payload_errors or [],
            },
            "decision": {"status": decision_status, "route": decision_route},
            "workspace": {
                "changedFiles": [],
                "untrackedFiles": [],
                "patchPath": None,
            },
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": payload_artifact_path,
                "diagnosticsPath": None,
            },
        }

    def _write_finish(self, artifacts_dir: Path, finish: dict) -> Path:
        finish_path = artifacts_dir / "finish.json"
        finish_path.write_text(json.dumps(finish), encoding="utf-8")
        return finish_path

    def _make_fake_opencode(self, script_body: str) -> Path:
        fake_dir = self._mktempdir("ocsubtask_fake_opencode_")
        script_path = fake_dir / "fake_opencode_impl.py"
        script_path.write_text(script_body, encoding="utf-8")
        if os.name == "nt":
            wrapper_path = fake_dir / "fake-opencode.cmd"
            wrapper_path.write_text(
                '@echo off\r\n"%s" "%%~dp0fake_opencode_impl.py" %%*\r\n'
                % sys.executable,
                encoding="utf-8",
            )
        else:
            wrapper_path = fake_dir / "fake-opencode"
            wrapper_path.write_text(
                "#!/bin/sh\n"
                'exec "%s" "$0.impl.py" "$@"\n' % sys.executable,
                encoding="utf-8",
            )
            impl_link = fake_dir / "fake-opencode.impl.py"
            impl_link.write_text(script_body, encoding="utf-8")
            wrapper_path.chmod(0o755)
            return wrapper_path
        return wrapper_path

    def _mocked_run(
        self,
        *,
        text: str,
        output_mode: str = "machine",
        diagnostics: str = "on-failure",
        nonce: str | None = None,
        changed_files: list[str] | None = None,
        untracked_files: list[str] | None = None,
        patch_name: str | None = None,
        run_outcome=None,
        fail_payload_write: bool = False,
    ) -> tuple[int, dict, Path]:
        artifacts_dir = self._mktempdir("ocsubtask_v2_run_")
        nonce_n = nonce or ("nonce-test" if output_mode == "machine" else None)
        args = self._build_run_args(
            artifacts_dir=artifacts_dir,
            run_id=f"run-{artifacts_dir.name}",
            output_mode=output_mode,
            diagnostics=diagnostics,
            nonce=nonce_n,
        )
        outcome = run_outcome or self._mod.RunOutcome(
            ok=True,
            exit_code=0,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id="ses_test",
            full_text=text,
            metrics=None,
            error=None,
        )
        original_atomic_write = self._mod._atomic_write_bytes

        def _atomic_write_wrapper(path, data, **kwargs):
            if fail_payload_write and isinstance(path, Path) and path.name == "payload.json":
                raise PermissionError("simulated payload write failure")
            return original_atomic_write(path, data, **kwargs)

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
            unittest.mock.patch.object(
                self._mod,
                "_git_patch",
                return_value=patch_name,
            ),
            unittest.mock.patch.object(
                self._mod,
                "_atomic_write_bytes",
                side_effect=_atomic_write_wrapper,
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_run(args)
        out = json.loads(buf.getvalue().strip())
        return rc, out, artifacts_dir

    def _run_cli_command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )

    def test_removed_public_flags_are_rejected(self) -> None:
        removed_flags = [
            ("--include-debug",),
            ("--max-text-chars", "10"),
            ("--no-contract",),
            ("--wrapper-log",),
            ("--timeout", "1"),
            ("--no-attach",),
        ]
        for removed in removed_flags:
            with self.subTest(flag=" ".join(removed)):
                proc = self._run_cli_command(
                    "run",
                    "--workdir",
                    str(REPO_ROOT),
                    "--opencode",
                    "__dummy_opencode__",
                    "--prompt",
                    "Act as a senior software engineer.",
                    *removed,
                )
                self.assertEqual(proc.returncode, 2, proc.stdout)
                out = json.loads(proc.stdout)
                self.assertEqual(out["type"], "opencode-subtask-error")
                self.assertEqual(out["error"]["name"], "BadArgs")
                self.assertIn(removed[0], out["error"]["message"])

    def test_wait_rejects_removed_timeout_flag(self) -> None:
        proc = self._run_cli_command("wait", "--run-id", "dummy", "--timeout", "1")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertEqual(out["type"], "opencode-subtask-error")
        self.assertEqual(out["error"]["name"], "BadArgs")
        self.assertIn("--timeout", out["error"]["message"])

    def test_legacy_execution_profile_is_rejected(self) -> None:
        proc = self._run_cli_command(
            "run",
            "--workdir",
            str(REPO_ROOT),
            "--opencode",
            "__dummy_opencode__",
            "--execution-profile",
            "legacy",
            "--prompt",
            "Act as a senior software engineer.",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertEqual(out["type"], "opencode-subtask-error")
        self.assertEqual(out["error"]["name"], "BadArgs")
        self.assertIn("--execution-profile", out["error"]["message"])

    def test_stderr_failure_error_detects_bun_crash(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_bun_crash_")
        stderr_path = artifacts_dir / "stderr.log"
        stderr_path.write_text(
            "panic(main thread): Segmentation fault at address 0x123\n"
            "oh no: Bun has crashed. This indicates a bug in Bun, not your code.\n",
            encoding="utf-8",
        )
        err = self._mod._stderr_failure_error(stderr_path, 3)
        self.assertEqual(err["name"], "EngineCrash")
        self.assertIn("runtime crashed", err["message"])

    def test_stderr_failure_error_enriches_nonzero_exit(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_nonzero_stderr_")
        stderr_path = artifacts_dir / "stderr.log"
        stderr_path.write_text("fatal: something bad happened\n", encoding="utf-8")
        err = self._mod._stderr_failure_error(stderr_path, 7)
        self.assertEqual(err["name"], "NonZeroExit")
        self.assertIn("fatal:", err["message"])

    def test_validate_finish_rejects_completed_outcome_with_execution_error(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_completed_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            outcome="completed",
            execution_error={"name": "RuntimeError", "message": "bad"},
        )
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "completed outcome requires execution.error=null",
        )

    def test_validate_finish_rejects_text_mode_payload_artifact(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_text_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            output_mode="text",
            payload_status="not_requested",
            decision_status="not_requested",
            payload_artifact_path="payload.json",
        )
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "text outputMode requires payload.artifact.path/digest=null",
        )

    def test_validate_finish_rejects_validated_payload_without_schema(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_schema_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_status="validated",
            payload_schema=None,
            payload_artifact_path="payload.json",
            payload_digest="abc",
            decision_status="determinate",
            decision_route="GO_NO_DELTA",
        )
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            f"validated payload requires payload.schema={self._mod.PAYLOAD_PROTOCOL}",
        )

    def test_validate_finish_rejects_validated_payload_without_digest(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_digest_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest=None,
            decision_status="determinate",
            decision_route="GO_NO_DELTA",
        )
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "validated payload requires payload.artifact.digest",
        )

    def test_validate_finish_rejects_validated_payload_with_unavailable_decision(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_decision_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest="abc",
            decision_status="unavailable",
        )
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "validated payload requires decision.status=determinate|abstained",
        )

    def test_validate_finish_rejects_invalid_engine_selected(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_engine_selected_")
        finish = self._make_finish(artifacts_dir=artifacts_dir)
        finish["execution"]["engine"]["selected"] = "ftp"
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "execution.engine.selected is invalid",
        )

    def test_validate_finish_rejects_invalid_engine_fallback(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_engine_fallback_")
        finish = self._make_finish(artifacts_dir=artifacts_dir)
        finish["execution"]["engine"]["fallbackFrom"] = "cli"
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "execution.engine.fallbackFrom is invalid",
        )

    def test_validate_finish_rejects_fallback_without_cli_engine(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_engine_pair_")
        finish = self._make_finish(artifacts_dir=artifacts_dir, fallback_from="http")
        finish["execution"]["engine"]["selected"] = "http"
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "execution.engine.fallbackFrom requires execution.engine.selected=cli",
        )

    def test_validate_finish_rejects_invalid_payload_error_code(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_validate_payload_error_code_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_errors=[{"code": "NOT_REAL", "message": "bad"}],
        )
        self.assertEqual(
            self._mod._validate_finish_envelope(finish),
            "payload.errors[0].code is invalid",
        )

    def test_run_machine_validated_go(self) -> None:
        nonce = "nonce-go"
        text = self._build_payload_text(nonce=nonce, decision="GO_NO_DELTA")
        rc, out, artifacts_dir = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["schemaVersion"], 2)
        self.assertEqual(out["outcome"], "completed")
        self.assertEqual(out["outputMode"], "machine")
        self.assertEqual(out["payload"]["status"], "validated")
        self.assertEqual(out["decision"]["status"], "determinate")
        self.assertEqual(out["decision"]["route"], "GO_NO_DELTA")
        payload_path = artifacts_dir / "payload.json"
        self.assertTrue(payload_path.exists())
        self.assertEqual(out["payload"]["artifact"]["path"], "payload.json")
        self.assertEqual(
            out["payload"]["artifact"]["digest"], self._mod._sha256_file(payload_path)
        )
        self.assertFalse((artifacts_dir / "diagnostics.json").exists())

    def test_run_machine_validated_when_markers_are_echoed_in_prose(self) -> None:
        nonce = "nonce-echoed"
        begin, end = self._mod._sentinel_markers(nonce)
        text = (
            f"The required terminal block uses {begin} and {end}.\n"
            f"{self._build_payload_text(nonce=nonce, decision='GO_NO_DELTA')}"
        )
        rc, out, _ = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "validated")
        self.assertEqual(out["decision"]["route"], "GO_NO_DELTA")

    def test_run_machine_validated_mandatory(self) -> None:
        nonce = "nonce-mandatory"
        text = self._build_payload_text(nonce=nonce, decision="MANDATORY_DELTA")
        rc, out, _ = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["outcome"], "completed")
        self.assertEqual(out["payload"]["status"], "validated")
        self.assertEqual(out["decision"]["route"], "MANDATORY_DELTA")

    def test_run_machine_validated_undetermined(self) -> None:
        nonce = "nonce-undetermined"
        text = self._build_payload_text(nonce=nonce, decision="UNDETERMINED")
        rc, out, _ = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "validated")
        self.assertEqual(out["decision"]["status"], "abstained")
        self.assertIsNone(out["decision"]["route"])

    def test_run_machine_missing_sentinel_emits_diagnostics(self) -> None:
        rc, out, artifacts_dir = self._mocked_run(text="plain assistant text only")
        self.assertEqual(rc, 0)
        self.assertEqual(out["outcome"], "completed")
        self.assertEqual(out["payload"]["status"], "missing")
        self.assertEqual(out["decision"]["status"], "unavailable")
        self.assertTrue((artifacts_dir / "diagnostics.json").exists())
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("PAYLOAD_MISSING", codes)

    def test_run_machine_multiple_sentinels_is_ambiguous(self) -> None:
        nonce = "nonce-ambiguous"
        text = (
            self._build_payload_text(nonce=nonce, decision="GO_NO_DELTA")
            + self._build_payload_text(nonce="other-nonce", decision="MANDATORY_DELTA")
        )
        rc, out, artifacts_dir = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "ambiguous")
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("SENTINEL_MULTIPLE", codes)
        self.assertTrue((artifacts_dir / "diagnostics.json").exists())

    def test_run_machine_trailing_markdown_after_end_is_malformed_not_ambiguous(self) -> None:
        nonce = "nonce-trailing-markdown"
        text = self._build_payload_text(nonce=nonce, decision="GO_NO_DELTA") + "---\n\nextra"
        rc, out, artifacts_dir = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "malformed")
        self.assertEqual(out["decision"]["status"], "unavailable")
        codes = [item["code"] for item in out["payload"]["errors"]]
        self.assertIn("SENTINEL_TRAILING_TEXT", codes)
        self.assertNotIn("SENTINEL_MULTIPLE", codes)
        self.assertTrue((artifacts_dir / "diagnostics.json").exists())

    def test_run_machine_trailing_text_is_malformed(self) -> None:
        nonce = "nonce-trailing"
        text = self._build_payload_text(nonce=nonce, decision="GO_NO_DELTA") + "extra"
        rc, out, _ = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "malformed")
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("SENTINEL_TRAILING_TEXT", codes)

    def test_run_machine_nonce_mismatch_is_malformed(self) -> None:
        text = self._build_payload_text(nonce="wrong-nonce", decision="GO_NO_DELTA")
        rc, out, _ = self._mocked_run(text=text, nonce="expected-nonce")
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "malformed")
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("NONCE_MISMATCH", codes)

    def test_run_machine_invalid_json_is_malformed(self) -> None:
        nonce = "nonce-json"
        begin, end = self._mod._sentinel_markers(nonce)
        text = f"{begin}\n{{not-json}}\n{end}\n"
        rc, out, _ = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "malformed")
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("PAYLOAD_JSON_INVALID", codes)

    def test_run_machine_schema_invalid_is_malformed(self) -> None:
        nonce = "nonce-schema"
        text = self._build_payload_text(
            nonce=nonce,
            decision="GO_NO_DELTA",
            summary=123,
        )
        rc, out, _ = self._mocked_run(text=text, nonce=nonce)
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "malformed")
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("PAYLOAD_SCHEMA_INVALID", codes)

    def test_run_payload_persist_failed(self) -> None:
        nonce = "nonce-persist"
        text = self._build_payload_text(nonce=nonce, decision="GO_NO_DELTA")
        rc, out, artifacts_dir = self._mocked_run(
            text=text,
            nonce=nonce,
            fail_payload_write=True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["payload"]["status"], "persist_failed")
        self.assertEqual(out["decision"]["status"], "unavailable")
        codes = {item["code"] for item in out["payload"]["errors"]}
        self.assertIn("PAYLOAD_PERSIST_FAILED", codes)
        self.assertTrue((artifacts_dir / "diagnostics.json").exists())

    def test_run_text_mode_marks_payload_not_requested(self) -> None:
        rc, out, artifacts_dir = self._mocked_run(
            text="freeform text",
            output_mode="text",
            nonce=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["outputMode"], "text")
        self.assertEqual(out["payload"]["status"], "not_requested")
        self.assertEqual(out["decision"]["status"], "not_requested")
        self.assertFalse((artifacts_dir / "payload.json").exists())
        self.assertFalse((artifacts_dir / "diagnostics.json").exists())

    def test_run_text_mode_failure_emits_diagnostics(self) -> None:
        timeout_outcome = self._mod.RunOutcome(
            ok=False,
            exit_code=124,
            timed_out=True,
            engine="cli",
            fallback_from=None,
            session_id=None,
            full_text="",
            metrics=None,
            error={"name": "Timeout", "message": "timed out"},
        )
        rc, out, artifacts_dir = self._mocked_run(
            text="",
            output_mode="text",
            nonce=None,
            run_outcome=timeout_outcome,
        )
        self.assertEqual(rc, 124)
        self.assertEqual(out["outputMode"], "text")
        self.assertEqual(out["outcome"], "timed_out")
        self.assertTrue((artifacts_dir / "diagnostics.json").exists())

    def test_run_timeout_maps_exit_code_from_execution_outcome(self) -> None:
        timeout_outcome = self._mod.RunOutcome(
            ok=False,
            exit_code=124,
            timed_out=True,
            engine="cli",
            fallback_from=None,
            session_id=None,
            full_text="",
            metrics=None,
            error={"name": "Timeout", "message": "timed out"},
        )
        rc, out, _ = self._mocked_run(text="", run_outcome=timeout_outcome)
        self.assertEqual(rc, 124)
        self.assertEqual(out["outcome"], "timed_out")

    def test_run_cli_captures_session_id_from_event_stream(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_cli_session_")
        prompt_path = artifacts_dir / "prompt.txt"
        prompt_path.write_text("prompt", encoding="utf-8")
        stderr_path = artifacts_dir / "stderr.log"

        class _FakeStdout:
            def __init__(self, chunks: list[bytes]) -> None:
                self._chunks = list(chunks)

            def readline(self) -> bytes:
                if not self._chunks:
                    return b""
                return self._chunks.pop(0)

        class _FakeProc:
            def __init__(self, chunks: list[bytes]) -> None:
                self.stdout = _FakeStdout(chunks)
                self.pid = 12345
                self.returncode = 0

            def wait(self, timeout=None) -> int:
                return 0

            def poll(self):
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        seen_session_ids: list[str] = []
        proc = _FakeProc(
            [b'{"type":"session.start","sessionID":"ses_stream"}\n', b""]
        )
        with unittest.mock.patch.object(self._mod.subprocess, "Popen", return_value=proc):
            outcome = self._mod._run_cli(
                opencode_bin="__dummy_opencode__",
                workdir=REPO_ROOT,
                env={},
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
                max_artifact_bytes=1024 * 1024,
                events_path=None,
                stderr_path=stderr_path,
                assistant_path=None,
                on_session_id=seen_session_ids.append,
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.session_id, "ses_stream")
        self.assertEqual(seen_session_ids, ["ses_stream"])

    def test_cancel_writes_cancelled_finish_when_worker_dead(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_cancel_")
        job = {
            "runId": "cancel-test",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": 1,
            "updatedAt": 1,
            "pid": 2147483647,
            "serverUrl": "http://127.0.0.1:99999",
            "sessionId": "ses_test",
            "httpAttempted": True,
            "serverStartedNew": False,
            "stopServerAfterRunMode": "never",
            "outputMode": "machine",
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        proc = self._run_cli_command("cancel", "--artifacts-dir", str(artifacts_dir))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        finish = json.loads((artifacts_dir / "finish.json").read_text(encoding="utf-8"))
        self.assertEqual(finish["outcome"], "cancelled")
        self.assertEqual(finish["payload"]["status"], "missing")

    def test_status_returns_terminal_finish_unchanged(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_status_")
        finish = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": "status-test",
            "workdir": str(REPO_ROOT),
            "outputMode": "machine",
            "outcome": "completed",
            "execution": {
                "exitCode": 0,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": None},
                "sessionId": None,
                "error": None,
                "warnings": [],
            },
            "payload": {
                "status": "missing",
                "schema": None,
                "artifact": {"path": None, "digest": None},
                "errors": [],
            },
            "decision": {"status": "unavailable", "route": None},
            "workspace": {
                "changedFiles": [],
                "untrackedFiles": [],
                "patchPath": None,
            },
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": None,
                "diagnosticsPath": None,
            },
        }
        (artifacts_dir / "finish.json").write_text(json.dumps(finish), encoding="utf-8")
        proc = self._run_cli_command("status", "--artifacts-dir", str(artifacts_dir))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), finish)

    def test_wait_timeout_returns_status_with_wait_expired(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_wait_")
        now_ms = self._mod._now_ms()
        job = {
            "runId": "wait-test",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        proc = self._run_cli_command(
            "wait",
            "--artifacts-dir",
            str(artifacts_dir),
            "--wait-timeout",
            "0.01",
            "--poll-interval",
            "0.01",
        )
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["type"], "opencode-subtask-status")
        self.assertTrue(out["waitExpired"])

    def test_wait_uses_finish_outcome_exit_code(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_wait_finish_")
        finish = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": "wait-finish-test",
            "workdir": str(REPO_ROOT),
            "outputMode": "machine",
            "outcome": "timed_out",
            "execution": {
                "exitCode": 124,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": None},
                "sessionId": None,
                "error": {"name": "Timeout", "message": "timed out"},
                "warnings": [],
            },
            "payload": {
                "status": "missing",
                "schema": None,
                "artifact": {"path": None, "digest": None},
                "errors": [{"code": "PAYLOAD_MISSING", "message": "missing"}],
            },
            "decision": {"status": "unavailable", "route": None},
            "workspace": {
                "changedFiles": [],
                "untrackedFiles": [],
                "patchPath": None,
            },
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": None,
                "diagnosticsPath": None,
            },
        }
        (artifacts_dir / "finish.json").write_text(json.dumps(finish), encoding="utf-8")
        proc = self._run_cli_command("wait", "--artifacts-dir", str(artifacts_dir))
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(json.loads(proc.stdout)["outcome"], "timed_out")

    def test_wait_synthesized_finish_uses_outcome_exit_code(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_wait_synth_")
        job = {
            "runId": "wait-synth-test",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": 1,
            "updatedAt": 1,
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        synthesized = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": "wait-synth-test",
            "workdir": str(REPO_ROOT),
            "outputMode": "machine",
            "outcome": "cancelled",
            "execution": {
                "exitCode": 130,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": None},
                "sessionId": None,
                "error": {"name": "Canceled", "message": "cancelled"},
                "warnings": [],
            },
            "payload": {
                "status": "missing",
                "schema": None,
                "artifact": {"path": None, "digest": None},
                "errors": [{"code": "PAYLOAD_MISSING", "message": "missing"}],
            },
            "decision": {"status": "unavailable", "route": None},
            "workspace": {
                "changedFiles": [],
                "untrackedFiles": [],
                "patchPath": None,
            },
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": None,
                "diagnosticsPath": None,
            },
        }
        args = argparse.Namespace(
            run_id=None,
            artifacts_dir=str(artifacts_dir),
            wait_timeout=1.0,
            poll_interval=0.01,
        )
        buf = io.StringIO()
        with (
            unittest.mock.patch.object(
                self._mod,
                "_maybe_finalize_stale_running_job",
                return_value=synthesized,
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_wait(args)
        self.assertEqual(rc, 130)
        self.assertEqual(json.loads(buf.getvalue().strip())["outcome"], "cancelled")

    def test_status_quarantines_invalid_finish_and_continues(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_status_invalid_")
        now_ms = self._mod._now_ms()
        job = {
            "runId": "status-invalid",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        (artifacts_dir / "finish.json").write_text("{}", encoding="utf-8")
        proc = self._run_cli_command("status", "--artifacts-dir", str(artifacts_dir))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertEqual(out["type"], "opencode-subtask-status")
        self.assertEqual(out["status"], "running")
        warning_names = {item["name"] for item in out["warnings"]}
        self.assertIn("FinishInvalidQuarantined", warning_names)
        self.assertFalse((artifacts_dir / "finish.json").exists())
        self.assertTrue(list(artifacts_dir.glob("finish.invalid.*.json")))

    def test_status_quarantines_legacy_v1_finish_and_continues(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_status_legacy_")
        now_ms = self._mod._now_ms()
        job = {
            "runId": "status-legacy",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        legacy_finish = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 1,
            "ok": True,
            "runId": "status-legacy",
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        (artifacts_dir / "finish.json").write_text(
            json.dumps(legacy_finish), encoding="utf-8"
        )
        proc = self._run_cli_command("status", "--artifacts-dir", str(artifacts_dir))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertEqual(out["type"], "opencode-subtask-status")
        self.assertEqual(out["status"], "running")
        warning_names = {item["name"] for item in out["warnings"]}
        self.assertIn("FinishInvalidQuarantined", warning_names)
        self.assertFalse((artifacts_dir / "finish.json").exists())
        self.assertTrue(list(artifacts_dir.glob("finish.invalid.*.json")))

    def test_wait_quarantines_invalid_finish_and_continues(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_wait_invalid_")
        now_ms = self._mod._now_ms()
        job = {
            "runId": "wait-invalid",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        (artifacts_dir / "finish.json").write_text("{}", encoding="utf-8")
        proc = self._run_cli_command(
            "wait",
            "--artifacts-dir",
            str(artifacts_dir),
            "--wait-timeout",
            "0.01",
            "--poll-interval",
            "0.01",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        out = json.loads(proc.stdout)
        self.assertEqual(out["type"], "opencode-subtask-status")
        self.assertTrue(out["waitExpired"])
        warning_names = {item["name"] for item in out["warnings"]}
        self.assertIn("FinishInvalidQuarantined", warning_names)
        self.assertFalse((artifacts_dir / "finish.json").exists())
        self.assertTrue(list(artifacts_dir.glob("finish.invalid.*.json")))

    def test_cancel_quarantines_invalid_finish_and_continues(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_cancel_invalid_")
        now_ms = self._mod._now_ms()
        job = {
            "runId": "cancel-invalid",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": now_ms,
            "updatedAt": now_ms,
            "outputMode": "machine",
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        (artifacts_dir / "finish.json").write_text("{}", encoding="utf-8")
        proc = self._run_cli_command("cancel", "--artifacts-dir", str(artifacts_dir))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        out = json.loads(proc.stdout)
        warning_names = {item["name"] for item in out["warnings"]}
        self.assertIn("FinishInvalidQuarantined", warning_names)
        finish = json.loads((artifacts_dir / "finish.json").read_text(encoding="utf-8"))
        self.assertEqual(finish["outcome"], "internal_error")
        self.assertTrue(list(artifacts_dir.glob("finish.invalid.*.json")))

    def test_status_synthesizes_finish_for_dead_worker_after_grace(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_status_stale_dead_")
        old_ms = self._mod._now_ms() - int((self._mod.DEFAULT_DEAD_WORKER_GRACE_S + 5) * 1000)
        job = {
            "runId": "stale-dead",
            "workdir": str(REPO_ROOT),
            "state": "running",
            "createdAt": old_ms,
            "updatedAt": old_ms,
            "pid": 2147483647,
            "outputMode": "machine",
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        buf = io.StringIO()
        args = argparse.Namespace(run_id=None, artifacts_dir=str(artifacts_dir))
        with contextlib.redirect_stdout(buf):
            rc = self._mod.cmd_status(args)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(out["type"], "opencode-subtask-finish")
        self.assertEqual(out["execution"]["error"]["name"], "WorkerNotRunning")
        self.assertTrue((artifacts_dir / "finish.json").exists())

    def test_status_synthesizes_finish_for_stuck_after_cancel(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_status_stale_cancel_")
        stale_age_s = max(
            self._mod.DEFAULT_STALE_IDLE_S, self._mod.DEFAULT_CANCEL_STUCK_GRACE_S
        ) + 5
        old_touch_ms = self._mod._now_ms() - int(stale_age_s * 1000)
        old_cancel_ms = self._mod._now_ms() - int(stale_age_s * 1000)
        job = {
            "runId": "stale-cancel",
            "workdir": str(REPO_ROOT),
            "state": "canceled",
            "createdAt": old_touch_ms,
            "updatedAt": old_touch_ms,
            "cancelAttemptedAt": old_cancel_ms,
            "pid": 99999,
            "outputMode": "machine",
        }
        (artifacts_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        buf = io.StringIO()
        args = argparse.Namespace(run_id=None, artifacts_dir=str(artifacts_dir))
        with (
            unittest.mock.patch.object(self._mod, "_pid_running", return_value=True),
            unittest.mock.patch.object(
                self._mod,
                "_pid_subtask_worker_ownership_status",
                return_value="verified",
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_status(args)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(out["type"], "opencode-subtask-finish")
        self.assertEqual(out["execution"]["error"]["name"], "StuckAfterCancel")

    def test_run_auto_skips_http_when_cli_only_options_requested(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_auto_cli_")
        extra_file = artifacts_dir / "context.txt"
        extra_file.write_text("context", encoding="utf-8")
        args = self._build_run_args(
            artifacts_dir=artifacts_dir,
            run_id="run-auto-cli",
            output_mode="text",
            engine="auto",
            extra_args=["--attach", "http://127.0.0.1:8765", "--file", str(extra_file)],
        )
        outcome = self._mod.RunOutcome(
            ok=True,
            exit_code=0,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id="ses_auto_cli",
            full_text="text response",
            metrics=None,
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
                "_run_http",
                side_effect=AssertionError("auto mode should skip HTTP here"),
            ),
            unittest.mock.patch.object(
                self._mod,
                "_git_status",
                return_value=([], []),
            ),
            unittest.mock.patch.object(
                self._mod,
                "_git_patch",
                return_value=None,
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_run(args)
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(rc, 0)
        self.assertEqual(out["execution"]["engine"]["selected"], "cli")
        warning_names = {item["name"] for item in out["execution"]["warnings"]}
        self.assertIn("HttpEngineSkipped", warning_names)

    def test_run_http_rejects_cli_only_options(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_http_flags_")
        extra_file = artifacts_dir / "context.txt"
        extra_file.write_text("context", encoding="utf-8")
        args = self._build_run_args(
            artifacts_dir=artifacts_dir,
            run_id="run-http-flags",
            output_mode="text",
            engine="http",
            extra_args=["--attach", "http://127.0.0.1:8765", "--file", str(extra_file)],
        )
        buf = io.StringIO()
        with (
            unittest.mock.patch.object(
                self._mod,
                "_apply_execution_profile",
                side_effect=lambda args, prompt, env: {"profile": args.execution_profile},
            ),
            unittest.mock.patch.object(
                self._mod,
                "_git_status",
                return_value=([], []),
            ),
            unittest.mock.patch.object(
                self._mod,
                "_git_patch",
                return_value=None,
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_run(args)
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(rc, 3)
        self.assertEqual(out["outcome"], "failed")
        self.assertEqual(
            out["execution"]["error"]["name"],
            "UnsupportedHttpOptions",
        )

    def test_run_auto_falls_back_from_http_to_cli(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_auto_fallback_")
        args = self._build_run_args(
            artifacts_dir=artifacts_dir,
            run_id="run-auto-fallback",
            output_mode="text",
            engine="auto",
            extra_args=["--attach", "http://127.0.0.1:8765"],
        )
        http_outcome = self._mod.RunOutcome(
            ok=False,
            exit_code=1,
            timed_out=False,
            engine="http",
            fallback_from=None,
            session_id="ses_http",
            full_text="",
            metrics=None,
            error={"name": "HttpError", "message": "server rejected request"},
        )
        cli_outcome = self._mod.RunOutcome(
            ok=True,
            exit_code=0,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id="ses_cli",
            full_text="text response",
            metrics=None,
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
            unittest.mock.patch.object(self._mod, "_run_http", return_value=http_outcome),
            unittest.mock.patch.object(self._mod, "_run_cli", return_value=cli_outcome),
            unittest.mock.patch.object(self._mod, "_git_status", return_value=([], [])),
            unittest.mock.patch.object(self._mod, "_git_patch", return_value=None),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_run(args)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(out["execution"]["engine"]["selected"], "cli")
        self.assertEqual(out["execution"]["engine"]["fallbackFrom"], "http")
        warning_names = {item["name"] for item in out["execution"]["warnings"]}
        self.assertIn("EngineFallback", warning_names)

    def test_run_cli_stderr_only_output_hits_artifact_cap(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_cli_cap_stderr_")
        fake_opencode = self._make_fake_opencode(
            "import sys, time\n"
            "chunk = 'x' * 2048\n"
            "while True:\n"
            "    sys.stderr.write(chunk)\n"
            "    sys.stderr.flush()\n"
            "    time.sleep(0.01)\n"
        )
        prompt_path = artifacts_dir / "prompt.txt"
        prompt_path.write_text("prompt", encoding="utf-8")
        outcome = self._mod._run_cli(
            opencode_bin=str(fake_opencode),
            workdir=artifacts_dir,
            env=os.environ.copy(),
            attach_url=None,
            prompt_path=prompt_path,
            continue_last=False,
            session_id=None,
            title=None,
            agent=None,
            model=None,
            variant=None,
            files=[],
            timeout_s=10.0,
            quiet=True,
            save_events=True,
            save_text=True,
            max_artifact_bytes=4096,
            events_path=artifacts_dir / "events.ndjson",
            stderr_path=artifacts_dir / "stderr.log",
            assistant_path=artifacts_dir / "assistant.txt",
            on_session_id=None,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error["name"], "OutputTooLarge")
        self.assertIn("stderr.log", outcome.error["message"])

    def test_run_cli_event_output_hits_artifact_cap(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_cli_cap_events_")
        fake_opencode = self._make_fake_opencode(
            "import json, sys, time\n"
            "evt = {'type': 'message', 'text': 'y' * 2048}\n"
            "while True:\n"
            "    sys.stdout.write(json.dumps(evt) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.01)\n"
        )
        prompt_path = artifacts_dir / "prompt.txt"
        prompt_path.write_text("prompt", encoding="utf-8")
        outcome = self._mod._run_cli(
            opencode_bin=str(fake_opencode),
            workdir=artifacts_dir,
            env=os.environ.copy(),
            attach_url=None,
            prompt_path=prompt_path,
            continue_last=False,
            session_id=None,
            title=None,
            agent=None,
            model=None,
            variant=None,
            files=[],
            timeout_s=10.0,
            quiet=True,
            save_events=True,
            save_text=True,
            max_artifact_bytes=4096,
            events_path=artifacts_dir / "events.ndjson",
            stderr_path=artifacts_dir / "stderr.log",
            assistant_path=artifacts_dir / "assistant.txt",
            on_session_id=None,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error["name"], "OutputTooLarge")
        self.assertIn("events.ndjson", outcome.error["message"])

    def test_run_http_event_output_hits_artifact_cap(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_http_cap_events_")
        stop_evt = threading.Event()

        class _FakeSseResponse:
            def __init__(self) -> None:
                self._pending: list[bytes] = []

            def readline(self) -> bytes:
                if stop_evt.is_set():
                    return b""
                if not self._pending:
                    evt = {
                        "type": "message",
                        "sessionID": "ses_http",
                        "text": "z" * 2048,
                    }
                    self._pending = [
                        f"data: {json.dumps(evt)}\n".encode("utf-8"),
                        b"\n",
                    ]
                    time.sleep(0.01)
                return self._pending.pop(0)

            def close(self) -> None:
                stop_evt.set()

        class _FakeHttpClient:
            def __init__(self, server_url, auth=None, timeout_s=10.0) -> None:
                self._aborted = stop_evt

            def health(self) -> dict:
                return {"healthy": True}

            def create_session(self) -> dict:
                return {"id": "ses_http"}

            def open_sse(self, path, timeout_s=2.0):
                return _FakeSseResponse()

            def abort(self, session_id: str) -> None:
                self._aborted.set()

            def reply_permission(self, session_id: str, permission_id: str, response: str, remember: bool) -> None:
                return None

            def send_message_sync(self, session_id: str, prompt: str, model, variant, agent, timeout_s: float) -> dict:
                deadline = time.time() + 5.0
                while not self._aborted.is_set() and time.time() < deadline:
                    time.sleep(0.01)
                if self._aborted.is_set():
                    raise RuntimeError("aborted for size")
                return {"parts": [{"type": "text", "text": "done"}]}

        with unittest.mock.patch.object(self._mod, "OpencodeHttpClient", _FakeHttpClient):
            outcome = self._mod._run_http(
                server_url="http://127.0.0.1:8765",
                workdir=artifacts_dir,
                env=os.environ.copy(),
                prompt="Act as a senior software engineer.",
                agent=None,
                model=None,
                variant=None,
                timeout_s=10.0,
                save_events=True,
                save_text=False,
                max_artifact_bytes=1024,
                events_path=artifacts_dir / "events.ndjson",
                stderr_path=artifacts_dir / "stderr.log",
                assistant_path=None,
                permission_mode="inherit",
                on_session_id=None,
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error["name"], "OutputTooLarge")
        self.assertIn("events.ndjson", outcome.error["message"])

    def test_run_http_assistant_output_hits_artifact_cap_after_sync_response(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_http_cap_assistant_")

        class _FakeHttpClient:
            def __init__(self, server_url, auth=None, timeout_s=10.0) -> None:
                return None

            def health(self) -> dict:
                return {"healthy": True}

            def create_session(self) -> dict:
                return {"id": "ses_http_text"}

            def open_sse(self, path, timeout_s=2.0):
                raise RuntimeError("SSE intentionally unavailable for direct-response path")

            def abort(self, session_id: str) -> None:
                return None

            def reply_permission(
                self, session_id: str, permission_id: str, response: str, remember: bool
            ) -> None:
                return None

            def send_message_sync(
                self, session_id: str, prompt: str, model, variant, agent, timeout_s: float
            ) -> dict:
                return {"parts": [{"type": "text", "text": "q" * 2048}]}

        with unittest.mock.patch.object(self._mod, "OpencodeHttpClient", _FakeHttpClient):
            outcome = self._mod._run_http(
                server_url="http://127.0.0.1:8765",
                workdir=artifacts_dir,
                env=os.environ.copy(),
                prompt="Act as a senior software engineer.",
                agent=None,
                model=None,
                variant=None,
                timeout_s=10.0,
                save_events=False,
                save_text=True,
                max_artifact_bytes=1024,
                events_path=None,
                stderr_path=artifacts_dir / "stderr.log",
                assistant_path=artifacts_dir / "assistant.txt",
                permission_mode="inherit",
                on_session_id=None,
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error["name"], "OutputTooLarge")
        self.assertIn("assistant.txt", outcome.error["message"])

    def test_run_http_preexisting_artifact_breach_aborts_created_session(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_http_cap_race_")
        stderr_path = artifacts_dir / "stderr.log"
        stderr_path.write_text("x" * 2048, encoding="utf-8")
        events_path = artifacts_dir / "events.ndjson"
        assistant_path = artifacts_dir / "assistant.txt"
        state: dict[str, object] = {
            "abort_calls": [],
            "send_called": False,
            "open_sse_called": False,
        }

        class _FakeHttpClient:
            def __init__(self, server_url, auth=None, timeout_s=10.0) -> None:
                return None

            def health(self) -> dict:
                return {"healthy": True}

            def create_session(self) -> dict:
                time.sleep(0.25)
                return {"id": "ses_race"}

            def open_sse(self, path, timeout_s=2.0):
                state["open_sse_called"] = True
                raise AssertionError("open_sse should not run after preexisting breach")

            def abort(self, session_id: str) -> None:
                cast(list[str], state["abort_calls"]).append(session_id)

            def reply_permission(
                self, session_id: str, permission_id: str, response: str, remember: bool
            ) -> None:
                return None

            def send_message_sync(
                self, session_id: str, prompt: str, model, variant, agent, timeout_s: float
            ) -> dict:
                state["send_called"] = True
                raise AssertionError(
                    "send_message_sync should not run after preexisting breach"
                )

        with unittest.mock.patch.object(self._mod, "OpencodeHttpClient", _FakeHttpClient):
            outcome = self._mod._run_http(
                server_url="http://127.0.0.1:8765",
                workdir=artifacts_dir,
                env=os.environ.copy(),
                prompt="Act as a senior software engineer.",
                agent=None,
                model=None,
                variant=None,
                timeout_s=10.0,
                save_events=True,
                save_text=True,
                max_artifact_bytes=1024,
                events_path=events_path,
                stderr_path=stderr_path,
                assistant_path=assistant_path,
                permission_mode="inherit",
                on_session_id=None,
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.session_id, "ses_race")
        self.assertEqual(outcome.error["name"], "OutputTooLarge")
        self.assertIn("stderr.log", outcome.error["message"])
        self.assertEqual(state["abort_calls"], ["ses_race"])
        self.assertFalse(cast(bool, state["open_sse_called"]))
        self.assertFalse(cast(bool, state["send_called"]))

    def test_run_reports_finish_write_failed_as_json_error(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_finish_write_failed_")
        args = self._build_run_args(
            artifacts_dir=artifacts_dir,
            run_id="finish-write-failed",
            output_mode="machine",
            nonce="nonce-finish-write",
        )
        outcome = self._mod.RunOutcome(
            ok=True,
            exit_code=0,
            timed_out=False,
            engine="cli",
            fallback_from=None,
            session_id="ses_finish_write",
            full_text=self._build_payload_text(
                nonce="nonce-finish-write",
                decision="GO_NO_DELTA",
            ),
            metrics=None,
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
            unittest.mock.patch.object(self._mod, "_git_status", return_value=([], [])),
            unittest.mock.patch.object(self._mod, "_git_patch", return_value=None),
            unittest.mock.patch.object(
                self._mod,
                "_write_finish_once",
                return_value=(False, "io-error", None),
            ),
            contextlib.redirect_stdout(buf),
        ):
            rc = self._mod.cmd_run(args)
        self.assertEqual(rc, 1)
        out = json.loads(buf.getvalue().strip())
        self.assertEqual(out["type"], "opencode-subtask-error")
        self.assertEqual(out["error"]["name"], "FinishWriteFailed")

    def test_judge_execution_only_accepts_completed(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_exec_")
        finish = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": "judge-exec",
            "workdir": str(REPO_ROOT),
            "outputMode": "machine",
            "outcome": "completed",
            "execution": {
                "exitCode": 0,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": None},
                "sessionId": None,
                "error": None,
                "warnings": [],
            },
            "payload": {
                "status": "missing",
                "schema": None,
                "artifact": {"path": None, "digest": None},
                "errors": [],
            },
            "decision": {"status": "unavailable", "route": None},
            "workspace": {"changedFiles": [], "untrackedFiles": [], "patchPath": None},
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": None,
                "diagnosticsPath": None,
            },
        }
        finish_path = artifacts_dir / "finish.json"
        finish_path.write_text(json.dumps(finish), encoding="utf-8")
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "execution-only",
        )
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["adapterVersion"], self._mod.ADAPTER_VERSION)
        self.assertEqual(out["verdict"], "accept")

    def test_judge_require_determinate_reroutes_mandatory_delta(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_det_")
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": "judge-det",
            "decision": "MANDATORY_DELTA",
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        digest = self._mod._sha256_file(payload_path)
        finish = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": "judge-det",
            "workdir": str(REPO_ROOT),
            "outputMode": "machine",
            "outcome": "completed",
            "execution": {
                "exitCode": 0,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": None},
                "sessionId": None,
                "error": None,
                "warnings": [],
            },
            "payload": {
                "status": "validated",
                "schema": self._mod.PAYLOAD_PROTOCOL,
                "artifact": {"path": "payload.json", "digest": digest},
                "errors": [],
            },
            "decision": {"status": "determinate", "route": "MANDATORY_DELTA"},
            "workspace": {"changedFiles": [], "untrackedFiles": [], "patchPath": None},
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": "payload.json",
                "diagnosticsPath": None,
            },
        }
        finish_path = artifacts_dir / "finish.json"
        finish_path.write_text(json.dumps(finish), encoding="utf-8")
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-determinate",
        )
        self.assertEqual(proc.returncode, 10)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "reroute")
        self.assertEqual(out["route"], "MANDATORY_DELTA")

    def test_judge_require_determinate_accepts_go_no_delta(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_go_")
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": "judge-go",
            "decision": "GO_NO_DELTA",
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            run_id="judge-go",
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest=self._mod._sha256_file(payload_path),
            decision_status="determinate",
            decision_route="GO_NO_DELTA",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-determinate",
        )
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "accept")

    def test_judge_require_go_no_delta_retries_on_digest_mismatch(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_digest_")
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text("{}", encoding="utf-8")
        finish = {
            "type": "opencode-subtask-finish",
            "schemaVersion": 2,
            "adapterVersion": "0.6.0",
            "timestamp": 1,
            "runId": "judge-digest",
            "workdir": str(REPO_ROOT),
            "outputMode": "machine",
            "outcome": "completed",
            "execution": {
                "exitCode": 0,
                "durationMs": 1,
                "engine": {"selected": "cli", "fallbackFrom": None},
                "sessionId": None,
                "error": None,
                "warnings": [],
            },
            "payload": {
                "status": "validated",
                "schema": self._mod.PAYLOAD_PROTOCOL,
                "artifact": {"path": "payload.json", "digest": "deadbeef"},
                "errors": [],
            },
            "decision": {"status": "determinate", "route": "GO_NO_DELTA"},
            "workspace": {"changedFiles": [], "untrackedFiles": [], "patchPath": None},
            "artifacts": {
                "dir": str(artifacts_dir),
                "jobPath": "job.json",
                "finishPath": "finish.json",
                "promptPath": None,
                "stderrPath": None,
                "assistantPath": None,
                "eventsPath": None,
                "wrapperLogPath": None,
                "payloadPath": "payload.json",
                "diagnosticsPath": None,
            },
        }
        finish_path = artifacts_dir / "finish.json"
        finish_path.write_text(json.dumps(finish), encoding="utf-8")
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-go-no-delta",
        )
        self.assertEqual(proc.returncode, 11)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "retry")
        self.assertEqual(out["reasonCode"], "PAYLOAD_DIGEST_MISMATCH")

    def test_judge_require_go_no_delta_accepts_go_no_delta(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_accept_go_")
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": "judge-accept-go",
            "decision": "GO_NO_DELTA",
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            run_id="judge-accept-go",
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest=self._mod._sha256_file(payload_path),
            decision_status="determinate",
            decision_route="GO_NO_DELTA",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-go-no-delta",
        )
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "accept")

    def test_judge_retries_invalid_finish_envelope(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_invalid_")
        finish_path = artifacts_dir / "finish.json"
        finish_path.write_text("{}", encoding="utf-8")
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "execution-only",
        )
        self.assertEqual(proc.returncode, 11)
        out = json.loads(proc.stdout)
        self.assertEqual(out["reasonCode"], "FINISH_INVALID")

    def test_judge_retries_missing_finish_file(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_missing_finish_")
        finish_path = artifacts_dir / "missing-finish.json"
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "execution-only",
        )
        self.assertEqual(proc.returncode, 11)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "retry")
        self.assertEqual(out["reasonCode"], "FINISH_NOT_FOUND")

    def test_judge_execution_only_retries_failed_outcome(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_exec_retry_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            outcome="failed",
            exit_code=3,
            execution_error={"name": "NonZeroExit", "message": "boom"},
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "execution-only",
        )
        self.assertEqual(proc.returncode, 11)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "retry")
        self.assertEqual(out["reasonCode"], "EXECUTION_FAILED")

    def test_judge_require_determinate_blocks_text_output(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_text_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            output_mode="text",
            payload_status="not_requested",
            decision_status="not_requested",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-determinate",
        )
        self.assertEqual(proc.returncode, 12)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "block")
        self.assertEqual(out["reasonCode"], "OUTPUT_NOT_MACHINE")

    def test_judge_require_determinate_reroutes_undetermined(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_abstained_")
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": "judge-undetermined",
            "decision": "UNDETERMINED",
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest=self._mod._sha256_file(payload_path),
            decision_status="abstained",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-determinate",
        )
        self.assertEqual(proc.returncode, 10)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "reroute")
        self.assertEqual(out["reasonCode"], "DECISION_UNDETERMINED")

    def test_judge_require_determinate_retries_missing_payload(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_missing_")
        finish = self._make_finish(artifacts_dir=artifacts_dir, payload_status="missing")
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-determinate",
        )
        self.assertEqual(proc.returncode, 11)
        out = json.loads(proc.stdout)
        self.assertEqual(out["reasonCode"], "PAYLOAD_MISSING")

    def test_judge_require_go_no_delta_blocks_mandatory_delta(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_block_mandatory_")
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": "judge-block",
            "decision": "MANDATORY_DELTA",
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest=self._mod._sha256_file(payload_path),
            decision_status="determinate",
            decision_route="MANDATORY_DELTA",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-go-no-delta",
        )
        self.assertEqual(proc.returncode, 12)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "block")
        self.assertEqual(out["reasonCode"], "DECISION_MANDATORY_DELTA")

    def test_judge_require_go_no_delta_blocks_undetermined(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_block_undetermined_")
        payload = {
            "protocol": self._mod.PAYLOAD_PROTOCOL,
            "nonce": "judge-block-undetermined",
            "decision": "UNDETERMINED",
            "summary": "done",
            "evidence": ["a.py:1 - fact"],
            "changes": ["x"],
            "next_steps": ["y"],
        }
        payload_path = artifacts_dir / "payload.json"
        payload_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            payload_status="validated",
            payload_schema=self._mod.PAYLOAD_PROTOCOL,
            payload_artifact_path="payload.json",
            payload_digest=self._mod._sha256_file(payload_path),
            decision_status="abstained",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-go-no-delta",
        )
        self.assertEqual(proc.returncode, 12)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "block")
        self.assertEqual(out["reasonCode"], "DECISION_UNDETERMINED")

    def test_judge_require_go_no_delta_retries_malformed_payload(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_malformed_")
        finish = self._make_finish(artifacts_dir=artifacts_dir, payload_status="malformed")
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-go-no-delta",
        )
        self.assertEqual(proc.returncode, 11)
        out = json.loads(proc.stdout)
        self.assertEqual(out["reasonCode"], "PAYLOAD_MALFORMED")

    def test_judge_require_go_no_delta_blocks_text_output(self) -> None:
        artifacts_dir = self._mktempdir("ocsubtask_v2_judge_text_block_")
        finish = self._make_finish(
            artifacts_dir=artifacts_dir,
            output_mode="text",
            payload_status="not_requested",
            decision_status="not_requested",
        )
        finish_path = self._write_finish(artifacts_dir, finish)
        proc = self._run_cli_command(
            "judge",
            "--finish",
            str(finish_path),
            "--policy",
            "require-go-no-delta",
        )
        self.assertEqual(proc.returncode, 12)
        out = json.loads(proc.stdout)
        self.assertEqual(out["verdict"], "block")
        self.assertEqual(out["reasonCode"], "OUTPUT_NOT_MACHINE")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
