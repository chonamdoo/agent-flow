from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core.architecture_lint import (  # noqa: E402
    validate_forbidden_tokens,
    validate_package_suffix,
)


CORE_DATA_ROLE = {"id": "core-data", "package_suffix": "core.data.<context>"}
CAPTURES = {"context": "auth"}


def _findings(package: str):
    return validate_package_suffix(
        "core/data/auth/src/main/java/io/levvels/samantha/core/data/auth/X.kt",
        f"package {package}\n",
        CORE_DATA_ROLE,
        CAPTURES,
    )


def test_exact_root_package_passes():
    assert _findings("io.levvels.samantha.core.data.auth") == []


def test_subpackages_pass():
    assert _findings("io.levvels.samantha.core.data.auth.repository") == []
    assert _findings("io.levvels.samantha.core.data.auth.source.remote") == []
    assert _findings("io.levvels.samantha.core.data.auth.di") == []


def test_wrong_context_fails():
    findings = _findings("io.levvels.samantha.core.data.user")
    assert len(findings) == 1
    assert "does not match role suffix core.data.auth" in findings[0].message


def test_partial_segment_is_not_a_false_match():
    assert len(_findings("io.levvels.samantha.core.data.authentication")) == 1


def test_missing_package_declaration_reported():
    findings = validate_package_suffix(
        "core/data/auth/X.kt",
        "// no package decl\n",
        CORE_DATA_ROLE,
        CAPTURES,
    )
    assert len(findings) == 1
    assert "requires package declaration" in findings[0].message


# 실제 프로필 값. android.yaml feature-presentation / ios.yaml core-domain /
# python.yaml core-domain / nextjs.yaml core-domain 순이다.
ANDROID_PRESENTATION_ROLE = {"id": "feature-presentation", "forbidden": ["ApiService", "RemoteDataSource", "Dto", "Entity"]}
IOS_DOMAIN_ROLE = {"id": "core-domain", "forbidden": ["ApiClient", "DTO", "View", "ViewModel"]}
PY_DOMAIN_ROLE = {"id": "core-domain", "forbidden": ["ApiClient", "DTO", "View", "Router"]}
WEB_DOMAIN_ROLE = {"id": "core-domain", "forbidden": ["ApiClient", "Dto", "Component", "Screen"]}
DART_DOMAIN_ROLE = {"id": "core-domain", "forbidden": ["package:flutter", "Widget", "BuildContext", "Dto"]}


def _forbidden(role: dict, name: str, text: str):
    return validate_forbidden_tokens(name, text, role)


def test_word_interior_match_is_not_a_forbidden_token():
    assert _forbidden(ANDROID_PRESENTATION_ROLE, "IdentityStore.kt", "val identity = user.identity\n") == []
    assert _forbidden(IOS_DOMAIN_ROLE, "Overview.swift", "struct ReviewPolicy {}\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "screening.ts", "const componentry = 1\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "ScreenshotTest.ts", "const shot = 1\n") == []
    assert _forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'const val IDENTITY_KEY = "identity"\n') == []


def test_uppercase_run_interior_is_not_a_forbidden_token():
    # 게이트를 막던 이름들의 SCREAMING_SNAKE 형태. 대문자 런 안쪽도 단어 중간이다.
    assert _forbidden(WEB_DOMAIN_ROLE, "Chat.ts", "const SCREENING_STATUS = 1\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "Chat.ts", "const SCREENSHOT_DIR = 1\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "Chat.ts", "const COMPONENTRY_CODE = 1\n") == []
    # 대문자 런이 토큰에서 끝나면 진짜 참조다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "const val USER_DTO = 1\n")) == 1
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "class DTOMapper\n")) == 1


def test_camel_hump_and_separator_boundaries_are_reported():
    findings = _forbidden(ANDROID_PRESENTATION_ROLE, "ChatScreen.kt", "import io.levvels.samantha.core.data.chat.ChatEntity\n")
    assert len(findings) == 1
    assert "feature-presentation contains forbidden token Entity" in findings[0].message
    # 프로필은 `Dto`로 적지만 실제 코드는 `CheckoutDTO`로 쓴다. 험프 경계가 이 둘을 잇는다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "ChatScreen.kt", "class CheckoutDTO\n")) == 1
    assert len(_forbidden(PY_DOMAIN_ROLE, "chat.py", "from src.core.data.chat.dto import X\n")) == 1


def test_token_after_an_uppercase_acronym_is_reported():
    assert len(_forbidden(PY_DOMAIN_ROLE, "order.py", "from rest_framework.views import APIView\n")) == 1
    assert len(_forbidden(PY_DOMAIN_ROLE, "service.py", "api = APIRouter()\n")) == 1
    assert len(_forbidden(IOS_DOMAIN_ROLE, "Order.swift", "final class Host: UIViewController {}\n")) == 1


def test_plural_and_non_lowercase_suffixes_are_reported():
    assert len(_forbidden(PY_DOMAIN_ROLE, "views.py", "def handle(): ...\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", "export * from './screens'\n")) == 1
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "class OrderDto2\n")) == 1
    # 한글이 뒤에 붙어도 단어는 끝난 것이다. Kotlin은 한글 식별자를 허용한다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "val OrderDto목록 = 1\n")) == 1
    # 복수형 뒤에 다시 대문자로 단어가 시작하면 그 자리도 경계다.
    assert len(_forbidden(IOS_DOMAIN_ROLE, "Order.swift", "struct ViewsMapper {}\n")) == 1
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "class OrderDtosMapper\n")) == 1


def test_file_name_is_also_checked():
    findings = _forbidden(ANDROID_PRESENTATION_ROLE, "ChatEntity.kt", "class ChatModel\n")
    assert len(findings) == 1
    assert "contains forbidden token Entity" in findings[0].message
    assert _forbidden(ANDROID_PRESENTATION_ROLE, "identity.kt", "class Identity\n") == []


