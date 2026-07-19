from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_flow.core.skill_compatibility import (
    SkillCompatibilityError,
    normalize_skill_compatibility,
)
from agent_flow.core.skill_plan import (
    SkillPlanSnapshotError,
    canonical_skill_plan_bytes,
    resolve_runtime_skill_plan,
)


KIT_ROOT = Path(__file__).resolve().parent.parent


def _node_plan(
    index: dict[str, object],
    *,
    phase: str,
    files: list[str],
    task: str,
    required_skills: list[str] | None = None,
) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { resolveRuntimeSkillPlan } from './lib/skill-selection.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(resolveRuntimeSkillPlan(input.index, {
  phaseId: input.phase,
  changedFiles: input.files,
  taskScope: input.task,
  requiredSkills: input.required_skills,
})));
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(
            {
                "index": index,
                "phase": phase,
                "files": files,
                "task": task,
                "required_skills": required_skills or [],
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)

def _node_plan_outcome(
    index: dict[str, object],
    *,
    phase: str,
    required_skills: list[str],
) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { resolveRuntimeSkillPlan } from './lib/skill-selection.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const value = resolveRuntimeSkillPlan(input.index, {
    phaseId: input.phase,
    requiredSkills: input.required_skills,
  });
  process.stdout.write(JSON.stringify({ ok: true, value }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: String(error.message || error).replace(/^blocked: /, ''),
  }));
}
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(
            {
                "index": index,
                "phase": phase,
                "required_skills": required_skills,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _node_compatibility_normalization(value: object) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { normalizeSkillCompatibility } from './lib/skill-compatibility.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  process.stdout.write(JSON.stringify({ ok: true, value: normalizeSkillCompatibility(input) }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: error.message }));
}
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _node_provider_metadata(index: dict[str, object]) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { canonicalizeRuntimeSkillProviderMetadata } from './lib/skill-selection.mjs';
const index = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(canonicalizeRuntimeSkillProviderMetadata(index)));
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(index),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _index(host: str, *, explicit: bool = False) -> dict[str, object]:
    return {
        "version": 2,
        "revision": "f" * 64,
        "selection": {
            "profiles": [],
            "explicit_skills": ["testing-localization"] if explicit else [],
            "profile_routing": {"profiles": {}, "escalations": {}},
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "code", "tree_hash": "a" * 64},
            {
                "name": "always-code",
                "path": "always",
                "tree_hash": "b" * 64,
                "source": "host-bootstrap",
                "source_host": host,
                "activation": "always",
                "requires": ["testing-localization"],
            },
            {
                "name": "conditional",
                "path": "conditional",
                "tree_hash": "c" * 64,
                "source": "host-bootstrap",
                "source_host": host,
                "activation": "conditional",
                "workflowPhases": ["implement"],
                "taskTerms": ["payment"],
                "pathGlobs": ["src/pay/**"],
            },
            {
                "name": "testing-localization",
                "path": "testing",
                "tree_hash": "d" * 64,
                "activation": "always",
            },
        ],
    }


@pytest.mark.parametrize("host", ["claude", "codex", "omp"])
def test_node_and_python_share_runtime_plan_for_each_real_host(host: str) -> None:
    index = _index(host)
    node = _node_plan(index, phase="implement", files=["src/pay/api.ts"], task="payment retry")
    python = resolve_runtime_skill_plan(
        index,
        phase_id="implement",
        changed_files=["src/pay/api.ts"],
        task_scope="payment retry",
    )

    assert python == node
    assert [skill["name"] for skill in python["skills"]] == [
        "always-code",
        "code-generation-discipline",
        "conditional",
    ]


def test_testing_localization_enters_both_runtimes_only_when_explicit() -> None:
    implicit = _index("codex")
    explicit = _index("codex", explicit=True)

    implicit_node = _node_plan(implicit, phase="implement", files=[], task="")
    implicit_python = resolve_runtime_skill_plan(implicit, phase_id="implement")
    explicit_node = _node_plan(explicit, phase="implement", files=[], task="")
    explicit_python = resolve_runtime_skill_plan(explicit, phase_id="implement")

    assert implicit_python == implicit_node
    assert explicit_python == explicit_node
    assert "testing-localization" not in {skill["name"] for skill in implicit_python["skills"]}
    assert "testing-localization" in {skill["name"] for skill in explicit_python["skills"]}


