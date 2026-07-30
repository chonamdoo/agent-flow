from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

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
    # 이 저장소는 한국어 주석이 규칙이다. 한글이 뒤에 붙어도 단어는 끝난 것이다.
    assert len(_forbidden(ANDROID_PRESENTATION_ROLE, "Chat.kt", "// OrderDto를 만든다\n")) == 1
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


def test_android_lint_activates_only_with_a_domain_root(tmp_path):
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
