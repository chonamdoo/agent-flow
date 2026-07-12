from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from agent_flow.adapters.base import _profile_projection
from agent_flow.core.local_skills import (
    applicable_code_review_skill_docs,
    parse_skill_frontmatter,
)
from agent_flow.core.skill_plan import (
    SkillPlanSnapshotError,
    _changed_files,
    _configured_base_commit,
    _validate_installed_android_official_provenance,
    resolve_runtime_skill_plan,
)


ROOT = Path(__file__).resolve().parent.parent
PROFILE_ROUTING = json.loads((ROOT / "skills" / "profile-routing.json").read_text(encoding="utf-8"))


def test_crlf_skill_frontmatter_matches_lf_and_node_metadata_plan(tmp_path: Path) -> None:
    lines = [
        "---",
        "name: crlf-policy",
        "activation: conditional",
        "workflowPhases: [implement, review]",
        "taskTerms: [intent security]",
        "pathGlobs: [app/src/**/AndroidManifest.xml]",
        "hosts: [claude, codex, omp]",
        "dependencies: [dependency-skill]",
        "---",
        "Use when testing line endings.",
        "",
    ]
    lf = "\n".join(lines)
    crlf = "\r\n".join(lines)
    expected = {
        "name": "crlf-policy",
        "activation": "conditional",
        "workflowPhases": ["implement", "review"],
        "taskTerms": ["intent security"],
        "pathGlobs": ["app/src/**/AndroidManifest.xml"],
        "hosts": ["claude", "codex", "omp"],
        "dependencies": ["dependency-skill"],
    }

    assert parse_skill_frontmatter(lf) == expected
    assert parse_skill_frontmatter(crlf) == expected
    script = (
        'import fs from "node:fs"; '
        'import { parseSkillFrontmatter } from "./lib/skill-selection.mjs"; '
        'process.stdout.write(JSON.stringify(parseSkillFrontmatter(fs.readFileSync(0,"utf8"))));'
    )
    node = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        input=crlf,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(node.stdout) == expected

    skill = tmp_path / ".agent-flow" / "local-skills" / "directory-alias"
    skill.mkdir(parents=True)
    skill_path = skill / "SKILL.md"
    logical_docs = []
    for text in (lf, crlf):
        skill_path.write_bytes(text.encode("utf-8"))
        docs = applicable_code_review_skill_docs(
            tmp_path,
            "implement",
            task_scope="intent security",
        )
        logical_docs.append(
            [
                (
                    doc.name,
                    doc.activation,
                    doc.workflow_phases,
                    doc.task_terms,
                    doc.path_globs,
                )
                for doc in docs
            ]
        )
    assert logical_docs[0] == logical_docs[1] == [
        (
            "crlf-policy",
            "conditional",
            ("implement", "review"),
            ("intent security",),
            ("app/src/**/AndroidManifest.xml",),
        )
    ]


def test_android_official_skill_routes_match_node_and_python() -> None:
    official = ["agp-9-upgrade", "android-intent-security", "play-policy-insights"]
    index = {
        "selection": {
            "profiles": ["android"],
            "skill_profiles": ["android"],
            "required_review": {"android": ["android-code-review"]},
            "conditional_skills": {
                "android": {"implementation": official, "review": official}
            },
            "profile_routing": PROFILE_ROUTING,
        },
        "skills": [
            {
                "name": name,
                "path": f"skills/{name}/SKILL.md",
                "tree_hash": name,
            }
            for name in [
                "code-generation-discipline",
                "android-code-review",
                *official,
            ]
        ],
    }
    cases = [
        ({"taskScope": "Migrate this Android app to AGP 9"}, "agp-9-upgrade"),
        (
            {"changedFiles": ["app/src/main/AndroidManifest.xml"]},
            "android-intent-security",
        ),
        (
            {"taskScope": "Run a Google Play Data Safety compliance audit"},
            "play-policy-insights",
        ),
    ]
    script = (
        'import fs from "node:fs"; '
        'import { resolveRuntimeSkillPlan } from "./lib/skill-selection.mjs"; '
        'const p=JSON.parse(fs.readFileSync(0,"utf8")); '
        'process.stdout.write(JSON.stringify(p.cases.map(c => '
        'resolveRuntimeSkillPlan(p.index,{phaseId:"review",...c}))));'
    )
    node = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps({"index": index, "cases": [case for case, _ in cases]}),
        text=True,
        capture_output=True,
        check=True,
    )
    node_plans = json.loads(node.stdout)

    for node_plan, (arguments, expected) in zip(node_plans, cases):
        python_plan = resolve_runtime_skill_plan(
            index,
            "review",
            arguments.get("changedFiles", []),
            arguments.get("taskScope", ""),
        )
        assert node_plan == python_plan
        assert expected in {skill["name"] for skill in python_plan["skills"]}


