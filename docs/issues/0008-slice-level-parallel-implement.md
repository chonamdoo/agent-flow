---
label: needs-triage
type: AFK
---

# Slice-level parallel implement

## What to build

의존 없는 slice를 각자 worktree에서 병렬로 `implement`한다.

지금 병렬 소비자는 `multi_review.py`(리뷰어 팬아웃)뿐이다. 그런데 인프라는 이미
완성돼 있다 — `provider_lease(capacity=...)` 슬롯 레지스트리, `worker_claim_lock`,
`DEFAULT_MAX_WORKERS = 8`, `worktree_creation_lock`, `plan_worktree(unique=...)`.
소비자가 리뷰 하나뿐인 상태다.

`slice-plan` phase가 이미 slice를 순서대로 쪼개고 각 slice가 "files expected to
change"를 선언한다. 이 선언을 **쓰기 범위**로 승격시키면 `assert_scopes_isolated`가
겹침을 사전에 거부할 수 있다. 그 함수는 이미 "겹치는 쓰기 범위는 양쪽이 격리를
선언할 때만 허용, glob 범위는 분리 증명 불가이므로 충돌로 간주"를 구현한다.

Xirp의 `chirp session new --depends-on`(부모/자식 큐)이 이 패턴의 실효성을 보여준다.
다만 Xirp는 스폰 트리거가 system prompt 산문이라 비결정적이다. 여기서는 트리거가
`slice-plan.md` 산출물이므로 결정성이 유지된다.

```
slice-plan.md → 의존 그래프 → 독립 slice N개
  → create_worktree(name=<slice-id>, unique=<worker-token>)
  → provider_lease 슬롯 안에서 병렬 implement
  → assert_scopes_isolated(scopes)로 쓰기 범위 겹침 사전 차단
  → 결정적 순서로 순차 병합
```

## Acceptance criteria

- [ ] `slice-plan` 산출물이 slice별 쓰기 범위를 기계 판독 가능한 형태로 선언한다.
- [ ] slice 간 의존 그래프를 산출물에서 유도하며 모델의 즉흥 판단에 의존하지 않는다.
- [ ] 독립 slice는 각자 worktree에서 병렬 실행되고 `provider_lease` 상한을 넘지 않는다.
- [ ] 쓰기 범위가 겹치는 slice 조합은 실행 전에 `assert_scopes_isolated`로 거부된다.
- [ ] glob 쓰기 범위는 분리 증명 불가로 간주되어 병렬 대상에서 제외된다.
- [ ] 병합 순서가 결정적이며 같은 slice-plan에 대해 재현된다.
- [ ] 각 worktree의 leader tripwire가 통과한다(`assert_leader_unchanged`).

## Blocked by

- `slice-plan` phase의 "files expected to change"를 서술 항목에서 선언 항목으로
  승격하는 스키마 결정.
