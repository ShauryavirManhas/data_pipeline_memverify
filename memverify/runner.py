"""Run streams under every policy, score them, and stop at a spend cap.

A stream is one repo in ``tasks.json`` order. Each (stream, policy) gets its
own SQLite file. ``run_stream`` deletes that file, so this runner calls
``run_one`` and only deletes the database when the stream has no log yet.
Resume skips a finished prefix and refuses a log that skips a task.

Scoring uses Shaurya's scorer. ``resolved is None`` is retried up to two
more times. Model dollars and a Modal time estimate both count toward the cap.

``python -m memverify.runner --dry-run`` prints the plan and does not start
a sandbox or a model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from memverify.config import RunConfig, load_config
from memverify.memory import MemoryStore
from memverify.run import _fresh_store, run_one
from memverify.types import POLICIES, RunRecord, Task, load_task

# Shaurya's go/no-go rate: about $0.95/hour at 4 cores and 16 GB.
USD_PER_HOUR_AT_4CPU_16GB = 0.95
SCORE_ATTEMPTS = 3
SANDBOX_LOST_TRIES = 3


class SpendCapExceeded(Exception):
    def __init__(self, total: float, cap: float) -> None:
        self.total = total
        self.cap = cap
        super().__init__(f"spend cap reached: ${total:.4f} >= ${cap:.4f}")


class ResumeError(Exception):
    """The JSONL log and the memory database do not describe one prefix."""


@dataclass(frozen=True)
class Stream:
    repo: str
    task_files: list[Path]


def modal_usd(seconds: float, cpu: float, memory_mb: int) -> float:
    """Scale the measured 4-core / 16 GB rate by sandbox size."""
    if seconds <= 0 or cpu <= 0 or memory_mb <= 0:
        return 0.0
    scale = (cpu / 4.0) * (memory_mb / 16384.0)
    return seconds / 3600.0 * USD_PER_HOUR_AT_4CPU_16GB * scale


def load_streams(tasks_json: Path | str) -> list[Stream]:
    """Streams from the loader's ``tasks.json``. Paths are relative to that file."""
    path = Path(tasks_json)
    payload = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    streams = []
    for item in payload["streams"]:
        files = [root / rel for rel in item["task_files"]]
        streams.append(Stream(repo=item["repo"], task_files=files))
    return streams


def select_streams(streams: list[Stream], names: Sequence[str] | None, take_all: bool) -> list[Stream]:
    if take_all:
        return list(streams)
    if not names:
        raise ValueError("pass --streams or --all")
    by_repo = {stream.repo: stream for stream in streams}
    missing = [name for name in names if name not in by_repo]
    if missing:
        known = ", ".join(stream.repo for stream in streams)
        raise ValueError(f"unknown stream {missing[0]!r}. Known repos: {known}")
    return [by_repo[name] for name in names]


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def read_log(path: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        found[item["record"]["task_id"]] = item
    return found


def write_log(path: Path, task_ids: Sequence[str], logged: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(logged[tid], sort_keys=True) for tid in task_ids if tid in logged]
    text = ("\n".join(lines) + "\n") if lines else ""
    path.write_text(text, encoding="utf-8")


def log_state(item: dict[str, Any] | None) -> str:
    """``missing``, ``rescore``, or ``complete``."""
    if item is None:
        return "missing"
    resolved = item["record"]["resolved"]
    attempts = int(item.get("score_attempts") or 0)
    if item.get("needs_score") and resolved is None and attempts < SCORE_ATTEMPTS:
        return "rescore"
    return "complete"


def split_work(task_ids: Sequence[str], logged: dict[str, dict[str, Any]]) -> tuple[list[str], int]:
    """Return task ids to rescore, and the index of the first task still to run.

    A logged task after a missing one is an error. Memory would not match the log.
    """
    rescore: list[str] = []
    for index, task_id in enumerate(task_ids):
        state = log_state(logged.get(task_id))
        if state == "missing":
            for later in task_ids[index + 1 :]:
                if later in logged:
                    raise ResumeError(
                        f"{later} is logged but {task_id} is not. "
                        "The stream has to stay in order so memory matches the log."
                    )
            return rescore, index
        if state == "rescore":
            rescore.append(task_id)
    return rescore, len(task_ids)


class SpendLedger:
    """Cross-process dollar total. ``add`` raises once the cap is passed."""

    def __init__(self, path: Path | str, cap: float) -> None:
        self.path = Path(path)
        self.cap = cap
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.is_file():
            self.path.write_text(json.dumps({"usd": 0.0, "events": []}), encoding="utf-8")

    def total(self) -> float:
        """Read the total under the same lock ``add`` uses.

        Two runners share this file. An unlocked read can land in the middle
        of a rewrite and raise ``JSONDecodeError``, which kills that seed.
        """
        import fcntl

        if not self.path.is_file():
            return 0.0
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            handle.seek(0)
            raw = handle.read() or '{"usd": 0.0, "events": []}'
            return float(json.loads(raw).get("usd") or 0.0)

    def exceeded(self) -> bool:
        return self.total() >= self.cap

    def add(self, usd: float, note: str) -> float:
        import fcntl

        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            raw = handle.read() or '{"usd": 0.0, "events": []}'
            data = json.loads(raw)
            data["usd"] = float(data.get("usd") or 0.0) + float(usd)
            data.setdefault("events", []).append({"usd": float(usd), "note": note})
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle)
            handle.flush()
            total = float(data["usd"])
        if total >= self.cap:
            raise SpendCapExceeded(total, self.cap)
        return total


