"""mini-swe-agent wrapper for one local task checkout.

Docker mode is a stub. The live model is LiteLLM; tests pass their own
model and do not construct ``LitellmModel``. The cost limit uses token
counts and the config prices. LiteLLM's ``agent.cost`` is only compared
afterwards.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from memverify.config import RunConfig, usd
from memverify.types import Task, resolve_workdir

logger = logging.getLogger("memverify.agent")

Complete = Callable[..., tuple[str, int, int]]


class ModelCallFailed(Exception):
    """A model call failed after LiteLLM's retries."""


@dataclass
class AgentResult:
    """Outcome of one agent attempt. Not a logged record.

    Dollar amounts here are the agent and the diagnosis call. The
    verifier is applied by a later stage. ``litellm_agent_cost`` is the
    sum of the costs LiteLLM reported on agent messages.
    """

    exit_status: str
    submission: str
    patch: str
    files_touched: list[str]
    diagnosis: str
    diagnosis_called: bool
    agent_tokens_in: int
    agent_tokens_out: int
    diagnosis_tokens_in: int
    diagnosis_tokens_out: int
    cost_agent_usd: float
    cost_diagnosis_usd: float
    litellm_agent_cost: float
    error: str | None
    wall_seconds: float
    model_call_failed: bool = False

    @property
    def tokens_in(self) -> int:
        return self.agent_tokens_in + self.diagnosis_tokens_in

    @property
    def tokens_out(self) -> int:
        return self.agent_tokens_out + self.diagnosis_tokens_out


