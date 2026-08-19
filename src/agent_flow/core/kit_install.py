"""`agent-flow <path>`가 프로젝트 설치를 실행하는 경로.

설치의 정본은 `bin/agent-flow-kit.mjs`다. 자산 복사·hook 등록·bootstrap 블록을 Python에
다시 구현하면 진입점마다 다른 설치본이 생기고, 둘이 같은지 보는 검사가 또 필요해진다.
그래서 여기서는 인수만 옮겨 그 스크립트를 그대로 부른다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

INSTALLER_RELATIVE = ("bin", "agent-flow-kit.mjs")


def run_project_install(
    *, kit_root: Path, target: Path, extra_args: Sequence[str] = ()
) -> int:
    script = kit_root.joinpath(*INSTALLER_RELATIVE)
    if not script.is_file():
        print(f"installer is missing from the kit: {script}", file=sys.stderr)
        return 2
    if not target.is_dir():
        print(
            f"install target is not an existing directory: {target}",
            file=sys.stderr,
        )
        return 2
    refusal = _refused_target(target)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
    node = _verified_node(target)
    if node is None:
        return 2
    completed = subprocess.run(
        (node, str(script), "install", "--root", str(target), *extra_args),
        check=False,
    )
    return completed.returncode


def _refused_target(target: Path) -> str | None:
    """설치가 프로젝트 자리가 아닌 곳으로 가는 두 경우를 막는다.

    install은 프로젝트 파일을 쓴다 — `AGENTS.md`, `.claude/settings.json`, hook 등록.
    홈이나 파일시스템 루트에 그것을 풀면 사용자의 전역 host 설정을 프로젝트 설정으로
    덮어쓰고, 되돌리는 일은 전부 사용자 몫이 된다. 경로 하나로 설치가 끝나는 지름길이
    있으니, 그 오타의 대가가 가장 큰 두 자리는 이름으로 거절한다.
    """
    if target.parent == target:
        return f"refusing to install into the filesystem root: {target}"
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    if target == home:
        return (
            f"refusing to install into the home directory: {target}. "
            "Pass a project directory."
        )
    return None


def _verified_node(target: Path) -> str | None:
    """PATH에서 찾은 node를 그대로 믿지 않는다.

    `providers/subprocess.py`가 provider 실행 파일에 같은 검사를 한다: PATH의 빈 항목이나
    `.`은 상대 경로를 만들고, 그 상대 경로는 지금 설치하려는 디렉터리 안의 `node`로
    풀릴 수 있다. 그 디렉터리는 agent가 쓰는 자리다.
    """
    candidate = shutil.which("node")
    if candidate is None:
        print(
            "node is required to install the project assets; install Node.js "
            "(brew install node) and run the command again",
            file=sys.stderr,
        )
        return None
    resolved = Path(candidate)
    if not resolved.is_absolute():
        print(f"refusing to run a node resolved by a relative PATH entry: {candidate}", file=sys.stderr)
        return None
    try:
        real = resolved.resolve()
        inside_target = real.is_relative_to(target.resolve())
    except OSError:
        return str(resolved)
    if inside_target:
        print(
            f"refusing to run the node found inside the install target: {real}",
            file=sys.stderr,
        )
        return None
    return str(resolved)
