from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.worktree_isolation import git_safe


SOURCE_SUFFIXES = {".gradle", ".kt", ".kts", ".java", ".swift", ".py", ".ts", ".tsx", ".js", ".jsx"}
IGNORED_PARTS = {
    ".agent-flow",
    ".git",
    ".gradle",
    ".idea",
    "build",
    "node_modules",
    "__pycache__",
    "__tests__",
    "tests",
    "test",
    "androidTest",
    "commonTest",
    "iosTest",
}
TEST_NAME_RE = re.compile(r"(^test_|[_-]test$|[_-]test\.|testcase|tests?$)", re.IGNORECASE)
CORE_FAMILY_SEGMENTS = {
    "data",
    "design-system",
    "designsystem",
    "domain",
    "navigation",
    "network",
    "permission",
    "platform",
    "resources",
    "ui",
}
PLACEHOLDER_RESERVED_SEGMENTS = {
    "src",
    "build",
    "gradle",
    ".gradle",
    "build.gradle",
    "build.gradle.kts",
}
# role id는 profile 간 공유 어휘다. 표를 모듈 상수로 두면 어떤 role이 gradle 의존
# 규칙을 갖고 어떤 role이 의도적으로 무제약인지 한자리에서 읽히고, 테스트가 profile의
# role 집합과 대조할 수 있다. if 사슬로 두면 role을 늘릴 때 규칙 누락이 조용히 통과한다.
FORBIDDEN_GRADLE_MODULES: dict[str, tuple[str, ...]] = {
    # 도메인이 Room 모듈을 보면 저장소 구현이 도메인 계약을 통과해 새어 들어온다.
    "core-domain": (":app", ":core:data", ":core:database", ":core:network", ":core:platform", ":core:navigation:impl", ":feature"),
    "core-data": (":app", ":feature"),
    # 선언된 방향은 `core:data -> core:database` 하나뿐이다. 역방향을 열어 두면
    # Room 모듈이 repository 구현을 통해 도메인 계약까지 되짚어 올라간다.
    "core-database": (":app", ":core:data", ":feature"),
    "feature-api": (":app", ":core:data", ":core:database", ":feature:<feature>:presentation"),
    "feature-presentation": (":app", ":core:data", ":core:database"),
    "navigation-api": (":app", ":core:navigation:impl", ":feature"),
}
REQUIRED_GRADLE_MODULES: dict[str, tuple[str, ...]] = {
    "core-data": (":core:domain:<context>",),
    "feature-presentation": (":feature:<feature>:api",),
    "navigation-impl": (":core:navigation:api",),
}
# gradle 의존 방향 제약을 두지 않기로 한 role. 표에 없는 것과 구분해서 적는다.
UNCONSTRAINED_GRADLE_ROLES: frozenset[str] = frozenset({
    "app-shell",
    "android-native",
    "core-ui",
    "core-design-system",
    "core-resources",
    "core-network",
    "core-platform",
    "core-platform-adapter",
    "core-permission",
})


@dataclass(frozen=True)
class Finding:
    path: str
    message: str


@dataclass(frozen=True)
class RoleMatch:
    role: dict[str, Any]
    captures: dict[str, str]
    pattern: str