def test_architecture_phase_includes_declared_cross_platform_skills_in_both_runtimes() -> None:
    index = _index("codex")
    index["skills"].extend(
        [
            {"name": "clean-architecture-core", "path": "clean", "tree_hash": "e" * 64},
            {
                "name": "ddd-architecture",
                "path": "ddd",
                "tree_hash": "f" * 64,
                "requires": ["clean-architecture-core"],
            },
        ]
    )
    required = ["ddd-architecture"]

    node = _node_plan(
        index,
        phase="ddd-design",
        files=[],
        task="architecture",
        required_skills=required,
    )
    python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        task_scope="architecture",
        required_skills=required,
    )

    assert python == node
    assert [skill["name"] for skill in python["skills"]] == [
        "always-code",
        "clean-architecture-core",
        "ddd-architecture",
    ]

    tuple_python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        task_scope="architecture",
        required_skills=tuple(required),
    )
    assert tuple_python == python


@pytest.mark.parametrize(
    ("files", "touched_profiles", "expected"),
    [
        (
            ["src/App.tsx"],
            ["react-native"],
            {
                "code-generation-discipline",
                "clean-architecture-core",
                "react-native-clean-architecture",
                "react-native-development-guide",
            },
        ),
        (
            ["android/app/src/main/java/demo/Main.kt"],
            ["android", "react-native"],
            {
                "code-generation-discipline",
                "clean-architecture-core",
                "android-clean-architecture",
                "android-code-review",
                "react-native-clean-architecture",
                "react-native-development-guide",
            },
        ),
        (
            ["ios/App/App.swift"],
            ["ios", "react-native"],
            {
                "code-generation-discipline",
                "clean-architecture-core",
                "ios-clean-architecture",
                "react-native-clean-architecture",
                "react-native-development-guide",
            },
        ),
    ],
)
def test_react_native_native_roots_inject_only_touched_platform_skills_in_both_runtimes(
    files: list[str],
    touched_profiles: list[str],
    expected: set[str],
) -> None:
    routing = json.loads((KIT_ROOT / "skills" / "profile-routing.json").read_text(encoding="utf-8"))
    skills = [
        {"name": "code-generation-discipline", "path": "code"},
        {"name": "clean-architecture-core", "path": "core"},
        {
            "name": "react-native-clean-architecture",
            "path": "rn-clean",
            "requires": ["clean-architecture-core"],
        },
        {"name": "react-native-development-guide", "path": "rn-guide"},
        {
            "name": "android-clean-architecture",
            "path": "android-clean",
            "requires": ["clean-architecture-core"],
        },
        {"name": "android-code-review", "path": "android-review"},
        {
            "name": "ios-clean-architecture",
            "path": "ios-clean",
            "requires": ["clean-architecture-core"],
        },
    ]
    for index, skill in enumerate(skills):
        skill["tree_hash"] = format(index, "x") * 64
    index = {
        "selection": {
            "profiles": ["react-native"],
            "skill_profiles": ["android", "ios", "react-native"],
            "explicit_skills": [],
            "required_review": {
                "react-native": [
                    "react-native-development-guide",
                    "react-native-clean-architecture",
                ],
                "android": ["android-code-review", "android-clean-architecture"],
                "ios": ["ios-clean-architecture"],
            },
            "profile_routing": routing,
        },
        "skills": skills,
    }

    node = _node_plan(index, phase="implement", files=files, task="")
    python = resolve_runtime_skill_plan(index, phase_id="implement", changed_files=files)

    assert python == node
    assert python["touched_profiles"] == touched_profiles
    selected = {skill["name"] for skill in python["skills"]}
    assert selected == expected


