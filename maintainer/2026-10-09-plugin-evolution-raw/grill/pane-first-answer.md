아니요. 형식상 장치는 동작하지만, 실제로 사용자 인터뷰를 했는지는 아무것도 확인하지 않습니다. 그리고 실제 run에서 인터뷰를 건너뛴 기록이 있습니다.

## 지금 확인하는 것과 확인하지 않는 것

- **실행 방식:** `HostedAdapter.execute`는 프롬프트를 출력만 하고 `False`를 반환합니다. 인터뷰와 artifact 작성은 전부 host 에이전트에게 맡겨집니다.
- **유일한 게이트:** `## Completion Gate` 아래에 마커 4줄(`domain-grill: complete`, `shared_understanding: reached` 등)이 있는지만 봅니다. 마커 형식 검사 자체는 테스트(`test_runner_smoke.py`, `test_cli.py`)로 잘 막혀 있습니다.
- **스킬 읽기 확인 없음:** `skill_markers_enforced()`는 `phase_id in CODE_PHASES`일 때만 참입니다. 그래서 실제 프롬프트에도 "This phase has no skill read gate"라고 나옵니다. `grilling/SKILL.md`를 읽었는지 아무도 모릅니다.
- **멈춤 없음:** `domain-grill`에는 `pause_after`가 없습니다(`prd`에는 있습니다). 반면 `agent-flow` SKILL은 "artifact를 쓰면 `next_command`를 실행하라"고 지시합니다. grilling 스킬의 "사용자 답을 기다려라"와 이 지시가 충돌하고, 어느 쪽도 강제되지 않습니다.

## 현재 소스로 직접 돌려본 결과

임시 프로젝트에서 `agent-flow run ... --workflow full-feature`를 실행했습니다. 프롬프트는 정상 출력됐습니다. 이어서 질문과 답이 하나도 없이 마커 4줄만 있는 7줄짜리 `domain-grill.md`를 쓰고 `agent-flow continue`를 실행했더니 rc=0으로 끝나고 `product-brief`로 넘어갔습니다. 테스트에 쓴 임시 디렉터리는 삭제했습니다.

## 실제 run 기록

| run | 시작부터 domain-grill 통과까지 | 인터뷰 흔적 |
|---|---|---|
| web_qa_skill (Codex, 10-02) | 약 36초 | 질문 0개, grilling 스킬 파일을 한 번도 읽지 않음. phase 프롬프트를 보기도 전에 artifact부터 쓰려다 경로가 없어 한 번 실패함 |
| chatbot (08-27) | 약 2분 | artifact 근거가 "기존 대화에서 검토"뿐 |
| kroute (09-04) | 약 4.5분 | artifact에 질문·답 기록 없음 |

web_qa 세션에서는 사용자의 "승인" 한마디(run 시작 전 계획 승인) 뒤에 사용자 메시지가 더 없었습니다. 그런데도 에이전트가 PRD pause까지 `--approve <hash>`로 스스로 통과시켰습니다. 사람이 확인해야 하는 지점을 host가 스스로 통과시킬 수 있다는 점에서 같은 문제입니다.

Claude Code 기록에서는 domain-grill 단계가 들어 있는 세션을 찾지 못해서 Claude 쪽 동작은 직접 보지 못했습니다. 게이트가 같으니 같은 위험이 있을 것으로 추정합니다.

## 고치는 방법 (결정이 필요합니다)

1. **프롬프트만 보강:** 이 단계는 대화형이라고 명시합니다. 첫 라운드 질문을 출력하고 턴을 끝내게 합니다. 이전 대화에서 이미 합의했다면 요약을 보여주고 사용자 확인을 받게 합니다. artifact에는 `## Rounds`(질문·답 원문)를 필수 마커로 추가합니다. 비용은 낮지만 에이전트가 스스로 마커를 써서 통과할 수 있다는 점은 그대로입니다.
2. **훅으로 증거 남기기:** `UserPromptSubmit` 훅이 사용자 턴을 기록하게 합니다(`skills-read.jsonl`과 같은 방식). runner는 domain-grill에 들어온 뒤 사용자 턴이 하나도 없으면 진행을 막습니다. 에이전트가 증거를 꾸며내기 어렵습니다. 다만 Codex 훅이 사용자 입력 이벤트를 주는지는 먼저 확인해야 합니다.

어느 쪽이든 "이미 합의된 경우"에는 요약과 확인 1회로 끝내는 경로를 따로 정해야 합니다. 그렇지 않으면 매번 인터뷰를 억지로 하게 됩니다.