"""Policy, stream, and CLI tests. The model and the completion are fakes."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from minisweagent.models.test_models import DeterministicModel, make_output

from memverify.agent import agent_litellm_kwargs
from memverify.config import load_config, usd
from memverify.memory import MemoryStore
from memverify.run import main, run_one, run_stream
from memverify.types import MemoryEntry, load_task

ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "tasks" / "mock_mean" / "task.json"
TASK2_PATH = ROOT / "tasks" / "mock_mean_2" / "task.json"
CONFIG_PATH = ROOT / "configs" / "default.yaml"


def _config():
    return replace(load_config(CONFIG_PATH), timeout=90)


def _message(command: str, tokens_in: int, tokens_out: int, litellm_cost: float) -> dict:
    item = make_output(command, [{"command": command}], cost=litellm_cost)
    item["extra"]["response"] = {"usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}}
    return item


def _bash_python(code: str) -> str:
    executable = Path(sys.executable)
    drive = executable.drive.rstrip(":").lower()
    rest = str(executable)[len(executable.drive) :].replace("\\", "/")
    quoted = shlex.quote(f"/{drive}{rest}")
    return f"{quoted} -c {shlex.quote(code)}"


def _fix_command() -> str:
    code = (
        "from pathlib import Path; p = Path('mean.py'); t = p.read_text(); "
        "t = t.replace(' / len(values) - 1', ' / len(values)'); "
        "t = t.replace(' // len(values)', ' / len(values)'); "
        "p.write_text(t)"
    )
    return _bash_python(code)


class RecordingModel(DeterministicModel):
    def __init__(self, outputs: list[dict]):
        super().__init__(outputs=outputs)
        self.prompts: list[str] = []

    def query(self, messages, **kwargs):
        self.prompts.append("\n".join(str(item.get("content") or "") for item in messages))
        return super().query(messages, **kwargs)


class BoomModel(DeterministicModel):
    def __init__(self):
        super().__init__(outputs=[])

    def query(self, messages, **kwargs):
        raise RuntimeError("rate limited")


def _fixing_model(cfg) -> RecordingModel:
    per = usd(20, 10, cfg.agent.price_in_per_1m, cfg.agent.price_out_per_1m)
    return RecordingModel(
        [
            _message(_fix_command(), 20, 10, per),
            _message("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", 20, 10, per),
        ]
    )


def _empty_model(cfg) -> RecordingModel:
    per = usd(4, 4, cfg.agent.price_in_per_1m, cfg.agent.price_out_per_1m)
    return RecordingModel([_message("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", 4, 4, per)])


def _four_step_model(cfg) -> RecordingModel:
    per = usd(20, 10, cfg.agent.price_in_per_1m, cfg.agent.price_out_per_1m)
    step = [
        _message(_fix_command(), 20, 10, per),
        _message("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", 20, 10, per),
    ]
    return RecordingModel(step + step)


def _entry(id_: str, created: str, issue: str, repo: str = "mock-mean") -> MemoryEntry:
    return MemoryEntry(
        id=id_,
        repo=repo,
        source_task_id=id_,
        created_at=created,
        base_commit="f64607f434063ab8e788510a8ff6776e121eae53",
        issue_text=issue,
        files_touched=["mean.py"],
        diagnosis="divide by the count",
        patch="diff --git a/mean.py b/mean.py\n@@ -5 +5 @@\n-    return sum(values) / len(values) - 1\n+    return sum(values) / len(values)\n",
    )


def _rows(store: MemoryStore, repo: str = "mock-mean") -> list[MemoryEntry]:
    return store.candidates(repo, "2099-01-01T00:00:00Z")


class PolicyTest(unittest.TestCase):
    def test_agent_kwargs_carry_num_retries_without_building_litellm(self):
        kwargs = agent_litellm_kwargs(_config())
        self.assertEqual(kwargs["model_kwargs"]["num_retries"], 3)
        self.assertEqual(kwargs["model_name"], _config().agent.model_name)

    def test_omitted_num_retries_defaults_to_three(self):
        text = CONFIG_PATH.read_text(encoding="utf-8").replace("num_retries: 3", "seed_note: 0", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(load_config(path).num_retries, 3)

    def test_policies_inject_drop_and_save(self):
        cfg = _config()
        task = load_task(TASK_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "stream.sqlite")
            store.add(_entry("rare", "2024-05-01T00:00:00+00:00", task.issue_text))
            store.add(_entry("noise", "2024-04-01T00:00:00+00:00", "widget color palette refresh"))
            store.add(_entry("later", "2024-08-01T00:00:00Z", task.issue_text))
            store.add(_entry("other", "2020-01-01T00:00:00Z", task.issue_text, repo="other-repo"))
            calls: list[dict] = []

            def diagnose(**kwargs):
                calls.append(kwargs)
                self.assertEqual(kwargs["num_retries"], 3)
                return "The divisor is off by one.", 5, 7

            model = _fixing_model(cfg)
            saved = run_one(task, TASK_PATH, cfg, "memory_no_check", store, model=model, complete=diagnose)
            self.assertIsNone(saved.error)
            self.assertEqual(saved.hint_id, "rare")
            self.assertTrue(saved.hint_injected)
            self.assertTrue(saved.entry_saved)
            self.assertIsNone(saved.verifier_decision)
            self.assertIn(cfg.hint_prefix, "\n".join(model.prompts))
            self.assertEqual(calls[0]["model"], cfg.agent.model_name)

            def forbid(**_kwargs):
                raise AssertionError("none must not call diagnosis or the verifier")

            skipped = run_one(task, TASK_PATH, cfg, "none", store, model=_fixing_model(cfg), complete=forbid)
            self.assertIsNone(skipped.error)
            self.assertTrue(skipped.patch.strip())
            self.assertIsNone(skipped.hint_id)
            self.assertFalse(skipped.hint_injected)
            self.assertFalse(skipped.entry_saved)
            self.assertEqual(skipped.cost_diagnosis_usd, 0.0)

            def bad_json(**kwargs):
                self.assertEqual(kwargs["num_retries"], 3)
                if kwargs["model"] == cfg.verifier.model_name:
                    return '{"decision":"yes","reason":"ok","extra":1}', 3, 3
                return "diagnosis", 5, 7

            rejected = run_one(task, TASK_PATH, cfg, "write_check", store, model=_fixing_model(cfg), complete=bad_json)
            self.assertIsNone(rejected.error)
            self.assertEqual(rejected.hint_id, "rare")
            self.assertTrue(rejected.hint_injected)
            self.assertEqual(rejected.verifier_decision, "no")
            self.assertIn("could not be parsed", rejected.verifier_reason or "")
            self.assertFalse(rejected.entry_saved)
            self.assertEqual(rejected.cost_verifier_usd, usd(3, 3, 0.07, 0.30))

            def verifier_down(**kwargs):
                if kwargs["model"] == cfg.verifier.model_name:
                    raise RuntimeError("rate limited")
                return "diagnosis", 5, 7

            failed = run_one(
                task, TASK_PATH, cfg, "write_check", store, model=_fixing_model(cfg), complete=verifier_down
            )
            self.assertIn("rate limited", failed.error or "")
            self.assertIsNone(failed.verifier_decision)
            self.assertIsNone(failed.verifier_reason)
            self.assertFalse(failed.entry_saved)

            def read_no(**kwargs):
                if kwargs["model"] == cfg.verifier.model_name:
                    return '{"decision":"no","reason":"different bug"}', 8, 2
                return "diagnosis", 5, 7

            reader = _fixing_model(cfg)
            dropped = run_one(task, TASK_PATH, cfg, "read_check", store, model=reader, complete=read_no)
            self.assertEqual(dropped.hint_id, "rare")
            self.assertFalse(dropped.hint_injected)
            self.assertNotIn(cfg.hint_prefix, "\n".join(reader.prompts))
            self.assertEqual(dropped.verifier_decision, "no")
            self.assertIsNone(dropped.error)
            self.assertTrue(dropped.entry_saved)

            def diagnosis_down(**_kwargs):
                raise RuntimeError("diagnosis down")

            before = len(_rows(store))
            broken = run_one(
                task, TASK_PATH, cfg, "memory_no_check", store, model=_fixing_model(cfg), complete=diagnosis_down
            )
            self.assertIn("diagnosis down", broken.error or "")
            self.assertFalse(broken.entry_saved)
            self.assertIsNone(broken.verifier_decision)
            self.assertEqual(len(_rows(store)), before)
            store.close()

        first = (ROOT / "tasks" / "mock_mean" / "repo" / "mean.py").read_text(encoding="utf-8")
        second = (ROOT / "tasks" / "mock_mean_2" / "repo" / "mean.py").read_text(encoding="utf-8")
        self.assertIn("/ len(values) - 1", first)
        self.assertIn("// len(values)", second)


class StreamTest(unittest.TestCase):
    def test_stream_orders_tasks_and_injects_the_earlier_fix(self):
        cfg = _config()
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "stream.sqlite"
            planted = MemoryStore(database)
            planted.add(_entry("planted", "2020-01-01T00:00:00Z", "planted row that must not survive"))
            planted.close()
            calls: list[int] = []

            def diagnose(**kwargs):
                calls.append(kwargs["num_retries"])
                return "Summarized the patch.", 5, 7

            model = _four_step_model(cfg)
            records = run_stream(
                [TASK2_PATH, TASK_PATH],
                "memory_no_check",
                database,
                cfg,
                model=model,
                complete=diagnose,
            )
            self.assertEqual([record.task_id for record in records], ["mock-mean-001", "mock-mean-002"])
            self.assertIsNone(records[0].hint_id)
            self.assertFalse(records[0].hint_injected)
            self.assertTrue(records[0].entry_saved)
            self.assertTrue(records[1].hint_injected)
            self.assertIsNone(records[1].error)
            self.assertNotIn(cfg.hint_prefix, model.prompts[0])
            self.assertIn(cfg.hint_prefix, model.prompts[2])
            self.assertIn("instead of 2.0", model.prompts[2])
            self.assertEqual(calls, [3, 3])
            again = MemoryStore(database)
            rows = _rows(again)
            ids = {row.id for row in rows}
            self.assertNotIn("planted", ids)
            self.assertEqual({row.source_task_id for row in rows}, {"mock-mean-001", "mock-mean-002"})
            first = next(row for row in rows if row.source_task_id == "mock-mean-001")
            self.assertEqual(records[1].hint_id, first.id)
            again.close()

    def test_second_fixture_fails_its_visible_test(self):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "test_mean.py"],
            cwd=ROOT / "tasks" / "mock_mean_2" / "repo",
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("test_mean_of_two", proc.stderr + proc.stdout)


class CliTest(unittest.TestCase):
    def test_cli_prints_the_record_and_uses_the_exit_code(self):
        cfg = _config()
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "cli.sqlite"
            out = Path(tmp) / "out" / "record.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--task",
                        str(TASK_PATH),
                        "--policy",
                        "none",
                        "--config",
                        str(CONFIG_PATH),
                        "--db",
                        str(database),
                        "--out",
                        str(out),
                        "--seed",
                        "4",
                    ],
                    model=_empty_model(cfg),
                    complete=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("not called")),
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["task_id"], "mock-mean-001")
            self.assertEqual(payload["seed"], 4)
            self.assertIsNone(payload["error"])
            self.assertIsNone(payload["resolved"])
            self.assertFalse(payload["entry_saved"])
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), payload)

            failed_out = io.StringIO()
            with redirect_stdout(failed_out):
                failed = main(
                    [
                        "--task",
                        str(TASK_PATH),
                        "--policy",
                        "memory_no_check",
                        "--config",
                        str(CONFIG_PATH),
                        "--db",
                        str(database),
                    ],
                    model=BoomModel(),
                )
            self.assertEqual(failed, 1)
            body = json.loads(failed_out.getvalue())
            self.assertIn("rate limited", body["error"])
            self.assertIsNone(body["verifier_decision"])
            self.assertFalse(body["entry_saved"])

            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                bad = main(
                    ["--task", str(TASK_PATH), "--policy", "nope", "--config", str(CONFIG_PATH), "--db", str(database)]
                )
            self.assertEqual(bad, 1)
            self.assertIn("policy", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