def _profile_matrix_index() -> dict[str, object]:
    routing = json.loads(
        (KIT_ROOT / "skills" / "profile-routing.json").read_text(encoding="utf-8")
    )
    required_review = {
        "android": ["android-code-review", "android-clean-architecture"],
        "nextjs": [
            "react-development-guide",
            "typescript-development-guide",
            "react-clean-architecture",
        ],
        "python": ["python-development-guide", "python-api-clean-architecture"],
    }
    android_conditional = {
        skill
        for route in routing["profiles"]["android"]["skill_routes"]
        for skill in route.get("skills", [])
    }
    names = {
        "code-generation-discipline",
        "clean-architecture-core",
        *required_review["android"],
        *required_review["nextjs"],
        *required_review["python"],
        *android_conditional,
    }
    skills = [
        {
            "name": name,
            "path": f"skills/{name}",
            "tree_hash": hashlib.sha256(name.encode()).hexdigest(),
            "requires": (
                ["clean-architecture-core"]
                if name
                in {
                    "android-clean-architecture",
                    "react-clean-architecture",
                    "python-api-clean-architecture",
                }
                else []
            ),
        }
        for name in sorted(names)
    ]
    return {
        "selection": {
            "profiles": ["android", "nextjs", "python"],
            "skill_profiles": ["android", "nextjs", "python"],
            "explicit_skills": [],
            "required_review": required_review,
            "conditional_skills": {
                "android": {
                    "implementation": sorted(android_conditional),
                    "review": sorted(android_conditional),
                }
            },
            "profile_routing": routing,
        },
        "skills": skills,
    }


@pytest.mark.parametrize(
    ("files", "touched", "expected"),
    [
        (
            ["app/src/main/java/demo/MainActivity.kt"],
            ["android"],
            {
                "code-generation-discipline",
                "android-code-review",
                "android-clean-architecture",
                "clean-architecture-core",
            },
        ),
        (
            ["feature/home/presentation/HomeScreen.kt"],
            ["android"],
            {
                "code-generation-discipline",
                "android-code-review",
                "android-clean-architecture",
                "clean-architecture-core",
                "compose-state-authoring",
                "compose-state-hoisting",
                "compose-state-holder-ui-split",
                "kotlin-flow-state-event-modeling",
            },
        ),
        (
            ["core/data/user/UserFlow.kt"],
            ["android"],
            {
                "code-generation-discipline",
                "android-code-review",
                "android-clean-architecture",
                "clean-architecture-core",
                "kotlin-coroutines-structured-concurrency",
                "kotlin-flow-state-event-modeling",
            },
        ),
        (
            ["src/app/agreements/page.tsx"],
            ["nextjs"],
            {
                "code-generation-discipline",
                "react-development-guide",
                "typescript-development-guide",
                "react-clean-architecture",
                "clean-architecture-core",
            },
        ),
        (
            ["src/app/agreements/api.py"],
            ["python"],
            {
                "code-generation-discipline",
                "python-development-guide",
                "python-api-clean-architecture",
                "clean-architecture-core",
            },
        ),
        (
            ["app/src/main/java/demo/Main.kt", "src/app/api.py"],
            ["android", "python"],
            {
                "code-generation-discipline",
                "android-code-review",
                "android-clean-architecture",
                "python-development-guide",
                "python-api-clean-architecture",
                "clean-architecture-core",
            },
        ),
        (
            ["docs/agent-flow.md"],
            [],
            {"code-generation-discipline"},
        ),
        (
            ["assets/unknown.payload"],
            [],
            {"code-generation-discipline"},
        ),
    ],
)
def test_changed_scope_selects_only_relevant_profile_skills_in_both_runtimes(
    files: list[str],
    touched: list[str],
    expected: set[str],
) -> None:
    index = _profile_matrix_index()

    node = _node_plan(index, phase="implement", files=files, task="")
    python = resolve_runtime_skill_plan(
        index,
        phase_id="implement",
        changed_files=files,
    )

    assert python == node
    assert python["touched_profiles"] == touched
    selected = {skill["name"] for skill in python["skills"]}
    assert selected == expected


def test_required_skill_missing_is_identical_and_explicit_in_both_runtimes() -> None:
    index = _profile_matrix_index()
    required = ["missing-required-skill"]

    node = _node_plan(
        index,
        phase="implement",
        files=["src/app/page.tsx"],
        task="",
        required_skills=required,
    )
    python = resolve_runtime_skill_plan(
        index,
        phase_id="implement",
        changed_files=["src/app/page.tsx"],
        required_skills=required,
    )

    assert python == node
    assert python["missing"] == required


