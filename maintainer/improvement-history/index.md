# 개선 사이클 추이

버전마다 한 줄. 자세한 내용은 각 기록과 [런북](README.md)에 있다.

| 기준선 | 기록 | 기준 sha | 대상 사례 before → after | 채택한 변경 | 위험 검사 | 비용 | PR |
|---|---|---|---|---|---|---:|---|
| v0.3.19 → main | [v0.3.19](v0.3.19.md) | `4a979c4` | fixloop 2/8 → 8/8 (p=0.003) | `code-generation-discipline` 한 줄(slice plan을 가리키는 phase에서 결정 대조) | 통과(회귀 0) | $41.43 | [#386](https://github.com/chonamdoo/agent-flow/pull/386), [#387](https://github.com/chonamdoo/agent-flow/pull/387) |
