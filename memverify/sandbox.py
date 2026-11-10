"""Run agent shell commands inside a Modal sandbox of the task image.

The agent loop stays on the runner (Python 3.11+). Only bash commands run
in the image. Task images can ship an old Python, so memverify itself never
starts inside them.

Each command is wrapped in the image's own environment. Modal injects
variables such as ``CFLAGS=-g0`` that Docker would not, and that broke a
conan test. The same prefix the scorer uses is applied here.

Before the first command, later commits are dropped. SWE-bench-Live images
contain commits after ``base_commit``, and the agent can read the real fix
out of git if those objects stay reachable. The sandbox also has no
outbound network, so the agent cannot download the task's pull request.
The scorer uses its own sandbox and is not affected.
"""

from __future__ import annotations

import shlex
import time
from typing import Any, Callable

from memverify.config import RunConfig
from memverify.types import Task

ShellResult = dict[str, Any]
Runner = Callable[..., ShellResult]

_SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def raise_if_submitted(output: ShellResult) -> None:
    """Match mini-swe-agent: the submit command ends the run."""
    from minisweagent.exceptions import Submitted

    lines = output.get("output", "").lstrip().splitlines(keepends=True)
    if lines and lines[0].strip() == _SUBMIT and output.get("returncode") == 0:
        submission = "".join(lines[1:])
        raise Submitted(
            {
                "role": "exit",
                "content": submission,
                "extra": {"exit_status": "Submitted", "submission": submission},
            }
        )


def sandbox_create_kwargs(
    *,
    image: Any,
    app: Any,
    timeout: int,
    cpu: float,
    memory_mb: int,
) -> dict[str, Any]:
    """Arguments for the agent sandbox.

    ``block_network`` stops the model from downloading the gold pull
    request. Package indexes and GitHub are blocked with it. Scoring
    runs in a different sandbox.
    """
    return {
        "image": image,
        "app": app,
        "timeout": timeout,
        "cpu": cpu,
        "memory": memory_mb,
        "block_network": True,
    }


def image_env_assignments(env: list[str]) -> list[str]:
    """``KEY=VALUE`` list for ``env -i``, including HOME and PATH when absent."""
    keys = {item.split("=", 1)[0] for item in env}
    items = list(env)
    if "HOME" not in keys:
        items.append("HOME=/root")
    if "PATH" not in keys:
        items.append("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return items


def history_script(base_commit: str, workdir: str) -> str:
    """Detach at ``base_commit`` and delete every ref that can see a later commit."""
    base = shlex.quote(base_commit)
    folder = shlex.quote(workdir)
    return f"""
set -e
cd {folder}
git checkout --detach {base}
git reset --hard {base}
git clean -fd
git for-each-ref --format='%(refname)' refs/heads refs/tags refs/remotes | while read -r ref; do
  git update-ref -d "$ref" || true
done
rm -f .git/ORIG_HEAD .git/FETCH_HEAD .git/MERGE_HEAD .git/CHERRY_PICK_HEAD
git reflog expire --expire=now --all
git gc --prune=now --quiet
"""


def strip_future_history(run: Runner, base_commit: str, forbid: list[str], workdir: str) -> dict[str, Any]:
    """Drop commits other than ``base_commit``.

    A forbidden commit is a leak only when it is still reachable and it is
    not an ancestor of ``HEAD``. Later tasks sometimes reuse this task's
    commit, or an older one, and those have to stay. ``probes[sha]`` is
    True when the object is gone. ``future_commits_absent`` is True when
    every forbidden commit is gone or is an ancestor of ``HEAD``, False
    when any other forbidden commit remains or the prep script failed, and
    None when nothing was forbidden.
    """
    result = run(history_script(base_commit, workdir), timeout=180)
    probes: dict[str, bool] = {}
    kept_ancestors: list[str] = []
    for sha in forbid:
        probe = run(f"git cat-file -e {shlex.quote(sha)}^{{commit}}", timeout=30)
        gone = probe["returncode"] != 0
        probes[sha] = gone
        if gone:
            continue
        ancestor = run(f"git merge-base --is-ancestor {shlex.quote(sha)} HEAD", timeout=30)
        if ancestor["returncode"] == 0:
            kept_ancestors.append(sha)
    if not forbid:
        absent: bool | None = None
    elif result["returncode"] != 0:
        absent = False
    else:
        absent = all(probes[sha] or sha in kept_ancestors for sha in forbid)
    return {
        "prep_ok": result["returncode"] == 0,
        "prep_output": (result.get("output") or "")[-2000:],
        "future_commits_absent": absent,
        "probes": probes,
        "kept_ancestors": kept_ancestors,
    }


def collect_patch_from_shell(run: Runner, base_commit: str) -> tuple[str, list[str]]:
    """``git diff`` against ``base_commit``, including untracked files."""
    from memverify.agent import _retarget_new_file, files_from_patch

    tracked = run(f"git -c core.autocrlf=false diff --binary {shlex.quote(base_commit)}", timeout=120)
    if tracked["returncode"] != 0:
        detail = (tracked.get("output") or "git diff failed").strip()
        raise RuntimeError(detail[-500:])
    parts = [tracked["output"]] if tracked.get("output", "").strip() else []
    others = run("git -c core.autocrlf=false ls-files --others --exclude-standard", timeout=60)
    if others["returncode"] != 0:
        detail = (others.get("output") or "git ls-files failed").strip()
        raise RuntimeError(detail[-500:])
    for rel in others.get("output", "").splitlines():
        rel = rel.strip().replace("\\", "/")
        if not rel:
            continue
        raw = run(
            f"git -c core.autocrlf=false diff --no-index --binary -- /dev/null {shlex.quote(rel)}",
            timeout=60,
        )
        if raw["returncode"] not in {0, 1}:
            detail = (raw.get("output") or "git diff --no-index failed").strip()
            raise RuntimeError(detail[-500:])
        if raw.get("output", "").strip():
            parts.append(_retarget_new_file(raw["output"], rel))
    if not parts:
        return "", []
    patch = "\n".join(part.rstrip("\n") for part in parts) + "\n"
    if not patch.strip():
        return "", []
    return patch, files_from_patch(patch)


class ShellEnvironment:
    """mini-swe-agent environment. Subclasses run the command somewhere."""

    workdir = "/testbed"
    timeout = 30
    is_open = False
    seconds = 0.0
    prep: dict[str, Any]

    def __init__(self) -> None:
        self.prep = {}

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        # The sandbox is Linux even when the runner is a Mac. The stock
        # template tells a Darwin host to use ``sed -i ''``.
        return {
            "cwd": self.workdir,
            "timeout": self.timeout,
            "system": "Linux",
            "release": "modal",
            "version": "",
            "machine": "x86_64",
            **kwargs,
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment_type": f"{type(self).__module__}.{type(self).__name__}",
                    "cwd": self.workdir,
                    "timeout": self.timeout,
                }
            }
        }

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        output = self._run_shell(action.get("command", ""), timeout)
        raise_if_submitted(output)
        return output

    def _run_shell(self, command: str, timeout: int | None = None) -> ShellResult:
        raise NotImplementedError

    def read_file(self, rel: str) -> str | None:
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return None
        quoted = shlex.quote(rel)
        result = self._run_shell(f"if test -f {quoted}; then cat {quoted}; else exit 2; fi", timeout=60)
        if result["returncode"] != 0:
            return None
        return result.get("output") or ""

    def collect_patch(self, base_commit: str) -> tuple[str, list[str]]:
        return collect_patch_from_shell(self._run_shell, base_commit)

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False


