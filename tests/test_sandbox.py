"""Future-commit removal, against a local git repo. No Modal and no model."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from memverify.agent import write_trace
from memverify.sandbox import SandboxLost, sandbox_create_kwargs, strip_future_history, wait_for_process


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=memverify@example.com", "-c", "user.name=memverify", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _runner(repo: Path):
    def run(command: str, timeout: int = 60) -> dict:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "output": (proc.stdout or "") + (proc.stderr or ""),
            "returncode": proc.returncode,
            "exception_info": "",
        }

    return run


class StripFutureHistoryTest(unittest.TestCase):
    def test_later_commit_becomes_unreachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-b", "main")
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "note.txt")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "note.txt").write_text("future fix\n", encoding="utf-8")
            _git(repo, "add", "note.txt")
            _git(repo, "commit", "-m", "future")
            future = _git(repo, "rev-parse", "HEAD")
            _git(repo, "tag", "later", future)
            subprocess.run(["git", "remote", "add", "origin", "https://example.com/repo.git"], cwd=repo, check=True)

            report = strip_future_history(_runner(repo), base, [future], str(repo))

            self.assertTrue(report["prep_ok"], report["prep_output"])
            self.assertTrue(report["future_commits_absent"])
            self.assertEqual(_git(repo, "rev-parse", "HEAD"), base)
            self.assertEqual((repo / "note.txt").read_text(encoding="utf-8"), "base\n")
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{future}^{{commit}}"],
                cwd=repo,
                capture_output=True,
            )
            self.assertNotEqual(probe.returncode, 0)

    def test_no_forbidden_commit_is_reported_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-b", "main")
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "note.txt")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            report = strip_future_history(_runner(repo), base, [], str(repo))
            self.assertTrue(report["prep_ok"], report["prep_output"])
            self.assertIsNone(report["future_commits_absent"])

    def test_ancestor_and_current_commit_are_not_a_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-b", "main")
            (repo / "note.txt").write_text("parent\n", encoding="utf-8")
            _git(repo, "add", "note.txt")
            _git(repo, "commit", "-m", "parent")
            parent = _git(repo, "rev-parse", "HEAD")
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "note.txt")
            _git(repo, "commit", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "note.txt").write_text("future fix\n", encoding="utf-8")
            _git(repo, "add", "note.txt")
            _git(repo, "commit", "-m", "future")
            future = _git(repo, "rev-parse", "HEAD")

            report = strip_future_history(_runner(repo), base, [parent, base, future], str(repo))

            self.assertTrue(report["prep_ok"], report["prep_output"])
            self.assertTrue(report["future_commits_absent"])
            self.assertTrue(report["probes"][future])
            self.assertFalse(report["probes"][parent])
            self.assertFalse(report["probes"][base])
            self.assertEqual(set(report["kept_ancestors"]), {parent, base})


class AgentSandboxPolicyTest(unittest.TestCase):
    def test_agent_sandbox_blocks_outbound_network(self):
        kwargs = sandbox_create_kwargs(image=None, app=None, timeout=10, cpu=1, memory_mb=512)
        self.assertTrue(kwargs["block_network"])

    def test_trace_is_written_only_when_the_directory_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            os_environ = __import__("os").environ
            previous = os_environ.pop("MEMVERIFY_TRACE_DIR", None)
            try:
                write_trace("task-a", {"task_id": "task-a", "messages": []})
                self.assertEqual(list(Path(tmp).iterdir()), [])
                os_environ["MEMVERIFY_TRACE_DIR"] = tmp
                write_trace("task-a", {"task_id": "task-a", "messages": [{"role": "user"}]})
            finally:
                if previous is None:
                    os_environ.pop("MEMVERIFY_TRACE_DIR", None)
                else:
                    os_environ["MEMVERIFY_TRACE_DIR"] = previous
            saved = json.loads((Path(tmp) / "task-a.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["task_id"], "task-a")


class _Proc:
    def __init__(self, release: threading.Event | None = None) -> None:
        self.release = release

    def wait(self) -> int:
        if self.release is not None:
            self.release.wait()
        return 0


class WaitForProcessTest(unittest.TestCase):
    def test_finished_command_returns(self) -> None:
        wait_for_process(_Proc(), lambda: None, limit=5, poll_every=0.01)

    def test_dead_sandbox_raises_without_waiting_out_the_timeout(self) -> None:
        started = time.monotonic()
        with self.assertRaises(SandboxLost):
            wait_for_process(_Proc(threading.Event()), lambda: 137, limit=30, poll_every=0.01)
        self.assertLess(time.monotonic() - started, 5)

    def test_poll_error_does_not_hide_a_finished_command(self) -> None:
        calls = {"n": 0}

        def poll() -> int | None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporary")
            return None

        release = threading.Event()

        def finish() -> None:
            time.sleep(0.05)
            release.set()

        threading.Thread(target=finish, daemon=True).start()
        wait_for_process(_Proc(release), poll, limit=5, poll_every=0.01)

    def test_blocking_poll_still_hits_the_deadline(self) -> None:
        def poll() -> int | None:
            time.sleep(30)
            return None

        started = time.monotonic()
        with self.assertRaises(SandboxLost):
            wait_for_process(
                _Proc(threading.Event()),
                poll,
                limit=0,
                poll_every=0.05,
                grace=0.2,
            )
        self.assertLess(time.monotonic() - started, 2)

    def test_sandbox_lost_from_poll_is_not_swallowed(self) -> None:
        def poll() -> int | None:
            raise SandboxLost("sandbox exited (1) while a command was running: spend limit")

        with self.assertRaises(SandboxLost) as caught:
            wait_for_process(_Proc(threading.Event()), poll, limit=30, poll_every=0.01)
        self.assertIn("spend limit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