def lint_project(
    root: Path,
    profile_id: str,
    files: list[str] | None = None,
    *,
    profile_root: Path | None = None,
) -> list[Finding]:
    profile = load_profile_payload(profile_id, profile_root or root)
    architecture = profile.get("architecture")
    if not isinstance(architecture, dict):
        return []
    roles = architecture.get("roles")
    if not isinstance(roles, list):
        return []
    candidates = files if files is not None else changed_files(root)
    normalized_candidates = normalized_candidate_files(candidates)
    if not architecture_lint_is_active(root, architecture, normalized_candidates):
        return []
    findings: list[Finding] = []
    managed_roots = architecture_managed_roots(roles)
    for rel_path in normalized_candidates:
        path = root / rel_path
        match = match_role(rel_path, roles)
        if match is None:
            if (
                managed_roots
                and is_managed_architecture_path(rel_path, managed_roots)
                and not is_root_gradle_config(rel_path)
            ):
                findings.append(Finding(rel_path, "path is outside profile architecture role mapping"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() and path.is_file() else ""
        findings.extend(validate_forbidden_tokens(rel_path, text, match.role))
        findings.extend(validate_package_suffix(rel_path, text, match.role, match.captures))
        findings.extend(validate_gradle_namespace(root, rel_path, match))
        findings.extend(validate_declared_modules(root, rel_path, match.role, match.captures))
        findings.extend(validate_gradle_dependencies(root, rel_path, match, roles))
        findings.extend(validate_pair(root, rel_path, roles, match))
    # 같은 `(path, message)`를 두 번 보고하지 않는다. 실제로 접히는 검사는
    # `validate_gradle_dependencies` 하나다 — 그 검사만 finding 경로를 모듈의
    # `build.gradle.kts`로 잡으므로, 같은 모듈의 소스 파일마다 다시 돌아도 좌표가
    # 같아져 한 줄로 겹친다. 실측(평면 Android 저장소, 소스 468개): raw 159 /
    # distinct 7 — 22배로 부풀면 운영자가 실제 위반 개수를 읽을 수 없다.
    #
    # `validate_gradle_namespace`·`validate_declared_modules`·`validate_pair`는 모듈
    # build 파일을 **읽지만** `rel_path`로 보고한다. 그래서 모듈 하나의 namespace 위반이
    # 소스 3개면 경로 3개로 남고 접히지 않는다 — 그게 의도다. 위반이 어느 파일에서
    # 걸렸는지가 사라지면 운영자가 고칠 자리를 잃는다.
    #
    # 검사별로 처방하지 않고 집계 지점에서 한 번 접는다. 검사를 더할 때 dedupe를 다시
    # 붙일 자리가 없어야 한다. `dict.fromkeys`는 최초 등장 순서를 지키므로 출력이
    # 계속 변경 파일 순서를 따라간다.
    return list(dict.fromkeys(findings))


def lint_profiles(
    root: Path,
    profile_ids: list[str],
    files: list[str] | None = None,
    *,
    profile_root: Path | None = None,
) -> dict[str, list[Finding]]:
    requested_profile_ids = list(profile_ids)
    candidates = normalized_candidate_files(files if files is not None else changed_files(root))
    profile_ids = expanded_lint_profile_ids(requested_profile_ids, candidates)
    source_root = profile_root or root
    if len(profile_ids) <= 1:
        return {
            profile_id: lint_project(
                root,
                profile_id,
                files=files,
                profile_root=source_root,
            )
            for profile_id in profile_ids
        }
    contexts = {
        profile_id: profile_lint_context(profile_id, source_root)
        for profile_id in profile_ids
    }
    android_is_supplemental = (
        "react-native" in requested_profile_ids
        and "android" not in requested_profile_ids
        and "android" in profile_ids
    )
    selected: dict[str, list[str]] = {profile_id: [] for profile_id in profile_ids}
    for rel_path in candidates:
        relevant_profiles = [
            profile_id
            for profile_id, context in contexts.items()
            if not (
                android_is_supplemental
                and profile_id == "android"
                and rel_path != "android"
                and not rel_path.startswith("android/")
            )
            and context_path_is_relevant(rel_path, context)
        ]
        if not relevant_profiles:
            fallback = first_profile_with_roles(profile_ids, contexts)
            if fallback:
                relevant_profiles = [fallback]
        for profile_id in relevant_profiles:
            selected[profile_id].append(rel_path)
    return {
        profile_id: lint_project(
            root,
            profile_id,
            files=profile_files,
            profile_root=source_root,
        )
        for profile_id, profile_files in selected.items()
    }


def inactive_lint_profile_ids(
    root: Path,
    profile_ids: list[str],
    candidates: list[str],
    *,
    profile_root: Path | None = None,
) -> list[str]:
    inactive: list[str] = []
    for profile_id in profile_ids:
        architecture = load_profile_payload(
            profile_id,
            profile_root or root,
        ).get("architecture")
        if not isinstance(architecture, dict):
            continue
        roles = architecture.get("roles")
        if not isinstance(roles, list) or not roles:
            continue
        if not architecture_lint_is_active(root, architecture, candidates):
            inactive.append(profile_id)
    return inactive


def unconfigured_lint_profile_ids(
    profile_ids: list[str],
    root: Path | None = None,
) -> list[str]:
    unconfigured: list[str] = []
    for profile_id in profile_ids:
        architecture = load_profile_payload(profile_id, root).get("architecture")
        if not isinstance(architecture, dict):
            unconfigured.append(profile_id)
            continue
        roles = architecture.get("roles")
        if not isinstance(roles, list) or not roles:
            unconfigured.append(profile_id)
    return unconfigured


def expanded_lint_profile_ids(profile_ids: list[str], candidates: list[str]) -> list[str]:
    expanded = list(profile_ids)
    if "react-native" in expanded and "android" not in expanded:
        if any(path == "android" or path.startswith("android/") for path in candidates):
            expanded.append("android")
    return expanded


def profile_lint_context(
    profile_id: str,
    root: Path | None = None,
) -> tuple[list[object], tuple[str, ...]]:
    profile = load_profile_payload(profile_id, root)
    architecture = profile.get("architecture")
    if not isinstance(architecture, dict):
        return ([], ())
    roles = architecture.get("roles")
    if not isinstance(roles, list):
        return ([], ())
    return (roles, architecture_managed_roots(roles))


def context_path_is_relevant(rel_path: str, context: tuple[list[object], tuple[str, ...]]) -> bool:
    roles, managed_roots = context
    if not roles:
        return False
    if is_root_gradle_config(rel_path):
        return True
    if match_role(rel_path, roles) is not None:
        return True
    return is_managed_architecture_path(rel_path, managed_roots)


def first_profile_with_roles(profile_ids: list[str], contexts: dict[str, tuple[list[object], tuple[str, ...]]]) -> str:
    for profile_id in profile_ids:
        roles, _managed_roots = contexts.get(profile_id, ([], ()))
        if roles:
            return profile_id
    return ""


def architecture_lint_is_active(root: Path, architecture: dict[str, Any], candidates: list[str]) -> bool:
    if architecture.get("strict_when_roots_present") is not True:
        return True
    activation_roots = architecture.get("activation_roots")
    if not isinstance(activation_roots, list):
        return True
    roots = tuple(
        item.strip("/")
        for item in activation_roots
        if isinstance(item, str) and item.strip("/")
    )
    if not roots:
        return True
    roles = architecture.get("roles")
    # 판정 기준은 하나다 — activation_root 아래에 놓인 role 패턴이 **실제로** 매치되는가.
    # 앞선 판정은 디렉터리 존재만 봤다. 평면 레이아웃 저장소는 `core/domain/` 아래가
    # `src/`뿐인데도 그 디렉터리가 있다는 이유로 strict 모드가 켜지고, 정작
    # `core/domain/<context>`는 아무것도 못 맞춰 변경 파일 전량이 미매핑 finding이
    # 됐다(실측 436개 중 182개). 존재는 채택의 증거가 아니다. 패턴이 맞는 것이 증거다.
    patterns = activation_role_patterns(roles if isinstance(roles, list) else [], roots)
    # 변경 후보를 먼저 본다 — 계약을 채택하는 그 커밋에는 아직 디스크에 디렉터리가 없다.
    # 후보가 activation_root 아래인지 따로 묻지 않는다. 패턴 자체가 그 아래에 있으므로
    # 매치되면 후보도 그 아래다. 같은 사실을 두 규칙으로 묻지 않는다.
    if any(
        match_pattern(candidate, pattern) is not None
        for candidate in candidates
        for pattern in patterns
    ):
        return True
    # 패턴이 하나도 없으면 여기서 False다. activation_roots가 role 표와 어긋나게 선언된
    # 경우인데, CLI는 그것을 "n/a"로 따로 찍으므로 무음 통과가 되지는 않는다.
    return any(activation_pattern_matches_on_disk(root, pattern) for pattern in patterns)


def activation_role_patterns(roles: list[object], activation_roots: tuple[str, ...]) -> tuple[str, ...]:
    """activation_root 자신이거나 그 아래에 놓인 role path 패턴. 채택 여부의 증거가 될 패턴들이다."""
    patterns: list[str] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        paths = role.get("paths")
        if not isinstance(paths, list):
            continue
        for pattern in paths:
            if not isinstance(pattern, str):
                continue
            normalized = pattern.strip("/")
            if not normalized or normalized in patterns:
                continue
            if root_path_is_active(normalized, activation_roots):
                patterns.append(normalized)
    return tuple(patterns)


def activation_pattern_matches_on_disk(root: Path, pattern: str) -> bool:
    """이 패턴 자리에 **소스를 가진** 모듈 디렉터리가 하나라도 있는가.

    증거는 두 겹이다. (1) 열거한 경로가 `match_pattern`을 통과하는가 — 예약 세그먼트
    판정을 여기서 새로 쓰면 role 매칭 규칙이 두 벌이 된다. (2) 그 경로 아래에
    `SOURCE_SUFFIXES` 파일이 하나라도 있는가.

    (2)가 이름 목록이 아니라 **성질**인 것이 핵심이다. 디렉터리 존재만 보면 Room의
    `room.schemaLocation` 기본 위치인 `core/domain/schemas/`(json만 있다)나 aar 보관
    디렉터리 `libs/`가 채택 증거로 통한다. 실측: 평면 저장소에 `core/domain/schemas/`
    하나만 있어도 strict가 켜지고 변경 파일이 전량 미매핑 finding이 됐다. 그런 이름을
    예외 목록으로 빼면 레이아웃이 하나 나올 때마다 목록이 늘고, 활성 조건이 어디서
    결정되는지 더는 한자리에서 읽히지 않는다. "소스가 있는가"는 빈 디렉터리와 산출물
    보관소를 전부 걸러내면서 실제 모듈은 항상 통과시키므로, 새 이름이 나와도 규칙이
    자라지 않는다. 반증 방향도 막혀 있다 — 소스가 있는데 비활성이 되려면 그 확장자가
    `SOURCE_SUFFIXES`에 없어야 하고, 그건 lint가 애초에 읽지 않는 파일이다.

    placeholder 자리만 `*`로 바꿔 그 깊이만 열거하므로 후보를 찾을 때 트리를 걷지
    않는다. 증거 탐색은 후보마다 첫 소스에서 즉시 멈추고 `IGNORED_PARTS` 아래로는
    내려가지 않는다.

    메모이즈하지 않는다. 이 판정은 변경 파일 루프 **밖**, profile당 한 번 불리므로
    파일 수에 비례하지 않는다(실측 436파일 저장소 전체 lint가 0.05초).
    `cached_gradle_modules`처럼 mtime으로 키를 잡을 수도 없다 —
    `src/features/<feature>/api`의 답은 `src/features`뿐 아니라 각 `<feature>`
    디렉터리의 내용에도 달려 있어서, 정적 접두사의 mtime만으로는 무효화가 안 되고
    조용히 낡은 False를 돌려준다. 이득 없는 캐시로 오답을 살 이유가 없다.
    """
    glob_pattern = "/".join(
        "*" if re.fullmatch(r"<[a-zA-Z][a-zA-Z0-9_-]*>", part) else part
        for part in pattern.split("/")
    )
    for path in root.glob(glob_pattern):
        if not path.is_dir():
            continue
        if match_pattern(path.relative_to(root).as_posix(), pattern) is None:
            continue
        if holds_source_file(path):
            return True
    return False


def holds_source_file(directory: Path) -> bool:
    """이 디렉터리 아래에 `SOURCE_SUFFIXES` 파일이 하나라도 있는가. 첫 증거에서 멈춘다.

    `IGNORED_PARTS`는 `build`/`node_modules`처럼 산출물·의존성 트리를 이미 알고 있다.
    그 안으로 내려가지 않는 이유는 비용이다 — 대상 저장소의 `build/`는 8천 파일이고,
    거기서 첫 `.kt`를 찾는 것은 채택의 증거도 아니다.

    같은 깊이의 파일을 다 본 뒤 하위로 내려간다. 모듈 루트의 `build.gradle.kts`가
    보통 첫 증거이므로 대개 iterdir 한 번으로 끝난다.
    """
    pending = [directory]
    while pending:
        try:
            entries = list(pending.pop().iterdir())
        except OSError:
            # 권한이나 경합으로 못 읽는 디렉터리는 증거가 없는 것으로 본다. 여기서
            # 예외를 올리면 필수 gate가 활성 판정 단계에서 죽는다.
            continue
        subdirectories: list[Path] = []
        for entry in entries:
            if entry.is_dir():
                # symlink는 따라가지 않는다. 순환 하나로 필수 gate가 멈춘다.
                if not entry.is_symlink() and entry.name not in IGNORED_PARTS:
                    subdirectories.append(entry)
                continue
            if entry.suffix in SOURCE_SUFFIXES:
                return True
        pending.extend(subdirectories)
    return False


def root_path_is_active(rel_path: str, activation_roots: tuple[str, ...]) -> bool:
    return any(rel_path == activation_root or rel_path.startswith(f"{activation_root}/") for activation_root in activation_roots)


def changed_files(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    tracked = git_safe(
        "diff", "--name-only", "-z", "--diff-filter=ACMRTUXB", "HEAD", "--",
        cwd=root,
        optional_locks=False,
    )
    tracked_outputs: list[str] = []
    if tracked.ok:
        tracked_outputs.append(tracked.stdout)
    else:
        staged = git_safe(
            "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB", "--",
            cwd=root,
            optional_locks=False,
        )
        unstaged = git_safe(
            "diff", "--name-only", "-z", "--diff-filter=ACMRTUXB", "--",
            cwd=root,
            optional_locks=False,
        )
        if not staged.ok or not unstaged.ok:
            details = [
                result.stderr.strip() or result.error or "git did not answer"
                for result in (tracked, staged, unstaged)
                if not result.ok
            ]
            raise ValueError(
                "git diff failed while discovering architecture lint candidates: "
                + "; ".join(details)
            )
        tracked_outputs.extend((staged.stdout, unstaged.stdout))
    untracked = git_safe(
        "ls-files", "-z", "--others", "--exclude-standard",
        cwd=root,
        optional_locks=False,
    )
    if not untracked.ok:
        detail = untracked.stderr.strip() or untracked.error or "git did not answer"
        raise ValueError(
            f"git ls-files failed while discovering architecture lint candidates: {detail}"
        )
    files = [
        item
        for output in (*tracked_outputs, untracked.stdout)
        for item in output.split("\0")
        if item
    ]
    return list(dict.fromkeys(files))


def normalized_candidate_files(files: list[str]) -> list[str]:
    normalized: list[str] = []
    for file_name in files:
        rel_path = file_name.replace("\\", "/").strip("/")
        if not rel_path or any(part in IGNORED_PARTS for part in rel_path.split("/")):
            continue
        if is_test_file(rel_path):
            continue
        if Path(rel_path).suffix not in SOURCE_SUFFIXES:
            continue
        normalized.append(rel_path)
    return normalized


def is_test_file(rel_path: str) -> bool:
    path = Path(rel_path)
    stem = path.stem
    if stem.endswith(("Test", "Tests", "Spec", "Specs")):
        return True
    return bool(TEST_NAME_RE.search(stem))


def match_role(rel_path: str, roles: list[object]) -> RoleMatch | None:
    matches: list[RoleMatch] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        paths = role.get("paths")
        if not isinstance(paths, list):
            continue
        for pattern in paths:
            if not isinstance(pattern, str):
                continue
            captures = match_pattern(rel_path, pattern)
            if captures is not None:
                matches.append(RoleMatch(role=role, captures=captures, pattern=pattern))
    if not matches:
        return None
    return max(matches, key=lambda match: pattern_specificity(match.pattern))


def pattern_specificity(pattern: str) -> tuple[int, int]:
    parts = [part for part in pattern.strip("/").split("/") if part]
    static_count = sum(1 for part in parts if not re.fullmatch(r"<[a-zA-Z][a-zA-Z0-9_-]*>", part))
    return (len(parts), static_count)


def match_pattern(rel_path: str, pattern: str) -> dict[str, str] | None:
    path_parts = rel_path.split("/")
    pattern_parts = pattern.strip("/").split("/")
    if len(path_parts) < len(pattern_parts):
        return None
    captures: dict[str, str] = {}
    for index, part in enumerate(pattern_parts):
        expected = pattern_parts[index]
        actual = path_parts[index]
        token = re.fullmatch(r"<([a-zA-Z][a-zA-Z0-9_-]*)>", expected)
        if token:
            if actual in PLACEHOLDER_RESERVED_SEGMENTS:
                return None
            captures[token.group(1)] = actual
            continue
        if expected != actual:
            return None
    return captures


def architecture_managed_roots(roles: list[object]) -> tuple[str, ...]:
    roots: set[str] = set()
    for role in roles:
        if not isinstance(role, dict):
            continue
        paths = role.get("paths")
        if not isinstance(paths, list):
            continue
        for pattern in paths:
            if isinstance(pattern, str):
                root = static_prefix_before_placeholder(pattern)
                if root:
                    roots.add(root)
                    family_parent = architecture_family_parent(root)
                    if family_parent:
                        roots.add(family_parent)
    return tuple(sorted(roots, key=lambda value: (-len(value), value)))


def static_prefix_before_placeholder(pattern: str) -> str:
    parts: list[str] = []
    for part in pattern.strip("/").split("/"):
        if re.fullmatch(r"<[a-zA-Z][a-zA-Z0-9_-]*>", part):
            break
        if part:
            parts.append(part)
    return "/".join(parts)


def architecture_family_parent(root: str) -> str:
    parts = root.split("/")
    if len(parts) <= 1:
        return ""
    if parts[-1].lower() in CORE_FAMILY_SEGMENTS:
        return "/".join(parts[:-1])
    return ""


def is_managed_architecture_path(rel_path: str, managed_roots: tuple[str, ...]) -> bool:
    return any(rel_path == root or rel_path.startswith(f"{root}/") for root in managed_roots)


def validate_forbidden_tokens(rel_path: str, text: str, role: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    forbidden = role.get("forbidden")
    if not isinstance(forbidden, list):
        return findings
    haystacks = [Path(rel_path).name, code_only(rel_path, text)]
    for token in forbidden:
        if not isinstance(token, str) or not token:
            continue
        if any(contains_forbidden_token(haystack, token) for haystack in haystacks):
            findings.append(Finding(rel_path, f"{role.get('id', 'role')} contains forbidden token {token}"))
    return findings

# 주석과 문자열 리터럴은 코드가 아니다. 거기 있는 토큰까지 위반으로 세면 `@media
# screen`, 문서 URL의 `#view`, 예시 코드 블록이 필수 pre-commit gate를 막는다 —
# 원래 보고된 70건과 같은 클래스다. 파서를 붙이는 대신 비-코드 구간을 공백으로
# 지운다. 길이를 유지하므로 뒤따르는 경계 판정이 그대로 성립한다.
#
# 결과를 다시 이 함수에 넣지 않는다. 마스킹된 텍스트는 더 이상 그 언어의 소스가
# 아니라(주석이 지워지며 따옴표 짝이 바뀐다) 2회차는 다른 답을 낸다.

# 문자열 문법은 언어마다 다르다. 한 벌로 뭉뚱그리면 없애려던 오탐이 다른 자리에
# 생기거나(`.py`의 `"${screen}"`에는 보간이 없다) 진짜 위반을 놓친다(Swift
# `"\(viewModel)"`은 코드다). 보간 방식을 확장자별로 가른다.
#   brace  - `${...}`만 (JS template literal)
#   dollar - `${...}`와 `$name` (Kotlin/Groovy 큰따옴표)
#   swift  - `\(...)`
#   fstring- `{...}`, 단 `f` 접두사가 붙은 리터럴에서만
JS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}
JSX_SUFFIXES = {".tsx", ".jsx"}
INTERPOLATION_STYLES = {
    ".ts": ("`", "brace"),
    ".tsx": ("`", "brace"),
    ".js": ("`", "brace"),
    ".jsx": ("`", "brace"),
    ".kt": ('"', "dollar"),
    ".kts": ('"', "dollar"),
    ".gradle": ('"', "dollar"),
    ".swift": ('"', "swift"),
    ".py": ("\"'", "fstring"),
}


@dataclass(frozen=True)
class Syntax:
    """확장자 하나에서 굳어지는 문법 규칙. 파일당 한 번만 만든다."""

    javascript: bool
    jsx: bool
    slash_comments: bool
    hash_comments: bool
    marks: str
    interpolating_marks: str
    style: str


def syntax_for(suffix: str) -> Syntax:
    # `.py`만 `#` 주석이고 `SOURCE_SUFFIXES`의 나머지는 전부 `//` 계열이다. 모르는
    # 확장자도 `//` 쪽으로 둔다 - 여기 오기 전에 이미 확장자로 걸러진다.
    javascript = suffix in JS_SUFFIXES
    hash_comments = suffix == ".py"
    interpolating_marks, style = INTERPOLATION_STYLES.get(suffix, ("", ""))
    return Syntax(
        javascript=javascript,
        jsx=suffix in JSX_SUFFIXES,
        slash_comments=not hash_comments,
        hash_comments=hash_comments,
        # backtick은 JS에서만 문자열이다. Kotlin에서는 escaped identifier라
        # 문자열로 보면 그 안의 위반을 놓친다.
        marks="\"'`" if javascript else "\"'",
        interpolating_marks=interpolating_marks,
        style=style,
    )


def code_only(rel_path: str, text: str, mask_strings: bool = True) -> str:
    syntax = syntax_for(Path(rel_path).suffix)
    out = list(text)
    index = 0
    length = len(text)
    # `text[index:]`로 꼬리를 뜨면 문자마다 파일 전체를 복사해 O(n^2)가 된다.
    # 필수 pre-commit gate라 실측 9.2MB에 9초였다 - `startswith(pat, index)`로 본다.
    # `regex_end` 실패는 줄 끝까지 훑는다. 한 줄에 그런 `/`가 여럿이면 이차식이
    # 되므로(실측 31KB 한 줄에 4.2초) 실패한 줄은 그 줄이 끝날 때까지 건너뛴다.
    # 실패는 "이 줄에 닫는 `/`가 없다"는 뜻이라 같은 줄의 뒤쪽도 정규식이 아니다.
    regex_blocked_until = -1
    while index < length:
        char = text[index]
        if char == "/" and syntax.javascript and index >= regex_blocked_until and is_regex_start(text, index, syntax):
            closed = regex_end(text, index)
            if closed != -1:
                if mask_strings:
                    blank_span(out, index + 1, closed)
                index = closed
                continue
            regex_blocked_until = line_end(text, index)
        if syntax.slash_comments and text.startswith("//", index) and not is_url_scheme_slash(text, index):
            index = blank_until(out, text, index, "\n", keep_terminator=True)
            continue
        if syntax.slash_comments and text.startswith("/*", index):
            index = blank_until(out, text, index + 2, "*/")
            continue
        if syntax.hash_comments and char == "#":
            index = blank_until(out, text, index, "\n", keep_terminator=True)
            continue
        if text.startswith(('"""', "'''"), index):
            # 삼중 따옴표는 보간을 따로 보지 않는다. 여러 줄 문자열이라 한 줄 가드가
            # 안 서고, 그 안의 보간까지 좇으면 스캐너가 파서가 된다.
            index = blank_until(out if mask_strings else None, text, index + 3, char * 3)
            continue
        if char in syntax.marks:
            # gradle 판독기는 문자열이 곧 데이터다(`project(":core:data")`). 주석만
            # 지우고 문자열은 남긴다.
            # 모듈 지정자 판정이 먼저다. `import a from'react'`의 여는 따옴표를
            # contraction으로 건너뛰면 닫는 따옴표가 여는 따옴표로 뒤집힌다.
            keep = not mask_strings or (syntax.javascript and is_module_specifier(text, index))
            if not keep and syntax.jsx and is_jsx_contraction(text, index):
                index += 1
                continue
            style = syntax.style if char in syntax.interpolating_marks else ""
            if style == "fstring" and not has_format_prefix(text, index):
                style = ""
            index = blank_string(None if keep else out, text, index, style, syntax)
            continue
        index += 1
    return "".join(out)


def blank_span(out: list[str], start: int, stop: int) -> None:
    for position in range(start, stop):
        out[position] = " "


# `//`는 문맥 없이 주석이 아니다. `https://x`의 스킴과 정규식 `/^https?:\/\//`의
# 이스케이프된 슬래시 뒤에 붙으면 그 줄 뒤쪽의 진짜 코드까지 지운다. 앞 글자만 보면
# `case KIND://메모`의 라벨까지 스킴이 되므로 실제 스킴만 목록으로 센다. 코드 위치의
# 맨 URL은 JSX 본문에서나 나오니 닫힌 집합으로 충분하다.
URL_SCHEMES = frozenset(
    {
        "http", "https", "ftp", "ftps", "file", "ws", "wss",
        "mailto", "data", "blob", "content", "market", "intent", "app", "about",
    }
)


def is_url_scheme_slash(text: str, index: int) -> bool:
    if index == 0:
        return False
    if text[index - 1] == "\\":
        return True
    if text[index - 1] != ":" or index < 2:
        return False
    return identifier_before(text, index - 1).lower() in URL_SCHEMES


def identifier_before(text: str, end: int) -> str:
    start = end
    while start > 0 and is_identifier_part(text[start - 1]):
        start -= 1
    return text[start:end]


# JS 문법상 식별자 문자 바로 뒤에는 문자열이 올 수 없다(`x'a'`는 SyntaxError).
# 그래서 JSX 본문의 `it's`는 문자열 시작이 아니다. 키워드 뒤는 예외다 -
# minified JS의 `case"x"`, `return'x'`, `get'name'()`이 그 모양이다.
JS_KEYWORDS_BEFORE_LITERAL = frozenset(
    {
        "case", "return", "typeof", "in", "of", "new", "delete", "void",
        "do", "else", "throw", "yield", "await", "instanceof", "default",
        "get", "set", "static", "async", "extends", "from", "import", "export",
    }
)
# 정규식이 올 수 있는 자리는 더 좁다. `from`/`get`/`export`는 흔한 변수·프로퍼티
# 이름이라 여기 넣으면 `const off = from / size / 2`의 나눗셈이 정규식이 된다.
JS_KEYWORDS_BEFORE_REGEX = frozenset(
    {
        "case", "return", "typeof", "in", "of", "new", "delete", "void",
        "do", "else", "throw", "yield", "await", "instanceof", "default",
    }
)


def is_jsx_contraction(text: str, index: int) -> bool:
    if text[index] != "'" or index == 0:
        return False
    if not is_identifier_part(text[index - 1]):
        return False
    if not is_identifier_start(text[index + 1 : index + 2]):
        return False
    return identifier_before(text, index) not in JS_KEYWORDS_BEFORE_LITERAL


# 정규식 리터럴 본문의 따옴표가 문자열을 열면 그 줄 나머지가 지워진다. 실측으로
# `s.replace(/'/g, '')` 한 줄이 뒤따르는 코드를 통째로 삼켰다. 나눗셈과 가르는
# 기준은 앞의 마지막 비공백 문자이거나, 그 앞 단어가 키워드인가다.
REGEX_START_PREFIX = frozenset("=(,:[!&|?+;{}>\n")
# JSX 본문의 `{a} / {b}`는 비율 표기지 정규식이 아니다. `.tsx`/`.jsx`에서 중괄호를
# 정규식 시작으로 보면 `<div>{n} / {m} <Screen /></div>`의 뒤쪽이 통째로 지워진다.
JSX_REGEX_START_PREFIX = REGEX_START_PREFIX - frozenset("{}")


def is_regex_start(text: str, index: int, syntax: Syntax) -> bool:
    if text.startswith(("//", "/*"), index):
        return False
    position = index - 1
    while position >= 0 and text[position] in " \t":
        position -= 1
    if position < 0:
        return True
    # 후위 증감 뒤는 나눗셈이다. `i++ / size`를 정규식으로 보면 뒤가 지워진다.
    if position > 0 and text[position - 1 : position + 1] in ("++", "--"):
        return False
    if is_identifier_part(text[position]):
        # `return /re/.test(s)`처럼 키워드 뒤는 값 자리다. 다만 `cfg.default / 2`의
        # 프로퍼티 이름은 키워드가 아니다 - 멤버 접근이면 나눗셈이다. `$`는 JS
        # 식별자 문자인데 `is_identifier_part`가 빼므로 `obs$in`도 여기서 걸린다.
        word = identifier_before(text, position + 1)
        if text[position - len(word) : position - len(word) + 1] in (".", "$", "#"):
            return False
        return word in JS_KEYWORDS_BEFORE_REGEX
    prefix = JSX_REGEX_START_PREFIX if syntax.jsx else REGEX_START_PREFIX
    return text[position] in prefix


def line_end(text: str, index: int) -> int:
    found = text.find("\n", index)
    return len(text) if found == -1 else found


def regex_end(text: str, start: int) -> int:
    """정규식 리터럴의 끝(플래그 포함) 다음 위치. 한 줄 안에서 못 닫으면 -1.

    패턴 본문은 데이터다. 코드로 남기면 `/Dto/` 같은 패턴이 위반으로 잡힌다.
    """
    position = start + 1
    length = len(text)
    in_class = False
    while position < length:
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == "\n":
            return -1
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            position += 1
            while position < length and is_identifier_part(text[position]):
                position += 1
            return position
        position += 1
    return -1


# Python 문자열 접두사는 닫힌 집합이다. 글자만 훑으면 `if"{x}"`의 `if`가 f 접두사로
# 통과해, 보간이 없는 문자열이 코드로 남는다.
FORMAT_PREFIXES = frozenset({"f", "F", "fr", "fR", "Fr", "FR", "rf", "rF", "Rf", "RF"})


def has_format_prefix(text: str, quote: int) -> bool:
    return identifier_before(text, quote) in FORMAT_PREFIXES


def blank_until(out: list[str] | None, text: str, start: int, terminator: str, keep_terminator: bool = False) -> int:
    end = text.find(terminator, start)
    stop = len(text) if end == -1 else min(end + len(terminator), len(text))
    if out is not None:
        for position in range(start, stop):
            if not (keep_terminator and text[position] == terminator):
                out[position] = " "
    return stop


# 모듈 지정자는 문자열이지만 코드 참조다. 지우면 `export * from './screens'` 같은
# 진짜 위반이 사라진다. 줄 전체를 보면 `export const CSS = '@media screen'`까지
# 코드로 남아 없애려던 오탐이 살아난다 - 따옴표 **바로 앞**만 본다. 그래야 여러 줄
# import(`} from './screens'`)와 `await import('./x')`도 함께 걸린다. JS에서만 쓴다 -
# Groovy `copy { from 'src/main/screens' }`까지 보존하면 그쪽이 오탐이 된다.
MODULE_SPECIFIER_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])(?:from|import|export)\s*$"
    r"|(?:^|[^A-Za-z0-9_$])(?:import|require)\s*\(\s*$"
)
MODULE_SPECIFIER_LOOKBEHIND = 32


