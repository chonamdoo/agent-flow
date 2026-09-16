from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from agent_flow.core.review_scope import (
    publication_review_base,
    validate_publication_review_base,
)
from agent_flow.core.worktree_isolation import (
    WorktreeIsolationError,
    git_proves_ancestor,
    git_safe,
)


_REVIEW_INPUT_TIMEOUT_S = 120
_REVIEW_INPUT_MAX_BYTES = 8 * 1024 * 1024
_OID_PATTERN = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
# base ref는 git argv에 그대로 들어간다. 옵션처럼 보이는 값이나 revision 문법이
# 섞인 값은 거부한다.
_BASE_REF_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/+@"
)

@dataclass(frozen=True)
class ReviewInputObservation:
    content: str
    document_scope: tuple[str, ...] | None


class _ReviewInputUnavailable(WorktreeIsolationError):
    """Publication limits only; observation and scope-integrity failures remain fatal."""


def review_document_scope(
    project_root: Path, run_meta: Mapping[str, object], *, base_branch: str | None = None,
) -> tuple[str, ...] | None:
    """Observe review scope without publishing or replacing reviewer evidence."""
    try:
        return capture_review_input(
            project_root, run_meta, "", base_branch=base_branch,
        ).document_scope
    except _ReviewInputUnavailable:
        return None