def test_compatibility_resolution_is_identical_in_both_runtimes() -> None:
    index = _index("codex")
    index["compatibility"] = {
        "skills": [
            {
                "canonical": "code-generation-discipline",
                "aliases": ["code-gen"],
                "renamed_from": ["old-code-gen"],
                "capabilities": ["implementation.code-generation"],
            }
        ]
    }
    required = ["code-gen", "old-code-gen"]

    node = _node_plan(
        index,
        phase="ddd-design",
        files=[],
        task="architecture",
        required_skills=required,
    )
    python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        task_scope="architecture",
        required_skills=required,
    )

    assert python == node
    assert node["missing"] == []
    assert [skill["name"] for skill in node["skills"]] == [
        "always-code",
        "code-generation-discipline",
    ]
    assert node["skills"][1]["capabilities"] == ["implementation.code-generation"]


def _index_with_provider_claims() -> dict[str, object]:
    index = _index("codex")
    fingerprint = "9" * 64
    index["provider_registry"] = {
        "version": 1,
        "fingerprint": fingerprint,
        "quarantined": [],
    }
    index["skill_providers"] = [
        {
            "concrete_id": skill["name"],
            "aliases": (
                ["code-generation"]
                if skill["name"] == "code-generation-discipline"
                else []
            ),
            "provider_id": "organization",
            "provider_version": "1.0.0",
            "trust_tier": "organization",
            "ownership": "organization",
            "provenance_revision": None,
            "source": "project://skills",
            "source_hash": skill["tree_hash"],
            "source_host": skill.get("source_host"),
            "source_kind": skill.get("source", "bundled"),
            "source_locator": (
                f"host://{skill['source_host']}/skills/{skill['name']}"
                if skill.get("source_host")
                else f"project://skills/{skill['name']}"
            ),
            "content_hash_mode": "verified",
            "catalog_ref": None,
            "catalog_hash": None,
            "adapter": "source-kind",
            "registry_fingerprint": fingerprint,
            "status": "verified",
            "compatibility": {
                "registry": 1,
                "profiles": ["*"],
                "hosts": ["*"],
                "source_kinds": ["bundled"],
            },
        }
        for skill in index["skills"]
    ]
    return index


def test_provider_identity_propagates_identically_in_both_runtimes() -> None:
    index = _index_with_provider_claims()
    fingerprint = "9" * 64

    node = _node_plan(
        index,
        phase="ddd-design",
        files=[],
        task="architecture",
        required_skills=["code-generation-discipline"],
    )
    python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        task_scope="architecture",
        required_skills=["code-generation-discipline"],
    )

    assert python == node
    selected = next(
        skill for skill in node["skills"]
        if skill["name"] == "code-generation-discipline"
    )
    assert selected["provider"] == {
        "id": "organization",
        "version": "1.0.0",
        "trust_tier": "organization",
        "ownership": "organization",
        "provenance_revision": None,
        "source": "project://skills",
        "adapter": "source-kind",
        "source_hash": "a" * 64,
        "source_host": None,
        "source_kind": "bundled",
        "source_locator": "project://skills/code-generation-discipline",
        "content_hash_mode": "verified",
        "catalog_ref": None,
        "catalog_hash": None,
        "status": "verified",
        "registry_fingerprint": fingerprint,
        "compatibility": {
            "registry": 1,
            "profiles": ["*"],
            "hosts": ["*"],
            "source_kinds": ["bundled"],
        },
    }


def test_numeric_integer_provider_registry_version_matches_both_runtimes() -> None:
    index = _index_with_provider_claims()
    registry = index["provider_registry"]
    assert isinstance(registry, dict)
    registry["version"] = 1.0

    node = _node_plan(
        index,
        phase="ddd-design",
        files=[],
        task="",
        required_skills=["code-generation-discipline"],
    )
    python = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        required_skills=["code-generation-discipline"],
    )

    assert python == node