def test_react_native_android_change_uses_same_touched_profile_union() -> None:
    index = {
        "selection": {
            "profiles": ["react-native"],
            "skill_profiles": ["android", "react-native"],
            "required_review": {
                "android": ["android-code-review"],
                "react-native": ["react-native-development-guide"],
            },
            "conditional_skills": {"android": {"implementation": [], "review": []}},
            "profile_routing": PROFILE_ROUTING,
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            {"name": "android-code-review", "path": "skills/android-code-review/SKILL.md", "tree_hash": "b"},
            {"name": "react-native-development-guide", "path": "skills/react-native-development-guide/SKILL.md", "tree_hash": "c"},
        ],
    }

    plan = resolve_runtime_skill_plan(
        index,
        "review",
        ["android/app/src/main/MainActivity.kt"],
    )

    assert plan["touched_profiles"] == ["android", "react-native"]
    assert [skill["name"] for skill in plan["skills"]] == [
        "android-code-review",
        "code-generation-discipline",
        "react-native-development-guide",
    ]


def test_python_change_narrows_multi_profile_plan() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["android", "python"],
                "skill_profiles": ["android", "python"],
                "required_review": {
                    "android": ["android-code-review"],
                    "python": ["python-development-guide"],
                },
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "android-code-review", "path": "skills/android-code-review/SKILL.md", "tree_hash": "b"},
                {"name": "python-development-guide", "path": "skills/python-development-guide/SKILL.md", "tree_hash": "c"},
            ],
        },
        "review",
        ["src/service.py"],
    )

    assert plan["touched_profiles"] == ["python"]
    assert [skill["name"] for skill in plan["skills"]] == [
        "code-generation-discipline",
        "python-development-guide",
    ]


def test_react_native_android_change_without_snapshot_fails_closed() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["react-native"],
                "skill_profiles": ["react-native"],
                "required_review": {
                    "android": ["android-clean-architecture", "android-code-review"],
                    "react-native": ["react-native-development-guide"],
                },
                "conditional_skills": {"android": {"implementation": [], "review": []}},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "react-native-development-guide", "path": "skills/react-native-development-guide/SKILL.md", "tree_hash": "b"},
            ],
        },
        "review",
        ["android/app/src/main/MainActivity.kt"],
    )

    assert plan["touched_profiles"] == ["android", "react-native"]
    assert plan["missing_profiles"] == ["android"]
    assert plan["missing"] == ["android-clean-architecture", "android-code-review"]


def test_android_compose_path_selects_matching_declared_specialist() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["android"],
                "skill_profiles": ["android"],
                "required_review": {"android": ["android-code-review"]},
                "conditional_skills": {
                    "android": {
                        "implementation": [],
                        "review": ["compose-side-effects", "compose-state-authoring"],
                    }
                },
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "android-code-review", "path": "skills/android-code-review/SKILL.md", "tree_hash": "b"},
                {"name": "compose-side-effects", "path": "skills/compose-side-effects/SKILL.md", "tree_hash": "c"},
                {"name": "compose-state-authoring", "path": "skills/compose-state-authoring/SKILL.md", "tree_hash": "d"},
            ],
        },
        "review",
        ["app/src/main/ui/FeatureScreen.kt"],
    )

    assert [skill["name"] for skill in plan["skills"]] == [
        "android-code-review",
        "code-generation-discipline",
        "compose-state-authoring",
    ]


