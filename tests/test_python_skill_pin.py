from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_flow.artifact import (
    ActiveRun,
    create_run,
    ensure_run_skill_plan_pinned,
    read_meta,
    write_meta,
)
from agent_flow.core.skill_plan import (
    MANAGED_HOOK_VERIFIER,
    SKILL_PLAN_HASH_VERSION,
    SkillPlanSnapshotError,
    canonical_skill_plan_bytes,
    compute_skill_plan_hash,
    hash_skill_tree,
    installed_skill_plan_pin,
    managed_hook_contract_commitment,
    managed_host_files_commitment,
    _trusted_managed_hook_script_name,
)
from agent_flow.core.local_skills import project_local_skill_plan_hash
from agent_flow.core.lore_snapshot import lore_snapshot_hash
from agent_flow.core.state import (
    RunRequest,
    start_run,
    status_summary,
    verify_run_state_snapshots,
)


KIT_ROOT = Path(__file__).resolve().parent.parent
MANAGED_HOST_FIXTURES = {
    ".Codex/agents/code-reviewer.md": (".Codex/agents/code-reviewer.md", b"codex reviewer\n"),
    ".claude/agents/code-reviewer.md": (".claude/agents/code-reviewer.md", b"claude reviewer\n"),
    ".omp/agents/code-reviewer.md": (".claude/agents/code-reviewer.md", b"claude reviewer\n"),
    ".omp/extensions/agent-flow-hooks.ts": ("generated:omp-hooks-extension", b"omp hooks\n"),
}
MANAGED_HOOK_SCRIPT_NAMES = (
    "guard-worktree.sh",
    "guard-worktree-write.py",
    "guard-protected-branch.sh",
    "show-phase-status.sh",
    "comment-checker.py",
)
from agent_flow.cli import _write_stage_prompts
from agent_flow.runner import (
    ResumeMode,
    Runner,
    _ensure_lore_snapshot,
    _lore_snapshot_metadata,
)


def _install_skill_fixture(root: Path) -> tuple[dict, str]:
    skill_rows = []
    for name, content in (
        ("alpha-guide", b"---\nname: alpha-guide\n---\n\x00binary\n"),
        ("beta-guide", "---\nname: beta-guide\n---\n한글\n".encode()),
        ("private-use-guide", "---\nname: private-use-guide\n---\n".encode()),
        ("emoji-guide", "---\nname: emoji-guide\n---\n".encode()),
    ):
        skill_dir = root / ".agent-flow" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(content)
        references = skill_dir / "references"
        references.mkdir()
        (references / "guide.txt").write_text(f"{name}\n", encoding="utf-8")
        (references / "\ue000.txt").write_text("private-use\n", encoding="utf-8")
        (references / "😀.txt").write_text("emoji\n", encoding="utf-8")
        skill_rows.append(
            {
                "name": name,
                "source": "project-snapshot",
                "source_host": None,
                "path": f".agent-flow/skills/{name}/SKILL.md",
                "tree_hash": hash_skill_tree(skill_dir),
                "profiles": ["z-profile", "python", "a-profile"],
            }
        )
    index = {
        "selection": {
            "profile_selection": "explicit",
            "profiles": ["z-profile", "python", "a-profile"],
            "skill_profiles": ["a-profile", "python", "z-profile"],
            "explicit_skills": [
                "private-use-guide",
                "emoji-guide",
                "beta-guide",
                "alpha-guide",
            ],
            "required_review": {
                "z-profile": ["private-use-guide", "emoji-guide"],
                "python": ["beta-guide", "alpha-guide"],
                "a-profile": ["emoji-guide", "private-use-guide"],
            },
            "conditional_skills": {
                "z-last": {"implementation": ["한글-route"]},
                "10": {"implementation": ["ten"]},
                "2": {"implementation": ["two"]},
                "a-first": {"review": ["검토"]},
            },
            "profile_routing": {
                "z-last": {"task_terms": ["파이썬"]},
                "a-first": {"file_rules": [{"extensions": [".py"]}]},
            },
        },
        "skills": list(reversed(skill_rows)),
    }
    index_path = root / ".agent-flow" / "skills" / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    profile_root = root / ".agent-flow" / "profiles"
    profile_root.mkdir()
    for profile in index["selection"]["profiles"]:
        (profile_root / f"{profile}.yaml").write_text(
            f"id: {profile}\nbranching:\n  base: HEAD\n  worktree: required\n"
            "  naming:\n    max_slug_length: 60\ngates: []\n",
            encoding="utf-8",
        )
    digest = compute_skill_plan_hash(index, root, verify_trees=True)
    managed_files = {}
    for relative, (source, content) in MANAGED_HOST_FIXTURES.items():
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        managed_files[relative] = {
            "source": source,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    kit_payload = {
        "profile": "z-profile",
        "primary_profile": "z-profile",
        "profile_selection": "explicit",
        "profiles": index["selection"]["profiles"],
        "skill_plan_hash": digest,
        "skill_plan_hash_version": SKILL_PLAN_HASH_VERSION,
        "managed_host_files": {"version": 1, "files": managed_files},
        "managed_host_files_commitment_version": 1,
    }
    kit_payload["managed_host_files_commitment"] = managed_host_files_commitment(
        kit_payload
    )
    scripts = {}
    for name in MANAGED_HOOK_SCRIPT_NAMES:
        relative = f".agent-flow/scripts/hooks/{name}"
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = f"{name}\n".encode()
        destination.write_bytes(content)
        destination.chmod(0o755)
        scripts[relative] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": "executable",
        }
    hook_settings = _managed_hook_settings(root)
    projection = [
        ["PostToolUse", _write_tool_matcher(), "command", "comment-checker.py"],
        ["PreToolUse", "Bash", "command", "guard-protected-branch.sh"],
        ["PreToolUse", "Bash", "command", "guard-worktree-write.py"],
        ["PreToolUse", "Bash", "command", "guard-worktree.sh"],
        ["PreToolUse", _write_tool_matcher(), "command", "guard-worktree-write.py"],
        ["Stop", "", "command", "show-phase-status.sh"],
    ]
    projection_hash = hashlib.sha256(
        json.dumps(sorted(projection), separators=(",", ":")).encode()
    ).hexdigest()
    configs = {}
    for relative in (
        ".Codex/hooks.json",
        ".codex/hooks.json",
        ".claude/settings.json",
    ):
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(hook_settings), encoding="utf-8")
        configs[relative] = {"sha256": projection_hash}
    kit_payload["managed_hook_contract"] = {
        "version": 2,
        "configs": configs,
        "scripts": scripts,
    }
    kit_payload["managed_hook_contract_commitment_version"] = 2
    kit_payload["managed_hook_contract_commitment"] = managed_hook_contract_commitment(
        kit_payload
    )
    (root / ".agent-flow" / "kit.json").write_text(
        json.dumps(kit_payload),
        encoding="utf-8",
    )
    return index, digest


