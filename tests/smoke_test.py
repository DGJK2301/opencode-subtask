#!/usr/bin/env python3
"""Lightweight smoke tests for opencode-subtask CLI.

Verifies the key contract invariants via captured CLI entrypoint invocations:
  1. lifecycle/control-plane stdout is exactly one JSON line
  2. BadRunId error classification (exit 2) for malicious --run-id
  3. BadArgs error classification (exit 2) for malformed --env
  4. MissingRunId error for status without --run-id
  5. Top-level schema fields (type, schemaVersion, ok, error) are present
  6. ask stdout is final assistant text, not the adapter JSON envelope
  7. run finish schema is strict v3 execution/task metadata
  8. ask can delegate workspace-writing tasks; it is not a read-only mode

Usage:
    python tests/smoke_test.py          # run all checks
    python -m pytest tests/smoke_test.py -v   # via pytest
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = str(REPO_ROOT / "scripts" / "opencode_subtask.py")
TIMEOUT = 15

_SPEC = importlib.util.spec_from_file_location("opencode_subtask_under_test", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ocsubtask = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ocsubtask
_SPEC.loader.exec_module(ocsubtask)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the adapter CLI entrypoint with captured stdout/stderr.

    This keeps smoke tests fast and avoids nested Python process trees. Engine
    tests still exercise real child-process execution through the fake opencode
    executable that cmd_run/cmd_ask spawn.
    """
    old_cwd = Path.cwd()
    out = io.StringIO()
    err = io.StringIO()
    rc = 0
    try:
        os.chdir(REPO_ROOT)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = int(ocsubtask.main(list(args)))
            except SystemExit as exc:
                try:
                    rc = int(exc.code) if exc.code is not None else 0
                except Exception:
                    rc = 1
    finally:
        os.chdir(old_cwd)
    return subprocess.CompletedProcess(
        [sys.executable, SCRIPT, *args],
        rc,
        out.getvalue(),
        err.getvalue(),
    )


