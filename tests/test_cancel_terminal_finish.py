import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestCancelTerminalFinish(unittest.TestCase):
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
                "serverUrl": "http://127.0.0.1:0",
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
            # cancel exits non-zero when it did not actively signal (kill/abort),
            # but it must still converge by writing a terminal finish.json.
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(
                finish_path.exists(),
                f"finish.json was not written; stdout={proc.stdout!r}",
            )
            fin = json.loads(finish_path.read_text(encoding="utf-8"))
            self.assertEqual(fin.get("type"), "opencode-subtask-finish")
            self.assertIsInstance(fin.get("error"), dict)
            self.assertEqual(fin["error"].get("name"), "CancelAbortFailed")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