def _write_tool_matcher() -> str:
    return (
        "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|"
        "write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$"
    )


def _managed_hook_settings(root: Path) -> dict[str, object]:
    def shell_quote(value: object) -> str:
        return "'" + str(value).replace("'", "'\\''") + "'"

    def hook(name: str) -> dict[str, str]:
        script_path = root / ".agent-flow" / "scripts" / "hooks" / name
        digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
        encoded_path = base64.b64encode(script_path.as_posix().encode("utf-8")).decode(
            "ascii"
        )
        command = " ".join(
            (
                shell_quote("/usr/bin/python3"),
                "-I",
                "-c",
                shell_quote(MANAGED_HOOK_VERIFIER),
                shell_quote(encoded_path),
                shell_quote(digest),
            )
        )
        return {"type": "command", "command": command}

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        hook("guard-worktree.sh"),
                        hook("guard-protected-branch.sh"),
                        hook("guard-worktree-write.py"),
                    ],
                },
                {
                    "matcher": _write_tool_matcher(),
                    "hooks": [hook("guard-worktree-write.py")],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": _write_tool_matcher(),
                    "hooks": [hook("comment-checker.py")],
                }
            ],
            "Stop": [{"hooks": [hook("show-phase-status.sh")]}],
        }
    }


def _write_unindexed_conditional_skill(root: Path, name: str = "mutable-policy") -> Path:
    skill_dir = root / ".agent-flow" / "local-skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "activation: conditional\n"
        "workflowPhases: [implement]\n"
        "taskTerms: [never-match]\n"
        "pathGlobs: []\n"
        "---\n\n# Policy\n",
        encoding="utf-8",
    )
    return skill_dir


