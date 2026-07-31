from __future__ import annotations

import sys
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
    (adopted / "core" / "domain" / "auth").mkdir(parents=True)
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
    (adopted / "core" / "domain" / "auth").mkdir(parents=True)
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
    monkeypatch.setattr(architecture_lint, "run_safe_command", lambda *args, **kwargs: failed)

    with pytest.raises(ValueError, match="git diff.*cannot read index"):
        architecture_lint.changed_files(tmp_path)


def test_unmapped_files_outside_managed_roots_do_not_fail_android_lint(tmp_path):
    """반증: 채택 저장소의 build-logic 변경까지 role 미매핑으로 막으면 필수 gate가 개발을 멈춘다."""
    from agent_flow.core.architecture_lint import lint_project

    (tmp_path / "core" / "domain" / "auth").mkdir(parents=True)
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