def capture_review_input(
    project_root: Path,
    run_meta: Mapping[str, object],
    phase_id: str,
    *,
    base_branch: str | None = None,
) -> ReviewInputObservation:
    """리뷰 증거의 기준점은 기본적으로 merge-base, publication 범위에서는 고정 OID다.

    `HEAD` 기준으로 찍으면 작업이 이미 커밋된 브랜치에서는 모든 섹션이 비는데,
    같은 프롬프트가 리뷰어에게 샌드박스 안에서 `git diff`를 돌리지 말라고 말한다.
    그래서 리뷰어는 근거 없이 판정하게 된다 — 라운드 하나가 실제로 그렇게 무너졌다.
    """
    try:
        publication_base = publication_review_base(run_meta)
        if publication_base is not None:
            validate_publication_review_base(project_root, publication_base)
    except ValueError as exc:
        raise WorktreeIsolationError(
            f"could not precompute reviewer input: invalid publication review scope: {exc}"
        ) from exc
    # 관측 하나당 상한을 전체 예산보다 낮게 잡는다. unborn HEAD 경로는 관측을
    # 셋까지 만들고, 합계가 예산을 넘으면 스냅샷 자체를 못 쓴다. 상한에 걸린
    # 섹션은 라운드를 죽이지 않고 머리말에 잘렸다고 적는다.
    observation_max_bytes = max(1, _REVIEW_INPUT_MAX_BYTES // 4)
    if publication_base is None:
        baseline = _resolve_review_baseline(
            project_root,
            base_branch,
            max_output_bytes=observation_max_bytes,
        )
    else:
        baseline = _ReviewBaseline(
            rev=publication_base,
            detail=(
                f"pinned publication base {publication_base} — every change "
                "through the current working tree is below, committed and "
                "uncommitted alike"
            ),
        )
    status = git_safe(
        "-c", "core.quotepath=true",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=observation_max_bytes,
    )
    diff = git_safe(
        "-c", "core.quotepath=true",
        "diff",
        "--no-ext-diff",
        "--no-color",
        "--raw", "--patch", "--no-renames",
        baseline.rev,
        "--",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=observation_max_bytes,
    )
    notes: list[str] = []
    if baseline.note:
        notes.append(baseline.note)
    diff_observations = []
    if publication_base is None and _is_unborn_head_failure(diff):
        notes.append(
            "HEAD carries no commit yet, so the staged and working-tree diffs "
            "below stand in for a baseline diff"
        )
        diff_observations.extend((
            (
                "git diff --cached",
                git_safe(
                    "-c", "core.quotepath=true",
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-color",
                    "--raw", "--patch", "--no-renames",
                    "--",
                    cwd=project_root,
                    optional_locks=False,
                    timeout_s=_REVIEW_INPUT_TIMEOUT_S,
                    max_output_bytes=observation_max_bytes,
                ),
            ),
            (
                "git diff",
                git_safe(
                    "-c", "core.quotepath=true",
                    "diff",
                    "--no-ext-diff",
                    "--no-color",
                    "--raw", "--patch", "--no-renames",
                    "--",
                    cwd=project_root,
                    optional_locks=False,
                    timeout_s=_REVIEW_INPUT_TIMEOUT_S,
                    max_output_bytes=observation_max_bytes,
                ),
            ),
        ))
    else:
        diff_observations.append((f"git diff {baseline.rev}", diff))
    observations = [("git status --porcelain=v1", status), *diff_observations]
    failed = [
        f"{label}: {result.stderr.strip() or result.error or result.returncode}"
        for label, result in observations
        if not result.ok and not _hit_output_limit(result)
    ]
    if failed:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: " + "; ".join(failed)
        )
    truncated = [
        label for label, result in observations if _hit_output_limit(result)
    ]
    if truncated:
        notes.append(
            f"truncated at {observation_max_bytes} bytes, so the sections are "
            f"incomplete: {', '.join(truncated)}"
        )
    has_diff = any(result.stdout.strip() for _, result in diff_observations)
    status_lines = [line for line in status.stdout.splitlines() if line.strip()]
    # 추적 중인 변경은 반드시 diff로도 나타난다. 그런데 diff가 비었다면 스냅샷을
    # 못 만든 것이다. 추적되지 않는 파일(`??`)은 status 목록 자체가 증거다.
    tracked = [line for line in status_lines if not line.startswith("??")]
    if tracked and not has_diff:
        raise WorktreeIsolationError(
            "could not precompute reviewer input: git status reports "
            f"{len(tracked)} tracked change(s) but the diff against "
            f"{baseline.rev} is empty"
        )
    # 성공했지만 빈 출력은 두 가지다. 변경이 정말 없는 review-only 작업은 정당하고,
    # 기준점을 못 잡아 아무것도 못 담은 것은 리뷰를 통과시키면 안 된다.
    if not status_lines and not has_diff:
        if baseline.base_unresolved:
            raise _ReviewInputUnavailable(
                "could not precompute reviewer input: no diff is available "
                f"against declared base `{base_branch}` ({baseline.detail}); "
                "reviewers would receive no evidence"
            )
        notes.append(
            "no change relative to this baseline: the snapshot is a verified "
            "empty diff, not a missing one"
        )
    header = [
        "# Reviewer input snapshot",
        "",
        f"- phase: {phase_id}",
        f"- diff baseline: {baseline.detail}",
    ]
    if publication_base is not None:
        header.extend((
            "- review scope: publication-only; not whole-PR or merge approval",
            "- scope contract: judge defects introduced or worsened by this "
            "delta, including security defects, without path or category exclusions; "
            "surrounding code may be read for context",
            "- prior findings: unchanged pre-baseline findings remain separately "
            "recorded risks, not fixed findings or approval of the existing PR",
        ))
    header.extend(f"- note: {note}" for note in notes)
    sections = [
        f"## {label}\n\n{result.stdout.rstrip() or '(empty)'}"
        for label, result in observations
    ]
    content = "\n".join(header) + "\n\n" + "\n\n".join(sections) + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > _REVIEW_INPUT_MAX_BYTES:
        raise _ReviewInputUnavailable(
            "could not precompute reviewer input: "
            f"snapshot exceeds {_REVIEW_INPUT_MAX_BYTES} bytes"
        )
    return ReviewInputObservation(
        content=content,
        document_scope=(
            None if truncated or baseline.base_unresolved
            else _snapshot_document_scope(status.stdout, [result.stdout for _, result in diff_observations])
        ),
    )


def _snapshot_document_scope(status: str, diffs: Sequence[str]) -> tuple[str, ...] | None:
    paths: set[str] = set()
    for diff in diffs:
        headers = [line for line in diff.splitlines() if line.startswith(":")]
        if diff.strip() and not headers:
            return None
        for header in headers:
            metadata, separator, raw = header.partition("\t")
            if not separator or not re.fullmatch(r":[0-7]{6} [0-7]{6} [0-9a-f]+ [0-9a-f]+ [A-Z][0-9]*", metadata):
                return None
            path = _snapshot_path(raw)
            if path is None:
                return None
            paths.add(path)
    for line in status.splitlines():
        if not line.strip():
            continue
        if len(line) < 4 or line[2] != " " or not re.fullmatch(r"[ MADRCU?!]{2}", line[:2]):
            return None
        raw_paths = (line[3:],)
        if "R" in line[:2] or "C" in line[:2]:
            renamed = re.fullmatch(
                r'("(?:\\.|[^"\\])*"|[^\s"]+) -> ("(?:\\.|[^"\\])*"|[^\s"]+)',
                line[3:],
            )
            if renamed is None:
                return None
            raw_paths = renamed.groups()
        for raw in raw_paths:
            path = _snapshot_path(raw)
            if path is None:
                return None
            paths.add(path)
    return tuple(sorted(paths))