def test_python_v2_payload_and_hash_match_node_bytes(tmp_path: Path) -> None:
    index, digest = _install_skill_fixture(tmp_path)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for cross-runner byte parity")
    lib_url = (Path(__file__).resolve().parents[1] / "lib" / "skill-selection.mjs").as_uri()
    script = f"""
import fs from "node:fs";
import path from "node:path";
import {{ hashSkillTree }} from {json.dumps(lib_url)};
const root = {json.dumps(str(tmp_path))};
const index = JSON.parse(fs.readFileSync(path.join(root, ".agent-flow", "skills", "index.json"), "utf8"));
const compareCodePoints = (left, right) => {{
  const a = Array.from(String(left), (char) => char.codePointAt(0));
  const b = Array.from(String(right), (char) => char.codePointAt(0));
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {{
    if (a[i] !== b[i]) return a[i] - b[i];
  }}
  return a.length - b.length;
}};
const skills = (index?.skills || []).map((skill) => {{
  const skillPath = path.resolve(root, String(skill.path || ""));
  const liveHash = hashSkillTree(path.dirname(skillPath));
  const relative = path.relative(root, skillPath).split(path.sep).join("/");
  return [skill.name, relative, skill.source, skill.source_host ?? null, liveHash, [...(skill.profiles || [])].sort(compareCodePoints)];
}}).sort((a, b) => compareCodePoints(a[0], b[0]));
const normalized = {{
  profiles: [...(index?.selection?.profiles || [])].sort(compareCodePoints),
  skill_profiles: [...(index?.selection?.skill_profiles || [])].sort(compareCodePoints),
  explicit_skills: [...(index?.selection?.explicit_skills || [])].sort(compareCodePoints),
  ...(Object.hasOwn(index?.selection || {{}}, "profile_selection")
    ? {{ profile_selection: index.selection.profile_selection }}
    : {{}}),
  required_review: Object.fromEntries(Object.entries(index?.selection?.required_review || {{}})
    .sort(([a], [b]) => compareCodePoints(a, b)).map(([profile, names]) => [profile, [...names].sort(compareCodePoints)])),
  conditional_skills: index?.selection?.conditional_skills || {{}},
  profile_routing: index?.selection?.profile_routing || {{}},
  skills,
}};
process.stdout.write(JSON.stringify(normalized));
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        text=False,
        capture_output=True,
        check=True,
    )

    assert canonical_skill_plan_bytes(index, tmp_path, verify_trees=True) == result.stdout
    assert compute_skill_plan_hash(index, tmp_path, verify_trees=True) == digest


def test_v2_hash_pins_the_selected_skill_path(tmp_path: Path) -> None:
    index, original_hash = _install_skill_fixture(tmp_path)
    source = tmp_path / ".agent-flow" / "skills" / "alpha-guide"
    duplicate = tmp_path / ".agent-flow" / "skills" / "alpha-guide-copy"
    shutil.copytree(source, duplicate)
    selected = next(skill for skill in index["skills"] if skill["name"] == "alpha-guide")
    selected["path"] = ".agent-flow/skills/alpha-guide-copy/SKILL.md"

    assert compute_skill_plan_hash(index, tmp_path, verify_trees=True) != original_hash


def test_v2_hash_pins_project_local_activation_metadata(tmp_path: Path) -> None:
    index, original_hash = _install_skill_fixture(tmp_path)
    selected = next(skill for skill in index["skills"] if skill["name"] == "alpha-guide")
    selected.update(
        {
            "activation": "conditional",
            "workflowPhases": ["implement"],
            "taskTerms": ["alpha task"],
            "pathGlobs": [],
        }
    )

    conditional_hash = compute_skill_plan_hash(index, tmp_path, verify_trees=True)
    selected["taskTerms"] = ["different task"]

    assert conditional_hash != original_hash
    assert compute_skill_plan_hash(index, tmp_path, verify_trees=True) != conditional_hash


def test_v2_hash_pins_external_exposure_closure(tmp_path: Path) -> None:
    index, original_hash = _install_skill_fixture(tmp_path)
    index["selection"]["external_exposure_skills"] = [
        "alpha-guide",
        "beta-guide",
    ]

    closure_hash = compute_skill_plan_hash(index, tmp_path, verify_trees=True)
    index["selection"]["external_exposure_skills"] = ["alpha-guide"]

    assert closure_hash != original_hash
    assert compute_skill_plan_hash(index, tmp_path, verify_trees=True) != closure_hash


def test_python_lore_snapshot_hash_matches_node_bytes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for cross-runner lore parity")
    citations = [
        {
            "path": ".agent-flow/memory/lore/인증.md",
            "title": "인증 boundary",
            "type": "decision",
            "weight": 2.0,
            "constraint": "토큰은 private",
            "directive": "경계를 유지",
        },
        {
            "path": ".agent-flow/memory/lore/cache.md",
            "title": "Cache",
            "type": "tradeoff",
            "weight": 1.5,
            "constraint": "bounded",
            "directive": "measure",
        },
    ]
    script = f"""
import crypto from "node:crypto";
const citations = {json.dumps(citations, ensure_ascii=False)};
const normalized = citations.map((entry) => ({{
  constraint: String(entry.constraint),
  directive: String(entry.directive),
  path: String(entry.path),
  title: String(entry.title),
  type: String(entry.type),
  weight: Number(entry.weight),
}}));
process.stdout.write(crypto.createHash("sha256").update(JSON.stringify(normalized)).digest("hex"));
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        text=True,
        capture_output=True,
        check=True,
    )

    assert lore_snapshot_hash(citations) == result.stdout


def test_create_run_pins_v2_and_status_fails_closed_on_tree_change(
    tmp_path: Path,
) -> None:
    _index, digest = _install_skill_fixture(tmp_path)
    run_dir = create_run(
        tmp_path,
        "default",
        "pin skills",
        config_root=tmp_path,
    )
    meta = read_meta(run_dir)
    assert meta["skill_plan_hash"] == digest
    assert meta["skill_plan_hash_version"] == SKILL_PLAN_HASH_VERSION

    (tmp_path / ".agent-flow" / "skills" / "alpha-guide" / "SKILL.md").write_text(
        "changed\n",
        encoding="utf-8",
    )
    active = ActiveRun(
        path=run_dir,
        run_id=meta["run_id"],
        workflow="default",
        task="pin skills",
        started_at=meta["started_at"],
    )
    with pytest.raises(SkillPlanSnapshotError, match="snapshot changed"):
        active.print_status(config_root=tmp_path)


