"""SQLite memory and BM25 retrieval over earlier issues in the same repo."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path

from rank_bm25 import BM25Okapi

from memverify.types import MemoryEntry, Task, is_strictly_earlier, parse_created_at

_TOKEN = re.compile(r"[A-Za-z0-9]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  source_task_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  issue_text TEXT NOT NULL,
  files_touched TEXT NOT NULL,
  diagnosis TEXT NOT NULL,
  patch TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_repo_time ON entries (repo, created_at);
"""


def tokenize(text: str) -> list[str]:
    """Split on non-alphanumeric characters and lowercase."""
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


class MemoryStore:
    """One SQLite file for one stream. The policy is not stored."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, entry: MemoryEntry) -> None:
        self._conn.execute(
            """
            INSERT INTO entries (
              id, repo, source_task_id, created_at, base_commit,
              issue_text, files_touched, diagnosis, patch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.repo,
                entry.source_task_id,
                entry.created_at,
                entry.base_commit,
                entry.issue_text,
                json.dumps(entry.files_touched),
                entry.diagnosis,
                entry.patch,
            ),
        )
        self._conn.commit()

    def candidates(self, repo: str, before: str) -> list[MemoryEntry]:
        """Same repo, created_at strictly earlier than ``before``."""
        rows = self._conn.execute(
            """
            SELECT id, repo, source_task_id, created_at, base_commit,
                   issue_text, files_touched, diagnosis, patch
            FROM entries
            WHERE repo = ?
            """,
            (repo,),
        )
        found = []
        for row in rows:
            if is_strictly_earlier(row["created_at"], before):
                found.append(_entry_from_row(row))
        return found

    def retrieve(self, task: Task) -> MemoryEntry | None:
        """Top-1 BM25 hit with score greater than 0, or None."""
        return top_hit(self.candidates(task.repo, task.created_at), task.issue_text)


def top_hit(entries: list[MemoryEntry], query_text: str) -> MemoryEntry | None:
    """Highest BM25 score above 0. Ties keep the later created_at, then the smaller id."""
    if not entries:
        return None
    scores = _scores([entry.issue_text for entry in entries], query_text)
    best = min(
        range(len(entries)),
        key=lambda index: (
            -scores[index],
            -parse_created_at(entries[index].created_at).timestamp(),
            entries[index].id,
        ),
    )
    if scores[best] <= 0:
        return None
    return entries[best]


class _LuceneIdfBM25(BM25Okapi):
    """BM25Okapi term frequency with Lucene's IDF.

    ``log(1 + (N - n + 0.5) / (n + 0.5))`` stays positive for a term
    that appears in one of two documents. Stock Okapi IDF is 0 there.
    """

    def _calc_idf(self, nd: dict) -> None:
        for word, freq in nd.items():
            self.idf[word] = math.log(1.0 + (self.corpus_size - freq + 0.5) / (freq + 0.5))
        self.average_idf = sum(self.idf.values()) / len(self.idf) if self.idf else 0.0


def _scores(documents: list[str], query_text: str) -> list[float]:
    query = tokenize(query_text)
    tokenized = [tokenize(document) for document in documents]
    if not query or all(not tokens for tokens in tokenized):
        return [0.0] * len(documents)
    safe = [tokens if tokens else ["__empty__"] for tokens in tokenized]
    raw = _LuceneIdfBM25(safe).get_scores(query)
    scores = []
    for value in raw:
        number = float(value)
        scores.append(0.0 if math.isnan(number) else number)
    return scores


def _entry_from_row(row: sqlite3.Row) -> MemoryEntry:
    files = json.loads(row["files_touched"])
    return MemoryEntry(
        id=row["id"],
        repo=row["repo"],
        source_task_id=row["source_task_id"],
        created_at=row["created_at"],
        base_commit=row["base_commit"],
        issue_text=row["issue_text"],
        files_touched=list(files),
        diagnosis=row["diagnosis"],
        patch=row["patch"],
    )