class ModalCommandSandbox(ShellEnvironment):
    """One long-lived Modal sandbox. One task, many commands."""

    def __init__(
        self,
        task: Task,
        config: RunConfig,
        *,
        forbid_commits: list[str] | None = None,
        cpu: float = 2.0,
        memory_mb: int = 8192,
        app_name: str = "memverify-agent",
    ) -> None:
        super().__init__()
        self.task = task
        self.timeout = config.timeout
        self.wall_timeout = config.agent.wall_time_limit_seconds
        self.forbid_commits = list(forbid_commits or [])
        self.cpu = cpu
        self.memory_mb = memory_mb
        self.app_name = app_name
        self.workdir = task.workdir or "/testbed"
        self._sandbox: Any = None
        self._env_assignments: list[str] | None = None

    def open(self) -> None:
        if self.is_open:
            return
        if not self.task.docker_image:
            raise ValueError("docker mode requires Task.docker_image")
        import modal

        from memverify.scorer import docker_like_env_prefix, image_env

        try:
            self._env_assignments = image_env_assignments(image_env(self.task.docker_image))
            self.prep["env_mode"] = "image"
        except Exception as exc:
            self._env_assignments = None
            self.prep["env_mode"] = "strip"
            self.prep["env_error"] = f"{type(exc).__name__}: {exc}"
        # Keep the unused import so a strip-mode sandbox still matches the scorer.
        self._strip_prefix = docker_like_env_prefix(None)
        app = modal.App.lookup(self.app_name, create_if_missing=True)
        lifetime = max(self.wall_timeout + 1800, 7200)
        started = time.perf_counter()
        self._sandbox = modal.Sandbox.create(
            **sandbox_create_kwargs(
                image=modal.Image.from_registry(self.task.docker_image),
                app=app,
                timeout=lifetime,
                cpu=self.cpu,
                memory_mb=self.memory_mb,
            )
        )
        self.prep["network"] = "blocked"
        self.is_open = True
        self.seconds += time.perf_counter() - started
        self.prep.update(
            strip_future_history(self._run_shell, self.task.base_commit, self.forbid_commits, self.workdir)
        )

    def close(self) -> None:
        sandbox = self._sandbox
        self._sandbox = None
        self.is_open = False
        if sandbox is not None:
            sandbox.terminate()

    def _argv(self, command: str) -> list[str]:
        inner = f"cd {shlex.quote(self.workdir)} && {command}"
        if self._env_assignments is not None:
            return ["env", "-i", *self._env_assignments, "bash", "-lc", inner]
        return ["bash", "-lc", self._strip_prefix + "\n" + inner]

    def _run_shell(self, command: str, timeout: int | None = None) -> ShellResult:
        if not self.is_open or self._sandbox is None:
            raise RuntimeError("sandbox is not open")
        limit = self.timeout if timeout is None else timeout
        started = time.perf_counter()
        try:
            proc = self._sandbox.exec(*self._argv(command), timeout=limit)
            proc.wait()
            stdout = proc.stdout.read() or ""
            stderr = proc.stderr.read() or ""
            code = 0 if proc.returncode is None else int(proc.returncode)
            return {"output": stdout + stderr, "returncode": code, "exception_info": ""}
        except Exception as exc:
            return {
                "output": "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        finally:
            self.seconds += time.perf_counter() - started


def open_task_sandbox(
    task: Task,
    config: RunConfig,
    sandbox: ShellEnvironment | None,
    forbid_commits: list[str] | None,
    factory: Callable[[Task, RunConfig, list[str]], ShellEnvironment] | None = None,
) -> ShellEnvironment:
    """Return an open sandbox. The caller closes it."""
    if sandbox is not None:
        box = sandbox
    elif factory is not None:
        box = factory(task, config, list(forbid_commits or []))
    else:
        box = ModalCommandSandbox(task, config, forbid_commits=list(forbid_commits or []))
    if not box.is_open:
        box.open()
    return box
