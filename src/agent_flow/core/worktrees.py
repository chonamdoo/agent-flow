from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_flow.core.commands import run_safe_command


PROTECTED_WORKTREE_BRANCHES = {"main", "master", "develop"}
GIT_WORKTREE_TIMEOUT_S = 300
ARTIFACT_EXTENSION_SOURCE = (
    r"fig|figma|png|jpe?g|gif|webp|svg|pdf|sketch|avif|bmp|ico|tiff?|heic|heif|psd"
)
MARKDOWN_REFERENCE_PATTERN = re.compile(
    r"(!?)\[([^\]\r\n]*)\]\(((?:[^()\r\n]|\([^()\r\n]*\))*)\)"
)
URL_PATTERN = re.compile(
    r"(?:https?://|file://|www\.)[^\s<]+|figma\.com/[^\s<]+",
    flags=re.IGNORECASE,
)
QUOTED_ARTIFACT_PATTERN = re.compile(
    rf"([\"'`“‘])[^\r\n]*?\.(?:{ARTIFACT_EXTENSION_SOURCE})"
    rf"(?:[?#][^\s\"'`”’]*)?[\"'`”’]",
    flags=re.IGNORECASE,
)
PATH_ARTIFACT_PATTERN = re.compile(
    r"(^|[\s(\[\{,:;])"
    r"(?:[A-Za-z]:[\\/]|(?:~|\.\.?)[\\/]|[\\/]"
    r"|(?:[^\s/\\()<>\[\]{}:;,]+[\\/])+)"
    rf"[^\r\n]*?\.(?:{ARTIFACT_EXTENSION_SOURCE})"
    r"(?:[?#][^\s:;,)\]\}>]*)?(?=$|[\s:;,)\]\}>])",
    flags=re.IGNORECASE,
)
BARE_ARTIFACT_PATTERN = re.compile(
    r"(^|[\s(\[\{,:;])[^\s/\\()<>\[\]{}]+?\."
    rf"(?:{ARTIFACT_EXTENSION_SOURCE})(?:[?#][^\s:;,)\]\}}>]*)?"
    r"(?=$|[\s:;,)\]\}>])",
    flags=re.IGNORECASE,
)
ARTIFACT_DESTINATION_PATTERN = re.compile(
    rf"\.(?:{ARTIFACT_EXTENSION_SOURCE})(?:[?#][^\s>]*)?"
    r"(?:\s+[\"'][^\"']*[\"'])?\s*>?$",
    flags=re.IGNORECASE,
)
FIGMA_DESTINATION_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?figma\.com/",
    flags=re.IGNORECASE,
)
SEMANTIC_NEUTRAL_FALLBACK = "task-attachment"


@dataclass(frozen=True)
class WorktreePlan:
    name: str
    branch: str
    path: Path
    base_ref: str
    branch_explicit: bool = False
    requested_name: str = ""


@dataclass(frozen=True)
class WorktreeStatus:
    name: str
    branch: str
    path: Path
    exists: bool
    branch_created_by_agent_flow: bool = False
    requested_name: str = ""


def plan_worktree(
    *,
    root: Path,
    name: str,
    branch: str | None = None,
    max_slug_length: int = 60,
    base_ref: str | None = None,
) -> WorktreePlan:
    safe_name = _feature_worktree_name(name, max_slug_length=max_slug_length)
    selected_branch = branch or f"feat/{safe_name.removeprefix('feat-')}"
    _validate_branch(selected_branch)
    if selected_branch in PROTECTED_WORKTREE_BRANCHES:
        raise ValueError(f"protected worktree branch is not allowed: {selected_branch}")
    if not selected_branch.startswith("feat/"):
        raise ValueError(f"worktree branch must start with feat/: {selected_branch}")
    return WorktreePlan(
        name=safe_name,
        branch=selected_branch,
        path=root / ".agent-flow" / "worktrees" / safe_name,
        # leader worktree가 feature branch여도 새 작업은 기본 브랜치 commit에서 시작한다.
        base_ref=base_ref or _default_base_ref(root),
        branch_explicit=branch is not None,
        requested_name=name,
    )