def test_numeric_integer_provider_version_has_one_canonical_hash_projection(
    tmp_path: Path,
) -> None:
    index = _index_with_provider_claims()
    for skill in index["skills"]:
        assert isinstance(skill, dict)
        skill_root = tmp_path / str(skill["name"])
        skill_root.mkdir()
        document = skill_root / "SKILL.md"
        document.write_text("---\nname: fixture\n---\n", encoding="utf-8")
        skill["path"] = document.relative_to(tmp_path).as_posix()
    integer = json.loads(json.dumps(index))
    floating = json.loads(json.dumps(index))
    floating["provider_registry"]["version"] = 1.0

    assert canonical_skill_plan_bytes(integer, tmp_path) == canonical_skill_plan_bytes(
        floating,
        tmp_path,
    )
    assert _node_provider_metadata(floating)["provider_registry"]["version"] == 1

def _malformed_provider_index(case: str) -> dict[str, object]:
    index = json.loads(json.dumps(_index_with_provider_claims()))
    if case == "provider-metadata-explicit-null":
        index["provider_registry"] = None
        index["skill_providers"] = None
        return index
    registry = index["provider_registry"]
    claims = index["skill_providers"]
    assert isinstance(registry, dict)
    assert isinstance(claims, list)
    assert isinstance(claims[0], dict)
    claim = claims[0]
    if case == "registry-version-bool":
        registry["version"] = True
    elif case == "registry-extra":
        registry["ignored"] = True
    elif case == "registry-missing-quarantined":
        del registry["quarantined"]
    elif case == "registry-fingerprint-list":
        registry["fingerprint"] = ["9" * 64]
    elif case == "registry-fingerprint-newline":
        registry["fingerprint"] = f"{'9' * 64}\n"
        claim["registry_fingerprint"] = registry["fingerprint"]
    elif case == "registry-version-fraction":
        registry["version"] = 1.5
    elif case == "registry-quarantined-surrogate":
        registry["quarantined"] = [{
            "reason": "provider_metadata_invalid",
            "provider_id": None,
            "detail": "\ud800",
            "metadata_path": "skill-provider-registry.json#provider:unknown",
            "repairable": False,
        }]
    elif case == "registry-quarantined-object":
        registry["quarantined"] = {}
    elif case == "claims-missing":
        del index["skill_providers"]
    elif case == "claims-object":
        index["skill_providers"] = {}
    elif case == "claim-extra":
        claim["ignored"] = True
    elif case == "claim-missing-status":
        del claim["status"]
    elif case == "claim-missing-adapter":
        del claim["adapter"]
    elif case == "claim-missing-compatibility":
        del claim["compatibility"]
    elif case == "claim-missing-provenance-revision":
        del claim["provenance_revision"]
    elif case == "claim-compatibility-extra":
        claim["compatibility"]["ignored"] = True
    elif case == "claim-compatibility-registry-bool":
        claim["compatibility"]["registry"] = True
    elif case == "claim-compatibility-source-kinds-string":
        claim["compatibility"]["source_kinds"] = "bundled"
    elif case == "claim-adapter-bool":
        claim["adapter"] = True
    elif case == "claim-adapter-newline":
        claim["adapter"] = "source-kind\n"
    elif case == "claim-aliases-non-list":
        claim["aliases"] = "legacy"
    elif case == "claim-alias-object":
        claim["aliases"] = [{"name": "legacy"}]
    elif case == "claim-duplicate-alias":
        claim["aliases"] = ["legacy", "LEGACY"]
    elif case == "claim-concrete-alias":
        claim["aliases"] = [claim["concrete_id"]]
    elif case == "claim-concrete-id-list":
        claim["concrete_id"] = ["code-generation-discipline"]
    elif case == "claim-concrete-id-newline":
        claim["concrete_id"] = "code-generation-discipline\n"
    elif case == "claim-provider-id-list":
        claim["provider_id"] = ["organization"]
    elif case == "claim-provider-id-newline":
        claim["provider_id"] = "organization\n"
    elif case == "claim-source-hash-list":
        claim["source_hash"] = ["a" * 64]
    elif case == "claim-source-hash-newline":
        claim["source_hash"] = f"{'a' * 64}\n"
    elif case == "claim-trust-list":
        claim["trust_tier"] = ["organization"]
    elif case == "claim-ownership-object":
        claim["ownership"] = {"name": "organization"}
    elif case == "claim-provenance-revision-object":
        claim["provenance_revision"] = {"revision": "a" * 40}
    elif case == "claim-provenance-revision-newline":
        claim["provenance_revision"] = f"{'a' * 40}\n"
    elif case == "claim-registry-fingerprint-list":
        claim["registry_fingerprint"] = ["9" * 64]
    elif case == "claim-status-list":
        claim["status"] = ["verified"]
    elif case == "claim-version-float":
        claim["provider_version"] = 1.0
    elif case == "claim-version-newline":
        claim["provider_version"] = "1.0.0\n"
    elif case == "claim-version-unicode":
        claim["provider_version"] = "١.٢.٣"
    elif case == "claim-source-object":
        claim["source"] = {"path": "project://skills"}
    elif case == "claim-source-control":
        claim["source"] = "project://skills\u001c"
    elif case == "claim-source-next-line":
        claim["source"] = "project://skills\u0085"
    elif case == "claim-source-bom":
        claim["source"] = "project://skills\ufeff"
    elif case == "claim-source-surrogate":
        claim["source"] = "project://skills\ud800"
    elif case == "claim-source-host-list":
        claim["source_host"] = ["codex"]
    elif case == "claim-source-kind-list":
        claim["source_kind"] = ["bundled"]
    elif case == "claim-source-locator-control":
        claim["source_locator"] = "project://skills\u001c"
    elif case == "claim-content-hash-mode-list":
        claim["content_hash_mode"] = ["verified"]
    elif case == "claim-catalog-pair-mismatch":
        claim["catalog_ref"] = "profile://android/android_skills"
    elif case == "claim-observed-status-mismatch":
        claim["content_hash_mode"] = "observed"
    else:
        raise AssertionError(f"unknown malformed provider case: {case}")
    return index


