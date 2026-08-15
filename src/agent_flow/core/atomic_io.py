"""run 디렉터리 **밖**의 state-bearing 파일을 위한 원자적 writer.

run 안쪽은 `worktree_isolation.write_run_artifact_text`가 attestation까지 하고
쓴다. 그 attestation을 걸 수 없는 자리(프로젝트 handoff, eval 출력, active
marker)가 남아 있어서, 그쪽이 그냥 `write_text`로 쓰이면 중간에 죽었을 때 잘린
파일이 남는다. 잘린 상태 파일은 다음 실행이 "손상"이 아니라 "그런 상태"로 읽는다.

표준 라이브러리만 import한다. 이 모듈은 `worktree_isolation`을 포함해 어떤
agent_flow 모듈도 부르지 않는다 — 그래야 어느 층에서든 순환 없이 쓸 수 있다.
"""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

__all__ = ["atomic_write_text", "fsync_directory"]

# 신규 파일 생성 mode. 커널이 umask를 걸어 실제 권한을 정한다.
_CREATE_MODE = 0o666


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """``path``를 한 번의 ``os.replace``로 갈아 끼운다.

    부모 디렉터리도 fsync한다. 파일 데이터만 내려보내면 rename 자체가 디스크에
    닿지 않아, 전원이 끊긴 뒤 대상 경로가 통째로 사라져 있을 수 있다.
    실패하면 임시 파일을 지우고 기존 파일은 손대지 않는다.

    권한 규칙은 JS 쌍둥이(`lib/installer-shared.mjs`의 `atomicWriteFileSync`)와
    한 글자도 다르지 않다. 근거는 그쪽 주석에 있다.

    부모 디렉터리도 그쪽처럼 필요하면 만든다. parity를 믿고 부르는 호출부가
    한쪽에서만 ``FileNotFoundError``를 받으면, 그 호출부는 두 구현 중 어느 쪽이
    정본인지 모르는 채 자기 자리에 mkdir를 한 벌 더 적는다.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    preserved = _existing_mode(path)
    # `tempfile.mkstemp`를 쓰지 않는 이유는 그것이 0600을 강제하기 때문이다.
    # rename은 임시 파일의 mode를 그대로 target에 실어 보내므로, 그러면 기존
    # 파일의 권한이 매 쓰기마다 조용히 좁아지고 신규 파일은 umask보다 좁아진다.
    temporary = directory / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _CREATE_MODE)
    try:
        if preserved is not None:
            os.fchmod(fd, preserved)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
    fsync_directory(directory)


def _existing_mode(path: Path) -> int | None:
    """갈아 끼울 기존 **정규 파일**의 권한 비트. 그 밖이면 ``None``.

    symlink는 따라가지 않는다. `os.replace`가 갈아 끼우는 것은 링크 자신이라
    보존할 권한이 애초에 없고, 링크의 mode(대개 0777)를 새 파일에 심으면 아무도
    요구하지 않은 권한 확대가 된다.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_mode & 0o777 if stat.S_ISREG(info.st_mode) else None


def fsync_directory(directory: Path) -> None:
    """``directory`` 엔트리 변경을 디스크에 내려보낸다. 실패는 삼킨다.

    디렉터리 fsync는 이식성이 없다. Windows에는 디렉터리 fd 자체가 없고, 일부
    파일시스템은 EINVAL을 낸다. 여기서 실패해도 rename은 이미 끝났으므로
    내구성만 약해질 뿐 원자성은 유지된다 — 예외로 올리면 성공한 쓰기가 실패로 보인다.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        # close도 EIO/ENOSPC를 낸다. rename은 이미 끝났으므로 여기서 예외를 올리면
        # 성공한 쓰기가 실패로 보고되고 호출부가 무의미한 재시도를 한다.
        try:
            os.close(dir_fd)
        except OSError:
            pass
