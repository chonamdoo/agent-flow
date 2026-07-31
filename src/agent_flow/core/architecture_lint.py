from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_flow.core.profiles import active_profile_ids, load_profile_payload
from agent_flow.core.commands import run_safe_command


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
        findings.extend(validate_gradle_dependencies(root, rel_path, match))
        findings.extend(validate_pair(root, rel_path, roles, match))
    return findings


def lint_profiles(root: Path, profile_ids: list[str], files: list[str] | None = None) -> dict[str, list[Finding]]:
    requested_profile_ids = list(profile_ids)
    candidates = normalized_candidate_files(files if files is not None else changed_files(root))
    profile_ids = expanded_lint_profile_ids(requested_profile_ids, candidates)
    if len(profile_ids) <= 1:
        return {
            profile_id: lint_project(root, profile_id, files=files)
            for profile_id in profile_ids
        }
    contexts = {
        profile_id: profile_lint_context(profile_id)
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
        profile_id: lint_project(root, profile_id, files=profile_files)
        for profile_id, profile_files in selected.items()
    }


def inactive_lint_profile_ids(root: Path, profile_ids: list[str], candidates: list[str]) -> list[str]:
    inactive: list[str] = []
    for profile_id in profile_ids:
        architecture = load_profile_payload(profile_id).get("architecture")
        if not isinstance(architecture, dict):
            continue
        roles = architecture.get("roles")
        if not isinstance(roles, list) or not roles:
            continue
        if not architecture_lint_is_active(root, architecture, candidates):
            inactive.append(profile_id)
    return inactive