def _make_fake_opencode(tmp_path: Path, text: str = "hello from opencode") -> Path:
    """Create a fake opencode executable that emits one JSON text event."""
    impl = tmp_path / "fake_opencode_impl.py"
    impl.write_text(
        "import json\n"
        f"print(json.dumps({{'type': 'message', 'sessionID': 'ses_ask', 'text': {text!r}}}))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake = tmp_path / "fake-opencode.cmd"
        fake.write_text(
            '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, impl),
            encoding="utf-8",
        )
    else:
        fake = tmp_path / "fake-opencode"
        fake.write_text(
            '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, impl),
            encoding="utf-8",
        )
        fake.chmod(0o755)
    return fake


def _make_fake_opencode_events(tmp_path: Path, events: list[dict]) -> Path:
    """Create a fake opencode executable that emits arbitrary JSON events."""
    impl = tmp_path / "fake_opencode_events_impl.py"
    impl.write_text(
        "import json\n"
        f"events = {events!r}\n"
        "for evt in events:\n"
        "    print(json.dumps(evt, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake = tmp_path / "fake-opencode-events.cmd"
        fake.write_text(
            '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, impl),
            encoding="utf-8",
        )
    else:
        fake = tmp_path / "fake-opencode-events"
        fake.write_text(
            '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, impl),
            encoding="utf-8",
        )
        fake.chmod(0o755)
    return fake


def _make_fake_opencode_writer(tmp_path: Path) -> Path:
    """Create a fake opencode executable that writes in cwd and emits final text."""
    impl = tmp_path / "fake_opencode_writer_impl.py"
    impl.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "Path('child_wrote.txt').write_text('written by child\\n', encoding='utf-8')\n"
        "print(json.dumps({'type': 'message', 'sessionID': 'ses_write', 'text': 'wrote file'}))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake = tmp_path / "fake-opencode-writer.cmd"
        fake.write_text(
            '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, impl),
            encoding="utf-8",
        )
    else:
        fake = tmp_path / "fake-opencode-writer"
        fake.write_text(
            '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, impl),
            encoding="utf-8",
        )
        fake.chmod(0o755)
    return fake


def _make_fake_opencode_recorder(tmp_path: Path, text: str = "captured") -> Path:
    """Create a fake opencode executable that records argv/stdin and emits final text."""
    impl = tmp_path / "fake_opencode_recorder_impl.py"
    impl.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "stdin_text = sys.stdin.read()\n"
        "Path('stdin_capture.txt').write_text(stdin_text, encoding='utf-8')\n"
        "Path('argv_capture.json').write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding='utf-8')\n"
        f"print(json.dumps({{'type': 'message', 'sessionID': 'ses_capture', 'text': {text!r}}}))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        fake = tmp_path / "fake-opencode-recorder.cmd"
        fake.write_text(
            '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, impl),
            encoding="utf-8",
        )
    else:
        fake = tmp_path / "fake-opencode-recorder"
        fake.write_text(
            '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, impl),
            encoding="utf-8",
        )
        fake.chmod(0o755)
    return fake


class SmokeTests(unittest.TestCase):
    """Pin down lifecycle JSON and ask text-output contracts."""

    # ---- helpers ----

    def _assert_single_json_line(self, proc: subprocess.CompletedProcess[str]) -> dict:
        """Assert stdout is exactly one non-empty JSON line, return parsed."""
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(
            len(lines), 1, f"expected 1 JSON line, got {len(lines)}: {proc.stdout!r}"
        )
        obj = json.loads(lines[0])
        self.assertIsInstance(obj, dict)
        return obj

    def _assert_error_schema(self, obj: dict) -> None:
        """Assert top-level error schema fields are present."""
        self.assertIn("type", obj)
        self.assertIn("schemaVersion", obj)
        self.assertIn("adapterVersion", obj)
        self.assertIsInstance(obj["adapterVersion"], str)
        self.assertIn("timestamp", obj)
        self.assertIsInstance(obj["timestamp"], int)
        self.assertIn("ok", obj)
        self.assertFalse(obj["ok"])
        self.assertIn("warnings", obj)
        self.assertIsInstance(obj["warnings"], list)
        self.assertIn("error", obj)
        self.assertIsInstance(obj["error"], dict)
        self.assertIn("name", obj["error"])
        self.assertIn("message", obj["error"])

    # ---- smoke checks ----

    def test_1_bad_run_id_exit2(self) -> None:
        """Malicious --run-id → BadRunId, exit 2, single JSON line."""
        proc = _run("status", "--run-id", "../escape")
        self.assertEqual(proc.returncode, 2)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "BadRunId")

    def test_2_bad_args_malformed_env_exit2(self) -> None:
        """Malformed --env (no '=') → BadArgs, exit 2, single JSON line."""
        proc = _run("run", "--env", "NO_EQUALS", "--", "echo", "hi")
        self.assertEqual(proc.returncode, 2)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "BadArgs")

    def test_3_bad_args_missing_env_file_exit2(self) -> None:
        """Nonexistent --env-file → BadArgs, exit 2, single JSON line."""
        proc = _run("run", "--env-file", "VAR=/no/such/file.txt", "--", "echo", "hi")
        self.assertEqual(proc.returncode, 2)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "BadArgs")

    def test_4_missing_run_id_error(self) -> None:
        """status without --run-id → MissingRunId, single JSON line."""
        proc = _run("status")
        self.assertNotEqual(proc.returncode, 0)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "MissingRunId")

    def test_5_bad_run_id_semicolon_exit2(self) -> None:
        """Injection attempt in --run-id → BadRunId, exit 2."""
        proc = _run("wait", "--run-id", "test;rm -rf /")
        self.assertEqual(proc.returncode, 2)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "BadRunId")

    def test_6_prompt_conflict_exit2(self) -> None:
        """Multiple prompt sources → PromptConflict, exit 2."""
        proc = _run(
            "run", "--prompt-file", "nonexistent.txt", "--", "extra", "positional"
        )
        self.assertEqual(proc.returncode, 2)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "PromptConflict")

    def test_7_persona_missing_exit2(self) -> None:
        """--persona-mode require without persona line → PersonaMissing, exit 2."""
        proc = _run(
            "run", "--persona-mode", "require", "--", "Do something without persona"
        )
        self.assertEqual(proc.returncode, 2)
        obj = self._assert_single_json_line(proc)
        self._assert_error_schema(obj)
        self.assertEqual(obj["error"]["name"], "PersonaMissing")

    def test_8_ask_stdout_is_final_text_not_json(self) -> None:
        """ask -> final assistant text on stdout, not the adapter JSON envelope."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_ask_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode(tmp_path)

            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--run-timeout",
                "5",
                "--",
                "plain request",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "hello from opencode")
            with self.assertRaises(json.JSONDecodeError):
                json.loads(proc.stdout)

    def test_9_run_finish_schema_is_strict_v3(self) -> None:
        """run finish.json is execution facts and task metadata only."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_run_") as tmp:
            tmp_path = Path(tmp)
            artifacts_dir = tmp_path / "artifacts"
            fake = _make_fake_opencode(tmp_path)

            proc = _run(
                "run",
                "--workdir",
                str(tmp_path),
                "--artifacts-dir",
                str(artifacts_dir),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--run-timeout",
                "5",
                "--",
                "Act as a senior software engineer. Plain request",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            obj = self._assert_single_json_line(proc)
            self.assertEqual(obj["type"], "opencode-subtask-finish")
            self.assertEqual(obj["schemaVersion"], 3)
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

            finish_obj = json.loads((artifacts_dir / "finish.json").read_text(encoding="utf-8"))
            self.assertEqual(set(finish_obj), set(obj))

    def test_10_run_help_surfaces_task_runtime_flags(self) -> None:
        """The public CLI is task-runtime oriented."""
        proc = _run("run", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--task-id", proc.stdout)
        self.assertIn("--subagent-type", proc.stdout)
        self.assertIn("--acceptance", proc.stdout)

    def test_11_ask_accumulates_latest_opencode_part_events(self) -> None:
        """Current OpenCode message.part.updated events become clean final text."""
        events = [
            {
                "type": "message.updated",
                "properties": {"info": {"id": "u1", "role": "user", "sessionID": "ses_evt"}},
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {"id": "up1", "messageID": "u1", "type": "text", "text": "USER ECHO"}
                },
            },
            {
                "type": "message.updated",
                "properties": {"info": {"id": "a1", "role": "assistant", "sessionID": "ses_evt"}},
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {"id": "r1", "messageID": "a1", "type": "reasoning", "text": "hidden"}
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {"id": "p1", "messageID": "a1", "type": "text", "text": "hel"}
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {"id": "p1", "messageID": "a1", "type": "text", "text": "hello"}
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {"id": "p1", "messageID": "a1", "type": "text"},
                    "delta": " world",
                },
            },
        ]
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_events_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode_events(tmp_path, events)
            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--run-timeout",
                "5",
                "--",
                "plain request",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "hello world")

    def test_12_http_permission_reply_uses_latest_endpoint(self) -> None:
        """HTTP auto permission replies target /permission/:requestID/reply."""

        class CapturingClient(ocsubtask.OpencodeHttpClient):
            def __init__(self) -> None:
                super().__init__("http://127.0.0.1:4096")
                self.calls = []

            def _request_json(self, method, path, body=None, timeout_s=None):  # type: ignore[override]
                self.calls.append((method, path, body, timeout_s))
                return 200, {"ok": True}

        client = CapturingClient()
        client.reply_permission("req/with/slash", reply="once")
        self.assertEqual(client.calls, [("POST", "/permission/req%2Fwith%2Fslash/reply", {"reply": "once"}, 5.0)])
        with self.assertRaises(ValueError):
            client.reply_permission("req", reply="allow")

    def test_13_ask_can_delegate_workspace_writes(self) -> None:
        """ask is the normal sub-agent execution path, not a read-only mode."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_write_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode_writer(tmp_path)
            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--run-timeout",
                "5",
                "--",
                "Implement the requested workspace change.",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "wrote file")
            self.assertEqual(
                (tmp_path / "child_wrote.txt").read_text(encoding="utf-8"),
                "written by child\n",
            )

    def test_14_ask_defaults_to_stdin_prompt_and_subagent_briefing(self) -> None:
        """ask should behave like a child-agent call, not a prompt-file attachment protocol."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_stdin_") as tmp:
            tmp_path = Path(tmp)
            artifacts_dir = tmp_path / "artifacts"
            fake = _make_fake_opencode_recorder(tmp_path)
            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--artifacts-dir",
                str(artifacts_dir),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--run-timeout",
                "5",
                "--",
                "Investigate the failing test and patch the workspace.",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "captured")

            argv = json.loads((tmp_path / "argv_capture.json").read_text(encoding="utf-8"))
            stdin_text = (tmp_path / "stdin_capture.txt").read_text(encoding="utf-8")
            prompt_artifact = (artifacts_dir / "prompt.txt").read_text(encoding="utf-8")

            self.assertEqual(argv[:3], ["run", "--format", "json"])
            self.assertNotIn("--file", argv)
            self.assertIn("Investigate the failing test", stdin_text)
            self.assertIn("Parent-agent boundary:", stdin_text)
            self.assertIn("Keep the final answer", stdin_text)
            self.assertEqual(stdin_text.rstrip(), prompt_artifact.rstrip())

    def test_15_ask_stdout_cap_keeps_full_assistant_artifact(self) -> None:
        """ask uses a token-saving stdout cap while preserving assistant.txt."""
        long_text = "A" * 300
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_cap_") as tmp:
            assistant_path = Path(tmp) / "assistant.txt"
            assistant_path.write_text(long_text, encoding="utf-8")
            out, truncated = ocsubtask._truncate_for_ask_stdout(
                long_text,
                max_chars=120,
                assistant_path=assistant_path,
            )
            self.assertTrue(truncated)
            self.assertIn("final answer truncated", out)
            self.assertIn(str(assistant_path), out)
            self.assertEqual(assistant_path.read_text(encoding="utf-8"), long_text)

    def test_16_http_created_session_uses_noninteractive_rules(self) -> None:
        """HTTP-created sessions deny interactive question/plan tools by default."""
        rules = ocsubtask._noninteractive_session_permission_rules(True)
        self.assertEqual(
            rules,
            [
                {"permission": "question", "action": "deny", "pattern": "*"},
                {"permission": "plan_enter", "action": "deny", "pattern": "*"},
                {"permission": "plan_exit", "action": "deny", "pattern": "*"},
            ],
        )
        self.assertEqual(ocsubtask._noninteractive_session_permission_rules(False), [])

    def test_17_http_model_shapes_match_current_server_boundaries(self) -> None:
        """Session create and message send use the server's distinct model field names."""
        self.assertEqual(
            ocsubtask._model_arg_to_http_session_model("anthropic/claude-sonnet-4"),
            {"providerID": "anthropic", "id": "claude-sonnet-4"},
        )
        self.assertEqual(
            ocsubtask._model_arg_to_http_session_model(
                "anthropic/claude-sonnet-4", "thinking"
            ),
            {"providerID": "anthropic", "id": "claude-sonnet-4", "variant": "thinking"},
        )
        self.assertEqual(
            ocsubtask._model_arg_to_http_message_model("anthropic/claude-sonnet-4"),
            {"providerID": "anthropic", "modelID": "claude-sonnet-4"},
        )

        class CapturingClient(ocsubtask.OpencodeHttpClient):
            def __init__(self) -> None:
                super().__init__("http://127.0.0.1:4096")
                self.calls = []

            def _request_json(self, method, path, body=None, timeout_s=None):  # type: ignore[override]
                self.calls.append((method, path, body, timeout_s))
                return 200, {"info": {"id": "msg_1"}, "parts": []}

        client = CapturingClient()
        client.send_message_sync(
            "ses/with/slash",
            prompt="hi",
            model="anthropic/claude-sonnet-4",
            variant="thinking",
            agent="build",
            timeout_s=12,
        )
        self.assertEqual(
            client.calls,
            [
                (
                    "POST",
                    "/session/ses%2Fwith%2Fslash/message",
                    {
                        "parts": [{"type": "text", "text": "hi"}],
                        "model": {
                            "providerID": "anthropic",
                            "modelID": "claude-sonnet-4",
                        },
                        "variant": "thinking",
                        "agent": "build",
                    },
                    12,
                )
            ],
        )


    def test_18_subtask_type_presets_mirror_child_agent_roles(self) -> None:
        """Adapter-level subtask roles select agent/profile and permission envelope."""
        ns = argparse.Namespace(
            subtask_type="explore",
            agent=None,
            execution_profile="hybrid",
            description="Inspect parser behavior",
            title=None,
            allow_nested_subtasks=False,
            allow_child_todos=False,
        )
        info = ocsubtask._apply_subtask_preset(ns)
        self.assertEqual(info["type"], "explore")
        self.assertEqual(ns.agent, "plan")
        self.assertEqual(ns.execution_profile, "latency")
        self.assertIn("Inspect parser behavior", ns.title)
        self.assertTrue(info["readOnly"])
        self.assertIn({"permission": "todowrite", "action": "deny", "pattern": "*"}, info["permissionRules"])
        self.assertIn({"permission": "task", "action": "deny", "pattern": "*"}, info["permissionRules"])
        self.assertIn({"permission": "edit", "action": "deny", "pattern": "*"}, info["permissionRules"])
        self.assertIn({"permission": "bash", "action": "deny", "pattern": "*"}, info["permissionRules"])

        worker_ns = argparse.Namespace(
            subtask_type="worker",
            agent=None,
            execution_profile="hybrid",
            description="Fix parser behavior",
            title=None,
            allow_nested_subtasks=False,
            allow_child_todos=False,
        )
        worker_info = ocsubtask._apply_subtask_preset(worker_ns)
        self.assertEqual(worker_info["type"], "worker")
        self.assertEqual(worker_ns.agent, "build")
        self.assertEqual(worker_ns.execution_profile, "checkpoint")
        self.assertFalse(worker_info["readOnly"])
        self.assertEqual(worker_info["permissionRules"], [])

    def test_19_ask_metadata_to_stderr_keeps_stdout_clean(self) -> None:
        """ask metadata is opt-in stderr only; stdout stays final assistant text."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_meta_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode(tmp_path, text="done")
            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--run-timeout",
                "5",
                "--description",
                "Metadata check",
                "--ask-metadata-to-stderr",
                "--",
                "plain request",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "done")
            meta_lines = [
                ln for ln in proc.stderr.splitlines() if ln.startswith("OPENCODE_SUBTASK_META ")
            ]
            self.assertEqual(len(meta_lines), 1, proc.stderr)
            meta = json.loads(meta_lines[0].split(" ", 1)[1])
            self.assertEqual(meta["type"], "opencode-subtask-ask-metadata")
            self.assertTrue(meta["ok"])
            self.assertTrue(str(meta["taskId"]).startswith("task_"))
            self.assertEqual(meta["sessionId"], "ses_ask")
            self.assertEqual(meta["subtask"]["description"], "Metadata check")
            self.assertTrue(meta["artifacts"]["assistantPath"].endswith("assistant.txt"))
            self.assertTrue(meta["artifacts"]["taskStatePath"].endswith(".json"))
            self.assertTrue(meta["artifacts"]["taskProgressPath"].endswith("progress.md"))

    def test_20_status_progress_summary_classifies_testing(self) -> None:
        """status progress includes a lightweight semantic phase summary."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_progress_") as tmp:
            artifacts = Path(tmp)
            (artifacts / "events.ndjson").write_text(
                json.dumps(
                    {
                        "type": "message.part.updated",
                        "properties": {
                            "part": {
                                "id": "tool1",
                                "messageID": "a1",
                                "type": "tool",
                                "tool": "bash",
                                "state": {"status": "running", "command": "pytest tests"},
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            progress = ocsubtask._progress_snapshot(artifacts)
            self.assertEqual(progress["summary"]["phase"], "testing")
            self.assertEqual(progress["summary"]["lastTool"], "bash")
            self.assertEqual(progress["summary"]["lastToolStatus"], "running")

    def test_21_task_id_is_not_raw_session_alias(self) -> None:
        """--task-id is the adapter handle; raw OpenCode sessions use --session."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_taskid_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode_recorder(tmp_path)
            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--task-id",
                "ses_resume",
                "--run-timeout",
                "5",
                "--",
                "plain request",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            argv = json.loads((tmp_path / "argv_capture.json").read_text(encoding="utf-8"))
            self.assertNotIn("--session", argv)

            explicit = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--session",
                "ses_resume",
                "--run-timeout",
                "5",
                "--",
                "plain request",
            )
            self.assertEqual(explicit.returncode, 0, explicit.stderr + explicit.stdout)
            argv = json.loads((tmp_path / "argv_capture.json").read_text(encoding="utf-8"))
            self.assertIn("--session", argv)
            self.assertEqual(argv[argv.index("--session") + 1], "ses_resume")

    def test_22_subtask_permission_rules_override_allow_mode(self) -> None:
        """Only read-only child-role deny rules override permissive base mode."""
        full_rules = ocsubtask._subtask_default_permission_rules("worker")
        self.assertEqual(full_rules, [])
        full_env = {"OPENCODE_PERMISSION": json.dumps({"*": "allow"})}
        ocsubtask._apply_subtask_permission_rules_to_env(full_env, full_rules)
        self.assertEqual(json.loads(full_env["OPENCODE_PERMISSION"]), {"*": "allow"})
        self.assertFalse(
            ocsubtask._effective_http_deny_interactive_tools(
                argparse.Namespace(http_deny_interactive_tools=None),
                {"readOnly": False},
            )
        )
        self.assertTrue(
            ocsubtask._effective_http_deny_interactive_tools(
                argparse.Namespace(http_deny_interactive_tools=None),
                {"readOnly": True},
            )
        )
        self.assertTrue(
            ocsubtask._effective_http_deny_interactive_tools(
                argparse.Namespace(http_deny_interactive_tools=True),
                {"readOnly": False},
            )
        )

        env = {"OPENCODE_PERMISSION": json.dumps({"*": "allow"})}
        rules = ocsubtask._subtask_default_permission_rules("thinker")

        ocsubtask._apply_subtask_permission_rules_to_env(env, rules)

        cfg = json.loads(env["OPENCODE_PERMISSION"])
        self.assertEqual(cfg["*"], "allow")
        self.assertEqual(cfg["todowrite"], "deny")
        self.assertEqual(cfg["task"], "deny")
        self.assertEqual(cfg["edit"], "deny")
        self.assertEqual(cfg["bash"], "deny")

        opt_out_rules = ocsubtask._subtask_default_permission_rules(
            "explore",
            allow_nested_subtasks=True,
            allow_child_todos=True,
        )
        self.assertNotIn(
            {"permission": "todowrite", "action": "deny", "pattern": "*"},
            opt_out_rules,
        )
        self.assertNotIn(
            {"permission": "task", "action": "deny", "pattern": "*"},
            opt_out_rules,
        )
        self.assertIn(
            {"permission": "edit", "action": "deny", "pattern": "*"},
            opt_out_rules,
        )

    def test_23_ask_metadata_uses_workspace_patch_path(self) -> None:
        """ask metadata exposes changes.patch from finish.workspace.patchPath."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_meta_patch_") as tmp:
            tmp_path = Path(tmp)
            finish = {
                "outcome": "completed",
                "runId": "run_meta_patch",
                "execution": {
                    "sessionId": "ses_meta_patch",
                    "engine": {"selected": "cli", "fallbackFrom": None},
                },
                "workspace": {
                    "changedFiles": ["example.py"],
                    "patchPath": "changes.patch",
                },
                "artifacts": {
                    "dir": str(tmp_path),
                    "finishPath": "finish.json",
                    "assistantPath": "assistant.txt",
                },
                "subtask": {"type": "worker"},
            }

            meta = ocsubtask._ask_metadata_obj(finish)

            self.assertEqual(meta["taskId"], "ses_meta_patch")
            self.assertEqual(meta["artifacts"]["patchPath"], str(tmp_path / "changes.patch"))


    def test_24_explicit_task_id_status_and_list_tasks(self) -> None:
        """Adapter task ids are stable caller-facing handles across ask/status/list-tasks."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_taskhandle_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode(tmp_path, text="handled")
            task_id = "task_smoke_handle"
            proc = _run(
                "ask",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--task-id",
                task_id,
                "--ask-metadata-to-stderr",
                "--run-timeout",
                "5",
                "--",
                "do it",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "handled")
            meta_line = [ln for ln in proc.stderr.splitlines() if ln.startswith("OPENCODE_SUBTASK_META ")][0]
            meta = json.loads(meta_line.split(" ", 1)[1])
            self.assertEqual(meta["taskId"], task_id)
            self.assertTrue(Path(meta["artifacts"]["taskStatePath"]).exists())

            status = _run("status", "--workdir", str(tmp_path), "--task-id", task_id)
            self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
            status_obj = json.loads(status.stdout.strip())
            self.assertEqual(status_obj["type"], "opencode-subtask-finish")
            self.assertEqual(status_obj["taskId"], task_id)

            listed = _run("list-tasks", "--workdir", str(tmp_path))
            self.assertEqual(listed.returncode, 0, listed.stderr + listed.stdout)
            listed_obj = json.loads(listed.stdout.strip())
            self.assertIn(task_id, [t["taskId"] for t in listed_obj["tasks"]])

    def test_25_task_memory_is_injected_on_task_continuation(self) -> None:
        """Known task ids inject bounded progress memory into the next child prompt."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_memory_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode_recorder(tmp_path, text="remembered")
            task_id = "task_memory_handle"
            first = _run(
                "ask", "--workdir", str(tmp_path), "--engine", "cli", "--opencode", str(fake),
                "--task-id", task_id, "--run-timeout", "5", "--", "first pass"
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            second = _run(
                "ask", "--workdir", str(tmp_path), "--engine", "cli", "--opencode", str(fake),
                "--task-id", task_id, "--run-timeout", "5", "--", "second pass"
            )
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            stdin_text = (tmp_path / "stdin_capture.txt").read_text(encoding="utf-8")
            self.assertIn("External task continuation memory", stdin_text)
            self.assertIn(task_id, stdin_text)
            argv = json.loads((tmp_path / "argv_capture.json").read_text(encoding="utf-8"))
            self.assertIn("--session", argv)

    def test_26_running_task_id_duplicate_guard(self) -> None:
        """A live adapter task id refuses accidental duplicate starts by default."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_running_") as tmp:
            tmp_path = Path(tmp)
            task_id = "task_running_guard"
            artifacts = tmp_path / "artifacts"
            artifacts.mkdir()
            (artifacts / "job.json").write_text(json.dumps({
                "runId": "run_live",
                "taskId": task_id,
                "state": "running",
                "pid": os.getpid(),
            }), encoding="utf-8")
            ocsubtask._write_task_state_locked(
                workdir=tmp_path,
                task_id=task_id,
                update={
                    "state": "running",
                    "latestRunId": "run_live",
                    "latestArtifactsDir": str(artifacts),
                },
            )
            fake = _make_fake_opencode(tmp_path, text="should not run")
            proc = _run(
                "ask", "--workdir", str(tmp_path), "--engine", "cli", "--opencode", str(fake),
                "--task-id", task_id, "--", "duplicate"
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertIn("TaskAlreadyRunning", proc.stderr)
            self.assertIn(task_id, proc.stderr)

    def test_27_watch_emits_progress_notifications_to_stderr(self) -> None:
        """watch gives native-task-like progress notifications without bloating stdout."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_watch_") as tmp:
            tmp_path = Path(tmp)
            task_id = "task_watch_progress"
            run_id = "run_watch_progress"
            artifacts = tmp_path / "artifacts"
            artifacts.mkdir()
            now = ocsubtask._now_ms()
            (artifacts / "job.json").write_text(json.dumps({
                "runId": run_id,
                "taskId": task_id,
                "state": "running",
                "pid": 0,
                "createdAt": now,
                "updatedAt": now,
            }), encoding="utf-8")
            (artifacts / "events.ndjson").write_text(json.dumps({
                "type": "message.part.updated",
                "properties": {"part": {"type": "tool", "tool": "bash", "state": {"command": "pytest"}}},
            }) + "\n", encoding="utf-8")
            ocsubtask._write_task_state_locked(
                workdir=tmp_path,
                task_id=task_id,
                update={"state": "running", "latestRunId": run_id, "latestArtifactsDir": str(artifacts)},
            )
            proc = _run(
                "watch", "--workdir", str(tmp_path), "--task-id", task_id,
                "--wait-timeout", "0.05", "--poll-interval", "0.01", "--progress-interval", "0.01"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("OPENCODE_SUBTASK_PROGRESS ", proc.stderr)
            obj = json.loads(proc.stdout.strip())
            self.assertTrue(obj["waitExpired"])
            self.assertEqual(obj["taskId"], task_id)

    def test_28_task_command_is_native_like_foreground_entrypoint(self) -> None:
        """task foreground prints text and emits metadata by default."""
        with tempfile.TemporaryDirectory(prefix="ocsubtask_smoke_task_cmd_") as tmp:
            tmp_path = Path(tmp)
            fake = _make_fake_opencode(tmp_path, text="task done")
            proc = _run(
                "task",
                "--workdir",
                str(tmp_path),
                "--engine",
                "cli",
                "--opencode",
                str(fake),
                "--task-id",
                "task_native_like",
                "--subagent-type",
                "worker",
                "--description",
                "Native-like foreground task",
                "--run-timeout",
                "5",
                "--",
                "Implement a small delegated task.",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout, "task done")
            meta_lines = [
                ln for ln in proc.stderr.splitlines() if ln.startswith("OPENCODE_SUBTASK_META ")
            ]
            self.assertEqual(len(meta_lines), 1, proc.stderr)
            meta = json.loads(meta_lines[0].split(" ", 1)[1])
            self.assertEqual(meta["taskId"], "task_native_like")
            self.assertEqual(meta["subtask"]["type"], "worker")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
