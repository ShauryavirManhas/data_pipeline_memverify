"""Runner resume, spend cap, and dry-run. No Modal and no model."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from memverify.config import load_config
from memverify.memory import MemoryStore
from memverify.runner import (
    ResumeError,
    SpendLedger,
    execute_stream,
    main,
    modal_usd,
    split_work,
)
from memverify.types import MemoryEntry, RunRecord, Task

ROOT = Path(__file__).resolve().parents[1]


def _task(task_id: str, created: str, base: str) -> Task:
    return Task(
        id=task_id,
        repo="example/repo",
        issue_text=f"issue {task_id}",
        base_commit=base,
        created_at=created,
        workdir="/testbed",
        docker_image="example/image:latest",
    )


def _record(task_id: str, policy: str = "none", cost: float = 0.0, resolved: bool | None = False) -> RunRecord:
    return RunRecord(
        task_id=task_id,
        policy=policy,
        seed=0,
        patch="diff --git a/a.py b/a.py\n",
        resolved=resolved,
        tokens_in=1,
        tokens_out=1,
        cost_usd=cost,
        cost_agent_usd=cost,
        cost_diagnosis_usd=0.0,
        cost_verifier_usd=0.0,
        hint_id=None,
        hint_injected=False,
        entry_saved=False,
        verifier_decision=None,
        verifier_reason=None,
        wall_seconds=1.0,
        error=None,
    )


class _Score:
    def __init__(self, details: dict | None = None) -> None:
        self.details = details or {}


class PlanTest(unittest.TestCase):
    def test_a_hole_in_the_log_is_an_error(self):
        with self.assertRaises(ResumeError):
            split_work(["a", "b", "c"], {"b": {"record": {"resolved": False}}})

    def test_modal_rate_scales_with_sandbox_size(self):
        full = modal_usd(3600, cpu=4, memory_mb=16384)
        self.assertAlmostEqual(full, 0.95)
        self.assertAlmostEqual(modal_usd(3600, cpu=2, memory_mb=8192), 0.95 / 4)


class ExecuteStreamTest(unittest.TestCase):
    def _tasks(self):
        specs = [
            ("t1", "2024-01-01T00:00:00+00:00", "a" * 40),
            ("t2", "2024-02-01T00:00:00+00:00", "b" * 40),
        ]
        return [(_task(*item), Path(f"{item[0]}.json")) for item in specs]

    def test_spend_cap_stops_before_the_next_task(self):
        tasks = self._tasks()
        seen: list[str] = []

        def run_one_fn(task, path, config, policy, store, seed, forbid):
            seen.append(task.id)
            record = _record(task.id, policy, cost=5.0)
            record.sandbox_prep = {"seconds": 0, "future_commits_absent": True}
            return record

        def score_fn(record, task):
            return record, _Score(), 1

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            status = execute_stream(
                tasks,
                "none",
                folder / "stream.sqlite",
                load_config(ROOT / "configs" / "default.yaml"),
                jsonl_path=folder / "run.jsonl",
                ledger=SpendLedger(folder / "spend.json", cap=4),
                run_one_fn=run_one_fn,
                score_fn=score_fn,
                seed=0,
            )
            logged = [json.loads(line)["record"]["task_id"] for line in (folder / "run.jsonl").read_text().splitlines()]
        self.assertEqual(status, "capped")
        self.assertEqual(seen, ["t1"])
        self.assertEqual(logged, ["t1"])

    def test_resume_keeps_memory_and_skips_the_finished_task(self):
        tasks = self._tasks()
        seen: list[str] = []

        def run_one_fn(task, path, config, policy, store, seed, forbid):
            seen.append(task.id)
            self.assertEqual(forbid, ["b" * 40] if task.id == "t1" else [])
            rows = store.candidates("example/repo", "2099-01-01T00:00:00+00:00")
            self.assertEqual([row.id for row in rows], ["kept"])
            record = _record(task.id, policy)
            record.sandbox_prep = {"seconds": 0, "future_commits_absent": True}
            return record

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            database = folder / "stream.sqlite"
            store = MemoryStore(database)
            store.add(
                MemoryEntry(
                    id="kept",
                    repo="example/repo",
                    source_task_id="t1",
                    created_at="2024-01-01T00:00:00+00:00",
                    base_commit="a" * 40,
                    issue_text="issue t1",
                    files_touched=["a.py"],
                    diagnosis="saved",
                    patch="diff\n",
                )
            )
            store.close()
            done = _record("t1")
            line = {
                "record": done.to_dict(),
                "score_details": {"where": "modal (env: image)"},
                "score_attempts": 1,
                "needs_score": False,
                "sandbox": {"future_commits_absent": True},
            }
            (folder / "run.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
            status = execute_stream(
                tasks,
                "none",
                database,
                load_config(ROOT / "configs" / "default.yaml"),
                jsonl_path=folder / "run.jsonl",
                ledger=SpendLedger(folder / "spend.json", cap=50),
                run_one_fn=run_one_fn,
                score_fn=lambda record, task: (record, _Score(), 1),
                seed=0,
            )
        self.assertEqual(status, "ok")
        self.assertEqual(seen, ["t2"])

    def test_a_remaining_future_commit_stops_the_stream(self):
        tasks = self._tasks()

        def run_one_fn(task, path, config, policy, store, seed, forbid):
            record = _record(task.id, policy)
            record.sandbox_prep = {"seconds": 0, "future_commits_absent": False}
            return record

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            status = execute_stream(
                tasks,
                "none",
                folder / "stream.sqlite",
                load_config(ROOT / "configs" / "default.yaml"),
                jsonl_path=folder / "run.jsonl",
                ledger=SpendLedger(folder / "spend.json", cap=50),
                run_one_fn=run_one_fn,
                score_fn=lambda record, task: (record, _Score(), 1),
                seed=0,
            )
        self.assertEqual(status, "leak")


class DryRunTest(unittest.TestCase):
    def test_dry_run_prints_the_two_pilot_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(
                    [
                        "--tasks",
                        str(ROOT / "data" / "live" / "tasks.json"),
                        "--config",
                        str(ROOT / "configs" / "pilot.yaml"),
                        "--streams",
                        "deepset-ai/haystack,reflex-dev/reflex",
                        "--spend-cap",
                        "20",
                        "--dry-run",
                        "--out",
                        tmp,
                    ]
                )
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("deepset-ai/haystack", text)
        self.assertIn("reflex-dev/reflex", text)
        self.assertNotIn("conan-io/conan", text)
        self.assertIn("pairs=8", text)


if __name__ == "__main__":
    unittest.main()
