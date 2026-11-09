# METRICS

Fixed before any full run. Owner: Shaurya. Changes after the pilot need a team note and a date in the changelog at the bottom.

## Units and data

- **Run**: one `RunRecord`, keyed by `(task_id, policy, seed)`.
- **Stream**: the tasks of one repo in `tasks.json` order (parsed `created_at`, then id). Every policy sees the same streams, the same order and the same seeds.
- **Pair**: the same `(task_id, seed)` under policy P and under `none`. All policy comparisons are paired.
- **Eval set**: the task ids in `tasks.json`, minus instances whose gold patch failed on our machines (`--exclude-file`), fixed before the full run. The same set is used for every policy.
- **Scorer**: `memverify.scorer.score`, harness pinned to SWE-bench-Live `python-only` at `ad79b85`, `SCORER_VERSION = 1`, test timeout 1800 s.

## `resolved` (per run)

`resolved = True` iff, in a fresh container of the task image, after applying the run's `patch` and the benchmark's `test_patch`, every `FAIL_TO_PASS` test passes and every `PASS_TO_PASS` test passes (harness rule: XFAIL counts as a pass, a test absent from the log counts as a failure). Scored from test output only. No LLM judge.

Fixed details:

- Every run's patch is scored, including runs with `error` set (`LimitsExceeded` and similar still leave a diff, as in SWE-bench).
- Empty patch (empty or whitespace-only after the step below) is `False` without running.
- Agent edits to files the `test_patch` touches are removed before applying. Edits to other test files are kept.
- Patch-apply failure, test timeout, or missing test markers in the log: `False`.
- Patch-apply uses the harness fallbacks in order: `git apply --verbose`, `git apply --verbose --reject`, `patch --batch --fuzz=5 -p1 -i`. Known harness quirk, kept on purpose: the last one can apply a hunk reversed when it looks already applied.
- `resolved = None` means the scorer could not run (image pull, container, backend error). Rescore up to 2 times. Still `None`: drop that `(task_id, seed)` from **every** policy for paired metrics, and report the count.

Validation: on 291 official Lite evaluation logs (OpenHands submission) and 254 more (SWE-agent submission), the scorer reproduces the official `resolved` and every F2P/P2P set exactly (`scripts/validate_grading.py`).

## 1. Resolve rate

For policy P: `#runs with resolved = True / #runs in the eval set`, after the paired exclusion above.

Report per policy, the paired difference `RR(P) - RR(none)`, and per-stream values. CI: 95% percentile bootstrap, 10,000 resamples of tasks (all seeds and policies of a resampled task move together). Robustness check: resample whole streams instead of tasks.

Noise floor: the run-to-run flip rate of `none` against itself over two seeds (share of tasks whose `resolved` differs). A policy difference smaller than this is not evidence.

## 2. Cost per task