@pytest.mark.parametrize("mutation", ("add", "edit", "delete", "rename"))
def test_active_python_run_pins_unindexed_local_union_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / mutation
    root.mkdir()
    _install_skill_fixture(root)
    mutable = _write_unindexed_conditional_skill(root)
    run_dir = create_run(root, "default", "unrelated task", config_root=root)
    pinned = read_meta(run_dir)
    assert pinned["local_skill_plan_hash"] == project_local_skill_plan_hash(root)
    assert pinned["local_skill_plan_hash_version"] == 2

    if mutation == "add":
        _write_unindexed_conditional_skill(root, "added-policy")
    elif mutation == "edit":
        with (mutable / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("changed\n")
    elif mutation == "delete":
        shutil.rmtree(mutable)
    else:
        mutable.rename(mutable.with_name("renamed-policy"))

    with pytest.raises(SkillPlanSnapshotError, match="project-local skill plan changed"):
        ensure_run_skill_plan_pinned(run_dir, root)


def test_python_local_pin_migrates_only_before_phase_prompt(tmp_path: Path) -> None:
    _index, digest = _install_skill_fixture(tmp_path)
    _write_unindexed_conditional_skill(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "pre-prompt"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "run_id": "pre-prompt",
            "workflow": "default",
            "task": "legacy",
            "current_phase": None,
            "phase_index": 0,
            "skill_plan_hash": digest,
            "skill_plan_hash_version": 2,
        },
    )

    migrated = ensure_run_skill_plan_pinned(run_dir, tmp_path)

    assert migrated["local_skill_plan_hash"] == project_local_skill_plan_hash(tmp_path)
    assert migrated["local_skill_plan_pin_migrated"] is True

    progressed = tmp_path / ".agent-flow" / "runs" / "progressed"
    progressed.mkdir()
    original = {
        "run_id": "progressed",
        "workflow": "default",
        "task": "legacy",
        "current_phase": "design",
        "phase_index": 0,
        "skill_plan_hash": digest,
        "skill_plan_hash_version": 2,
    }
    write_meta(progressed, original)
    with pytest.raises(RuntimeError, match="original skill snapshot"):
        ensure_run_skill_plan_pinned(progressed, tmp_path)
    assert read_meta(progressed) == original


def test_runner_resume_verifies_pin_before_adapter_or_prompt(tmp_path: Path) -> None:
    index, _digest = _install_skill_fixture(tmp_path)
    run_dir = create_run(tmp_path, "default", "resume", config_root=tmp_path)
    index["selection"]["profiles"] = ["python", "react"]
    (tmp_path / ".agent-flow" / "skills" / "index.json").write_text(
        json.dumps(index, ensure_ascii=False),
        encoding="utf-8",
    )
    runner = Runner.__new__(Runner)
    runner.run_dir = run_dir
    runner.config_root = tmp_path

    with pytest.raises(SkillPlanSnapshotError, match="kit.json|profiles do not match"):
        runner.run(ResumeMode.RESUME)


