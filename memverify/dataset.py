"""SWE-bench-Live loader: dataset rows -> ordered per-repo task streams.

Owner: Shaurya. Stage 2 of the data plan.

The selection rule is code, not hand picking:

1. Load every row of the split (Hugging Face name or a local JSONL).
2. Drop rows listed in an exclude file (for example instances whose
   gold patch failed on our machines in the go/no-go or a gold rerun).
3. Group by ``repo``. Order each group by ``created_at`` (parsed, UTC),
   then by ``instance_id`` to break ties.
4. Keep a repo only if it still has at least ``min_stream_len`` tasks.
5. Optionally cap a stream at its first ``max_stream_len`` tasks (time
   order) and keep at most ``max_streams`` streams (longest first, then
   repo name).

Outputs, all under ``--out-dir``:

- ``tasks.json``: the rule, the parameters, dataset name, split, and the
  ordered task ids per stream. This is the file the runner reads.
- ``tasks/<instance_id>/task.json``: one ``Task`` per instance, in the
  exact SPEC.md shape. Agent-visible.
- ``instances.jsonl``: the scorer-only fields (gold patch, test patch,
  FAIL_TO_PASS, PASS_TO_PASS, test commands, parser). The agent must
  never see this file. Do not mount it into the agent container.

``Task`` itself is unchanged. ``Task.workdir`` is ``/testbed`` (where the
repo lives inside the SWE-bench-Live image) and ``Task.docker_image``
is the official prebuilt image.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from memverify.types import Task, parse_created_at

DEFAULT_DATASET = "SWE-bench-Live/SWE-bench-Live"
DEFAULT_SPLIT = "lite"
DEFAULT_NAMESPACE = "starryzhang"
CONTAINER_WORKDIR = "/testbed"

# Fields the scorer needs. Everything here is hidden from the agent.
SCORER_FIELDS: tuple[str, ...] = (
    "instance_id",
    "repo",
    "base_commit",
    "created_at",
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "test_cmds",
    "log_parser",
    "version",
    "environment_setup_commit",
)
REQUIRED_FIELDS: tuple[str, ...] = (
    "instance_id",
    "repo",
    "base_commit",
    "created_at",
    "problem_statement",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
)


def docker_image_name(instance_id: str, namespace: str = DEFAULT_NAMESPACE, arch: str = "x86_64") -> str:
    """Official SWE-bench-Live instance image (same rule as the harness)."""
    key = f"sweb.eval.{arch}.{instance_id.lower()}:latest"
    return f"{namespace}/{key}".replace("__", "_1776_")


def normalize_created_at(value: Any) -> str:
    """Return a timezone-aware ISO-8601 string accepted by ``parse_created_at``.

    The dataset stores an ISO string; ``datasets`` may hand back a
    ``datetime``. A naive value is taken as UTC (GitHub timestamps are UTC).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Arrow timestamps sometimes arrive as epoch milliseconds.
        seconds = value / 1000 if value > 1e11 else value
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace(" ", "T", 1) if "T" not in value else value.strip()
        text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError(f"created_at is missing or not a timestamp: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    out = parsed.astimezone(timezone.utc).isoformat()
    parse_created_at(out)  # same validation the shared types use
    return out


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        loaded = json.loads(value) if value.strip() else []
    else:
        loaded = list(value or [])
    if not all(isinstance(item, str) for item in loaded):
        raise ValueError("test lists must be lists of strings")
    return loaded


def dataset_revision(dataset: str, revision: str | None = None) -> str | None:
    """Commit sha of a Hugging Face dataset (best effort; None for local files or offline)."""
    if Path(dataset).exists():
        return None
    try:
        from huggingface_hub import HfApi

        return HfApi().dataset_info(dataset, revision=revision).sha
    except Exception:
        return revision


def load_rows(dataset: str, split: str | None, revision: str | None = None) -> list[dict[str, Any]]:
    """Load rows from a local ``.jsonl``/``.json`` file or a Hugging Face dataset."""
    path = Path(dataset)
    if path.exists():
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return list(loaded)
    from datasets import load_dataset  # heavy import, only when needed

    return [dict(row) for row in load_dataset(dataset, split=split, revision=revision)]


@dataclass(frozen=True)
class SelectionParams:
    min_stream_len: int = 6
    max_stream_len: int | None = None
    max_streams: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_stream_len": self.min_stream_len,
            "max_stream_len": self.max_stream_len,
            "max_streams": self.max_streams,
        }


SELECTION_RULE = (
    "Group rows by repo after dropping excluded ids. Order each repo by parsed created_at (UTC), "
    "then instance_id. Keep repos with at least min_stream_len tasks. If max_stream_len is set, keep "
    "the first max_stream_len tasks of each stream in time order. If max_streams is set, keep the "
    "longest streams (ties broken by repo name)."
)


def check_row(row: Mapping[str, Any]) -> None:
    missing = [name for name in REQUIRED_FIELDS if name not in row or row[name] is None]
    if missing:
        raise ValueError(f"{row.get('instance_id', '<no id>')}: missing fields {missing}")


