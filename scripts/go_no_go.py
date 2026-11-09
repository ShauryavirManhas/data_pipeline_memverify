"""Stage 1 go/no-go for SWE-bench-Live Lite. Needs Docker (or Modal) and Hugging Face access.

Picks 5 tasks by rule (the first task of each of the 5 longest streams
under the loader's selection rule, so 5 different repos and images),
then for each task:

1. pulls the official image (timed);
2. probes the container: where the repo lives, whether git history is
   full or shallow, whether the next task's base commit is present in the
   image, Python version;
3. runs the gold patch -> must be resolved;
4. runs with no patch (base + test patch) -> must be unresolved, and the
   tests must actually have run (an infra failure is not a pass).

Decision rule (team plan): GO if at most 1 of 5 tasks fails a check.
NO-GO (switch to data-eng-bench and tell the TA) if 2 or more fail.

Writes ``<out>/results.json``, ``<out>/report.md`` and
``<out>/gold_failures.txt`` (usable as ``memverify.dataset --exclude-file``).

Usage:
    python scripts/go_no_go.py --out go_no_go            # 5 tasks, local Docker
    python scripts/go_no_go.py --backend modal ...       # Modal (backend untested)
    python scripts/go_no_go.py --instance-ids A B C D E  # override the pick
    python scripts/go_no_go.py --all --out gold_all      # gold-check every selected task
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memverify.dataset import (  # noqa: E402
    DEFAULT_DATASET,
    DEFAULT_SPLIT,
    SelectionParams,
    load_rows,
    row_to_task,
    scorer_record,
    select_streams,
)
from memverify.scorer import BackendError, InstanceStore, eval_script_for, make_backend, score  # noqa: E402

PROBE = r"""
exec 2>&1
cd /testbed 2>/dev/null && echo "TESTBED=yes" || echo "TESTBED=no"
echo "TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null)"
echo "HEAD=$(git rev-parse HEAD 2>/dev/null)"
echo "SHALLOW=$(git rev-parse --is-shallow-repository 2>/dev/null)"
echo "COMMITS=$(git rev-list --count HEAD 2>/dev/null)"
echo "REMOTES=$(git remote | tr '\n' ' ')"
echo "DIRTY=$(git status --porcelain 2>/dev/null | wc -l)"
echo "NEXT_PRESENT=$(git cat-file -e __NEXT__^{commit} 2>/dev/null && echo yes || echo no)"
echo "PYTHON=$(python --version 2>&1)"
echo "PYTHON_PATH=$(command -v python)"
echo "GIT=$(git --version 2>&1)"
echo "BASH=$(bash --version | head -1)"
echo "LS=$(ls -A /testbed 2>/dev/null | head -20 | tr '\n' ' ')"
"""


def probe(backend, image: str, next_commit: str | None) -> dict:
    script = PROBE.replace("__NEXT__", next_commit or "0" * 40)
    start = time.monotonic()
    result = backend.run(image, script, 300)
    info = {"seconds": round(time.monotonic() - start, 1), "pull_seconds": round(result.pull_seconds, 1), "where": result.where}
    for line in result.output.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            key, value = line.split("=", 1)
            info[key.lower()] = value.strip()
    if next_commit is None:
        info["next_present"] = "n/a"
    return info


def check_task(task, instance, next_commit, backend, store, timeout, log_dir, skip_probe=False) -> dict:
    image, _ = eval_script_for(instance)
    out = {"instance_id": task.id, "repo": task.repo, "image": image, "created_at": task.created_at}
    try:
        if not skip_probe:
            out["probe"] = probe(backend, image, next_commit)
    except BackendError as exc:
        out["probe"] = {"error": str(exc)}
    t0 = time.monotonic()
    gold = score(task, instance["patch"], instances=store, backend=backend, timeout=timeout, log_dir=Path(log_dir) / "gold")
    out["gold"] = {"resolved": gold.resolved, "seconds": round(time.monotonic() - t0, 1), **_brief(gold.details)}
    t0 = time.monotonic()
    empty = score(task, "", instances=store, backend=backend, timeout=timeout, log_dir=Path(log_dir) / "empty", run_if_empty=True)
    out["empty"] = {"resolved": empty.resolved, "seconds": round(time.monotonic() - t0, 1), **_brief(empty.details)}
    out["gold_ok"] = gold.resolved is True
    out["empty_ok"] = empty.resolved is False and bool(empty.details.get("tests_ran"))
    out["ok"] = out["gold_ok"] and out["empty_ok"]
    return out


def _brief(d: dict) -> dict:
    keep = ("error", "tests_ran", "patch_applied", "timed_out", "seconds_pull", "seconds_run", "where", "log_path", "log_problem")
    out = {k: d[k] for k in keep if k in d}
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        if key in d:
            out[key] = {part: len(v) for part, v in d[key].items()}
    return out


def report_md(results: dict) -> str:
    rows = results["tasks"]
    lines = [
        f"# Go/no-go: {results['dataset']} ({results['split']})",
        "",
        f"Decision: **{results['decision']}** ({results['n_ok']}/{len(rows)} tasks passed both checks; rule: GO if at most 1 of 5 fails).",
        "",
        f"Ran on {results['host']} ({results['platform']}), backend `{results['backend']}`, {results['finished_at']}.",
        "",
        "| Task | Gold resolves | Empty fails (tests ran) | Pull s | Gold s | Empty s | Repo at /testbed | Shallow | Commits | Next base in image |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        p = r.get("probe", {})
        lines.append(
            f"| {r['instance_id']} | {'yes' if r['gold_ok'] else 'NO'} | {'yes' if r['empty_ok'] else 'NO'} | "
            f"{p.get('pull_seconds', '?')} | {r['gold']['seconds']} | {r['empty']['seconds']} | {p.get('testbed', '?')} | "
            f"{p.get('shallow', '?')} | {p.get('commits', '?')} | {p.get('next_present', '?')} |"
        )
    lines += ["", "## Failures", ""]
    bad = [r for r in rows if not r["ok"]]
    lines += [f"- {r['instance_id']}: gold={r['gold']}, empty={r['empty']}" for r in bad] or ["None."]
    lines += [
        "",
        "## Answers for the handoff",
        "",
        "- Where the repo lives: see the /testbed column (the harness runs everything in /testbed).",
        "- Full git history: see Shallow and Commits. Staleness does not depend on this; it uses a host-side GitHub clone.",
        "- Next base in image: `yes` means later commits are inside the image. The agent could `git log --all` its way to the future fix; the runner should restrict that (SWE-bench-Live protocol).",
        "- Agent-edited test files: the scorer drops agent edits to files the test patch touches (the harness resets them anyway) and keeps edits to other test files.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--split", default=DEFAULT_SPLIT)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--instance-ids", nargs="+", default=None)
    ap.add_argument("--all", action="store_true", help="gold+empty check for every task in the selected streams")
    ap.add_argument("--min-stream-len", type=int, default=6)
    ap.add_argument("--backend", default="docker", help="docker, modal, or local:<checkout> for a dry run")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--out", default="go_no_go")
    args = ap.parse_args(argv)

    rows = load_rows(args.dataset, args.split)
    streams = select_streams(rows, SelectionParams(min_stream_len=args.min_stream_len))
    by_id = {r["instance_id"]: r for items in streams.values() for r in items}
    next_base = {}
    for items in streams.values():
        for a, b in zip(items, items[1:]):
            next_base[a["instance_id"]] = b["base_commit"]
    if args.instance_ids:
        all_rows = {r["instance_id"]: r for r in rows}
        picked = [all_rows[i] for i in args.instance_ids]
    elif args.all:
        picked = [r for items in streams.values() for r in items]
    else:
        picked = [items[0] for items in list(streams.values())[: args.n]]
    print(f"{len(streams)} streams; checking {len(picked)} tasks: {[r['instance_id'] for r in picked]}", flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = InstanceStore({r["instance_id"]: scorer_record(r) for r in picked})
    backend = make_backend(args.backend)
    if isinstance(backend, __import__("memverify.scorer", fromlist=["DockerBackend"]).DockerBackend):
        backend.remove_image = args.all  # keep disk bounded on the big run
    results = {
        "dataset": args.dataset, "split": args.split, "backend": args.backend,
        "host": socket.gethostname(), "platform": f"{platform.system()} {platform.machine()}",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tasks": [],
    }
    for row in picked:
        task = row_to_task(row)
        print(f"== {task.id}", flush=True)
        r = check_task(task, store.get(task.id), next_base.get(task.id), backend, store, args.timeout, out_dir / "logs", skip_probe=args.all)
        print(f"   gold_ok={r['gold_ok']} empty_ok={r['empty_ok']} gold={r['gold']['seconds']}s empty={r['empty']['seconds']}s", flush=True)
        results["tasks"].append(r)
        (out_dir / "results.json").write_text(json.dumps(results, indent=2))

    n_ok = sum(r["ok"] for r in results["tasks"])
    results["n_ok"] = n_ok
    results["decision"] = "GO" if len(picked) - n_ok <= 1 else "NO-GO"
    if len(picked) < 5 and not args.all:
        results["decision"] = f"TRIAL ONLY ({len(picked)} task(s); the GO/NO-GO rule needs 5)"
    if args.all:
        results["decision"] = f"{n_ok}/{len(picked)} valid"
    results["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    (out_dir / "report.md").write_text(report_md(results))
    (out_dir / "gold_failures.txt").write_text("".join(r["instance_id"] + "\n" for r in results["tasks"] if not r["ok"]))
    print(report_md(results))
    return 0 if results["decision"] != "NO-GO" else 3


if __name__ == "__main__":
    sys.exit(main())
