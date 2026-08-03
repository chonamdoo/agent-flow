"""설치본이 어느 kit source에서 왔는지 대조한다.

`bin/agent-flow-kit.mjs`의 `kitSourceDigest()`와 **같은 값**을 내야 한다. 문서가
안내하는 `agent-flow run/status/continue`는 Python 진입점으로 직접 들어오므로,
JS 래퍼에만 검사가 있으면 일반적인 사용자는 낡은 설치본을 끝까지 못 본다.
두 구현이 갈라지면 한쪽만 경고하므로 `scripts/check-agent-flow-parity.mjs`가
같은 kit root에 대해 두 값을 대조한다.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# install이 target으로 복사하는 자산. host 경로는 installer가 kit에서 **읽는** 자리만
# 넣는다 — `.Codex/hooks.json`, `.claude/settings.json`, `.Codex/rules/concise-output.md`는
# installer가 target에 만들어 내는 산출물이라 넣으면 self-install 직후부터 자산 변경
# 없이 지문이 흔들린다.
KIT_SOURCE_DIGEST_ROOTS: tuple[str, ...] = (
    "templates",
    "bootstrap",
    "skills",
    "scripts",
    "src/agent_flow",
    "bin",
    "lib",
    ".Codex/agents",
    ".Codex/rules/context",
    ".Codex/rules/codebase-rubric.md",
    ".Codex/context",
    ".claude/agents",
)

# 파생 산출물은 소스가 아니다. `.pyc`는 헤더에 소스 mtime을 담는데 git은 mtime을
# 보존하지 않으므로, 넣으면 같은 커밋의 두 체크아웃이 다른 지문을 낸다.
DIGEST_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".agent-flow",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)


def kit_source_digest(kit_root: Path) -> str:
    digest = hashlib.sha256()
    for relative_root in KIT_SOURCE_DIGEST_ROOTS:
        for path in _walk_files_sorted(kit_root.joinpath(*relative_root.split("/"))):
            digest.update(path.relative_to(kit_root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(
                hashlib.sha256(path.read_bytes()).hexdigest().encode("utf-8")
            )
            digest.update(b"\0")
    return digest.hexdigest()


def _walk_files_sorted(target: Path) -> list[Path]:
    if not target.exists():
        return []
    # digest root는 디렉터리일 수도 단일 파일일 수도 있다.
    if not target.is_dir():
        return [target]
    files: list[Path] = []
    for entry in sorted(target.iterdir(), key=lambda path: path.name):
        if entry.name in DIGEST_EXCLUDED_NAMES or entry.name.endswith(".pyc"):
            continue
        if entry.is_dir():
            files.extend(_walk_files_sorted(entry))
        elif entry.is_file():
            files.append(entry)
    return files


def warn_if_installed_kit_is_stale(root: Path, kit_root: Path) -> None:
    """낡은 설치본은 조용히 옛 workflow/profile/skill/runtime을 돌린다.

    `skills sync`는 이것을 고치지 않는다 — 그 명령은 외부 skill_sources만 fetch한다.
    """
    if not (
        (kit_root / "pyproject.toml").is_file()
        and (kit_root / "bin" / "agent-flow-kit.mjs").is_file()
    ):
        # 프로젝트·global runtime의 축소 패키지는 source kit과 디렉터리 구성이
        # 다르다. 이 경로를 source로 해시하면 install 직후에도 반드시 stale이 된다.
        return
    try:
        payload = json.loads(
            (root / ".agent-flow" / "kit.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return
    recorded = payload.get("kit_source_digest") if isinstance(payload, dict) else None
    if not isinstance(recorded, str) or not recorded:
        # 이 지문을 기록하기 전에 설치된 프로젝트. 대조 기준이 없으므로 판정하지 않는다.
        return
    try:
        current = kit_source_digest(kit_root)
    except OSError:
        # 지문을 못 읽는다고 사용자의 명령을 막지는 않는다.
        return
    if recorded == current:
        return
    print(
        "warning: the installed agent-flow assets are older than this kit "
        "(workflows, profiles, skills, or the Python runtime changed). "
        "run: agent-flow-kit install",
        file=sys.stderr,
    )
