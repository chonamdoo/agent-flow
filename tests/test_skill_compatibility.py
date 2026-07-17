from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_flow.core import skill_plan as skill_plan_module
from agent_flow.core.skill_compatibility import SkillResolutionError
from agent_flow.core.skill_plan import (
    SkillDocumentResolutionError,
    SkillPlanSnapshotError,
    compute_skill_plan_hash,
    resolve_runtime_skill_plan,
)


def _compat_index() -> dict[str, object]:
    return {
        "version": 2,
        "revision": "f" * 64,
        "selection": {"profiles": [], "explicit_skills": [], "profile_routing": {"profiles": {}, "escalations": {}}},
        "compatibility": {
            "skills": [
                {
                    "canonical": "clean-architecture-core",
                    "capabilities": ["architecture.clean.boundary"],
                    "aliases": ["clean-architecture-boundaries"],
                    "renamed_from": ["clean-architecture-v1"],
                },
                {
                    "canonical": "legacy-clean-architecture",
                    "status": "deprecated",
                    "replaced_by": "clean-architecture-core",
                    "aliases": ["deprecated-clean-architecture"],
                },
            ]
        },
        "skills": [
            {
                "name": "clean-architecture-core",
                "path": "skills/clean-architecture-core/SKILL.md",
                "tree_hash": "a" * 64,
            }
        ],
    }

def _node_skill_document(root: Path) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    script = """
import { readSkillDocument } from './lib/skill-selection.mjs';
try {
  readSkillDocument(process.argv[1], 'example-skill');
  process.stdout.write(JSON.stringify({ ok: true }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    code: error.code,
    diagnostics: error.diagnostics,
    message: error.message,
  }));
}
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script, str(root)),
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)



def test_skill_compatibility_alias_rename_and_deprecated_replacement_resolve_to_canonical() -> None:
    plan = resolve_runtime_skill_plan(
        _compat_index(),
        phase_id="ddd-design",
        required_skills=[
            "clean-architecture-boundaries",
            "clean-architecture-v1",
            "deprecated-clean-architecture",
        ],
    )

    assert plan["missing"] == []
    assert [skill["name"] for skill in plan["skills"]] == ["clean-architecture-core"]
    assert plan["skills"][0]["capabilities"] == ["architecture.clean.boundary"]


def test_existing_concrete_id_resolves_unchanged_with_or_without_metadata() -> None:
    with_metadata = _compat_index()
    without_metadata = json.loads(json.dumps(with_metadata))
    without_metadata.pop("compatibility")

    for index in (with_metadata, without_metadata):
        plan = resolve_runtime_skill_plan(
            index,
            phase_id="ddd-design",
            required_skills=["clean-architecture-core"],
        )
        assert plan["missing"] == []
        assert [skill["name"] for skill in plan["skills"]] == [
            "clean-architecture-core"
        ]


def test_skill_compatibility_removed_without_replacement_reports_structured_missing() -> None:
    index = _compat_index()
    index["compatibility"] = {
        "skills": [
            {
                "canonical": "removed-skill",
                "status": "removed",
                "capabilities": ["obsolete.capability"],
            }
        ]
    }

    plan = resolve_runtime_skill_plan(
        index,
        phase_id="ddd-design",
        required_skills=["removed-skill"],
    )

    assert plan["missing"] == ["removed-skill"]
    assert plan["resolution_errors"] == [
        {
            "reason": "removed_without_replacement",
            "requested": "removed-skill",
            "canonical": "removed-skill",
            "capabilities": ["obsolete.capability"],
            "repairable": False,
        }
    ]


def test_skill_compatibility_rejects_duplicate_alias_and_replacement_cycle(tmp_path: Path) -> None:
    base = _compat_index()
    base["compatibility"] = {
        "skills": [
            {"canonical": "one", "aliases": ["shared-alias"]},
            {"canonical": "two", "renamed_from": ["shared-alias"]},
        ]
    }
    with pytest.raises(SkillPlanSnapshotError, match="duplicate compatibility reference"):
        compute_skill_plan_hash(base, tmp_path)

    cyclic = _compat_index()
    cyclic["compatibility"] = {
        "skills": [
            {"canonical": "one", "status": "deprecated", "replaced_by": "two"},
            {"canonical": "two", "status": "deprecated", "replaced_by": "one"},
        ]
    }
    with pytest.raises(SkillPlanSnapshotError, match="replacement cycle"):
        compute_skill_plan_hash(cyclic, tmp_path)

    shadow = _compat_index()
    shadow["compatibility"] = {
        "skills": [
            {
                "canonical": "replacement-skill",
                "aliases": ["clean-architecture-core"],
            }
        ]
    }
    with pytest.raises(SkillPlanSnapshotError, match="shadows concrete skill"):
        compute_skill_plan_hash(shadow, tmp_path)


def test_skill_plan_hash_changes_when_compatibility_projection_changes(tmp_path: Path) -> None:
    root = tmp_path
    skill_dir = root / "skills" / "clean-architecture-core"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: clean-architecture-core\n---\n", encoding="utf-8")
    index = _compat_index()
    first = compute_skill_plan_hash(index, root)
    changed = json.loads(json.dumps(index))
    changed["compatibility"]["skills"][0]["capabilities"].append("architecture.clean.mapper")

    assert compute_skill_plan_hash(changed, root) != first


@pytest.mark.parametrize("replacement_kind", ["symlink", "fifo"])
def test_node_skill_tree_hash_rejects_entry_replacement(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("secure descriptor flags are required")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: example-skill\n---\n", encoding="utf-8")
    payload = root / "payload.txt"
    payload.write_text("safe", encoding="utf-8")
    replacement = tmp_path / "replacement"
    if replacement_kind == "symlink":
        replacement.write_text("outside", encoding="utf-8")
    else:
        os.mkfifo(replacement)
    script = """
