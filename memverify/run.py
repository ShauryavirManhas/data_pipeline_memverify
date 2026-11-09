"""Run one task, or a stream of tasks, under one memory policy.

``python -m memverify.run`` is the single-task command. ``run_stream``
runs several tasks in dataset order against a fresh database.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from memverify.agent import (
    Complete,
    ModelCallFailed,
    make_checkout,
    remove_checkout,
    run_in_checkout,
    run_task,
)
from memverify.config import RunConfig, load_config
from memverify.memory import MemoryStore
from memverify.policies import compose_prompt, format_hint, reads_memory, should_save
from memverify.types import POLICIES, MemoryEntry, RunRecord, Task, load_task, parse_created_at
from memverify.verifier import VerifierVerdict, build_file_contents, verify_read, verify_write


def run_one(
    task: Task,
    task_path: Path | str,
    config: RunConfig,
    policy: str,
    store: MemoryStore,
    *,
    model: Any = None,
    complete: Complete | None = None,
    seed: int | None = None,
) -> RunRecord:
    """Execute ``task`` and, when the policy says so, read or write ``store``.

    ``resolved`` stays null. A verifier rejection is a ``no`` on the record,
    not an ``error``. ``error`` is the agent failure, when there is one.
    """
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
    if seed is not None:
        config = replace(config, seed=seed)
    started = time.perf_counter()
    if config.environment_kind == "docker":
        agent = run_task(task, task_path, config, model=model, complete=complete, diagnose=False)
        return _record(
            task,
            policy,
            config,
            agent_tokens_in=agent.agent_tokens_in,
            agent_tokens_out=agent.agent_tokens_out,
            diagnosis_tokens_in=0,
            diagnosis_tokens_out=0,
            cost_agent_usd=agent.cost_agent_usd,
            cost_diagnosis_usd=0.0,
            verifier=None,
            hint_id=None,
            hint_injected=False,
            entry_saved=False,
            patch=agent.patch,
            wall_seconds=time.perf_counter() - started,
            error=agent.error,
        )

    hint_id: str | None = None
    hint_injected = False
    entry_saved = False
    verdict: VerifierVerdict | None = None
    patch = ""
    agent_in = agent_out = diagnosis_in = diagnosis_out = 0
    cost_agent = cost_diagnosis = 0.0
    error: str | None = None
    checkout = None
    try:
        checkout = make_checkout(task, task_path)
        prompt = task.issue_text
        hint = store.retrieve(task) if reads_memory(policy) else None
        if hint is not None and policy == "read_check":
            hint_id = hint.id
            verdict = verify_read(
                config,
                issue_text=task.issue_text,
                hint=hint,
                file_contents=build_file_contents(checkout, hint, config),
                complete=complete,
            )
            if verdict.decision == "yes":
                hint_injected = True
                prompt = compose_prompt(task.issue_text, format_hint(hint, config.hint_prefix))
        elif hint is not None:
            hint_id = hint.id
            hint_injected = True
            prompt = compose_prompt(task.issue_text, format_hint(hint, config.hint_prefix))

        agent = run_in_checkout(
            checkout,
            task,
            config,
            model=model,
            complete=complete,
            task_prompt=prompt,
            diagnose=policy != "none",
        )
        patch = agent.patch
        agent_in = agent.agent_tokens_in
        agent_out = agent.agent_tokens_out
        diagnosis_in = agent.diagnosis_tokens_in
        diagnosis_out = agent.diagnosis_tokens_out
        cost_agent = agent.cost_agent_usd
        cost_diagnosis = agent.cost_diagnosis_usd
        error = agent.error
        if not agent.model_call_failed and policy == "write_check" and patch.strip():
            verdict = verify_write(
                config,
                issue_text=task.issue_text,
                diagnosis=agent.diagnosis,
                patch=patch,
                complete=complete,
            )
        write_decision = verdict.decision if policy == "write_check" and verdict is not None else None
        if not agent.model_call_failed and should_save(policy, patch, write_decision):
            store.add(_entry(task, agent))
            entry_saved = True
    except ModelCallFailed as exc:
        error = str(exc)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if checkout is not None:
            remove_checkout(checkout)
    return _record(
        task,
        policy,
        config,
        agent_tokens_in=agent_in,
        agent_tokens_out=agent_out,
        diagnosis_tokens_in=diagnosis_in,
        diagnosis_tokens_out=diagnosis_out,
        cost_agent_usd=cost_agent,
        cost_diagnosis_usd=cost_diagnosis,
        verifier=verdict,
        hint_id=hint_id,
        hint_injected=hint_injected,
        entry_saved=entry_saved,
        patch=patch,
        wall_seconds=time.perf_counter() - started,
        error=error,
    )


def _entry(task: Task, agent: Any) -> MemoryEntry:
    return MemoryEntry(
        id=uuid.uuid4().hex,
        repo=task.repo,
        source_task_id=task.id,
        created_at=task.created_at,
        base_commit=task.base_commit,
        issue_text=task.issue_text,
        files_touched=list(agent.files_touched),
        diagnosis=agent.diagnosis,
        patch=agent.patch,
    )


def _record(
    task: Task,
    policy: str,
    config: RunConfig,
    *,
    agent_tokens_in: int,
    agent_tokens_out: int,
    diagnosis_tokens_in: int,
    diagnosis_tokens_out: int,
    cost_agent_usd: float,
    cost_diagnosis_usd: float,
    verifier: VerifierVerdict | None,
    hint_id: str | None,
    hint_injected: bool,
    entry_saved: bool,
    patch: str,
    wall_seconds: float,
    error: str | None,
) -> RunRecord:
    verifier_in = 0 if verifier is None else verifier.tokens_in
    verifier_out = 0 if verifier is None else verifier.tokens_out
    verifier_cost = 0.0 if verifier is None else verifier.cost_usd
    tokens_in = agent_tokens_in + diagnosis_tokens_in + verifier_in
    tokens_out = agent_tokens_out + diagnosis_tokens_out + verifier_out
    return RunRecord(
        task_id=task.id,
        policy=policy,
        seed=config.seed,
        patch=patch,
        resolved=None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_agent_usd + cost_diagnosis_usd + verifier_cost,
        cost_agent_usd=cost_agent_usd,
        cost_diagnosis_usd=cost_diagnosis_usd,
        cost_verifier_usd=verifier_cost,
        hint_id=hint_id,
        hint_injected=hint_injected,
        entry_saved=entry_saved,
        verifier_decision=None if verifier is None else verifier.decision,
        verifier_reason=None if verifier is None else verifier.reason,
        wall_seconds=wall_seconds,
        error=error,
    )


def run_stream(
    tasks: Sequence[Path | str | tuple[Task, Path | str]],
    policy: str,
    db_path: Path | str,
    config: RunConfig,
    *,
    model: Any = None,
    complete: Complete | None = None,
    seed: int | None = None,
) -> list[RunRecord]:
    """Run ``tasks`` in ``created_at`` order, then by ``id``, on a fresh database.

    Each item is a task JSON path, or a ``(Task, task_path)`` pair.
    ``task_path`` is the file ``workdir`` resolves against. An existing
    file at ``db_path`` is deleted first, so the stream does not see
    rows from an earlier run.
    """
    loaded = [_coerce_task(item) for item in tasks]
    loaded.sort(key=lambda pair: (parse_created_at(pair[0].created_at), pair[0].id))
    store = _fresh_store(Path(db_path))
    try:
        return [
            run_one(task, path, config, policy, store, model=model, complete=complete, seed=seed)
            for task, path in loaded
        ]
    finally:
        store.close()


def main(argv: Sequence[str] | None = None, *, model: Any = None, complete: Complete | None = None) -> int:
    """Single-task CLI. Prints the RunRecord JSON. Exits 0 only when ``error`` is null."""
    parser = argparse.ArgumentParser(prog="memverify.run")
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = load_config(args.config)
        task = load_task(args.task)
        database = args.db if args.db is not None else Path(config.db_path)
        store = MemoryStore(database)
        try:
            record = run_one(
                task,
                args.task,
                config,
                args.policy,
                store,
                model=model,
                complete=complete,
                seed=args.seed,
            )
        finally:
            store.close()
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = record.to_json()
    sys.stdout.write(payload)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    return 0 if record.error is None else 1


def _coerce_task(item: Path | str | tuple[Task, Path | str]) -> tuple[Task, Path | str]:
    if isinstance(item, tuple):
        task, path = item
        return task, path
    path = Path(item)
    return load_task(path), path


def _fresh_store(path: Path) -> MemoryStore:
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.is_file():
            candidate.unlink()
    return MemoryStore(path)


if __name__ == "__main__":
    raise SystemExit(main())
