"""Preview how often remembered fixes would be stale on real Lite streams.

Uses only public data: each instance's base commit (from the harness
eval.sh in a submission's logs) and the files a submitted patch touched
(a stand-in for what memory would store). For every repo with at least
``--min`` instances, order instances by PR number (a proxy for
created_at, which is not in the logs; the real loader uses created_at),
and for every ordered pair (earlier i, later j) report whether the
files i's patch touched changed between base(i) and base(j).

Also reports whether base(i) is an ancestor of base(j) (SPEC "gap 3").

Usage:
    python scripts/staleness_preview.py <submission>/logs --cache data/repos --out preview.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memverify.agent import files_from_patch  # noqa: E402
from memverify.staleness import RepoCache, changed_files, changed_hunk_files, is_ancestor, patch_applies  # noqa: E402
from scripts.validate_grading import parse_eval_sh, repo_from_id  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs_dir")
    ap.add_argument("--cache", default="data/repos")
    ap.add_argument("--min", type=int, default=6)
    ap.add_argument("--out", default=None)
    ap.add_argument("--detail", action="store_true", help="also hunk-level staleness and patch-applies (fetches blobs)")
    args = ap.parse_args(argv)

    by_repo = defaultdict(list)
    for d in sorted(p for p in Path(args.logs_dir).iterdir() if p.is_dir()):
        if not (d / "eval.sh").exists():
            continue
        base = parse_eval_sh((d / "eval.sh").read_text())["base_commit"]
        if base is None:  # test patch only adds files, so eval.sh never names the base commit
            continue
        patch = (d / "patch.diff").read_text(errors="replace") if (d / "patch.diff").exists() else ""
        # keep only non-test files: a stored fix is about source code
        patch = "".join(c for c in re.split(r"(?=^diff --git )", patch, flags=re.M) if c and "/test" not in c.split("\n")[0] and "test_" not in c.split("\n")[0])
        num = int(re.search(r"-(\d+)$", d.name).group(1))
        by_repo[repo_from_id(d.name)].append({"id": d.name, "num": num, "base": base, "files": files_from_patch(patch), "patch": patch})

    cache = RepoCache(args.cache)
    report = {"repos": [], "note": "ordered by PR number as a proxy for created_at; files from a submitted (not gold) patch"}
    tot_pairs = tot_stale = tot_adj = tot_adj_stale = tot_anc = tot_hunk = tot_noapply = 0
    for repo, items in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
        if len(items) < args.min:
            continue
        items.sort(key=lambda x: x["num"])
        path = cache.ensure(repo, [x["base"] for x in items])
        pairs = stale_n = adj = adj_stale = anc = hunk_n = noapply_n = 0
        for i, a in enumerate(items):
            for j in range(i + 1, len(items)):
                b = items[j]
                if not a["files"]:
                    continue
                s = bool(changed_files(path, a["base"], b["base"], a["files"]))
                pairs += 1
                stale_n += s
                anc += is_ancestor(path, a["base"], b["base"])
                if args.detail:
                    hunk_n += bool(s and changed_hunk_files(path, a["base"], b["base"], a["patch"]))
                    noapply_n += patch_applies(path, a["patch"], b["base"]) is False
                if j == i + 1:
                    adj += 1
                    adj_stale += s
        report["repos"].append({"repo": repo, "n": len(items), "pairs": pairs, "stale_pairs": stale_n,
                                "adjacent_pairs": adj, "adjacent_stale": adj_stale, "ancestor_pairs": anc,
                                "stale_hunk_pairs": hunk_n, "patch_no_longer_applies": noapply_n})
        tot_pairs += pairs; tot_stale += stale_n; tot_adj += adj; tot_adj_stale += adj_stale; tot_anc += anc
        print(f"{repo:45s} n={len(items):2d} pairs={pairs:3d} stale={stale_n:3d} ({stale_n / max(pairs, 1):.0%})"
              f" adjacent stale={adj_stale}/{adj} ancestor={anc}/{pairs}"
              + (f" hunk-stale={hunk_n} ({hunk_n / max(pairs, 1):.0%}) no-longer-applies={noapply_n} ({noapply_n / max(pairs, 1):.0%})" if args.detail else ""), flush=True)
        tot_hunk += hunk_n; tot_noapply += noapply_n
    report["totals"] = {"pairs": tot_pairs, "stale_pairs": tot_stale, "adjacent_pairs": tot_adj,
                        "adjacent_stale": tot_adj_stale, "ancestor_pairs": tot_anc,
                        "stale_hunk_pairs": tot_hunk, "patch_no_longer_applies": tot_noapply}
    print(json.dumps(report["totals"]))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
