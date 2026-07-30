from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_flow.core.profiles import active_profile_ids, load_profile_payload


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
PLACEHOLDER_RESERVED_SEGMENTS = {"src", "build", "gradle", ".gradle"}
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
    "navigation-impl",
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


def lint_project(root: Path, profile_id: str, files: list[str] | None = None) -> list[Finding]:
    profile = load_profile_payload(profile_id)
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
            if managed_roots and not is_root_gradle_config(rel_path):
                findings.append(Finding(rel_path, "path is outside profile architecture role mapping"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() and path.is_file() else ""
        findings.extend(validate_forbidden_tokens(rel_path, text, match.role))
        findings.extend(validate_package_suffix(rel_path, text, match.role, match.captures))
        findings.extend(validate_gradle_namespace(root, rel_path, match))
        findings.extend(validate_declared_modules(root, rel_path, match.role, match.captures))
        findings.extend(validate_gradle_dependencies(root, rel_path, match))
        findings.extend(validate_pair(root, rel_path, roles, match))
    return findings


def lint_profiles(root: Path, profile_ids: list[str], files: list[str] | None = None) -> dict[str, list[Finding]]:
    candidates = normalized_candidate_files(files if files is not None else changed_files(root))
    profile_ids = expanded_lint_profile_ids(profile_ids, candidates)
    if len(profile_ids) <= 1:
        return {
            profile_id: lint_project(root, profile_id, files=files)
            for profile_id in profile_ids
        }
    contexts = {
        profile_id: profile_lint_context(profile_id)
        for profile_id in profile_ids
    }
    selected: dict[str, list[str]] = {profile_id: [] for profile_id in profile_ids}
    for rel_path in candidates:
        relevant_profiles = [
            profile_id
            for profile_id, context in contexts.items()
            if context_path_is_relevant(rel_path, context)
        ]
        if not relevant_profiles:
            fallback = first_profile_with_roles(profile_ids, contexts)
            if fallback:
                relevant_profiles = [fallback]
        for profile_id in relevant_profiles:
            selected[profile_id].append(rel_path)
    return {
        profile_id: lint_project(root, profile_id, files=profile_files)
        for profile_id, profile_files in selected.items()
    }


def inactive_lint_profile_ids(root: Path, profile_ids: list[str], candidates: list[str]) -> list[str]:
    inactive: list[str] = []
    for profile_id in profile_ids:
        architecture = load_profile_payload(profile_id).get("architecture")
        if not isinstance(architecture, dict) or not isinstance(architecture.get("roles"), list):
            continue
        if not architecture_lint_is_active(root, architecture, candidates):
            inactive.append(profile_id)
    return inactive


def expanded_lint_profile_ids(profile_ids: list[str], candidates: list[str]) -> list[str]:
    expanded = list(profile_ids)
    if "react-native" in expanded and "android" not in expanded:
        if any(path == "android" or path.startswith("android/") for path in candidates):
            expanded.append("android")
    return expanded


def profile_lint_context(profile_id: str) -> tuple[list[object], tuple[str, ...]]:
    profile = load_profile_payload(profile_id)
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
    return any(root_path_is_active(candidate, roots) for candidate in candidates) or any(
        (root / activation_root).exists() for activation_root in roots
    )


def root_path_is_active(rel_path: str, activation_roots: tuple[str, ...]) -> bool:
    return any(rel_path == activation_root or rel_path.startswith(f"{activation_root}/") for activation_root in activation_roots)


def changed_files(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD", "--"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    files: list[str] = []
    if tracked.returncode == 0:
        files.extend(line.strip() for line in tracked.stdout.splitlines() if line.strip())
    if untracked.returncode == 0:
        files.extend(line.strip() for line in untracked.stdout.splitlines() if line.strip())
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
    haystacks = [Path(rel_path).name, text]
    for token in forbidden:
        if not isinstance(token, str) or not token:
            continue
        if any(contains_forbidden_token(haystack, token) for haystack in haystacks):
            findings.append(Finding(rel_path, f"{role.get('id', 'role')} contains forbidden token {token}"))
    return findings


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


def validate_gradle_dependencies(root: Path, rel_path: str, match: RoleMatch) -> list[Finding]:
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
        if module not in dependencies:
            findings.append(Finding(str(build_file.relative_to(root)), f"{role_id} must depend on {module}"))
    return findings


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


def declared_gradle_modules(root: Path) -> set[str]:
    settings = next((root / name for name in ("settings.gradle.kts", "settings.gradle") if (root / name).is_file()), None)
    if settings is None:
        return set()
    text = settings.read_text(encoding="utf-8", errors="replace")
    modules = set(re.findall(r"['\"](:[A-Za-z0-9_:-]+)['\"]", text))
    modules.update(re.findall(r"include\s+['\"](:[A-Za-z0-9_:-]+)['\"]", text))
    return modules


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
    text = path.read_text(encoding="utf-8", errors="replace")
    dependencies = set(re.findall(r"project\(\s*['\"](:[A-Za-z0-9_:-]+)['\"]\s*\)", text))
    dependencies.update(re.findall(r"project\s+['\"](:[A-Za-z0-9_:-]+)['\"]", text))
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
    if role_id == "core-data" and captures.get("context"):
        return [f":core:domain:{captures['context']}"]
    if role_id == "feature-presentation" and captures.get("feature"):
        return [f":feature:{captures['feature']}:api"]
    if role_id == "navigation-impl":
        return [":core:navigation:api"]
    return []


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
    parser.add_argument("--profile", default="auto")
    parser.add_argument("--files", nargs="*")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        profile_ids = active_profile_ids(root, args.profile)
        # `lint_profiles`가 안에서 하는 확장을 여기서도 해 둔다. 출력이 실제로 검사한
        # profile 집합과 어긋나면 "무엇이 비적용이었나"가 다시 불명확해진다.
        candidates = normalized_candidate_files(
            args.files if args.files is not None else changed_files(root)
        )
        profile_ids = expanded_lint_profile_ids(profile_ids, candidates)
        findings_by_profile = lint_profiles(root, profile_ids, files=candidates)
        inactive = inactive_lint_profile_ids(root, profile_ids, candidates)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not any(findings_by_profile.values()):
        # 필수 gate가 한 파일도 검사하지 않고 "passed"를 찍으면 운영자는 통과와
        # 비적용을 구분할 수 없다. 활성 조건을 도입한 순간부터 둘은 다른 사실이다.
        checked = [profile_id for profile_id in profile_ids if profile_id not in inactive]
        if checked:
            print(f"{','.join(checked)}: architecture lint passed")
        if inactive:
            print(f"{','.join(inactive)}: architecture lint n/a (activation_roots absent)")
        return 0
    # 실패 헤드라인도 검사한 profile만 이름 부른다. 비활성 profile을 함께 적으면
    # 성공 경로에서 분리한 "통과 아님 = 비적용"이 실패 경로에서 다시 뭉개진다.
    failed = [profile_id for profile_id, findings in findings_by_profile.items() if findings]
    print(f"{','.join(failed)}: architecture lint failed", file=sys.stderr)
    if inactive:
        print(
            f"{','.join(inactive)}: architecture lint n/a (activation_roots absent)",
            file=sys.stderr,
        )
    for profile_id, findings in findings_by_profile.items():
        for finding in findings:
            print(f"- [{profile_id}] {finding.path}: {finding.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