def test_haystack_edges_are_boundaries():
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Dto.kt", "Entity")) == 2


def test_case_folding_length_change_keeps_the_boundary_index_valid():
    # `İ`.lower()는 코드포인트가 늘어난다. 접은 문자열에서 얻은 인덱스를 원문에 그대로
    # 쓰면 경계 판정이 밀려 IndexError로 필수 gate가 죽는다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "İ" * 8 + "\nclass ChatEntity\n")) == 1


ANDROID_PROFILE = KIT_ROOT / "src" / "agent_flow" / "profiles" / "android.yaml"


def _android_architecture() -> dict[str, Any]:
    return yaml.safe_load(ANDROID_PROFILE.read_text(encoding="utf-8"))["architecture"]


def _adopted_core_domain_module(root: Path, context: str = "auth") -> Path:
    """채택된 nested 모듈. 빈 디렉터리는 채택의 증거가 아니라 소스가 증거다.

    `core/domain/schemas/`처럼 소스 없는 디렉터리가 증거로 통하면 평면 저장소가 strict로
    켜진다. 활성 픽스처도 그래서 실제 모듈 모양이어야 한다.
    """
    module = root / "core" / "domain" / context
    module.mkdir(parents=True)
    (module / "build.gradle.kts").write_text(
        f'android {{ namespace = "com.example.core.domain.{context}" }}\n', encoding="utf-8"
    )
    return module


def test_android_lint_activates_only_on_adopted_roots(tmp_path):
    """반증: 평면 `core/*` 저장소에서 필수 gate가 변경 파일 전량을 미매핑으로 잡았다."""
    from agent_flow.core.architecture_lint import architecture_lint_is_active

    architecture = _android_architecture()
    assert architecture["strict_when_roots_present"] is True
    roots = architecture["activation_roots"]
    assert "core/domain" in roots
    # react-native 저장소는 `android/` 변경에 이 profile이 덧붙는다. 이 루트가 없으면
    # `app-shell`이 선언한 `android/app` 검사까지 함께 꺼져 무음 통과가 된다.
    assert "android/app" in roots
    # 맨 `app`이 들어오면 평면 Android 저장소의 미매핑 오탐이 되살아난다.
    assert "app" not in roots

    flat = tmp_path / "flat"
    (flat / "core" / "data" / "src" / "main").mkdir(parents=True)
    (flat / "app").mkdir()
    assert (
        architecture_lint_is_active(flat, architecture, ["core/data/src/main/Repo.kt"]) is False
    )

    adopted = tmp_path / "adopted"
    _adopted_core_domain_module(adopted)
    assert architecture_lint_is_active(adopted, architecture, ["app/Main.kt"]) is True

    react_native = tmp_path / "rn"
    (react_native / "android" / "app" / "src").mkdir(parents=True)
    (react_native / "src" / "core" / "domain").mkdir(parents=True)
    assert (
        architecture_lint_is_active(react_native, architecture, ["android/app/src/Main.kt"]) is True
    )

    # 디렉터리가 아직 없어도 변경 후보에 들어오면 그 커밋이 계약을 채택하는 커밋이다.
    assert (
        architecture_lint_is_active(flat, architecture, ["core/domain/auth/Session.kt"]) is True
    )


def test_core_database_role_is_mapped_and_dependency_gated():
    """반증: core/database 경로에 role이 없어 Room 모듈 전체가 미매핑으로 잡혔다."""
    from agent_flow.core.architecture_lint import forbidden_gradle_dependencies, match_role

    roles = _android_architecture()["roles"]
    match = match_role("core/database/src/main/java/com/example/app/core/database/ScreenDao.kt", roles)
    assert match is not None
    assert match.role["id"] == "core-database"
    assert match.role["package_suffix"] == "core.database"

    # 선언된 방향은 `core:data -> core:database` 하나뿐이다.
    assert forbidden_gradle_dependencies("core-database", {}) == [":app", ":core:data", ":feature"]
    # 도메인이 Room 모듈을 보면 저장소 구현이 도메인 계약을 통과해 새어 들어온다.
    assert ":core:database" in forbidden_gradle_dependencies("core-domain", {})
    assert ":core:database" in forbidden_gradle_dependencies("feature-presentation", {})
    assert ":core:database" in forbidden_gradle_dependencies("feature-api", {"feature": "home"})
    assert ":core:database" not in forbidden_gradle_dependencies("core-data", {})


def test_gradle_named_project_dependencies_are_extracted(tmp_path):
    """반증: named `path` 형식을 놓치면 금지된 모듈 의존이 gate를 통과한다."""
    from agent_flow.core.architecture_lint import gradle_project_dependencies

    kotlin_build = tmp_path / "build.gradle.kts"
    kotlin_build.write_text(
        'implementation(project(path = ":core:data"))\n',
        encoding="utf-8",
    )
    groovy_build = tmp_path / "build.gradle"
    groovy_build.write_text(
        "implementation(project(path: ':core:database'))\n",
        encoding="utf-8",
    )

    assert gradle_project_dependencies(kotlin_build) == {":core:data"}
    assert gradle_project_dependencies(groovy_build) == {":core:database"}


def test_container_build_file_does_not_bind_to_placeholder_role():
    """반증: leaf 모듈의 build.gradle.kts가 `<adapter>` 하위 모듈로 오인됐다."""
    from agent_flow.core.architecture_lint import match_role

    roles = _android_architecture()["roles"]
    match = match_role("core/platform/build.gradle.kts", roles)

    assert match is not None
    assert match.role["id"] == "core-platform"
    assert match.captures == {}


