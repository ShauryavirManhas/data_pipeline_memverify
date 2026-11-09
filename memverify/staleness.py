"""Staleness of a remembered fix. Owner: Shaurya. Stage 4 of the data plan.

Definition (METRICS.md): a memory entry is *stale* for a task when any
path in ``entry.files_touched`` differs between ``entry.base_commit`` and
``task.base_commit``, by git tree comparison:

    git diff --no-renames --name-only <entry.base_commit> <task.base_commit> -- <files_touched>

A path that was deleted, added, or renamed away counts as changed. This
is a property of the two commits only. It ignores what the agent did,
and it does not ask whether the old patch still applies.

Two finer, secondary measures are also available (``detail=True``),
because on SWE-bench-Live Lite the file-level flag is true for most
pairs (see METRICS.md):

- ``stale_hunks``: some line the remembered patch edited (old side,
  +/- 3 lines) changed between the two commits;
- ``patch_applies``: the remembered patch still passes
  ``git apply --check`` at ``task.base_commit``.

Edge cases, fixed by definition:

- same commit, or ``files_touched`` empty -> not stale;
- ``entry.repo != task.repo`` -> error (retrieval never crosses repos).

The check runs on the host against a full-history clone of the GitHub
repo (``RepoCache``), not inside the task image, so it does not depend
on how much history the image kept. ``StaleResult.is_ancestor`` also
answers the SPEC's open "ancestor check" question for each pair.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from memverify.types import MemoryEntry, Task


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, ok: Sequence[int] = (0,)) -> subprocess.CompletedProcess:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if done.returncode not in ok:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {done.stderr.strip()}")
    return done


def has_commit(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", ok=(0, 1, 128)).returncode == 0


class RepoCache:
    """Full-history, blobless clones of GitHub repos, one per ``owner/name``.

    Blobless (``--filter=blob:none``) is enough: name-only diffs and
    ancestry only read commits and trees.
    """

    def __init__(self, root: str | Path | None = None, remote_fmt: str = "https://github.com/{repo}.git"):
        self.root = Path(root or os.environ.get("MEMVERIFY_REPO_CACHE", "data/repos"))
        self.remote_fmt = remote_fmt

    def path(self, repo: str) -> Path:
        return self.root / repo.replace("/", "__")

    def ensure(self, repo: str, commits: Sequence[str] = ()) -> Path:
        path = self.path(repo)
        if not (path / "HEAD").exists() and not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            done = subprocess.run(
                ["git", "clone", "--bare", "--filter=blob:none", self.remote_fmt.format(repo=repo), str(path)],
                capture_output=True, text=True,
            )
            if done.returncode != 0:
                raise GitError(f"clone of {repo} failed: {done.stderr.strip()}")
        missing = [c for c in commits if not has_commit(path, c)]
        if missing:
            _git(path, "fetch", "--filter=blob:none", "origin", *missing, ok=(0, 1, 128))
            still = [c for c in missing if not has_commit(path, c)]
            if still:
                raise GitError(f"{repo}: commits not found after fetch: {still}")
        return path


def changed_files(repo_dir: str | Path, old: str, new: str, paths: Sequence[str]) -> list[str]:
    """Subset of ``paths`` whose content differs between commits ``old`` and ``new``."""
    if not paths or old == new:
        return []
    done = _git(Path(repo_dir), "diff", "--no-renames", "--name-only", old, new, "--", *paths)
    wanted = set(paths)
    return [line for line in done.stdout.splitlines() if line in wanted]


def is_ancestor(repo_dir: str | Path, old: str, new: str) -> bool:
    return _git(Path(repo_dir), "merge-base", "--is-ancestor", old, new, ok=(0, 1)).returncode == 0


def _old_ranges(patch: str) -> dict[str, list[tuple[int, int]]]:
    """Old-side line ranges ``[start, end]`` per file, from a unified diff."""
    from unidiff import PatchSet

    out: dict[str, list[tuple[int, int]]] = {}
    for item in PatchSet(patch):
        if item.source_file == "/dev/null":
            continue
        rel = item.source_file[2:] if item.source_file.startswith("a/") else item.source_file
        for hunk in item:
            start = hunk.source_start
            end = start + max(hunk.source_length, 1) - 1
            out.setdefault(rel, []).append((start, end))
    return out


def changed_hunk_files(repo_dir: str | Path, old: str, new: str, patch: str, margin: int = 3) -> list[str]:
    """Files where lines the remembered patch edited (old side, +/- ``margin``) changed between commits.

    Secondary, finer measure than file-level staleness. A file the patch
    edits that is gone at ``new`` counts. Files the patch creates do not.
    """
    if old == new or not patch.strip():
        return []
    try:
        mine = _old_ranges(patch)
    except Exception:
        return []
    if not mine:
        return []
    done = _git(Path(repo_dir), "diff", "--no-renames", "-U0", old, new, "--", *mine)
    try:
        theirs = _old_ranges(done.stdout)
    except Exception:
        return sorted(mine)
    hit: set[str] = set()  # a deleted file shows up in ``theirs`` covering every old line
    for rel, ranges in mine.items():
        for a0, a1 in ranges:
            for b0, b1 in theirs.get(rel, []):
                if b0 <= a1 + margin and a0 - margin <= b1:
                    hit.add(rel)
    return sorted(hit)


def patch_applies(repo_dir: str | Path, patch: str, commit: str) -> bool | None:
    """Whether ``patch`` still applies cleanly (``git apply --check``) at ``commit``.

    Works in a bare or blobless clone (temporary index; blobs are fetched on demand).
    ``None`` for an empty patch.
    """
    if not patch.strip():
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        repo = str(repo_dir)
        subprocess.run(["git", "-C", repo, "read-tree", commit], check=True, capture_output=True, env=env)
        done = subprocess.run(
            ["git", "-C", repo, "apply", "--cached", "--check", "-"],
            input=patch, capture_output=True, text=True, env=env,
        )
        return done.returncode == 0


@dataclass
class StaleResult:
    stale: bool
    changed_files: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    is_ancestor: bool | None = None
    commits_between: int | None = None
    # Secondary measures (filled when ``detail=True``):
    stale_hunks: bool | None = None  # the edited lines (+/- 3) changed
    changed_hunk_files: list[str] | None = None
    patch_applies: bool | None = None  # remembered patch still applies at task.base_commit


def staleness(
    entry: MemoryEntry,
    task: Task,
    repo_dir: str | Path | None = None,
    cache: RepoCache | None = None,
    detail: bool = False,
) -> StaleResult:
    """Full staleness result for one (entry, task) pair.

    ``detail=True`` also fills the secondary measures (needs file contents,
    so a blobless clone fetches the blobs it needs).
    """
    if entry.repo != task.repo:
        raise ValueError(f"entry repo {entry.repo} != task repo {task.repo}")
    files = list(entry.files_touched)
    same = entry.base_commit == task.base_commit
    if not files or same:
        result = StaleResult(False, [], files, True if same else None, 0 if same else None)
        if detail:
            result.stale_hunks, result.changed_hunk_files = False, []
        if detail and same and entry.patch.strip():
            # same tree as when the fix was made; it applies there by construction
            result.patch_applies = True
        return result
    if repo_dir is None:
        repo_dir = (cache or RepoCache()).ensure(task.repo, [entry.base_commit, task.base_commit])
    changed = changed_files(repo_dir, entry.base_commit, task.base_commit, files)
    ancestor = is_ancestor(repo_dir, entry.base_commit, task.base_commit)
    between = None
    if ancestor:
        between = int(_git(Path(repo_dir), "rev-list", "--count", f"{entry.base_commit}..{task.base_commit}").stdout.strip())
    result = StaleResult(bool(changed), changed, files, ancestor, between)
    if detail:
        hunk_files = changed_hunk_files(repo_dir, entry.base_commit, task.base_commit, entry.patch) if changed else []
        result.changed_hunk_files = hunk_files
        result.stale_hunks = bool(hunk_files)
        result.patch_applies = patch_applies(repo_dir, entry.patch, task.base_commit)
    return result


def stale(entry: MemoryEntry, task: Task, repo_dir: str | Path | None = None, cache: RepoCache | None = None) -> bool:
    """True if any file in ``entry.files_touched`` changed between the two base commits."""
    return staleness(entry, task, repo_dir=repo_dir, cache=cache).stale


def load_entry(db_path: str | Path, entry_id: str) -> MemoryEntry | None:
    """Read one entry from a stream's SQLite file without touching the store's schema."""
    import sqlite3

    from memverify.memory import _entry_from_row

    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return _entry_from_row(row) if row else None
    finally:
        conn.close()


def stale_for_record(record, task: Task, db_path: str | Path, cache: RepoCache | None = None, detail: bool = True) -> StaleResult | None:
    """Staleness of the hint a ``RunRecord`` retrieved, or ``None`` when it retrieved nothing.

    Uses ``hint_id`` (the retrieved candidate), so it is defined for
    ``read_check`` runs whose hint was dropped, too. Analysis joins this
    with ``hint_injected`` and ``verifier_decision``.
    """
    if record.hint_id is None:
        return None
    entry = load_entry(db_path, record.hint_id)
    if entry is None:
        raise KeyError(f"hint {record.hint_id} not found in {db_path}")
    return staleness(entry, task, cache=cache, detail=detail)