def plan_fresh_worktree(
    *,
    root: Path,
    name: str,
    branch: str | None = None,
    max_slug_length: int = 60,
    base_ref: str | None = None,
    reuse_registered: bool = False,
) -> WorktreePlan:
    """Allocate a new deterministic path/branch without reusing completed work."""
    initial = plan_worktree(
        root=root,
        name=name,
        branch=branch,
        max_slug_length=max_slug_length,
        base_ref=base_ref,
    )
    reusable = _matching_registered_worktree(root=root, plan=initial)
    if reuse_registered and reusable is not None:
        return WorktreePlan(
            name=initial.name,
            branch=reusable.branch,
            path=reusable.path,
            base_ref=initial.base_ref,
            branch_explicit=branch is not None,
            requested_name=name,
        )
    if branch is not None or reuse_registered:
        runtime_root = worktree_runtime_root(root=root, name=initial.name)
        if (
            initial.path.exists()
            or initial.path.is_symlink()
            or runtime_root.exists()
            or runtime_root.is_symlink()
        ):
            existing = get_worktree_status(root=root, name=initial.name)
            if existing.exists and existing.branch and existing.branch != initial.branch:
                raise ValueError(
                    f"worktree {initial.name} already uses branch {existing.branch}, "
                    f"not {initial.branch}"
                )
            raise ValueError(
                f"explicit worktree branch or path already exists: {initial.branch}"
            )
        if worktree_branch_exists(root=root, branch=initial.branch):
            raise ValueError(f"explicit worktree branch already exists: {initial.branch}")
        return initial
    base_slug = initial.name.removeprefix("feat-")
    limit = max(12, min(int(max_slug_length), 100))
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"-{counter}"
        stem = base_slug[: max(1, limit - len(suffix))].rstrip("-") or "task"
        slug = f"{stem}{suffix}"
        candidate = WorktreePlan(
            name=f"feat-{slug}",
            branch=f"feat/{slug}",
            path=root / ".agent-flow" / "worktrees" / f"feat-{slug}",
            base_ref=initial.base_ref,
            branch_explicit=False,
            requested_name=name,
        )
        if (
            not candidate.path.exists()
            and not candidate.path.is_symlink()
            and not worktree_runtime_root(root=root, name=candidate.name).exists()
            and not worktree_runtime_root(root=root, name=candidate.name).is_symlink()
            and not worktree_branch_exists(root=root, branch=candidate.branch)
        ):
            return candidate
        counter += 1


def _matching_registered_worktree(
    *,
    root: Path,
    plan: WorktreePlan,
) -> WorktreeStatus | None:
    manifest = _worktree_manifest_path(root=root, name=plan.name)
    legacy_manifest = _legacy_worktree_manifest_path(root=root, name=plan.name)
    if not manifest.exists() and legacy_manifest.exists():
        manifest = legacy_manifest
    if manifest.is_symlink() or not manifest.is_file():
        return None
    status = get_worktree_status(root=root, name=plan.name)
    if (
        not status.exists
        or status.name != plan.name
        or status.branch != plan.branch
        or not _registered_worktree_matches(
            root=root,
            path=status.path,
            branch=status.branch,
        )
    ):
        return None
    return status


def validate_worktree_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if normalized.lower().startswith("feat-"):
        normalized = normalized[5:]
    if not any(unicodedata.category(character)[0] in {"L", "N"} for character in normalized):
        raise ValueError(f"worktree name must contain at least one safe character: {value}")
    return value