def test_inactive_profile_is_reported_separately_from_passed(tmp_path, capsys):
    """반증: 한 파일도 검사하지 않은 필수 gate가 "passed"를 찍으면 통과와 비적용이 같아진다."""
    from agent_flow.core.architecture_lint import inactive_lint_profile_ids, main

    flat = tmp_path / "flat"
    (flat / "core" / "data" / "src" / "main").mkdir(parents=True)
    probe = flat / "core" / "data" / "src" / "main" / "Repo.kt"
    probe.write_text("package com.example.core.data\nclass Repo\n", encoding="utf-8")

    assert inactive_lint_profile_ids(flat, ["android"], ["core/data/src/main/Repo.kt"]) == ["android"]
    assert main(
        ["--root", str(flat), "--profile", "android", "--files", "core/data/src/main/Repo.kt"]
    ) == 0
    out = capsys.readouterr().out
    assert "android: architecture lint n/a (activation_roots absent)" in out
    assert "architecture lint passed" not in out

    adopted = tmp_path / "adopted"
    _adopted_core_domain_module(adopted)
    assert inactive_lint_profile_ids(adopted, ["android"], []) == []
    assert main(["--root", str(adopted), "--profile", "android", "--files"]) == 0
    assert "android: architecture lint passed" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "--profile", "generic", "--files"]) == 0
    out = capsys.readouterr().out
    assert "generic: architecture lint n/a (architecture contract absent)" in out
    assert "architecture lint passed" not in out


def test_changed_file_discovery_fails_closed_when_git_fails(tmp_path, monkeypatch):
    """반증: git 실패가 빈 후보가 되면 필수 architecture gate가 거짓 green이 된다."""
    from agent_flow.core import architecture_lint
    from agent_flow.core.commands import SafeCommandResult

    (tmp_path / ".git").mkdir()
    failed = SafeCommandResult(
        args=("git", "diff"),
        returncode=128,
        stdout="",
        stderr="fatal: cannot read index",
    )
    monkeypatch.setattr(architecture_lint, "git_safe", lambda *args, **kwargs: failed)

    with pytest.raises(ValueError, match="git diff.*cannot read index"):
        architecture_lint.changed_files(tmp_path)


def test_unmapped_files_outside_managed_roots_do_not_fail_android_lint(tmp_path):
    """반증: 채택 저장소의 build-logic 변경까지 role 미매핑으로 막으면 필수 gate가 개발을 멈춘다."""
    from agent_flow.core.architecture_lint import lint_project

    _adopted_core_domain_module(tmp_path)
    outside = tmp_path / "build-logic" / "convention" / "ConventionPlugin.kt"
    outside.parent.mkdir(parents=True)
    outside.write_text("class ConventionPlugin\n", encoding="utf-8")
    inside = tmp_path / "core" / "unmapped" / "Thing.kt"
    inside.parent.mkdir(parents=True)
    inside.write_text("class Thing\n", encoding="utf-8")

    assert lint_project(tmp_path, "android", files=[str(outside.relative_to(tmp_path))]) == []
    findings = lint_project(tmp_path, "android", files=[str(inside.relative_to(tmp_path))])
    assert [finding.message for finding in findings] == [
        "path is outside profile architecture role mapping"
    ]


def test_react_native_android_escalation_keeps_js_app_out_of_android_profile(tmp_path):
    """반증: android 변경이 하나 있다고 Expo Router의 app 트리까지 Android 규칙으로 보면 안 된다."""
    from agent_flow.core.architecture_lint import lint_profiles

    native = tmp_path / "android" / "app" / "src" / "main" / "MainActivity.kt"
    native.parent.mkdir(parents=True)
    native.write_text("class MainActivity\n", encoding="utf-8")
    route = tmp_path / "app" / "(tabs)" / "index.tsx"
    route.parent.mkdir(parents=True)
    route.write_text("export class CheckoutDto {}\n", encoding="utf-8")
    files = [str(native.relative_to(tmp_path)), str(route.relative_to(tmp_path))]

    findings = lint_profiles(tmp_path, ["react-native"], files=files)

    assert "android" in findings
    assert all(finding.path != str(route.relative_to(tmp_path)) for finding in findings["android"])


