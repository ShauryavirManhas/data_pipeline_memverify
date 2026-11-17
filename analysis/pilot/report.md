# Pilot report

Paired tasks: 26. Pairs: 26.
Dropped incomplete pairs: 0. Dropped unresolved pairs: 2.

## Resolve rate and cost

| policy | resolve rate | 95% CI | mean cost | cost CI | difference vs none | difference CI |
| --- | ---: | --- | ---: | --- | ---: | --- |
| none | 0.269 | [0.115, 0.462] | 0.1401 | [0.1200, 0.1611] |  |  |
| memory_no_check | 0.192 | [0.038, 0.346] | 0.1490 | [0.1294, 0.1695] | -0.077 | [-0.231, +0.077] |
| write_check | 0.192 | [0.038, 0.346] | 0.1601 | [0.1380, 0.1842] | -0.077 | [-0.192, +0.000] |
| read_check | 0.308 | [0.154, 0.500] | 0.1326 | [0.1133, 0.1524] | +0.038 | [+0.000, +0.115] |

Stream-resampled difference CIs (robustness check):

- memory_no_check: [-0.143, +0.000]
- write_check: [-0.167, +0.000]
- read_check: [+0.000, +0.083]

## Regressions

- memory_no_check: regressions 3, improvements 1, net -2, no-hint flips 0, regression rate 0.500 (3/6)
  - stale: regressions 3, improvements 1
  - fresh: regressions 0, improvements 0
  - unknown: regressions 0, improvements 0
- write_check: regressions 1, improvements 0, net -1, no-hint flips 1, regression rate 0.167 (1/6)
  - stale: regressions 1, improvements 0
  - fresh: regressions 0, improvements 0
  - unknown: regressions 0, improvements 0
- read_check: regressions 0, improvements 0, net 0, no-hint flips 1, regression rate 0.000 (0/0)
  - stale: regressions 0, improvements 0
  - fresh: regressions 0, improvements 0
  - unknown: regressions 0, improvements 0

## Spend ledger

Total recorded by the runner: $20.1271.