import fs from 'node:fs';
import { hashSkillTree } from './lib/skill-selection.mjs';
const [root, target, replacement, replacementKind] = process.argv.slice(1);
const originalOpenSync = fs.openSync;
let secureFlags = false;
let swapped = false;
fs.openSync = (file, flags, ...args) => {
  const descriptor = originalOpenSync(file, flags, ...args);
  if (!swapped && String(file) === target) {
    secureFlags = (
      (flags & fs.constants.O_NOFOLLOW) === fs.constants.O_NOFOLLOW
      && (flags & fs.constants.O_NONBLOCK) === fs.constants.O_NONBLOCK
    );
    if (secureFlags) {
      fs.renameSync(target, `${target}.original`);
      if (replacementKind === 'symlink') {
        fs.symlinkSync(replacement, target);
      } else {
        fs.renameSync(replacement, target);
      }
      swapped = true;
    }
  }
  return descriptor;
};
try {
  hashSkillTree(root);
  process.stdout.write(JSON.stringify({ ok: true, secureFlags, swapped }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    secureFlags,
    swapped,
    message: error instanceof Error ? error.message : String(error),
  }));
}
"""
    result = subprocess.run(
        (
            node,
            "--input-type=module",
            "-e",
            script,
            str(root),
            str(payload),
            str(replacement),
            replacement_kind,
        ),
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["secureFlags"] is True
    assert outcome["swapped"] is True
    assert outcome["ok"] is False
    assert "skill source" in outcome["message"]
    assert "changed" in outcome["message"]


@pytest.mark.parametrize("replacement_kind", ["symlink", "fifo"])
def test_python_skill_tree_hash_rejects_entry_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("secure descriptor flags are required")
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: example-skill\n---\n", encoding="utf-8")
    payload = root / "payload.txt"
    payload.write_text("safe", encoding="utf-8")
    replacement = tmp_path / "replacement"
    if replacement_kind == "symlink":
        replacement.write_text("outside", encoding="utf-8")
    else:
        os.mkfifo(replacement)
    original_open = skill_plan_module.os.open
    observed = {"secure_flags": False, "swapped": False}

    def swapping_open(file: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(file, flags, *args, **kwargs)
        if not observed["swapped"] and Path(file) == payload:
            observed["secure_flags"] = (
                flags & os.O_NOFOLLOW == os.O_NOFOLLOW
                and flags & os.O_NONBLOCK == os.O_NONBLOCK
            )
            if observed["secure_flags"]:
                payload.rename(payload.with_suffix(".original"))
                if replacement_kind == "symlink":
                    payload.symlink_to(replacement)
                else:
                    replacement.rename(payload)
                observed["swapped"] = True
        return descriptor

    monkeypatch.setattr(skill_plan_module.os, "open", swapping_open)

    with pytest.raises(
        skill_plan_module.SkillPlanSnapshotError,
        match="skill source (?:directory )?changed",
    ):
        skill_plan_module.hash_skill_tree(root)
    assert observed == {"secure_flags": True, "swapped": True}


def test_node_skill_document_rejects_same_content_replacement(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("secure descriptor flags are required")
    root = tmp_path / "skill"
    root.mkdir()
    document = root / "SKILL.md"
    content = "---\nname: example-skill\n---\n"
    document.write_text(content, encoding="utf-8")
    script = """
