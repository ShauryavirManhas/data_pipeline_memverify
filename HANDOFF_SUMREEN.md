# HANDOFF for Sumreen: runner, scoring, analysis inputs

From Shaurya, 2026-11-09. What you need from the data and scoring side to build the runner, run the pilot, and do the analysis. Background: `HANDOFF_SHAURYA.md`. Metric definitions: `METRICS.md` (fixed before any full run; please read sections 1 to 5 before writing the notebook).

## 1. Status: GO

The go/no-go passed 5 out of 5 on Modal (2026-11-09). For each task, the gold patch resolved and an empty patch failed with the tests actually running. Report: `go_no_go/report.md`.

| Task | Score time (gold) | Score time (no patch) | First image load |
| --- | --- | --- | --- |
| conan-io__conan-17092 | 11.1 min | 10.2 min | ~1 min |
| matplotlib__matplotlib-28933 | 5.1 min | 6.8 min | 1.0 min |
| reflex-dev__reflex-4087 | 2.0 min | 1.6 min | 0.7 min |
| aws-cloudformation__cfn-lint-3749 | 1.2 min | 1.2 min | 0.6 min |
| deepset-ai__haystack-8489 | 0.7 min | 1.0 min | 2.2 min |

Scoring sandboxes use 4 cores and 16 GB. The whole go/no-go cost about $1 of Modal.

## 2. Setup

```bash
python -m pip install -r requirements.txt -r requirements-eval.txt
pip install modal && modal token new
python -m memverify.dataset --split lite --out-dir data/live
```

The loader writes:

- `data/live/tasks.json`: the streams (one per repo, tasks in time order) and the selection rule. On the current Hugging Face revision (`babb27e4...`) this is **13 streams**. Your runner reads this file.
- `data/live/tasks/<id>/task.json`: one `Task` per task (SPEC.md shape, `workdir: /testbed`, `docker_image` set).
- `data/live/instances.jsonl`: gold patches and test lists. **Scorer only. Never put this file, or the `data/` folder, inside the agent's container.**

## 3. Scoring each run

After each agent run:

```python
from memverify.scorer import InstanceStore, ModalBackend, score_record

store = InstanceStore.from_jsonl("data/live/instances.jsonl")   # load once
backend = ModalBackend()                                         # reuse

record, result = score_record(record, task, instances=store, backend=backend,
                              log_dir="score_logs/<stream>/<policy>")
```

- `record.resolved` is `True` or `False` when scoring worked. **`None` means the scorer itself failed** (Modal error, image pull). Retry up to 2 times. If still `None`, leave it `None`; METRICS.md drops that `(task, seed)` from every policy.
- Score every record, including ones with `error` set. Their partial diff still counts (METRICS.md section 1).
- `result.details` has the F2P/P2P pass and fail lists, timings, and `where` (should say `modal (env: image)`). Keep `details` next to the record if you can; it explains every failure.
- One fresh sandbox per score. Scoring is independent of the agent's container, so you can score in parallel.
- CLI alternative: `python -m memverify.scorer --task <task.json> --record <run.json> --backend modal --out <scored.json>`.

**Modal finding you need to know.** Modal adds environment variables that Docker would not (`CFLAGS=-g0`, `PYTHONHASHSEED=0`, `PYTHONPATH=/pkg/:/root/`, thread limits). `CFLAGS=-g0` made a conan test fail on every run. The scorer now runs with the image's own environment (read from Docker Hub). **The agent's sandbox has the same problem**: the agent would see failing tests that are not real. See section 4.

## 4. Running the agent on real tasks (Dhiksha's gap 1, still open)

`environment_kind: docker` in memverify is a stub. Someone has to implement it before the pilot. What I learned that should shape it:

- **memverify cannot run inside the task image.** The images ship the repo's own Python (conan: Python 3.6). memverify needs 3.11+. So the agent loop runs in your runner container, and only its shell commands run in the task image.
- **mini-swe-agent already has this pattern**: `minisweagent.environments.extra.swerex_modal.SwerexModalEnvironment(image=..., cwd="/testbed")` runs each command in a Modal sandbox of the image. Caveat: it installs pipx and the SWE-ReX runtime into the image, which needs a modern Python, and old images may not have one. A thin environment that calls `modal.Sandbox.exec("bash", "-c", cmd)` (the same calls as `scorer.ModalBackend`) only needs bash and is safer. Worth a 30-minute test on the conan image either way.
- **Give the agent Docker's environment.** Reuse `scorer.image_env(image)` and `scorer.docker_like_env_prefix(env)` so the agent's commands see the same variables as the scorer.
- **Remove future commits before the agent starts.** All 5 go/no-go images contain later commits (`Next base in image: yes`), so the agent could find the real fix through git. Run this once in the agent's sandbox before the first step:
  ```bash
  cd /testbed && git reflog expire --expire=now --all && git gc --prune=now --quiet
  ```
  Then check that `git cat-file -e <next task's base_commit>` fails. Agent's sandbox only. The scorer uses fresh containers and does not need this.
- **Patch collection** (`collect_patch`, `git diff` against `base_commit`) must run inside the sandbox, not on the runner host.
- `step_limit: 25` and `timeout: 30` were set for the mock tasks (gap 2). Conan's tests take about 10 minutes in full, so a 30 s command timeout will kill any full test run the agent attempts. Fine if the agent runs single test files; decide in the pilot.

## 5. Cost planning (scoring only)

From the go/no-go timings, a score averages about 4 minutes of sandbox time. At about $0.95/hour per scoring sandbox (Modal sandbox rates, 4 cores, 16 GB):

| Run | Scores | Scoring cost |
| --- | --- | --- |
| Pilot: 2 streams × 4 policies × 1 seed (~24 tasks) | ~96 | ~$6 |
| Full: 13 streams (~156 tasks) × 4 policies × 1 seed | ~624 | ~$40 |

Not included: the agent's own sandboxes (alive for the whole agent run) and Fireworks. Streams with conan or matplotlib cost more per score. Halving the sandbox to 2 cores (`ModalBackend(cpu=2, memory_mb=8192)`) should roughly halve the scoring cost; try it on the pilot and compare against `seconds_run`.

## 6. Analysis inputs

- **Resolve rate, cost, regressions:** from the scored RunRecords. Definitions in METRICS.md sections 1 to 3. Pairs are `(task_id, seed)` across policies.
- **Stale or fresh hint, per run:**
  ```python
  from memverify.staleness import stale_for_record
  s = stale_for_record(record, task, "runs/<stream>__<policy>.sqlite")  # None if no hint
  s.stale, s.stale_hunks, s.patch_applies
  ```
  Keep every stream's SQLite DB after the run; this reads the remembered entry from it. Uses blobless GitHub clones cached in `data/repos/`.
- **Expect few fresh hints.** On Lite, 80 to 94% of hints will be stale (METRICS.md section 4). The stale/fresh table will have small fresh cells; the team picks the primary split after the pilot.
- **Noise floor:** METRICS.md section 1 asks for a second `none` seed to measure run-to-run flips. Budget for it in the full run.

## 7. Pilot checklist (what I would check)

1. `details["where"]` says `modal (env: image)` on every score.
2. No `resolved = None` left after retries.
3. The future-commit check fails as expected in the agent's sandbox.
4. Policies differ at all: hint rate, `hint_injected`, `verifier_decision` counts.
5. Verifier parse failures under 10% (METRICS.md section 5).

Questions about scoring or data: ask Shaurya.
