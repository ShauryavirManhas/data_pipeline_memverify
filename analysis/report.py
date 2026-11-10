"""Paired pilot report from METRICS.md.

Reads the runner's JSONL logs. A (task, seed) is kept only when every
policy has a record and none of them has resolved = null. Bootstrap
resamples tasks, and every policy of a drawn task moves together.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from memverify.runner import slug
from memverify.staleness import stale_for_record
from memverify.types import POLICIES, RunRecord, load_task

BOOTSTRAP = 10_000
POLICIES_VS_NONE = tuple(policy for policy in POLICIES if policy != "none")


def load_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(run_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                item["_log"] = str(path)
                rows.append(item)
    return rows


def task_index(tasks_json: Path) -> dict[str, tuple[str, Path]]:
    payload = json.loads(tasks_json.read_text(encoding="utf-8"))
    root = tasks_json.parent
    found: dict[str, tuple[str, Path]] = {}
    for stream in payload["streams"]:
        for rel, task_id in zip(stream["task_files"], stream["task_ids"]):
            found[task_id] = (stream["repo"], root / rel)
    return found


def paired(rows: list[dict[str, Any]]) -> tuple[dict[tuple[str, int], dict[str, dict[str, Any]]], dict[str, int]]:
    """Keep pairs that have every policy and no unresolved score."""
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in rows:
        record = item["record"]
        grouped[(record["task_id"], int(record["seed"]))][record["policy"]] = item
    kept: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    dropped_incomplete = 0
    dropped_unresolved = 0
    for key, by_policy in grouped.items():
        if any(policy not in by_policy for policy in POLICIES):
            dropped_incomplete += 1
            continue
        if any(by_policy[policy]["record"]["resolved"] is None for policy in POLICIES):
            dropped_unresolved += 1
            continue
        kept[key] = by_policy
    return kept, {"incomplete": dropped_incomplete, "unresolved": dropped_unresolved}


def _rate(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ci(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    last = len(ordered) - 1
    return ordered[int(0.025 * last)], ordered[int(0.975 * last)]


def bootstrap(kept: dict[tuple[str, int], dict[str, dict[str, Any]]], repos: dict[str, str], seed: int) -> dict[str, Any]:
    by_task: dict[str, list[tuple[str, int]]] = defaultdict(list)
    by_stream: dict[str, list[str]] = defaultdict(list)
    for task_id, run_seed in kept:
        by_task[task_id].append((task_id, run_seed))
        stream = repos[task_id]
        if task_id not in by_stream[stream]:
            by_stream[stream].append(task_id)
    task_ids = list(by_task)
    stream_names = list(by_stream)
    rng = random.Random(seed)

    def rates(keys: list[tuple[str, int]]) -> dict[str, float]:
        return {
            policy: _rate([bool(kept[key][policy]["record"]["resolved"]) for key in keys])
            for policy in POLICIES
        }

    def costs(keys: list[tuple[str, int]]) -> dict[str, float]:
        return {
            policy: _mean([float(kept[key][policy]["record"]["cost_usd"]) for key in keys])
            for policy in POLICIES
        }

    point_keys = [key for keys in by_task.values() for key in keys]
    point_rates = rates(point_keys)
    point_costs = costs(point_keys)
    rate_samples = {policy: [] for policy in POLICIES}
    cost_samples = {policy: [] for policy in POLICIES}
    diff_samples = {policy: [] for policy in POLICIES_VS_NONE}
    stream_diff_samples = {policy: [] for policy in POLICIES_VS_NONE}
    for _ in range(BOOTSTRAP):
        drawn_tasks = [rng.choice(task_ids) for _ in task_ids]
        keys = [key for task_id in drawn_tasks for key in by_task[task_id]]
        drawn_rates = rates(keys)
        drawn_costs = costs(keys)
        for policy in POLICIES:
            rate_samples[policy].append(drawn_rates[policy])
            cost_samples[policy].append(drawn_costs[policy])
        for policy in POLICIES_VS_NONE:
            diff_samples[policy].append(drawn_rates[policy] - drawn_rates["none"])
        drawn_streams = [rng.choice(stream_names) for _ in stream_names]
        stream_tasks = [task_id for name in drawn_streams for task_id in by_stream[name]]
        stream_keys = [key for task_id in stream_tasks for key in by_task[task_id]]
        stream_rates = rates(stream_keys)
        for policy in POLICIES_VS_NONE:
            stream_diff_samples[policy].append(stream_rates[policy] - stream_rates["none"])
    return {
        "n_tasks": len(task_ids),
        "n_pairs": len(kept),
        "rates": point_rates,
        "rate_ci": {policy: _ci(rate_samples[policy]) for policy in POLICIES},
        "costs": point_costs,
        "cost_ci": {policy: _ci(cost_samples[policy]) for policy in POLICIES},
        "diff": {policy: point_rates[policy] - point_rates["none"] for policy in POLICIES_VS_NONE},
        "diff_ci": {policy: _ci(diff_samples[policy]) for policy in POLICIES_VS_NONE},
        "stream_diff_ci": {policy: _ci(stream_diff_samples[policy]) for policy in POLICIES_VS_NONE},
    }


def regressions(kept: dict[tuple[str, int], dict[str, dict[str, Any]]], stale_of: dict[tuple[str, int, str], bool | None]) -> list[dict[str, Any]]:
    rows = []
    for policy in POLICIES_VS_NONE:
        counts = {"regression": 0, "improvement": 0, "no_hint_flip": 0, "hint_when_none_resolved": 0}
        split = {"stale": {"regression": 0, "improvement": 0}, "fresh": {"regression": 0, "improvement": 0}, "unknown": {"regression": 0, "improvement": 0}}
        for key, by_policy in kept.items():
            base = bool(by_policy["none"]["record"]["resolved"])
            other = by_policy[policy]["record"]
            changed = bool(other["resolved"]) != base
            injected = bool(other["hint_injected"])
            if base and injected:
                counts["hint_when_none_resolved"] += 1
            if not changed:
                continue
            if not injected:
                counts["no_hint_flip"] += 1
                continue
            kind = "regression" if base else "improvement"
            counts[kind] += 1
            flag = stale_of.get((*key, policy))
            bucket = "unknown" if flag is None else ("stale" if flag else "fresh")
            split[bucket][kind] += 1
        counts["net"] = counts["improvement"] - counts["regression"]
        rows.append({"policy": policy, **counts, "split": split})
    return rows


def staleness_flags(
    kept: dict[tuple[str, int], dict[str, dict[str, Any]]],
    tasks: dict[str, tuple[str, Path]],
    run_dir: Path,
    seed: int,
) -> dict[tuple[str, int, str], bool | None]:
    flags: dict[tuple[str, int, str], bool | None] = {}
    loaded: dict[str, Any] = {}
    for (task_id, run_seed), by_policy in kept.items():
        repo, task_path = tasks[task_id]
        if task_id not in loaded:
            loaded[task_id] = load_task(task_path)
        task = loaded[task_id]
        for policy, item in by_policy.items():
            record = RunRecord.from_dict(item["record"])
            if record.hint_id is None:
                flags[(task_id, run_seed, policy)] = None
                continue
            db_path = run_dir / f"{slug(repo)}__{policy}__s{seed}.sqlite"
            result = stale_for_record(record, task, db_path)
            flags[(task_id, run_seed, policy)] = None if result is None else result.stale
    return flags


def write_plot(path: Path, summary: dict[str, Any]) -> None:
    width, height, left, bottom = 640, 420, 70, 50
    plot_w, plot_h = width - left - 30, height - bottom - 30
    rates = summary["rates"]
    costs = summary["costs"]
    max_cost = max(costs.values()) if costs else 1.0
    max_cost = max(max_cost * 1.25, 0.01)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{left}" y2="{bottom + plot_h}" stroke="#222"/>',
        f'<line x1="{left}" y1="{bottom + plot_h}" x2="{left + plot_w}" y2="{bottom + plot_h}" stroke="#222"/>',
        f'<text x="{left + plot_w / 2}" y="{height - 12}" text-anchor="middle" font-size="13">mean cost per task (USD)</text>',
        f'<text x="18" y="{bottom + plot_h / 2}" font-size="13" transform="rotate(-90 18 {bottom + plot_h / 2})">resolve rate</text>',
    ]
    for policy in POLICIES:
        x = left + (costs[policy] / max_cost) * plot_w
        y = bottom + plot_h - rates[policy] * plot_h
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#1f4b99"/>')
        parts.append(f'<text x="{x + 8:.1f}" y="{y:.1f}" font-size="12">{policy}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_report(path: Path, summary: dict[str, Any], drops: dict[str, int], regression_rows: list[dict[str, Any]], spend: dict[str, Any] | None) -> None:
    lines = [
        "# Pilot report",
        "",
        f"Paired tasks: {summary['n_tasks']}. Pairs: {summary['n_pairs']}.",
        f"Dropped incomplete pairs: {drops['incomplete']}. Dropped unresolved pairs: {drops['unresolved']}.",
        "",
        "## Resolve rate and cost",
        "",
        "| policy | resolve rate | 95% CI | mean cost | cost CI | difference vs none | difference CI |",
        "| --- | ---: | --- | ---: | --- | ---: | --- |",
    ]
    for policy in POLICIES:
        rate_lo, rate_hi = summary["rate_ci"][policy]
        cost_lo, cost_hi = summary["cost_ci"][policy]
        if policy == "none":
            diff, diff_ci = "", ""
        else:
            diff = f"{summary['diff'][policy]:+.3f}"
            lo, hi = summary["diff_ci"][policy]
            diff_ci = f"[{lo:+.3f}, {hi:+.3f}]"
        lines.append(
            f"| {policy} | {summary['rates'][policy]:.3f} | [{rate_lo:.3f}, {rate_hi:.3f}] | "
            f"{summary['costs'][policy]:.4f} | [{cost_lo:.4f}, {cost_hi:.4f}] | {diff} | {diff_ci} |"
        )
    lines.extend(["", "Stream-resampled difference CIs (robustness check):", ""])
    for policy in POLICIES_VS_NONE:
        lo, hi = summary["stream_diff_ci"][policy]
        lines.append(f"- {policy}: [{lo:+.3f}, {hi:+.3f}]")
    lines.extend(["", "## Regressions", ""])
    for row in regression_rows:
        denom = row["hint_when_none_resolved"]
        rate = row["regression"] / denom if denom else 0.0
        lines.append(
            f"- {row['policy']}: regressions {row['regression']}, improvements {row['improvement']}, "
            f"net {row['net']}, no-hint flips {row['no_hint_flip']}, "
            f"regression rate {rate:.3f} ({row['regression']}/{denom})"
        )
        for bucket in ("stale", "fresh", "unknown"):
            part = row["split"][bucket]
            lines.append(f"  - {bucket}: regressions {part['regression']}, improvements {part['improvement']}")
    if spend is not None:
        lines.extend(["", "## Spend ledger", "", f"Total recorded by the runner: ${float(spend.get('usd', 0)):.4f}."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the paired pilot report")
    parser.add_argument("--run", type=Path, default=Path("runs/pilot"))
    parser.add_argument("--tasks", type=Path, default=Path("data/live/tasks.json"))
    parser.add_argument("--out", type=Path, default=Path("analysis/pilot"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--skip-staleness", action="store_true")
    args = parser.parse_args(argv)
    rows = load_rows(args.run)
    if not rows:
        print(f"no JSONL rows in {args.run}")
        return 1
    tasks = task_index(args.tasks)
    kept, drops = paired(rows)
    if not kept:
        print(f"no complete pairs yet. incomplete={drops['incomplete']} unresolved={drops['unresolved']}")
        return 1
    repos = {task_id: tasks[task_id][0] for task_id, _seed in kept}
    summary = bootstrap(kept, repos, args.seed)
    if args.skip_staleness:
        flags = {}
    else:
        flags = staleness_flags(kept, tasks, args.run, args.seed)
    regression_rows = regressions(kept, flags)
    spend_path = args.run / "spend.json"
    spend = json.loads(spend_path.read_text(encoding="utf-8")) if spend_path.is_file() else None
    args.out.mkdir(parents=True, exist_ok=True)
    write_report(args.out / "report.md", summary, drops, regression_rows, spend)
    write_plot(args.out / "resolve_vs_cost.svg", summary)
    print(f"wrote {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