For policy P: mean of `cost_usd` over its runs (config prices, not LiteLLM's numbers), with the three components (`cost_agent_usd`, `cost_diagnosis_usd`, `cost_verifier_usd`) reported separately. All runs count, including errored ones.

Also: **cost per resolved task** = `sum(cost_usd) / #resolved`, and mean `tokens_in`, `tokens_out`, `wall_seconds`. Scoring cost (containers) is not in `cost_usd` and is reported separately as compute time.

Headline figure: resolve rate (y) against mean cost per task (x), one point per policy, with bootstrap CIs on both axes.

## 3. Regressions versus the no-memory baseline

For a pair `(task_id, seed)`:

- **Regression**: `none` resolved = True, P resolved = False, and P's run had `hint_injected = True`.
- **Improvement**: `none` resolved = False, P resolved = True, and `hint_injected = True`.
- Pairs that flip with `hint_injected = False` are reported as **no-hint flips**. They estimate nondeterminism, not memory effects.

Report counts, `regressions / #pairs with none resolved and a hint injected` (regression rate), and net = improvements - regressions. Split every count by stale or fresh hint (section 4).

## 4. Stale flag (per run that retrieved a hint)

For a run with `hint_id` not null, take the entry `E` from that stream's SQLite DB and the run's task `T` (`memverify.staleness.stale_for_record`):

- **stale (primary)**: some path in `E.files_touched` differs between `E.base_commit` and `T.base_commit` (`git diff --no-renames --name-only`, content comparison, deleted counts as changed). Same commit or empty `files_touched` means fresh.
- **stale_hunks (secondary)**: some line `E.patch` edited (old side, plus or minus 3 lines) changed between the two commits.
- **patch_applies (secondary)**: `E.patch` passes `git apply --check` at `T.base_commit`.

Computed on the host from a full-history GitHub clone, so it does not depend on the image's git history. It is defined for every retrieved hint, including hints `read_check` dropped.

**Preview, measured on Lite already.** For the 11 repos with 6 or more tasks in the July 2025 OpenHands submission's Lite set (`docs/validation/staleness_preview_lite.json`), using that submission's patches as stand-in memory entries (831 earlier-to-later pairs): 94% stale (primary), 79% stale_hunks, 81% no longer apply; 78% of adjacent pairs are stale (primary). 99% of pairs are ancestor-related. So on Lite almost every hint will be stale under the primary definition. The stale-versus-fresh table will have small fresh cells, and the team should decide, after the pilot, whether `stale_hunks` is the split we report (see "Open decision").

## 5. Verifier catch rate on stale hints (`read_check`)

Over `read_check` runs with `hint_id` not null:

- **Catch rate** = `#(stale and verifier_decision = "no") / #stale`.
- **False-drop rate** = `#(fresh and verifier_decision = "no") / #fresh`.
- **Parse-failure rate** = share of `"no"` whose `verifier_reason` says the output could not be parsed. Catch and false-drop rates are reported both with and without parse failures. If parse failures exceed 10% of verifier calls, the read_check result is flagged as unreliable (SPEC note).

Runs with `error` set and `verifier_decision = null` are excluded and counted.

`write_check` analogues (its verifier never sees later code, so it cannot catch staleness by construction): write rejection rate = `#("no") / #write-check calls`; share of `write_check` retrieved hints that are stale (all passed the write check).

**Wasted checks**: `write_check` verifier calls on entries that were saved and never retrieved later in their stream, divided by all `write_check` verifier calls. Rejected entries are counted separately, not as wasted. `read_check` calls happen only on retrieval, so this is 0 by construction and the comparison is cost per useful check.

## 6. Verifier agreement with human labels

Sample: 50 to 100 `read_check` hint decisions (`hint_id` not null, no error), drawn by script with a fixed seed, stratified to roughly half `"yes"` and half `"no"` and to include both stale and fresh hints. Labelers see what the read-time verifier saw: new issue, remembered issue, remembered diagnosis, remembered patch, and the current file contents at `T.base_commit`. They do not see the verifier's decision, the staleness flag or any test result.

Question: "Should this hint be shown to the agent?" Answer `yes` or `no`. Guideline: `yes` only if the remembered fix is about the same kind of bug and still matches the current code well enough to guide the fix.

Two people label independently. Report:

- Inter-annotator raw agreement and Cohen's kappa.
- Adjudicated label: the shared label, or a short discussion when they disagree (both original labels kept).
- Verifier agreement: accuracy and Cohen's kappa against the adjudicated label, the 2x2 confusion matrix, and precision and recall with `"no"` (block) as the positive class. 95% bootstrap CIs over items.
- Also the verifier's agreement with each labeler separately, next to the inter-annotator agreement as the ceiling.

## Other reported counts

Hint rate (`hint_id` not null), injection rate (`hint_injected`), save rate (`entry_saved`), error rate by `error` type, and `resolved = None` count, all per policy.

## Open decision (after the pilot)

1. Primary stale split: file-level `stale` as planned, or `stale_hunks`, given the preview above.
2. If fresh hints are too rare on Lite, consider the `full` split for denser streams (`memverify.dataset --split full`); the rule is the same.

## Changelog

- 2026-11-08: first version (Shaurya).