def unconfigured_lint_profile_ids(profile_ids: list[str]) -> list[str]:
    unconfigured: list[str] = []
    for profile_id in profile_ids:
        architecture = load_profile_payload(profile_id).get("architecture")
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
    tracked = run_safe_command(
        ["git", "diff", "--name-only", "-z", "--diff-filter=ACMRTUXB", "HEAD", "--"],
        cwd=root,
    )
    tracked_outputs: list[str] = []
    if tracked.ok:
        tracked_outputs.append(tracked.stdout)
    else:
        staged = run_safe_command(
            ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRTUXB", "--"],
            cwd=root,
        )
        unstaged = run_safe_command(
            ["git", "diff", "--name-only", "-z", "--diff-filter=ACMRTUXB", "--"],
            cwd=root,
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
    untracked = run_safe_command(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=root,
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


def code_only(rel_path: str, text: str, mask_strings: bool = True) -> str:
    # `.py`만 `#` 주석이고 `SOURCE_SUFFIXES`의 나머지는 전부 `//` 계열이다. 모르는
    # 확장자도 `//` 쪽으로 둔다 - 여기 오기 전에 이미 확장자로 걸러진다.
    suffix = Path(rel_path).suffix
    hash_comments = suffix == ".py"
    slash_comments = not hash_comments
    javascript = suffix in JS_SUFFIXES
    interpolating_marks, interpolation_style = INTERPOLATION_STYLES.get(suffix, ("", ""))
    # backtick은 JS에서만 문자열이다. Kotlin에서는 escaped identifier라
    # 문자열로 보면 그 안의 위반을 놓친다.
    marks = "\"'`" if javascript else "\"'"
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        rest = text[index:]
        if slash_comments and rest.startswith("//") and not is_url_scheme_slash(text, index):
            index = blank_until(out, text, index, "\n", keep_terminator=True)
            continue
        if slash_comments and rest.startswith("/*"):
            index = blank_until(out, text, index + 2, "*/")
            continue
        if hash_comments and rest.startswith("#"):
            index = blank_until(out, text, index, "\n", keep_terminator=True)
            continue
        quote = next((mark for mark in ('"""', "'''") if rest.startswith(mark)), "")
        if quote:
            # 삼중 따옴표는 보간을 따로 보지 않는다. 여러 줄 문자열이라 한 줄 가드가
            # 안 서고, 그 안의 보간까지 좇으면 스캐너가 파서가 된다.
            index = blank_until(out if mask_strings else None, text, index + 3, quote)
            continue
        if text[index] in marks:
            # gradle 판독기는 문자열이 곧 데이터다(`project(":core:data")`). 주석만
            # 지우고 문자열은 남긴다.
            keep = not mask_strings or (javascript and is_module_specifier(text, index))
            style = interpolation_style if text[index] in interpolating_marks else ""
            if style == "fstring" and not has_format_prefix(text, index):
                style = ""
            index = blank_string(None if keep else out, text, index, style)
            continue
        index += 1
    return "".join(out)


# `//`는 문맥 없이 주석이 아니다. `https://x`의 스킴과 정규식 `/^https?:\/\//`의
# 이스케이프된 슬래시 뒤에 붙으면 그 줄 뒤쪽의 진짜 코드까지 지운다. 스킴은 앞이
# 반드시 글자다 - `case 1://메모`의 `:`까지 코드로 보면 주석을 놓친다.
def is_url_scheme_slash(text: str, index: int) -> bool:
    if index == 0:
        return False
    if text[index - 1] == "\\":
        return True
    if text[index - 1] != ":" or index < 2:
        return False
    previous = text[index - 2]
    return previous.isascii() and previous.isalpha()


def has_format_prefix(text: str, quote: int) -> bool:
    """`f"..."`처럼 f 접두사가 붙은 Python 리터럴인가."""
    start = quote
    while start > 0 and text[start - 1].isascii() and text[start - 1].isalpha():
        start -= 1
    prefix = text[start:quote]
    return len(prefix) <= 2 and "f" in prefix.lower()


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


def blank_string(out: list[str] | None, text: str, start: int, style: str = "") -> int:
    """문자열 하나를 지나가며 `out`이 있으면 그 구간을 공백으로 만든다.

    보간식 안쪽은 코드로 남긴다. `"${viewModel.state}"`, `"\\(viewModel.state)"`,
    `f"{api_client.value}"`는 문자열 안에 있어도 코드다.
    """
    mark = text[start]
    length = len(text)
    position = start + 1
    while position < length:
        char = text[position]
        if char == mark:
            return position + 1
        # 짝이 없는 따옴표 하나가 파일 끝까지 지우면 진짜 위반이 통째로 사라진다.
        # backtick만 예외다 - 여러 줄 template literal이 정상 문법이다.
        if char == "\n" and mark != "`":
            return position
        if char == "\\":
            if style == "swift" and text[position + 1 : position + 2] == "(":
                closed = skip_balanced(text, position + 1, "(", ")", mark)
                if closed != -1:
                    position = closed
                    continue
            # 파일이 백슬래시로 끝나면 건너뛴 자리가 범위 밖이다. 여기서 멈추지
            # 않으면 필수 gate가 finding 대신 IndexError로 죽는다.
            if position + 1 >= length:
                return length
            # 개행을 이스케이프로 넘기면 위의 개행 가드가 무력해진다 - 짝 없는
            # 따옴표 하나가 다음 줄의 진짜 위반을 통째로 지운다.
            if text[position + 1] == "\n" and mark != "`":
                return position
            if out is not None:
                out[position] = " "
            position += 2
            continue
        following = text[position + 1 : position + 2]
        if style in ("brace", "dollar") and char == "$" and following == "{":
            closed = skip_balanced(text, position + 1, "{", "}", mark)
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
            closed = skip_balanced(text, position, "{", "}", mark)
            if closed != -1:
                position = closed
                continue
        if out is not None:
            out[position] = " "
        position += 1
    return length


def is_identifier_start(char: str) -> bool:
    return bool(char) and (char == "_" or (char.isascii() and char.isalpha()))


def is_identifier_part(char: str) -> bool:
    return char == "_" or (char.isascii() and char.isalnum())


def skip_balanced(text: str, opener: int, open_char: str, close_char: str, mark: str) -> int:
    """짝이 맞는 닫는 괄호 다음을 돌려준다. 문자열 안에서 못 찾으면 -1.

    못 찾았는데 파일 끝을 돌려주면 그 지점부터 마스킹이 통째로 꺼진다 - `"${"`
    하나가 뒤따르는 주석 전부를 코드로 만든다.

    보간식 안의 문자열은 바깥 문자열의 끝이 아니다. `"${orderDto.format("x")}"`에서
    안쪽 따옴표를 종료로 보면 앞부분의 `orderDto`까지 지워 위반을 놓친다.
    """
    depth = 0
    position = opener
    length = len(text)
    while position < length:
        char = text[position]
        if char in "\"'`":
            position = skip_nested_string(text, position, mark)
            if position == -1:
                return -1
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
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
    text = code_only(settings.name, settings.read_text(encoding="utf-8", errors="replace"), mask_strings=False)
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
    parser.add_argument("--profile", default="auto")
    parser.add_argument("--files", nargs="*")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        profile_ids = active_profile_ids(root, args.profile)
        unconfigured = unconfigured_lint_profile_ids(profile_ids)
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
        findings_by_profile = lint_profiles(root, profile_ids, files=candidates)
        profile_ids = list(findings_by_profile)
        inactive = inactive_lint_profile_ids(root, profile_ids, candidates)
        unconfigured = unconfigured_lint_profile_ids(profile_ids)
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
