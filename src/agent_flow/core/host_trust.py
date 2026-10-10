"""host가 이 checkout의 hook 실행을 허락했는지 host 소유 설정에서 읽는다.

읽기만 한다. installer는 Codex project trust를 걷어내기만 하고 심지 않는다(승인
세탁 방지). 진단이 host 설정을 쓰면 그 금지선을 다른 입구로 넘는다.

등록과 신뢰는 서로를 증명하지 않는다. Codex는 project 신뢰와 hook별 신뢰가 모두
있어야 project hook을 실행하고, 신뢰가 없으면 등록된 hook을 말없이 건너뛴다. hook별
신뢰는 그 hook이 꺼져 있지 않고 기록된 hash가 현재 등록의 hash와 같을 때만 유효하다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from agent_flow.core.atomic_io import read_bounded_regular_file

TrustState = Literal["yes", "no", "unknown", "n/a"]

CODEX_TRUST_HINT = (
    "Codex에서 이 폴더를 신뢰하고 /hooks에서 agent-flow 훅을 승인한 뒤 세션을 다시 시작하세요"
)
CODEX_DISABLED_HINT = "Codex /hooks에서 꺼진 agent-flow 훅을 켜고 세션을 다시 시작하세요"
CODEX_HASH_HINT = (
    "Codex /hooks에서 agent-flow 훅이 모두 trusted인지 확인하세요(modified면 다시 승인하세요)"
)
CLAUDE_TRUST_HINT = (
    "interactive Claude 세션은 이 폴더의 신뢰 대화를 수락해야 hook을 실행합니다"
    "(-p 실행은 신뢰로 취급됩니다)"
)
CLAUDE_DISABLED_HINT = "disableAllHooks를 끄고 세션을 다시 시작하세요"
OMP_DISABLED_HINT = "disabledExtensions에서 agent-flow-hooks를 빼고 세션을 다시 시작하세요"

_UNREADABLE = object()
# `~/.claude.json`은 project 기록이 쌓여 커진다. 넉넉히 잡되 끝없이 읽지는 않는다.
_CONFIG_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class HostTrust:
    state: TrustState
    source: str
    detail: str
    hint: str = ""


def read_host_trust(
    host: str, *, home: Path, checkout: Path, project_root: Path, env: Mapping[str, str]
) -> HostTrust:
    """`project_root`는 checkout이 linked worktree일 때 그 main checkout(leader)이다."""
    reader = _READERS.get(host)
    if reader is None:
        return HostTrust("unknown", "", f"unsupported host: {host}")
    return reader(home, checkout, project_root, env)


def _claude_trust(
    home: Path, checkout: Path, project_root: Path, env: Mapping[str, str]
) -> HostTrust:
    config_dir = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else None
    for path in (
        (config_dir or home / ".claude") / "settings.json",
        checkout / ".claude" / "settings.json",
        checkout / ".claude" / "settings.local.json",
    ):
        settings = _read_json(path)
        if settings is _UNREADABLE:
            return HostTrust("unknown", str(path), "cannot parse the settings file")
        if isinstance(settings, dict) and settings.get("disableAllHooks") is True:
            return HostTrust("no", str(path), "disableAllHooks is true", CLAUDE_DISABLED_HINT)
    state_file = (config_dir / ".claude.json") if config_dir else home / ".claude.json"
    state = _read_json(state_file)
    if state is _UNREADABLE:
        return HostTrust("unknown", str(state_file), "cannot parse the Claude state file")
    projects = state.get("projects") if isinstance(state, dict) else None
    entries = projects if isinstance(projects, dict) else {}
    accepted = any(
        isinstance(entries.get(key), dict)
        and entries[key].get("hasTrustDialogAccepted") is True
        for key in _path_keys(checkout)
    )
    if accepted:
        return HostTrust("yes", str(state_file), "workspace trust accepted")
    # `-p` 실행은 신뢰 대화 없이 hook을 돌린다. 기록이 없다는 것만으로는 hook이 안 돈다고 말할 수 없다.
    return HostTrust(
        "unknown",
        str(state_file),
        "workspace trust is not recorded for this checkout",
        CLAUDE_TRUST_HINT,
    )


def _codex_trust(
    home: Path, checkout: Path, project_root: Path, env: Mapping[str, str]
) -> HostTrust:
    codex_home = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
    config_path = codex_home / "config.toml"
    # 3.11 표준 모듈이다. 진단 하나 때문에 `agent_flow.cli` import가 실패하면 안 된다.
    try:
        import tomllib
    except ModuleNotFoundError:
        return HostTrust("unknown", str(config_path), "this Python has no TOML reader")
    text = _read_text(config_path)
    if text is _UNREADABLE:
        return HostTrust("unknown", str(config_path), "cannot read the Codex config")
    try:
        config = tomllib.loads(text) if isinstance(text, str) else {}
    except tomllib.TOMLDecodeError:
        return HostTrust("unknown", str(config_path), "cannot parse the Codex config")
    projects = config.get("projects")
    projects = projects if isinstance(projects, dict) else {}
    # Codex는 linked worktree의 project 신뢰를 main checkout에서 찾는다
    # (codex-rs `resolve_root_git_project_for_trust`). hook 신뢰는 hooks.json 경로마다 따로다.
    if not any(
        isinstance(projects.get(key), dict) and projects[key].get("trust_level") == "trusted"
        for key in (*_path_keys(checkout), *_path_keys(project_root))
    ):
        return HostTrust(
            "no",
            str(config_path),
            "project is not trusted for this checkout or its main checkout",
            CODEX_TRUST_HINT,
        )
    hooks_path = checkout / ".codex" / "hooks.json"
    handlers = _codex_handler_ids(hooks_path)
    if handlers is None:
        return HostTrust("unknown", str(hooks_path), "cannot read the Codex hook registration")
    hooks = config.get("hooks")
    states = hooks.get("state") if isinstance(hooks, dict) else None
    states = states if isinstance(states, dict) else {}
    records = {
        handler: [
            states[key]
            for key in (f"{prefix}:{handler}" for prefix in _path_keys(hooks_path))
            if isinstance(states.get(key), dict)
        ]
        for handler in handlers
    }
    if any(
        "trusted_hash" in record
        and (not isinstance(record["trusted_hash"], str) or not record["trusted_hash"])
        for found in records.values()
        for record in found
    ):
        return HostTrust("unknown", str(config_path), "a Codex hook trust record is malformed")
    # `/hooks`에서 끈 hook은 신뢰 hash가 남아 있어도 실행되지 않는다.
    disabled = [
        handler
        for handler, found in records.items()
        if any(record.get("enabled") is False for record in found)
    ]
    if disabled:
        return HostTrust(
            "no",
            str(config_path),
            f"{len(disabled)} of {len(handlers)} hooks are disabled",
            CODEX_DISABLED_HINT,
        )
    untrusted = [
        handler
        for handler, found in records.items()
        if not any("trusted_hash" in record for record in found)
    ]
    if untrusted:
        return HostTrust(
            "no",
            str(config_path),
            f"{len(untrusted)} of {len(handlers)} hooks are not trusted",
            CODEX_TRUST_HINT,
        )
    # Codex는 기록된 hash가 현재 등록의 hash와 다르면 수정된 hook으로 보고 건너뛴다. hash는
    # Codex 내부 직렬화로 계산하므로 같은지 알 수 없다. 그래서 기록이 다 있어도 `yes`가 아니다.
    return HostTrust(
        "unknown",
        str(config_path),
        "project and hook trust recorded; the Codex hook hash is not verified",
        CODEX_HASH_HINT,
    )


def _omp_trust(
    home: Path, checkout: Path, project_root: Path, env: Mapping[str, str]
) -> HostTrust:
    path = home / ".omp" / "agent" / "config.yml"
    text = _read_text(path)
    if text is _UNREADABLE:
        return HostTrust("unknown", str(path), "cannot read the OMP config")
    try:
        config = yaml.safe_load(text) if isinstance(text, str) else None
    except yaml.YAMLError:
        return HostTrust("unknown", str(path), "cannot parse the OMP config")
    disabled = config.get("disabledExtensions") if isinstance(config, dict) else None
    if isinstance(disabled, list) and any("agent-flow-hooks" in str(item) for item in disabled):
        return HostTrust("no", str(path), "agent-flow-hooks is disabled", OMP_DISABLED_HINT)
    return HostTrust("n/a", str(path), "OMP has no per-project hook trust")


def _codex_handler_ids(hooks_path: Path) -> list[str] | None:
    """Codex hook 신뢰 키의 `<event>:<group>:<handler>` 부분. 읽지 못하면 None이다."""
    payload = _read_json(hooks_path)
    if payload is None or payload is _UNREADABLE or not isinstance(payload, dict):
        return None
    events = payload.get("hooks")
    if not isinstance(events, dict):
        return None
    handlers: list[str] = []
    for event, groups in events.items():
        if not isinstance(groups, list):
            continue
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", str(event)).lower()
        for group_index, group in enumerate(groups):
            entries = group.get("hooks") if isinstance(group, dict) else None
            for handler_index, _ in enumerate(entries if isinstance(entries, list) else ()):
                handlers.append(f"{snake}:{group_index}:{handler_index}")
    return handlers


def _read_json(path: Path) -> object:
    """없으면 None, 읽거나 해석하지 못하면 `_UNREADABLE`이다."""
    text = _read_text(path)
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return _UNREADABLE


def _read_text(path: Path) -> object:
    """없으면 None, 일반 파일이 아니거나 읽지 못하면 `_UNREADABLE`, 아니면 본문이다.

    status는 매번 세 host의 설정과 checkout 등록 파일을 연다. 등록 파일은 agent가 바꿀 수
    있다. FIFO나 장치 파일을 열면 status가 멈추고, 진단 경계의 예외 처리는 멈춘 호출을 잡지
    못한다. 이름으로 확인한 뒤 다시 이름으로 열면 그 사이에 바뀔 수 있으므로, 비차단으로 연
    descriptor의 종류를 본다. host는 설정 symlink를 따라가므로 여기서도 따라간다.
    """
    try:
        raw, _ = read_bounded_regular_file(
            path, max_bytes=_CONFIG_MAX_BYTES, follow_symlinks=True
        )
        return raw.decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return _UNREADABLE


def _path_keys(path: Path) -> tuple[str, ...]:
    """host는 자기가 본 경로 문자열로 기록한다. symlink 경로와 실경로를 모두 본다."""
    keys = [str(path)]
    try:
        resolved = str(path.resolve())
    except OSError:
        return tuple(keys)
    if resolved not in keys:
        keys.append(resolved)
    return tuple(keys)


_READERS: dict[str, Callable[[Path, Path, Path, Mapping[str, str]], HostTrust]] = {
    "claude": _claude_trust,
    "codex": _codex_trust,
    "omp": _omp_trust,
}
