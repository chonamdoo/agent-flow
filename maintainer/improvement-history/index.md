# 개선 사이클 추이

버전마다 한 줄. 자세한 내용은 각 기록과 [런북](README.md)에 있다.

| 기준선 | 기록 | 기준 sha | 대상 사례 before → after | 채택한 변경 | 위험 검사 | 비용 | PR |
|---|---|---|---|---|---|---:|---|
| v0.3.19 → main | [v0.3.19](v0.3.19.md) | `4a979c4` | fixloop 2/8 → 8/8 (p=0.003) | `code-generation-discipline` 한 줄(slice plan을 가리키는 phase에서 결정 대조) | 통과(회귀 0) | $41.43 | [#386](https://github.com/chonamdoo/agent-flow/pull/386), [#387](https://github.com/chonamdoo/agent-flow/pull/387) |
| v0.3.19 → v0.3.20 | [v0.3.20](v0.3.20.md) | `4a979c4` → 게이트 `388c163` | fixloop Claude 2/8 → 8/8 (p=0.003), Codex 8/8 → 8/8; 리뷰 무효 19% → 0%; routing 블라인드 heldout 12/30 → 17/30; 게이트 리뷰 정답 0.955 → 1.000, review uncached 1.108 → 0.924 | slice-plan 규칙 after6, 리뷰 템플릿 판정 줄 1회, routing 한글 경계·어휘, auth 라우팅 오탐 수정, 리뷰어 중복 읽기 제거 | 통과(Claude·Codex 회귀 0, 3차 게이트 통과) | $112.39 | [#386](https://github.com/chonamdoo/agent-flow/pull/386), [#389](https://github.com/chonamdoo/agent-flow/pull/389), [#392](https://github.com/chonamdoo/agent-flow/pull/392), [#393](https://github.com/chonamdoo/agent-flow/pull/393), [#396](https://github.com/chonamdoo/agent-flow/pull/396), [#401](https://github.com/chonamdoo/agent-flow/pull/401), [#402](https://github.com/chonamdoo/agent-flow/pull/402) |
| v0.3.20 → v0.3.21 | [v0.3.21](v0.3.21.md) | `207a54c` → 게이트 `c1d9dfc` | 실전 run 결함 F13·F1·F9 재현 테스트 xfail → pass; 게이트 리뷰 정답 0.955 → 1.000, 오탐 0.017 → 0 | fix 라운드 순차 진입 제외, Linux 리뷰 조기 경고, 브랜치 이름 길이·점 | 게이트 2회 author uncached 1.069·1.052 > 1.05(backend #394 skill +9% 일정, 나머지 캐시 변동) → 이번 릴리스 한도 1.06(유지보수자 결정) | Codex 구독 | [#406](https://github.com/chonamdoo/agent-flow/pull/406) |
