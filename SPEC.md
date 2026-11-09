# memverify specification

Stage 1 contract for the CMU 11-768 memory-verification experiment.
Later stages implement this file. The logged objects are `Task`,
`MemoryEntry`, and `RunRecord`. Do not add, rename, or drop one of
their fields without calling it out.

The question under test is when a coding agent should verify a
remembered fix: before the fix is written into memory, or when a
later task reads it back.

## Agent runtime

Confirmed against mini-swe-agent v2 (SWE-agent/mini-swe-agent, docs
and `main` source as of 2026-11-07).

Programmatic run, local mode:

```python
from minisweagent.agents.default import DefaultAgent
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.environments.local import LocalEnvironment

model = LitellmModel(
    model_name=agent_model_name,  # from config, never hardcoded in the runner
    model_kwargs={"temperature": temperature, "seed": seed, "num_retries": num_retries},
    cost_tracking="ignore_errors",
)
env = LocalEnvironment(cwd=str(workdir), timeout=timeout)
agent = DefaultAgent(
    model,
    env,
    system_template=SYSTEM_TEMPLATE,
    instance_template=INSTANCE_TEMPLATE,
    step_limit=step_limit,
    cost_limit=0,  # the wrapper enforces agent.cost_limit from config prices
    wall_time_limit_seconds=wall_time_limit_seconds,
)
result = agent.run(prompt)
# result["exit_status"], result["submission"]
# agent.cost is LiteLLM's figure for the agent calls, used only as a cross-check
```

`DefaultAgent.run` returns the `extra` dict of the final exit message,
not a `(submission, exit_status)` tuple. That tuple was v1. The patch
we log is not `result["submission"]`. Submission only signals that the
agent finished. The patch is the git diff described below.

`LitellmModel` calls `litellm.completion` with a bash tool. The agent
model must support function calling. `LitellmModel` does not expose
token totals. Each response's `usage.prompt_tokens` and
`usage.completion_tokens` are summed by the wrapper, including calls
that raise `FormatError` (those still carry `extra["response"]`).

Logged costs come from those token counts and the prices in the
config:

```
usd = (tokens_in * price_in_per_1m + tokens_out * price_out_per_1m) / 1_000_000
```

Agent calls use the agent prices. The diagnosis call uses the agent
prices too. The verifier call uses the verifier prices.
`cost_agent_usd`, `cost_diagnosis_usd`, and `cost_verifier_usd` are
those three amounts. `cost_usd` is their sum. A call that did not
happen contributes 0 tokens and 0 dollars. A missing
`price_in_per_1m` or `price_out_per_1m` on the agent model or the
verifier model fails the run before any model call, with `error`
naming the missing price.

`agent.cost` is LiteLLM's own total for the agent calls. It is a
cross-check of `cost_agent_usd` and is not a field on `RunRecord`.
When both numbers exist and

```
abs(agent.cost - cost_agent_usd) > 0.05 * max(cost_agent_usd, agent.cost)
```

the runner logs a warning. The warning leaves `error` null and leaves
the recorded costs as the config-priced figures. `cost_tracking` is
`ignore_errors`, so a missing LiteLLM registry price does not abort
the agent. In that case `agent.cost` stays 0 and the warning still
fires when `cost_agent_usd` is positive.

The cost limit is separate from that cross-check. The wrapper keeps a
running agent cost from token counts and the agent prices in the
config. Before each agent call, if `agent.cost_limit` is positive and
the running cost is already greater than or equal to it, the wrapper
stops the agent. The exit matches mini-swe-agent's `LimitsExceeded`:
`exit_status` is `LimitsExceeded` and `error` is that status. The
call that crosses the limit is already finished; the next call does
not start. This check does not read LiteLLM's `agent.cost`.
`step_limit` and `wall_time_limit_seconds` still apply. The library's
own `cost_limit` is set to 0 on the `DefaultAgent` instance so its
check, which reads `agent.cost`, stays off.