import fs from 'node:fs';
import { readSkillDocument } from './lib/skill-selection.mjs';
const [root, target, content] = process.argv.slice(1);
const originalOpenSync = fs.openSync;
let secureFlags = false;
let swapped = false;
fs.openSync = (file, flags, ...args) => {
  const descriptor = originalOpenSync(file, flags, ...args);
  if (!swapped && String(file) === target) {
    secureFlags = (
      (flags & fs.constants.O_NOFOLLOW) === fs.constants.O_NOFOLLOW
      && (flags & fs.constants.O_NONBLOCK) === fs.constants.O_NONBLOCK
    );
    if (secureFlags) {
      swapped = true;
      fs.renameSync(target, `${target}.original`);
      fs.writeFileSync(target, content);
    }
  }
  return descriptor;
};
try {
  readSkillDocument(root, 'example-skill');
  process.stdout.write(JSON.stringify({ ok: true, secureFlags, swapped }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    secureFlags,
    swapped,
    code: error?.code,
    diagnostics: error?.diagnostics,
  }));
}
"""
    result = subprocess.run(
        (
            node,
            "--input-type=module",
            "-e",
            script,
            str(root),
            str(document),
            content,
        ),
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome == {
        "ok": False,
        "secureFlags": True,
        "swapped": True,
        "code": "skill_resolution_error",
        "diagnostics": [
            {
                "reason": "skill_document_unavailable",
                "requested": "example-skill",
                "canonical": "example-skill",
                "capabilities": [],
                "state": "changed",
                "path": str(document),
                "repairable": False,
            }
        ],
    }


@pytest.mark.parametrize("replacement_level", ["root", "ancestor"])
def test_node_skill_tree_hash_rejects_directory_replacement(
    tmp_path: Path,
    replacement_level: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    root = tmp_path / "skill"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n",
        encoding="utf-8",
    )
    (nested / "payload.txt").write_text("safe", encoding="utf-8")
    target = root if replacement_level == "root" else nested
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    script = """
import fs from 'node:fs';
import { hashSkillTree } from './lib/skill-selection.mjs';
const [root, target, replacement] = process.argv.slice(1);
const originalReaddirSync = fs.readdirSync;
let swapped = false;
fs.readdirSync = (directory, options) => {
  const entries = originalReaddirSync(directory, options);
  if (!swapped && String(directory) === target) {
    swapped = true;
    fs.renameSync(target, `${target}.original`);
    fs.symlinkSync(replacement, target, 'dir');
  }
  return entries;
};
try {
  hashSkillTree(root);
  process.stdout.write(JSON.stringify({ ok: true, swapped }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    swapped,
    message: error instanceof Error ? error.message : String(error),
  }));
}
"""
    result = subprocess.run(
        (
            node,
            "--input-type=module",
            "-e",
            script,
            str(root),
            str(target),
            str(replacement),
        ),
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["swapped"] is True
    assert outcome["ok"] is False
    assert "skill source directory changed while hashing" in outcome["message"]


@pytest.mark.parametrize("replacement_kind", ["symlink", "fifo"])
def test_python_snapshot_json_rejects_entry_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("secure descriptor flags are required")
    root = tmp_path / "project"
    target = root / ".agent-flow" / "skills" / "index.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"version": 2}\n', encoding="utf-8")
    replacement = tmp_path / "replacement"
    if replacement_kind == "symlink":
        replacement.write_text('{"version": 999}\n', encoding="utf-8")
    else:
        os.mkfifo(replacement)
    original_open = os.open
    swapped = False

    def swapping_open(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(file, flags, mode, dir_fd=dir_fd)
        if not swapped and Path(file) == target:
            target.rename(target.with_suffix(".original"))
            if replacement_kind == "symlink":
                target.symlink_to(replacement)
            else:
                replacement.rename(target)
            swapped = True
        return descriptor

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(SkillPlanSnapshotError, match="changed while reading"):
        skill_plan_module._read_snapshot_json(root, target, "installed skill index")
    assert swapped is True


def test_python_snapshot_json_rejects_authority_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    authority = root / ".agent-flow" / "skills"
    target = authority / "index.json"
    authority.mkdir(parents=True)
    target.write_text('{"version": 2}\n', encoding="utf-8")
    original_open = os.open
    swapped = False

    def swapping_open(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(file, flags, mode, dir_fd=dir_fd)
        if not swapped and Path(file) == target:
            authority.rename(authority.with_name("skills.original"))
            authority.mkdir()
            target.write_text('{"version": 999}\n', encoding="utf-8")
            swapped = True
        return descriptor

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(SkillPlanSnapshotError, match="authority changed while reading"):
        skill_plan_module._read_snapshot_json(root, target, "installed skill index")
    assert swapped is True


def test_python_skill_tree_hash_rejects_hardlinked_document(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: example-skill\n---\n", encoding="utf-8")
    os.link(outside, root / "SKILL.md")

    with pytest.raises(
        SkillPlanSnapshotError,
        match="skill source changed or is unreadable while hashing",
    ):
        skill_plan_module.hash_skill_tree(root)


def test_node_and_python_reject_hardlinked_auxiliary_file_identically(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, root / "payload.txt")

    with pytest.raises(
        SkillPlanSnapshotError,
        match="skill source changed or is unreadable while hashing",
    ):
        skill_plan_module.hash_skill_tree(root)

    script = """
