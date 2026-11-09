"""Check memverify.scorer against official SWE-bench-Live Lite evaluation logs.

Input: one submission's ``logs/`` directory from
https://github.com/SWE-bench-Live/submission (MIT). Each instance folder
has the harness's ``eval.sh``, ``test_output.txt`` and ``report.json``.

For every instance this rebuilds the scorer-side instance (base commit,
test patch and test commands from ``eval.sh``; FAIL_TO_PASS and
PASS_TO_PASS from the report's tests_status) and checks:

1. ``scorer.eval_script_for`` regenerates the same ``eval.sh``;
2. ``scorer.grade_log`` on the real ``test_output.txt`` gives the same
   ``resolved`` and the same F2P/P2P success/failure sets as the
   official ``report.json``.

Usage:
    python scripts/validate_grading.py <submission>/logs [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memverify.scorer import eval_script_for, grade_log  # noqa: E402

HEREDOC = "EOF_114329324912"


def repo_from_id(instance_id: str) -> str:
    owner, rest = instance_id.split("__", 1)
    return f"{owner}/{re.sub(r'-[0-9]+$', '', rest)}"


def parse_eval_sh(text: str) -> dict:
    lines = text.splitlines()
    base = None
    for line in lines:
        m = re.match(r"git checkout ([0-9a-f]{40})\b", line)
        if m:
            base = m.group(1)
            break
    start = text.index(f"<<'{HEREDOC}'\n") + len(f"<<'{HEREDOC}'\n")
    end = text.index(f"\n{HEREDOC}\n", start)
    test_patch = text[start:end]
    after = text[end:].splitlines()
    i0 = next(i for i, l in enumerate(after) if "Start Test Output" in l)
    i1 = next(i for i, l in enumerate(after) if "End Test Output" in l)
    test_cmds = after[i0 + 1 : i1]
    return {"base_commit": base, "test_patch": test_patch, "test_cmds": test_cmds}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs_dir")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    rows = []
    for inst_dir in sorted(p for p in Path(args.logs_dir).iterdir() if p.is_dir()):
        iid = inst_dir.name
        rec = {"instance_id": iid}
        try:
            report = json.loads((inst_dir / "report.json").read_text())[iid]
        except Exception as exc:
            rec["skip"] = f"no report: {exc}"
            rows.append(rec)
            continue
        status = report.get("tests_status")
        if not status or not (inst_dir / "test_output.txt").exists() or not (inst_dir / "eval.sh").exists():
            rec["skip"] = "no tests_status/test_output (patch did not apply or not run)"
            rec["official_resolved"] = report.get("resolved")
            rows.append(rec)
            continue
        eval_sh = (inst_dir / "eval.sh").read_text()
        parsed = parse_eval_sh(eval_sh)
        instance = {
            "instance_id": iid,
            "repo": repo_from_id(iid),
            "base_commit": parsed["base_commit"],
            "test_patch": parsed["test_patch"],
            "test_cmds": parsed["test_cmds"],
            "log_parser": "pytest",
            "FAIL_TO_PASS": status["FAIL_TO_PASS"]["success"] + status["FAIL_TO_PASS"]["failure"],
            "PASS_TO_PASS": status["PASS_TO_PASS"]["success"] + status["PASS_TO_PASS"]["failure"],
        }
        _, script = eval_script_for(instance)
        rec["eval_script_match"] = script.strip() == eval_sh.strip()
        graded = grade_log(instance, (inst_dir / "test_output.txt").read_text(errors="replace"))
        rec["official_resolved"] = report["resolved"]
        rec["our_resolved"] = graded["resolved"]
        rec["resolved_match"] = graded["resolved"] == report["resolved"]
        rec["sets_match"] = all(
            set(graded[k][part]) == set(status[k][part])
            for k in ("FAIL_TO_PASS", "PASS_TO_PASS")
            for part in ("success", "failure")
        )
        rows.append(rec)

    checked = [r for r in rows if "skip" not in r]
    summary = {
        "instances": len(rows),
        "checked": len(checked),
        "skipped": len(rows) - len(checked),
        "eval_script_match": sum(r["eval_script_match"] for r in checked),
        "resolved_match": sum(r["resolved_match"] for r in checked),
        "sets_match": sum(r["sets_match"] for r in checked),
        "official_resolved_true": sum(bool(r["official_resolved"]) for r in checked),
        # Grading mismatches fail the check. An eval.sh difference alone is reported but
        # does not fail: older submissions ran an earlier harness that also printed
        # `git status` / `git show` and used `git apply -v`, which does not change grading.
        "mismatches": [r for r in checked if not (r["resolved_match"] and r["sets_match"])],
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "mismatches"}, indent=2))
    for r in summary["mismatches"][:20]:
        print("MISMATCH", r)
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    return 0 if not summary["mismatches"] else 1


if __name__ == "__main__":
    sys.exit(main())