def create_worktree(*, root: Path, plan: WorktreePlan, allow_dirty: bool = False) -> WorktreeStatus:
    if not allow_dirty and _git_dirty(root):
        raise RuntimeError("leader workspace is dirty; pass --allow-dirty to create a worktree anyway")
    if plan.path.exists():
        if not (plan.path / ".git").exists():
            raise RuntimeError(
                f"worktree path exists but is not a git worktree: {plan.path}. "
                f"Run `agent-flow-python worktree remove --name {plan.name}` to clear stale state."
            )
        existing = get_worktree_status(root=root, name=plan.name)
        if plan.branch_explicit and existing.branch != plan.branch:
            raise ValueError(
                f"worktree {plan.name} already uses branch {existing.branch}; "
                f"requested {plan.branch}"
            )
        _assert_same_requested_name(root=root, plan=plan)
        return existing
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_branch_exists(root=root, branch=plan.branch):
        _run_git(root, "worktree", "add", str(plan.path), plan.branch)
        branch_created = False
    else:
        _run_git(root, "worktree", "add", "-b", plan.branch, str(plan.path), plan.base_ref)
        branch_created = True
    status = WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=True,
        branch_created_by_agent_flow=branch_created,
        requested_name=plan.requested_name,
    )
    try:
        write_worktree_manifest(root=root, status=status)
    except Exception:
        try:
            remove_worktree(root=root, status=status)
        except Exception:
            pass
        raise
    return status


def remove_worktree(*, root: Path, status: WorktreeStatus, delete_branch: bool = True) -> None:
    branch_to_delete = _owned_branch_for_live_worktree(root=root, status=status) if delete_branch else None
    if status.path.exists():
        _run_git(root, "worktree", "remove", "--force", str(status.path))
    if branch_to_delete is not None:
        _run_git(root, "branch", "-D", branch_to_delete)
    remove_worktree_metadata(root=root, name=status.name)


def _assert_same_requested_name(*, root: Path, plan: WorktreePlan) -> None:
    # 서로 다른 이름이 같은 safe name으로 정규화되면 기존 worktree를 조용히
    # 재사용해 상태가 섞일 수 있다. 명시적 별칭 재사용(--worktree)은 지원되는
    # 플로우라 차단하지 않고, 기록된 원본 이름과 다르면 경고만 남긴다.
    manifest = _worktree_manifest_path(root=root, name=plan.name)
    if not manifest.exists():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    recorded = payload.get("requested_name")
    if not isinstance(recorded, str) or not recorded or not plan.requested_name:
        return
    if recorded != plan.requested_name:
        print(
            f"warning: worktree {plan.name} was created for '{recorded}'; "
            f"reusing it for '{plan.requested_name}'",
            file=sys.stderr,
        )


def worktree_branch_exists(*, root: Path, branch: str) -> bool:
    result = run_safe_command(("git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"), cwd=root)
    return result.ok


def _default_base_ref(root: Path) -> str:
    for ref in ("main", "origin/main", "master", "origin/master", "develop", "origin/develop"):
        if _git_commit_ref_exists(root=root, ref=ref):
            return ref
    return "HEAD"


def _git_commit_ref_exists(*, root: Path, ref: str) -> bool:
    result = run_safe_command(("git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"), cwd=root)
    # git을 호출할 수 없으면 기본 ref 후보가 없는 것으로 보고 HEAD fallback을 쓴다.
    return result.ok


def get_worktree_status(*, root: Path, name: str) -> WorktreeStatus:
    validate_worktree_identifier(name)
    plan = plan_worktree(root=root, name=name)
    manifest = _worktree_manifest_path(root=root, name=plan.name)
    legacy_manifest = _legacy_worktree_manifest_path(root=root, name=plan.name)
    if not manifest.exists() and legacy_manifest.exists():
        manifest = legacy_manifest
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorktreeStatus(
                name=plan.name,
                branch=plan.branch,
                path=plan.path,
                exists=plan.path.exists(),
                branch_created_by_agent_flow=False,
            )
        manifest_name = payload.get("name", plan.name)
        status_name = plan.name
        if isinstance(manifest_name, str):
            try:
                manifest_name_safe = _safe_component(manifest_name)
            except ValueError:
                manifest_name_safe = ""
            if manifest_name_safe == plan.name:
                status_name = plan.name

        manifest_branch = payload.get("branch", plan.branch)
        status_branch = plan.branch
        manifest_branch_valid = False
        if isinstance(manifest_branch, str):
            try:
                _validate_branch(manifest_branch)
            except ValueError:
                manifest_branch_valid = False
            else:
                status_branch = manifest_branch
                manifest_branch_valid = True

        branch_created = (
            payload.get("branch_created_by_agent_flow") is True
            and manifest_branch_valid
            and status_branch == plan.branch
        )
        status_path = plan.path
        manifest_path = payload.get("path")
        if isinstance(manifest_path, str) and manifest_path:
            candidate = Path(manifest_path)
            candidate = candidate if candidate.is_absolute() else root / candidate
            candidate = candidate.resolve()
            if _registered_worktree_matches(root=root, path=candidate, branch=status_branch):
                status_path = candidate
        return WorktreeStatus(
            name=status_name,
            branch=status_branch,
            path=status_path,
            exists=status_path.exists(),
            branch_created_by_agent_flow=branch_created,
        )
    return WorktreeStatus(
        name=plan.name,
        branch=plan.branch,
        path=plan.path,
        exists=plan.path.exists(),
        branch_created_by_agent_flow=False,
    )


