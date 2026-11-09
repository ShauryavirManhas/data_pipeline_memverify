"""Scorer tests. No Docker, no network, no LLM.

Two layers:

- ``RealLogGradingTest``: three real SWE-bench-Live Lite logs (from the
  public submission repo, MIT) graded by ``grade_log`` must match the
  official harness ``report.json``. The full 291-instance check is
  ``scripts/validate_grading.py``.
- ``EndToEndLocalTest``: a tiny git repo with a real test patch and
  known good and bad patches, run through the official harness eval
  script on a local checkout (``LocalCheckoutBackend``).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from memverify.scorer import (
    BackendResult,
    InstanceStore,
    LocalCheckoutBackend,
    build_container_script,
    eval_script_for,
    grade_log,
    patch_paths,
    score,
    score_record,
    strip_test_patch_files,
)
from memverify.types import RunRecord, Task

FIXTURES = Path(__file__).parent / "fixtures" / "submission_logs"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.validate_grading import parse_eval_sh, repo_from_id  # noqa: E402


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


class RealLogGradingTest(unittest.TestCase):
    def _instance(self, iid: str) -> tuple[dict, dict, str]:
        d = FIXTURES / iid
        report = json.loads((d / "report.json").read_text())[iid]
        parsed = parse_eval_sh((d / "eval.sh").read_text())
        st = report["tests_status"]
        instance = {
            "instance_id": iid,
            "repo": repo_from_id(iid),
            "log_parser": "pytest",
            "FAIL_TO_PASS": st["FAIL_TO_PASS"]["success"] + st["FAIL_TO_PASS"]["failure"],
            "PASS_TO_PASS": st["PASS_TO_PASS"]["success"] + st["PASS_TO_PASS"]["failure"],
            **parsed,
        }
        return instance, report, (d / "test_output.txt").read_text()

    def test_matches_official_reports(self):
        for iid in ("scrapy-plugins__scrapy-splash-324", "encode__starlette-2812", "cyclotruc__gitingest-134"):
            with self.subTest(iid):
                instance, report, log = self._instance(iid)
                graded = grade_log(instance, log)
                self.assertEqual(graded["resolved"], report["resolved"])
                for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                    for part in ("success", "failure"):
                        self.assertEqual(set(graded[key][part]), set(report["tests_status"][key][part]))

    def test_eval_script_is_the_official_one(self):
        instance, _, _ = self._instance("scrapy-plugins__scrapy-splash-324")
        image, script = eval_script_for(instance)
        self.assertEqual(script.strip(), (FIXTURES / "scrapy-plugins__scrapy-splash-324" / "eval.sh").read_text().strip())
        self.assertEqual(image, "starryzhang/sweb.eval.x86_64.scrapy-plugins_1776_scrapy-splash-324:latest")

    def test_one_missing_f2p_test_means_not_resolved(self):
        instance, _, log = self._instance("scrapy-plugins__scrapy-splash-324")
        instance["FAIL_TO_PASS"] = instance["FAIL_TO_PASS"] + ["tests/test_nope.py::test_not_in_log"]
        self.assertFalse(grade_log(instance, log)["resolved"])

    def test_no_parsed_results_is_flagged(self):
        instance, _, _ = self._instance("scrapy-plugins__scrapy-splash-324")
        log = ">>>>> Start Test Output\n/usr/bin/python: No module named pytest\n>>>>> End Test Output\n"
        graded = grade_log(instance, log)
        self.assertFalse(graded["resolved"])
        self.assertEqual(graded["n_tests_parsed"], 0)
        self.assertIn("no test results", graded["log_problem"][0])

    def test_apply_failure_and_timeout_are_unresolved(self):
        instance, _, log = self._instance("scrapy-plugins__scrapy-splash-324")
        self.assertFalse(grade_log(instance, ">>>>> Patch Apply Failed\n" + log)["resolved"])
        graded = grade_log(instance, log + "\n>>>>> Tests Timed Out\n")
        self.assertFalse(graded["resolved"])
        self.assertTrue(graded["timed_out"])
        self.assertFalse(grade_log(instance, "no markers at all")["resolved"])


class PatchHelpersTest(unittest.TestCase):
    PATCH = textwrap.dedent(
        """\
        diff --git a/src/a.py b/src/a.py
        --- a/src/a.py
        +++ b/src/a.py
        @@ -1 +1 @@
        -x = 1
        +x = 2
        diff --git a/tests/test_a.py b/tests/test_a.py
        --- a/tests/test_a.py
        +++ b/tests/test_a.py
        @@ -1 +1 @@
        -assert False
        +assert True
        diff --git a/tests/test_new.py b/tests/test_new.py
        new file mode 100644
        --- /dev/null
        +++ b/tests/test_new.py
        @@ -0,0 +1 @@
        +pass
        """
    )

    def test_patch_paths(self):
        self.assertEqual(patch_paths(self.PATCH), ["src/a.py", "tests/test_a.py", "tests/test_new.py"])

    def test_strip_only_test_patch_files(self):
        test_patch = self.PATCH.split("diff --git a/tests/test_a.py")[0].replace("src/a.py", "tests/test_a.py")
        test_patch += "diff --git a/tests/test_new.py" + self.PATCH.split("diff --git a/tests/test_new.py")[1]
        kept, dropped = strip_test_patch_files(self.PATCH, test_patch)
        self.assertEqual(dropped, ["tests/test_a.py", "tests/test_new.py"])
        self.assertIn("src/a.py", kept)
        self.assertNotIn("tests/", kept)

    def test_strip_with_no_overlap_keeps_bytes(self):
        kept, dropped = strip_test_patch_files(self.PATCH, "")
        self.assertEqual((kept, dropped), (self.PATCH, []))

    def test_script_payload_survives_heredoc_marker(self):
        tricky = "diff --git a/x b/x\n+EOF_114329324912\n+'quotes' and $vars\n"
        script = build_container_script(tricky, "#!/bin/bash\necho hi\n", 10)
        self.assertNotIn("$vars", script)  # only inside the base64 payload


import importlib.util


@unittest.skipIf(
    importlib.util.find_spec("pytest") is None,
    "pytest is not installed in this Python; run: pip install -r requirements-eval.txt",
)
class EndToEndLocalTest(unittest.TestCase):
    """Synthetic SWE-bench-style instance graded by the real harness eval script."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="memverify-scorer-test-"))
        repo = cls.tmp / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")
        (repo / "tests" / "__init__.py").write_text("")
        (repo / "tests" / "test_calc.py").write_text("from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        cls.base = git(repo, "rev-parse", "HEAD").strip()

        def diff_after(edit) -> str:
            edit()
            git(repo, "add", "-A")
            out = git(repo, "diff", "--cached", cls.base)
            git(repo, "reset", "-q", "--hard", cls.base)
            git(repo, "clean", "-qfd")
            return out

        def write(rel, text):
            return lambda: (repo / rel).write_text(text)

        test_calc_new = (
            "from calc import add, mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n"
        )
        cls.test_patch = diff_after(
            lambda: (
                write("tests/test_calc.py", test_calc_new)(),
                write("tests/test_new.py", "from calc import add\n\n\ndef test_add_negative():\n    assert add(-1, -1) == -2\n")(),
            )
        )
        good_calc = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        cls.gold = diff_after(write("calc.py", good_calc))
        cls.wrong = diff_after(write("calc.py", "def add(a, b):\n    return a + b + 1\n\n\ndef mul(a, b):\n    return a * b\n"))
        cls.regress = diff_after(write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"))
        # Agent "fixes" the tests instead of the code: edits a test-patch file and creates the new test file.
        cls.cheat = diff_after(
            lambda: (
                write("tests/test_calc.py", test_calc_new.replace("== 5", "== -1"))(),
                write("tests/test_new.py", "def test_add_negative():\n    pass\n")(),
            )
        )
        # Correct code fix plus an edit to a test-patch file: the test edit must be dropped.
        cls.gold_plus_test_edit = diff_after(
            lambda: (write("calc.py", good_calc)(), write("tests/test_calc.py", "def test_mul():\n    pass\n")())
        )
        # Modifies a file that does not exist. (A hunk that is merely the reverse of
        # the real fix is NOT unappliable: the harness's last fallback,
        # `patch --batch --fuzz=5`, applies it reversed. See METRICS.md.)
        cls.unappliable = (
            "diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1,2 +1,2 @@\n"
            " def subtract(a, b):\n-    return a + b\n+    return a - b\n"
        )
        cls.repo = repo
        cls.instance = {
            "instance_id": "local__calc-1",
            "repo": "local/calc",
            "base_commit": cls.base,
            "created_at": "2025-01-01T00:00:00+00:00",
            "patch": cls.gold,
            "test_patch": cls.test_patch,
            "FAIL_TO_PASS": ["tests/test_calc.py::test_add", "tests/test_new.py::test_add_negative"],
            "PASS_TO_PASS": ["tests/test_calc.py::test_mul"],
            # quoted: the interpreter path may contain spaces (e.g. ".../AI Agents/...")
            "test_cmds": [f"{shlex.quote(sys.executable)} -m pytest -rA -p no:cacheprovider tests"],
            "log_parser": "pytest",
        }
        cls.store = InstanceStore({"local__calc-1": cls.instance})
        cls.task = Task(
            id="local__calc-1", repo="local/calc", issue_text="add() subtracts", base_commit=cls.base,
            created_at="2025-01-01T00:00:00+00:00", workdir="/testbed", docker_image="local/calc:latest",
        )
        cls.backend = LocalCheckoutBackend(repo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_score(self, patch, **kw):
        return score(self.task, patch, instances=self.store, backend=self.backend, timeout=120, **kw)

    def test_gold_resolves(self):
        result = self.run_score(self.gold)
        self.assertTrue(result.resolved, result.details)
        self.assertEqual(sorted(result.details["FAIL_TO_PASS"]["success"]), sorted(self.instance["FAIL_TO_PASS"]))

    def test_wrong_fix_fails_f2p(self):
        result = self.run_score(self.wrong)
        self.assertFalse(result.resolved)
        self.assertTrue(result.details["tests_ran"])
        self.assertEqual(result.details["PASS_TO_PASS"]["failure"], [])
        self.assertEqual(len(result.details["FAIL_TO_PASS"]["failure"]), 2)

    def test_regression_fails_p2p(self):
        result = self.run_score(self.regress)
        self.assertFalse(result.resolved)
        self.assertEqual(result.details["PASS_TO_PASS"]["failure"], ["tests/test_calc.py::test_mul"])

    def test_editing_tests_does_not_help(self):
        result = self.run_score(self.cheat)
        self.assertFalse(result.resolved)
        self.assertEqual(result.details["dropped_test_files"], ["tests/test_calc.py", "tests/test_new.py"])
        self.assertTrue(result.details["empty_patch"])

    def test_test_edits_are_dropped_but_code_fix_counts(self):
        result = self.run_score(self.gold_plus_test_edit)
        self.assertTrue(result.resolved, result.details)
        self.assertEqual(result.details["dropped_test_files"], ["tests/test_calc.py"])

    def test_unappliable_patch_fails(self):
        result = self.run_score(self.unappliable)
        self.assertFalse(result.resolved)
        self.assertFalse(result.details["patch_applied"])

    def test_empty_patch_is_unresolved_without_running(self):
        class Boom:
            name = "boom"

            def run(self, *a, **k):
                raise AssertionError("backend must not run for an empty patch")

        result = score(self.task, "  \n", instances=self.store, backend=Boom())
        self.assertFalse(result.resolved)
        self.assertEqual(result.details["reason"], "empty patch")

    def test_empty_patch_run_fails_f2p_keeps_p2p(self):
        # The go/no-go check: base + test patch, no fix.
        result = self.run_score("", run_if_empty=True)
        self.assertFalse(result.resolved)
        self.assertTrue(result.details["tests_ran"])
        self.assertEqual(len(result.details["FAIL_TO_PASS"]["failure"]), 2)
        self.assertEqual(result.details["PASS_TO_PASS"]["success"], ["tests/test_calc.py::test_mul"])

    def test_backend_error_is_none_not_false(self):
        class Down:
            name = "down"

            def run(self, *a, **k):
                raise RuntimeError("docker daemon not running")

        result = score(self.task, self.gold, instances=self.store, backend=Down())
        self.assertIsNone(result.resolved)
        self.assertIn("docker daemon", result.details["error"])

    def test_score_record_sets_only_resolved(self):
        record = RunRecord(
            task_id=self.task.id, policy="none", seed=0, patch=self.gold, resolved=None,
            tokens_in=10, tokens_out=5, cost_usd=0.0, cost_agent_usd=0.0, cost_diagnosis_usd=0.0,
            cost_verifier_usd=0.0, hint_id=None, hint_injected=False, entry_saved=False,
            verifier_decision=None, verifier_reason=None, wall_seconds=1.0, error=None,
        )
        scored, _ = score_record(record, self.task, instances=self.store, backend=self.backend, timeout=120)
        self.assertTrue(scored.resolved)
        self.assertEqual({**scored.to_dict(), "resolved": None}, record.to_dict())
        RunRecord.from_json(scored.to_json())  # still a valid record
        other = Task.from_dict({**self.task.to_dict(), "id": "other"})
        with self.assertRaises(ValueError):
            score_record(record, other, instances=self.store, backend=self.backend)

    def test_base_commit_mismatch_is_an_error(self):
        bad = Task.from_dict({**self.task.to_dict(), "base_commit": "0" * 40})
        with self.assertRaises(ValueError):
            score(bad, self.gold, instances=self.store, backend=self.backend)

    def test_log_dir_written(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.run_score(self.gold, log_dir=d)
            self.assertTrue((Path(d) / self.task.id / "test_output.txt").exists())
            self.assertTrue(json.loads((Path(d) / self.task.id / "report.json").read_text())["resolved"])
            self.assertTrue(result.resolved)


if __name__ == "__main__":
    unittest.main()


class DockerBackendCliTest(unittest.TestCase):
    """The docker CLI sequence, against a fake ``docker`` that logs its calls."""

    def test_call_sequence(self):
        from memverify.scorer import DockerBackend

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.log"
            fake = Path(d) / "docker"
            fake.write_text(textwrap.dedent(f"""\
                #!/bin/bash
                echo "$*" >> {log}
                case "$1 $2" in
                  "image inspect") exit 1 ;;
                  "pull "*) exit 0 ;;
                  "run -d") echo cid ;;
                  "exec -i") bash -s ;;
                  "rm -f") exit 0 ;;
                esac
                """))
            fake.chmod(0o755)
            result = DockerBackend(docker=str(fake)).run("ns/img:latest", "echo out; echo err >&2; exit 3", 60)
            self.assertIn("out", result.output)
            self.assertIn("err", result.output)
            self.assertEqual(result.exit_code, 3)
            calls = log.read_text().splitlines()
            self.assertEqual(calls[0], "image inspect ns/img:latest")
            self.assertTrue(calls[1].startswith("pull --platform linux/amd64 ns/img:latest"))
            self.assertTrue(calls[2].startswith("run -d --name memverify-score-"))
            self.assertIn("ns/img:latest tail -f /dev/null", calls[2])
            self.assertRegex(calls[3], r"^exec -i -u root memverify-score-\w+ bash -s$")
            self.assertRegex(calls[4], r"^rm -f memverify-score-\w+$")

    def test_missing_docker_is_backend_error(self):
        from memverify.scorer import BackendError, DockerBackend

        with self.assertRaises(BackendError):
            DockerBackend(docker="/nonexistent/docker").run("img", "true", 10)


class ModalEnvParityTest(unittest.TestCase):
    """Modal adds env vars `docker exec` would not (CFLAGS=-g0 broke a conan test)."""

    MODAL_ENV = {
        "CFLAGS": "-g0", "PYTHONPATH": "/pkg/:/root/", "PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "1641013200",
        "OMP_NUM_THREADS": "2", "MODAL_TASK_ID": "ta-1", "MODAL_SANDBOX_ID": "sb-1", "TERM": "xterm-mono",
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
    }
    IMAGE_ENV = ["PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.6.15"]

    def _run(self, wrapped: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sh"
            path.write_text(wrapped)
            out = subprocess.run(["bash", str(path)], env=self.MODAL_ENV, capture_output=True, text=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def test_image_env_mode_matches_docker(self):
        from unittest import mock

        from memverify import scorer

        backend = scorer.ModalBackend()
        with mock.patch.object(scorer, "image_env", return_value=self.IMAGE_ENV):
            wrapped, mode = backend.wrap("ns/img:latest", "#!/bin/bash\nexec 2>&1\nenv\necho 'q=it''s'\n")
        self.assertEqual(mode, "image")
        env = self._run(wrapped)
        env.pop("PWD", None), env.pop("SHLVL", None), env.pop("_", None), env.pop("q", None)
        self.assertEqual(env, {"PATH": self.IMAGE_ENV[0][5:], "LANG": "C.UTF-8", "PYTHON_VERSION": "3.6.15", "HOME": "/root"})

    def test_strip_mode_when_registry_unreachable(self):
        from unittest import mock

        from memverify import scorer

        backend = scorer.ModalBackend()
        with mock.patch.object(scorer, "image_env", side_effect=OSError("no network")):
            wrapped, mode = backend.wrap("ns/img:latest", "env\n")
        self.assertEqual(mode, "strip")
        env = self._run(wrapped)
        for gone in ("CFLAGS", "PYTHONPATH", "PYTHONHASHSEED", "SOURCE_DATE_EPOCH", "OMP_NUM_THREADS", "MODAL_TASK_ID", "MODAL_SANDBOX_ID", "TERM"):
            self.assertNotIn(gone, env)
        self.assertEqual(env["LANG"], "C.UTF-8")

    def test_strip_keeps_image_values_that_differ_from_modal_defaults(self):
        from memverify.scorer import docker_like_env_prefix

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sh"
            path.write_text(docker_like_env_prefix(None) + "\nenv\n")
            env_in = {**self.MODAL_ENV, "CFLAGS": "-O2", "PYTHONPATH": "/opt/lib"}
            out = subprocess.run(["bash", str(path)], env=env_in, capture_output=True, text=True).stdout
        self.assertIn("CFLAGS=-O2", out)
        self.assertIn("PYTHONPATH=/opt/lib", out)

    def test_image_env_reads_registry_config(self):
        from unittest import mock

        from memverify import scorer

        calls = []

        def fake(url, token, accept=None):
            calls.append(url)
            if url.startswith("https://auth.docker.io"):
                return {"token": "t"}
            if url.endswith("/manifests/latest"):
                return {"manifests": [
                    {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                    {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
                ]}
            if url.endswith("/manifests/sha256:amd"):
                return {"config": {"digest": "sha256:cfg"}}
            if url.endswith("/blobs/sha256:cfg"):
                return {"config": {"Env": self.IMAGE_ENV}}
            raise AssertionError(url)

        scorer._IMAGE_ENV_CACHE.clear()
        with mock.patch.object(scorer, "_registry_get", side_effect=fake):
            env = scorer.image_env("starryzhang/sweb.eval.x86_64.conan-io_1776_conan-17092:latest")
        self.assertEqual(env, self.IMAGE_ENV)
        self.assertIn("repository:starryzhang/sweb.eval.x86_64.conan-io_1776_conan-17092:pull", calls[0])
        self.assertTrue(calls[1].endswith("starryzhang/sweb.eval.x86_64.conan-io_1776_conan-17092/manifests/latest"))
