# HANDOFF: dataset, scoring, stale flag (Shaurya)

SPEC.md is still the contract. **No shared object changed.** `Task`, `MemoryEntry` and `RunRecord` are unchanged. Everything below is new files plus conventions.

## Status by stage

| Stage | Status |
| --- | --- |
| 1. Go/no-go on 5 Lite tasks | **Script ready, not yet run.** The build machine had no Docker daemon and no Hugging Face access. Run `python scripts/go_no_go.py` on an x86_64 Linux box with Docker (or Modal). Apple Silicon runs these x86 images under emulation, so don't trust a failure there. |
| 2. Loader + tasks.json | Done. `memverify/dataset.py` (rule in code, revision pinned in tasks.json). |
| 3. Scorer | Done. `memverify/scorer.py`. Matches official reports on 545 real Lite logs (see below). |
| 4. Stale flag | Done. `memverify/staleness.py`. |
| 5. Unit tests | Done. 41 new tests, no network/Docker/LLM. Full suite: 57 pass, 1 skipped. |
| 6. METRICS.md | Done. Please read before the pilot. |
| 7. Human labels | Later (needs read_check runs). Protocol fixed in METRICS.md section 6. |

## What was verified, and how

- **Grading matches the official harness.** I rebuilt each instance from public SWE-bench-Live submission logs and graded the real test output with `scorer.grade_log`. The official `resolved` and every FAIL_TO_PASS/PASS_TO_PASS set matched on 291/291 OpenHands instances and 254/254 SWE-agent instances. The generated `eval.sh` is byte-identical to the official one on all 291 OpenHands logs. `docs/validation/`, `scripts/validate_grading.py`.
- **End to end on a real eval script.** A small git repo with a real test patch runs through the harness eval script locally. Gold resolves. Wrong fix, P2P regression, test-only cheating and an unappliable patch all fail. Empty patch fails, and the go/no-go "empty run" executes the tests. A scorer crash gives `None`, not `False`.
- **Modal, verified 2026-11-08** on `conan-io__conan-17092`: image loads in ~1 min, full eval runs (8 to 11 min for conan), grading works. Finding: Modal adds environment variables that `docker exec` would not (`CFLAGS=-g0`, `PYTHONHASHSEED=0`, `PYTHONPATH=/pkg/:/root/`, thread limits). `CFLAGS=-g0` made a conan P2P test fail on every run. `ModalBackend` now runs with the image's own ENV read from Docker Hub (fallback: strip Modal's known additions); `details["where"]` says which. `scripts/modal_diag.py` reproduces and checks this in ~3 min.
- **Not verified:** the Docker backend against a real daemon (the CLI call sequence is tested with a fake `docker`).

## Answers to Dhiksha's three questions

1. **Where the repo lives:** `/testbed`. Every official Lite eval script does `cd /testbed`. `Task.workdir` is `/testbed` for real tasks.
2. **Full git history in the image:** the go/no-go probe reports shallow/commit count per image. Staleness does not depend on it, because it runs on a host-side blobless GitHub clone. 820 of 831 earlier-to-later Lite pairs are ancestor-related, so the SPEC's optional ancestor check is cheap host-side (`StaleResult.is_ancestor`).
3. **Agent-edited test files:** the scorer removes agent edits to files the test patch touches (the harness resets those anyway; this also avoids a conflict when the test patch creates a file the agent also created). Edits to other test files are kept and run, as in the official harness. `details["dropped_test_files"]` lists them.

## Things the team should decide (pilot call)

1. **Almost every Lite hint will be stale.** Lite tasks are months apart. On the 11 Lite repos with 6+ tasks, 94% of earlier-to-later pairs are stale under the planned file-level definition (78% of adjacent pairs), 79% under the hunk-level one, and 81% of remembered patches no longer apply. Good for giving read_check something to catch, but the "fresh" cells of the stale/fresh table will be small. Options: report `stale_hunks` as the split, or use the denser `full` split (`--split full`, same rule).
2. **Future-commit leak.** If the probe shows "next base in image = yes", the agent can find later fixes via git. Sumreen: consider `git reflog expire --expire=now --all && git gc --prune=now` in the agent container before it starts (SWE-bench-Live protocol forbids future commits).
3. **Stream size depends on the split version.** Public Lite id lists give 11 streams / 125 tasks or 13 / 156. The loader prints the real numbers and records the HF revision.

## For Sumreen (runner)

```python
from memverify.scorer import score_record, InstanceStore
store = InstanceStore.from_jsonl("data/live/instances.jsonl")
record, result = score_record(record, task, instances=store)   # resolved set
# result.resolved is None -> scorer infra failure: retry (METRICS: up to 2), never count as False
```

- `data/live/instances.jsonl` holds gold patches and test lists. **Keep it out of the agent's container.**
- Scoring needs one fresh container per score (an image of a few GB, plus tests that can take up to 30 min on big repos). On Modal, use `ModalBackend` (sandbox from the official image). The harness's own `--modal` path rebuilds images and does not support Live rows.
- Stale/fresh table: `memverify.staleness.stale_for_record(record, task, db_path)`. The stream DBs must be kept after each run.

## New files

`memverify/dataset.py`, `memverify/scorer.py`, `memverify/staleness.py`, `scripts/go_no_go.py`, `scripts/validate_grading.py`, `scripts/staleness_preview.py`, `tests/test_scorer.py`, `tests/test_staleness.py`, `tests/test_dataset.py`, `tests/fixtures/submission_logs/` (3 real logs, MIT), `METRICS.md`, `requirements-eval.txt`, `docs/validation/*.json`, this file. README gained a "Data and scoring" section; `.gitignore` gained data paths.

Suggested branch: `shaurya/data-scoring`.
