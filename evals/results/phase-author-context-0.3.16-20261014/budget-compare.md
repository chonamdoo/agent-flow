## phase_budget

9 profile × mode combinations, `layer` condition, bytes/4.

| section | before | after | change |
|---|---:|---:|---:|
| author phase envelopes + required reads | 1,204k | 1,214k | +0.8% |
| multi-review · architecture-review controller | 34k | 34k | +0.0% |
| reviewer subprocesses | 2,993k | 3,009k | +0.5% |
| total | 4,233k | 4,258k | +0.6% |

Per combination: +0.4% (flutter:clean) to +0.8% (python:local).

### Findings
- total bytes grew in `baseline`: 17,761,945 → 17,862,316
- total bytes grew in `committed`: 17,839,910 → 17,940,281
- total bytes grew in `layer`: 16,933,321 → 17,033,692
- total bytes grew in `none`: 14,773,362 → 14,873,733
- total bytes grew in `planned`: 15,440,455 → 15,540,826