def test_node_and_python_runtime_skill_plans_match() -> None:
    index = {
        "selection": {
            "profiles": ["android", "python"],
            "skill_profiles": ["android", "python"],
            "required_review": {
                "android": ["android-code-review"],
                "python": ["python-development-guide"],
            },
            "conditional_skills": {
                "android": {"implementation": [], "review": ["compose-state-authoring"]}
            },
            "profile_routing": PROFILE_ROUTING,
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            {"name": "android-code-review", "path": "skills/android-code-review/SKILL.md", "tree_hash": "b"},
            {"name": "python-development-guide", "path": "skills/python-development-guide/SKILL.md", "tree_hash": "c"},
            {"name": "compose-state-authoring", "path": "skills/compose-state-authoring/SKILL.md", "tree_hash": "d"},
        ],
    }
    payload = {
        "index": index,
        "phaseId": "review",
        "changedFiles": ["app/ui/FeatureScreen.kt", "src/api.py"],
        "taskScope": "Jetpack Compose UI and Python API",
    }
    script = (
        'import fs from "node:fs"; '
        'import { resolveRuntimeSkillPlan } from "./lib/skill-selection.mjs"; '
        'const p=JSON.parse(fs.readFileSync(0,"utf8")); '
        'process.stdout.write(JSON.stringify(resolveRuntimeSkillPlan(p.index,{phaseId:p.phaseId,changedFiles:p.changedFiles,taskScope:p.taskScope})));'
    )
    node = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    python_plan = resolve_runtime_skill_plan(
        index,
        payload["phaseId"],
        payload["changedFiles"],
        payload["taskScope"],
    )
    assert json.loads(node.stdout) == python_plan


def test_node_and_python_runtime_skill_plan_order_matches_for_portable_names() -> None:
    cases = [
        {
            "index": {
                "selection": {
                    "profiles": ["node"],
                    "skill_profiles": ["node"],
                    "required_review": {},
                    "conditional_skills": {},
                    "profile_routing": {},
                },
                "skills": [
                    {
                        "name": "code-generation-discipline",
                        "path": "skills/code-generation-discipline/SKILL.md",
                        "tree_hash": "a",
                    }
                ],
            },
            "changedFiles": ["\U0001f600.js", "\ue000.js"],
        },
        {
            "index": {
                "selection": {
                    "profiles": ["python", "node"],
                    "skill_profiles": [],
                    "required_review": {},
                    "conditional_skills": {},
                    "profile_routing": {},
                },
                "skills": [
                    {
                        "name": "code-generation-discipline",
                        "path": "skills/code-generation-discipline/SKILL.md",
                        "tree_hash": "a",
                    }
                ],
            },
            "changedFiles": [],
        },
    ]
    script = (
        'import fs from "node:fs"; '
        'import { resolveRuntimeSkillPlan } from "./lib/skill-selection.mjs"; '
        'const cases=JSON.parse(fs.readFileSync(0,"utf8")); '
        'process.stdout.write(JSON.stringify(cases.map(item => '
        'resolveRuntimeSkillPlan(item.index,{phaseId:"implement",changedFiles:item.changedFiles}))));'
    )
    node = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        check=True,
    )
    python_plans = [
        resolve_runtime_skill_plan(
            case["index"],
            "implement",
            case["changedFiles"],
        )
        for case in cases
    ]

    assert json.loads(node.stdout) == python_plans
    assert python_plans[0]["changed_files"] == ["\ue000.js", "\U0001f600.js"]
    assert python_plans[1]["touched_profiles"] == ["node", "python"]
    assert python_plans[1]["missing_profiles"] == ["node", "python"]


def test_node_and_python_same_family_task_union_match() -> None:
    index = {
        "selection": {
            "profiles": ["node", "react"],
            "skill_profiles": ["node", "react"],
            "required_review": {
                "node": ["node-development-guide"],
                "react": ["react-development-guide"],
            },
            "conditional_skills": {},
            "profile_routing": PROFILE_ROUTING,
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            {"name": "node-development-guide", "path": "skills/node-development-guide/SKILL.md", "tree_hash": "b"},
            {"name": "react-development-guide", "path": "skills/react-development-guide/SKILL.md", "tree_hash": "c"},
        ],
    }
    task_scope = "Build a React frontend and Node.js API"
    script = (
        'import fs from "node:fs"; '
        'import { resolveRuntimeSkillPlan } from "./lib/skill-selection.mjs"; '
        'const p=JSON.parse(fs.readFileSync(0,"utf8")); '
        'process.stdout.write(JSON.stringify(resolveRuntimeSkillPlan(p.index,{phaseId:"implement",taskScope:p.taskScope})));'
    )
    node = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps({"index": index, "taskScope": task_scope}),
        text=True,
        capture_output=True,
        check=True,
    )
    python_plan = resolve_runtime_skill_plan(
        index,
        "implement",
        task_scope=task_scope,
    )

    assert json.loads(node.stdout) == python_plan
    assert python_plan["touched_profiles"] == ["node", "react"]