import { hashSkillTree } from './lib/skill-selection.mjs';
try {
  hashSkillTree(process.argv[1]);
  process.stdout.write(JSON.stringify({ ok: true }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    message: error instanceof Error ? error.message : String(error),
  }));
}
"""
    result = subprocess.run(
        (node, "--input-type=module", "-e", script, str(root)),
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["ok"] is False
    assert "skill source changed or is unreadable while hashing" in outcome["message"]



@pytest.mark.parametrize("replacement_level", ["root", "ancestor"])
def test_python_skill_tree_hash_rejects_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_level: str,
) -> None:
    root = tmp_path / "skill"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n",
        encoding="utf-8",
    )
    (nested / "payload.txt").write_text("safe", encoding="utf-8")
    target = root if replacement_level == "root" else nested
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    original_iterdir = Path.iterdir
    swapped = False

    def swapping_iterdir(directory: Path) -> object:
        nonlocal swapped
        entries = list(original_iterdir(directory))
        if not swapped and directory == target:
            swapped = True
            target.rename(target.with_suffix(".original"))
            target.symlink_to(replacement, target_is_directory=True)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", swapping_iterdir)

    with pytest.raises(
        skill_plan_module.SkillPlanSnapshotError,
        match="skill source directory changed while hashing",
    ):
        skill_plan_module.hash_skill_tree(root)
    assert swapped is True


@pytest.mark.parametrize("replacement_level", ["root", "ancestor"])
def test_node_skill_tree_hash_rejects_authority_replacement(
    tmp_path: Path,
    replacement_level: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    authority = tmp_path / "project"
    base = authority / "skills"
    root = base / "example-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n",
        encoding="utf-8",
    )
    script = """