@pytest.mark.parametrize(
    "case",
    [
        "provider-metadata-explicit-null",
        "registry-version-bool",
        "registry-extra",
        "registry-missing-quarantined",
        "registry-fingerprint-list",
        "registry-quarantined-object",
        "registry-fingerprint-newline",
        "registry-version-fraction",
        "registry-quarantined-surrogate",
        "claims-missing",
        "claims-object",
        "claim-extra",
        "claim-missing-adapter",
        "claim-missing-status",
        "claim-missing-compatibility",
        "claim-missing-provenance-revision",
        "claim-compatibility-extra",
        "claim-compatibility-registry-bool",
        "claim-compatibility-source-kinds-string",
        "claim-adapter-bool",
        "claim-adapter-newline",
        "claim-aliases-non-list",
        "claim-alias-object",
        "claim-duplicate-alias",
        "claim-concrete-alias",
        "claim-concrete-id-list",
        "claim-provider-id-list",
        "claim-concrete-id-newline",
        "claim-source-hash-list",
        "claim-provider-id-newline",
        "claim-trust-list",
        "claim-ownership-object",
        "claim-source-hash-newline",
        "claim-provenance-revision-object",
        "claim-provenance-revision-newline",
        "claim-registry-fingerprint-list",
        "claim-status-list",
        "claim-version-float",
        "claim-version-unicode",
        "claim-version-newline",
        "claim-source-object",
        "claim-source-control",
        "claim-source-next-line",
        "claim-source-bom",
        "claim-source-surrogate",
        "claim-source-host-list",
        "claim-source-kind-list",
        "claim-source-locator-control",
        "claim-content-hash-mode-list",
        "claim-catalog-pair-mismatch",
        "claim-observed-status-mismatch",
    ],
)
def test_malformed_provider_metadata_is_rejected_identically_in_both_runtimes(
    case: str,
) -> None:
    index = _malformed_provider_index(case)
    node = _node_plan_outcome(
        index,
        phase="ddd-design",
        required_skills=["code-generation-discipline"],
    )
    try:
        resolve_runtime_skill_plan(
            index,
            phase_id="ddd-design",
            required_skills=["code-generation-discipline"],
        )
    except SkillPlanSnapshotError as exc:
        python = {"ok": False, "error": str(exc).removeprefix("blocked: ")}
    else:
        python = {"ok": True}

    assert node == python
    assert node == {
        "ok": False,
        "error": (
            "invalid skill provider index"
            if case.startswith("registry-")
            or case in {
                "claims-missing",
                "claims-object",
                "provider-metadata-explicit-null",
            }
            else "invalid skill provider claim"
        ),
    }