def test_schema_role_vocabulary_covers_every_shipped_role():
    """반증: 어떤 코드도 이 열거를 파싱하지 않아 profile이 role을 늘릴 때마다 낡는다."""
    profiles_dir = KIT_ROOT / "src" / "agent_flow" / "profiles"
    schema = yaml.safe_load((profiles_dir / "_schema.yaml").read_text(encoding="utf-8"))
    declared = {
        part.strip()
        for part in schema["optional"]["architecture"]["roles"][0]["id"].split("|")
    }
    shipped: set[str] = set()
    for path in sorted(profiles_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        architecture = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("architecture") or {}
        for role in architecture.get("roles") or []:
            shipped.add(role["id"])
    assert shipped - declared == set()
    assert declared - shipped == set()


def test_failure_headline_names_only_the_profiles_it_checked(tmp_path, capsys):
    """반증: 비활성 profile을 실패 헤드라인에 함께 적으면 성공 경로에서 나눈 사실이 다시 뭉개진다."""
    from agent_flow.core.architecture_lint import main

    root = tmp_path / "mixed"
    wrong = root / "src" / "core" / "wrong"
    wrong.mkdir(parents=True)
    (wrong / "Thing.ts").write_text("export const value = 1\n", encoding="utf-8")
    # python은 `src/core`를 activation_roots로 갖고 nextjs는 조건 없이 활성이다.
    # 여기서는 android를 비활성 쪽으로 붙여 두 사실이 한 출력에 섞이는 경우를 만든다.
    assert main(
        [
            "--root",
            str(root),
            "--profile",
            "nextjs,android",
            "--files",
            "src/core/wrong/Thing.ts",
        ]
    ) == 1
    err = capsys.readouterr().err
    assert "nextjs: architecture lint failed" in err
    assert "nextjs,android: architecture lint failed" not in err
    assert "android: architecture lint n/a (activation_roots absent)" in err


def test_failure_output_separates_passed_and_unconfigured_profiles(tmp_path, capsys):
    """반증: 다른 profile 하나가 실패해도 나머지 pass와 n/a 사실은 사라지면 안 된다."""
    from agent_flow.core.architecture_lint import main

    root = tmp_path / "mixed-status"
    (root / "src" / "core" / "domain" / "auth").mkdir(parents=True)
    (root / "src" / "core" / "data" / "auth").mkdir(parents=True)
    source = root / "src" / "core" / "domain" / "auth" / "Thing.py"
    source.write_text("class Screen:\n    pass\n", encoding="utf-8")

    assert main(
        [
            "--root",
            str(root),
            "--profile",
            "nextjs,python,generic",
            "--files",
            "src/core/domain/auth/Thing.py",
        ]
    ) == 1
    err = capsys.readouterr().err
    assert "nextjs: architecture lint failed" in err
    assert "python: architecture lint passed" in err
    assert "generic: architecture lint n/a (architecture contract absent)" in err


def _shipped_role_ids() -> set[str]:
    profiles_dir = KIT_ROOT / "src" / "agent_flow" / "profiles"
    shipped: set[str] = set()
    for path in sorted(profiles_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        architecture = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("architecture") or {}
        for role in architecture.get("roles") or []:
            shipped.add(role["id"])
    return shipped


def test_every_shipped_role_declares_its_gradle_stance():
    """반증: 표에 없는 role은 의존 규칙 0개로 조용히 통과한다. 무제약과 누락이 같아진다."""
    from agent_flow.core.architecture_lint import (
        FORBIDDEN_GRADLE_MODULES,
        REQUIRED_GRADLE_MODULES,
        UNCONSTRAINED_GRADLE_ROLES,
    )

    constrained = set(FORBIDDEN_GRADLE_MODULES) | set(REQUIRED_GRADLE_MODULES)
    covered = constrained | set(UNCONSTRAINED_GRADLE_ROLES)
    assert _shipped_role_ids() - covered == set()
    assert covered - _shipped_role_ids() == set()
    assert constrained & set(UNCONSTRAINED_GRADLE_ROLES) == set()


def test_unresolved_placeholder_module_is_dropped_instead_of_silently_dead():
    """반증: `:feature::presentation`은 어떤 모듈과도 매치되지 않아 규칙이 사라진다."""
    from agent_flow.core.architecture_lint import forbidden_gradle_dependencies

    with_feature = forbidden_gradle_dependencies("feature-api", {"feature": "home"})
    assert ":feature:home:presentation" in with_feature

    without_feature = forbidden_gradle_dependencies("feature-api", {})
    assert all(":presentation" not in module for module in without_feature)
    assert ":core:data" in without_feature


def test_comments_and_string_literals_are_not_code():
    """주석·문자열의 토큰까지 세면 `@media screen`과 문서 URL이 필수 gate를 막는다."""
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "// 참고: https://x/#screen\nconst a = 1\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "/* @media screen */\nconst a = 1\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", 'const css = "@media screen";\n') == []
    assert _forbidden(IOS_DOMAIN_ROLE, "Chat.swift", "// ViewModel 설명\nstruct A {}\n") == []
    assert _forbidden(PY_DOMAIN_ROLE, "chat.py", "# View 관련 메모\nx = 1\n") == []


def test_module_specifiers_stay_code_even_though_they_are_strings():
    """반증: 문자열을 통째로 지우면 `export * from './screens'` 같은 진짜 위반이 사라진다."""
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", "export * from './screens'\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", 'import { X } from "./ChatScreen"\n')) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", 'const x = require("./ChatScreen")\n')) == 1


def test_masking_survives_inputs_that_used_to_crash_or_swallow_code():
    """마스킹이 필수 gate를 죽이거나 진짜 위반을 삼키면 안 된다."""
    # 파일이 백슬래시로 끝나면 예전에는 IndexError로 gate가 죽었다.
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", 'const a = "abc\\') == []
    # 짝 없는 따옴표가 다음 줄의 진짜 위반을 지우면 안 된다 - 개행 이스케이프 포함.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", 'const a = "x\\\nconst b = Dto\n')) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const a = 'x\nconst b = Dto\n")) == 1
    # `//`는 문맥 없이 주석이 아니다. 스킴과 정규식 뒤의 코드가 살아 있어야 한다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const r = /^https?:\\/\\//.test(Dto.url)\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.tsx", "<T>http://x {Dto.name}</T>\n")) == 1
    # 여러 줄 template literal의 닫는 backtick이 새 문자열을 열면 안 된다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const m = `a\nb` + Dto.name\n")) == 1
    # 문자열 보간 안은 코드다. Kotlin/Compose 소스에서 흔하다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "${orderDto.id}"\n')) == 1


def test_module_specifiers_are_found_by_the_quote_prefix_not_the_line():
    """줄 전체를 보면 `export const CSS = '@media screen'`까지 코드로 남는다."""
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "export const CSS = '@media screen'\n") == []
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", "import {\n  X,\n} from './screens'\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", "await import('./ChatScreen')\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "index.ts", "require( './ChatScreen')\n")) == 1


def test_gradle_readers_do_not_count_commented_out_declarations():
    """같은 gate의 gradle 판독기가 주석을 읽으면 주석만으로 위반이 난다."""
    from agent_flow.core.architecture_lint import code_only

    build = 'dependencies {\n  // implementation(project(":feature:home:presentation"))\n}\n'
    stripped = code_only("build.gradle.kts", build, mask_strings=False)
    assert ":feature:home:presentation" not in stripped
    # 문자열은 남는다 - gradle 판독기에는 문자열이 곧 데이터다.
    live = 'dependencies {\n  implementation(project(":core:data"))\n}\n'
    assert ":core:data" in code_only("build.gradle.kts", live, mask_strings=False)