def select_streams(
    rows: Iterable[Mapping[str, Any]],
    params: SelectionParams = SelectionParams(),
    exclude_ids: Iterable[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Apply the selection rule. Returns ``{repo: [rows in stream order]}``."""
    excluded = set(exclude_ids)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for raw in rows:
        check_row(raw)
        row = dict(raw)
        if row["instance_id"] in seen:
            raise ValueError(f"duplicate instance_id {row['instance_id']}")
        seen.add(row["instance_id"])
        if row["instance_id"] in excluded:
            continue
        row["created_at"] = normalize_created_at(row["created_at"])
        groups[row["repo"]].append(row)

    streams: dict[str, list[dict[str, Any]]] = {}
    for repo, items in groups.items():
        items.sort(key=lambda r: (parse_created_at(r["created_at"]), r["instance_id"]))
        if len(items) < params.min_stream_len:
            continue
        if params.max_stream_len is not None:
            items = items[: params.max_stream_len]
        streams[repo] = items

    order = sorted(streams, key=lambda repo: (-len(streams[repo]), repo))
    if params.max_streams is not None:
        order = order[: params.max_streams]
    return {repo: streams[repo] for repo in order}


def row_to_task(row: Mapping[str, Any], namespace: str = DEFAULT_NAMESPACE) -> Task:
    """Build the agent-visible ``Task`` (only ``problem_statement`` from the issue)."""
    return Task(
        id=row["instance_id"],
        repo=row["repo"],
        issue_text=row["problem_statement"],
        base_commit=row["base_commit"],
        created_at=normalize_created_at(row["created_at"]),
        workdir=CONTAINER_WORKDIR,
        docker_image=row.get("docker_image") or docker_image_name(row["instance_id"], namespace),
    )


def scorer_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """The hidden, scorer-only view of a row."""
    out: dict[str, Any] = {}
    for name in SCORER_FIELDS:
        if name in row and row[name] is not None:
            out[name] = row[name]
    out["created_at"] = normalize_created_at(row["created_at"])
    out["FAIL_TO_PASS"] = _as_list(row["FAIL_TO_PASS"])
    out["PASS_TO_PASS"] = _as_list(row["PASS_TO_PASS"])
    if "test_cmds" in out:
        out["test_cmds"] = _as_list(out["test_cmds"]) if not isinstance(out["test_cmds"], str) else [out["test_cmds"]]
    return out


def write_outputs(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    out_dir: Path,
    *,
    dataset: str,
    split: str | None,
    params: SelectionParams,
    exclude_ids: Sequence[str],
    namespace: str = DEFAULT_NAMESPACE,
    revision: str | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    task_root = out_dir / "tasks"
    manifest_streams = []
    with (out_dir / "instances.jsonl").open("w", encoding="utf-8") as hidden:
        for repo, items in streams.items():
            ids = []
            for row in items:
                task = row_to_task(row, namespace)
                task_dir = task_root / task.id
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "task.json").write_text(task.to_json(), encoding="utf-8")
                hidden.write(json.dumps(scorer_record(row)) + "\n")
                ids.append(task.id)
            manifest_streams.append(
                {
                    "repo": repo,
                    "n_tasks": len(ids),
                    "task_ids": ids,
                    "task_files": [f"tasks/{i}/task.json" for i in ids],
                    "first_created_at": normalize_created_at(items[0]["created_at"]),
                    "last_created_at": normalize_created_at(items[-1]["created_at"]),
                }
            )
    manifest = {
        "dataset": dataset,
        "split": split,
        "revision": revision,
        "namespace": namespace,
        "selection_rule": SELECTION_RULE,
        "params": params.to_dict(),
        "excluded_ids": sorted(exclude_ids),
        "n_streams": len(manifest_streams),
        "n_tasks": sum(s["n_tasks"] for s in manifest_streams),
        "streams": manifest_streams,
        "task_ids": [i for s in manifest_streams for i in s["task_ids"]],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "tasks.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_exclude_file(path: str | None) -> list[str]:
    if not path:
        return []
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        loaded = json.loads(text)
        return list(loaded if isinstance(loaded, list) else loaded.get("exclude_ids", []))
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select SWE-bench-Live task streams and write tasks.json")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="HF dataset name or local .jsonl")
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--out-dir", default="data/live")
    parser.add_argument("--min-stream-len", type=int, default=6)
    parser.add_argument("--max-stream-len", type=int, default=None)
    parser.add_argument("--max-streams", type=int, default=None)
    parser.add_argument("--exclude-file", default=None, help="ids to drop (txt one per line, or JSON list)")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--revision", default=None, help="HF dataset commit to pin (recorded in tasks.json)")
    args = parser.parse_args(argv)

    params = SelectionParams(args.min_stream_len, args.max_stream_len, args.max_streams)
    revision = dataset_revision(args.dataset, args.revision)
    rows = load_rows(args.dataset, args.split, revision)
    exclude = read_exclude_file(args.exclude_file)
    streams = select_streams(rows, params, exclude)
    manifest = write_outputs(
        streams,
        Path(args.out_dir),
        dataset=args.dataset,
        split=args.split,
        params=params,
        exclude_ids=exclude,
        namespace=args.namespace,
        revision=revision,
    )
    print(f"{len(rows)} rows -> {manifest['n_streams']} streams, {manifest['n_tasks']} tasks")
    for stream in manifest["streams"]:
        print(f"  {stream['n_tasks']:3d}  {stream['repo']}")
    return 0 if manifest["n_streams"] else 1


if __name__ == "__main__":
    sys.exit(main())
