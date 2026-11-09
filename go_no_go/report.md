# Go/no-go: SWE-bench-Live/SWE-bench-Live (lite)

Decision: **GO** (5/5 tasks passed both checks; rule: GO if at most 1 of 5 fails).

Ran on Shauryavirs-MacBook-Pro.local (Darwin arm64), backend `modal`, 2026-11-09T05:03:30+00:00.

| Task | Gold resolves | Empty fails (tests ran) | Pull s | Gold s | Empty s | Repo at /testbed | Shallow | Commits | Next base in image |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| conan-io__conan-17092 | yes | yes | 0.2 | 663.8 | 611.9 | yes | false | 8556 | yes |
| aws-cloudformation__cfn-lint-3749 | yes | yes | 34.4 | 73.8 | 72.0 | yes | false | 2846 | yes |
| deepset-ai__haystack-8489 | yes | yes | 129.8 | 44.4 | 57.9 | yes | false | 3703 | yes |
| matplotlib__matplotlib-28933 | yes | yes | 57.1 | 303.4 | 408.1 | yes | false | 51126 | yes |
| reflex-dev__reflex-4087 | yes | yes | 40.3 | 120.2 | 95.6 | yes | false | 1779 | yes |

## Failures

None.

## Answers for the handoff

- Where the repo lives: see the /testbed column (the harness runs everything in /testbed).
- Full git history: see Shallow and Commits. Staleness does not depend on this; it uses a host-side GitHub clone.
- Next base in image: `yes` means later commits are inside the image. The agent could `git log --all` its way to the future fix; the runner should restrict that (SWE-bench-Live protocol).
- Agent-edited test files: the scorer drops agent edits to files the test patch touches (the harness resets them anyway) and keeps edits to other test files.
