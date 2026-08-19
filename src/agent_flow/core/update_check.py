"""새 릴리스가 나왔는지 하루 한 번만 확인해 알린다.

설치본 대조(`kit_digest`)와는 다른 축이다. 그쪽은 "이 프로젝트에 복사된 자산이 지금
kit과 같은가"를 보고, 이쪽은 "이 kit 자체가 최신 릴리스인가"를 본다. 둘을 한 경고로
합치면 재설치로 풀리는 문제와 업그레이드로 풀리는 문제가 같은 문장을 갖는다.

네트워크는 하루 한 번, 1.5초 상한으로만 건드린다. **실패도 캐시한다** — 실패를 캐시하지
않으면 네트워크가 막힌 환경에서 모든 명령이 상한만큼 멈춘다. 반대로 조회에 성공했던
값은 실패가 덮어쓰지 않는다. 덮어쓰면 이미 알던 새 릴리스가 하루 동안 사라진다.

이 경로는 조언이다. 그래서 무엇이 실패해도 사용자의 명령을 죽이지 않는다: 태그 문자열은
길이와 형태를 통과한 것만 받고(그러지 않으면 64 KB 응답이 `int()`를 넘어뜨린다), 경고를
내는 함수는 어떤 예외도 밖으로 내보내지 않는다.

`AGENT_FLOW_NO_UPDATE_CHECK=1`은 자동 확인만 끈다. `agent-flow update`는 사용자가 직접
물은 것이므로 그 스위치를 보지 않는다.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from agent_flow.core.atomic_io import atomic_write_text

# 릴리스 정본은 GitHub Releases다. `/releases/latest`는 draft와 prerelease를 빼고
# 최신 정식 릴리스만 돌려주므로, rc 태그가 사용자에게 업그레이드로 보이지 않는다.
LATEST_RELEASE_URL = "https://api.github.com/repos/chonamdoo/agent-flow/releases/latest"
CHECK_TTL_S = 86_400
FETCH_TIMEOUT_S = 1.5
DISABLE_ENV = "AGENT_FLOW_NO_UPDATE_CHECK"
DISTRIBUTION_NAME = "agent-flow"
# tap 이름을 붙여서 말한다. Homebrew 6.0은 3rd-party tap에 명시적 신뢰를 요구하고,
# 짧은 이름으로 치면 `brew trust`를 한 번 더 요구받는다.
TAP_FORMULA = "chonamdoo/agent-flow/agent-flow"
MAX_TAG_LENGTH = 64
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# 숫자 그룹을 9자리로 묶는다. 상한이 없으면 응답 본문이 그대로 `int()`에 들어가고,
# 4300자리를 넘는 순간 ValueError가 사용자의 명령을 죽인다.
#
# `(?![\d.])`가 숫자 코어를 고정한다. 없으면 접미사 그룹이 코어가 이미 먹은 자리까지
# 되짚어 삼킨다: `0.1.9rc1`이 `(0, 1)`로 읽혀 `0.1.5`가 업그레이드로 보인다.
# `\Z`는 `$`와 달리 끝의 개행을 인정하지 않는다 — 이 gate가 막으려는 것이 제어 문자다.
_TAG_HEAD = re.compile(
    r"^v?(\d{1,9}(?:\.\d{1,9}){0,3})(?![\d.])(?:[-+.][0-9A-Za-z.+-]{0,32})?\Z"
)


def cache_path(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    state_home = environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".agent-flow"
    # 상대 경로를 그대로 쓰면 캐시가 그때그때의 cwd(= 대개 프로젝트 체크아웃)에 생기고,
    # leader tripwire가 그것을 오염으로 보고한다.
    if not base.is_absolute():
        raise ValueError(f"XDG_STATE_HOME must be absolute: {base}")
    return base / "update-check.json"


def installed_version() -> str | None:
    """설치된 배포본의 버전. 버전 상수를 코드에 박지 않으려고 메타데이터를 읽는다."""
    try:
        return metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return None


def fetch_latest_release(
    *, url: str = LATEST_RELEASE_URL, timeout_s: float = FETCH_TIMEOUT_S
) -> str | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-flow-update-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            # 릴리스 하나의 메타데이터는 이 상한 안에 들어온다. 상한이 없으면 응답이
            # 무엇이든 전부 메모리로 읽는다.
            payload = json.loads(response.read(64_000).decode("utf-8"))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        # 릴리스가 아직 없으면 404다. 그것은 오류가 아니라 "알릴 것이 없음"이다.
        # 잘린 응답(IncompleteRead)은 URLError도 OSError도 아니므로 따로 적는다.
        return None
    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag_name")
    return _accepted_tag(tag.strip()) if isinstance(tag, str) else None


def latest_release(
    *,
    force: bool = False,
    now: float | None = None,
    fetch: Callable[[], str | None] | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    environ = os.environ if env is None else env
    if not force and environ.get(DISABLE_ENV, "").strip().lower() in _TRUTHY:
        return None
    moment = time.time() if now is None else now
    path = cache_path(environ)
    cached = _read_cache(path)
    if not force and cached is not None and moment - cached[0] < CHECK_TTL_S:
        return cached[1] or None
    fetched = _accepted_tag((fetch or fetch_latest_release)())
    if fetched is None and cached is not None and cached[1]:
        # 조회 실패가 이미 알던 릴리스를 지우지 않게 한다. 시각만 새로 찍어 다음
        # 시도를 TTL 뒤로 미룬다.
        _write_cache(path, checked_at=moment, latest=cached[1])
        return cached[1]
    _write_cache(path, checked_at=moment, latest=fetched)
    return fetched


def update_available(installed: str | None, latest: str | None) -> bool:
    left = _version_tuple(installed)
    right = _version_tuple(latest)
    if left is None or right is None:
        return False
    size = max(len(left), len(right))
    return _padded(right, size) > _padded(left, size)


def is_homebrew_install(kit_root: Path) -> bool:
    return homebrew_cellar_version(kit_root) is not None


def homebrew_cellar_version(kit_root: Path) -> str | None:
    """kit이 선 Homebrew keg의 버전 디렉터리 이름. Cellar 밖이면 ``None``.

    `HOMEBREW_PREFIX`는 빌드·테스트 중에만 보장되는 값이므로 실행 시점에 읽지 않는다.
    설치 자리는 `<prefix>/Cellar/agent-flow/<version>/libexec` 하나뿐이라 경로로 읽는다.
    HEAD 빌드는 그 자리에 `HEAD-<sha>`가 오고, 그 구분이 업그레이드 명령을 가른다.
    """
    try:
        parts = kit_root.resolve().parts
    except OSError:
        return None
    for index in range(len(parts) - 2):
        if parts[index] == "Cellar" and parts[index + 1] == DISTRIBUTION_NAME:
            return parts[index + 2]
    return None


def upgrade_command(kit_root: Path) -> str:
    version = homebrew_cellar_version(kit_root)
    if version is None:
        return f"git -C {shlex.quote(str(kit_root))} pull"
    # HEAD 설치본에는 `--fetch-HEAD`가 필요하다. 그 플래그가 없으면 `brew upgrade`는
    # upstream commit을 아예 보지 않고 "최신"이라고 답한다 — 사용자는 올린 줄 안다.
    fetch_head = "--fetch-HEAD " if version.startswith("HEAD") else ""
    return f"brew upgrade {fetch_head}{TAP_FORMULA}"


def warn_if_update_available(
    kit_root: Path,
    *,
    fetch: Callable[[], str | None] | None = None,
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> None:
    try:
        installed = installed_version()
        if installed is None:
            # 설치 메타데이터가 없으면 무엇과 비교할지 알 수 없다. 추측해서 알리지 않는다.
            return
        latest = latest_release(fetch=fetch, now=now, env=env)
        if not update_available(installed, latest):
            return
        print(
            f"agent-flow {latest} is available (installed {installed}); "
            f"upgrade: {upgrade_command(kit_root)}",
            file=sys.stderr,
        )
    except Exception:
        # 이 경로는 조언이다. 조언이 실패하면 조용해야 한다 — 여기서 예외가 새면
        # 사용자가 실제로 치려던 명령이 알림 때문에 죽는다.
        return


def print_update_report(
    kit_root: Path,
    *,
    fetch: Callable[[], str | None] | None = None,
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> int:
    installed = installed_version()
    try:
        latest = latest_release(force=True, fetch=fetch, now=now, env=env)
    except (ValueError, RuntimeError) as error:
        # 캐시 자리를 정할 수 없는 상태(상대 `XDG_STATE_HOME`, HOME 없는 환경)다.
        # 직접 물은 명령이 traceback으로 끝나지 않게, 아는 것까지 말하고 사유를 남긴다.
        print(f"the update cache is unusable: {error}", file=sys.stderr)
        latest = None
    print(f"installed: {installed or 'unknown'}")
    print(f"latest: {latest or 'unknown'}")
    if latest is None:
        print(
            "no release was readable: the repository has none published yet, or "
            "github.com was unreachable"
        )
        return 0
    if update_available(installed, latest):
        print(f"upgrade: {upgrade_command(kit_root)}")
        return 0
    print("up to date")
    return 0


def _accepted_tag(value: str | None) -> str | None:
    """읽을 수 있는 태그만 통과시킨다.

    네트워크와 캐시 양쪽의 입구다. 여기서 좁히지 않으면 응답 본문 전체가 버전 비교와
    화면 출력에 그대로 흘러간다 — 제어 문자를 담은 문자열이 신뢰받는 CLI 메시지 안에
    끼어들고, 자릿수 없는 숫자가 `int()`를 넘어뜨린다.
    """
    if not value or len(value) > MAX_TAG_LENGTH:
        return None
    return value if _TAG_HEAD.match(value) else None


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    accepted = _accepted_tag(text.strip() if text else None)
    if accepted is None:
        return None
    match = _TAG_HEAD.match(accepted)
    assert match is not None  # _accepted_tag가 이미 같은 패턴으로 판정했다
    return tuple(int(part) for part in match.group(1).split("."))


def _padded(value: tuple[int, ...], size: int) -> tuple[int, ...]:
    return value + (0,) * (size - len(value))


def _read_cache(path: Path) -> tuple[float, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_at = payload.get("checked_at")
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return None
    # `json.loads`는 `Infinity`를 float으로 받는다. 그대로 두면 TTL 비교가 영원히
    # 참이 되어 캐시가 다시 갱신되지 않는다.
    if not math.isfinite(checked_at):
        return None
    latest = payload.get("latest")
    return float(checked_at), _accepted_tag(latest) or ""


def _write_cache(path: Path, *, checked_at: float, latest: str | None) -> None:
    payload = json.dumps({"checked_at": checked_at, "latest": latest or ""})
    try:
        # 손으로 임시 파일을 만들지 않는다. 이 저장소의 writer는 O_EXCL로 열고
        # 심링크를 따라가지 않는다 — 예측 가능한 이름의 임시 파일을 링크로 미리
        # 심어 두는 경로가 그래서 막힌다.
        atomic_write_text(path, payload)
    except OSError:
        # 캐시를 못 쓰는 것으로 사용자의 명령을 막지 않는다.
        return
