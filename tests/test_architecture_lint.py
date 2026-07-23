from __future__ import annotations

import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core.architecture_lint import validate_package_suffix  # noqa: E402


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