def is_module_specifier(text: str, index: int) -> bool:
    return bool(MODULE_SPECIFIER_RE.search(text[max(0, index - MODULE_SPECIFIER_LOOKBEHIND) : index]))


def blank_string(
    out: list[str] | None,
    text: str,
    start: int,
    style: str,
    syntax: Syntax,
) -> int:
    """문자열 하나를 지나가며 `out`이 있으면 그 구간을 공백으로 만든다.

    보간식 안쪽은 코드로 남긴다. `"${viewModel.state}"`, `"\\(viewModel.state)"`,
    `f"{api_client.value}"`는 문자열 안에 있어도 코드다.
    """
    mark = text[start]
    length = len(text)
    position = start + 1
    # line continuation 판정은 문자열당 한 번이다. continuation마다 파일 끝까지
    # 다시 훑으면 O(n^2)가 된다 - 실측 24KB에 1.2초, 2배마다 4배였다.
    continues: bool | None = None
    while position < length:
        char = text[position]
        if char == mark:
            return position + 1
        # 짝이 없는 따옴표 하나가 파일 끝까지 지우면 진짜 위반이 통째로 사라진다.
        # backtick만 예외다 - 여러 줄 template literal이 정상 문법이다.
        if char in "\n\r" and mark != "`":
            return position
        if char == "\\":
            if style == "swift" and text[position + 1 : position + 2] == "(":
                closed = skip_balanced(out, text, position + 1, "(", ")", mark, style, syntax)
                if closed != -1:
                    position = closed
                    continue
            # 파일이 백슬래시로 끝나면 건너뛴 자리가 범위 밖이다. 여기서 멈추지
            # 않으면 필수 gate가 finding 대신 IndexError로 죽는다.
            if position + 1 >= length:
                return length
            skipped = escaped_newline_length(text, position)
            if skipped and mark != "`":
                # JS는 줄 끝 백슬래시가 합법적인 line continuation이다. 여기서
                # 끊으면 닫는 따옴표가 여는 따옴표로 뒤집혀 줄 나머지가 밀린다.
                # 다른 언어이거나 뒤에서 안 닫히면 짝 없는 따옴표로 본다.
                if continues is None:
                    continues = syntax.javascript and closes_later(text, position + skipped, mark)
                if not continues:
                    return position
                if out is not None:
                    for blank in range(position, position + skipped):
                        if text[blank] != "\n":
                            out[blank] = " "
                position += skipped
                continue
            if out is not None:
                out[position] = " "
                out[position + 1] = " "
            position += 2
            continue
        following = text[position + 1 : position + 2]
        if style in ("brace", "dollar") and char == "$" and following == "{":
            closed = skip_balanced(out, text, position + 1, "{", "}", mark, style, syntax)
            if closed != -1:
                position = closed
                continue
        if style == "dollar" and char == "$" and is_identifier_start(following):
            # Kotlin/Groovy는 중괄호 없는 `$name`도 보간이다. 이름 부분만 코드다.
            position += 1
            while position < length and is_identifier_part(text[position]):
                position += 1
            continue
        if style == "fstring" and char == "{":
            if following == "{":
                # `{{`는 리터럴 중괄호다. 보간이 아니다.
                if out is not None:
                    out[position] = " "
                    out[position + 1] = " "
                position += 2
                continue
            closed = skip_balanced(out, text, position, "{", "}", mark, style, syntax)
            if closed != -1:
                position = closed
                continue
        if out is not None:
            out[position] = " "
        position += 1
    return length


