"""Retrieval tests. No model calls."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from memverify.memory import MemoryStore, top_hit
from memverify.types import MemoryEntry, load_task

ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "tasks" / "mock_mean" / "task.json"


def _entry(id_: str, created: str, issue: str, repo: str = "mock-mean") -> MemoryEntry:
    return MemoryEntry(
        id=id_,
        repo=repo,
        source_task_id=id_,
        created_at=created,
        base_commit="abc",
        issue_text=issue,
        files_touched=["mean.py"],
        diagnosis="note",
        patch="diff --git a/mean.py b/mean.py\n",
    )


class RetrievalTest(unittest.TestCase):
    def test_rare_words_rank_ahead_of_a_newer_entry(self):
        query = "xylophone residual diverged"
        newer = _entry("common", "2024-06-01T00:00:00+00:00", "widget color palette refresh")
        older = _entry("rare", "2024-01-01T00:00:00Z", "xylophone residual in the solver")
        hit = top_hit([newer, older], query)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.id, "rare")

    def test_zero_score_is_not_a_hit(self):
        docs = [
            _entry("a", "2024-01-01T00:00:00Z", "alpha beta"),
            _entry("b", "2024-06-01T00:00:00+00:00", "gamma delta"),
        ]
        self.assertIsNone(top_hit(docs, "xylophone"))
        task = replace(
            load_task(TASK_PATH),
            issue_text="xylophone",
            created_at="2024-08-01T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "stream.sqlite")
            for doc in docs:
                store.add(doc)
            self.assertEqual(len(store.candidates(task.repo, task.created_at)), 2)
            self.assertIsNone(store.retrieve(task))
            store.close()

    def test_later_time_and_other_repo_are_invisible(self):
        task = load_task(TASK_PATH)
        same = task.issue_text
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "stream.sqlite")
            store.add(_entry("same-time", task.created_at, same))
            store.add(_entry("later", "2024-08-01T00:00:00Z", same))
            store.add(_entry("other", "2020-01-01T00:00:00Z", same, repo="other-repo"))
            store.add(_entry("early", "2024-01-01T00:00:00Z", same))
            found = store.retrieve(task)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.id, "early")
            store.close()

    def test_equal_positive_scores_keep_the_later_entry(self):
        text = "mean returns 1.0 instead of 2.0"
        hit = top_hit(
            [
                _entry("older", "2024-01-01T00:00:00Z", text),
                _entry("newer", "2024-05-01T00:00:00+00:00", text),
            ],
            text,
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.id, "newer")


if __name__ == "__main__":
    unittest.main()
