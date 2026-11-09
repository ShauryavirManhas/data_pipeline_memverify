"""Local-mode checks for the agent wrapper. No live model calls."""

from __future__ import annotations

import logging
import shlex
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from minisweagent.models.test_models import DeterministicModel, make_output

from memverify.agent import find_bash, make_checkout, remove_checkout, run_in_checkout, run_task
from memverify.config import load_config, usd
from memverify.types import load_task

ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "tasks" / "mock_mean" / "task.json"


def _config(**agent_overrides):
    config = load_config(ROOT / "configs" / "default.yaml")
    agent = replace(config.agent, **agent_overrides) if agent_overrides else config.agent
    return replace(config, agent=agent, timeout=90)


def _message(command: str, *, tokens_in: int, tokens_out: int, litellm_cost: float) -> dict:
    message = make_output(command, [{"command": command}], cost=litellm_cost)
    message["extra"]["response"] = {
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}
    }
    return message


def _bash_python(code: str) -> str:
    executable = Path(sys.executable)
    drive = executable.drive.rstrip(":").lower()
    rest = str(executable)[len(executable.drive) :].replace("\\", "/")
    quoted = shlex.quote(f"/{drive}{rest}")
    return f"{quoted} -c {shlex.quote(code)}"


class LocalAgentTest(unittest.TestCase):
    def test_config_has_prices_and_no_global_docker_image(self):
        config = load_config(ROOT / "configs" / "default.yaml")
        self.assertEqual(config.environment_kind, "local")
        self.assertEqual(config.agent.price_in_per_1m, 0.15)
        self.assertEqual(config.verifier.price_out_per_1m, 0.30)
        self.assertNotIn("docker_image", config.__dict__)

    def test_scripted_fix_on_mock_task(self):
        task = load_task(TASK_PATH)
        config = _config()
        per_call = usd(20, 10, config.agent.price_in_per_1m, config.agent.price_out_per_1m)
        code = (
            'from pathlib import Path; p = Path("mean.py"); '
            'p.write_text(p.read_text().replace(" / len(values) - 1", " / len(values)"))'
        )
        model = DeterministicModel(
            outputs=[
                _message(_bash_python(code), tokens_in=20, tokens_out=10, litellm_cost=per_call),
                _message(
                    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                    tokens_in=20,
                    tokens_out=10,
                    litellm_cost=per_call,
                ),
            ]
        )
        seen = {}

        def complete(**kwargs):
            seen["prompt"] = kwargs["messages"][0]["content"]
            return "The change returns the sum divided by the count.", 5, 7

        checkout = make_checkout(task, TASK_PATH)
        try:
            with self.assertNoLogs("memverify.agent", level=logging.WARNING):
                result = run_in_checkout(checkout, task, config, model=model, complete=complete)
            self.assertEqual(result.exit_status, "Submitted")
            self.assertIsNone(result.error)
            self.assertEqual(result.files_touched, ["mean.py"])
            self.assertIn("-    return sum(values) / len(values) - 1", result.patch)
            self.assertTrue(result.diagnosis_called)
            self.assertIn("mean([1.0, 2.0, 3.0])", seen["prompt"])
            self.assertEqual(result.agent_tokens_in, 40)
            self.assertEqual(result.agent_tokens_out, 20)
            self.assertEqual(result.diagnosis_tokens_in, 5)
            self.assertEqual(result.cost_diagnosis_usd, usd(5, 7, 0.15, 0.60))
            self.assertAlmostEqual(result.cost_agent_usd, per_call * 2)
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "test_mean.py"],
                cwd=checkout,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        finally:
            remove_checkout(checkout)
        fixture = (ROOT / "tasks" / "mock_mean" / "repo" / "mean.py").read_text(encoding="utf-8")
        self.assertIn("/ len(values) - 1", fixture)

    def test_cost_limit_uses_config_prices_not_litellm_cost(self):
        task = load_task(TASK_PATH)
        config = _config(cost_limit=0.001)
        # LiteLLM reports almost nothing. The token price is over the limit.
        self.assertGreater(usd(10_000, 10_000, 0.15, 0.60), config.agent.cost_limit)
        model = DeterministicModel(
            outputs=[
                _message("echo still-under-limit", tokens_in=10_000, tokens_out=10_000, litellm_cost=0.0),
                _message("echo should-not-run", tokens_in=10_000, tokens_out=10_000, litellm_cost=0.0),
            ]
        )

        def complete(**_kwargs):
            raise AssertionError("diagnosis must not run after the limit stops the agent")

        with self.assertLogs("memverify.agent", level=logging.WARNING) as logs:
            result = run_task(task, TASK_PATH, config, model=model, complete=complete)
        self.assertEqual(result.exit_status, "LimitsExceeded")
        self.assertEqual(result.error, "LimitsExceeded")
        self.assertEqual(model.current_index, 0)
        self.assertEqual(result.patch, "")
        self.assertFalse(result.diagnosis_called)
        self.assertEqual(result.cost_diagnosis_usd, 0.0)
        self.assertGreaterEqual(result.cost_agent_usd, config.agent.cost_limit)
        self.assertEqual(result.litellm_agent_cost, 0.0)
        self.assertTrue(any("5%" in line or "5%%" in line or "differs" in line for line in logs.output))

    def test_empty_patch_skips_diagnosis(self):
        task = load_task(TASK_PATH)
        config = _config()
        per_call = usd(4, 4, config.agent.price_in_per_1m, config.agent.price_out_per_1m)
        model = DeterministicModel(
            outputs=[
                _message(
                    "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                    tokens_in=4,
                    tokens_out=4,
                    litellm_cost=per_call,
                )
            ]
        )

        def complete(**_kwargs):
            raise AssertionError("empty patch must skip diagnosis")

        result = run_task(task, TASK_PATH, config, model=model, complete=complete)
        self.assertEqual(result.exit_status, "Submitted")
        self.assertIsNone(result.error)
        self.assertEqual(result.patch, "")
        self.assertEqual(result.files_touched, [])
        self.assertFalse(result.diagnosis_called)
        self.assertEqual(result.diagnosis, "")
        self.assertEqual(result.cost_diagnosis_usd, 0.0)

    def test_docker_mode_does_not_start_a_container(self):
        task = load_task(TASK_PATH)
        config = replace(_config(), environment_kind="docker")

        class Boom:
            def query(self, *_args, **_kwargs):
                raise AssertionError("docker mode must not query a model")

        result = run_task(task, TASK_PATH, config, model=Boom())
        self.assertEqual(result.error, "Docker mode is not implemented")
        self.assertEqual(result.patch, "")
        self.assertFalse(result.diagnosis_called)

    def test_windows_bash_is_git_bash(self):
        if sys.platform != "win32":
            self.skipTest("Git Bash preference is the Windows path")
        bash = find_bash()
        self.assertIsNotNone(bash)
        assert bash is not None
        self.assertTrue(bash.lower().endswith("bash.exe"))
        self.assertIn("git", bash.lower())


if __name__ == "__main__":
    unittest.main()
