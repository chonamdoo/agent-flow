# karpathy/autoresearch PR 검토: 우리 루프에 가져올 기능 (2026-10-23)

- 대상: https://github.com/karpathy/autoresearch 의 PR 416개.
- 방법: 원본을 shallow clone하고, PR 머리 commit을 `git fetch --depth=1 origin '+refs/pull/*/head:refs/remotes/pr/*'`로 받았다. 제목, 바뀐 파일, `program.md` 등의 diff를 읽었다.
- 대부분은 모델 튜닝, 포크 소개, 하드웨어 포팅이다. 실험 루프 자체를 다루는 것은 20개쯤이다.

## 우리 루프(`tools/autoresearch/loop.py`)에 이미 있는 것

- 한 번에 한 편집, 숨은 고정 평가기, git ratchet(채택하면 commit을 남기고 아니면 reset)
- held-out은 기록하되 결정에 쓰지 않음
- patience 중단, 편집 범위 guard
- routing track에서 같은 점수면 더 짧은 쪽을 채택
- 모델 run 잡음 때문에 통과 수가 엄격히 더 많아야 채택

## 가져올 것

| # | 기능 | PR | 요지 |
|---|---|---|---|
| 1 | 잡음 바닥 | #560, #303 | 기준선을 두 번 돌려 σ를 잰다. |Δ| < 2σ면 방향과 무관하게 버린다 |
| 2 | 재실행 확인 | #218 | 좋아 보이는 변경은 한 번 더 돌린다. 사라지면 버린다 |
| 3 | 실험 기억 | #110, #417 | `insights.md`(최고 결과, 된 것, 안 된 것, 다음 가설)와 막다른 길 목록, 20회마다 전략 점검 |
| 4 | 축 기록·다양성 | #572, #110, #558 | results에 axis 열을 둔다. 같은 축은 연속 3회 금지. 5회 정체면 구조적 변경 |
| 5 | 가설·되돌림 | #336, #562 | 시도마다 가설 열과 "되돌리는 최소 변경"을 적는다 |
| 6 | 게이밍 감사자 | #407 (`.claude/agents/experiment-auditor.md`) | 점수가 오르면 별도 에이전트가 diff를 보고 진짜인지 게이밍인지 판정한다 |
| 7 | holdout 보호 hook | #407 (`.claude/hooks/protect_holdout.py`) | 제안자가 holdout 경로를 읽으면 막는다(지시 대신 강제) |
| 8 | Pareto 기록 | #303 | 비용 대 점수를 같이 본다. v2에서는 Pareto를 채택 규칙이 아니라 보고용으로만 쓴다 |
| 9 | 이전 확인 | #566 | 한 설정에서 이긴 것을 다른 설정에서 다시 확인한 뒤에만 승격한다 |
| 10 | 사전 검증 | #90 | 비싼 run 전에 싼 검사로 실패할 변경을 거른다 |

## 가져오지 않을 것

- simulated annealing(#563), pheromone 축 선택(#569) 같은 물리·생물 비유 탐색 전략(#554~#570). 잡음이 크고 n이 작은 우리 조건에서는 효과를 확인할 수 없다.
- 대시보드, 멀티 GPU, 포크 소개.
