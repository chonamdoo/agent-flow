"""phase 경계 leader tripwire의 sweep 범위 해석. 소비자 셋이 공유하는 정본이다.

이 모듈이 따로 있는 이유는 실측된 사고다. 해석이 `runner`의 private 함수로
갇혀 있던 동안, 같은 정책이 필요한 다른 두 소비 지점(`multi_review.run_distribution`,
`cli` team claim)은 그 함수를 부를 수 없었고 그래서 실제로 배선이 빠졌다 —
프로젝트가 `tracked-only`를 선언해도 그 두 경로는 기본값 전수 sweep으로 돌아
막혔다. 특히 multi-review는 reviewer subprocess를 병렬로 돌리는 가장 긴 창이라
leader daemon이 gitignored 산출물을 건드릴 확률이 phase 중 가장 높다.

그래서 정책은 core에 둔다. `core`는 `cli`/`runner`를 import하지 않으므로 세
소비자가 모두 여기로 수렴할 수 있다.

해석은 **선언 파일 단위**다. 병합된 profile dict를 받지 않는다 — 병합은 어느
파일이 무엇을 선언했는지를 지우는데, 좁힘 판정에는 바로 그 provenance가 필요하다
(`_assert_declaration_is_tracked` 참조).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agent_flow.core.profiles import (
    active_profile_ids,
    project_profile_override_path,
    project_profile_path,
)
from agent_flow.core.worktree_isolation import (
    LEADER_SWEEP_ALL,
    LEADER_SWEEP_SCOPES,
    LEADER_SWEEP_TRACKED_ONLY,
    WorktreeIsolationError,
    git_safe,
)

_UNTRACKED_PATHSPEC = "did not match any file"


class LeaderTripwireDeclarationError(ValueError):
    """선언 자체가 틀렸다. 읽기 실패와 구분하려고 따로 둔다.

    이 둘은 응답이 반대다. 읽기 실패는 전수 sweep으로 접어야 하고(모르면 넓게
    본다), 잘못된 선언은 올려야 한다(프로젝트는 좁혔다고 믿는데 run은 계속
    전수로 도는 상태를 만들면 안 된다). `ValueError` 전부를 올리면 손상된
    `kit.json`의 `validate_safe_name` 오류까지 함께 올라가 그 구분이 사라진다.
    """


def leader_sweep_include_ignored(leader_root: Path | None) -> bool:
    """phase 경계 tripwire가 ignored 경로까지 훑는가. leader의 profile이 정한다.

    기본은 전수 sweep이다. 끄는 것은 탐지 범위를 좁히는 일이므로 프로젝트가
    명시적으로 선언해야 한다.

    선언을 **leader에서** 읽는다. 돌고 있는 체크아웃에서 읽으면, leader가 `all`을
    선언하고 있어도 워커가 자기 worktree의 `.agent-flow/profiles/`에 `tracked-only`를
    흘려 실효 값을 갈아치울 수 있다.
    """
    if leader_root is None:
        # 지킬 leader가 없다(leader에서 그대로 도는 실행). 좁힐 대상도 없다.
        return True
    declarations = leader_tripwire_declarations(leader_root)
    values = {value for _, _, value in declarations}
    if len(values) > 1:
        raise LeaderTripwireDeclarationError(
            "active profiles declare conflicting branching.leader_tripwire values: "
            + ", ".join(
                f"{profile_id}={value}"
                + (f" ({_leader_relative(leader_root, path)})" if path else " (default)")
                for profile_id, path, value in declarations
            )
        )
    if not values or values == {LEADER_SWEEP_ALL}:
        return True
    for _profile_id, path, _value in declarations:
        assert path is not None  # tracked-only는 선언이 있어야만 나온다
        _assert_declaration_is_tracked(leader_root, path)
    return False


def leader_tripwire_declarations(
    leader_root: Path,
) -> tuple[tuple[str, Path | None, str], ...]:
    """active profile마다 하나씩, `(profile id, 선언 파일, 실효 값)`.

    후보는 id당 두 개뿐이다: `<id>.yaml`(install이 덮는 배포 사본)과
    `<id>.local.yaml`(install이 손대지 않는 override). 목록이 profile 수에
    비례할 뿐 저장소마다 자라지 않는다.

    같은 id 안에서는 override가 이긴다. `_schema.yaml`이 "스칼라는 통째로
    대체한다"고 선언했으므로 그 자리에서만 다르게 병합하면 선언과 동작이 갈린다.

    **선언하지 않은 profile은 건너뛰지 않고 기본값 `all`로 센다.** 건너뛰면
    android+react-native처럼 stack이 둘 붙은 저장소에서 한쪽만 `tracked-only`를
    적었을 때 상충이 보고되지 않고 run 전체가 조용히 좁아진다 — 기본값 `all`을
    기대한 profile의 감시가 그 선언 하나로 꺼진다. 생략은 침묵이 아니라 `all`이다.

    값 검증을 여기서 하는 이유는 모르는 값을 기본값으로 접지 않기 위해서다.
    `traked-only` 같은 오타를 조용히 `all`로 읽으면 프로젝트는 껐다고 믿는데 run은
    계속 막히고, 반대로 접으면 켰다고 믿는 프로젝트의 탐지가 조용히 꺼진다.
    """
    found: list[tuple[str, Path | None, str]] = []
    for profile_id in active_profile_ids(leader_root, "auto"):
        declaration: tuple[Path, str] | None = None
        for path in (
            project_profile_override_path(leader_root, profile_id),
            project_profile_path(leader_root, profile_id),
        ):
            value = _declared_in_file(path)
            if value is None:
                continue
            if value not in LEADER_SWEEP_SCOPES:
                raise LeaderTripwireDeclarationError(
                    "profile branching.leader_tripwire must be one of "
                    f"{', '.join(LEADER_SWEEP_SCOPES)}: got {value!r} in {path}"
                )
            declaration = (path, value)
            break
        if declaration is None:
            found.append((profile_id, None, LEADER_SWEEP_ALL))
        else:
            found.append((profile_id, declaration[0], declaration[1]))
    return tuple(found)


def _declared_in_file(path: Path) -> object | None:
    if not path.is_file():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    branching = payload.get("branching")
    if not isinstance(branching, dict):
        return None
    return branching.get("leader_tripwire")


def _assert_declaration_is_tracked(leader_root: Path, declaration: Path) -> None:
    """좁히는 선언은 좁힌 뒤에도 보이는 자리에 있어야 한다.

    이것이 없으면 knob이 스스로를 승인한다. 선언이 읽히는 자리는 leader의
    `.agent-flow/profiles/`인데 그 디렉터리는 install이 gitignore에 넣고, 도구
    호출마다 도는 PostToolUse tripwire는 이미 부분 sweep이다. 즉 그 파일을 본
    적 있는 통제는 phase 경계의 전수 sweep 하나뿐이었다 — 워커가 dynamic shell로
    거기에 `tracked-only`를 심으면, 그 파일이 자신을 감시하던 유일한 눈을 끈다.

    그래서 경로를 예외 목록에 넣는 대신 규칙을 하나 세운다: **좁히는 선언을 담은
    그 파일이 git에 추적돼야 한다.** 그러면 선언 자체가 좁아진 sweep의 감시 대상
    안에 남고, 심는 행위는 tracked 변경으로 그대로 보고된다.

    판정 단위가 파일인 것이 이 규칙의 전부다. 디렉터리로 물으면
    (`git ls-files --error-unmatch <dir>`) 그 아래 **아무** 파일 하나가 추적되기만
    해도 통과한다 — 팀이 공유하려고 `android.local.yaml`을 추적하는 순간, 워커가
    흘린 `python.local.yaml`의 `tracked-only`가 그대로 효력을 갖는다. 실측으로
    확인한 우회로이므로 pathspec은 반드시 개별 파일이어야 한다.

    잔여 위험: index에만 올린(`git add -f` 후 커밋하지 않은) 선언도 통과한다.
    staging은 리뷰가 아니다. 그 창을 막는 것은 leader의 PostToolUse tripwire가
    staged 변경을 보고하는 것인데, unbound 세션에는 그 눈이 없다. 그래서 이
    규칙은 "리뷰를 강제한다"가 아니라 "선언을 감시 대상 안에 남긴다"이다.
    """
    relative = _leader_relative(leader_root, declaration)
    result = git_safe(
        "ls-files", "--error-unmatch", "-z", "--", relative, cwd=leader_root
    )
    if result.ok:
        return
    if _UNTRACKED_PATHSPEC not in (result.stderr or ""):
        # git이 대답하지 못한 것과 "추적되지 않았다"는 다르다. 접어서 한 메시지로
        # 내면 사용자가 있지도 않은 추적 문제를 고치려 든다.
        raise WorktreeIsolationError(
            "cannot read the leader index to verify the leader_tripwire declaration: "
            f"{(result.stderr or result.error or '').strip() or result.returncode}"
        )
    raise WorktreeIsolationError(
        "branching.leader_tripwire: tracked-only is declared in a file the narrowed "
        f"sweep could not see: {relative}. Track that exact file "
        "(git add -f <file>) so narrowing the watch stays inside the watched set, "
        "or leave the default 'all'."
    )


def _leader_relative(leader_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(leader_root))
    except ValueError:
        return str(path)


def leader_sweep_include_ignored_for(leader_root: Path | None) -> bool:
    """소비자용 진입점. 읽기 실패만 전수 sweep으로 접는다.

    "읽지 못함"을 "좁혀도 된다"로 번역하면 IO 실패 하나가 조용히 탐지 범위를 줄이고,
    그 축소는 아무 신호도 남기지 않는다. 그래서 fail-closed다.

    삼키는 것은 **읽기 실패뿐**이다. 잘못된 선언(오타·상충·감시 밖 선언)은 전용
    예외로 구분해 그대로 올린다 — 그것은 읽기 실패가 아니라 선언과 동작이 갈라지는
    실패이고, 접으면 프로젝트는 좁혔다고 믿는데 run은 계속 전수로 돈다.
    """
    try:
        return leader_sweep_include_ignored(leader_root)
    except (LeaderTripwireDeclarationError, WorktreeIsolationError):
        raise
    except Exception as exc:  # yaml 파싱·손상된 kit.json·미지의 profile·IO
        print(
            "agent-flow: profile을 읽지 못해 leader tripwire를 전수 sweep으로 둔다: "
            f"{exc}",
            file=sys.stderr,
        )
        return True
