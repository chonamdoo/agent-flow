"""관리 hook 등록 무결성 — 런 시작 시 `kit.json` 기록과 실제 등록을 대조한다.

tripwire(`assert_leader_unchanged`)는 런 *도중*의 변경만 본다. 런이 시작될 때
이미 오염돼 있으면 `capture_leader_snapshot`이 그 오염을 그대로 기준선으로
굳혀서 tripwire까지 통째로 무의미해진다. 그래서 이 검증은 **반드시 첫
`capture_leader_snapshot`보다 먼저** 돈다.

변경탐지가 아니라 대조인 이유는 오라클이 있기 때문이다. install이 심는
스크립트 집합(`MANAGED_HOOK_SCRIPTS`)과 hook 사용 여부(`kit.json:hooks`)가
기대값이다. 그래서 판정은 "없으면 위반"이 아니라 **"기록과 다르면 위반"**이다.
`--no-hooks` 설치와 hook 미지원 host는 정상 상태이고, `hooks` 키가 아예 없는
설치본은 대조할 기록이 없으므로 검증하지 않는다.

**관측 hook은 PostToolUse, 강제 hook은 PreToolUse.** PreToolUse는 host가 허용/
차단 판정을 기대하는 자리라, 관측용 스크립트를 거기 달면 스크립트가 없거나
죽는 순간 host가 그것을 차단으로 읽어 사용자 도구가 통째로 막힌다. 관측자를
늘리려던 변경이 실행 경로를 끊는 것이 이 계약이 막는 사고다.

판정 범위는 **관리 네임스페이스**(`.agent-flow/scripts/hooks/`)로 한정한다.
그 밖의 사용자 hook은 정당한 설정이라 오라클이 없다 — 런 *도중*에 그 파일들이
바뀌는 것은 tripwire 심층 스캔(`worktree_isolation._EXEC_SURFACE_PATHS`)이 본다.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# install이 심는 정확히 그 스크립트들. JS 쪽 등록 지점 3곳
# (`bin/agent-flow-install.mjs`, `bin/agent-flow-kit.mjs`,
# `scripts/check-agent-flow-parity.mjs`)과 갈라지면 parity가 잡는다.
MANAGED_HOOK_SCRIPTS = (
    "guard-protected-branch.sh",
    "show-phase-status.sh",
    "comment-checker.py",
    "record-skill-read.py",
)

# 판정하지 않고 기록만 하는 hook. 항상 exit 0이며 PreToolUse에 달면 안 된다.
OBSERVATIONAL_HOOK_SCRIPTS = (
    "record-skill-read.py",
)

# host가 허용/차단을 기대하는 이벤트. 관측 hook 금지 구역이다.
ENFORCEMENT_EVENT = "PreToolUse"

HOOK_DIR_RELATIVE = Path(".agent-flow") / "scripts" / "hooks"
KIT_JSON_RELATIVE = Path(".agent-flow") / "kit.json"

# install이 실제로 쓰는 등록 파일들. JSON host는 이벤트별 구조를 그대로 읽고,
# OMP 확장은 kit이 통째로 생성하는 소스라 스크립트 이름 존재만 본다.
JSON_REGISTRATION_FILES = (
    Path(".claude") / "settings.json",
    Path(".Codex") / "hooks.json",
    Path(".codex") / "hooks.json",
)
OMP_REGISTRATION_FILE = Path(".omp") / "extensions" / "agent-flow-hooks.ts"

# 관리 디렉터리 안에서 hook이 아닌 것으로 인정하는 이름. `--no-hooks`가 남기는
# 은퇴 사본과 실행으로 생기는 bytecode다.
_NON_HOOK_SUFFIXES = (".removed",)
_NON_HOOK_DIRS = ("__pycache__",)


class HookIntegrityError(RuntimeError):
    """관리 hook 등록이 `kit.json` 기록과 어긋난다."""


@dataclass(frozen=True)
class HookIntegrityReport:
    root: Path
    recorded: bool          # kit.json에 `hooks` 기록이 있는가
    expected_enabled: bool
    violations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class _Surface:
    name: str
    present: bool
    readable: bool
    entries: tuple[tuple[str, str], ...]  # (event, managed-path script name)

    def scripts(self) -> set[str]:
        return {script for _, script in self.entries}


def find_install_root(start) -> Path | None:
    """`.agent-flow/kit.json`을 가진 가장 가까운 조상. hook 자신과 같은 규칙이다."""
    if start is None:
        return None
    current = Path(start)
    for candidate in [current, *current.parents]:
        if _is_file(candidate / KIT_JSON_RELATIVE):
            return candidate
    return None


def assert_managed_hooks_registered(*roots) -> tuple[HookIntegrityReport, ...]:
    """런 시작 게이트. 어긋나면 `HookIntegrityError`로 멈춘다.

    되돌리거나 재설치하지 않는다. install은 현재 등록된 것을 그대로 기대값으로
    되받아 적으므로, 위반을 자동 복구하면 그게 곧 승인 세탁이다.
    """
    reports = verify_managed_hooks(*roots)
    failed = [report for report in reports if not report.ok]
    if not failed:
        return reports
    detail = "; ".join(
        f"{report.root}: " + ", ".join(report.violations) for report in failed
    )
    raise HookIntegrityError(
        "managed hook registration does not match .agent-flow/kit.json: "
        + detail
        + ". Nothing was changed. The enforcement and observation hooks this run "
        "depends on may be disabled, so the run refuses to start. Re-run "
        "`node bin/agent-flow-kit.mjs install --hooks` from the leader checkout "
        "only after confirming the registration was not tampered with."
    )


def verify_managed_hooks(*roots) -> tuple[HookIntegrityReport, ...]:
    """중복 없는 설치 루트마다 리포트를 만든다. 설치본이 없으면 빈 튜플이다."""
    seen: list[Path] = []
    for candidate in roots:
        root = find_install_root(candidate)
        if root is not None and root not in seen:
            seen.append(root)
    return tuple(_verify_root(root) for root in seen)


def _verify_root(root: Path) -> HookIntegrityReport:
    kit = _read_kit_json(root)
    if not isinstance(kit.get("hooks"), bool):
        return HookIntegrityReport(root, recorded=False, expected_enabled=False, violations=())
    expected_enabled = bool(kit["hooks"])
    surfaces = tuple(_read_surfaces(root))
    violations: list[str] = []
    violations.extend(_unapproved_scripts_on_disk(root))
    violations.extend(
        f"cannot read the hook registration file: {surface.name}"
        for surface in surfaces
        if surface.present and not surface.readable
    )
    if expected_enabled:
        violations.extend(_missing_registrations(root, surfaces))
    else:
        violations.extend(_unexpected_registrations(surfaces))
    violations.extend(_unapproved_registrations(surfaces))
    violations.extend(_misplaced_observational_hooks(surfaces))
    return HookIntegrityReport(
        root,
        recorded=True,
        expected_enabled=expected_enabled,
        violations=tuple(violations),
    )


def _unapproved_scripts_on_disk(root: Path) -> Iterator[str]:
    """관리 디렉터리에 관리 대상이 아닌 실행 파일이 있는가.

    등록 전에 심어 두고 다음 install이 등록을 만들어 주기를 기다리는 경로를
    막는다. 등록 검사만으로는 그 시점을 못 본다.
    """
    hook_dir = root / HOOK_DIR_RELATIVE
    try:
        entries = sorted(hook_dir.iterdir())
    except OSError:
        # 못 읽는 디렉터리를 예외로 흘리면 복구 명령까지 같이 죽는다.
        return
    for entry in entries:
        name = entry.name
        if name in MANAGED_HOOK_SCRIPTS or name in _NON_HOOK_DIRS:
            continue
        if name.endswith(_NON_HOOK_SUFFIXES):
            continue
        try:
            info = entry.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            yield f"unapproved executable in the managed hook directory: {name}"


def _missing_registrations(root: Path, surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    hook_dir = root / HOOK_DIR_RELATIVE
    for script in MANAGED_HOOK_SCRIPTS:
        if not _is_file(hook_dir / script):
            yield f"managed hook script is missing from disk: {script}"
    for surface in surfaces:
        if not surface.present:
            yield f"{surface.name} is missing, so no managed hook is registered there"
            continue
        if not surface.readable:
            continue
        registered = surface.scripts()
        for script in MANAGED_HOOK_SCRIPTS:
            if script not in registered:
                yield f"{surface.name} does not register {script}"


def _unexpected_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for script in sorted(surface.scripts() & set(MANAGED_HOOK_SCRIPTS)):
            yield f"hooks are disabled in kit.json but {surface.name} still registers {script}"


def _unapproved_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for script in sorted(surface.scripts() - set(MANAGED_HOOK_SCRIPTS)):
            yield f"{surface.name} registers an unapproved managed-path hook: {script}"


def _misplaced_observational_hooks(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for event, script in sorted(set(surface.entries)):
            if event == ENFORCEMENT_EVENT and script in OBSERVATIONAL_HOOK_SCRIPTS:
                yield (
                    f"{surface.name} registers the observation hook {script} on "
                    f"{ENFORCEMENT_EVENT}; observation hooks must stay on PostToolUse "
                    "or a failing observer becomes a block decision"
                )


def _read_surfaces(root: Path) -> Iterator[_Surface]:
    """install이 쓰는 등록 파일마다 `(event, script)` 목록을 만든다.

    존재하지 않는 파일도 레코드를 남긴다. 없는 것을 건너뛰면 등록 파일을
    통째로 지우는 것이 가장 싼 우회가 된다.
    """
    for relative in JSON_REGISTRATION_FILES:
        path = root / relative
        if not _is_file(path):
            yield _Surface(str(relative), present=False, readable=False, entries=())
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 읽지 못하는 등록 파일을 "등록 없음"으로 접으면, 파일을 깨뜨리는
            # 것만으로 검증이 사라진다. 읽기 실패 자체를 위반으로 든다.
            yield _Surface(str(relative), present=True, readable=False, entries=())
            continue
        yield _Surface(
            str(relative),
            present=True,
            readable=True,
            entries=tuple(_json_registrations(root, payload)),
        )
    omp = root / OMP_REGISTRATION_FILE
    if not _is_file(omp):
        yield _Surface(str(OMP_REGISTRATION_FILE), present=False, readable=False, entries=())
        return
    try:
        text = omp.read_text(encoding="utf-8")
    except OSError:
        yield _Surface(str(OMP_REGISTRATION_FILE), present=True, readable=False, entries=())
        return
    # OMP 확장은 kit이 통째로 생성하는 소스다. 이벤트 자리는 소스 안의
    # `pi.on(...)` 핸들러라 여기서 판정하지 않는다. 그 배치는 두 installer의
    # 확장 소스가 바이트 단위로 같은지 보는 parity가 지킨다.
    yield _Surface(
        str(OMP_REGISTRATION_FILE),
        present=True,
        readable=True,
        entries=tuple(("", script) for script in MANAGED_HOOK_SCRIPTS if script in text),
    )


def _json_registrations(root: Path, payload: object) -> Iterator[tuple[str, str]]:
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for command in _entry_commands(entry):
                script = managed_path_hook_name(root, command)
                if script is not None:
                    yield str(event), script


def _entry_commands(entry: object) -> Iterator[str]:
    if not isinstance(entry, dict):
        return
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return
    for hook in hooks:
        if isinstance(hook, dict) and isinstance(hook.get("command"), str):
            yield hook["command"]


def managed_path_hook_name(root: Path, command: str) -> str | None:
    """`command`가 이 루트의 관리 hook 디렉터리를 가리키면 스크립트 이름을 준다.

    이름만 비교하지 않고 디렉터리까지 대조하는 이유는, 같은 이름을 다른 곳에
    두고 등록하는 우회를 막기 위해서다. 반대로 관리 디렉터리 안이기만 하면
    관리 대상이 아닌 이름도 돌려준다 — 그게 "미승인 hook"의 정의다.
    """
    normalized = _unquote(command).replace("\\", "/")
    marker = f"/{HOOK_DIR_RELATIVE.as_posix()}/"
    index = normalized.rfind(marker)
    if index < 0:
        prefix = f"{HOOK_DIR_RELATIVE.as_posix()}/"
        if not normalized.startswith(prefix):
            return None
        tail = normalized[len(prefix):]
        base = str(root)
    else:
        tail = normalized[index + len(marker):]
        base = normalized[:index]
    name = tail.split()[0] if tail.split() else ""
    if not name or "/" in name:
        return None
    try:
        if os.path.realpath(base) != os.path.realpath(root):
            return None
    except OSError:
        return None
    return name


def _unquote(value: str) -> str:
    text = value.strip()
    for quote in ("'", '"'):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            return text[1:-1]
    return text


def _read_kit_json(root: Path) -> dict:
    try:
        payload = json.loads((root / KIT_JSON_RELATIVE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_file(path: Path) -> bool:
    """`Path.is_file`은 EACCES를 삼키지 않고 다시 던진다. 여기서는 없는 것과 같다."""
    try:
        return path.is_file()
    except OSError:
        return False