import fs from 'node:fs';
import { hashSkillTree } from './lib/skill-selection.mjs';
const [authority, target, root] = process.argv.slice(1);
const originalReaddirSync = fs.readdirSync;
let swapped = false;
fs.readdirSync = (directory, options) => {
  const entries = originalReaddirSync(directory, options);
  if (!swapped && String(directory) === root) {
    swapped = true;
    fs.renameSync(target, `${target}.original`);
    fs.symlinkSync(`${target}.original`, target, 'dir');
  }
  return entries;
};
try {
  hashSkillTree(root, { authorityRoot: authority });
  process.stdout.write(JSON.stringify({ ok: true, swapped }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    swapped,
    message: error instanceof Error ? error.message : String(error),
  }));
}
"""
    result = subprocess.run(
        (
            node,
            "--input-type=module",
            "-e",
            script,
            str(authority),
            str(authority if replacement_level == "root" else base),
            str(root),
        ),
        cwd=Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["swapped"] is True
    assert outcome["ok"] is False
    assert "skill source directory changed while hashing" in outcome["message"]


@pytest.mark.parametrize("replacement_level", ["root", "ancestor"])
def test_python_skill_tree_hash_rejects_authority_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_level: str,
) -> None:
    authority = tmp_path / "project"
    base = authority / "skills"
    root = base / "example-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\n---\n",
        encoding="utf-8",
    )
    target = authority if replacement_level == "root" else base
    original_iterdir = Path.iterdir
    swapped = False

    def swapping_iterdir(directory: Path) -> object:
        nonlocal swapped
        entries = list(original_iterdir(directory))
        if not swapped and directory == root:
            swapped = True
            original = target.with_suffix(".original")
            target.rename(original)
            target.symlink_to(original, target_is_directory=True)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", swapping_iterdir)

    with pytest.raises(
        skill_plan_module.SkillPlanSnapshotError,
        match="skill source directory changed while hashing",
    ):
        skill_plan_module.hash_skill_tree(root, authority_root=authority)
    assert swapped is True


def test_missing_skill_document_reports_structured_snapshot_error(tmp_path: Path) -> None:
    with pytest.raises(SkillDocumentResolutionError) as captured:
        compute_skill_plan_hash(_compat_index(), tmp_path)
    assert isinstance(captured.value, SkillResolutionError)

    assert captured.value.diagnostics[0]["state"] == "missing"
    assert captured.value.diagnostics[0]["reason"] == "skill_document_unavailable"
    assert "skill_resolution_error" in str(captured.value)
    assert "FileNotFoundError" not in str(captured.value)


@pytest.mark.parametrize(
    ("state", "setup"),
    [
        ("missing", "missing"),
        ("symlink", "symlink"),
        ("dangling_symlink", "dangling"),
        ("non_regular", "directory"),
    ],
)
def test_node_skill_document_failures_are_structured(
    tmp_path: Path,
    state: str,
    setup: str,
) -> None:
    skill_root = tmp_path / setup
    skill_root.mkdir()
    skill_document = skill_root / "SKILL.md"
    if setup == "symlink":
        target = tmp_path / "target.md"
        target.write_text("---\nname: example-skill\n---\n", encoding="utf-8")
        skill_document.symlink_to(target)
    elif setup == "dangling":
        skill_document.symlink_to(tmp_path / "missing-target.md")
    elif setup == "directory":
        skill_document.mkdir()

    result = _node_skill_document(skill_root)

    assert result["ok"] is False
    assert result["code"] == "skill_resolution_error"
    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, list)
    assert diagnostics[0]["state"] == state
    assert diagnostics[0]["reason"] == "skill_document_unavailable"
    assert "ENOENT" not in str(result["message"])