def _snapshot_path(raw: str) -> str | None:
    try:
        value = ast.literal_eval(raw) if raw.startswith('"') else raw
        if raw.startswith('"') and re.search(r"\\[0-7]{3}", raw):
            value = value.encode("latin-1").decode("utf-8")
    except (SyntaxError, ValueError, UnicodeError):
        return None
    if (
        not isinstance(value, str) or not value or value.startswith("/")
        or ".." in value.split("/") or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


@dataclass(frozen=True)
class _ReviewBaseline:
    rev: str
    detail: str
    # 선언된 base가 있는데 그걸 기준으로 삼지 못한 상태. 이때의 빈 diff는
    # "변경 없음"의 증거가 될 수 없다.
    base_unresolved: bool = False
    # 기준점을 그 후보에서 잡은 근거. 선언된 base를 쓰지 못한 경우에만 채운다.
    note: str = ""


def _resolve_review_baseline(
    project_root: Path,
    base_branch: str | None,
    *,
    max_output_bytes: int,
) -> _ReviewBaseline:
    fallback = "`HEAD` — changes already committed on this branch are NOT below"
    if not base_branch:
        return _ReviewBaseline(
            rev="HEAD",
            detail=(
                f"{fallback} (the active profile declares no `branching.base`)"
            ),
        )
    if not _is_usable_base_ref(base_branch):
        return _ReviewBaseline(
            rev="HEAD",
            detail=(
                f"{fallback} (declared base `{base_branch}` is not a usable git "
                "ref name)"
            ),
            base_unresolved=True,
        )
    diagnostic = "no common ancestor"
    resolved: list[_BaseCandidate] = []
    for candidate in _base_candidate_refs(
        project_root, base_branch, max_output_bytes=max_output_bytes
    ):
        result = git_safe(
            "merge-base",
            "HEAD",
            candidate,
            cwd=project_root,
            optional_locks=False,
            timeout_s=_REVIEW_INPUT_TIMEOUT_S,
            max_output_bytes=max_output_bytes,
        )
        oid = result.stdout.strip() if result.ok else ""
        if _OID_PATTERN.fullmatch(oid):
            resolved.append(_BaseCandidate(ref=candidate, oid=oid))
            continue
        if not result.ok:
            stderr = result.stderr.strip()
            diagnostic = (
                stderr.splitlines()[-1]
                if stderr
                else result.error or f"git exited {result.returncode}"
            )
    if not resolved:
        return _ReviewBaseline(
            rev="HEAD",
            detail=(
                f"{fallback} (declared base `{base_branch}` could not be used: "
                f"{diagnostic})"
            ),
            base_unresolved=True,
        )
    choice = _newest_review_baseline(project_root, resolved)
    return _ReviewBaseline(
        rev=choice.oid,
        detail=(
            f"`git merge-base HEAD {choice.candidate}` = {choice.oid} — every "
            "change from the base through the current working tree is below, "
            "committed and uncommitted alike"
        ),
        note=choice.note,
    )


def _base_candidate_refs(
    project_root: Path,
    base_branch: str,
    *,
    max_output_bytes: int,
) -> tuple[str, ...]:
    """선언된 base와, 그 base가 실제로 추적하는 remote ref.

    `origin/`을 정본으로 두면 fork나 다중 remote 체크아웃에서 틀린 기준점을 고른다 —
    선언된 `main`이 `upstream/main`을 추적하는데 `origin/main`을 기준으로 잡으면,
    origin에만 있는 커밋이 선언된 base 대비 변경인데도 diff에서 조용히 빠진다.
    추적 설정이 없으면 `origin/<base>`로 내려간다. 그 경우가 단일 remote 체크아웃이다.
    """
    result = git_safe(
        "rev-parse",
        "--symbolic-full-name",
        f"{base_branch}@{{upstream}}",
        cwd=project_root,
        optional_locks=False,
        timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        max_output_bytes=max_output_bytes,
    )
    tracked = result.stdout.strip() if result.ok else ""
    prefix = "refs/remotes/"
    remote_ref = tracked[len(prefix):] if tracked.startswith(prefix) else ""
    # git이 준 값도 argv에 그대로 들어간다. 선언된 base와 같은 검사를 통과해야 한다.
    if not remote_ref or not _is_usable_base_ref(remote_ref):
        remote_ref = f"origin/{base_branch}"
    if remote_ref == base_branch:
        return (base_branch,)
    return (base_branch, remote_ref)


class _BaseCandidate(NamedTuple):
    """base ref 하나와 그 merge-base. 두 칸이 모두 `str`이라 위치로 두면 조용히 섞인다."""

    ref: str
    oid: str


class _BaselineChoice(NamedTuple):
    """어느 후보에서 기준점을 잡았는가. 세 칸이 모두 `str`이라 위치로 두면 조용히 섞인다."""

    candidate: str
    oid: str
    note: str


def _newest_review_baseline(
    project_root: Path,
    resolved: Sequence[_BaseCandidate],
) -> _BaselineChoice:
    """후보 중 가장 descendant인 merge-base와 그 선택의 근거. `resolved`는 비어 있지 않다.

    먼저 resolve된 후보를 쓰면 뒤처진 로컬 base ref가 항상 이긴다. 로컬 base는
    아무도 전진시키지 않는다(킷의 유일한 fetch는 cleanup 전용이고 remote-tracking
    ref만 갱신한다). 그러면 스냅샷에 이미 upstream에 머지된 커밋이 들어가고,
    리뷰어는 그것을 이 브랜치의 변경으로 읽어 코드로는 지울 수 없는
    request-changes를 낸다.

    순서를 정하지 못한 두 기준점 중에서는 뒤에 선언된 후보(remote-tracking)를
    쓴다. 둘 다 HEAD의 조상이지만 서로를 포함하지 않는 상태에서 하나를 골라야
    하고, 이미 통합된 쪽을 기준으로 삼는 것이 리뷰 범위에 대한 사실에 가깝다.
    그때 다른 후보에서만 닿는 커밋은 diff에 남는다 — rev 하나로는 두 base를
    동시에 뺄 수 없으므로, 그 사실을 note에 적는다.

    note는 비교마다 **누적한다**. 후보가 셋 이상일 때 뒤 비교가 앞의 인정을 덮으면
    머리말은 깨끗한 기준점을 주장하면서 diff에 남은 커밋을 숨긴다 — 이 변경이
    지우려는 실패가 그 자리에서 그대로 돌아온다.
    """
    best = resolved[0]
    notes: list[str] = []
    for candidate in resolved[1:]:
        if candidate.oid == best.oid:
            continue
        if git_proves_ancestor(
            root=project_root,
            ancestor=best.oid,
            descendant=candidate.oid,
            timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        ):
            notes.append(
                f"declared base `{best.ref}` is behind `{candidate.ref}`, so its "
                "merge-base still carries commits that are already merged upstream; "
                f"the baseline above is the `{candidate.ref}` merge-base and those "
                "commits are not in the diff below"
            )
        elif git_proves_ancestor(
            root=project_root,
            ancestor=candidate.oid,
            descendant=best.oid,
            timeout_s=_REVIEW_INPUT_TIMEOUT_S,
        ):
            continue
        else:
            notes.append(
                f"`{best.ref}` and `{candidate.ref}` merge-bases could not be "
                "ordered, so the baseline above is the remote-tracking one "
                f"(`{candidate.ref}`); commits reachable only from `{best.ref}` are "
                "still in the diff below"
            )
        best = candidate
    return _BaselineChoice(candidate=best.ref, oid=best.oid, note="; ".join(notes))


def _is_usable_base_ref(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("-")
        and ".." not in value
        and set(value) <= _BASE_REF_CHARS
    )


def _hit_output_limit(result) -> bool:
    """상한에 걸려 잘린 것과 실패한 것은 다르다. 잘려도 부분 증거는 남는다."""
    return (
        result.output_truncated
        and not result.timed_out
        and result.error is None
    )


def _is_unborn_head_failure(result) -> bool:
    diagnostic = f"{result.stderr}\n{result.error or ''}".lower()
    return (
        result.returncode == 128
        and "head" in diagnostic
        and ("ambiguous" in diagnostic or "bad revision" in diagnostic)
    )


