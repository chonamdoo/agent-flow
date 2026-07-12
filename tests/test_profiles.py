from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agent_flow.core.profiles import (
    active_profile_ids,
    detect_profile,
    load_profile,
    load_project_profile_payload,
    primary_profile_id,
    profile_prompt_projection,
    render_profile_prompt_block,
)
from agent_flow.runner import Runner, _run_base_snapshot


def test_detects_spring_boot_maven_project(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        """
<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
  </parent>
</project>
""".strip(),
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "spring"


def test_explicit_profiles_use_portable_casefold_deduplication(tmp_path: Path) -> None:
    assert active_profile_ids(tmp_path, "Node,node,NODE") == ["node"]


def test_explicit_profiles_reject_unicode_before_casefolding(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid profile name"):
        active_profile_ids(tmp_path, "K")


def test_detects_spring_boot_gradle_project_before_android(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "service"\n', encoding="utf-8")
    (tmp_path / "build.gradle.kts").write_text(
        'plugins { id("org.springframework.boot") version "3.5.0" }\n',
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "spring"


def test_android_gradle_project_is_not_misdetected_as_spring(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "android-app"\n', encoding="utf-8")
    (tmp_path / "build.gradle.kts").write_text(
        'plugins { id("com.android.application") version "9.0.0" apply false }\n',
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "android"


def test_plain_maven_project_does_not_claim_spring(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.example</groupId><artifactId>plain</artifactId></project>\n",
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "generic"


@pytest.mark.parametrize(
    "marker",
    ("requirements-dev.txt", "Pipfile", "poetry.lock", "uv.lock"),
)
def test_python_lockfile_markers_select_python(tmp_path: Path, marker: str) -> None:
    (tmp_path / marker).write_text("\n", encoding="utf-8")

    assert detect_profile(tmp_path) == "python"


def test_detects_one_level_spring_service_module(tmp_path: Path) -> None:
    service = tmp_path / "orders-service"
    service.mkdir()
    (tmp_path / "settings.gradle.kts").write_text('include(":orders-service")\n', encoding="utf-8")
    (service / "build.gradle.kts").write_text(
        'plugins { id("org.springframework.boot") version "3.5.0" }\n',
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "spring"


@pytest.mark.parametrize("ignored_dir", ("node_modules", ".cache"))
def test_spring_detection_ignores_dependency_and_hidden_directories(
    tmp_path: Path,
    ignored_dir: str,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "latest"}}),
        encoding="utf-8",
    )
    candidate = tmp_path / ignored_dir
    candidate.mkdir()
    (candidate / "pom.xml").write_text(
        "<project><artifactId>spring-boot</artifactId></project>\n",
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "react"


def test_runner_detects_spring_profile_without_install_metadata(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><parent><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-parent</artifactId></parent></project>\n",
        encoding="utf-8",
    )

    runner = Runner(tmp_path, config_root=tmp_path, workflow="default")

    assert runner.profile_id == "spring"
    assert runner.profile["id"] == "spring"


@pytest.mark.parametrize(
    "dependency",
    ("express", "@nestjs/core", "fastify", "koa", "@hapi/hapi", "hapi"),
)
def test_typescript_node_backend_dependency_selects_node(
    tmp_path: Path,
    dependency: str,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {dependency: "latest"}}),
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    assert detect_profile(tmp_path) == "node"


def test_plain_typescript_project_is_not_promoted_to_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"typescript": "latest"}}),
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    assert detect_profile(tmp_path) == "typescript"


def test_react_native_web_dependency_does_not_select_react_native(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"react": "19", "react-native-web": "0.20"}}
        ),
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "react"


@pytest.mark.parametrize("dependency", ("react-native", "expo"))
def test_exact_react_native_markers_select_react_native(
    tmp_path: Path,
    dependency: str,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {dependency: "latest", "react": "19"}}),
        encoding="utf-8",
    )

    assert detect_profile(tmp_path) == "react-native"


def test_node_typescript_catalog_and_profile_schema_mirrors_match() -> None:
    root = Path(__file__).resolve().parents[1]
    source_profile = root / "profiles" / "node.yaml"
    packaged_profile = root / "src" / "agent_flow" / "profiles" / "node.yaml"
    source_schema = root / "profiles" / "_schema.yaml"
    packaged_schema = root / "src" / "agent_flow" / "profiles" / "_schema.yaml"

    assert source_profile.read_bytes() == packaged_profile.read_bytes()
    assert source_schema.read_bytes() == packaged_schema.read_bytes()
    profile = yaml.safe_load(source_profile.read_text(encoding="utf-8"))
    assert "typescript-development-guide" in profile["skills"]["install"]
    assert profile["typescript_skills"] == {
        "implementation": [
            {
                "skill": "typescript-development-guide",
                "when": "Node.js TypeScript files, tsconfig, or TypeScript task scope changes",
            }
        ],
        "review": [
            {
                "skill": "typescript-development-guide",
                "when": "reviewing Node.js TypeScript files, tsconfig, or TypeScript task scope",
            }
        ],
    }