On Windows, `LocalEnvironment` executes through `cmd.exe`
(`subprocess` with `shell=True`). The stock prompt assumes bash. The
wrapper therefore runs local commands with `bash -lc` when `bash` is
on `PATH` (Git Bash qualifies), and uses `LocalEnvironment` unchanged
on POSIX when `bash` is the system shell. On Windows, if Git's
`bash.exe` is installed beside `git`, the wrapper uses that program.
The `bash` entry on `PATH` is often the WSL launcher.

`environment.kind: docker` is a stub. The runner does not start a
container. It fails the run and sets `error` to say Docker mode is
not implemented. Real tasks carry their image on `Task.docker_image`.
Connecting that image waits until we see how those containers lay out
the repo: where the code lives, and how the diff and `file_contents`
are read. The mock task sets `docker_image` to null and uses
`environment.kind: local`.

The agent and the verifier are different calls. The verifier does not
go through `DefaultAgent`. It is one `litellm.completion` with no
tools, using the model and prompt named in config. The diagnosis call
is the same kind of completion, on the agent model.

`num_retries` (default 3) is passed to every one of those
`litellm.completion` calls. LiteLLM retries a network error, a rate
limit, or a server error that many times. If the call still fails,
`RunRecord.error` is that failure. The run does not insert a memory
row, and a failed verifier call is not recorded as decision `no`.
Only a reply that came back and is not the required JSON is
decision `no`.

mini-swe-agent also retries `LitellmModel.query`, controlled by
`MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT` (default 10). The wrapper sets
that outer loop to one attempt on the model it builds, so the retry
budget stays `num_retries`.

Model names come only from the config file. API keys come only from
the environment. LiteLLM's Fireworks variable is `FIREWORKS_AI_API_KEY`.
The config file must not contain a key, and logs must not echo one.

Default models, chosen because they are cheap Fireworks models that
support the call pattern above (prices checked 2026-11-07):

| Role | LiteLLM name | Price per 1M tokens |
| --- | --- | --- |
| Agent | `fireworks_ai/accounts/fireworks/models/qwen3-coder-30b-a3b-instruct` | $0.15 in / $0.60 out. Function calling. |
| Verifier | `fireworks_ai/accounts/fireworks/models/gpt-oss-20b` | $0.07 in / $0.30 out. Structured output. |

Names and prices are both set in the config. Diagnosis uses the agent
model and the agent prices.

## Objects

Implemented in `memverify.types`. Every field is present in the JSON
object. Loading rejects a missing or unknown field. `created_at` is a
timezone-aware ISO-8601 string (`Z` means UTC). Comparison parses the
timestamps; it does not sort the raw strings.

`cost_usd`, `cost_agent_usd`, `cost_diagnosis_usd`,
`cost_verifier_usd`, and `wall_seconds` are JSON numbers and are
stored as floats. Loading rejects a record whose `cost_usd` differs
from the sum of the three component costs by more than `1e-9`.
`seed`, `tokens_in`, and `tokens_out` are non-negative integers.
`hint_injected` and `entry_saved` are JSON booleans. Booleans are not
accepted as numbers.

### Task

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable task id. |
| `repo` | string | Repository key. Retrieval never crosses repos. |
| `issue_text` | string | The problem statement given to the agent. |
| `base_commit` | string | Git commit the workdir must be on before the agent starts. |
| `created_at` | string | Dataset time. An entry saved from this task carries the same timestamp. |
| `workdir` | string | Checkout to edit. Relative paths resolve against the task file's directory, not the process cwd. |
| `docker_image` | string or null | Image name for a containerized task. Null when the task runs on the local checkout. The mock task is null. |

### MemoryEntry

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | New id for this stored fix (`uuid4` hex). |
| `repo` | string | Copied from the task. |
| `source_task_id` | string | `Task.id` that produced the fix. |
| `created_at` | string | Copied from the task. This is the causal order, not the wall clock. |
| `base_commit` | string | Copied from the task. |
| `issue_text` | string | Copied from the task. This is the BM25 document. |
| `files_touched` | list of strings | Paths added or modified by the patch, relative to the repo root. |
| `diagnosis` | string | Short summary from the agent model. Empty when that call is skipped. A failure after retries sets `RunRecord.error` and does not save the entry. |
| `patch` | string | Unified diff against `base_commit`. May be empty. |