def test_node_typescript_conditional_plan_matches_javascript_resolver() -> None:
    index = {
        "selection": {
            "profiles": ["node"],
            "skill_profiles": ["node"],
            "required_review": {"node": ["node-development-guide"]},
            "conditional_skills": {
                "node": {
                    "implementation": ["typescript-development-guide"],
                    "review": ["typescript-development-guide"],
                }
            },
            "profile_routing": PROFILE_ROUTING,
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            {"name": "node-development-guide", "path": "skills/node-development-guide/SKILL.md", "tree_hash": "b"},
            {"name": "typescript-development-guide", "path": "skills/typescript-development-guide/SKILL.md", "tree_hash": "c"},
        ],
    }
    cases = [
        {"phaseId": "implement", "changedFiles": ["server/orders.ts"], "taskScope": ""},
        {"phaseId": "review", "changedFiles": ["tsconfig.json"], "taskScope": ""},
        {"phaseId": "implement", "changedFiles": [], "taskScope": "Implement a NestJS TypeScript API"},
        {"phaseId": "review", "changedFiles": ["server/orders.js"], "taskScope": "Review the Express API"},
    ]
    script = (
        'import fs from "node:fs"; '
        'import { resolveRuntimeSkillPlan } from "./lib/skill-selection.mjs"; '
        'const p=JSON.parse(fs.readFileSync(0,"utf8")); '
        'process.stdout.write(JSON.stringify(p.cases.map((item) => '
        'resolveRuntimeSkillPlan(p.index,{phaseId:item.phaseId,changedFiles:item.changedFiles,taskScope:item.taskScope}))));'
    )
    node = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps({"index": index, "cases": cases}),
        text=True,
        capture_output=True,
        check=True,
    )
    python_plans = [
        resolve_runtime_skill_plan(
            index,
            case["phaseId"],
            case["changedFiles"],
            case["taskScope"],
        )
        for case in cases
    ]

    assert json.loads(node.stdout) == python_plans
    assert [skill["name"] for skill in python_plans[0]["skills"]] == [
        "code-generation-discipline",
        "node-development-guide",
        "typescript-development-guide",
    ]
    assert [skill["name"] for skill in python_plans[-1]["skills"]] == [
        "code-generation-discipline",
        "node-development-guide",
    ]


def test_task_scope_and_changed_files_form_one_profile_union() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["android", "python"],
                "skill_profiles": ["android", "python"],
                "required_review": {
                    "android": ["android-code-review"],
                    "python": ["python-development-guide"],
                },
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "android-code-review", "path": "skills/android-code-review/SKILL.md", "tree_hash": "b"},
                {"name": "python-development-guide", "path": "skills/python-development-guide/SKILL.md", "tree_hash": "c"},
            ],
        },
        "review",
        ["src/service.py"],
        "안드로이드 컴포즈 화면과 파이썬 API를 함께 수정",
    )

    assert plan["touched_profiles"] == ["android", "python"]
    assert plan["task_scope"] == "안드로이드 컴포즈 화면과 파이썬 API를 함께 수정"


def test_task_scope_keeps_separately_named_profiles_in_same_family() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["node", "react"],
                "skill_profiles": ["node", "react"],
                "required_review": {
                    "node": ["node-development-guide"],
                    "react": ["react-development-guide"],
                },
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "node-development-guide", "path": "skills/node-development-guide/SKILL.md", "tree_hash": "b"},
                {"name": "react-development-guide", "path": "skills/react-development-guide/SKILL.md", "tree_hash": "c"},
            ],
        },
        "implement",
        task_scope="Build a React frontend and Node.js API",
    )

    assert plan["touched_profiles"] == ["node", "react"]
    assert [skill["name"] for skill in plan["skills"]] == [
        "code-generation-discipline",
        "node-development-guide",
        "react-development-guide",
    ]