def write_worktree_manifest(*, root: Path, status: WorktreeStatus) -> Path:
    path = _worktree_manifest_path(root=root, name=status.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    try:
        payload["path"] = str(status.path.relative_to(root))
    except ValueError:
        payload["path"] = str(status.path)
    payload["leader_root"] = str(root)
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    return path


def worktree_runtime_root(*, root: Path, name: str) -> Path:
    return _agent_flow_git_dir(root) / "worktrees" / _feature_worktree_name(name)


def known_worktree_names(*, root: Path) -> list[str]:
    names: set[str] = set()
    runtime_state_root = _agent_flow_git_dir(root)
    for state_root, label, boundary in (
        (root / ".agent-flow", "checkout", root),
        (runtime_state_root, "runtime", runtime_state_root.parent),
    ):
        if not state_root.exists() and not state_root.is_symlink():
            continue
        if state_root.is_symlink() or not state_root.is_dir():
            raise RuntimeError(
                f"blocked: unsafe agent-flow worktree {label} state root: {state_root}"
            )
        try:
            state_root.resolve().relative_to(boundary.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"blocked: agent-flow worktree {label} state root escapes its boundary: {state_root}"
            ) from exc
        candidate_root = state_root / "worktrees"
        if not candidate_root.exists() and not candidate_root.is_symlink():
            continue
        if candidate_root.is_symlink() or not candidate_root.is_dir():
            raise RuntimeError(
                f"blocked: unsafe agent-flow worktree {label} root: {candidate_root}"
            )
        resolved_root = candidate_root.resolve()
        for candidate in candidate_root.iterdir():
            if candidate.is_symlink():
                raise RuntimeError(
                    f"blocked: unsafe agent-flow worktree {label} entry: {candidate}"
                )
            if candidate.is_file():
                continue
            if not candidate.is_dir():
                raise RuntimeError(
                    f"blocked: unsafe agent-flow worktree {label} entry: {candidate}"
                )
            try:
                candidate.resolve().relative_to(resolved_root)
            except ValueError as exc:
                raise RuntimeError(
                    f"blocked: agent-flow worktree {label} entry escapes its root: {candidate}"
                ) from exc
            names.add(candidate.name)
    return sorted(names)


def remove_worktree_metadata(*, root: Path, name: str) -> None:
    runtime_root = worktree_runtime_root(root=root, name=name)
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    legacy_manifest = _legacy_worktree_manifest_path(root=root, name=name)
    if legacy_manifest.exists():
        legacy_manifest.unlink()


def _git_dirty(root: Path) -> bool:
    result = _run_git(root, "status", "--porcelain")
    dirty_lines = [
        line
        for line in result.stdout.splitlines()
        if not _is_agent_flow_status_line(line)
    ]
    return bool(dirty_lines)


def _worktree_manifest_path(*, root: Path, name: str) -> Path:
    return worktree_runtime_root(root=root, name=name) / "manifest.json"


def _legacy_worktree_manifest_path(*, root: Path, name: str) -> Path:
    return root / ".agent-flow" / "worktrees" / _feature_worktree_name(name) / "manifest.json"


def _agent_flow_git_dir(root: Path) -> Path:
    result = run_safe_command(("git", "rev-parse", "--git-common-dir"), cwd=root)
    if not result.ok:
        return root / ".agent-flow"
    git_common = Path(result.stdout.strip())
    if not git_common.is_absolute():
        git_common = root / git_common
    return git_common / "agent-flow"


def _is_agent_flow_status_line(line: str) -> bool:
    path = line[3:] if len(line) > 3 else ""
    return path == ".agent-flow" or path.startswith(".agent-flow/")


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # worktree add/remove는 큰 저장소나 느린 디스크에서 30초를 넘을 수 있어 별도 여유를 둔다.
    result = run_safe_command(("git", *args), cwd=root, timeout_s=GIT_WORKTREE_TIMEOUT_S)
    if not result.ok:
        # 호출자는 기존 subprocess 예외 경로로 처리하므로 형태를 유지한다.
        raise subprocess.CalledProcessError(
            result.returncode or 1,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return subprocess.CompletedProcess(result.args, result.returncode or 0, result.stdout, result.stderr)


def _owned_branch_for_live_worktree(*, root: Path, status: WorktreeStatus) -> str | None:
    planned_branch = plan_worktree(root=root, name=status.name).branch
    if not status.branch_created_by_agent_flow or status.branch != planned_branch:
        return None
    result = run_safe_command(("git", "-C", str(status.path), "branch", "--show-current"), cwd=root)
    if not result.ok:
        return None
    current_branch = result.stdout.strip()
    return current_branch if current_branch == planned_branch else None


def _registered_worktree_matches(*, root: Path, path: Path, branch: str) -> bool:
    result = run_safe_command(("git", "worktree", "list", "--porcelain"), cwd=root)
    if not result.ok:
        return False
    return any(
        block.startswith(f"worktree {path}\n") and f"\nbranch refs/heads/{branch}" in block
        for block in result.stdout.split("\n\n")
    )


def _safe_component(value: str, *, max_length: int = 60) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    without_artifacts = _strip_artifact_references(normalized)
    safe = "".join(
        character if unicodedata.category(character)[0] in {"L", "N"} else "-"
        for character in without_artifacts
    )
    safe = safe.strip("-")
    safe = re.sub(r"-+", "-", safe)
    if not safe:
        safe = SEMANTIC_NEUTRAL_FALLBACK
    limit = max(12, min(int(max_length), 100))
    if len(safe) > limit:
        digest = hashlib.sha1(safe.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe[: limit - 7].rstrip('-')}-{digest}"
    return safe


def _strip_artifact_references(value: str) -> str:
    def replace_markdown(match: re.Match[str]) -> str:
        image_marker, label, destination = match.groups()
        normalized_destination = destination.strip().removeprefix("<").removesuffix(">")
        if (
            image_marker
            or FIGMA_DESTINATION_PATTERN.search(normalized_destination)
            or ARTIFACT_DESTINATION_PATTERN.search(normalized_destination)
        ):
            return " "
        return f" {label} "

    stripped = MARKDOWN_REFERENCE_PATTERN.sub(replace_markdown, value)
    stripped = URL_PATTERN.sub(" ", stripped)
    stripped = QUOTED_ARTIFACT_PATTERN.sub(" ", stripped)
    stripped = PATH_ARTIFACT_PATTERN.sub(lambda match: match.group(1), stripped)
    return BARE_ARTIFACT_PATTERN.sub(lambda match: match.group(1), stripped)


def _feature_worktree_name(value: str, *, max_slug_length: int = 60) -> str:
    # Keep the deterministic feat/<slug> branch ↔ feat-<slug> directory
    # contract shared by YAML, Python, Node, and all host adapters.
    normalized = unicodedata.normalize("NFKC", value).strip()
    if normalized.lower().startswith("feat-"):
        normalized = normalized[5:]
    slug = _safe_component(normalized, max_length=max_slug_length)
    return f"feat-{slug}"


def _validate_branch(value: str) -> None:
    invalid_chars = set(" ~^:?*[\\")
    invalid = (
        not value
        or value.startswith("-")
        or value.startswith(".")
        or value.startswith("/")
        or value.endswith("/")
        or value.endswith(".")
        or value.endswith(".lock")
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(ord(ch) < 32 or ch == "\x7f" or ch in invalid_chars for ch in value)
    )
    if invalid:
        raise ValueError(f"unsafe worktree branch: {value}")
