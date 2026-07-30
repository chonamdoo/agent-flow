"""관리 hook 등록 무결성 — 런 시작 시 `kit.json` 기록과 실제 등록을 대조한다.

tripwire(`assert_leader_unchanged`)는 런 *도중*의 변경만 본다. 런이 시작될 때
이미 오염돼 있으면 `capture_leader_snapshot`이 그 오염을 그대로 기준선으로
굳혀서 tripwire까지 통째로 무의미해진다. 그래서 이 검증은 **반드시 첫
`capture_leader_snapshot`보다 먼저** 돈다.

변경탐지가 아니라 대조인 이유는 오라클이 있기 때문이다. install이 심는
스크립트 집합(`MANAGED_HOOK_SCRIPTS`)과 hook 사용 여부(`kit.json:hooks`)가
기대값이다. 그래서 판정은 "없으면 위반"이 아니라 **"기록과 다르면 위반"**이다.
`--no-hooks` 설치와 hook 미지원 host는 파일 상태 검사에서는 정상으로 보고하지만,
런 시작 게이트는 강제 hook이 없는 상태를 안전하다고 증명할 수 없어 거부한다.

**관측 hook은 PostToolUse, 강제 hook은 PreToolUse.** PreToolUse는 host가 허용/
차단 판정을 기대하는 자리라, 관측용 스크립트를 거기 달면 스크립트가 없거나
죽는 순간 host가 그것을 차단으로 읽어 사용자 도구가 통째로 막힌다. 관측자를
늘리려던 변경이 실행 경로를 끊는 것이 이 계약이 막는 사고다.
판정 범위는 **관리 네임스페이스**(`.agent-flow/scripts/hooks/`)로 한정한다.
그 밖의 사용자 hook은 정당한 설정이라 오라클이 없다 — 런 *도중*에 그 파일들이
바뀌는 것은 tripwire 심층 스캔(`worktree_isolation._EXEC_SURFACE_PATHS`)이 본다.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from agent_flow.core.worktree_isolation import leader_root_for

# install이 심는 정확히 그 스크립트들. JS 쪽 등록 지점 3곳
# (`bin/agent-flow-install.mjs`, `bin/agent-flow-kit.mjs`,
# `scripts/check-agent-flow-parity.mjs`)과 갈라지면 parity가 잡는다.
MANAGED_HOOK_SCRIPTS = (
    "bind-host-worktree.py",
    "confirm-spec-user-prompt.py",
    "guard-protected-branch.sh",
    "guard-host-worktree.sh",
    "guard-spec-approval.sh",
    "prepare-spec-user-prompt.py",
    "show-phase-status.sh",
    "comment-checker.py",
    "record-skill-read.py",
    "record-command-run.py",
    "worktree-tripwire.py",
)

# 판정하지 않고 기록만 하는 hook. 항상 exit 0이며 PreToolUse에 달면 안 된다.
OBSERVATIONAL_HOOK_SCRIPTS = (
    "record-skill-read.py",
    "record-command-run.py",
)

# host가 허용/차단을 기대하는 이벤트. 관측 hook 금지 구역이다.
ENFORCEMENT_EVENT = "PreToolUse"

# 스크립트별 기대 이벤트와 matcher. **이름 집합만 대조하면 자리 바꾸기를 못 본다** —
# `guard-protected-branch.sh`를 PostToolUse로 옮기면 보호 브랜치 명령이 *실행된 뒤*
# hook이 불려 차단이 사라지는데, 이름은 그대로라 통과한다. matcher도 같은 이유다:
# `"Bash"`를 `"^$"`로 바꾸면 등록은 남고 hook은 영영 안 불린다.
# JS installer의 실제 등록과 갈라지면 parity가 잡는다.
MANAGED_HOOK_PLACEMENT = {
    "bind-host-worktree.py": (
        "PostToolUse",
        "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$",
    ),
    "confirm-spec-user-prompt.py": ("UserPromptSubmit", ""),
    "guard-protected-branch.sh": ("PreToolUse", "Bash"),
    "guard-host-worktree.sh": (
        "PreToolUse",
        "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|"
        "Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$",
    ),
    "guard-spec-approval.sh": (
        "PreToolUse",
        "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|"
        "Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$",
    ),
    "prepare-spec-user-prompt.py": (
        "PostToolUse",
        "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|"
        "Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$",
    ),
    "comment-checker.py": (
        "PostToolUse",
        "^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$",
    ),
    # 값은 한 줄로 둔다. parity 파서가 암시적 문자열 연결을 접지 못해 항목이 무음으로
    # 탈락하고, 그러면 JS/Python 갈라짐이 CI를 통과한다.
    "record-skill-read.py": ("PostToolUse", "^(Read|read|read_file|view|cat|Skill|skill|Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$"),
    "record-command-run.py": (
        "PostToolUse",
        "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$",
    ),
    "worktree-tripwire.py": (
        "PostToolUse",
        "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$",
    ),
    "show-phase-status.sh": ("Stop", ""),
}

HOOK_DIR_RELATIVE = Path(".agent-flow") / "scripts" / "hooks"
KIT_JSON_RELATIVE = Path(".agent-flow") / "kit.json"
# spec 승인 hook이 CLI를 실행하는 유일한 경로. 이 파일이 없으면 hook은 실행할
# 것을 못 찾아 조용히 exit 0 하고, 사용자는 채팅 `승인`이 무시되는 이유를
# 어디에서도 못 본다. 그래서 등록과 같은 급으로 런 시작 때 대조한다.
PROJECT_LAUNCHER_RELATIVE = Path(".agent-flow") / "bin" / "agent-flow"
# launcher가 실제로 태울 CLI. digest만 맞고 이게 없으면 launcher는 exit 127이다.
RUNTIME_CLI_RELATIVE = Path(".agent-flow") / "runtime" / "python" / "agent_flow" / "cli.py"

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
_SAFE_HOOK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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
    entries: tuple[tuple[str, str, str], ...]  # (event, matcher, managed script name)
    # 관리 디렉터리를 가리키지만 **정확한 호출이 아닌** 명령. `... || true`처럼
    # 후행 셸 구문이 붙으면 hook의 실패를 셸이 덮어 차단이 사라진다.
    malformed: tuple[str, ...] = ()

    def scripts(self) -> set[str]:
        return {script for _, _, script in self.entries}

    def placements(self, script: str) -> set[tuple[str, str]]:
        return {(event, matcher) for event, matcher, name in self.entries if name == script}


def find_install_root(start) -> Path | None:
    """이 checkout이 딛고 있는 설치본. leader를 먼저 묻고, 그다음 조상 탐색이다.

    순서가 중요하다. 조상 탐색을 먼저 하면 (1) `$HOME/.agent-flow/kit.json`이 있는
    사용자에게는 홈이 모든 저장소의 설치본을 가려 버리고, (2) 워커가 자기 checkout에
    `.agent-flow/kit.json`을 쓰면 그 파일이 자기 무결성 기준선이 된다. leader를 먼저
    확정하면 두 경로 모두 닫힌다 — leader의 git dir은 워커가 쓸 수 없다.
    """
    if start is None:
        return None
    current = Path(start)
    leader = leader_root_for(current)
    if leader is not None and _is_file(leader / KIT_JSON_RELATIVE):
        return leader
    for candidate in [current, *current.parents]:
        if _is_file(candidate / KIT_JSON_RELATIVE):
            return candidate
    return None


def assert_managed_hooks_registered(*roots) -> tuple[HookIntegrityReport, ...]:
    """런 시작 게이트. 강제 hook을 증명하지 못하면 시작 전에 멈춘다."""
    reports = verify_managed_hooks(*roots)
    if not reports:
        raise HookIntegrityError(
            "managed hook installation could not be found. Nothing was changed. "
            "Run `node bin/agent-flow-kit.mjs install --hooks` from the leader "
            "checkout before starting a run."
        )
    failed = [
        report
        for report in reports
        if not report.recorded or not report.expected_enabled or not report.ok
    ]
    if not failed:
        return reports
    detail = "; ".join(
        f"{report.root}: "
        + (
            ", ".join(report.violations)
            if report.violations
            else "managed enforcement hooks are not enabled"
        )
        for report in failed
    )
    raise HookIntegrityError(
        "managed hook registration does not match the enabled enforcement "
        "contract in .agent-flow/kit.json: "
        + detail
        + ". Nothing was changed. The enforcement hooks this run depends on "
        "cannot be proven active, so the run refuses to start. Re-run "
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
        violations.extend(_managed_hook_digest_violations(root, kit))
        violations.extend(_project_launcher_violations(root, kit))
        violations.extend(_misplaced_managed_hooks(surfaces))
    else:
        violations.extend(_unexpected_registrations(surfaces))
    violations.extend(_unapproved_registrations(surfaces))
    violations.extend(_malformed_registrations(surfaces))
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


def _managed_hook_digest_violations(
    root: Path, kit: dict
) -> Iterator[str]:
    expected = kit.get("managed_hook_digests")
    if not isinstance(expected, dict):
        yield "kit.json does not record managed hook content digests"
        return
    hook_dir = root / HOOK_DIR_RELATIVE
    for script in MANAGED_HOOK_SCRIPTS:
        recorded = expected.get(script)
        if not isinstance(recorded, str) or not re.fullmatch(
            r"[0-9a-f]{64}", recorded
        ):
            yield f"kit.json has no valid content digest for managed hook: {script}"
            continue
        path = hook_dir / script
        try:
            identity = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(identity.st_mode) or stat.S_ISLNK(identity.st_mode):
            yield f"managed hook script is not a regular owned file: {script}"
            continue
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            yield f"managed hook script cannot be read for digest verification: {script}"
            continue
        if actual != recorded:
            yield f"managed hook content digest does not match kit.json: {script}"


def _project_launcher_violations(root: Path, kit: dict) -> Iterator[str]:
    """승인 hook이 실행할 launcher를 hook과 **같은 기준**으로 대조한다.

    hook의 `_agent_flow_command()`도 같은 다섯 항목 + kit.json digest를 본다. 두
    기준이 갈라지면 게이트가 통과하는데 hook만 거부하거나(승인이 무음으로 무시),
    반대로 게이트가 거부한 launcher를 hook이 매 프롬프트마다 실행한다.

    launcher가 **실행될 수 있는지**도 같이 본다. digest만 맞고 관리 runtime이
    없으면 launcher는 exit 127이고, hook은 그 stderr를 버리므로 사용자에게는
    승인이 무시된 것으로만 보인다.
    """
    if "project_launcher_digest" not in kit:
        # 변조와 "이 설치본이 launcher 도입보다 오래됨"을 구분한다. 상위 문구가
        # "변조가 아님을 확인한 뒤 재설치"라고 경고하므로, 섞으면 업그레이드
        # 사용자를 변조 조사로 보낸다.
        yield (
            "this installation predates the managed launcher; re-run "
            "`node bin/agent-flow-kit.mjs install` from the leader checkout"
        )
        return
    recorded = kit["project_launcher_digest"]
    if recorded is None:
        # install이 launcher를 못 만든 상태다(PyYAML 인터프리터 없음). 여기서
        # "재설치하라"만 말하면 같은 결과가 반복되므로 원인 쪽을 가리킨다.
        yield (
            "install could not create the managed launcher; fix the interpreter it "
            "reported as `managed launcher not installed` (PyYAML is required) and "
            "re-run `node bin/agent-flow-kit.mjs install` from the leader checkout"
        )
        return
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        yield (
            "kit.json has no valid content digest for the managed launcher: "
            f"{PROJECT_LAUNCHER_RELATIVE}"
        )
        return
    path = root / PROJECT_LAUNCHER_RELATIVE
    try:
        identity = path.lstat()
    except OSError:
        yield (
            "managed launcher is missing, so chat SPEC confirmation cannot run: "
            f"{PROJECT_LAUNCHER_RELATIVE}"
        )
        return
    if not stat.S_ISREG(identity.st_mode):
        yield f"managed launcher is not a regular file: {PROJECT_LAUNCHER_RELATIVE}"
        return
    if identity.st_uid != os.getuid():
        yield f"managed launcher is not owned by the current user: {PROJECT_LAUNCHER_RELATIVE}"
    if identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        yield f"managed launcher is group or world writable: {PROJECT_LAUNCHER_RELATIVE}"
    if not os.access(path, os.X_OK):
        yield f"managed launcher is not executable: {PROJECT_LAUNCHER_RELATIVE}"
    if not _is_file(root / RUNTIME_CLI_RELATIVE):
        yield (
            "managed python runtime is missing, so the launcher cannot run the CLI: "
            f"{RUNTIME_CLI_RELATIVE}"
        )
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        yield (
            "managed launcher cannot be read for digest verification: "
            f"{PROJECT_LAUNCHER_RELATIVE}"
        )
        return
    if actual != recorded:
        yield f"managed launcher content digest does not match kit.json: {PROJECT_LAUNCHER_RELATIVE}"
    yield from _launcher_python_violations(kit)


def _launcher_python_violations(kit: dict) -> Iterator[str]:
    """launcher가 exec하는 인터프리터도 대조한다.

    launcher 바이트만 보면 그가 실행하는 대상은 무검증으로 남는다 — 그 자리를
    갈아끼우면 digest는 그대로 일치한 채 승인 capability를 넘겨받는다.
    """
    record = kit.get("project_launcher_python")
    if record is None and "project_launcher_python" in kit:
        # launcher를 못 만든 설치본이다. 그 사유는 digest 분기가 이미 말한다.
        return
    if not isinstance(record, dict):
        yield (
            "kit.json does not record the interpreter the managed launcher execs; "
            "re-run `node bin/agent-flow-kit.mjs install` from the leader checkout"
        )
        return
    recorded_path = record.get("path")
    recorded_digest = record.get("sha256")
    if not isinstance(recorded_path, str) or not recorded_path:
        yield "kit.json records no path for the managed launcher interpreter"
        return
    if not isinstance(recorded_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", recorded_digest
    ):
        yield f"kit.json records no valid digest for the managed launcher interpreter: {recorded_path}"
        return
    interpreter = Path(recorded_path)
    try:
        identity = interpreter.lstat()
    except OSError:
        yield f"the interpreter the managed launcher execs is missing: {recorded_path}"
        return
    # install은 realpath를 박으므로 여기 symlink가 보이면 그 자체가 교체 신호다.
    if not stat.S_ISREG(identity.st_mode):
        yield f"the managed launcher interpreter is not a regular file: {recorded_path}"
        return
    if not os.access(interpreter, os.X_OK):
        # launcher의 두 exit 127 분기 중 하나다. 게이트가 이걸 안 보면 hook은
        # 그 실패를 DEVNULL로 버려 사용자에게는 승인 무음으로만 보인다.
        yield f"the managed launcher interpreter is not executable: {recorded_path}"
    # 권한 비트는 이 자리에서 쓸 수 있는 신호가 아니다. 실측: GitHub runner의
    # `/opt/hostedtoolcache/Python/.../python3.12`는 world-writable이고 homebrew의
    # toolchain은 group-writable이다. 그걸 위반으로 보면 정당한 환경의 모든 run이
    # 시작 거부된다. 교체는 아래 digest가 잡는다 — 그게 이 검사의 오라클이다.
    try:
        actual = hashlib.sha256(interpreter.read_bytes()).hexdigest()
    except OSError:
        yield f"the managed launcher interpreter cannot be read for digest verification: {recorded_path}"
        return
    if actual != recorded_digest:
        # python 업그레이드도 여기로 온다. 그래서 문구가 재설치를 안내한다.
        yield (
            "the managed launcher interpreter changed since install: "
            f"{recorded_path}; re-run `node bin/agent-flow-kit.mjs install` from the leader checkout"
        )


def _unexpected_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for script in sorted(surface.scripts() & set(MANAGED_HOOK_SCRIPTS)):
            yield f"hooks are disabled in kit.json but {surface.name} still registers {script}"


def _unapproved_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for script in sorted(surface.scripts() - set(MANAGED_HOOK_SCRIPTS)):
            yield f"{surface.name} registers an unapproved managed-path hook: {script}"


def _misplaced_managed_hooks(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    """이벤트와 matcher가 계약과 같은가.

    이름 집합만 대조하면 `guard-protected-branch.sh`를 PostToolUse로 옮기는 것을
    못 본다 — 등록은 남고 hook은 명령이 *실행된 뒤* 불려 차단이 사라진다.
    matcher도 같다: `"Bash"`를 안 맞는 패턴으로 바꾸면 hook은 영영 안 불린다.
    """
    for surface in surfaces:
        if not surface.present or not surface.readable or not surface.entries:
            continue
        # OMP 확장은 생성된 소스라 이벤트/matcher가 JSON 필드가 아니다. 그 배치는
        # 두 installer의 확장 소스를 바이트 비교하는 parity가 지킨다.
        if all(event == "" for event, _, _ in surface.entries):
            continue
        for script, (event, matcher) in sorted(MANAGED_HOOK_PLACEMENT.items()):
            found = surface.placements(script)
            if not found or (event, matcher) in found:
                continue
            actual = ", ".join(f"{ev or '(no event)'}/{mt or '(no matcher)'}" for ev, mt in sorted(found))
            reason = (
                "observation hooks must stay off PreToolUse or a failing observer "
                "becomes a block decision"
                if script in OBSERVATIONAL_HOOK_SCRIPTS
                else "an enforcement hook off its event or matcher never blocks anything"
            )
            yield (
                f"{surface.name} registers {script} at {actual}, expected "
                f"{event}/{matcher or '(no matcher)'}; {reason}"
            )


def _malformed_registrations(surfaces: tuple[_Surface, ...]) -> Iterator[str]:
    for surface in surfaces:
        for command in sorted(set(surface.malformed)):
            yield (
                f"{surface.name} registers a managed hook path with extra shell syntax: "
                f"{command!r}; the shell would swallow the hook's exit code"
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
        entries, malformed = _json_registrations(root, payload)
        yield _Surface(
            str(relative),
            present=True,
            readable=True,
            entries=entries,
            malformed=malformed,
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
        entries=tuple(("", "", script) for script in MANAGED_HOOK_SCRIPTS if script in text),
    )


def _json_registrations(
    root: Path, payload: object
) -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return (), ()
    entries: list[tuple[str, str, str]] = []
    malformed: list[str] = []
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            matcher = block.get("matcher", "") if isinstance(block, dict) else ""
            for command in _entry_commands(block):
                script = managed_path_hook_name(root, command)
                if script is not None:
                    entries.append((str(event), str(matcher or ""), script))
                elif mentions_managed_hook_dir(root, command):
                    malformed.append(command)
    return tuple(entries), tuple(malformed)


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
    """Return the script name only for one exact trusted hook invocation."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(tokens) == 2 and tokens[0] == "/bin/bash":
        candidate = tokens[1]
        expected_suffix = ".sh"
    elif (
        len(tokens) == 3
        and tokens[:2] == ["/usr/bin/python3", "-I"]
    ):
        candidate = tokens[2]
        expected_suffix = ".py"
    else:
        return None
    normalized = candidate.replace("\\", "/")
    name = posixpath.basename(normalized)
    if (
        not name.endswith(expected_suffix)
        or not _SAFE_HOOK_NAME.fullmatch(name)
    ):
        return None
    try:
        parent = os.path.realpath(posixpath.dirname(normalized))
        expected = os.path.realpath(Path(root) / HOOK_DIR_RELATIVE)
    except OSError:
        return None
    return name if parent == expected else None


def mentions_managed_hook_dir(root: Path, command: str) -> bool:
    """정확한 호출은 아니지만 관리 hook 경로를 품고 있는가.

    이걸 조용히 무시하면 후행 셸 구문이 곧 무증거가 된다.
    """
    normalized = _unquote(command).replace("\\", "/")
    marker = f"/{HOOK_DIR_RELATIVE.as_posix()}/"
    index = normalized.rfind(marker)
    if index < 0:
        return False
    # 앞부분은 통짜 명령의 일부라 인용부호나 `sh -c` 같은 래퍼를 달고 있다.
    # 경로로 쓰인 마지막 토큰만 떼어 루트와 대조한다.
    head = normalized[:index].split()[-1] if normalized[:index].split() else ""
    try:
        return os.path.realpath(head.lstrip("'\"")) == os.path.realpath(root)
    except OSError:
        return False


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