### RunRecord

| Field | Type | Meaning |
| --- | --- | --- |
| `task_id` | string | `Task.id`. |
| `policy` | string | One of `none`, `memory_no_check`, `write_check`, `read_check`. |
| `seed` | int | Seed passed to every model call in the run. |
| `patch` | string | Worktree diff against `base_commit` after the agent stops. |
| `resolved` | bool or null | Always null when the agent writes the record. A later scorer sets it. |
| `tokens_in` | int | Prompt tokens across agent, diagnosis, and verifier calls. |
| `tokens_out` | int | Completion tokens across those same calls. |
| `cost_usd` | float | Sum of the three component costs below. |
| `cost_agent_usd` | float | Config-priced cost of the agent calls. |
| `cost_diagnosis_usd` | float | Config-priced cost of the diagnosis call. 0 when that call is skipped. |
| `cost_verifier_usd` | float | Config-priced cost of the verifier call. 0 when that call is skipped. |
| `hint_id` | string or null | Id of the retrieved candidate, or null when nothing was retrieved. Set even if read-time verification drops the hint. |
| `hint_injected` | bool | True only when the hint text was included in the prompt shown to the agent. |
| `entry_saved` | bool | True only when a memory row was inserted. |
| `verifier_decision` | `"yes"`, `"no"`, or null | The one verifier call this policy makes. Null when the policy does not call the verifier. |
| `verifier_reason` | string or null | Reason from that same call. Null exactly when `verifier_decision` is null. |
| `wall_seconds` | float | Wall time of the whole run, including retrieval, verification, the agent, diagnosis, and the save. |
| `error` | string or null | Null on a clean finish. Otherwise a short failure description. A failure still writes the record. |

`hint_id` is the candidate that retrieval returned. It is not "the
hint that was injected". `hint_injected` records whether that text
was shown to the agent. For `read_check`, a non-null `hint_id`
together with `hint_injected: false` and `verifier_decision: "no"`
means the hint was retrieved and then dropped.

## Config

YAML. One file, passed as `--config`. Shape:

```yaml
seed: 0
num_retries: 3           # LiteLLM retries for the agent, diagnosis, and verifier. Default 3 if omitted.
agent:
  model_name: fireworks_ai/accounts/fireworks/models/qwen3-coder-30b-a3b-instruct
  price_in_per_1m: 0.15
  price_out_per_1m: 0.60
  temperature: 0.0
  step_limit: 25          # mock task only. Real tasks need larger limits, set after the pilot.
  cost_limit: 0.50
  wall_time_limit_seconds: 300
  cost_tracking: ignore_errors
environment:
  kind: local              # local | docker (docker is a stub)
  timeout: 30              # mock task only. Real tasks need larger limits, set after the pilot.
verifier:
  model_name: fireworks_ai/accounts/fireworks/models/gpt-oss-20b
  price_in_per_1m: 0.07
  price_out_per_1m: 0.30
  temperature: 0.0
  max_file_chars: 20000
  max_file_contents_chars: 60000
  write_prompt: |
    ...
  read_prompt: |
    ...
diagnosis:
  prompt: |
    ...
memory:
  db_path: runs/stream.sqlite
  hint_prefix: "A similar past issue in this repo was handled like this:"
```

`step_limit: 25` and `timeout: 30` are for the mock task only. Real
tasks need larger limits, chosen after a pilot run. The image for a
containerized task comes from `Task.docker_image`, not from this file.

`--seed` and `--db` on the CLI override `seed` and `memory.db_path`.
The prompts below are the default text. Config may replace them; the
runner does not rewrite them.

Diagnosis prompt:

```
Summarize this attempted fix in two or three sentences.
Describe what the change does. Do not say whether tests passed.
You have not been shown test results.

Issue:
{issue_text}

Patch:
{patch}
```

Write-time verifier prompt:

```
Decide whether this attempted fix should be saved for future tasks
in the same repository. Say yes only if the patch is a plausible
attempt at the issue and the diagnosis describes that patch.
Do not use test results. You have not been shown any.

Issue:
{issue_text}

Diagnosis:
{diagnosis}

Patch:
{patch}

Reply with JSON only, no markdown:
{"decision": "yes" or "no", "reason": "one or two sentences"}
```

Read-time verifier prompt:

```
Decide whether a fix remembered from an earlier issue should be
shown to an agent that is about to solve a new issue. Say yes only
if the remembered fix is about the same kind of bug and still matches
the current contents of the files it touched. Say no if the files
have changed in a way that makes the old patch a poor guide, or if
the issues are only superficially similar.

New issue:
{issue_text}

Remembered issue:
{hint_issue_text}

Remembered diagnosis:
{diagnosis}

Remembered patch:
{patch}

Current contents of the files the remembered fix touched:
{file_contents}

Reply with JSON only, no markdown:
{"decision": "yes" or "no", "reason": "one or two sentences"}
```

`{file_contents}` is a concatenation of the files in
`files_touched`, read from the worktree at `base_commit` before the
agent edits anything. Each file is wrapped as
`----- {path} -----\n{text}\n`. A missing file is wrapped as
`----- {path} -----\n<file not present>\n`.

Each file's text is cut to `verifier.max_file_chars` characters
(default 20000) before it is wrapped. The joined `{file_contents}`
string is then cut to `verifier.max_file_contents_chars` characters
(default 60000). A cut keeps the prefix and appends a line

```
<truncated, N characters omitted>
```

where `N` is the number of characters removed by that cut. The marker
is part of the prompt the verifier sees.

Stage 3 note. `read_check` will not send a prefix of each file.
`{file_contents}` will be windows of about 60 lines around the hunks
in the remembered patch. Those windows are approximate, because line
numbers may have shifted between the remembered commit and the
current `base_commit`. If the hunks cannot be parsed, fall back to
the prefix above. The per-file cap, the total cap, and the
`<truncated, N characters omitted>` marker stay as written.

## Memory

One SQLite file per stream. The caller keeps a stream on a single
policy; the library does not record the policy inside the database.

```sql
CREATE TABLE entries (
  id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  source_task_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  issue_text TEXT NOT NULL,
  files_touched TEXT NOT NULL,  -- JSON array of strings
  diagnosis TEXT NOT NULL,
  patch TEXT NOT NULL
);
CREATE INDEX idx_entries_repo_time ON entries (repo, created_at);
```

`memory_no_check` and `read_check` store an entry after every task
whose patch is non-empty, including when the patch is wrong. The
runner builds that entry from the task, the patch, and the diagnosis.
`write_check` builds the same entry, the verifier reviews it, and the
row is inserted only when the decision is `yes`. A malformed verifier
reply is a `no` (see below), so the row is not inserted. A verifier
call that fails after retries sets `error` and does not insert.
`none` skips retrieval, skips the diagnosis call, and saves nothing.

An empty patch is never stored, under every policy. Empty means the
diff is empty or only whitespace. The diagnosis call is skipped,
`cost_diagnosis_usd` stays 0, and `write_check` does not call the
verifier. `entry_saved` is false. `entry_saved` is true only when a
row was inserted.

The agent never sees hidden test results. Nothing in the entry is
taken from a test runner. `resolved` is not an input to memory.

## Retrieval

Candidates are rows with the same `repo` and with `created_at`
strictly earlier than the current task. Rank them with BM25
(`rank_bm25.BM25Okapi`) over `issue_text`, tokenized by splitting on
non-alphanumeric characters and lowercasing. The query is the current
task's `issue_text`, tokenized the same way. Term frequency and
length normalization stay BM25Okapi. The IDF is the Lucene form
`log(1 + (N - n + 0.5) / (n + 0.5))`, where `N` is the number of
candidates and `n` is how many of them contain the term.

Return the top 1 whose score is greater than 0. A score of 0 is not
a hit. No hit means no hint: `hint_id` is null, `hint_injected` is
false, and the verifier is not called for `read_check`.

Ties: higher BM25 score wins. Equal scores keep the later
`created_at`. A remaining tie keeps the lexicographically smaller
`id`.