@pytest.mark.parametrize(
    "target_kind",
    ("agent-flow", "skills", "index", "skill-directory", "skill-file"),
)
def test_installed_snapshot_rejects_every_symlinked_ancestor_or_leaf(
    tmp_path: Path,
    target_kind: str,
) -> None:
    root = tmp_path / target_kind
    root.mkdir()
    _install_skill_fixture(root)
    agent_flow = root / ".agent-flow"
    skills = agent_flow / "skills"
    index = skills / "index.json"
    alpha = skills / "alpha-guide"
    alpha_skill = alpha / "SKILL.md"
    if target_kind == "agent-flow":
        outside = root / "outside-agent-flow"
        agent_flow.rename(outside)
        agent_flow.symlink_to(outside, target_is_directory=True)
    elif target_kind == "skills":
        outside = root / "outside-skills"
        skills.rename(outside)
        skills.symlink_to(outside, target_is_directory=True)
    elif target_kind == "index":
        outside = root / "outside-index.json"
        index.rename(outside)
        index.symlink_to(outside)
    elif target_kind == "skill-directory":
        outside = root / "outside-alpha"
        alpha.rename(outside)
        alpha.symlink_to(outside, target_is_directory=True)
    else:
        outside = root / "outside-skill.md"
        alpha_skill.rename(outside)
        alpha_skill.symlink_to(outside)

    with pytest.raises(SkillPlanSnapshotError, match="symlink"):
        installed_skill_plan_pin(root)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_python_skill_tree_hash_rejects_special_files(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: special\n---\n", encoding="utf-8")
    os.mkfifo(skill / "runtime.pipe")

    with pytest.raises(SkillPlanSnapshotError, match="only regular files and directories"):
        hash_skill_tree(skill)


def test_python_pin_rejects_managed_hook_config_and_script_tampering(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    config = tmp_path / ".Codex" / "hooks.json"
    original_config = config.read_bytes()
    settings = json.loads(original_config)
    settings["hooks"]["PreToolUse"][0]["hooks"].pop()
    config.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(SkillPlanSnapshotError, match="managed hook settings changed"):
        installed_skill_plan_pin(tmp_path)
    config.write_bytes(original_config)

    script = tmp_path / ".agent-flow" / "scripts" / "hooks" / "guard-worktree.sh"
    script.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SkillPlanSnapshotError, match="managed hook script changed"):
        installed_skill_plan_pin(tmp_path)


@pytest.mark.parametrize(
    "config_relative",
    (".Codex/hooks.json", ".codex/hooks.json", ".claude/settings.json"),
)
@pytest.mark.parametrize(
    "command_kind", ("raw-path", "forged-verifier", "wrapped-raw-path")
)
def test_python_pin_rejects_noncanonical_managed_hook_commands(
    tmp_path: Path,
    config_relative: str,
    command_kind: str,
) -> None:
    _install_skill_fixture(tmp_path)
    config = tmp_path / config_relative
    settings = json.loads(config.read_text(encoding="utf-8"))
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    script = tmp_path / ".agent-flow" / "scripts" / "hooks" / "guard-worktree.sh"
    if command_kind == "raw-path":
        hook["command"] = f"'{script}'"
    elif command_kind == "wrapped-raw-path":
        hook["command"] = f'/bin/bash "{script}"'
    else:
        encoded_path = base64.b64encode(script.as_posix().encode("utf-8")).decode(
            "ascii"
        )
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        hook["command"] = (
            f"'/usr/bin/python3' -I -c 'pass' '{encoded_path}' '{digest}'"
        )
    config.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(SkillPlanSnapshotError, match="command is not immutable"):
        installed_skill_plan_pin(tmp_path)


def test_python_managed_hook_command_hash_is_bound_to_committed_contract(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    kit = json.loads(
        (tmp_path / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
    )
    expected_hashes = {
        relative: entry["sha256"]
        for relative, entry in kit["managed_hook_contract"]["scripts"].items()
    }
    script = tmp_path / ".agent-flow" / "scripts" / "hooks" / "guard-worktree.sh"
    script.write_text("#!/bin/sh\necho transient-uncommitted-hook\n", encoding="utf-8")
    script.chmod(0o755)
    transient_command = _managed_hook_settings(tmp_path)["hooks"]["PreToolUse"][0][
        "hooks"
    ][0]["command"]

    assert (
        _trusted_managed_hook_script_name(tmp_path, transient_command)
        == "guard-worktree.sh"
    )
    assert (
        _trusted_managed_hook_script_name(
            tmp_path,
            transient_command,
            expected_hashes,
        )
        == ""
    )


@pytest.mark.parametrize(
    "command_kind",
    (
        "raw-path",
        "forged-verifier",
        "wrapped-raw-path",
        "double-quoted-args",
        "variable-path",
    ),
)
def test_python_pin_rejects_additive_noncanonical_managed_hook_commands(
    tmp_path: Path,
    command_kind: str,
) -> None:
    _install_skill_fixture(tmp_path)
    config = tmp_path / ".Codex" / "hooks.json"
    settings = json.loads(config.read_text(encoding="utf-8"))
    script = tmp_path / ".agent-flow" / "scripts" / "hooks" / "guard-worktree.sh"
    if command_kind == "raw-path":
        command = str(script)
    elif command_kind == "wrapped-raw-path":
        command = f'/bin/bash "{script}"'
    elif command_kind == "double-quoted-args":
        canonical = _managed_hook_settings(tmp_path)["hooks"]["PreToolUse"][0][
            "hooks"
        ][0]["command"]
        suffix = re.search(
            r" '([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$", canonical
        )
        assert suffix is not None
        command = (
            canonical[: suffix.start()]
            + f' "{suffix.group(1)}" "{suffix.group(2)}"'
        )
    elif command_kind == "variable-path":
        canonical = _managed_hook_settings(tmp_path)["hooks"]["PreToolUse"][0][
            "hooks"
        ][0]["command"]
        suffix = re.search(
            r" '([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$", canonical
        )
        assert suffix is not None
        command = (
            f"P='{suffix.group(1)}'; "
            + canonical[: suffix.start()]
            + f' "$P" \'{suffix.group(2)}\''
        )
    else:
        encoded_path = base64.b64encode(script.as_posix().encode("utf-8")).decode(
            "ascii"
        )
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        command = f"'/usr/bin/python3' -I -c 'pass' '{encoded_path}' '{digest}'"
    settings["hooks"]["PreToolUse"][0]["hooks"].append(
        {"type": "command", "command": command}
    )
    config.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(SkillPlanSnapshotError, match="command is not immutable"):
        installed_skill_plan_pin(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX execute bits")
def test_python_pin_rejects_non_executable_managed_hook_script(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    script = tmp_path / ".agent-flow" / "scripts" / "hooks" / "guard-worktree.sh"
    script.chmod(0o644)

    with pytest.raises(
        SkillPlanSnapshotError,
        match="managed hook script is not executable",
    ):
        installed_skill_plan_pin(tmp_path)


@pytest.mark.parametrize("target_name", ("kit.json", "skills/index.json"))
@pytest.mark.parametrize("payload", ("{invalid", "[]\n"))
def test_python_installed_metadata_is_strict_json_object(
    tmp_path: Path,
    target_name: str,
    payload: str,
) -> None:
    root = tmp_path / target_name.replace("/", "-") / str(len(payload))
    root.mkdir(parents=True)
    _install_skill_fixture(root)
    (root / ".agent-flow" / target_name).write_text(payload, encoding="utf-8")

    with pytest.raises(SkillPlanSnapshotError, match="installed (kit metadata|skill index)"):
        installed_skill_plan_pin(root)


def test_node_install_managed_host_commitment_is_enforced_by_python(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    root = tmp_path / "project"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    install = subprocess.run(
        (
            node,
            str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"),
            "install",
            "--profile",
            "generic",
        ),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "HOME": str(home),
            "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        },
    )
    assert install.returncode == 0, install.stderr
    assert installed_skill_plan_pin(root)["skill_plan_hash_version"] == 2

    relative = ".Codex/agents/code-reviewer.md"
    destination = root.joinpath(*relative.split("/"))
    user_bytes = b"user-owned reviewer\n"
    destination.write_bytes(user_bytes)
    with pytest.raises(SkillPlanSnapshotError, match="managed host file changed"):
        installed_skill_plan_pin(root)

    kit_path = root / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit["managed_host_files"]["files"][relative]["sha256"] = hashlib.sha256(
        user_bytes
    ).hexdigest()
    kit_path.write_text(json.dumps(kit, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(
        SkillPlanSnapshotError,
        match="managed host file commitment does not match provenance",
    ):
        installed_skill_plan_pin(root)


def test_python_installed_metadata_rejects_missing_index_and_profile_mutation(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    _install_skill_fixture(missing_root)
    (missing_root / ".agent-flow" / "skills" / "index.json").unlink()
    with pytest.raises(SkillPlanSnapshotError, match="index is missing"):
        installed_skill_plan_pin(missing_root)

    missing_kit_root = tmp_path / "missing-kit"
    missing_kit_root.mkdir()
    _install_skill_fixture(missing_kit_root)
    (missing_kit_root / ".agent-flow" / "kit.json").unlink()
    with pytest.raises(SkillPlanSnapshotError, match="kit metadata is missing"):
        installed_skill_plan_pin(missing_kit_root)

    changed_root = tmp_path / "profile-changed"
    changed_root.mkdir()
    _install_skill_fixture(changed_root)
    kit_path = changed_root / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit["profiles"] = ["python"]
    kit_path.write_text(json.dumps(kit), encoding="utf-8")
    with pytest.raises(SkillPlanSnapshotError, match="profiles do not match"):
        installed_skill_plan_pin(changed_root)


def test_profile_prompt_does_not_silently_ignore_corrupt_installed_index(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    (tmp_path / ".agent-flow" / "skills" / "index.json").write_text(
        "{invalid",
        encoding="utf-8",
    )

    with pytest.raises(SkillPlanSnapshotError, match="installed skill index"):
        from agent_flow.core.skill_plan import profile_skill_prompt_block

        profile_skill_prompt_block(tmp_path, "implement", tmp_path)


def test_compatibility_start_run_pins_skill_and_lore_snapshots(tmp_path: Path) -> None:
    _index, digest = _install_skill_fixture(tmp_path)
    state = start_run(
        root=tmp_path,
        request=RunRequest(
            workflow_id="review",
            task="snapshot compatibility run",
            adapter="manual",
            profile="python",
            run_id="r1",
            config_root=tmp_path,
        ),
    )
    manifest = json.loads((state.run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["skill_plan_hash"] == digest
    assert manifest["skill_plan_hash_version"] == SKILL_PLAN_HASH_VERSION
    assert manifest["lore_snapshot_version"] == 1
    assert isinstance(manifest["lore_snapshot_hash"], str)
    assert manifest["lore_citations"] == []


def test_compatibility_status_fails_closed_after_skill_change(tmp_path: Path) -> None:
    _install_skill_fixture(tmp_path)
    state = start_run(
        root=tmp_path,
        request=RunRequest(
            workflow_id="review",
            task="snapshot compatibility run",
            adapter="manual",
            profile="python",
            run_id="r1",
            config_root=tmp_path,
        ),
    )
    (state.run_dir / "prompts").mkdir()
    (state.run_dir / "prompts" / "review.md").write_text("prompt\n", encoding="utf-8")
    (tmp_path / ".agent-flow" / "skills" / "beta-guide" / "SKILL.md").write_text(
        "changed\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillPlanSnapshotError, match="snapshot changed"):
        status_summary(tmp_path)
    with pytest.raises(SkillPlanSnapshotError, match="snapshot changed"):
        verify_run_state_snapshots(
            state_root=tmp_path,
            run_dir=state.run_dir,
            config_root=tmp_path,
        )


def test_compatibility_prompt_write_revalidates_snapshot(tmp_path: Path) -> None:
    _install_skill_fixture(tmp_path)
    state = start_run(
        root=tmp_path,
        request=RunRequest(
            workflow_id="review",
            task="snapshot compatibility run",
            adapter="manual",
            profile="python",
            run_id="r1",
            config_root=tmp_path,
        ),
    )
    (tmp_path / ".agent-flow" / "skills" / "beta-guide" / "SKILL.md").write_text(
        "changed\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillPlanSnapshotError, match="snapshot changed"):
        _write_stage_prompts(
            root=tmp_path,
            config_root=tmp_path,
            project_root=tmp_path,
            state=state,
            workflow=SimpleNamespace(stages=[]),
        )
    assert not (state.run_dir / "prompts").exists()


def test_compatibility_private_state_root_resolves_leader_snapshot(
    tmp_path: Path,
) -> None:
    leader = tmp_path / "project"
    leader.mkdir()
    _index, digest = _install_skill_fixture(leader)
    state_root = leader / ".git" / "agent-flow" / "worktrees" / "feat-demo"
    state_root.mkdir(parents=True)

    state = start_run(
        root=state_root,
        request=RunRequest(
            workflow_id="review",
            task="private runtime",
            adapter="manual",
            profile="python",
            run_id="r1",
        ),
    )
    manifest = json.loads((state.run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["skill_plan_hash"] == digest
    assert manifest["skill_plan_hash_version"] == SKILL_PLAN_HASH_VERSION


def test_legacy_meta_migrates_once_after_validating_kit(tmp_path: Path) -> None:
    _index, digest = _install_skill_fixture(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "legacy"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "run_id": "legacy",
            "workflow": "default",
            "task": "legacy",
            "current_phase": None,
            "phase_index": 0,
        },
    )

    migrated = ensure_run_skill_plan_pinned(run_dir, tmp_path)

    assert migrated["skill_plan_hash"] == digest
    assert migrated["skill_plan_hash_version"] == SKILL_PLAN_HASH_VERSION
    assert migrated["skill_plan_pin_migrated"] is True
    assert ensure_run_skill_plan_pinned(run_dir, tmp_path) == migrated


def test_legacy_hash_is_replaced_only_from_current_whole_tree_before_prompt(
    tmp_path: Path,
) -> None:
    _index, digest = _install_skill_fixture(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "legacy"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "run_id": "legacy",
            "workflow": "default",
            "task": "legacy",
            "skill_plan_hash": "old-run-hash",
            "skill_plan_hash_version": 1,
        },
    )

    migrated = ensure_run_skill_plan_pinned(run_dir, tmp_path)

    assert migrated["skill_plan_hash"] == digest
    assert migrated["skill_plan_hash_version"] == SKILL_PLAN_HASH_VERSION
    assert migrated["skill_plan_hash_migrated_from_version"] == 1


def test_pinned_legacy_run_after_prompt_does_not_trust_mutable_kit(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "legacy-active"
    run_dir.mkdir(parents=True)
    original = {
        "run_id": "legacy-active",
        "workflow": "default",
        "task": "legacy",
        "current_phase": "design",
        "phase_index": 0,
        "skill_plan_hash": "0" * 64,
        "skill_plan_hash_version": 1,
    }
    write_meta(run_dir, original)
    kit_path = tmp_path / ".agent-flow" / "kit.json"
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    kit["skill_plan_hash"] = "0" * 64
    kit["skill_plan_hash_version"] = 1
    kit["managed_host_files_commitment"] = managed_host_files_commitment(kit)
    kit["managed_hook_contract_commitment"] = managed_hook_contract_commitment(kit)
    kit_path.write_text(json.dumps(kit), encoding="utf-8")

    with pytest.raises(RuntimeError, match="original skill snapshot"):
        ensure_run_skill_plan_pinned(run_dir, tmp_path)
    assert read_meta(run_dir) == original


def test_unpinned_legacy_run_after_prompt_is_not_blindly_migrated(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "legacy-active"
    run_dir.mkdir(parents=True)
    original = {
        "run_id": "legacy-active",
        "workflow": "default",
        "task": "legacy",
        "current_phase": "design",
        "phase_index": 0,
    }
    write_meta(run_dir, original)

    with pytest.raises(RuntimeError, match="original skill snapshot"):
        ensure_run_skill_plan_pinned(run_dir, tmp_path)
    assert read_meta(run_dir) == original


def test_compatibility_pinned_legacy_run_after_prompt_is_not_migrated(
    tmp_path: Path,
) -> None:
    _install_skill_fixture(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "review" / "legacy-active"
    prompts = run_dir / "prompts"
    prompts.mkdir(parents=True)
    manifest = {
        "run_id": "legacy-active",
        "workflow_id": "review",
        "skill_plan_hash": "old-run-hash",
        "skill_plan_hash_version": 1,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (prompts / "review.md").write_text("prompt\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="original skill snapshot"):
        verify_run_state_snapshots(
            state_root=tmp_path,
            run_dir=run_dir,
            config_root=tmp_path,
        )
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_compatibility_main_v2_without_local_pin_after_prompt_is_not_migrated(
    tmp_path: Path,
) -> None:
    _index, digest = _install_skill_fixture(tmp_path)
    _write_unindexed_conditional_skill(tmp_path)
    run_dir = tmp_path / ".agent-flow" / "runs" / "review" / "missing-local-pin"
    prompts = run_dir / "prompts"
    prompts.mkdir(parents=True)
    manifest = {
        "run_id": "missing-local-pin",
        "workflow_id": "review",
        "skill_plan_hash": digest,
        "skill_plan_hash_version": 2,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (prompts / "review.md").write_text("prompt\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="original skill snapshot"):
        verify_run_state_snapshots(
            state_root=tmp_path,
            run_dir=run_dir,
            config_root=tmp_path,
        )
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_lore_citations_are_pinned_and_ignore_live_changes(tmp_path: Path) -> None:
    lore_dir = tmp_path / ".agent-flow" / "memory" / "lore"
    lore_dir.mkdir(parents=True)
    lore_file = lore_dir / "authentication.md"
    lore_file.write_text(
        "---\n"
        "title: Authentication boundary\n"
        "type: decision\n"
        "date: 2026-07-11\n"
        "scope: auth\n"
        "weight: 2.0\n"
        "tags: [authentication]\n"
        "---\n"
        "## Constraint\nKeep tokens private.\n"
        "## Rejected\n- global state\n"
        "## Directive\nUse the original boundary.\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / ".agent-flow" / "runs" / "lore-run"
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": "lore-run",
        "workflow": "default",
        "task": "authentication change",
    }
    meta.update(_lore_snapshot_metadata(tmp_path, meta["task"]))
    write_meta(run_dir, meta)

    lore_file.write_text(lore_file.read_text().replace("original", "mutated"), encoding="utf-8")
    first_meta, first = _ensure_lore_snapshot(run_dir, tmp_path, tmp_path)
    second_meta, second = _ensure_lore_snapshot(run_dir, tmp_path, tmp_path)

    assert first[0].directive == "Use the original boundary."
    assert second[0].directive == first[0].directive
    assert second_meta == first_meta


def test_legacy_lore_snapshot_migrates_once(tmp_path: Path) -> None:
    lore_dir = tmp_path / ".agent-flow" / "memory" / "lore"
    lore_dir.mkdir(parents=True)
    lore_file = lore_dir / "python.md"
    lore_file.write_text(
        "---\ntitle: Python rule\ntype: decision\ndate: 2026-07-11\n"
        "scope: python\nweight: 1\ntags: [python]\n---\n"
        "## Constraint\nStable output.\n## Rejected\n- mutation\n"
        "## Directive\nKeep version one.\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / ".agent-flow" / "runs" / "legacy-lore"
    run_dir.mkdir(parents=True)
    write_meta(
        run_dir,
        {
            "run_id": "legacy-lore",
            "workflow": "default",
            "task": "python work",
            "current_phase": None,
            "phase_index": 0,
        },
    )

    migrated, first = _ensure_lore_snapshot(run_dir, tmp_path, tmp_path)
    lore_file.write_text(lore_file.read_text().replace("version one", "version two"), encoding="utf-8")
    persisted, second = _ensure_lore_snapshot(run_dir, tmp_path, tmp_path)

    assert migrated["lore_snapshot_migrated"] is True
    assert first[0].directive == "Keep version one."
    assert second[0].directive == first[0].directive
    assert persisted == migrated


def test_legacy_lore_after_prompt_is_not_reconstructed_from_live_files(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / ".agent-flow" / "runs" / "legacy-lore-active"
    run_dir.mkdir(parents=True)
    original = {
        "run_id": "legacy-lore-active",
        "workflow": "default",
        "task": "python work",
        "current_phase": "design",
        "phase_index": 0,
    }
    write_meta(run_dir, original)

    with pytest.raises(RuntimeError, match="original lore citations"):
        _ensure_lore_snapshot(run_dir, tmp_path, tmp_path)
    assert read_meta(run_dir) == original
