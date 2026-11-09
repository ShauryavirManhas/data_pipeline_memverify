"""JSON-serializable records for a memverify run.

The fields on Task, MemoryEntry, and RunRecord are the experiment
contract in SPEC.md. Serialization writes every field, and loading
rejects a missing or unknown field.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

POLICIES: tuple[str, ...] = (
    "none",
    "memory_no_check",
    "write_check",
    "read_check",
)
VERIFIER_DECISIONS: tuple[str, ...] = ("yes", "no")


def parse_created_at(value: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp.

    ``Z`` is accepted as UTC. Naive timestamps are rejected so that
    "strictly earlier" has one ordering.
    """
    if not isinstance(value, str) or value == "":
        raise ValueError("created_at must be a non-empty ISO-8601 string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"created_at is not ISO-8601: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"created_at must include a timezone: {value}")
    return parsed


def is_strictly_earlier(created_at: str, than: str) -> bool:
    """Return whether ``created_at`` is strictly before ``than``."""
    return parse_created_at(created_at) < parse_created_at(than)


def resolve_workdir(task: Task, task_path: Path | str) -> Path:
    """Resolve ``task.workdir`` against the directory that holds the task file.

    Absolute paths are returned unchanged. Relative paths are not
    resolved against the process working directory.
    """
    path = Path(task.workdir)
    if path.is_absolute():
        return path
    return (Path(task_path).resolve().parent / path).resolve()


def _require_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{name} must be a non-empty string")


def _require_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a bool")


def _require_count(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int")


def _require_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return float(value)


def _from_mapping(cls: type, data: Mapping[str, Any]) -> Any:
    if not isinstance(data, Mapping):
        raise TypeError(f"{cls.__name__} expected a JSON object, got {type(data).__name__}")
    known = {item.name for item in fields(cls)}
    extra = sorted(set(data) - known)
    missing = sorted(known - set(data))
    if extra or missing:
        raise ValueError(f"{cls.__name__} fields do not match: missing={missing} extra={extra}")
    return cls(**dict(data))


class JsonRecord:
    """Shared JSON helpers. Not a record of its own."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        return _from_mapping(cls, data)

    @classmethod
    def from_json(cls, text: str):
        loaded = json.loads(text)
        return cls.from_dict(loaded)


@dataclass
class Task(JsonRecord):
    """One SWE-bench-style issue pinned to a git checkout.

    ``docker_image`` is null when the task runs on the local checkout.
    """

    id: str
    repo: str
    issue_text: str
    base_commit: str
    created_at: str
    workdir: str
    docker_image: str | None

    def __post_init__(self) -> None:
        for name in ("id", "repo", "issue_text", "base_commit", "created_at", "workdir"):
            _require_str(name, getattr(self, name))
        if self.docker_image is not None:
            _require_str("docker_image", self.docker_image)
        parse_created_at(self.created_at)


@dataclass
class MemoryEntry(JsonRecord):
    """A fix remembered from an earlier task in the same repo.

    ``created_at`` is copied from the source task. It is the dataset
    time used for the "strictly earlier" filter, not the wall clock of
    the run that produced the entry. ``diagnosis`` and ``patch`` may
    be empty strings. ``files_touched`` may be an empty list.
    """

    id: str
    repo: str
    source_task_id: str
    created_at: str
    base_commit: str
    issue_text: str
    files_touched: list[str]
    diagnosis: str
    patch: str

    def __post_init__(self) -> None:
        for name in ("id", "repo", "source_task_id", "created_at", "base_commit", "issue_text"):
            _require_str(name, getattr(self, name))
        parse_created_at(self.created_at)
        if not isinstance(self.files_touched, list) or any(
            not isinstance(item, str) or item == "" for item in self.files_touched
        ):
            raise ValueError("files_touched must be a list of non-empty strings")
        if not isinstance(self.diagnosis, str):
            raise ValueError("diagnosis must be a string")
        if not isinstance(self.patch, str):
            raise ValueError("patch must be a string")


@dataclass
class RunRecord(JsonRecord):
    """One task execution under one memory policy.

    ``resolved`` is left null by the agent. A separate scorer fills it
    in later. The agent never reads hidden test results, so it cannot
    set this field from a test run.

    ``cost_usd`` is the sum of ``cost_agent_usd``,
    ``cost_diagnosis_usd``, and ``cost_verifier_usd``.
    """

    task_id: str
    policy: str
    seed: int
    patch: str
    resolved: bool | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cost_agent_usd: float
    cost_diagnosis_usd: float
    cost_verifier_usd: float
    hint_id: str | None
    hint_injected: bool
    entry_saved: bool
    verifier_decision: str | None
    verifier_reason: str | None
    wall_seconds: float
    error: str | None

    def __post_init__(self) -> None:
        _require_str("task_id", self.task_id)
        if self.policy not in POLICIES:
            raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
        _require_count("seed", self.seed)
        if not isinstance(self.patch, str):
            raise ValueError("patch must be a string")
        if not isinstance(self.resolved, bool) and self.resolved is not None:
            raise ValueError("resolved must be a bool or null")
        _require_count("tokens_in", self.tokens_in)
        _require_count("tokens_out", self.tokens_out)
        self.cost_usd = _require_number("cost_usd", self.cost_usd)
        self.cost_agent_usd = _require_number("cost_agent_usd", self.cost_agent_usd)
        self.cost_diagnosis_usd = _require_number("cost_diagnosis_usd", self.cost_diagnosis_usd)
        self.cost_verifier_usd = _require_number("cost_verifier_usd", self.cost_verifier_usd)
        parts = self.cost_agent_usd + self.cost_diagnosis_usd + self.cost_verifier_usd
        if abs(self.cost_usd - parts) > 1e-9:
            raise ValueError(
                "cost_usd must equal cost_agent_usd + cost_diagnosis_usd + cost_verifier_usd"
            )
        if self.hint_id is not None:
            _require_str("hint_id", self.hint_id)
        _require_bool("hint_injected", self.hint_injected)
        _require_bool("entry_saved", self.entry_saved)
        decision_set = self.verifier_decision is not None
        reason_set = self.verifier_reason is not None
        if decision_set != reason_set:
            raise ValueError("verifier_decision and verifier_reason must both be set or both be null")
        if self.verifier_decision is not None and self.verifier_decision not in VERIFIER_DECISIONS:
            raise ValueError("verifier_decision must be 'yes', 'no', or null")
        if self.verifier_reason is not None and not isinstance(self.verifier_reason, str):
            raise ValueError("verifier_reason must be a string or null")
        self.wall_seconds = _require_number("wall_seconds", self.wall_seconds)
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("error must be a string or null")


def load_task(path: Path | str) -> Task:
    """Load a task JSON file. Does not resolve ``workdir``."""
    return Task.from_json(Path(path).read_text(encoding="utf-8"))