Possible later tightening: require that the candidate's
`base_commit` is an ancestor of the task's `base_commit`, if real
task checkouts have full git history. The current rule does not look
at commits.

## Policies

One switch, the `policy` string.

1. Copy the workdir to a temporary directory and check out
   `base_commit` there. The fixture is not edited in place.
   `environment.kind: docker` does not run; it fails as described
   under Agent runtime.
2. `none` skips retrieval. The other policies retrieve as above.
3. Apply the policy to the candidate:

| Policy | Inject a hint? | Verifier | What is stored afterward? |
| --- | --- | --- | --- |
| `none` | Never. | Not called. | Nothing. No diagnosis call. |
| `memory_no_check` | Yes, if a candidate exists. | Not called. | The new entry, when the patch is non-empty. |
| `write_check` | Yes, if a candidate exists. No second check on the way in. | Reviews the new entry before it is saved. Skipped when the patch is empty. | The new entry only when the patch is non-empty and the decision is `yes`. |
| `read_check` | Yes only when a candidate exists and the verifier returns `yes`. | Reviews the candidate against the new issue and the current files. | The new entry when the patch is non-empty. |

`write_check` still reads memory. The check is on the way out, so
the store only contains entries a previous write-check accepted.
`read_check` saves the new entry and filters hints on the way in.
`none` does not read, does not call the diagnosis model, and does
not insert a row.

4. When a hint is injected, the task prompt is the issue text plus a
   blank line plus:

```
A similar past issue in this repo was handled like this:
Issue: {hint.issue_text}
Diagnosis: {hint.diagnosis}
Patch:
{hint.patch}
```

The prefix is `memory.hint_prefix` from config. The agent still has
to solve the task itself. The hint is context, not a command to
replay the patch. `hint_injected` is true only when this block was
added to the prompt.

5. Run the agent in the temporary checkout.
6. Collect the patch, token totals, the three cost components,
   `cost_usd`, and wall time.
7. If the patch is empty, stop. Skip the diagnosis call. Skip the
   verifier, including `write_check`. `entry_saved` is false and
   `cost_diagnosis_usd` is 0. This holds for every policy.
   If the patch is non-empty, `none` still skips diagnosis and does
   not insert. The other policies ask the agent model for the
   diagnosis, then save or drop the new entry according to the table.
   `entry_saved` is true only when the insert runs. If that diagnosis
   call or the write-time verifier call fails after retries, set
   `error` and do not insert.

## Verifier output

The completion text must be a JSON object with `decision` equal to
`yes` or `no`, and `reason` a string. A surrounding ```json fence is
stripped once before parsing. Anything else, including an extra key
or a decision outside those two strings, is logged as `no` with a
reason that says the output could not be parsed. That `no` drops the
hint under `read_check` and skips the insert under `write_check`.
It does not set `RunRecord.error`.

A call that never returns (network error, rate limit, or server
error after `num_retries`) is not a `no`. `verifier_decision` and
`verifier_reason` stay null, `RunRecord.error` is the failure, and
nothing is inserted. The same rule applies to a failed agent call
and a failed diagnosis call: `error` is set and no row is written.

Both fields are copied onto the `RunRecord`. `write_check` logs the
review of the entry being saved. `read_check` logs the review of the
retrieved hint. The other two policies leave both fields null.

## Patch

After the agent stops, from the temporary checkout:

- `git diff --binary <base_commit>` for tracked changes, including
  changes the agent committed on top of `base_commit`.
- For each untracked, non-ignored file, a new-file unified diff.

`files_touched` is the repo-relative paths in that diff, in diff
order, without duplicates. The index is left as the agent left it.
No commit is created by the runner. A diff that is empty or only
whitespace is an empty patch: nothing is saved, and the diagnosis
call does not run.

`error` is null when the agent submitted and every model call that
the policy required succeeded. `LimitsExceeded`, `TimeExceeded`,
`RepeatedFormatError`, a model call that still failed after
`num_retries`, or any other exception sets `error` to that status
or exception text. The record still includes whatever diff is on
disk. A model-call failure does not insert a memory row.

## CLI

```
python -m memverify.run --task PATH --policy POLICY --config PATH
                        [--db PATH] [--seed N] [--out PATH]