def test_task_scope_uses_longer_overlapping_profile_term() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["react", "react-native"],
                "skill_profiles": ["react", "react-native"],
                "required_review": {},
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            ],
        },
        "implement",
        task_scope="Build a React Native screen",
    )

    assert plan["touched_profiles"] == ["react-native"]


def test_react_native_web_task_scope_prefers_react_without_native_evidence() -> None:
    index = {
        "selection": {
            "profiles": ["react", "react-native"],
            "skill_profiles": ["react", "react-native"],
            "required_review": {
                "react": ["react-development-guide"],
                "react-native": ["react-native-development-guide"],
            },
            "conditional_skills": {},
            "profile_routing": PROFILE_ROUTING,
        },
        "skills": [
            {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            {"name": "react-development-guide", "path": "skills/react-development-guide/SKILL.md", "tree_hash": "b"},
            {"name": "react-native-development-guide", "path": "skills/react-native-development-guide/SKILL.md", "tree_hash": "c"},
        ],
    }
    for task_scope in (
        "Build a react-native-web component",
        "Build a React Native Web component",
    ):
        plan = resolve_runtime_skill_plan(index, "implement", task_scope=task_scope)
        assert plan["touched_profiles"] == ["react"]
        assert [skill["name"] for skill in plan["skills"]] == [
            "code-generation-discipline",
            "react-development-guide",
        ]

    generic_web_plan = resolve_runtime_skill_plan(
        index,
        "implement",
        ["src/App.tsx"],
        task_scope="Build a react-native-web component",
    )
    assert generic_web_plan["touched_profiles"] == ["react"]

    native_plan = resolve_runtime_skill_plan(
        index,
        "implement",
        ["src/screens/NativeHome.tsx"],
        task_scope="Build a react-native-web component",
    )
    assert native_plan["touched_profiles"] == ["react", "react-native"]


def test_family_specificity_separates_web_and_backend_typescript_files() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["nextjs", "node", "react", "typescript"],
                "skill_profiles": ["nextjs", "node", "react", "typescript"],
                "required_review": {
                    "nextjs": ["react-development-guide"],
                    "node": ["node-development-guide"],
                    "react": ["react-development-guide"],
                    "typescript": ["typescript-development-guide"],
                },
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "node-development-guide", "path": "skills/node-development-guide/SKILL.md", "tree_hash": "b"},
                {"name": "react-development-guide", "path": "skills/react-development-guide/SKILL.md", "tree_hash": "c"},
                {"name": "typescript-development-guide", "path": "skills/typescript-development-guide/SKILL.md", "tree_hash": "d"},
            ],
        },
        "review",
        ["server/routes.ts", "app/page.tsx"],
    )

    assert plan["touched_profiles"] == ["nextjs", "node"]
    assert [skill["name"] for skill in plan["skills"]] == [
        "code-generation-discipline",
        "node-development-guide",
        "react-development-guide",
    ]


def test_generic_profile_is_suppressed_when_specific_file_profile_matches() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["generic", "python"],
                "skill_profiles": ["generic", "python"],
                "required_review": {},
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            ],
        },
        "review",
        ["src/service.py"],
    )

    assert plan["touched_profiles"] == ["python"]


def test_spring_path_outranks_android_extension_only_match() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["android", "spring"],
                "skill_profiles": ["android", "spring"],
                "required_review": {
                    "android": ["android-code-review"],
                    "spring": ["spring-development-guide"],
                },
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
                {"name": "android-code-review", "path": "skills/android-code-review/SKILL.md", "tree_hash": "b"},
                {"name": "spring-development-guide", "path": "skills/spring-development-guide/SKILL.md", "tree_hash": "c"},
            ],
        },
        "review",
        ["src/main/java/com/example/OrderService.java"],
    )

    assert plan["touched_profiles"] == ["spring"]
    assert [skill["name"] for skill in plan["skills"]] == [
        "code-generation-discipline",
        "spring-development-guide",
    ]


