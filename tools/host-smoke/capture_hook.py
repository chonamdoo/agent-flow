"""host-smoke 캡처 hook. stdin payload를 그대로 파일 하나에 남기고 항상 통과한다.

    capture_hook.py <capture dir> <event name>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


def main() -> int:
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{sys.argv[2]}-{time.time_ns()}.json").write_text(
        sys.stdin.read(), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
