"""Loader tests on synthetic rows shaped like SWE-bench-Live. No network."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from memverify.dataset import (
    SelectionParams,
    docker_image_name,
    main,
    normalize_created_at,
    row_to_task,
    select_streams,
    write_outputs,
)
from memverify.types import Task


def row(repo: str, n: int, created_at, **extra):
    iid = repo.replace("/", "__") + f"-{n}"
    return {
        "instance_id": iid, "repo": repo, "base_commit": f"{n:040x}", "created_at": created_at,
        "problem_statement": f"issue {iid}", "patch": "gold", "test_patch": "tp",
        "FAIL_TO_PASS": json.dumps([f"t::{n}"]), "PASS_TO_PASS": [], "test_cmds": ["pytest -rA"],
        "log_parser": "pytest", "hints_text": "SECRET HINT", **extra,
    }


def corpus():
    rows = []
    for i in range(7):
        rows.append(row("big/repo", 100 + i, f"2025-03-0{i + 1}T10:00:00Z"))
    for i in range(6):
        rows.append(row("mid/repo", 200 + i, datetime(2025, 4, i + 1, tzinfo=timezone.utc)))
    rows.append(row("small/repo", 300, "2025-01-01T00:00:00+00:00"))
    rows.append(row("small/repo", 301, "2025-01-02 00:00:00"))  # naive, space separated
    random.Random(0).shuffle(rows)
    return rows


class SelectionTest(unittest.TestCase):
    def test_rule(self):
        streams = select_streams(corpus(), SelectionParams(min_stream_len=6))
        self.assertEqual(list(streams), ["big/repo", "mid/repo"])  # longest first, small dropped
        self.assertEqual([r["instance_id"] for r in streams["big/repo"]], [f"big__repo-{100 + i}" for i in range(7)])

    def test_order_is_by_parsed_time_then_id(self):
        rows = [
            row("a/b", 2, "2025-01-01T12:00:00+02:00"),  # 10:00 UTC
            row("a/b", 1, "2025-01-01T10:00:00Z"),        # same instant, smaller id
            row("a/b", 3, "2025-01-01T09:00:00Z"),
        ]
        streams = select_streams(rows, SelectionParams(min_stream_len=1))
        self.assertEqual([r["instance_id"] for r in streams["a/b"]], ["a__b-3", "a__b-1", "a__b-2"])

    def test_exclusions_apply_before_the_threshold(self):
        excluded = ["mid__repo-200"]
        streams = select_streams(corpus(), SelectionParams(min_stream_len=6), excluded)
        self.assertEqual(list(streams), ["big/repo"])

    def test_caps(self):
        streams = select_streams(corpus(), SelectionParams(min_stream_len=6, max_stream_len=6, max_streams=1))
        self.assertEqual(list(streams), ["big/repo"])
        self.assertEqual(len(streams["big/repo"]), 6)
        self.assertEqual(streams["big/repo"][-1]["instance_id"], "big__repo-105")

    def test_duplicates_and_missing_fields_fail(self):
        rows = corpus()
        with self.assertRaises(ValueError):
            select_streams(rows + [rows[0]])
        bad = dict(rows[0])
        del bad["FAIL_TO_PASS"]
        with self.assertRaises(ValueError):
            select_streams([bad])

    def test_created_at_formats(self):
        self.assertEqual(normalize_created_at("2025-01-02T03:04:05Z"), "2025-01-02T03:04:05+00:00")
        self.assertEqual(normalize_created_at("2025-01-02 03:04:05"), "2025-01-02T03:04:05+00:00")
        self.assertEqual(normalize_created_at("2025-01-02T05:04:05+02:00"), "2025-01-02T03:04:05+00:00")
        self.assertEqual(normalize_created_at(datetime(2025, 1, 2, 3, 4, 5)), "2025-01-02T03:04:05+00:00")
        self.assertEqual(normalize_created_at(1735787045000), "2025-01-02T03:04:05+00:00")
        with self.assertRaises(ValueError):
            normalize_created_at("")


class TaskOutputTest(unittest.TestCase):
    def test_image_name_matches_harness(self):
        from swebench.harness.test_spec.test_spec import make_test_spec

        r = row("conan-io/conan", 17102, "2025-01-01T00:00:00Z")
        spec = make_test_spec({**r, "instance_id": "conan-io__conan-17102"}, namespace="starryzhang")
        # The harness picks the host's arch; published SWE-bench-Live images are x86_64 only.
        expected = spec.instance_image_key.replace(".arm64.", ".x86_64.")
        self.assertEqual(docker_image_name("conan-io__conan-17102"), expected)
        self.assertEqual(expected, "starryzhang/sweb.eval.x86_64.conan-io_1776_conan-17102:latest")

    def test_task_has_only_agent_visible_fields(self):
        task = row_to_task(row("big/repo", 100, "2025-03-01T10:00:00Z"))
        text = task.to_json()
        Task.from_json(text)  # strict SPEC shape
        for secret in ("gold", "SECRET HINT", "FAIL_TO_PASS", "test_patch"):
            self.assertNotIn(secret, text)
        self.assertEqual(task.workdir, "/testbed")
        self.assertEqual(task.docker_image, "starryzhang/sweb.eval.x86_64.big_1776_repo-100:latest")

    def test_write_outputs_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "rows.jsonl"
            src.write_text("".join(json.dumps(r, default=str) + "\n" for r in corpus()))
            out = Path(d) / "out"
            self.assertEqual(main(["--dataset", str(src), "--out-dir", str(out)]), 0)
            manifest = json.loads((out / "tasks.json").read_text())
            self.assertEqual(manifest["n_streams"], 2)
            self.assertEqual(manifest["n_tasks"], 13)
            self.assertEqual(manifest["streams"][0]["repo"], "big/repo")
            first = manifest["streams"][0]["task_files"][0]
            Task.from_json((out / first).read_text())
            hidden = [json.loads(l) for l in (out / "instances.jsonl").read_text().splitlines()]
            self.assertEqual(len(hidden), 13)
            self.assertIsInstance(hidden[0]["FAIL_TO_PASS"], list)
            self.assertNotIn("problem_statement", hidden[0])
            self.assertNotIn("hints_text", hidden[0])
            # same input -> same selection
            out2 = Path(d) / "out2"
            main(["--dataset", str(src), "--out-dir", str(out2)])
            self.assertEqual(json.loads((out2 / "tasks.json").read_text())["task_ids"], manifest["task_ids"])


if __name__ == "__main__":
    unittest.main()
