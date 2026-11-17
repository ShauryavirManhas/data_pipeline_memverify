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
) -> tuple[dict[tuple[str, int, str], bool | None], dict[tuple[str, int, str], Any]]:
    """File-level stale flags, plus the full staleness result for each retrieved hint.

    The database name uses the run's own seed. The report's ``--seed`` is only
    the bootstrap draw, and using it here would read seed 0's memory for seed 1.
    """
    flags: dict[tuple[str, int, str], bool | None] = {}
    details: dict[tuple[str, int, str], Any] = {}
    loaded: dict[str, Any] = {}
    for (task_id, run_seed), by_policy in kept.items():
        repo, task_path = tasks[task_id]
        if task_id not in loaded:
            loaded[task_id] = load_task(task_path)
        task = loaded[task_id]
        for policy, item in by_policy.items():
            record = RunRecord.from_dict(item["record"])
            key = (task_id, run_seed, policy)
            if record.hint_id is None:
                flags[key] = None
                details[key] = None
                continue
            db_path = run_dir / f"{slug(repo)}__{policy}__s{run_seed}.sqlite"
            result = stale_for_record(record, task, db_path)
            flags[key] = None if result is None else result.stale
            details[key] = result
    return flags, details


def noise_floor(kept: dict[tuple[str, int], dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Share of tasks whose ``none`` result flips between seed 0 and seed 1."""
    by_task: dict[str, dict[int, bool]] = defaultdict(dict)
    for (task_id, run_seed), by_policy in kept.items():
        by_task[task_id][run_seed] = bool(by_policy["none"]["record"]["resolved"])
    both = [seeds for seeds in by_task.values() if 0 in seeds and 1 in seeds]
    flips = sum(1 for seeds in both if seeds[0] != seeds[1])
    return {"n": len(both), "flips": flips, "rate": (flips / len(both) if both else None)}


def cost_parts(kept: dict[tuple[str, int], dict[str, dict[str, Any]]]) -> dict[str, dict[str, float | None]]:
    parts: dict[str, dict[str, float | None]] = {}
    for policy in POLICIES:
        records = [by_policy[policy]["record"] for by_policy in kept.values()]
        resolved = [record for record in records if record["resolved"]]
        spent = sum(float(record["cost_usd"]) for record in records)
        parts[policy] = {
            "agent": _mean([float(record["cost_agent_usd"]) for record in records]),
            "diagnosis": _mean([float(record["cost_diagnosis_usd"]) for record in records]),
            "verifier": _mean([float(record["cost_verifier_usd"]) for record in records]),
            "per_resolved": (spent / len(resolved) if resolved else None),
            "tokens_in": _mean([float(record["tokens_in"]) for record in records]),
            "tokens_out": _mean([float(record["tokens_out"]) for record in records]),
            "wall": _mean([float(record["wall_seconds"]) for record in records]),
        }
    return parts


def stream_rates(
    kept: dict[tuple[str, int], dict[str, dict[str, Any]]],
    repos: dict[str, str],
) -> list[tuple[str, dict[str, float], int]]:
    grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for key in kept:
        grouped[repos[key[0]]].append(key)
    rows = []
    for repo in sorted(grouped):
        keys = grouped[repo]
        rates = {
            policy: _rate([bool(kept[key][policy]["record"]["resolved"]) for key in keys])
            for policy in POLICIES
        }
        rows.append((repo, rates, len(keys)))
    return rows


def _share(num: int, den: int) -> str:
    if den == 0:
        return "n/a"
    return f"{num / den:.3f} ({num}/{den})"


def verifier_lines(
    kept: dict[tuple[str, int], dict[str, dict[str, Any]]],
    details: dict[tuple[str, int, str], Any],
) -> list[str]:
    """METRICS.md section 5. Parse failures use the verifier's own reason string."""
    parse_reason = "output could not be parsed"
    read_rows = []
    write_rows = []
    for (task_id, run_seed), by_policy in kept.items():
        for policy, item in by_policy.items():
            record = item["record"]
            if record.get("hint_id") is None or record.get("verifier_decision") is None:
                continue
            if record.get("error") and record.get("verifier_decision") is None:
                continue
            result = details.get((task_id, run_seed, policy))
            row = (record, result)
            if policy == "read_check":
                read_rows.append(row)
            elif policy == "write_check":
                write_rows.append(row)

    def rates(rows: list[tuple[dict[str, Any], Any]], drop_parse: bool) -> tuple[str, str, int]:
        usable = []
        for record, result in rows:
            if result is None:
                continue
            parsed_fail = record.get("verifier_reason") == parse_reason
            if drop_parse and parsed_fail:
                continue
            usable.append((record, result))
        stale = [(record, result) for record, result in usable if result.stale]
        fresh = [(record, result) for record, result in usable if not result.stale]
        caught = sum(1 for record, _result in stale if record["verifier_decision"] == "no")
        dropped = sum(1 for record, _result in fresh if record["verifier_decision"] == "no")
        return _share(caught, len(stale)), _share(dropped, len(fresh)), len(usable)

    lines = ["", "## Verifier", ""]
    if not read_rows and not write_rows:
        lines.append("No verifier calls in the paired set.")
        return lines
    caught, dropped, n_read = rates(read_rows, drop_parse=False)
    caught_clean, dropped_clean, n_clean = rates(read_rows, drop_parse=True)
    parse_fails = sum(1 for record, _result in read_rows if record.get("verifier_reason") == parse_reason)
    lines.append(f"read_check calls with a hint: {len(read_rows)}. Used for rates: {n_read}.")
    lines.append(f"- Catch rate (stale and decision no): {caught}.")
    lines.append(f"- False-drop rate (fresh and decision no): {dropped}.")
    lines.append(f"- Same rates with parse failures removed: catch {caught_clean}, false-drop {dropped_clean} ({n_clean} calls).")
    lines.append(f"- Parse failures: {_share(parse_fails, len(read_rows))}.")
    if read_rows and parse_fails / len(read_rows) > 0.10:
        lines.append("- Parse failures are over 10% of read_check calls, so that catch rate is unreliable.")
    write_no = sum(1 for record, _result in write_rows if record["verifier_decision"] == "no")
    write_stale = sum(1 for _record, result in write_rows if result is not None and result.stale)
    write_known = sum(1 for _record, result in write_rows if result is not None)
    lines.append(f"- write_check rejection rate: {_share(write_no, len(write_rows))}.")
    lines.append(f"- write_check retrieved hints that are file-stale: {_share(write_stale, write_known)}.")
    return lines


def wasted_write_checks(
    kept: dict[tuple[str, int], dict[str, dict[str, Any]]],
    tasks: dict[str, tuple[str, Path]],
    run_dir: Path,
) -> str:
    """Saved write_check entries that no later task in the stream retrieved."""
    import sqlite3

    calls = 0
    rejected = 0
    for _key, by_policy in kept.items():
        record = by_policy["write_check"]["record"]
        if record.get("verifier_decision") is None:
            continue
        calls += 1
        if record["verifier_decision"] == "no":
            rejected += 1
    seen: set[tuple[str, int]] = set()
    wasted = 0
    for (task_id, run_seed) in kept:
        repo = tasks[task_id][0]
        key = (repo, run_seed)
        if key in seen:
            continue
        seen.add(key)
        db_path = run_dir / f"{slug(repo)}__write_check__s{run_seed}.sqlite"
        if not db_path.is_file():
            continue
        hint_ids = {
            item["write_check"]["record"].get("hint_id")
            for (other_id, other_seed), item in kept.items()
            if other_seed == run_seed and tasks[other_id][0] == repo
        }
        hint_ids.discard(None)
        kept_sources = {
            other_id
            for (other_id, other_seed) in kept
            if other_seed == run_seed and tasks[other_id][0] == repo
        }
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT id, source_task_id FROM entries").fetchall()
        finally:
            conn.close()
        wasted += sum(1 for entry_id, source in rows if source in kept_sources and entry_id not in hint_ids)
    return (
        f"write_check wasted saves (saved, never retrieved): {_share(wasted, calls)}. "
        f"Rejected calls, counted separately: {rejected}."
    )


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


def write_report(
    path: Path,
    summary: dict[str, Any],
    drops: dict[str, int],
    regression_rows: list[dict[str, Any]],
    spend: dict[str, Any] | None,
    *,
    title: str,
    floor: dict[str, Any],
    parts: dict[str, dict[str, float | None]],
    by_stream: list[tuple[str, dict[str, float], int]],
    hunk_rows: list[dict[str, Any]] | None,
    stale_counts: dict[str, int] | None,
    verifier: list[str],
) -> None:
    lines = [
        f"# {title}",
        "",
        f"Paired tasks: {summary['n_tasks']}. Pairs: {summary['n_pairs']}.",
        f"Dropped incomplete pairs: {drops['incomplete']}. Dropped unresolved pairs: {drops['unresolved']}.",
        "",
        "## Resolve rate and cost",
        "",
    ]
    if floor["rate"] is None:
        lines.append("Noise floor: fewer than two seeds of `none`, so it is not computed.")
    else:
        lines.append(
            f"Noise floor (`none` vs itself): {floor['rate']:.3f} "
            f"({floor['flips']}/{floor['n']} tasks flip between seed 0 and seed 1)."
        )
    lines.extend([
        "",
        "| policy | resolve rate | 95% CI | mean cost | cost CI | difference vs none | difference CI |",
        "| --- | ---: | --- | ---: | --- | ---: | --- |",
    ])
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
    lines.extend(["", "Cost components, tokens, and wall time (paired runs):", ""])
    lines.append("| policy | agent | diagnosis | verifier | cost per resolved | tokens in | tokens out | wall seconds |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for policy in POLICIES:
        part = parts[policy]
        per = "n/a" if part["per_resolved"] is None else f"{part['per_resolved']:.4f}"
        lines.append(
            f"| {policy} | {part['agent']:.4f} | {part['diagnosis']:.4f} | {part['verifier']:.4f} | "
            f"{per} | {part['tokens_in']:.0f} | {part['tokens_out']:.0f} | {part['wall']:.0f} |"
        )
    lines.extend(["", "Stream-resampled difference CIs (robustness check):", ""])
    for policy in POLICIES_VS_NONE:
        lo, hi = summary["stream_diff_ci"][policy]
        lines.append(f"- {policy}: [{lo:+.3f}, {hi:+.3f}]")
    lines.extend(["", "Resolve rate by stream:", "", "| stream | pairs | none | memory_no_check | write_check | read_check |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for repo, rates, n_pairs in by_stream:
        lines.append(
            f"| {repo} | {n_pairs} | {rates['none']:.3f} | {rates['memory_no_check']:.3f} | "
            f"{rates['write_check']:.3f} | {rates['read_check']:.3f} |"
        )
    lines.extend(["", "## Regressions", "", "Split by the file-level stale flag.", ""])
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
    if hunk_rows is not None:
        lines.extend(["", "Same counts split by `stale_hunks` instead of file-level `stale`:", ""])
        for row in hunk_rows:
            lines.append(
                f"- {row['policy']}: regressions {row['regression']}, improvements {row['improvement']}, net {row['net']}"
            )
            for bucket in ("stale", "fresh", "unknown"):
                part = row["split"][bucket]
                lines.append(f"  - {bucket}: regressions {part['regression']}, improvements {part['improvement']}")
    if stale_counts is not None:
        n_hints = stale_counts["n"]
        lines.extend(["", "## Hint staleness", ""])
        lines.append(f"Retrieved hints in the paired set: {n_hints}.")
        lines.append(f"- File-level stale: {_share(stale_counts['stale'], n_hints)}.")
        lines.append(f"- Hunk-level stale: {_share(stale_counts['stale_hunks'], n_hints)}.")
        lines.append(f"- Remembered patch still applies: {_share(stale_counts['applies'], n_hints)}.")
    lines.extend(verifier)
    if spend is not None:
        lines.extend(["", "## Spend ledger", "", f"Total recorded by the runner: ${float(spend.get('usd', 0)):.4f}."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the paired pilot report")
    parser.add_argument("--run", type=Path, default=Path("runs/pilot"))
    parser.add_argument("--tasks", type=Path, default=Path("data/live/tasks.json"))
    parser.add_argument("--out", type=Path, default=Path("analysis/pilot"))
    parser.add_argument("--seed", type=int, default=0, help="Bootstrap draw only. Not the run seed.")
    parser.add_argument("--title", default="")
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
    hunk_rows = None
    stale_counts = None
    verifier: list[str] = []
    if args.skip_staleness:
        flags: dict[tuple[str, int, str], bool | None] = {}
    else:
        flags, details = staleness_flags(kept, tasks, args.run)
        hunk_flags = {key: (None if result is None else result.stale_hunks) for key, result in details.items()}
        hunk_rows = regressions(kept, hunk_flags)
        present = [result for result in details.values() if result is not None]
        stale_counts = {
            "n": len(present),
            "stale": sum(1 for result in present if result.stale),
            "stale_hunks": sum(1 for result in present if result.stale_hunks),
            "applies": sum(1 for result in present if result.patch_applies),
        }
        verifier = verifier_lines(kept, details)
        verifier.append("- " + wasted_write_checks(kept, tasks, args.run))
    regression_rows = regressions(kept, flags)
    spend_path = args.run / "spend.json"
    spend = json.loads(spend_path.read_text(encoding="utf-8")) if spend_path.is_file() else None
    args.out.mkdir(parents=True, exist_ok=True)
    title = args.title or ("Full run report" if args.run.name == "full" else "Pilot report")
    write_report(
        args.out / "report.md",
        summary,
        drops,
        regression_rows,
        spend,
        title=title,
        floor=noise_floor(kept),
        parts=cost_parts(kept),
        by_stream=stream_rates(kept, repos),
        hunk_rows=hunk_rows,
        stale_counts=stale_counts,
        verifier=verifier,
    )
    write_plot(args.out / "resolve_vs_cost.svg", summary)
    print(f"wrote {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
