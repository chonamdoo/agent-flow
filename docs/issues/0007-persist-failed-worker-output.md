---
label: needs-triage
type: AFK
---

# Persist failed worker output as run evidence

## What to build

죽거나 타임아웃된 워커의 마지막 출력과 종료 상태를 run artifact로 남긴다.

근거는 `docs/xirp-chirp-teardown-and-agent-flow-gap.md` §4.5다. Xirp는 tmux
`pane-died` hook으로 죽는 순간 `capture-pane -p -J -S -100`과 `#{pane_dead_status}`를
파일로 떨어뜨린 뒤 세션을 정리한다. 진단 증거를 잃지 않는다.

agent-flow의 `subprocess_pool.py`는 `_kill_process_tree`, `_bounded_reap`,
`_wait_for_provider(timeout)`으로 워커를 회수한다. 회수 시점에 마지막 출력이
영구 저장되는지는 확인되지 않았다(`subprocess_pool.py:42-48`의 `SubprocessJob` /
`SubprocessResult` 필드 정의 미확인).

저장되지 않고 있다면 `implement` phase가 강제하는 `test-run-evidence: verified|unavailable`
계약과 충돌한다. 증거를 요구하는 워크플로가 죽은 워커의 증거는 잃는 셈이다.
그 상태에서 `fix-loop`는 로그 대신 추측으로 시작한다.

## Acceptance criteria

- [x] `SubprocessResult`가 실패·타임아웃 시 마지막 출력과 종료 상태를 담는다.
- [x] 실패한 job 하나당 run artifact 파일 하나가 남고 경로가 결정적이다.
- [x] 타임아웃(`timed_out`)과 비정상 종료(`returncode != 0`)를 구분해 기록한다.
- [x] `_kill_process_tree`로 강제 종료된 경우에도 그 시점까지의 출력이 남는다.
- [x] 이미 저장하고 있었다면 그 사실을 확인 근거와 함께 기록하고 이 issue를 닫는다.

## Resolution

확인일 2026-08-18. 실패 워커의 출력은 이미 저장되고 있었다.

| 확인 대상 | 근거 |
|---|---|
| 필드 정의 | `subprocess_pool.py:51-59` — `stdout` `stderr` `returncode` `timed_out` `error` |
| 강제 종료 후 출력 | `subprocess_pool.py:126-133` — `_kill_process_tree`/`_bounded_reap` 뒤 `finally`에서 읽는다. 자식이 파이프가 아니라 `launch.scratch/stdout` 파일로 직접 쓰므로 SIGKILL 뒤에도 남는다 |
| 실패 job artifact | `multi_review.py:594-605` — 성공 여부로 걸러내지 않는다. 경로는 `validate_run_artifact_target`이 검증한 `job.output_path` |
| 상태 구분 | `multi_review.py:704-720` — `status: OK\|TIMEOUT\|ERROR`, `returncode`, `## error`, `## stdout`, `## stderr` |

위 `What to build`가 말한 `implement` phase 증거 계약 충돌은 성립하지 않는다.
`subprocess_pool` 소비자는 `multi_review.py` 하나뿐이고(`adapters/hosted.py`는 타입만
import한다) `implement` 워커는 이 경로를 쓰지 않는다.

### 실제로 고친 것

`_run_one`이 출력 한계 초과로 `outcome`을 덮어쓰고 있었다. 덮으면 스스로 종료한
프로세스의 exit status가 `-1`이 되고, timeout으로 죽인 워커의 `timed_out`이 `False`가
되어 artifact가 `TIMEOUT`을 `ERROR`로 적었다. 대기 판정은 그대로 두고 한계 초과만
`output_limited`로 따로 들고 간다(`subprocess_pool.py:108-113`, `:147-151`).

두 경우 모두 폴링 간격(`_PROVIDER_POLL_S = 0.05`) 안의 경합에서만 나타난다.
`_wait_for_provider`가 종료 여부보다 한계 초과를 먼저 검사하므로(`:185-194`) 평소에는
한계 초과가 대기 루프에서 판정되고 `-1`이 정확하다. 그래서 결정적 재현 테스트는 없다.
회귀 확인은 `tests/test_worktree_isolation.py`의
`test_provider_output_is_bounded_for_sync_and_parallel_paths`와
`test_provider_timeouts_reap_sync_and_parallel_processes`다.

### 남긴 것

`providers/subprocess.py:986-990`의 `stream.read(limit)`은 8MB를 넘으면 앞을 남기고
뒤를 버린다. 실패 원인은 보통 뒤에 있다. 다만 그 경우의 실패 사유(한계 초과)는 `error`
필드에 이미 적히고, 8MB를 넘는 리뷰어 출력은 관측된 적이 없다. 별건으로 둔다.
