# memverify

Runs [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) on two small local tasks under four memory policies and writes a `RunRecord` for each run. The policies are `none`, `memory_no_check`, `write_check`, and `read_check`.

## Setup

Python 3.11 or newer.

```
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set the key there, or export it in the shell. The only key the runner reads is:

```
FIREWORKS_AI_API_KEY
```

Model names and prices come from `configs/default.yaml`. The config file must not contain the key.

## One task

Use a separate database per policy so the streams do not mix.

```
python -m memverify.run --task tasks/mock_mean/task.json --policy none --config configs/default.yaml --db runs/none.sqlite
python -m memverify.run --task tasks/mock_mean/task.json --policy memory_no_check --config configs/default.yaml --db runs/memory_no_check.sqlite
python -m memverify.run --task tasks/mock_mean/task.json --policy write_check --config configs/default.yaml --db runs/write_check.sqlite
python -m memverify.run --task tasks/mock_mean/task.json --policy read_check --config configs/default.yaml --db runs/read_check.sqlite
```

The command prints the `RunRecord` JSON. `--out PATH` writes the same JSON. The process exits 0 when `error` is null and 1 when it is set. `resolved` stays null.

`--db` defaults to `memory.db_path` in the config (`runs/stream.sqlite`). A second CLI run with the same database can see rows saved by the first.

## A stream

`run_stream(tasks, policy, db_path, config)` sorts the tasks by `created_at`, then by `id`, deletes any database already at `db_path`, and calls `run_one` in that order. `tasks` is a list of task JSON paths, or `(Task, task_path)` pairs. It returns the `RunRecord`s. It does not set `resolved`.

```python
from memverify.config import load_config
from memverify.run import run_stream

records = run_stream(
    ["tasks/mock_mean/task.json", "tasks/mock_mean_2/task.json"],
    "memory_no_check",
    "runs/stream.sqlite",
    load_config("configs/default.yaml"),
)
```

`tasks/mock_mean_2` is the same repo name (`mock-mean`) with a later `created_at` and a different bug, so this stream can retrieve the first fix while solving the second.

## Tests

```
python -m unittest discover -s tests -v
```

The tests use a fake model and a fake completion. They do not call Fireworks and they do not construct `LitellmModel`.

## Known gaps

Docker mode is a stub. `environment.kind: docker` does not start a container. `run_one` in `memverify/run.py` returns immediately with `error` set to `Docker mode is not implemented`, and it does not retrieve or save. The same string is built in `_docker_stub` in `memverify/agent.py`. Real tasks carry the image on `Task.docker_image`. There is no global image key.

A real task image has to change these local-checkout assumptions:

- `make_checkout` in `memverify/agent.py` copies the task workdir and checks out `base_commit` with local git. A container run has to start `Task.docker_image` and do that checkout inside it.
- `collect_patch` and `_git` in `memverify/agent.py` read `git diff` from that local worktree. The diff has to be read from the container instead.
- `build_file_contents` and `_read_repo_file` in `memverify/verifier.py` read files from the local worktree at `base_commit`, before the agent edits. Those reads have to happen in the container.

`agent.step_limit` (25) and `environment.timeout` (30) in `configs/default.yaml` are for the mock tasks only. Real tasks need larger limits after a pilot. `agent.wall_time_limit_seconds` may need the same.

`MemoryStore.retrieve` in `memverify/memory.py` keeps a candidate when the repo matches and `created_at` is strictly earlier. It does not check that the candidate's `base_commit` is an ancestor of the task's `base_commit`. That check is the optional tightening described in `SPEC.md`, once real checkouts have full git history.

## After cloning

The mock task repos are stored as git bundles. Rebuild them once after cloning:

```
bash scripts/setup_mock_tasks.sh
```

## Data and scoring (SWE-bench-Live Lite)

Install the scorer extras on top of the agent requirements. Python 3.11+, Docker, and Hugging Face access are needed for real tasks.

```
python -m pip install -r requirements.txt -r requirements-eval.txt
```

### Go/no-go (5 tasks)

```
python scripts/go_no_go.py --out go_no_go
```

Picks the first task of each of the 5 longest streams, pulls each official image, probes it, runs the gold patch (must resolve) and no patch (must fail with tests actually run). Writes `go_no_go/report.md`. Rule: GO if at most 1 of 5 fails. `--backend modal` uses `ModalBackend`, which is untested.

### Select tasks (loader)

```
python -m memverify.dataset --split lite --min-stream-len 6 --out-dir data/live [--exclude-file go_no_go/gold_failures.txt]
```

Writes `data/live/tasks.json` (streams and ordered ids, plus the rule and parameters), `data/live/tasks/<id>/task.json` (one `Task` each, `workdir: /testbed`, `docker_image` set), and `data/live/instances.jsonl`. **`instances.jsonl` holds the gold patch, test patch and test lists. It is for the scorer only. Never mount it into the agent's container.** The script prints the real stream counts. On public Lite id lists, the rule gives 11 streams and 125 tasks (the July 2025 OpenHands submission, 299 ids), or 13 and 156 (a 2026 submission with a different 300-id set). The split's contents have changed over time, so record the HF dataset revision.

### Score

```python
from memverify.scorer import score, score_record
result = score(task, patch)                # ScoreResult(resolved, details)
record, result = score_record(record, task)  # RunRecord copy with resolved set
```

CLI: `python -m memverify.scorer --task data/live/tasks/<id>/task.json --record runs/x.json --out runs/x.scored.json` (also `--patch-file`, `--gold`, `--empty`). The backend comes from `--backend` or `MEMVERIFY_SCORER_BACKEND` (`docker` by default). The instance store comes from `--instances` or `MEMVERIFY_INSTANCES` (default `data/live/instances.jsonl`). `resolved = None` means the scorer could not run, so retry. It is not a failure. Definitions are in `METRICS.md`.

### Stale flag

```python
from memverify.staleness import stale, staleness, stale_for_record
stale(entry, task)                                   # bool, file-level (primary)
staleness(entry, task, detail=True)                  # + stale_hunks, patch_applies, ancestry
stale_for_record(record, task, "runs/<stream>.sqlite")  # for analysis; None if no hint
```

Uses blobless GitHub clones cached under `data/repos` (or `MEMVERIFY_REPO_CACHE`).

### Checks that need no Docker

```
python -m unittest discover -s tests -v
python scripts/validate_grading.py <submission>/logs              # vs official reports
python scripts/staleness_preview.py <submission>/logs --detail    # stale rates on real streams
```

Submission logs come from https://github.com/SWE-bench-Live/submission. Results are in `docs/validation/`.