def estimate_charge(
    record: RunRecord,
    prep: dict[str, Any] | None,
    details: dict[str, Any] | None,
    *,
    agent_cpu: float,
    agent_memory_mb: int,
    score_cpu: float,
    score_memory_mb: int,
    include_model: bool,
) -> float:
    agent_seconds = float((prep or {}).get("seconds") or 0.0)
    score_seconds = 0.0
    if details and str(details.get("where") or "").startswith("modal"):
        score_seconds = float(details.get("seconds_total") or 0.0)
    dollars = modal_usd(agent_seconds, agent_cpu, agent_memory_mb)
    dollars += modal_usd(score_seconds, score_cpu, score_memory_mb)
    if include_model:
        dollars += record.cost_usd
    return dollars


def execute_stream(
    tasks: Sequence[tuple[Task, Path]],
    policy: str,
    db_path: Path,
    config: RunConfig,
    *,
    jsonl_path: Path,
    ledger: SpendLedger,
    run_one_fn: Callable[..., RunRecord],
    score_fn: Callable[[RunRecord, Task], tuple[RunRecord, Any, int]] | None,
    seed: int,
    agent_cpu: float = 2.0,
    agent_memory_mb: int = 8192,
    score_cpu: float = 2.0,
    score_memory_mb: int = 8192,
) -> str:
    """Run one stream under one policy. Returns ``ok``, ``capped``, or ``leak``."""
    logged = read_log(jsonl_path)
    task_ids = [task.id for task, _path in tasks]
    rescore_ids, start = split_work(task_ids, logged)
    if logged and not db_path.is_file():
        raise ResumeError(f"memory database missing for a resumed stream: {db_path}")
    store: MemoryStore = _fresh_store(db_path) if not logged else MemoryStore(db_path)
    by_id = {task.id: (task, path) for task, path in tasks}
    try:
        for task_id in rescore_ids:
            if ledger.exceeded():
                return "capped"
            if score_fn is None:
                continue
            item = logged[task_id]
            record = RunRecord.from_dict(item["record"])
            task, _path = by_id[task_id]
            record, result, attempts = score_fn(record, task)
            details = getattr(result, "details", {}) or {}
            item["record"] = record.to_dict()
            item["score_details"] = details
            item["score_attempts"] = int(item.get("score_attempts") or 0) + attempts
            item["needs_score"] = record.resolved is None
            logged[task_id] = item
            write_log(jsonl_path, task_ids, logged)
            charge = estimate_charge(
                record,
                None,
                details,
                agent_cpu=agent_cpu,
                agent_memory_mb=agent_memory_mb,
                score_cpu=score_cpu,
                score_memory_mb=score_memory_mb,
                include_model=False,
            )
            if _charge(ledger, charge, f"rescore {task_id} {policy}"):
                return "capped"

        bases = [task.base_commit for task, _path in tasks]
        for offset, (task, path) in enumerate(tasks[start:]):
            if ledger.exceeded():
                return "capped"
            index = start + offset
            record = None
            for attempt in range(1, SANDBOX_LOST_TRIES + 1):
                try:
                    record = run_one_fn(
                        task,
                        path,
                        config,
                        policy,
                        store,
                        seed,
                        bases[index + 1 :],
                    )
                    break
                except Exception as exc:
                    from memverify.sandbox import SandboxLost

                    if not isinstance(exc, SandboxLost):
                        raise
                    print(
                        f"{task.id} {policy}: sandbox lost ({attempt}/{SANDBOX_LOST_TRIES}): {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    text = str(exc).lower()
                    if "spend limit" in text or "billing cycle" in text:
                        return "capped"
                    if attempt == SANDBOX_LOST_TRIES:
                        raise
                    time.sleep(5)
            assert record is not None
            prep = getattr(record, "sandbox_prep", None)
            details = None
            attempts = 0
            needs_score = False
            if score_fn is not None:
                record, result, attempts = score_fn(record, task)
                details = getattr(result, "details", {}) or {}
                needs_score = record.resolved is None
            logged[task.id] = {
                "record": record.to_dict(),
                "score_details": details,
                "score_attempts": attempts,
                "needs_score": needs_score,
                "sandbox": prep,
            }
            write_log(jsonl_path, task_ids, logged)
            charge = estimate_charge(
                record,
                prep if isinstance(prep, dict) else None,
                details,
                agent_cpu=agent_cpu,
                agent_memory_mb=agent_memory_mb,
                score_cpu=score_cpu,
                score_memory_mb=score_memory_mb,
                include_model=True,
            )
            capped = _charge(ledger, charge, f"{task.id} {policy}")
            absent = None if not isinstance(prep, dict) else prep.get("future_commits_absent")
            if absent is False:
                return "leak"
            if capped:
                return "capped"
        return "ok"
    finally:
        store.close()


def _charge(ledger: SpendLedger, usd: float, note: str) -> bool:
    try:
        ledger.add(usd, note)
    except SpendCapExceeded:
        return True
    return False


def _score_with_retries(spec: dict, record: RunRecord, task: Task) -> tuple[RunRecord, Any, int]:
    from memverify.scorer import InstanceStore, make_backend, score_record

    if not hasattr(_score_with_retries, "_cache"):
        _score_with_retries._cache = {}  # type: ignore[attr-defined]
    cache: dict = _score_with_retries._cache  # type: ignore[attr-defined]
    key = (spec["instances"], spec["score_backend"], spec["score_cpu"], spec["score_memory_mb"])
    if key not in cache:
        backend = make_backend(spec["score_backend"])
        if spec["score_backend"] == "modal":
            backend.cpu = spec["score_cpu"]
            backend.memory_mb = spec["score_memory_mb"]
        cache[key] = (InstanceStore.from_jsonl(spec["instances"]), backend)
    store, backend = cache[key]
    log_dir = str(Path(spec["out"]) / "score_logs" / spec["slug"] / spec["policy"])
    last = None
    current = record
    for attempt in range(1, SCORE_ATTEMPTS + 1):
        current, last = score_record(
            current,
            task,
            instances=store,
            backend=backend,
            log_dir=log_dir,
        )
        if current.resolved is not None:
            return current, last, attempt
    return current, last, SCORE_ATTEMPTS


def _run_pair(spec: dict) -> dict[str, str]:
    """One (stream, policy). Top-level so a process pool can call it."""
    os.environ["MEMVERIFY_TRACE_DIR"] = str(Path(spec["out"]) / "traces" / spec["slug"] / spec["policy"])
    config = load_config(spec["config"])
    config = replace(config, seed=int(spec["seed"]))
    tasks = [(load_task(path), Path(path)) for path in spec["task_files"]]
    ledger = SpendLedger(spec["spend_path"], float(spec["spend_cap"]))

    def run_one_fn(task, path, cfg, policy, store, seed, forbid):
        factory = None
        if cfg.environment_kind == "docker":
            from memverify.sandbox import ModalCommandSandbox

            def factory(current, current_config, commits, _cpu=spec["agent_cpu"], _mem=spec["agent_memory_mb"]):
                return ModalCommandSandbox(
                    current,
                    current_config,
                    forbid_commits=commits,
                    cpu=_cpu,
                    memory_mb=_mem,
                )

        return run_one(
            task,
            path,
            cfg,
            policy,
            store,
            seed=seed,
            sandbox_factory=factory,
            forbid_commits=forbid,
        )

    score_fn = None if spec.get("no_score") else (lambda record, task: _score_with_retries(spec, record, task))
    status = execute_stream(
        tasks,
        spec["policy"],
        Path(spec["db_path"]),
        config,
        jsonl_path=Path(spec["jsonl_path"]),
        ledger=ledger,
        run_one_fn=run_one_fn,
        score_fn=score_fn,
        seed=int(spec["seed"]),
        agent_cpu=float(spec["agent_cpu"]),
        agent_memory_mb=int(spec["agent_memory_mb"]),
        score_cpu=float(spec["score_cpu"]),
        score_memory_mb=int(spec["score_memory_mb"]),
    )
    print(f"{spec['repo']} {spec['policy']}: {status}", file=sys.stderr)
    return {"repo": spec["repo"], "policy": spec["policy"], "status": status}


def build_specs(
    streams: Sequence[Stream],
    policies: Sequence[str],
    *,
    config: Path,
    out: Path,
    seed: int,
    spend_cap: float,
    instances: Path,
    score_backend: str,
    agent_cpu: float,
    agent_memory_mb: int,
    score_cpu: float,
    score_memory_mb: int,
    no_score: bool,
) -> list[dict[str, Any]]:
    specs = []
    for stream in streams:
        for policy in policies:
            name = f"{slug(stream.repo)}__{policy}__s{seed}"
            specs.append(
                {
                    "repo": stream.repo,
                    "policy": policy,
                    "slug": slug(stream.repo),
                    "task_files": [str(path) for path in stream.task_files],
                    "config": str(config),
                    "db_path": str(out / f"{name}.sqlite"),
                    "jsonl_path": str(out / f"{name}.jsonl"),
                    "spend_path": str(out / "spend.json"),
                    "spend_cap": spend_cap,
                    "seed": seed,
                    "out": str(out),
                    "instances": str(instances),
                    "score_backend": score_backend,
                    "agent_cpu": agent_cpu,
                    "agent_memory_mb": agent_memory_mb,
                    "score_cpu": score_cpu,
                    "score_memory_mb": score_memory_mb,
                    "no_score": no_score,
                }
            )
    return specs


def format_plan(specs: Sequence[dict[str, Any]]) -> str:
    lines = []
    for spec in specs:
        lines.append(f"{spec['repo']}  {spec['policy']}  tasks={len(spec['task_files'])}  db={spec['db_path']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memverify.runner")
    parser.add_argument("--tasks", type=Path, default=Path("data/live/tasks.json"))
    parser.add_argument("--config", type=Path, default=Path("configs/pilot.yaml"))
    parser.add_argument("--streams", default="", help="Comma-separated repo names, for example deepset-ai/haystack,reflex-dev/reflex")
    parser.add_argument("--all", action="store_true", help="Every stream in tasks.json")
    parser.add_argument("--policies", default=",".join(POLICIES))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--spend-cap", type=float, required=True, help="Abort once model dollars plus estimated Modal dollars reach this")
    parser.add_argument("--out", type=Path, default=Path("runs/pilot"))
    parser.add_argument("--jobs", type=int, default=1, help="Parallel (stream, policy) processes. Default 1.")
    parser.add_argument("--instances", type=Path, default=Path("data/live/instances.jsonl"))
    parser.add_argument("--score-backend", default="modal", choices=["modal", "docker"])
    parser.add_argument("--agent-cpu", type=float, default=2.0)
    parser.add_argument("--agent-memory-mb", type=int, default=8192)
    parser.add_argument("--score-cpu", type=float, default=2.0)
    parser.add_argument("--score-memory-mb", type=int, default=8192)
    parser.add_argument("--no-score", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    policies = [item.strip() for item in args.policies.split(",") if item.strip()]
    unknown = [item for item in policies if item not in POLICIES]
    if unknown or not policies:
        print(f"policies must be among {', '.join(POLICIES)}", file=sys.stderr)
        return 1
    names = [item.strip() for item in args.streams.split(",") if item.strip()]
    try:
        streams = select_streams(load_streams(args.tasks), names, args.all)
    except (OSError, ValueError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not args.no_score and not args.dry_run and not args.instances.is_file():
        print(
            f"instances file not found: {args.instances}\n"
            "Generate it with: python -m memverify.dataset --split lite --out-dir data/live",
            file=sys.stderr,
        )
        return 1
    specs = build_specs(
        streams,
        policies,
        config=args.config,
        out=args.out,
        seed=args.seed,
        spend_cap=args.spend_cap,
        instances=args.instances,
        score_backend=args.score_backend,
        agent_cpu=args.agent_cpu,
        agent_memory_mb=args.agent_memory_mb,
        score_cpu=args.score_cpu,
        score_memory_mb=args.score_memory_mb,
        no_score=args.no_score,
    )
    if args.dry_run:
        print(format_plan(specs))
        print(f"pairs={len(specs)} spend_cap={args.spend_cap}")
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    SpendLedger(args.out / "spend.json", args.spend_cap)
    if args.jobs <= 1:
        results = [_run_pair(spec) for spec in specs]
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(_run_pair, specs))
    print(json.dumps(results, indent=2))
    failed = [item for item in results if item["status"] != "ok"]
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