def escaped_newline_length(text: str, backslash: int) -> int:
    """`\\` 다음이 줄바꿈이면 그 이스케이프의 길이. 아니면 0."""
    if text[backslash + 1 : backslash + 2] == "\n":
        return 2
    if text[backslash + 1 : backslash + 3] == "\r\n":
        return 3
    return 0


def closes_later(text: str, start: int, mark: str) -> bool:
    """이 문자열이 뒤에서 실제로 닫히는가. 이스케이프를 건너뛰며 본다."""
    position = start
    length = len(text)
    while position < length:
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == mark:
            return True
        position += 1
    return False


def is_identifier_start(char: str) -> bool:
    return bool(char) and (char == "_" or (char.isascii() and char.isalpha()))


def is_identifier_part(char: str) -> bool:
    return char == "_" or (char.isascii() and char.isalnum())


def skip_balanced(
    out: list[str] | None,
    text: str,
    opener: int,
    open_char: str,
    close_char: str,
    mark: str,
    style: str,
    syntax: Syntax,
) -> int:
    """짝이 맞는 닫는 괄호 다음을 돌려준다. 문자열 안에서 못 찾으면 -1.

    못 찾았는데 파일 끝을 돌려주면 그 지점부터 마스킹이 통째로 꺼진다 - `"${"`
    하나가 뒤따르는 주석 전부를 코드로 만든다.

    보간식 안의 문자열은 바깥 문자열의 끝이 아니다. `"${orderDto.format("x")}"`에서
    안쪽 따옴표를 종료로 보면 앞부분의 `orderDto`까지 지워 위반을 놓친다. 다만 그
    안쪽 문자열과 주석은 여전히 코드가 아니므로 같이 지운다.

    실패하면 `out`을 건드리지 않는다. 먼저 지워 놓고 -1을 내면 호출자가 되돌릴 수
    없어, 안 닫힌 `/*` 하나가 파일 끝까지 공백으로 만든다.
    """
    # `.py` 보간식에는 주석이 없다. `//`는 floor division이다.
    slash_comments = style != "fstring"
    depth = 0
    position = opener
    length = len(text)
    spans: list[tuple[int, int]] = []
    regex_blocked_until = -1
    while position < length:
        char = text[position]
        if char in "\"'`":
            closed = skip_nested_string(text, position, mark)
            if closed == -1:
                return -1
            spans.append((position + 1, closed - 1))
            position = closed
            continue
        if char == "/" and syntax.javascript and position >= regex_blocked_until and is_regex_start(text, position, syntax):
            closed = regex_end(text, position)
            if closed != -1:
                spans.append((position + 1, closed))
                position = closed
                continue
            regex_blocked_until = line_end(text, position)
        if slash_comments and text.startswith("/*", position):
            closed = text.find("*/", position + 2)
            if closed == -1:
                return -1
            spans.append((position, closed + 2))
            position = closed + 2
            continue
        if slash_comments and text.startswith("//", position) and not is_url_scheme_slash(text, position):
            closed = text.find("\n", position)
            if closed == -1:
                return -1
            spans.append((position, closed))
            position = closed
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                if out is not None:
                    for span_start, span_stop in spans:
                        blank_span(out, span_start, span_stop)
                return position + 1
        elif char == "\n" and mark != "`":
            return -1
        position += 1
    return -1