def test_spring_gates_support_gradle_and_maven_projects() -> None:
    profile = load_profile("spring")
    commands = {gate.gate_id: " ".join(gate.command) for gate in profile.gates}

    assert "./gradlew" in commands["build"]
    assert "./mvnw" in commands["build"]
    assert "./gradlew" in commands["test"]
    assert "./mvnw" in commands["test"]


def test_profile_prompt_projection_keeps_phase_contract_and_trims_runtime_roles() -> None:
    profile = {
        "id": "custom",
        "branching": {"base": "release", "naming": {"max_slug_length": 37}},
        "gates": [{"id": "verify", "command": ["custom-check", "--strict"]}],
        "skills": {"install": ["large-catalog"]},
        "android_skills": {"implementation": [{"skill": "large-android-catalog"}]},
        "architecture": {
            "contract": "clean",
            "roles": [{"id": "domain", "paths": ["domain"]}],
        },
    }

    planning = profile_prompt_projection(profile, "slice-plan")
    runtime = profile_prompt_projection(profile, "implement")
    rendered = render_profile_prompt_block("custom", profile, "implement")

    assert planning["architecture"]["roles"][0]["id"] == "domain"
    assert "roles" not in runtime["architecture"]
    assert runtime["branching"]["naming"]["max_slug_length"] == 37
    assert runtime["gates"][0]["command"] == ["custom-check", "--strict"]
    assert "skills" not in runtime
    assert "android_skills" not in runtime
    assert "custom-check" in rendered
    assert "large-catalog" not in rendered


def _write_installed_profile(
    root: Path,
    profile: str,
    *,
    base: str = "HEAD",
    worktree: str = "required",
    max_slug_length: int = 60,
) -> None:
    profile_root = root / ".agent-flow" / "profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    (profile_root / f"{profile}.yaml").write_text(
        "\n".join(
            [
                f"id: {profile}",
                "branching:",
                f"  worktree: {worktree}",
                f"  base: {base}",
                "  naming:",
                '    prefix: "feat/"',
                f"    max_slug_length: {max_slug_length}",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"profile": "python", "profiles": ["android", "python"]}, "python"),
        ({"profile": "generic", "profiles": ["android", "python"]}, "android"),
        ({"profile": "python", "profiles": []}, "python"),
        ({"profiles": []}, "generic"),
        (
            {
                "profile": "generic",
                "primary_profile": "python",
                "profiles": ["android", "python"],
            },
            "python",
        ),
    ),
)
def test_primary_profile_resolution_and_legacy_fallback(
    tmp_path: Path,
    payload: dict[str, object],
    expected: str,
) -> None:
    profiles = {
        "generic",
        str(payload.get("profile") or "generic"),
        str(payload.get("primary_profile") or expected),
        *(str(profile) for profile in payload.get("profiles", [])),
    }
    for profile in profiles:
        _write_installed_profile(tmp_path, profile)
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    assert primary_profile_id(tmp_path) == expected