def test_android_module_path_outranks_spring_source_pattern() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["android", "spring"],
                "skill_profiles": ["android", "spring"],
                "required_review": {},
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            ],
        },
        "review",
        ["feature/orders/src/main/kotlin/OrdersScreen.kt"],
    )

    assert plan["touched_profiles"] == ["android"]


def test_unmatched_early_task_scope_keeps_all_active_profiles() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["android", "python"],
                "skill_profiles": ["android", "python"],
                "required_review": {},
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            ],
        },
        "implement",
        task_scope="Fix the login regression",
    )

    assert plan["touched_profiles"] == ["android", "python"]


def test_task_terms_do_not_match_inside_larger_ascii_words() -> None:
    plan = resolve_runtime_skill_plan(
        {
            "selection": {
                "profiles": ["react", "spring"],
                "skill_profiles": ["react", "spring"],
                "required_review": {},
                "conditional_skills": {},
                "profile_routing": PROFILE_ROUTING,
            },
            "skills": [
                {"name": "code-generation-discipline", "path": "skills/code-generation-discipline/SKILL.md", "tree_hash": "a"},
            ],
        },
        "implement",
        task_scope="Fix Spring reactive streams",
    )

    assert plan["touched_profiles"] == ["spring"]


def test_changed_file_collection_includes_deletions_and_pinned_base() -> None:
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        source = root / "app" / "DeletedScreen.kt"
        source.parent.mkdir()
        source.write_text("class DeletedScreen\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, env=git_env, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
        source.unlink()

        assert "app/DeletedScreen.kt" in _changed_files(root, base)


def test_configured_base_commit_uses_profile_base_not_hardcoded_main() -> None:
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "main"], cwd=root, env=git_env, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "develop"], cwd=root, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "develop"], cwd=root, env=git_env, check=True)
        develop = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "-b", "feat/work"], cwd=root, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "feature"], cwd=root, env=git_env, check=True)
        agent_flow = root / ".agent-flow"
        (agent_flow / "profiles").mkdir(parents=True)
        (agent_flow / "kit.json").write_text('{"profile":"spring"}\n', encoding="utf-8")
        (agent_flow / "profiles" / "spring.yaml").write_text(
            "id: spring\nbranching:\n  base: develop\n",
            encoding="utf-8",
        )

        assert _configured_base_commit(root, root) == develop


def test_non_code_phase_loads_no_profile_skill_docs() -> None:
    plan = resolve_runtime_skill_plan({}, "product-brief", ["android/Main.kt"])
    assert plan["skills"] == []
    assert plan["touched_profiles"] == []


def test_android_profile_projection_omits_install_catalog_from_phase_prompts() -> None:
    profile = yaml.safe_load((ROOT / "profiles" / "android.yaml").read_text(encoding="utf-8"))

    red = yaml.safe_dump(_profile_projection(profile, "red"), sort_keys=False)
    architecture_review = yaml.safe_dump(
        _profile_projection(profile, "architecture-review"),
        sort_keys=False,
    )

    assert "android_skills:" not in red + architecture_review
    assert "android_ecosystem_skills:" not in red + architecture_review
    assert "chrisbanes_skills:" not in red + architecture_review
    assert len(red) < 2_000
    assert len(architecture_review) < 5_000


def test_python_runtime_lookup_casefolds_names_and_rejects_conflicts() -> None:
    selection = {
        "profiles": ["NODE", "node"],
        "required_review": {"node": ["NODE-DEVELOPMENT-GUIDE"]},
        "conditional_skills": {},
        "profile_routing": PROFILE_ROUTING,
    }
    plan = resolve_runtime_skill_plan(
        {
            "selection": selection,
            "skills": [
                {
                    "name": "Code-Generation-Discipline",
                    "path": "skills/code/SKILL.md",
                    "tree_hash": "a",
                },
                {
                    "name": "Node-Development-Guide",
                    "path": "skills/node/SKILL.md",
                    "tree_hash": "b",
                },
            ],
        },
        "review",
    )
    assert plan["active_profiles"] == ["node"]
    assert [skill["name"] for skill in plan["skills"]] == [
        "code-generation-discipline",
        "node-development-guide",
    ]

    with pytest.raises(
        SkillPlanSnapshotError,
        match="conflicting installed skill index logical skill name: guide",
    ):
        resolve_runtime_skill_plan(
            {
                "selection": selection,
                "skills": [
                    {"name": "guide", "path": "skills/a/SKILL.md", "tree_hash": "a"},
                    {"name": "GUIDE", "path": "skills/b/SKILL.md", "tree_hash": "b"},
                ],
            },
            "review",
        )