def skip_nested_string(text: str, start: int, mark: str) -> int:
    """보간식 안의 문자열 하나를 지나 그 다음 위치를 돌려준다. 못 닫으면 -1."""
    nested = text[start]
    position = start + 1
    length = len(text)
    while position < length:
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == nested:
            return position + 1
        if char == "\n" and mark != "`":
            return -1
        position += 1
    return -1


def contains_forbidden_token(haystack: str, token: str) -> bool:
    # 원문에서 직접 찾는다. `haystack.lower()`로 접으면 `İ`처럼 소문자화가 길이를 바꾸는
    # 문자에서 인덱스가 밀려 경계 판정이 엉뚱한 문자를 보거나 IndexError로 게이트가 죽는다.
    pattern = re.compile(re.escape(token), re.IGNORECASE)
    position = 0
    # 겹치는 매치도 봐야 한다. `forbidden`은 사용자가 편집하는 값이라 앞 매치가 경계에서
    # 떨어졌을 때 그 안쪽의 유효한 험프를 건너뛰면 진짜 위반을 놓친다.
    while (match := pattern.search(haystack, position)) is not None:
        if is_token_boundary(haystack, match.start(), match.end()):
            return True
        position = match.start() + 1
    return False


def is_token_boundary(haystack: str, start: int, end: int) -> bool:
    """식별자 경계에서만 토큰을 인정한다.

    단순 부분문자열 비교는 `identity`를 `Entity`로, `Overview`를 `View`로 잡아 필수
    pre-commit gate가 오탐으로 커밋을 막는다. 반대로 대소문자만 구분하면 `CheckoutDTO`가
    `Dto`를 피해 진짜 위반을 놓친다. 그래서 단어가 새로 시작하는 자리와 단어가 끝나는
    자리만 경계로 본다.
    """
    previous = haystack[start - 1] if start else ""
    if previous.isalnum() and not starts_new_word(haystack[start:end], previous):
        return False
    return ends_at_boundary(haystack, end, haystack[start:end])


