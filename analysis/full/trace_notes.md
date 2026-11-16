# Full-run trace notes

These notes sit next to `analysis/full/report.md`. They do not replace the 20 haystack pilot notes in `analysis/trace_notes.md`.

The question here is the hint-linked outcome changes: a paired run where `none` and another policy disagree, and the other policy actually injected a hint. Flips with `hint_injected` false are nondeterminism and are not in this list. Trace files are one per task and policy under `runs/full/traces`. A later seed overwrites the earlier seed's file, so a trace is the last seed that wrote it, not both.

## What the paired log says

Hint-linked changes, counted the same way as the report:

- `memory_no_check`: 12 regressions, 4 improvements. Eleven of the regressions are file-stale hints.
- `write_check`: 13 regressions, 7 improvements. Twelve of the regressions are file-stale hints. The verifier had already said yes on most of the hints that were shown.
- `read_check`: 0 regressions and 0 improvements. It injected a hint on 2 of the runs where `none` resolved, and those two did not change the outcome. The 21 outcome flips on this policy had no hint.

So a shown hint is associated with more losses than gains, and `read_check` almost never shows one. That matches the report: catch rate 245/250 on stale hints, and a false-drop on all 5 fresh hints.

## What the trajectories show

The hint text is in the prompt under "A similar past issue in this repo was handled like this." On the trajectories checked, the agent does not paste that patch in as its edit. It still lists files, edits, and either submits or hits the step limit.

- `haystack-8799`, `memory_no_check`, seed 1, is an improvement in the paired table. The trace hits the step limit with the hint present. The pilot already found this task copying a later `haystack-experimental` out of the image uv cache, and the scorer marked that copy resolved. This improvement is not evidence that the hint fixed the issue.
- `matplotlib-29249`, `memory_no_check`, submits with the hint in the prompt. `none` resolved and this policy did not, so it is one of the 12 memory regressions. The session is a normal edit-and-submit, not a dump of the stored diff.
- `conan-17123`, `memory_no_check`, seed 1, also submits with the hint present and is a regression against `none`.
- `haystack-8799`, `read_check`, has no hint text in the prompt. The trace hits the step limit. That is the usual `read_check` result: the verifier said no, so the agent never saw the remembered fix.

Across these, a stale hint is extra text in the prompt. The runs that lose to `none` still look like debugging sessions. They do not look like the agent applying the remembered patch and getting it wrong in an obvious way.