@pytest.mark.parametrize(
    "compatibility",
    [
        {"version": True},
        {"version": None},
        {"version": 1.0},
        {"skills": None},
        {"skills": [{"canonical": None}]},
        {"skills": [{"canonical": "bad/name"}]},
        {"skills": [{"canonical": "one", "status": []}]},
        {"skills": [{"canonical": "one", "capabilities": "capability"}]},
        {"skills": [{"canonical": "one", "aliases": ["same", "SAME"]}]},
        {
            "skills": [
                {
                    "canonical": "one",
                    "aliases": ["same"],
                    "renamed_from": ["SAME"],
                }
            ]
        },
        {
            "skills": [
                {
                    "canonical": "one",
                    "status": "deprecated",
                    "replaced_by": ["two", "TWO"],
                }
            ]
        },
        {
            "skills": [
                {
                    "canonical": "one",
                    "status": "active",
                    "replaced_by": "two",
                }
            ]
        },
        {
            "skills": [
                {
                    "canonical": "one",
                    "status": "deprecated",
                    "replaced_by": "two",
                },
                {
                    "canonical": "two",
                    "status": "renamed",
                    "replaced_by": "one",
                },
            ]
        },
        {
            "skills": [
                {
                    "canonical": "one",
                    "status": "deprecated",
                    "aliases": ["legacy-one"],
                    "replaced_by": "legacy-one",
                }
            ]
        },
    ],
)
def test_compatibility_metadata_validation_is_identical_in_both_runtimes(
    compatibility: dict[str, object],
) -> None:
    try:
        normalized = normalize_skill_compatibility(compatibility)
    except SkillCompatibilityError as exc:
        python = {"ok": False, "error": str(exc)}
    else:
        python = {"ok": True, "value": normalized}

    assert _node_compatibility_normalization(compatibility) == python

def test_installed_route_skill_activates_without_catalog_membership_in_both_runtimes() -> None:
    # A routed skill that is installed (bundled skills.install) but NOT a conditional-catalog
    # member must still activate, identically in Node and Python.
    routing = {
        "version": 1,
        "profiles": {
            "android": {
                "task_terms": ["android crash"],
                "file_rules": [{"path_terms": ["debug"]}],
                "skill_routes": [
                    {
                        "id": "android-debugging",
                        "task_terms": ["android crash"],
                        "file_rules": [{"path_terms": ["debug"]}],
                        "skills": ["android-debugging"],
                    }
                ],
            }
        },
        "escalations": {},
    }
    index = {
        "selection": {
            "profiles": ["android"],
            "skill_profiles": ["android"],
            "explicit_skills": [],
            "required_review": {},
            "conditional_skills": {"android": {"implementation": [], "review": []}},
            "profile_routing": routing,
        },
        "skills": [
            {
                "name": name,
                "path": f"skills/{name}",
                "tree_hash": hashlib.sha256(name.encode()).hexdigest(),
            }
            for name in ("android-debugging", "code-generation-discipline")
        ],
    }
    files = ["app/src/debug/CrashReporter.kt"]
    node = _node_plan(index, phase="implement", files=files, task="android crash")
    python = resolve_runtime_skill_plan(
        index, phase_id="implement", changed_files=files, task_scope="android crash"
    )
    assert python == node
    assert "android-debugging" in {skill["name"] for skill in python["skills"]}