def starts_new_word(matched: str, previous: str) -> bool:
    head = matched[:1]
    if not head.isalnum():
        return True
    if not head.isupper():
        # `identity`의 `entity`, `Preview`의 `view`. 소문자 연속은 한 단어다.
        return False
    # 대문자 연속 안쪽은 험프가 아니다(`IDENTITY`의 `ENTITY`). 반대로 약어 뒤에 붙은
    # 혼합 대소문자 토큰은 별개 단어다(`APIView`, `UIViewController`의 `View`).
    return not previous.isupper() or not matched.isupper()


def ends_at_boundary(haystack: str, end: int, matched: str) -> bool:
    if ends_word(haystack, end, matched):
        return True
    # 복수형은 같은 타입을 가리킨다. `views.py`, `OrderDtosMapper`가 실제 위반 이름이다.
    # `Screenshot`은 `s` 다음이 소문자 `h`라 여전히 단어 중간이다.
    return any(
        haystack[end : end + len(plural)].lower() == plural
        and ends_word(haystack, end + len(plural), matched)
        for plural in ("s", "es")
    )


def ends_word(haystack: str, end: int, matched: str) -> bool:
    following = haystack[end : end + 1]
    if not following.isalnum():
        return True
    if not following.isupper():
        # 숫자와 한글은 단어를 잇지 않는다(`OrderDto2`, 주석의 `OrderDto를`).
        return not following.islower()
    # 전부 대문자인 매치 뒤에 대문자가 둘 더 이어지면 같은 대문자 런의 안쪽이다
    # (`SCREENSHOT`·`SCREENING_STATUS`의 `SCREEN`). `DTOMapper`처럼 대문자 하나
    # 뒤에 소문자가 오면 거기서 단어가 새로 시작한다.
    return not (matched.isupper() and haystack[end + 1 : end + 2].isupper())


