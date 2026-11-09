"""Staleness tests on a local git repo with a known history. No network."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from memverify.memory import MemoryStore
from memverify.staleness import RepoCache, changed_files, load_entry, stale, stale_for_record, staleness
from memverify.types import MemoryEntry, RunRecord, Task

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def commit(repo: Path, msg: str, **files: str | None) -> str:
    for rel, text in files.items():
        path = repo / rel.replace("__", "/")
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


def entry(base: str, files: list[str], repo: str = "o/r") -> MemoryEntry:
    return MemoryEntry(
        id="e1", repo=repo, source_task_id="t0", created_at="2025-01-01T00:00:00+00:00", base_commit=base,
        issue_text="issue", files_touched=files, diagnosis="d", patch="p",
    )


def task(base: str, repo: str = "o/r") -> Task:
    return Task(id="t1", repo=repo, issue_text="new", base_commit=base, created_at="2025-02-01T00:00:00+00:00", workdir="/testbed", docker_image=None)


class StalenessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="memverify-stale-"))
        repo = cls.tmp / "o__r"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        cls.c0 = commit(repo, "c0", **{"src__a.py": "a = 0\n", "src__b.py": "b = 0\n", "README": "x\n"})
        cls.c1 = commit(repo, "c1 edits a", **{"src__a.py": "a = 1\n"})
        cls.c2 = commit(repo, "c2 edits README only", README="y\n")
        cls.c3 = commit(repo, "c3 deletes b", **{"src__b.py": None})
        cls.c4 = commit(repo, "c4 restores a to c0 content", **{"src__a.py": "a = 0\n"})
        git(repo, "checkout", "-q", "-b", "side", cls.c0)
        cls.side = commit(repo, "side edits b", **{"src__b.py": "b = 9\n"})
        git(repo, "checkout", "-q", "main")
        cls.repo = repo

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_touched_file_changed_is_stale(self):
        r = staleness(entry(self.c0, ["src/a.py"]), task(self.c1), repo_dir=self.repo)
        self.assertTrue(r.stale)
        self.assertEqual(r.changed_files, ["src/a.py"])
        self.assertTrue(r.is_ancestor)
        self.assertEqual(r.commits_between, 1)

    def test_other_file_changed_is_fresh(self):
        r = staleness(entry(self.c1, ["src/a.py", "src/b.py"]), task(self.c2), repo_dir=self.repo)
        self.assertFalse(r.stale)
        self.assertEqual(r.changed_files, [])

    def test_deleted_file_is_stale(self):
        self.assertTrue(stale(entry(self.c2, ["src/b.py"]), task(self.c3), repo_dir=self.repo))

    def test_content_compare_not_history(self):
        # a.py changed in c1 and was put back in c4: same content, so not stale.
        self.assertFalse(stale(entry(self.c0, ["src/a.py"]), task(self.c4), repo_dir=self.repo))

    def test_only_listed_files_count(self):
        self.assertEqual(changed_files(self.repo, self.c0, self.c3, ["src/a.py"]), ["src/a.py"])
        self.assertEqual(changed_files(self.repo, self.c0, self.c3, ["src/a.py", "src/b.py", "src/never.py"]), ["src/a.py", "src/b.py"])

    def test_same_commit_and_empty_files_are_fresh(self):
        self.assertFalse(stale(entry(self.c1, ["src/a.py"]), task(self.c1), repo_dir=self.repo))
        self.assertFalse(stale(entry(self.c0, []), task(self.c3), repo_dir=self.repo))

    def test_non_ancestor_still_compares_trees(self):
        r = staleness(entry(self.side, ["src/b.py"]), task(self.c2), repo_dir=self.repo)
        self.assertTrue(r.stale)
        self.assertFalse(r.is_ancestor)
        self.assertIsNone(r.commits_between)

    def test_cross_repo_is_an_error(self):
        with self.assertRaises(ValueError):
            stale(entry(self.c0, ["src/a.py"], repo="o/other"), task(self.c1), repo_dir=self.repo)

    def test_repo_cache_clones_and_fetches(self):
        with tempfile.TemporaryDirectory() as root:
            cache = RepoCache(root, remote_fmt=str(self.tmp) + "/{repo_dir}".replace("{repo_dir}", "o__r"))
            self.assertTrue(stale(entry(self.c0, ["src/a.py"]), task(self.c1), cache=cache))
            # second call reuses the clone; the side-branch commit is fetched on demand
            self.assertTrue(stale(entry(self.side, ["src/b.py"]), task(self.c2), cache=cache))

    def test_secondary_measures(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "h"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            lines = [f"x{i} = {i}\n" for i in range(40)]
            b0 = commit(repo, "b0", **{"long.py": "".join(lines)})
            # the remembered fix edits line 3
            edited = list(lines)
            edited[2] = "x2 = 'fixed'\n"
            (repo / "long.py").write_text("".join(edited))
            fix = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True).stdout
            git(repo, "checkout", "-q", "long.py")
            far = list(lines)
            far[30] = "x30 = 'other'\n"
            b1 = commit(repo, "b1 edits far away", **{"long.py": "".join(far)})
            near = list(far)
            near[3] = "x3 = 'near'\n"
            b2 = commit(repo, "b2 edits next to the fix", **{"long.py": "".join(near)})

            e = MemoryEntry(**{**entry(b0, ["long.py"]).to_dict(), "patch": fix})
            r1 = staleness(e, task(b1), repo_dir=repo, detail=True)
            self.assertTrue(r1.stale)  # file-level: the file changed
            self.assertFalse(r1.stale_hunks)  # but not near the edited line
            self.assertTrue(r1.patch_applies)
            r2 = staleness(e, task(b2), repo_dir=repo, detail=True)
            self.assertTrue(r2.stale_hunks)
            self.assertEqual(r2.changed_hunk_files, ["long.py"])
            self.assertFalse(r2.patch_applies)  # context line 4 changed
            r0 = staleness(e, task(b0), repo_dir=repo, detail=True)
            self.assertEqual((r0.stale, r0.stale_hunks, r0.patch_applies), (False, False, True))

    def test_stale_for_record_reads_the_stream_db(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "stream.sqlite"
            store = MemoryStore(db)
            store.add(entry(self.c0, ["src/a.py"]))
            store.close()
            self.assertEqual(load_entry(db, "e1").files_touched, ["src/a.py"])
            base = dict(
                task_id="t1", policy="read_check", seed=0, patch="", resolved=None, tokens_in=0, tokens_out=0,
                cost_usd=0.0, cost_agent_usd=0.0, cost_diagnosis_usd=0.0, cost_verifier_usd=0.0,
                hint_injected=False, entry_saved=False, verifier_decision="no", verifier_reason="r",
                wall_seconds=0.0, error=None,
            )
            cache = RepoCache(Path(d) / "repos", remote_fmt=str(self.repo))
            got = stale_for_record(RunRecord(hint_id="e1", **base), task(self.c1), db, cache=cache)
            self.assertTrue(got.stale)
            self.assertIsNone(stale_for_record(RunRecord(hint_id=None, **base), task(self.c1), db, cache=cache))


if __name__ == "__main__":
    unittest.main()
