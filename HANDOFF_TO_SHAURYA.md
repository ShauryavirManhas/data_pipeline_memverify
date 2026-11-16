# Handoff to Shaurya

The corrected full run is finished. No further agent run is needed for this scale.

## Where the logs are

`runs/full` is gitignored. `git pull` will not bring it. Copy the directory. It has 104 logs (`{repo}__{policy}__s{seed}.jsonl`), the sqlite memory files, `spend.json`, score logs, and traces. Do not mix in `runs/pilot`, `runs/one`, or the `runs/full-backup-*` directories.

`data/live/instances.jsonl` is also gitignored. The scorer needs that local file. It must not be mounted into the agent sandbox.

## What was kept

1,248 of 1,248 slots are saved, in task order, with no holes. No billing-limit rows, no real history leaks, and no resolved row whose score is the old "test output markers not found" failure. Eighteen records are still `resolved: null` after 3 scoring attempts. The report drops those pairs: 14 unresolved pairs, 150 paired tasks, 298 pairs.

Headline from `analysis/full/report.md` (bootstrap seed 0, 10,000 resamples):

| policy | resolve rate | 95% CI | difference vs none |
| --- | ---: | --- | ---: |
| none | 0.282 | [0.214, 0.353] |  |
| memory_no_check | 0.245 | [0.182, 0.310] | -0.037 [-0.067, -0.007] |
| write_check | 0.262 | [0.197, 0.329] | -0.020 [-0.057, +0.017] |
| read_check | 0.265 | [0.199, 0.334] | -0.017 [-0.050, +0.017] |

Noise floor, `none` against itself across the two seeds: 0.047 (7/148). The memory drop clears that floor and its CI. Write-check and read-check do not.

## Stale versus stale hunks

On 783 retrieved hints in the paired set:

- File-level stale: 766/783 (97.8%).
- Hunk-level stale: 736/783 (94.0%).
- Remembered patch still applies: 55/783 (7.0%).

Hint-linked regressions are mostly the file-stale ones (memory 11 of 12, write-check 12 of 13). Splitting the same regressions by `stale_hunks` moves a few into the fresh bucket (memory 3, write-check 3) because the hunk flag is stricter. Fresh cells stay small either way. The open choice is which flag to treat as the primary stale label. The file-level flag matches "the hint names a file the gold patch also touches." The hunk flag matches "the hint touches the same lines."

## Verifier

`read_check` saw 255 hints. Catch rate 245/250. False-drop 5/5. Parse failures 0/255. It injected a hint on only 2 runs where `none` resolved, and neither of those changed the outcome.

`write_check` rejected 8/244. Of the hints it retrieved, 237/244 are file-stale. Wasted saves, saved and never retrieved: 126/267.

## Caveats to keep

- `haystack-8799` can copy a later `haystack-experimental` out of the image uv cache and score resolved. The network block does not stop that. One paired "improvement" on that task is this leak, not a hint that fixed the issue. See `analysis/full/trace_notes.md`.
- `llama_deploy-330` `memory_no_check` seed 0 is `TimeExceeded` with `future_commits_absent` false and no leftover future commit. It was kept, and it scored false.
- The runner now aborts a command if the sandbox has already exited, and retries that task up to 3 times without logging the dead wait. That is how the last keras tasks were finished. Do not start another full agent run unless the team changes the scale or the verifier.

## Your part

Human labels are still open: 50 to 100 hint decisions, two independent labelers. That is the remaining stage. The numbers above are the input to that choice, not a request for another Modal run.

The model endpoint used for the corrected pass is the shared `memverify` endpoint on the `drathis` workspace (`nvidia/GLM-5.3-Flash-NVFP4`). `configs/full.yaml` points both `api_base` lines there. The proxy token is only in the local `.env` (`MODAL_PROXY_TOKEN`, no `Bearer` prefix) and is not in git.
