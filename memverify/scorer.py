"""Scorer for SWE-bench-Live tasks. Sets ``RunRecord.resolved``.

Owner: Shaurya. Stage 3 of the data plan.

``score(task, patch)`` is resolved exactly when, after applying the
patch to a fresh container of the task image and running the
benchmark's own eval script:

- every test in the instance's ``FAIL_TO_PASS`` list passes, and
- every test in ``PASS_TO_PASS`` still passes.

No LLM judging. The pieces that decide the verdict come from the
official SWE-bench-Live harness (``python-only`` branch, pinned in
``requirements-eval.txt``):

- ``make_test_spec(...).eval_script``: reset the test files that the
  test patch touches to ``base_commit``, apply ``test_patch``, run the
  instance's ``test_cmds`` between the start/end markers;
- the patch-apply fallbacks ``GIT_APPLY_CMDS``;
- the instance's log parser (pytest ``-rA``) and
  ``get_eval_tests_report`` / ``get_resolution_status``.

Deviations from the harness, both on purpose:

1. Agent edits to files the ``test_patch`` touches are removed from the
   patch before it is applied. The harness would reset those files to
   ``base_commit`` anyway, so for files that already existed this changes
   nothing. For files the test patch *creates*, an agent-created copy
   would make ``git apply`` of the test patch fail and leave the agent's
   version in place; stripping avoids that. Agent edits to any other
   test file are kept and run, as in the harness. ``details`` lists what
   was dropped.
2. ``run_if_empty=True`` runs the tests on an unmodified checkout. The
   harness never runs an empty patch (it counts as unresolved). We need
   the real run for the go/no-go "empty patch fails" check.

``resolved`` is ``None`` (not ``False``) only when the scorer itself
could not run: image pull failure, container start failure, backend
exception. A failed patch apply, a test timeout or a missing test in the
log count as ``False``, as in the harness.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from unidiff import PatchSet

from swebench.harness.constants import (
    APPLY_PATCH_FAIL,
    APPLY_PATCH_PASS,
    DOCKER_WORKDIR,
    END_TEST_OUTPUT,
    FAIL_ONLY_REPOS,
    FAIL_TO_PASS,
    PASS_TO_PASS,
    RESET_FAILED,
    START_TEST_OUTPUT,
    TESTS_ERROR,
    TESTS_TIMEOUT,
    EvalType,
    ResolvedStatus,
)
from swebench.harness.grading import get_eval_tests_report, get_resolution_status
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
from swebench.harness.run_evaluation import GIT_APPLY_CMDS
from swebench.harness.test_spec.test_spec import make_test_spec

from memverify.types import RunRecord, Task

DEFAULT_NAMESPACE = "starryzhang"
DEFAULT_INSTANCES = Path("data/live/instances.jsonl")
DEFAULT_TIMEOUT = 1800  # seconds for the test command, same as the harness default
SCORER_VERSION = "1"


# --------------------------------------------------------------------------
# Instance store (hidden fields, never shown to the agent)
# --------------------------------------------------------------------------


class InstanceStore:
    """Lookup of scorer-only rows by instance id, from ``instances.jsonl``."""

    def __init__(self, rows: Mapping[str, Mapping[str, Any]]):
        self._rows = dict(rows)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "InstanceStore":
        rows: dict[str, dict[str, Any]] = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["instance_id"]] = row
        return cls(rows)

    def get(self, instance_id: str) -> dict[str, Any]:
        try:
            return dict(self._rows[instance_id])
        except KeyError:
            raise KeyError(f"instance {instance_id} is not in the scorer's instance store") from None

    def __contains__(self, instance_id: str) -> bool:
        return instance_id in self._rows


_DEFAULT_STORE: InstanceStore | None = None


def default_store() -> InstanceStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        path = Path(os.environ.get("MEMVERIFY_INSTANCES", DEFAULT_INSTANCES))
        _DEFAULT_STORE = InstanceStore.from_jsonl(path)
    return _DEFAULT_STORE


# --------------------------------------------------------------------------
# Patch handling
# --------------------------------------------------------------------------


def patch_paths(patch: str) -> list[str]:
    """Repo-relative paths a unified diff touches (source and target)."""
    out: list[str] = []
    for item in PatchSet(patch):
        for name in (item.source_file, item.target_file):
            if name and name != "/dev/null":
                rel = name[2:] if name[:2] in ("a/", "b/") else name
                if rel not in out:
                    out.append(rel)
    return out


def _split_file_diffs(patch: str) -> list[str]:
    """Split a ``git diff`` into per-file chunks, keeping bytes exact (binary-safe)."""
    chunks: list[str] = []
    current: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            chunks.append("".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _chunk_paths(chunk: str) -> set[str]:
    first = chunk.splitlines()[0] if chunk else ""
    paths: set[str] = set()
    if first.startswith("diff --git "):
        rest = first[len("diff --git ") :]
        # "a/x b/x"; paths with spaces are rare in these repos; fall back to --- / +++ below
        if " b/" in rest:
            left, right = rest.split(" b/", 1)
            paths.add(left[2:] if left.startswith("a/") else left)
            paths.add(right)
    for line in chunk.splitlines():
        if line.startswith(("--- ", "+++ ")):
            name = line[4:].split("\t")[0].strip()
            if name != "/dev/null":
                paths.add(name[2:] if name[:2] in ("a/", "b/") else name)
    return paths


def strip_test_patch_files(patch: str, test_patch: str) -> tuple[str, list[str]]:
    """Remove per-file diffs that touch any file the test patch touches."""
    if not patch.strip():
        return patch, []
    protected = set(patch_paths(test_patch)) if test_patch.strip() else set()
    kept: list[str] = []
    dropped: list[str] = []
    for chunk in _split_file_diffs(patch):
        paths = _chunk_paths(chunk)
        hit = sorted(paths & protected)
        if hit:
            dropped.extend(p for p in hit if p not in dropped)
        else:
            kept.append(chunk)
    return "".join(kept), dropped


# --------------------------------------------------------------------------
# Script building
# --------------------------------------------------------------------------


def eval_script_for(instance: Mapping[str, Any], namespace: str = DEFAULT_NAMESPACE) -> tuple[str, str]:
    """Return ``(image, eval_script)`` from the official harness."""
    spec = make_test_spec(dict(instance), namespace=namespace)
    # Not spec.instance_image_key: on an arm64 host (Apple Silicon) the harness names an
    # arm64 image, and SWE-bench-Live only publishes x86_64 images.
    from memverify.dataset import docker_image_name

    image = instance.get("docker_image") or docker_image_name(instance["instance_id"], namespace)
    return image, spec.eval_script


def build_container_script(patch: str, eval_script: str, timeout: int, workdir: str = DOCKER_WORKDIR) -> str:
    """One bash script that applies the patch the harness way, then runs the eval script.

    Payloads travel base64-encoded so no heredoc delimiter can collide
    with patch content. Output markers are the harness constants, so the
    harness log rules apply unchanged.
    """
    patch_b64 = base64.b64encode(patch.encode("utf-8", "surrogateescape")).decode("ascii")
    eval_b64 = base64.b64encode(eval_script.encode("utf-8")).decode("ascii")
    apply_lines = []
    for cmd in GIT_APPLY_CMDS:
        apply_lines.append(
            f'if [ "$APPLIED" = 0 ] && {cmd} /tmp/memverify_patch.diff; then APPLIED=1; echo {shlex.quote(APPLY_PATCH_PASS)}; fi'
        )
    return "\n".join(
        [
            "#!/bin/bash",
            # One ordered stream, like the harness's exec_run: set -x markers go to stderr,
            # test output to stdout, and grading depends on their order.
            "exec 2>&1",
            "set -uo pipefail",
            f"cd {shlex.quote(workdir)}",
            f"echo '{patch_b64}' | base64 -d > /tmp/memverify_patch.diff",
            f"echo '{eval_b64}' | base64 -d > /tmp/memverify_eval.sh",
            "APPLIED=0",
            "if [ -s /tmp/memverify_patch.diff ]; then",
            *("  " + line for line in apply_lines),
            '  if [ "$APPLIED" = 0 ]; then echo ' + shlex.quote(APPLY_PATCH_FAIL) + "; exit 0; fi",
            "else",
            "  echo '>>>>> Empty patch, running tests on the unmodified checkout'",
            "fi",
            "echo '>>>>> memverify git diff after patch:'",
            "git -c core.fileMode=false diff --stat",
            # containers are Linux (coreutils timeout); macOS has no `timeout`, so fall back
            "if command -v timeout >/dev/null 2>&1; then",
            f"  timeout {int(timeout)} bash /tmp/memverify_eval.sh",
            "elif command -v gtimeout >/dev/null 2>&1; then",
            f"  gtimeout {int(timeout)} bash /tmp/memverify_eval.sh",
            "else",
            "  bash /tmp/memverify_eval.sh",
            "fi",
            "CODE=$?",
            'if [ "$CODE" = 124 ]; then echo ' + shlex.quote(TESTS_TIMEOUT) + "; fi",
            'echo ">>>>> memverify eval exit code $CODE"',
            "exit 0",
            "",
        ]
    )


# --------------------------------------------------------------------------
# Backends: anything that can run a bash script in a fresh container of an image
# --------------------------------------------------------------------------


@dataclass
class BackendResult:
    output: str
    exit_code: int
    pull_seconds: float = 0.0
    run_seconds: float = 0.0
    where: str = ""


class Backend(Protocol):
    name: str

    def run(self, image: str, script: str, timeout: int) -> BackendResult: ...


class BackendError(RuntimeError):
    """The scorer could not run (not a test failure)."""


class DockerBackend:
    """Local Docker through the ``docker`` CLI. One throwaway container per score."""

    name = "docker"

    def __init__(self, docker: str = "docker", platform: str = "linux/amd64", pull: bool = True, remove_image: bool = False):
        self.docker = docker
        self.platform = platform
        self.pull = pull
        self.remove_image = remove_image

    def _cli(self, *args: str, timeout: int | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.docker, *args],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def run(self, image: str, script: str, timeout: int) -> BackendResult:
        if shutil.which(self.docker) is None:
            raise BackendError(f"{self.docker} is not on PATH")
        pull_seconds = 0.0
        if self.pull and self._cli("image", "inspect", image).returncode != 0:
            start = time.monotonic()
            pulled = self._cli("pull", "--platform", self.platform, image, timeout=3600)
            pull_seconds = time.monotonic() - start
            if pulled.returncode != 0:
                raise BackendError(f"docker pull {image} failed: {pulled.stderr.strip()[-500:]}")
        name = f"memverify-score-{uuid.uuid4().hex[:12]}"
        started = self._cli(
            "run", "-d", "--name", name, "--platform", self.platform, "--entrypoint", "",
            image, "tail", "-f", "/dev/null",
        )
        if started.returncode != 0:
            raise BackendError(f"docker run {image} failed: {started.stderr.strip()[-500:]}")
        try:
            start = time.monotonic()
            done = self._cli("exec", "-i", "-u", "root", name, "bash", "-s", input_text=script, timeout=timeout + 600)
            run_seconds = time.monotonic() - start
            return BackendResult(done.stdout + done.stderr, done.returncode, pull_seconds, run_seconds, where="local docker")
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"docker exec exceeded {timeout + 600}s") from exc
        finally:
            self._cli("rm", "-f", name)
            if self.remove_image:
                self._cli("image", "rm", "-f", image)


# Variables Modal sets in every sandbox that `docker exec` on the same image would not.
# Measured on conan-io__conan-17092 (2026-11-08): CFLAGS=-g0 broke a conan test.
MODAL_ADDED_ENV: dict[str, str | None] = {
    # name: value Modal sets (None = remove whatever it is)
    "CFLAGS": "-g0",
    "PYTHONPATH": "/pkg/:/root/",
    "PYTHONHASHSEED": "0",
    "SOURCE_DATE_EPOCH": None,
    "BLIS_NUM_THREADS": None,
    "MKL_NUM_THREADS": None,
    "OMP_NUM_THREADS": None,
    "OPENBLAS_NUM_THREADS": None,
    "ORT_INTER_OP_NUM_THREADS": None,
    "ORT_INTRA_OP_NUM_THREADS": None,
    "PIP_DEFAULT_TIMEOUT": None,
    "PIP_NO_CACHE_DIR": None,
    "PIP_ROOT_USER_ACTION": None,
    "UV_BREAK_SYSTEM_PACKAGES": None,
    "TERM": "xterm-mono",
}


def _registry_get(url: str, token: str | None, accept: str | None = None):
    import urllib.request

    req = urllib.request.Request(url)
    if token:
        # unredirected: blob downloads redirect to a CDN that must not get the token
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


_IMAGE_ENV_CACHE: dict[str, list[str]] = {}


def image_env(image: str) -> list[str]:
    """The image's own ``ENV`` list (``KEY=VALUE``), read from Docker Hub.

    This is the environment ``docker exec`` starts from. Raises on failure.
    """
    if image in _IMAGE_ENV_CACHE:
        return _IMAGE_ENV_CACHE[image]
    name, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "latest")
    tag = tag or "latest"
    repo = name if "/" in name else f"library/{name}"
    token = _registry_get(
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull", None
    )["token"]
    accept = ", ".join(
        [
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]
    )
    base = f"https://registry-1.docker.io/v2/{repo}"
    manifest = _registry_get(f"{base}/manifests/{tag}", token, accept)
    if "manifests" in manifest:  # multi-arch index: take linux/amd64
        pick = next(
            m for m in manifest["manifests"]
            if m.get("platform", {}).get("os") == "linux" and m.get("platform", {}).get("architecture") == "amd64"
        )
        manifest = _registry_get(f"{base}/manifests/{pick['digest']}", token, accept)
    config = _registry_get(f"{base}/blobs/{manifest['config']['digest']}", token)
    env = list(config.get("config", {}).get("Env") or [])
    _IMAGE_ENV_CACHE[image] = env
    return env


def docker_like_env_prefix(env: list[str] | None) -> str:
    """Shell lines that make the current environment match ``docker exec``.

    With the image's ENV list: start from an empty environment and set exactly
    that, plus HOME (Docker sets it for root when the image does not).
    Without it: remove the variables Modal is known to add.
    """
    if env is not None:
        keys = {item.split("=", 1)[0] for item in env}
        assignments = [shlex.quote(item) for item in env]
        if "HOME" not in keys:
            assignments.append("HOME=/root")
        if "PATH" not in keys:
            assignments.append("PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        return "exec env -i " + " ".join(assignments) + ' bash "$0" "$@"'
    lines = ['for v in $(env | sed -n "s/^\\(MODAL_[A-Za-z0-9_]*\\)=.*/\\1/p"); do unset "$v"; done']
    for key, value in MODAL_ADDED_ENV.items():
        if value is None:
            lines.append(f"unset {key}")
        else:
            lines.append(f'[ "${{{key}:-}}" = {shlex.quote(value)} ] && unset {key}')
    return "\n".join(lines)


class ModalBackend:
    """Modal Sandbox from the official image.

    Verified on Modal 2026-11-08 (conan-io__conan-17092): image loads, eval
    script runs, F2P/P2P graded. Modal adds environment variables that Docker
    would not (``CFLAGS=-g0`` broke a conan test), so the script runs with the
    image's own ENV, read from Docker Hub (``env_mode="image"``). If that
    lookup fails, known Modal additions are removed instead
    (``env_mode="strip"``). ``BackendResult.where`` records which was used.

    The upstream harness's ``--modal`` path rebuilds images from env scripts and
    does not support SWE-bench-Live rows, so we run the prebuilt image directly.
    """

    name = "modal"

    def __init__(self, app_name: str = "memverify-scorer", cpu: float = 4.0, memory_mb: int = 16384, docker_env: bool = True):
        self.app_name = app_name
        self.cpu = cpu
        self.memory_mb = memory_mb
        self.docker_env = docker_env

    def wrap(self, image: str, script: str) -> tuple[str, str]:
        """Return ``(script, env_mode)`` with the Docker-parity environment prefix."""
        if not self.docker_env:
            return script, "modal-default"
        try:
            env, mode = image_env(image), "image"
        except Exception:
            env, mode = None, "strip"
        prefix = docker_like_env_prefix(env)
        if env is not None:
            # re-exec this same file under `env -i`, guarded so it happens once
            body = f'if [ -z "${{MEMVERIFY_ENV_RESET:-}}" ]; then\n  export MEMVERIFY_ENV_RESET=1\n  {prefix}\nfi\nunset MEMVERIFY_ENV_RESET\n'
            prefix = body.replace("exec env -i ", "exec env -i MEMVERIFY_ENV_RESET=1 ")
        return prefix + "\n" + script, mode

    def run(self, image: str, script: str, timeout: int) -> BackendResult:
        try:
            import modal
        except ImportError as exc:
            raise BackendError("modal is not installed") from exc
        script, env_mode = self.wrap(image, script)
        app = modal.App.lookup(self.app_name, create_if_missing=True)
        start = time.monotonic()
        sandbox = modal.Sandbox.create(
            image=modal.Image.from_registry(image),
            app=app,
            timeout=timeout + 900,
            cpu=self.cpu,
            memory=self.memory_mb,
        )
        pull_seconds = time.monotonic() - start
        try:
            start = time.monotonic()
            encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
            proc = sandbox.exec("bash", "-c", f"echo '{encoded}' | base64 -d > /tmp/memverify_score.sh && bash /tmp/memverify_score.sh")
            proc.wait()
            output = proc.stdout.read() + proc.stderr.read()
            return BackendResult(output, proc.returncode or 0, pull_seconds, time.monotonic() - start, where=f"modal (env: {env_mode})")
        finally:
            sandbox.terminate()


class LocalCheckoutBackend:
    """Runs the script against a local git checkout instead of a container.

    For tests and fixtures only: the checkout path replaces ``/testbed``.
    The checkout is copied first so the source is never modified.
    """

    name = "local"

    def __init__(self, checkout: str | Path):
        self.checkout = Path(checkout)

    def run(self, image: str, script: str, timeout: int) -> BackendResult:
        tmp = Path(tempfile.mkdtemp(prefix="memverify-score-"))
        work = tmp / "testbed"
        try:
            shutil.copytree(self.checkout, work, symlinks=True)
            local_script = script.replace(DOCKER_WORKDIR, str(work))
            # the eval script travels base64-encoded, so patch it there too
            local_script = _rewrite_b64_eval(local_script, DOCKER_WORKDIR, str(work))
            start = time.monotonic()
            done = subprocess.run(
                ["bash", "-s"], input=local_script, capture_output=True, text=True,
                timeout=timeout + 60, cwd=work, env={**os.environ, "HOME": str(tmp)},
            )
            return BackendResult(done.stdout + done.stderr, done.returncode, 0.0, time.monotonic() - start, where="local checkout")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _rewrite_b64_eval(script: str, old: str, new: str) -> str:
    out = []
    for line in script.splitlines():
        if line.startswith("echo '") and "/tmp/memverify_eval.sh" in line:
            payload = line.split("'")[1]
            decoded = base64.b64decode(payload).decode("utf-8").replace(old, new)
            line = f"echo '{base64.b64encode(decoded.encode()).decode()}' | base64 -d > /tmp/memverify_eval.sh"
        out.append(line)
    return "\n".join(out) + "\n"


def make_backend(name: str | None = None) -> Backend:
    name = name or os.environ.get("MEMVERIFY_SCORER_BACKEND", "docker")
    if name == "docker":
        return DockerBackend()
    if name == "modal":
        return ModalBackend()
    if name.startswith("local:"):  # tests and dry runs: a local git checkout stands in for /testbed
        return LocalCheckoutBackend(name.split(":", 1)[1])
    raise ValueError(f"unknown scorer backend {name!r} (docker, modal, or local:<checkout>)")


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def _parser_for(instance: Mapping[str, Any]) -> Callable[[str, Any], dict[str, str]]:
    spec = make_test_spec(dict(instance), namespace=DEFAULT_NAMESPACE)
    if spec.log_parser is not None:
        return spec.log_parser
    return MAP_REPO_TO_PARSER[instance["repo"]]


def grade_log(instance: Mapping[str, Any], log: str) -> dict[str, Any]:
    """Grade a container log with the harness rules. Pure function, no I/O.

    Returns ``resolved`` (bool), ``patch_applied``, ``tests_ran``,
    ``timed_out`` and the four F2P/P2P success/failure lists.
    """
    f2p = list(instance["FAIL_TO_PASS"])
    p2p = list(instance["PASS_TO_PASS"])
    out: dict[str, Any] = {
        "resolved": False,
        "patch_applied": APPLY_PATCH_FAIL not in log,
        "tests_ran": False,
        "timed_out": TESTS_TIMEOUT in log,
        "resolution_status": ResolvedStatus.NO.value,
        "FAIL_TO_PASS": {"success": [], "failure": list(f2p)},
        "PASS_TO_PASS": {"success": [], "failure": list(p2p)},
    }
    bad = [code for code in (APPLY_PATCH_FAIL, RESET_FAILED, TESTS_ERROR, TESTS_TIMEOUT) if code in log]
    if bad or START_TEST_OUTPUT not in log or END_TEST_OUTPUT not in log:
        out["log_problem"] = bad or ["test output markers not found"]
        return out
    # The eval script echoes ": '>>>>> Start Test Output'" under set -x; split like the harness.
    section = log.split(START_TEST_OUTPUT)[1].split(END_TEST_OUTPUT)[0]
    status_map = _parser_for(instance)(section, None)
    out["tests_ran"] = True
    out["n_tests_parsed"] = len(status_map)
    if not status_map:
        # Harness rule still applies (every listed test counts as failed), but say why:
        # usually the test command itself failed (missing runner, bad path).
        out["log_problem"] = ["tests ran but no test results were parsed"]
    eval_type = EvalType.FAIL_ONLY if instance["repo"] in FAIL_ONLY_REPOS else EvalType.PASS_AND_FAIL
    report = get_eval_tests_report(
        status_map,
        {"instance_id": instance["instance_id"], FAIL_TO_PASS: f2p, PASS_TO_PASS: p2p},
        eval_type=eval_type,
    )
    status = get_resolution_status(report)
    out["resolution_status"] = status
    out["resolved"] = status == ResolvedStatus.FULL.value
    out["FAIL_TO_PASS"] = report[FAIL_TO_PASS]
    out["PASS_TO_PASS"] = report[PASS_TO_PASS]
    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


@dataclass
class ScoreResult:
    resolved: bool | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"resolved": self.resolved, "details": self.details}


def score(
    task: Task,
    patch: str,
    *,
    instances: InstanceStore | None = None,
    backend: Backend | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    log_dir: str | Path | None = None,
    run_if_empty: bool = False,
    namespace: str = DEFAULT_NAMESPACE,
) -> ScoreResult:
    """Score one patch for one task. See the module docstring for the definition."""
    instance = (instances or default_store()).get(task.id)
    if instance["base_commit"] != task.base_commit:
        raise ValueError(f"{task.id}: task base_commit does not match the instance store")
    details: dict[str, Any] = {
        "instance_id": task.id,
        "scorer_version": SCORER_VERSION,
        "n_fail_to_pass": len(instance["FAIL_TO_PASS"]),
        "n_pass_to_pass": len(instance["PASS_TO_PASS"]),
    }
    applied_patch, dropped = strip_test_patch_files(patch or "", instance.get("test_patch", ""))
    details["dropped_test_files"] = dropped
    details["empty_patch"] = not applied_patch.strip()
    if details["empty_patch"] and not run_if_empty:
        details["reason"] = "empty patch (after removing test-patch files)" if dropped else "empty patch"
        return ScoreResult(False, details)

    image, eval_script = eval_script_for(instance, namespace)
    details["image"] = image
    script = build_container_script(applied_patch if applied_patch.strip() else "", eval_script, timeout)
    backend = backend or make_backend()
    details["backend"] = backend.name
    start = time.monotonic()
    try:
        result = backend.run(image, script, timeout)
    except Exception as exc:  # infrastructure, not a test outcome
        details["error"] = f"{type(exc).__name__}: {exc}"
        details["seconds_total"] = round(time.monotonic() - start, 2)
        return ScoreResult(None, details)
    details["seconds_total"] = round(time.monotonic() - start, 2)
    details["seconds_pull"] = round(result.pull_seconds, 2)
    details["seconds_run"] = round(result.run_seconds, 2)
    details["where"] = result.where
    graded = grade_log(instance, result.output)
    details.update({k: v for k, v in graded.items() if k != "resolved"})
    if log_dir is not None:
        path = Path(log_dir) / task.id
        path.mkdir(parents=True, exist_ok=True)
        (path / "test_output.txt").write_text(result.output, encoding="utf-8")
        (path / "patch.diff").write_text(applied_patch, encoding="utf-8")
        (path / "report.json").write_text(json.dumps({"resolved": graded["resolved"], **details}, indent=2), encoding="utf-8")
        details["log_path"] = str(path / "test_output.txt")
    return ScoreResult(graded["resolved"], details)


def score_record(record: RunRecord, task: Task, **kwargs: Any) -> tuple[RunRecord, ScoreResult]:
    """Return a copy of ``record`` with ``resolved`` set, plus the score details.

    If the scorer could not run, ``resolved`` stays ``None`` so the runner can retry.
    """
    if record.task_id != task.id:
        raise ValueError(f"record is for {record.task_id}, task is {task.id}")
    result = score(task, record.patch, **kwargs)
    return dataclasses.replace(record, resolved=result.resolved), result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a patch (or a RunRecord) on a SWE-bench-Live task")
    parser.add_argument("--task", required=True, help="Task JSON file")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", help="RunRecord JSON; writes it back with resolved set (see --out)")
    group.add_argument("--patch-file", help="unified diff to score")
    group.add_argument("--gold", action="store_true", help="score the gold patch")
    group.add_argument("--empty", action="store_true", help="run the tests with no patch")
    parser.add_argument("--instances", default=None, help="instances.jsonl (default $MEMVERIFY_INSTANCES or data/live/instances.jsonl)")
    parser.add_argument("--backend", default=None, choices=["docker", "modal"])
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    task = Task.from_json(Path(args.task).read_text(encoding="utf-8"))
    store = InstanceStore.from_jsonl(args.instances) if args.instances else default_store()
    kwargs = dict(instances=store, backend=make_backend(args.backend), timeout=args.timeout, log_dir=args.log_dir)
    if args.record:
        record = RunRecord.from_json(Path(args.record).read_text(encoding="utf-8"))
        record, result = score_record(record, task, **kwargs)
        text = record.to_json()
    else:
        if args.gold:
            patch = store.get(task.id)["patch"]
        elif args.empty:
            patch = ""
        else:
            patch = Path(args.patch_file).read_text(encoding="utf-8", errors="surrogateescape")
        result = score(task, patch, run_if_empty=args.empty, **kwargs)
        text = json.dumps(result.to_dict(), indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result.resolved is not None else 2


if __name__ == "__main__":
    sys.exit(main())