def test_uppercase_route_skill_matches_installed_skill_case_insensitively_in_both_runtimes() -> None:
    # A route referencing an installed skill in a different case must be casefold-matched in the
    # installed-index admission branch identically by Node and Python (no parity drift).
    routing = {
        "version": 1,
        "profiles": {
            "android": {
                "task_terms": ["android crash"],
                "file_rules": [{"path_terms": ["debug"]}],
                "skill_routes": [
                    {
                        "id": "android-debugging",
                        "task_terms": ["android crash"],
                        "file_rules": [{"path_terms": ["debug"]}],
                        "skills": ["Android-Debugging"],
                    }
                ],
            }
        },
        "escalations": {},
    }
    index = {
        "selection": {
            "profiles": ["android"],
            "skill_profiles": ["android"],
            "explicit_skills": [],
            "required_review": {},
            "conditional_skills": {"android": {"implementation": [], "review": []}},
            "profile_routing": routing,
        },
        "skills": [
            {
                "name": name,
                "path": f"skills/{name}",
                "tree_hash": hashlib.sha256(name.encode()).hexdigest(),
            }
            for name in ("android-debugging", "code-generation-discipline")
        ],
    }
    files = ["app/src/debug/CrashReporter.kt"]
    node = _node_plan(index, phase="implement", files=files, task="android crash")
    python = resolve_runtime_skill_plan(
        index, phase_id="implement", changed_files=files, task_scope="android crash"
    )
    assert python == node
    assert "android-debugging" in {skill["name"] for skill in python["skills"]}


_CAPABILITY_CATALOG = {
    "version": 1,
    "skills": [
        {"canonical": "compose-state-authoring", "capabilities": ["compose.state"]},
        {"canonical": "compose-state-holder-ui-split", "capabilities": ["compose.state"]},
        {"canonical": "python-development-guide", "capabilities": ["lang.python"]},
        {
            "canonical": "old-guide",
            "status": "deprecated",
            "capabilities": ["lang.python"],
            "replaced_by": ["python-development-guide"],
        },
    ],
}


def _capability_catalog():
    from agent_flow.core.skill_compatibility import SkillCompatibilityCatalog

    return SkillCompatibilityCatalog.from_value(_CAPABILITY_CATALOG)


def test_capability_providers_indexes_active_records_only() -> None:
    providers = _capability_catalog().capability_providers()
    assert providers["compose.state"] == (
        "compose-state-authoring",
        "compose-state-holder-ui-split",
    )
    # deprecated old-guide is excluded even though it declares lang.python
    assert providers["lang.python"] == ("python-development-guide",)


def test_resolve_capability_single_provider_resolves() -> None:
    resolution = _capability_catalog().resolve_capability(
        "lang.python", ["python-development-guide"]
    )
    assert resolution.resolved is True
    assert resolution.canonical == "python-development-guide"
    assert resolution.reason is None


def test_resolve_capability_zero_available_is_unresolved() -> None:
    resolution = _capability_catalog().resolve_capability("lang.python", [])
    assert resolution.resolved is False
    assert resolution.canonical is None
    assert resolution.reason == "capability_unresolved"


def test_resolve_capability_multiple_available_is_ambiguous() -> None:
    resolution = _capability_catalog().resolve_capability(
        "compose.state",
        ["compose-state-authoring", "compose-state-holder-ui-split"],
    )
    assert resolution.resolved is False
    assert resolution.canonical is None
    assert resolution.reason == "stack_ambiguity"
    assert resolution.providers == (
        "compose-state-authoring",
        "compose-state-holder-ui-split",
    )


def _node_resolve_capability(
    catalog: dict[str, object], capability: str, available: list[str]
) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import fs from 'node:fs';
import { createSkillCompatibilityCatalog } from './lib/skill-compatibility.mjs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const cat = createSkillCompatibilityCatalog(input.catalog);
const r = cat.resolveCapability(input.capability, input.available);
process.stdout.write(JSON.stringify({
  resolved: r.resolved,
  canonical: r.canonical,
  providers: r.providers,
  reason: r.reason,
}));
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script),
        cwd=KIT_ROOT,
        input=json.dumps(
            {"catalog": catalog, "capability": capability, "available": available}
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "capability,available",
    [
        ("lang.python", ["python-development-guide"]),
        ("lang.python", []),
        ("compose.state", ["compose-state-authoring", "compose-state-holder-ui-split"]),
        ("compose.state", ["compose-state-authoring"]),
        ("missing.cap", ["python-development-guide"]),
    ],
)
def test_capability_resolution_parity_node_python(
    capability: str, available: list[str]
) -> None:
    node = _node_resolve_capability(_CAPABILITY_CATALOG, capability, available)
    py = _capability_catalog().resolve_capability(capability, available)
    assert node == {
        "resolved": py.resolved,
        "canonical": py.canonical,
        "providers": list(py.providers),
        "reason": py.reason,
    }