@pytest.mark.parametrize("name", ["skill name", ".hidden", "a..b", "ᾲ", "ὰι"])
def test_python_runtime_rejects_nonportable_skill_names(name: str) -> None:
    with pytest.raises(
        SkillPlanSnapshotError,
        match="installed skill index has invalid skill name",
    ):
        resolve_runtime_skill_plan(
            {
                "selection": {
                    "profiles": ["node"],
                    "required_review": {"node": ["guide"]},
                    "conditional_skills": {},
                    "profile_routing": PROFILE_ROUTING,
                },
                "skills": [
                    {"name": name, "path": "skills/guide/SKILL.md", "tree_hash": "a"},
                ],
            },
            "review",
        )


@pytest.mark.parametrize("name", ["skill name", ".hidden", "a..b", "ᾲ", "ὰι"])
def test_python_project_local_tree_rejects_nonportable_skill_names(
    tmp_path: Path,
    name: str,
) -> None:
    skill_root = tmp_path / ".agent-flow" / "local-skills" / "unsafe-policy"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {name}\nactivation: always\n---\npolicy\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsafe project-local skill name"):
        applicable_code_review_skill_docs(tmp_path, "review")


def test_python_runtime_validates_android_official_lock_without_network(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / ".agent-flow" / "skills"
    profiles_root = tmp_path / ".agent-flow" / "profiles"
    skills_root.mkdir(parents=True)
    profiles_root.mkdir(parents=True)
    license_path = skills_root / "LICENSE.txt"
    license_path.write_text("license\n", encoding="utf-8")
    (profiles_root / "android.yaml").write_text(
        "id: android\n"
        "android_skills:\n"
        "  implementation:\n"
        "    - skill: guide\n"
        "      when: fixture\n"
        "  review: []\n",
        encoding="utf-8",
    )
    policy = "offline-catalog-lock-and-indexed-project-snapshot"
    (skills_root / "source-policy.yaml").write_text(
        "official_project_snapshots:\n"
        "  source: https://github.com/android/skills\n"
        f"  commit: {'a' * 40}\n"
        '  catalog: "profiles/android.yaml#android_skills.implementation"\n'
        f"  install_policy: {policy}\n"
        "  runtime_fetch: false\n"
        "  offline_validation: required\n"
        "  runtime_tree_verification: installed-index\n",
        encoding="utf-8",
    )
    lock = {
        "android_official": {
            "source": "https://github.com/android/skills",
            "commit": "a" * 40,
            "policy": policy,
            "runtime_fetch": False,
            "catalog": "profiles/android.yaml#android_skills.implementation",
            "runtime_tree_verification": "installed-index",
            "license_reference": "LICENSE.txt",
            "license_sha256": hashlib.sha256(license_path.read_bytes()).hexdigest(),
            "snapshots": {
                "guide": {
                    "upstream_path": "fixture/guide",
                    "upstream_tree_hash": "b" * 64,
                    "upstream_skill_sha256": "c" * 64,
                    "project_tree_hash": None,
                    "snapshot_mode": "install-time-indexed",
                }
            },
        }
    }
    (skills_root / "upstream-lock.json").write_text(
        json.dumps(lock),
        encoding="utf-8",
    )
    index = {
        "selection": {"profiles": ["android"], "skill_profiles": ["android"]},
        "skills": [{"name": "GUIDE", "path": "skills/guide/SKILL.md"}],
    }

    _validate_installed_android_official_provenance(tmp_path, index)
    (profiles_root / "android.yaml").write_text(
        "id: android\nandroid_skills:\n  implementation: []\n  review: []\n",
        encoding="utf-8",
    )
    with pytest.raises(
        SkillPlanSnapshotError,
        match="catalog does not match lock coverage",
    ):
        _validate_installed_android_official_provenance(tmp_path, index)