```

`--task` is a `Task` JSON file. `--policy` is required and must be
one of the four names. The command prints the `RunRecord` JSON to
stdout and, with `--out`, writes the same JSON to that path. It exits
0 when `error` is null and 1 when `error` is set. It does not decide
`resolved`. `--db` defaults to `memory.db_path`. Repeated CLI runs
with the same database append to that stream.

`run_stream(tasks, policy, db_path, config)` runs more than one task.
`tasks` is a sequence of task JSON paths, or `(Task, task_path)`
pairs. It sorts by `created_at`, then by `id`, deletes any file
already at `db_path`, and calls `run_one` in that order on the new
store. It returns the `RunRecord` list. It does not set `resolved`.

## Mock task

`tasks/mock_mean/` is a local fixture. No Docker image.
`docker_image` in `task.json` is null.

- `task.json` is a `Task`. `workdir` is `repo`, resolved next to the
  task file.
- `repo/` is a git checkout of `mock-mean` at `base_commit`.
- `repo/mean.py` implements `mean`. The empty-input guard is correct.
  The returned value subtracts 1 after dividing, so
  `mean([1.0, 2.0, 3.0])` is `1.0`.
- `repo/test_mean.py` is a unittest. `test_mean_of_three` fails.
  `test_mean_of_empty_is_an_error` passes.

The test file is the visible reproduction, the way a SWE-bench issue
comes with a failing public check. It is not a hidden scorer.
`RunRecord.resolved` stays null if this task is run.

Check the fixture from the repo directory:

```
python -m unittest test_mean.py
```

`test_mean_of_three` fails. That is the expected state of the
checkout.

`tasks/mock_mean_2/` is a second local fixture for the same `repo`
name, `mock-mean`, with a later `created_at`. Its bug is different:
`mean` uses floor division, so `mean([1.0, 2.0])` is `1.0` instead
of `1.5`. The empty-input guard is still correct.
`test_mean_of_two` fails. `docker_image` is null. A stream of the
two tasks can retrieve the first entry while solving the second.

## Tests

Tests must not call a real LLM and must not construct `LitellmModel`.
Stage 4 supplies a fake client with scripted completions
(`text`, `tokens_in`, `tokens_out`). Logged dollar amounts are
computed from those token counts and the prices in the test config.
The fake client does not supply a dollar amount. The tests cover:

- each policy's inject / drop / save decision, including a verifier
  reply that is not valid JSON, and the resulting `hint_injected`
  and `entry_saved` values
- a model call that raises: `error` is set, the verifier decision
  stays null, and no row is inserted
- retrieval: a later `created_at` is invisible, a different `repo` is
  invisible, and the top-1 BM25 hit is the one injected
- a two-entry corpus where the entry that shares rare words with the
  query ranks first, and a query that scores 0 against every entry
  is not a hit

The fake client is a test double. It is not another logged object.

## Out of scope

- The scorer that sets `resolved`.
- A dataset loader beyond the two mock tasks.
- A working Docker environment. `environment.kind: docker` stays a
  stub until real task images show where the repo lives and how to
  read the diff and `file_contents`.
- Training, fine-tuning, or a leaderboard.
- Editing mini-swe-agent itself.

## Package layout

```
memverify/types.py          stage 1  Task, MemoryEntry, RunRecord
memverify/agent.py          stage 2  mini-swe-agent wrapper, patch, diagnosis
memverify/memory.py         stage 3  SQLite and BM25
memverify/verifier.py       stage 3  one-shot JSON verifier
memverify/policies.py       stage 3  the four policies
memverify/run.py            stage 3  orchestration, run_stream, stage 4 CLI
tests/                      stage 4
configs/default.yaml        stage 2, matching the schema above
tasks/mock_mean/            stage 1
tasks/mock_mean_2/          stage 4  later task, same repo name, different bug
SPEC.md                     this file
README.md                   stage 4
```