def validate_package_suffix(rel_path: str, text: str, role: dict[str, Any], captures: dict[str, str]) -> list[Finding]:
    suffix = role.get("package_suffix")
    if not isinstance(suffix, str) or not suffix:
        return []
    if is_gradle_build_file(rel_path):
        return []
    package_name = package_from_source(text)
    if not package_name:
        return [Finding(rel_path, f"{role.get('id', 'role')} requires package declaration")]
    expected = suffix
    for key, value in captures.items():
        expected = expected.replace(f"<{key}>", package_segment(value))
    if f".{expected}." not in f".{package_name}.":
        return [Finding(rel_path, f"package {package_name} does not match role suffix {expected}")]
    return []


def validate_gradle_namespace(root: Path, rel_path: str, match: RoleMatch) -> list[Finding]:
    suffix = match.role.get("package_suffix")
    if not isinstance(suffix, str) or not suffix:
        return []
    build_file = role_build_file(root, match.pattern, match.captures, rel_path)
    if build_file is None:
        return []
    namespace = namespace_from_gradle(build_file)
    if not namespace:
        return []
    expected = suffix
    for key, value in match.captures.items():
        expected = expected.replace(f"<{key}>", package_segment(value))
    if not namespace.endswith(expected):
        return [Finding(rel_path, f"namespace {namespace} does not match role suffix {expected}")]
    return []


def validate_pair(root: Path, rel_path: str, roles: list[object], match: RoleMatch) -> list[Finding]:
    pair_id = match.role.get("pair_with")
    if not isinstance(pair_id, str) or not pair_id:
        return []
    if not match.captures:
        return []
    pair_role = next((role for role in roles if isinstance(role, dict) and role.get("id") == pair_id), None)
    if not isinstance(pair_role, dict):
        return []
    pair_paths = pair_role.get("paths")
    if not isinstance(pair_paths, list):
        return []
    for pattern in pair_paths:
        if not isinstance(pattern, str):
            continue
        concrete = pattern
        for key, value in match.captures.items():
            concrete = concrete.replace(f"<{key}>", value)
        if (root / concrete).exists():
            return []
    return [Finding(rel_path, f"{match.role.get('id', 'role')} requires paired role {pair_id}")]


def validate_declared_modules(root: Path, rel_path: str, role: dict[str, Any], captures: dict[str, str]) -> list[Finding]:
    expected = expected_modules(role, captures)
    if not expected:
        return []
    declared = declared_gradle_modules(root)
    if not declared:
        return []
    missing = [module for module in expected if module not in declared]
    return [Finding(rel_path, f"Gradle module {module} is not declared in settings") for module in missing]


def validate_gradle_dependencies(
    root: Path, rel_path: str, match: RoleMatch, roles: list[object]
) -> list[Finding]:
    build_file = role_build_file(root, match.pattern, match.captures, rel_path)
    if build_file is None:
        return []
    dependencies = gradle_project_dependencies(build_file)
    role_id = str(match.role.get("id", ""))
    findings: list[Finding] = []
    for module in forbidden_gradle_dependencies(role_id, match.captures):
        if module in dependencies or any(dep.startswith(f"{module}:") for dep in dependencies):
            findings.append(Finding(str(build_file.relative_to(root)), f"{role_id} has forbidden Gradle dependency {module}"))
    required = required_gradle_dependencies(role_id, match.captures)
    for module in required:
        # 이 표의 required 항목은 전부 **다른 role이 소유한 모듈**이다
        # (`core-data` → core-domain, `feature-presentation` → feature-api,
        # `navigation-impl` → navigation-api). 그 role을 선언하지 않은 profile에서
        # 그 모듈을 요구하는 것은 계층 위반이 아니라 표와 저장소 구조의 불일치다 —
        # 평면 레이아웃에서 `:feature:<f>:api`를 요구한 오탐이 실측으로 5건이었다.
        #
        # 판정 근거를 settings.gradle의 모듈 목록이 아니라 **role 선언**에 두는
        # 이유: 모듈이 지워졌다는 사실만으로 규칙이 꺼지면, 그 모듈을 없애고 의존을
        # 끊는 변경이 조용히 통과한다. role이 남아 있는 한 규칙도 남는다.
        if not role_owns_module(roles, module):
            continue
        if module not in dependencies:
            findings.append(Finding(str(build_file.relative_to(root)), f"{role_id} must depend on {module}"))
    return findings


def role_owns_module(roles: list[object], module: str) -> bool:
    """활성 profile의 어떤 role이 이 모듈을 소유한다고 선언했는가."""
    for role in roles:
        if not isinstance(role, dict):
            continue
        declared = role.get("modules")
        if not isinstance(declared, list):
            continue
        for pattern in declared:
            if isinstance(pattern, str) and module_pattern_matches(pattern, module):
                return True
    return False


def module_pattern_matches(pattern: str, module: str) -> bool:
    """`:feature:<feature>:api` 같은 placeholder 패턴이 이 모듈 좌표와 맞는가.

    placeholder는 세그먼트 하나를 받는다. 세그먼트 수가 다르면 다른 좌표다.
    """
    pattern_parts = pattern.split(":")
    module_parts = module.split(":")
    if len(pattern_parts) != len(module_parts):
        return False
    return all(
        (expected.startswith("<") and expected.endswith(">") and bool(actual))
        or expected == actual
        for expected, actual in zip(pattern_parts, module_parts)
    )


