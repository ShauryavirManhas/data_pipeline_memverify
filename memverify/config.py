"""Load the run config described in SPEC.md."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class AgentSettings:
    model_name: str
    price_in_per_1m: float
    price_out_per_1m: float
    temperature: float
    step_limit: int
    cost_limit: float
    wall_time_limit_seconds: int
    cost_tracking: str


@dataclass(frozen=True)
class VerifierSettings:
    model_name: str
    price_in_per_1m: float
    price_out_per_1m: float
    temperature: float
    max_file_chars: int
    max_file_contents_chars: int
    write_prompt: str
    read_prompt: str


@dataclass(frozen=True)
class RunConfig:
    seed: int
    num_retries: int
    agent: AgentSettings
    environment_kind: str
    timeout: int
    verifier: VerifierSettings
    diagnosis_prompt: str
    db_path: str
    hint_prefix: str


def load_config(path: Path | str) -> RunConfig:
    """Load YAML. A missing model price raises before any model call."""
    file = Path(path)
    loaded = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError(f"config must be a mapping: {file}")
    agent = _mapping(loaded, "agent")
    environment = _mapping(loaded, "environment")
    verifier = _mapping(loaded, "verifier")
    diagnosis = _mapping(loaded, "diagnosis")
    memory = _mapping(loaded, "memory")
    kind = _require_str(environment, "environment.kind")
    if kind not in {"local", "docker"}:
        raise ValueError("environment.kind must be 'local' or 'docker'")
    tracking = _require_str(agent, "agent.cost_tracking")
    if tracking not in {"default", "ignore_errors"}:
        raise ValueError("agent.cost_tracking must be 'default' or 'ignore_errors'")
    return RunConfig(
        seed=_require_int(loaded, "seed"),
        num_retries=_optional_retries(loaded),
        agent=AgentSettings(
            model_name=_require_str(agent, "agent.model_name"),
            price_in_per_1m=_require_price(agent, "agent.price_in_per_1m"),
            price_out_per_1m=_require_price(agent, "agent.price_out_per_1m"),
            temperature=_require_float(agent, "agent.temperature"),
            step_limit=_require_int(agent, "agent.step_limit"),
            cost_limit=_require_price(agent, "agent.cost_limit"),
            wall_time_limit_seconds=_require_int(agent, "agent.wall_time_limit_seconds"),
            cost_tracking=tracking,
        ),
        environment_kind=kind,
        timeout=_require_int(environment, "environment.timeout"),
        verifier=VerifierSettings(
            model_name=_require_str(verifier, "verifier.model_name"),
            price_in_per_1m=_require_price(verifier, "verifier.price_in_per_1m"),
            price_out_per_1m=_require_price(verifier, "verifier.price_out_per_1m"),
            temperature=_require_float(verifier, "verifier.temperature"),
            max_file_chars=_require_int(verifier, "verifier.max_file_chars"),
            max_file_contents_chars=_require_int(verifier, "verifier.max_file_contents_chars"),
            write_prompt=_require_str(verifier, "verifier.write_prompt"),
            read_prompt=_require_str(verifier, "verifier.read_prompt"),
        ),
        diagnosis_prompt=_require_str(diagnosis, "diagnosis.prompt"),
        db_path=_require_str(memory, "memory.db_path"),
        hint_prefix=_require_str(memory, "memory.hint_prefix"),
    )


def usd(tokens_in: int, tokens_out: int, price_in_per_1m: float, price_out_per_1m: float) -> float:
    """Dollar cost from token counts and per-million prices."""
    return (tokens_in * price_in_per_1m + tokens_out * price_out_per_1m) / 1_000_000


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"config is missing {key}")
    return value


def _require_str(data: Mapping[str, Any], name: str) -> str:
    value = _lookup(data, name)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"missing price or setting: {name}")
    return value


def _require_price(data: Mapping[str, Any], name: str) -> float:
    value = _lookup(data, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"missing price or setting: {name}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return float(value)


def _require_float(data: Mapping[str, Any], name: str) -> float:
    value = _lookup(data, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"missing price or setting: {name}")
    return float(value)


def _optional_retries(data: Mapping[str, Any]) -> int:
    """LiteLLM ``num_retries``. Omitted means 3."""
    if "num_retries" not in data or data["num_retries"] is None:
        return 3
    value = data["num_retries"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("num_retries must be an integer >= 0")
    return value


def _require_int(data: Mapping[str, Any], name: str) -> int:
    value = _lookup(data, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"missing price or setting: {name}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _lookup(data: Mapping[str, Any], name: str) -> Any:
    key = name.rsplit(".", 1)[-1]
    if key not in data or data[key] is None:
        raise ValueError(f"missing price or setting: {name}")
    return data[key]
