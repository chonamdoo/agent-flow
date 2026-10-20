# 개선 사이클 추이

버전마다 한 줄. 자세한 내용은 각 기록과 [런북](README.md)에 있다.

| 기준선 | 기록 | 기준 sha | 대상 사례 before → after | 채택한 변경 | 위험 검사 | 비용 | PR |
|---|---|---|---|---|---|---:|---|
| v0.3.19 → main | [v0.3.19](v0.3.19.md) | `4a979c4` | fixloop 2/8 → 8/8 (p=0.003) | `code-generation-discipline` 한 줄(slice plan을 가리키는 phase에서 결정 대조) | 통과(회귀 0) | $41.43 | [#386](https://github.com/chonamdoo/agent-flow/pull/386), [#387](https://github.com/chonamdoo/agent-flow/pull/387) |
| v0.3.19 → v0.3.20 | [v0.3.20](v0.3.20.md) | `4a979c4` | fixloop Claude 2/8 → 8/8 (p=0.003), Codex 8/8 → 8/8; 리뷰 무효 19% → 0%; routing 블라인드 heldout 12/30 → 17/30 | slice-plan 규칙 after6, 리뷰 템플릿 판정 줄 1회, routing 한글 경계·어휘 | 통과(Claude·Codex 회귀 0) | 측정 중 | [#386](https://github.com/chonamdoo/agent-flow/pull/386), [#389](https://github.com/chonamdoo/agent-flow/pull/389), [#392](https://github.com/chonamdoo/agent-flow/pull/392), [#387](https://github.com/chonamdoo/agent-flow/pull/387), [#393](https://github.com/chonamdoo/agent-flow/pull/393) |