def test_interpolation_is_code_only_where_the_language_has_it():
    """한 벌로 뭉뚱그리면 없애려던 문자열 오탐이 다른 자리에 생긴다."""
    # 보간이 없는 언어/따옴표에서는 그냥 문자열이다.
    assert _forbidden(PY_DOMAIN_ROLE, "chat.py", 'pattern = "${view}"\n') == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const p = '${screen}'\n") == []
    # 있는 곳에서는 코드다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "${orderDto.id}"\n')) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const s = `${OrderDto.name}`\n")) == 1


def test_an_unclosed_interpolation_does_not_disable_the_rest_of_the_file():
    """`${` 하나가 파일 끝까지 마스킹을 끄면 뒤따르는 주석이 전부 코드가 된다."""
    assert _forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val T = "${"\n// Dto 메모\n') == []
    from agent_flow.core.architecture_lint import code_only

    build = 'val tpl = "${"\ndependencies {\n  // implementation(project(":a:b"))\n}\n'
    assert ":a:b" not in code_only("build.gradle.kts", build, mask_strings=False)


def test_backticks_and_slashes_follow_the_language_not_a_guess():
    """Kotlin backtick은 escaped identifier고, `case 1://`의 `:`는 스킴이 아니다."""
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "val `dtoState` = 1\n")) == 1
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "switch (x) { case 1://Screen 메모\n}\n") == []
    # Groovy `from`은 모듈 지정자가 아니다.
    assert _forbidden(ANDROID_PRESENTATION_ROLE, "build.gradle", "copy { from 'src/main/dto' }\n") == []


def test_every_language_keeps_its_own_interpolation_as_code():
    """보간식은 문자열 안에 있어도 코드다. 마스킹하면 위반을 조용히 놓친다."""
    # `View`와 `ViewModel` 둘 다 걸린다 - 세는 건 보간이 코드로 남았는가다.
    assert _forbidden(IOS_DOMAIN_ROLE, "Chat.swift", 'let s = "\\(ViewModel.value)"\n')
    assert len(_forbidden(PY_DOMAIN_ROLE, "chat.py", 'x = f"{ApiClient.value}"\n')) == 1
    # Kotlin/Groovy는 중괄호 없는 `$name`도 보간이다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "$orderDto"\n')) == 1
    # Dart는 두 따옴표 모두 보간이고, `r` 접두사만 그것을 끈다.
    assert len(_forbidden(DART_DOMAIN_ROLE, "chat.dart", "final s = 'order ${o.orderDto.id}';\n")) == 1
    assert len(_forbidden(DART_DOMAIN_ROLE, "chat.dart", 'final s = "$orderDto";\n')) == 1
    assert _forbidden(DART_DOMAIN_ROLE, "chat.dart", "final s = r'raw $orderDto';\n") == []
    assert _forbidden(DART_DOMAIN_ROLE, "chat.dart", "final s = 'plain Dto text';\n") == []
    # Dart import 지정자는 문자열이지만 코드 참조다.
    assert len(_forbidden(DART_DOMAIN_ROLE, "chat.dart", "import 'package:flutter/material.dart';\n")) == 1
    # 접두사 없는 리터럴과 `{{` 이스케이프는 보간이 아니다.
    assert _forbidden(PY_DOMAIN_ROLE, "chat.py", 'x = "{ApiClient.value}"\n') == []
    assert _forbidden(PY_DOMAIN_ROLE, "chat.py", 'x = f"{{ApiClient}}"\n') == []
    assert _forbidden(IOS_DOMAIN_ROLE, "Chat.swift", 'let s = "ViewModel 설명"\n') == []


def test_a_string_inside_an_interpolation_is_not_the_end_of_the_outer_string():
    """안쪽 따옴표를 종료로 보면 보간식 앞부분의 위반이 지워진다."""
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "${orderDto.format("x")}"\n')) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", 'const s = `${OrderDto.format("x")}`\n')) == 1
    # 그래도 못 닫힌 보간은 파일을 열어두지 않는다.
    assert _forbidden(IOS_DOMAIN_ROLE, "Chat.swift", 'let T = "\\("\n// Dto 메모\n') == []
    assert _forbidden(PY_DOMAIN_ROLE, "chat.py", 'x = f"{"\n# Dto 메모\n') == []


def test_jsx_body_contractions_are_not_string_starts():
    """`it's`의 아포스트로피를 문자열 시작으로 보면 그 줄 나머지가 뒤집힌다."""
    assert _forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "<p>it's {t('@media screen')}</p>\n") == []
    assert len(_forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "<p>don't</p>{renderScreen()}\n")) == 1
    # 키워드 뒤는 예외다. minified JS가 `case"x"`, `return'x'` 모양으로 나온다.
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.jsx", "switch(x){case'screen':break}\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.jsx", "return'screen'\n") == []


def test_regex_literals_do_not_open_strings():
    """정규식 본문의 따옴표가 문자열을 열면 그 줄 나머지가 지워진다."""
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "s.replace(/'/g, '') + OrderDto.x\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.tsx", ".replace(/`([^`]+)`/g, '$1').map(OrderDto)\n")) == 1
    # 패턴 본문은 데이터다. 나눗셈은 정규식이 아니다.
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const r = /Dto/.test(x)\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const q = a / b; const w = c / d;\n") == []


def test_js_line_continuation_does_not_flip_quote_parity():
    """줄 끝 백슬래시는 JS에서 합법이다. 끊으면 닫는 따옴표가 여는 따옴표가 된다."""
    source = 'const s = "@media \\\nscreen";\nconst u = "Dto";\n'
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", source) == []
    # 다른 언어에서는 여전히 짝 없는 따옴표로 본다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "x\\\nval b = Dto\n')) == 1


