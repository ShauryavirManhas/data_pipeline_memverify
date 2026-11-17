# Trace notes

Read 20 saved trajectories from the haystack pilot (`runs/pilot/traces`). These are the files mini-swe-agent's trajectory inspector loads. The sample is 8 resolved, 8 unresolved, and the 4 policies on `haystack-8619`, whose score never completed.

The agent is GLM 5.3 Flash. Each run stops at 40 steps or when it submits. The sandbox has no outbound network.

## What looks like a real debugging session

On the resolved tasks the agent does the usual thing. It lists the repo, opens the files named in the issue, writes a small reproduction, edits the source with a Python or heredoc rewrite, runs pytest, and only then submits.

- `haystack-8609` (`none` and `memory_no_check`). Removes the deprecated `converter` argument from the PDF converter, updates the tests, and submits. Both resolved.
- `haystack-8725` (`none` and `memory_no_check`). Removes `ChatMessage.from_function` and the `function` role, checks the release note, runs the dataclass tests, and submits. Both resolved. The memory run was shown an earlier fix, and that fix was about CSV splitting, a different issue. The agent did not apply that patch. It solved the chat-message issue from the code.
- `haystack-8879` (`none`). Finds the streaming tool-call bug in the OpenAI generator, edits it, runs the chat tests, and submits. The tests did not accept the patch (`resolved` false). The session itself is coherent. The memory run of the same task makes the same kind of edit and also fails.
- `haystack-8525` (`none` and `memory_no_check`). Builds a `CSVDocumentSplitter`, writes tests, and keeps iterating. Neither submits before the step limit, and neither patch resolves. The `none` run even notices that GitHub is unreachable and says it will implement from the issue text.
- `haystack-8940` (`memory_no_check`). Reproduces a pipeline output bug, edits `_write_component_outputs`, and reruns tests. It does not submit in time. The patch does not resolve.

## What does not look realistic

A few runs stop behaving like a programmer and start thrashing.

- `haystack-8489` (`memory_no_check`) makes no source edit. After the network block, it rummages through the pip HTTP cache for  the rest of the 40 steps and saves an empty patch. The `none` run of the same task does try to edit `haystack/tracing/tracer.py`, but it also dies on the step limit with a short patch that does not resolve.
- `haystack-8973` (`memory_no_check`) spends the run building `MagicMock` stubs for missing imports instead of changing the code that the issue is about. The patch is empty.
- `haystack-8799` (`none` and `memory_no_check`) finds a newer `haystack-experimental` checkout in the image's uv cache and copies drawing code out of it. That is a clever agent move, and it is also a leak: the image contains a later implementation of the feature. Both runs hit the step limit. The scorer still marked them resolved, so the copied code happened to pass.
- `haystack-8619`, all four policies. The agent edits several converters and writes a long patch (20 KB to 34 KB). Scoring then fails for every policy because the patch does not fit in a Modal command argument. The sessions look like real edits followed by an infrastructure stop, not like a model failure.

A few commands still mention `github.com`. None of them succeed. The network block is holding.

## What the hints are doing

When a hint is injected, it is appended after the issue, under "A similar past issue in this repo was handled like this." On `haystack-8725` that past issue is CSV splitting, and the agent ignores it. Top-1 retrieval is returning an earlier task in the same repo, not a fix for the same bug. `read_check` rejected the hint on `haystack-8619` (`verifier_decision` no), so that run never saw it. `write_check` had accepted the earlier patch.

Across these 20, a shown hint did not make the agent apply the stored diff. The runs that resolve are the ones that read the named files and edit them. The runs that fail either never edit, or edit and run out of steps before the tests pass.

## Consequence for the pilot

The policies can differ in the log (`hint_injected`, verifier yes or no) while the agent still does the same kind of work, because it treats a mismatched hint as optional text. A resolve-rate gap will be small unless a later hint is actually about the same files. The uv-cache copy on `haystack-8799` is the other thing to flag: some "solved" tasks may be solved from files already inside the image, not from memory and not from a fresh edit.