def expected_modules(role: dict[str, Any], captures: dict[str, str]) -> list[str]:
    modules = role.get("modules")
    if not isinstance(modules, list):
        return []
    expected: list[str] = []
    for module in modules:
        if not isinstance(module, str):
            continue
        concrete = replace_placeholders(module, captures)
        if "<" not in concrete and concrete not in expected:
            expected.append(concrete)
    return expected


def declared_gradle_modules(root: Path) -> frozenset[str]:
    """변경 파일마다 다시 읽지 않는다. per-file 루프 안에서 불린다."""
    settings = next((root / name for name in ("settings.gradle.kts", "settings.gradle") if (root / name).is_file()), None)
    if settings is None:
        return frozenset()
    stat = settings.stat()
    return cached_gradle_modules(settings, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=32)
def cached_gradle_modules(settings: Path, mtime_ns: int, size: int) -> frozenset[str]:
    text = code_only(settings.name, settings.read_text(encoding="utf-8", errors="replace"), mask_strings=False)
    # 일반 패턴이 `include ":x"`도 이미 잡는다.
    return frozenset(re.findall(r"['\"](:[A-Za-z0-9_:-]+)['\"]", text))


def role_build_file(root: Path, pattern: str, captures: dict[str, str], rel_path: str | None = None) -> Path | None:
    if rel_path and is_gradle_build_file(rel_path):
        path = root / rel_path
        if path.is_file():
            return path
    role_root = root / replace_placeholders(pattern, captures)
    for name in ("build.gradle.kts", "build.gradle"):
        path = role_root / name
        if path.is_file():
            return path
    return None


def gradle_project_dependencies(path: Path) -> set[str]:
    # 주석 처리된 `// implementation(project(":feature:home:presentation"))`을
    # 선언으로 세면 같은 gate가 주석 때문에 오탐을 낸다.
    text = code_only(path.name, path.read_text(encoding="utf-8", errors="replace"), mask_strings=False)
    dependencies = set(re.findall(r"project\(\s*['\"](:[A-Za-z0-9_:-]+)['\"]\s*\)", text))
    dependencies.update(re.findall(r"project\s+['\"](:[A-Za-z0-9_:-]+)['\"]", text))
    dependencies.update(
        re.findall(
            r"\bproject\(\s*path\s*(?:=|:)\s*['\"](:[A-Za-z0-9_:-]+)['\"]",
            text,
        )
    )
    dependencies.update(type_safe_project_dependencies(text))
    return dependencies


def type_safe_project_dependencies(text: str) -> set[str]:
    modules: set[str] = set()
    for match in re.finditer(r"\bprojects((?:\.[A-Za-z_][A-Za-z0-9_]*)+)", text):
        parts = [part for part in match.group(1).split(".") if part]
        if not parts:
            continue
        modules.add(":" + ":".join(parts))
        modules.add(":" + ":".join(gradle_accessor_segment_to_module(part) for part in parts))
    return modules


def gradle_accessor_segment_to_module(segment: str) -> str:
    kebab = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", segment)
    return kebab.replace("_", "-").lower()


def forbidden_gradle_dependencies(role_id: str, captures: dict[str, str]) -> list[str]:
    return _resolved_modules(FORBIDDEN_GRADLE_MODULES.get(role_id, ()), captures)


def _resolved_modules(templates: tuple[str, ...], captures: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    for template in templates:
        module = replace_placeholders(template, captures)
        # 치환되지 않은 placeholder가 남으면 어떤 모듈 문자열과도 매치되지 않는다.
        # 그대로 반환하면 규칙이 finding 없이 사라지므로 여기서 떨어뜨린다.
        if "<" in module:
            continue
        resolved.append(module)
    return resolved


def required_gradle_dependencies(role_id: str, captures: dict[str, str]) -> list[str]:
    return _resolved_modules(REQUIRED_GRADLE_MODULES.get(role_id, ()), captures)


def replace_placeholders(value: str, captures: dict[str, str]) -> str:
    concrete = value
    for key, replacement in captures.items():
        concrete = concrete.replace(f"<{key}>", replacement)
    return concrete


def package_from_source(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;?\s*$", line)
        if match:
            return match.group(1)
    return ""


def is_root_gradle_config(rel_path: str) -> bool:
    return rel_path in {"settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"}


def is_gradle_build_file(rel_path: str) -> bool:
    return Path(rel_path).name in {"build.gradle", "build.gradle.kts"}


def namespace_from_gradle(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\bnamespace\s*=?\s*['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]", text)
    return match.group(1) if match else ""


def package_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", value.replace("-", "_")).lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-flow architecture-lint")
    parser.add_argument("--root", default=".")
    parser.add_argument("--profile-root")
    parser.add_argument("--profile", default="auto")
    parser.add_argument("--files", nargs="*")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    profile_root = Path(args.profile_root).resolve() if args.profile_root else root
    try:
        profile_ids = active_profile_ids(profile_root, args.profile)
        unconfigured = unconfigured_lint_profile_ids(profile_ids, profile_root)
        if profile_ids and len(unconfigured) == len(profile_ids):
            print(
                f"{','.join(unconfigured)}: architecture lint n/a "
                "(architecture contract absent)"
            )
            return 0
        # 확장 출처는 `lint_profiles`가 보존해야 한다. react-native가 덧붙인 Android
        # profile은 `android/**`만 검사하고, 명시적으로 요청된 Android는 전체 role을 검사한다.
        candidates = normalized_candidate_files(
            args.files if args.files is not None else changed_files(root)
        )
        findings_by_profile = lint_profiles(
            root,
            profile_ids,
            files=candidates,
            profile_root=profile_root,
        )
        profile_ids = list(findings_by_profile)
        inactive = inactive_lint_profile_ids(
            root,
            profile_ids,
            candidates,
            profile_root=profile_root,
        )
        unconfigured = unconfigured_lint_profile_ids(profile_ids, profile_root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    checked = [
        profile_id
        for profile_id in profile_ids
        if profile_id not in inactive and profile_id not in unconfigured
    ]
    if not any(findings_by_profile.values()):
        # 필수 gate가 한 파일도 검사하지 않고 "passed"를 찍으면 운영자는 통과와
        # 비적용을 구분할 수 없다. 활성 조건을 도입한 순간부터 둘은 다른 사실이다.
        if checked:
            print(f"{','.join(checked)}: architecture lint passed")
        if inactive:
            print(f"{','.join(inactive)}: architecture lint n/a (activation_roots absent)")
        if unconfigured:
            print(f"{','.join(unconfigured)}: architecture lint n/a (architecture contract absent)")
        return 0
    # 실패 헤드라인도 profile별 판정 사실을 유지한다. 한 profile의 실패 때문에 다른
    # profile의 통과와 비적용이 출력에서 사라지면 gate 결과를 구분할 수 없다.
    failed = [profile_id for profile_id, findings in findings_by_profile.items() if findings]
    passed = [profile_id for profile_id in checked if profile_id not in failed]
    print(f"{','.join(failed)}: architecture lint failed", file=sys.stderr)
    if passed:
        print(f"{','.join(passed)}: architecture lint passed", file=sys.stderr)
    if inactive:
        print(
            f"{','.join(inactive)}: architecture lint n/a (activation_roots absent)",
            file=sys.stderr,
        )
    if unconfigured:
        print(
            f"{','.join(unconfigured)}: architecture lint n/a (architecture contract absent)",
            file=sys.stderr,
        )
    for profile_id, findings in findings_by_profile.items():
        for finding in findings:
            print(f"- [{profile_id}] {finding.path}: {finding.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