def test_only_real_schemes_keep_a_double_slash_as_code():
    """`case KIND://`의 라벨까지 스킴으로 보면 주석이 코드로 남는다."""
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "switch(k){case KIND://screen 메모\n}\n") == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const o = { key://screen 메모\n}\n") == []
    assert len(_forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "<a>https://x {Dto.n}</a>\n")) == 1


def test_non_code_inside_an_interpolation_is_still_not_code():
    """보간식을 건너뛰기만 하면 그 안의 문자열·주석이 코드로 남는다."""
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", 'const s = `${theme("@media screen")}`\n') == []
    assert _forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "${fmt("@media dto")}"\n') == []
    assert _forbidden(IOS_DOMAIN_ROLE, "Chat.swift", 'let s = "\\(f("@media view"))"\n') == []
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const s = `${x /* screen */}`\n") == []
    # 보간식 자체는 그대로 코드다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val s = "${orderDto.format("x")}"\n')) == 1


def test_jsx_braces_are_not_regex_starts():
    """`{a} / {b}`는 비율 표기다. 정규식으로 보면 뒤 엘리먼트가 통째로 지워진다."""
    assert len(_forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "<div>{n} / {m} <Screen /></div>\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "<span>{used} / {OrderDto.total}</span>\n")) == 1


def test_a_regex_in_keyword_position_is_still_a_regex():
    """`return /re/`의 `/`를 나눗셈으로 보면 본문의 따옴표가 줄을 삼킨다."""
    source = """return /['\\"]/.test(s) && OrderDto.x\n"""
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", source)) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "throw /'/.test(s) ? new ScreenError() : e\n")) == 1
    # 후위 증감 뒤는 나눗셈이다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const r = i++ / OrderDto.SIZE / total;\n")) == 1


def test_a_failed_interpolation_scan_leaves_the_buffer_untouched():
    """먼저 지워 놓고 실패하면 호출자가 되돌릴 수 없다 - 파일 끝까지 사라진다."""
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const a = `${/* note }`;\nexport class OrderDto {}\n")) == 1
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", 'val a = "${/* note }"\nclass OrderDto\n')) == 1


def test_comment_rules_inside_an_interpolation_follow_the_language():
    """`.py` 보간식의 `//`는 floor division이고, JS 보간식의 `/../`는 정규식이다."""
    assert len(_forbidden(PY_DOMAIN_ROLE, "chat.py", 'msg = f"{total // ApiClient.count}"\n')) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const s = `${orderDto.path.replace(/\\//g, '-')}`\n")) == 1


def test_module_specifiers_win_over_the_contraction_rule():
    """`from'react'`의 여는 따옴표를 축약형으로 보면 따옴표 짝이 뒤집힌다."""
    assert _forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "import a from'aaa';import c from'bbb'\n") == []
    # 속성 접근자도 문자열이 올 수 있는 자리다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "Chat.tsx", "class A { get'name'(){ return OrderDto; } }\n")) == 1


def test_only_real_python_prefixes_enable_interpolation():
    """글자만 훑으면 `if\"{x}\"`의 `if`가 f 접두사로 통한다."""
    assert _forbidden(PY_DOMAIN_ROLE, "chat.py", 'if"{api_client}" in s:\n    pass\n') == []


def test_line_continuations_do_not_rescan_the_file_each_time():
    """continuation마다 파일 끝까지 다시 훑으면 O(n^2)다 - 실측 24KB에 1.2초였다."""
    from agent_flow.core.architecture_lint import code_only

    source = 'const s = "' + "@media \\\n" * 4000 + '";\n'
    started = time.perf_counter()
    code_only("big.ts", source)
    assert time.perf_counter() - started < 2.0


def test_a_keyword_named_property_is_not_a_regex_position():
    """`cfg.default / 2`의 프로퍼티 이름은 키워드가 아니다. 양방향으로 깨진다."""
    # 누락 방향: 정규식으로 보면 뒤쪽 참조가 지워진다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const r = cfg.default / OrderDto.count / 2;\n")) == 1
    # 오탐 방향: 주석의 첫 슬래시가 정규식 종료로 잡혀 주석 본문이 코드로 남는다.
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const r = cfg.default / 2 // OrderDto 는 data 전용\n") == []
    # 보간식 안에서는 닫는 중괄호까지 삼켜 보간 전체가 사라진다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const s = `${ obj.default / 2 }` + OrderDto.x\n")) == 1
    # `$`는 JS 식별자 문자다. 잘라 읽으면 `obs$in`의 `in`이 키워드가 된다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const off = obs$in / OrderDto.size / 2;\n")) == 1


def test_regex_position_keywords_are_narrower_than_literal_position_ones():
    """`from`/`async`는 흔한 변수명이다. 정규식 자리로 보면 나눗셈이 깨진다."""
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const off = from / OrderDto.pageSize / 2;\n")) == 1
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const off = async / OrderDto.size / 2;\n")) == 1
    # 진짜 키워드 자리는 그대로 정규식이다.
    assert len(_forbidden(WEB_DOMAIN_ROLE, "chat.ts", "const r = typeof /re/;\nconst u = OrderDto;\n")) == 1
    # 화살표 함수 뒤도 값 자리다.
    assert _forbidden(WEB_DOMAIN_ROLE, "chat.ts", "xs.filter(s => /Dto/.test(s))\n") == []


def test_a_failed_regex_scan_does_not_rescan_the_rest_of_the_line():
    """줄에 미닫힘 `/`가 여럿이면 매번 줄 끝까지 훑어 이차식이 된다."""
    from agent_flow.core.architecture_lint import code_only

    source = "const s = `${ x" + ",/[a" * 2000 + " }`\n"
    started = time.perf_counter()
    code_only("big.ts", source)
    assert time.perf_counter() - started < 1.0


def test_skip_balanced_leaves_the_buffer_untouched_when_it_fails():
    """실패 경로에서 out을 고치면 호출자가 되돌릴 수 없다."""
    from agent_flow.core.architecture_lint import skip_balanced, syntax_for

    text = 'const a = `${/* note }`;\nexport class OrderDto {}\n'
    out = list(text)
    assert skip_balanced(out, text, text.index("${") + 1, "{", "}", "`", "brace", syntax_for(".ts")) == -1
    assert "".join(out) == text



def _source(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.name


def _flat_android_repository(root: Path) -> list[str]:
    """실측 대상 저장소의 모양. `core/domain`은 있지만 그 아래가 `src/`뿐이다."""
    files = [
        "core/domain/build.gradle.kts",
        "core/domain/src/main/java/com/example/domain/Journey.kt",
        "core/data/src/main/java/com/example/data/JourneyRepository.kt",
        "core/model/src/main/java/com/example/model/Stop.kt",
        "feature/journey/src/main/java/com/example/journey/JourneyScreen.kt",
    ]
    for rel_path in files:
        _source(root / rel_path, "package com.example\n")
    return files


def test_flat_layout_is_not_governed_just_because_the_root_directory_exists(tmp_path):
    """반증: `core/domain/`이 있다는 이유로 strict가 켜져 변경 파일 전량이 미매핑이 됐다(436개 중 182개)."""
    from agent_flow.core.architecture_lint import architecture_lint_is_active, lint_project

    flat = tmp_path / "flat"
    files = _flat_android_repository(flat)
    assert (flat / "core" / "domain").is_dir()

    # 존재는 채택의 증거가 아니다. `core/domain/<context>`가 맞출 디렉터리가 없다 —
    # `src`/`build`는 role 매칭이 이미 예약 세그먼트로 거부하는 이름이다.
    assert architecture_lint_is_active(flat, _android_architecture(), files) is False
    assert lint_project(flat, "android", files=files) == []


def test_nested_layout_stays_governed_and_still_catches_violations(tmp_path):
    """활성 범위를 좁히는 변경이 정당한 저장소를 끄면 필수 gate가 무음 통과가 된다."""
    from agent_flow.core.architecture_lint import architecture_lint_is_active, lint_project

    nested = tmp_path / "nested"
    (nested / "core" / "data" / "journey").mkdir(parents=True)
    _source(
        nested / "core" / "domain" / "journey" / "build.gradle.kts",
        'android { namespace = "com.example.core.domain.journey" }\n',
    )
    offender = "core/domain/journey/src/main/java/com/example/core/domain/journey/JourneyViewModel.kt"
    _source(
        nested / offender,
        "package com.example.core.domain.journey\n\nclass JourneyViewModel\n",
    )

    assert architecture_lint_is_active(nested, _android_architecture(), [offender]) is True
    assert [
        (finding.path, finding.message) for finding in lint_project(nested, "android", files=[offender])
    ] == [(offender, "core-domain contains forbidden token ViewModel")]


def test_a_directory_without_source_is_not_adoption_evidence(tmp_path):
    """반증: `core/domain/schemas/`(Room `room.schemaLocation`의 기본 위치) 하나만 있어도
    평면 저장소가 strict로 켜져 변경 파일 전량이 미매핑이 됐다.

    증거를 예약 세그먼트 이름 목록에 기대면 레이아웃이 하나 나올 때마다 목록이 늘어난다.
    아래 `match_pattern` 단정이 그 사실을 못 박는다 — 패턴은 맞는다. 활성을 막는 것은
    이름이 아니라 "그 아래에 소스가 없다"는 성질이다.
    """
    from agent_flow.core.architecture_lint import (
        architecture_lint_is_active,
        lint_project,
        match_pattern,
    )

    for name, artifact in (("schemas", "x.json"), ("libs", "x.aar"), ("main", None)):
        assert match_pattern(f"core/domain/{name}", "core/domain/<context>") is not None
        flat = tmp_path / name
        files = _flat_android_repository(flat)
        placeholder = flat / "core" / "domain" / name
        placeholder.mkdir(parents=True)
        if artifact:
            (placeholder / artifact).write_text("{}\n", encoding="utf-8")
        assert architecture_lint_is_active(flat, _android_architecture(), files) is False, name
        assert lint_project(flat, "android", files=files) == []


def test_a_module_holding_source_is_still_adoption_evidence(tmp_path):
    """반증 짝: 증거를 좁히다 실제 모듈까지 끄면 필수 gate가 조용히 무음 통과가 된다.

    변경 후보는 role 패턴 밖(`build-logic/`)이라 후보만으로는 켜지지 않는다. 활성은
    디스크의 `core/domain/journey/` 아래 `.kt`에서만 나온다.
    """
    from agent_flow.core.architecture_lint import architecture_lint_is_active, lint_project

    nested = tmp_path / "nested"
    (nested / "core" / "data" / "journey").mkdir(parents=True)
    offender = "core/domain/journey/src/main/java/com/example/core/domain/journey/JourneyViewModel.kt"
    _source(nested / offender, "package com.example.core.domain.journey\n\nclass JourneyViewModel\n")
    unrelated = "build-logic/src/main/kotlin/Conventions.kt"
    _source(nested / unrelated, "package conventions\n")

    assert architecture_lint_is_active(nested, _android_architecture(), [unrelated]) is True
    assert [
        (finding.path, finding.message) for finding in lint_project(nested, "android", files=[offender])
    ] == [(offender, "core-domain contains forbidden token ViewModel")]


def test_a_build_file_violation_is_reported_once_not_once_per_source_file(tmp_path):
    """반증: build 파일 단위 검사가 소스 파일마다 다시 돌아 같은 위반을 6배로 부풀렸다(raw 273 / distinct 45)."""
    from agent_flow.core.architecture_lint import lint_project

    root = tmp_path / "nested"
    (root / "core" / "data" / "journey").mkdir(parents=True)
    build_file = "core/domain/journey/build.gradle.kts"
    _source(
        root / build_file,
        'android { namespace = "com.example.core.domain.journey" }\n'
        'dependencies { implementation(project(":core:data:journey")) }\n',
    )
    package = "core/domain/journey/src/main/java/com/example/core/domain/journey"
    clean = f"{package}/Journey.kt"
    _source(root / clean, "package com.example.core.domain.journey\n\nclass Journey\n")
    offenders = [f"{package}/JourneyViewModel.kt", f"{package}/StopViewModel.kt"]
    for rel_path in offenders:
        _source(root / rel_path, "package com.example.core.domain.journey\n\nclass Holder\n")

    findings = lint_project(root, "android", files=[clean, *offenders])

    # 세 소스 파일이 모두 같은 모듈 build 파일을 다시 읽지만 위반은 하나다.
    assert [
        finding.path for finding in findings if "forbidden Gradle dependency" in finding.message
    ] == [build_file]
    # 접는 기준은 `(path, message)`다. 파일별 위반은 파일 수만큼 남아야 한다 —
    # 메시지가 같다고 뭉개면 위반한 파일 목록이 사라진다.
    assert [
        finding.path for finding in findings if "forbidden token ViewModel" in finding.message
    ] == offenders


def _presentation_module(root: Path, deps: str) -> tuple[str, str]:
    build_file = "feature/journey/presentation/build.gradle.kts"
    _source(
        root / build_file,
        'android { namespace = "com.example.feature.journey.presentation" }\n'
        f"dependencies {{ {deps} }}\n",
    )
    source = (
        "feature/journey/presentation/src/main/java/com/example/feature/journey"
        "/presentation/JourneyScreen.kt"
    )
    _source(
        root / source,
        "package com.example.feature.journey.presentation\n\nclass JourneyScreen\n",
    )
    return build_file, source


def test_a_required_module_is_only_required_when_a_role_declares_it(tmp_path):
    """반증: `feature-presentation`은 `:feature:<f>:api` 의존을 요구하는데, 그 role을
    선언하지 않은 평면 profile에서는 요구할 대상 자체가 없다. 실측으로 저장소 하나에
    존재하지 않는 모듈을 요구하는 오탐이 5건이었다.

    반증 짝: `feature-api` role이 선언된 profile에서는 그대로 요구돼야 한다. 안 그러면
    이 변경은 규칙을 고친 게 아니라 지운 것이다.
    """
    from agent_flow.core.architecture_lint import lint_project

    governed = tmp_path / "governed"
    _adopted_core_domain_module(governed, "journey")
    build_file, source = _presentation_module(governed, "")

    # shipped android profile은 `feature-api` role을 선언한다 — 요구가 살아 있어야 한다.
    assert (build_file, "feature-presentation must depend on :feature:journey:api") in [
        (finding.path, finding.message)
        for finding in lint_project(governed, "android", files=[source])
    ]

    # 같은 저장소, `feature-api`가 없는 profile. 요구할 대상이 없으므로 요구도 없다.
    flat_profile = governed / ".agent-flow" / "profiles"
    flat_profile.mkdir(parents=True, exist_ok=True)
    architecture = _android_architecture()
    architecture["roles"] = [
        role for role in architecture["roles"] if role.get("id") != "feature-api"
    ]
    (flat_profile / "android.yaml").write_text(
        yaml.safe_dump({"id": "android", "architecture": architecture}, allow_unicode=True),
        encoding="utf-8",
    )
    assert [
        finding.message
        for finding in lint_project(governed, "android", files=[source])
        if "must depend on" in finding.message
    ] == []


def test_role_module_ownership_matches_on_coordinates_not_substrings(tmp_path):
    """placeholder는 세그먼트 하나다. 깊이가 다른 좌표를 같다고 보면 규칙이 조용히 꺼진다."""
    from agent_flow.core.architecture_lint import module_pattern_matches, role_owns_module

    assert module_pattern_matches(":feature:<feature>:api", ":feature:journey:api")
    assert not module_pattern_matches(":feature:<feature>:api", ":feature:journey")
    assert not module_pattern_matches(":feature:<feature>:api", ":feature:journey:api:v2")
    assert not module_pattern_matches(":feature:<feature>:api", ":feature::api")
    assert module_pattern_matches(":core:domain", ":core:domain")
    assert not module_pattern_matches(":core:domain", ":core:data")

    roles = [{"id": "feature-api", "modules": [":feature:<feature>:api"]}, "not a dict"]
    assert role_owns_module(roles, ":feature:journey:api")
    assert not role_owns_module(roles, ":feature:journey:presentation")
    assert not role_owns_module([], ":feature:journey:api")


def test_every_activation_root_has_a_role_pattern_that_can_prove_adoption():
    """반증: role 표와 어긋난 activation_root는 어떤 저장소에서도 켜지지 않아 필수 gate가 조용히 n/a가 된다."""
    from agent_flow.core.architecture_lint import activation_role_patterns

    profiles_dir = KIT_ROOT / "src" / "agent_flow" / "profiles"
    checked = 0
    for path in sorted(profiles_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        architecture = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("architecture") or {}
        if architecture.get("strict_when_roots_present") is not True:
            continue
        roles = architecture.get("roles") or []
        for activation_root in architecture.get("activation_roots") or []:
            assert activation_role_patterns(roles, (activation_root.strip("/"),)), (
                f"{path.name}: activation root {activation_root} has no role path under it"
            )
            checked += 1
    assert checked > 0