def test_installed_profile_snapshot_rejects_missing_symlink_and_wrong_id(
    tmp_path: Path,
) -> None:
    agent_flow = tmp_path / ".agent-flow"
    profiles = agent_flow / "profiles"
    profiles.mkdir(parents=True)
    (agent_flow / "kit.json").write_text(
        json.dumps(
            {
                "profile": "python",
                "primary_profile": "python",
                "profiles": ["python"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing or unreadable"):
        primary_profile_id(tmp_path)

    outside = tmp_path / "outside.yaml"
    outside.write_text("id: python\n", encoding="utf-8")
    (profiles / "python.yaml").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        primary_profile_id(tmp_path)

    (profiles / "python.yaml").unlink()
    (profiles / "python.yaml").write_text("id: android\n", encoding="utf-8")
    with pytest.raises(ValueError, match="id mismatch"):
        primary_profile_id(tmp_path)

    (profiles / "python.yaml").write_text("id: python\n- malformed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable installed profile"):
        primary_profile_id(tmp_path)


@pytest.mark.parametrize(
    ("profile_text", "message"),
    (
        ("- id: python\n", "must be a mapping"),
        ("id: python\nbranching: []\n", "branching must be a mapping"),
        (
            "id: python\nbranching:\n  worktree: true\n",
            "branching.worktree",
        ),
        (
            "id: python\nbranching:\n  base: --upload-pack=malicious\n",
            "branching.base is unsafe",
        ),
        (
            "id: python\nbranching:\n  naming: scalar\n",
            "branching.naming must be a mapping",
        ),
        (
            "id: python\nbranching:\n  naming:\n    prefix: feature/\n",
            "branching.naming.prefix",
        ),
        (
            "id: python\nbranching:\n  naming:\n    max_slug_length: 11\n",
            "branching.naming.max_slug_length",
        ),
        ("id: python\ngates: {}\n", "gates must be a list"),
    ),
)
def test_installed_profile_snapshot_rejects_invalid_runtime_shape(
    tmp_path: Path,
    profile_text: str,
    message: str,
) -> None:
    profile_root = tmp_path / ".agent-flow" / "profiles"
    profile_root.mkdir(parents=True)
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps({"primary_profile": "python", "profiles": ["python"]}),
        encoding="utf-8",
    )
    (profile_root / "python.yaml").write_text(profile_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_project_profile_payload(tmp_path, "python")


def test_installed_profile_snapshot_accepts_safe_custom_base(tmp_path: Path) -> None:
    _write_installed_profile(tmp_path, "python", base="origin/develop")
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps({"primary_profile": "python", "profiles": ["python"]}),
        encoding="utf-8",
    )

    payload = load_project_profile_payload(tmp_path, "python")

    assert payload["branching"]["base"] == "origin/develop"


def test_installed_profile_snapshot_accepts_canonical_inline_yaml(tmp_path: Path) -> None:
    profile_root = tmp_path / ".agent-flow" / "profiles"
    profile_root.mkdir(parents=True)
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps({"primary_profile": "python", "profiles": ["python"]}),
        encoding="utf-8",
    )
    (profile_root / "python.yaml").write_text(
        "id: python\n"
        "branching: {base: main, worktree: disabled, "
        'naming: {prefix: "feat/", max_slug_length: 12}}\n'
        "gates: []\n",
        encoding="utf-8",
    )

    payload = load_project_profile_payload(tmp_path, "python")

    assert payload["branching"]["worktree"] == "disabled"
    assert payload["branching"]["naming"]["max_slug_length"] == 12


def test_python_worktree_settings_use_primary_installed_profile(tmp_path: Path) -> None:
    from agent_flow.cli import (
        _profile_worktree_mode,
        _worktree_base_ref,
        _worktree_max_slug_length,
    )

    _write_installed_profile(tmp_path, "android", base="main")
    _write_installed_profile(
        tmp_path,
        "python",
        base="develop",
        worktree="disabled",
        max_slug_length=17,
    )
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "profile": "generic",
                "primary_profile": "python",
                "profiles": ["python"],
            }
        ),
        encoding="utf-8",
    )

    assert _worktree_base_ref(tmp_path) == "develop"
    assert _worktree_max_slug_length(tmp_path) == 17
    assert _profile_worktree_mode(tmp_path, ["python"]) == "disabled"


@pytest.mark.parametrize(
    ("primary", "profile_order", "expected"),
    (
        ("python", ["python", "node"], "disabled"),
        ("python", ["node", "python"], "disabled"),
        ("node", ["python", "node"], "required"),
        ("node", ["node", "python"], "required"),
    ),
)
def test_python_worktree_mode_uses_only_canonical_primary_profile(
    tmp_path: Path,
    primary: str,
    profile_order: list[str],
    expected: str,
) -> None:
    from agent_flow.cli import _profile_worktree_mode

    _write_installed_profile(tmp_path, "python", worktree="disabled")
    _write_installed_profile(tmp_path, "node", worktree="required")
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "profile": "generic",
                "primary_profile": primary,
                "profiles": profile_order,
            }
        ),
        encoding="utf-8",
    )

    assert _profile_worktree_mode(tmp_path, profile_order) == expected


def test_installed_runner_ignores_env_override_and_uses_primary_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_installed_profile(tmp_path, "android", base="main")
    _write_installed_profile(tmp_path, "python", base="develop")
    (tmp_path / ".agent-flow" / "kit.json").write_text(
        json.dumps(
            {
                "profile": "generic",
                "primary_profile": "python",
                "profiles": ["android", "python"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_FLOW_PROFILE", "android")

    runner = Runner(tmp_path, config_root=tmp_path, workflow="default")

    assert runner.profile_id == "android,python"
    assert runner.profile["primary_profile"] == "python"
    assert _run_base_snapshot(runner.profile, tmp_path)["base_ref"] == "develop"