def find_bash() -> str | None:
    """Prefer Git Bash on Windows. PATH ``bash`` is often the WSL launcher."""
    git = shutil.which("git")
    if git:
        git_path = Path(git).resolve()
        candidates = [
            git_path.parent.parent / "bin" / "bash.exe",
            git_path.parent / "bash.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


def make_checkout(task: Task, task_path: Path | str) -> Path:
    """Copy the task workdir to a temp git checkout at ``base_commit``."""
    source = resolve_workdir(task, task_path)
    if not (source / ".git").exists():
        raise FileNotFoundError(f"workdir is not a git checkout: {source}")
    parent = Path(tempfile.mkdtemp(prefix="memverify-"))
    dest = parent / "checkout"
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _git(dest, "reset", "--hard", task.base_commit)
    _git(dest, "clean", "-fd")
    return dest


def remove_checkout(checkout: Path) -> None:
    parent = checkout.parent
    if parent.name.startswith("memverify-"):
        shutil.rmtree(parent, ignore_errors=True)


def collect_patch(repo: Path, base_commit: str) -> tuple[str, list[str]]:
    """Worktree diff against ``base_commit``, including untracked files."""
    tracked = _git(repo, "diff", "--binary", base_commit)
    parts = [tracked] if tracked.strip() else []
    others = _git(repo, "ls-files", "--others", "--exclude-standard")
    for rel in others.splitlines():
        rel = rel.strip().replace("\\", "/")
        if not rel:
            continue
        raw = _git(repo, "diff", "--no-index", "--binary", "--", os.devnull, rel, ok={0, 1})
        if raw.strip():
            parts.append(_retarget_new_file(raw, rel))
    if not parts:
        return "", []
    patch = "\n".join(part.rstrip("\n") for part in parts) + "\n"
    if not patch.strip():
        return "", []
    return patch, files_from_patch(patch)


def files_from_patch(patch: str) -> list[str]:
    found: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        left, right = parts[-2], parts[-1]
        chosen = right[2:] if right.startswith("b/") else right
        if chosen in {"/dev/null", "dev/null"} and left.startswith("a/"):
            chosen = left[2:]
        chosen = chosen.replace("\\", "/")
        if chosen and chosen not in found:
            found.append(chosen)
    return found


def run_task(
    task: Task,
    task_path: Path | str,
    config: RunConfig,
    *,
    model: Any = None,
    complete: Complete | None = None,
    task_prompt: str | None = None,
    diagnose: bool = True,
) -> AgentResult:
    """Copy the workdir, run the agent, and remove the copy."""
    if config.environment_kind == "docker":
        return _docker_stub()
    started = time.perf_counter()
    checkout = make_checkout(task, task_path)
    try:
        result = run_in_checkout(
            checkout,
            task,
            config,
            model=model,
            complete=complete,
            task_prompt=task_prompt,
            diagnose=diagnose,
        )
        result.wall_seconds = time.perf_counter() - started
        return result
    finally:
        remove_checkout(checkout)


def run_in_checkout(
    checkout: Path,
    task: Task,
    config: RunConfig,
    *,
    model: Any = None,
    complete: Complete | None = None,
    task_prompt: str | None = None,
    diagnose: bool = True,
) -> AgentResult:
    """Run in an existing checkout. The caller owns that directory."""
    if config.environment_kind == "docker":
        return _docker_stub()
    started = time.perf_counter()
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.exceptions import FormatError, LimitsExceeded, TimeExceeded

    class PricedAgent(DefaultAgent):
        """Stops on config-priced cost. Leaves LiteLLM cost on ``self.cost``."""

        def __init__(self, model: Any, env: Any, **kwargs: Any):
            super().__init__(model, env, **kwargs)
            self.price_in_per_1m = config.agent.price_in_per_1m
            self.price_out_per_1m = config.agent.price_out_per_1m
            self.price_limit = config.agent.cost_limit
            self.priced_cost = 0.0
            self.litellm_cost = 0.0
            self.tokens_in = 0
            self.tokens_out = 0

        def query(self) -> dict:
            if 0 < self.config.step_limit <= self.n_calls or 0 < self.price_limit <= self.priced_cost:
                raise LimitsExceeded(
                    {
                        "role": "exit",
                        "content": "LimitsExceeded",
                        "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                    }
                )
            if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
                raise TimeExceeded(
                    {
                        "role": "exit",
                        "content": "TimeExceeded",
                        "extra": {"exit_status": "TimeExceeded", "submission": ""},
                    }
                )
            self.n_calls += 1
            try:
                message = self.model.query(self.messages)
            except FormatError as exc:
                for item in exc.messages:
                    self._charge(item, update_agent_cost=False)
                raise
            self._charge(message, update_agent_cost=True)
            self.add_messages(message)
            return message

        def _charge(self, message: dict, *, update_agent_cost: bool) -> None:
            extra = message.get("extra") or {}
            litellm_cost = float(extra.get("cost") or 0.0)
            tokens_in, tokens_out = _token_counts(message)
            self.litellm_cost += litellm_cost
            self.priced_cost += usd(
                tokens_in,
                tokens_out,
                self.price_in_per_1m,
                self.price_out_per_1m,
            )
            self.tokens_in += tokens_in
            self.tokens_out += tokens_out
            if update_agent_cost:
                self.cost = self.litellm_cost

    env = _environment(checkout, config.timeout)
    system_template, instance_template = stock_templates()
    agent = PricedAgent(
        model if model is not None else build_litellm_model(config),
        env,
        system_template=system_template,
        instance_template=instance_template,
        step_limit=config.agent.step_limit,
        cost_limit=0,
        wall_time_limit_seconds=config.agent.wall_time_limit_seconds,
        output_path=None,
    )
    prompt = task.issue_text if task_prompt is None else task_prompt
    error: str | None
    model_call_failed = False
    try:
        info = agent.run(prompt)
    except Exception as exc:
        last = agent.messages[-1] if agent.messages else {}
        extra = last.get("extra") or {}
        info = {
            "exit_status": extra.get("exit_status") or type(exc).__name__,
            "submission": extra.get("submission") or "",
        }
        error = str(exc) if isinstance(exc, ModelCallFailed) else f"{type(exc).__name__}: {exc}"
        model_call_failed = True
    else:
        status = str(info.get("exit_status") or "")
        error = None if status == "Submitted" else (status or "agent failed")
    patch, touched = collect_patch(checkout, task.base_commit)
    if diagnose and not model_call_failed:
        try:
            diagnosis, called, diagnosis_in, diagnosis_out, diagnosis_cost = _diagnose(
                task,
                config,
                patch,
                complete,
            )
        except ModelCallFailed as exc:
            diagnosis, called, diagnosis_in, diagnosis_out, diagnosis_cost = "", False, 0, 0, 0.0
            error = str(exc)
            model_call_failed = True
    else:
        diagnosis, called, diagnosis_in, diagnosis_out, diagnosis_cost = "", False, 0, 0, 0.0
    _warn_if_cost_disagrees(agent.litellm_cost, agent.priced_cost)
    return AgentResult(
        exit_status=str(info.get("exit_status") or ""),
        submission=str(info.get("submission") or ""),
        patch=patch,
        files_touched=touched,
        diagnosis=diagnosis,
        diagnosis_called=called,
        agent_tokens_in=agent.tokens_in,
        agent_tokens_out=agent.tokens_out,
        diagnosis_tokens_in=diagnosis_in,
        diagnosis_tokens_out=diagnosis_out,
        cost_agent_usd=agent.priced_cost,
        cost_diagnosis_usd=diagnosis_cost,
        litellm_agent_cost=agent.litellm_cost,
        error=error,
        wall_seconds=time.perf_counter() - started,
        model_call_failed=model_call_failed,
    )


def agent_litellm_kwargs(config: RunConfig) -> dict[str, Any]:
    """Arguments for ``LitellmModel``. Tests check these and do not construct the model."""
    return {
        "model_name": config.agent.model_name,
        "model_kwargs": {
            "temperature": config.agent.temperature,
            "seed": config.seed,
            "num_retries": config.num_retries,
        },
        "cost_tracking": config.agent.cost_tracking,
    }


def build_litellm_model(config: RunConfig) -> Any:
    """Real agent model. Tests do not call this.

    mini-swe-agent retries ``query`` on its own (default 10 attempts).
    This model forces that outer loop to one attempt. ``num_retries``
    on the LiteLLM call is the retry budget.
    """
    from minisweagent.models.litellm_model import LitellmModel

    class _NumRetriesModel(LitellmModel):
        def query(self, messages: list[dict[str, str]], **kwargs: Any) -> dict:
            previous = os.environ.get("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT")
            os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
            try:
                return super().query(messages, **kwargs)
            finally:
                if previous is None:
                    os.environ.pop("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", None)
                else:
                    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = previous

    return _NumRetriesModel(**agent_litellm_kwargs(config))


def litellm_complete(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    seed: int,
    num_retries: int = 3,
) -> tuple[str, int, int]:
    """One completion. Returns text, prompt tokens, completion tokens."""
    import litellm

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=temperature,
            seed=seed,
            num_retries=num_retries,
        )
    except Exception as exc:
        raise ModelCallFailed(f"{type(exc).__name__}: {exc}") from exc
    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    return text, tokens_in, tokens_out


def stock_templates() -> tuple[str, str]:
    """System and instance templates shipped with mini-swe-agent."""
    import minisweagent

    path = Path(minisweagent.__file__).parent / "config" / "mini.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"mini-swe-agent templates not found at {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    agent = loaded["agent"]
    return agent["system_template"], agent["instance_template"]


def _environment(checkout: Path, timeout: int) -> Any:
    from minisweagent.environments.local import LocalEnvironment

    if os.name != "nt":
        return LocalEnvironment(cwd=str(checkout), timeout=timeout)

    class BashLocalEnvironment(LocalEnvironment):
        """Run each command with ``bash -lc`` so the stock prompt works on Windows."""

        def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
            bash = find_bash()
            if not bash:
                return super().execute(action, cwd, timeout=timeout)
            command = action.get("command", "")
            workdir = cwd or self.config.cwd or os.getcwd()
            limit = self.config.timeout if timeout is None else timeout
            try:
                completed = subprocess.run(
                    [bash, "-lc", command],
                    cwd=workdir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=limit,
                )
                output = {
                    "output": completed.stdout or "",
                    "returncode": completed.returncode,
                    "exception_info": "",
                }
            except Exception as exc:
                raw = getattr(exc, "stdout", None) or getattr(exc, "output", None) or ""
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                output = {
                    "output": raw or "",
                    "returncode": -1,
                    "exception_info": f"An error occurred while executing the command: {exc}",
                    "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
                }
            self._check_finished(output)
            return output

    return BashLocalEnvironment(cwd=str(checkout), timeout=timeout)


def _diagnose(
    task: Task,
    config: RunConfig,
    patch: str,
    complete: Complete | None,
) -> tuple[str, bool, int, int, float]:
    if not patch.strip():
        return "", False, 0, 0, 0.0
    caller = complete or litellm_complete
    prompt = config.diagnosis_prompt.replace("{issue_text}", task.issue_text).replace("{patch}", patch)
    text, tokens_in, tokens_out = call_model(
        caller,
        model=config.agent.model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=config.agent.temperature,
        seed=config.seed,
        num_retries=config.num_retries,
    )
    cost = usd(tokens_in, tokens_out, config.agent.price_in_per_1m, config.agent.price_out_per_1m)
    return text, True, tokens_in, tokens_out, cost


def call_model(caller: Complete, **kwargs: Any) -> tuple[str, int, int]:
    """Call a completion. A raised exception becomes ``ModelCallFailed``."""
    try:
        return caller(**kwargs)
    except ModelCallFailed:
        raise
    except Exception as exc:
        raise ModelCallFailed(f"{type(exc).__name__}: {exc}") from exc


def _warn_if_cost_disagrees(litellm_cost: float, priced_cost: float) -> None:
    scale = max(litellm_cost, priced_cost)
    if scale == 0:
        return
    if abs(litellm_cost - priced_cost) > 0.05 * scale:
        logger.warning(
            "LiteLLM agent cost %.6g differs from config-priced cost %.6g by more than 5%%",
            litellm_cost,
            priced_cost,
        )


def _token_counts(message: dict) -> tuple[int, int]:
    extra = message.get("extra") or {}
    usage = extra.get("usage")
    if not isinstance(usage, dict):
        response = extra.get("response") or {}
        usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    return _usage_int(usage, "prompt_tokens", "input_tokens"), _usage_int(
        usage, "completion_tokens", "output_tokens"
    )


def _usage_int(usage: dict, *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < 0:
            continue
        return value
    return 0


def _docker_stub() -> AgentResult:
    return AgentResult(
        exit_status="DockerNotImplemented",
        submission="",
        patch="",
        files_touched=[],
        diagnosis="",
        diagnosis_called=False,
        agent_tokens_in=0,
        agent_tokens_out=0,
        diagnosis_tokens_in=0,
        diagnosis_tokens_out=0,
        cost_agent_usd=0.0,
        cost_diagnosis_usd=0.0,
        litellm_agent_cost=0.0,
        error="Docker mode is not implemented",
        wall_seconds=0.0,
    )


def _git(repo: Path, *args: str, ok: set[int] | None = None) -> str:
    allowed = {0} if ok is None else ok
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode not in allowed:
        detail = proc.stderr.strip() or f"git {' '.join(args)} failed"
        raise RuntimeError(detail)
    return proc.stdout


def _retarget_new_file(raw: str, rel: str) -> str:
    lines = []
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            lines.append(f"diff --git a/{rel} b/{rel}")
        elif line.startswith("--- "):
            lines.append("--- /dev/null")
        elif line.startswith("+++ "):
            lines.append(f"+++ b/{rel}")
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")
