#!/usr/bin/env python3
"""Lightweight smoke tests for opencode-subtask CLI.

Verifies the five key contract invariants via actual subprocess invocations:
  1. stdout is always exactly one JSON line
  2. BadRunId error classification (exit 2) for malicious --run-id
  3. BadArgs error classification (exit 2) for malformed --env
  4. MissingRunId error for status without --run-id
  5. Top-level schema fields (type, schemaVersion, ok, error) are present

Usage:
    python tests/smoke_test.py          # run all checks
    python -m pytest tests/smoke_test.py -v   # via pytest
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = str(REPO_ROOT / "scripts" / "opencode_subtask.py")
TIMEOUT = 15


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the adapter script with the given arguments."""
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=TIMEOUT,
    )


class SmokeTests(unittest.TestCase):
    """Pin down stdout-single-line contract + exit code classification."""

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
        self.assertIn("ok", obj)
        self.assertFalse(obj["ok"])
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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
