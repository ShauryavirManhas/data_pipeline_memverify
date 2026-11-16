# Full run report

Paired tasks: 150. Pairs: 298.
Dropped incomplete pairs: 0. Dropped unresolved pairs: 14.

## Resolve rate and cost

Noise floor (`none` vs itself): 0.047 (7/148 tasks flip between seed 0 and seed 1).

| policy | resolve rate | 95% CI | mean cost | cost CI | difference vs none | difference CI |
| --- | ---: | --- | ---: | --- | ---: | --- |
| none | 0.282 | [0.214, 0.353] | 0.2499 | [0.2307, 0.2688] |  |  |
| memory_no_check | 0.245 | [0.182, 0.310] | 0.2637 | [0.2434, 0.2839] | -0.037 | [-0.067, -0.007] |
| write_check | 0.262 | [0.197, 0.329] | 0.2732 | [0.2532, 0.2933] | -0.020 | [-0.057, +0.017] |
| read_check | 0.265 | [0.199, 0.334] | 0.2470 | [0.2250, 0.2689] | -0.017 | [-0.050, +0.017] |

Cost components, tokens, and wall time (paired runs):

| policy | agent | diagnosis | verifier | cost per resolved | tokens in | tokens out | wall seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 0.2499 | 0.0000 | 0.0000 | 0.8866 | 1588640 | 23243 | 539 |
| memory_no_check | 0.2631 | 0.0006 | 0.0000 | 1.0764 | 1678878 | 23724 | 477 |
| write_check | 0.2712 | 0.0006 | 0.0014 | 1.0436 | 1736940 | 25223 | 497 |
| read_check | 0.2448 | 0.0006 | 0.0016 | 0.9318 | 1564105 | 24825 | 489 |

Stream-resampled difference CIs (robustness check):

- memory_no_check: [-0.076, -0.004]
- write_check: [-0.059, +0.020]
- read_check: [-0.049, +0.019]

Resolve rate by stream:

| stream | pairs | none | memory_no_check | write_check | read_check |
| --- | ---: | ---: | ---: | ---: | ---: |
| aws-cloudformation/cfn-lint | 50 | 0.000 | 0.000 | 0.000 | 0.000 |
| conan-io/conan | 60 | 0.400 | 0.350 | 0.350 | 0.350 |
| deepset-ai/haystack | 32 | 0.438 | 0.438 | 0.469 | 0.406 |
| instructlab/instructlab | 21 | 0.095 | 0.143 | 0.095 | 0.048 |
| keras-team/keras | 14 | 0.214 | 0.071 | 0.071 | 0.000 |
| matplotlib/matplotlib | 23 | 0.652 | 0.478 | 0.522 | 0.609 |
| pdm-project/pdm | 6 | 0.333 | 0.333 | 0.167 | 0.333 |
| pvlib/pvlib-python | 14 | 0.429 | 0.357 | 0.357 | 0.429 |
| pydata/xarray | 12 | 0.167 | 0.250 | 0.250 | 0.250 |
| reflex-dev/reflex | 24 | 0.167 | 0.167 | 0.250 | 0.250 |
| run-llama/llama_deploy | 16 | 0.250 | 0.125 | 0.375 | 0.312 |
| sphinx-doc/sphinx | 12 | 0.083 | 0.000 | 0.000 | 0.000 |
| streamlink/streamlink | 14 | 0.500 | 0.500 | 0.429 | 0.571 |

## Regressions

Split by the file-level stale flag.

- memory_no_check: regressions 12, improvements 4, net -8, no-hint flips 5, regression rate 0.167 (12/72)
  - stale: regressions 11, improvements 4
  - fresh: regressions 1, improvements 0
  - unknown: regressions 0, improvements 0
- write_check: regressions 13, improvements 7, net -6, no-hint flips 8, regression rate 0.176 (13/74)
  - stale: regressions 12, improvements 7
  - fresh: regressions 1, improvements 0
  - unknown: regressions 0, improvements 0
- read_check: regressions 0, improvements 0, net 0, no-hint flips 21, regression rate 0.000 (0/2)
  - stale: regressions 0, improvements 0
  - fresh: regressions 0, improvements 0
  - unknown: regressions 0, improvements 0

Same counts split by `stale_hunks` instead of file-level `stale`:

- memory_no_check: regressions 12, improvements 4, net -8
  - stale: regressions 9, improvements 4
  - fresh: regressions 3, improvements 0
  - unknown: regressions 0, improvements 0
- write_check: regressions 13, improvements 7, net -6
  - stale: regressions 10, improvements 5
  - fresh: regressions 3, improvements 2
  - unknown: regressions 0, improvements 0
- read_check: regressions 0, improvements 0, net 0
  - stale: regressions 0, improvements 0
  - fresh: regressions 0, improvements 0
  - unknown: regressions 0, improvements 0

## Hint staleness

Retrieved hints in the paired set: 783.
- File-level stale: 0.978 (766/783).
- Hunk-level stale: 0.940 (736/783).
- Remembered patch still applies: 0.070 (55/783).

## Verifier

read_check calls with a hint: 255. Used for rates: 255.
- Catch rate (stale and decision no): 0.980 (245/250).
- False-drop rate (fresh and decision no): 1.000 (5/5).
- Same rates with parse failures removed: catch 0.980 (245/250), false-drop 1.000 (5/5) (255 calls).
- Parse failures: 0.000 (0/255).
- write_check rejection rate: 0.033 (8/244).
- write_check retrieved hints that are file-stale: 0.971 (237/244).
- write_check wasted saves (saved, never retrieved): 0.472 (126/267). Rejected calls, counted separately: 8.

## Spend ledger

Total recorded by the runner: $503.4655.
