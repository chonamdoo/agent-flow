#!/usr/bin/env python3
"""`preferredPython()` 후보로 위장하는 stub 인터프리터.

wrapper가 stdin을 자식에게 그대로 넘기는지만 본다. 진짜 python으로는 이걸 볼 수
없다 — 승인 입력을 받는 코드가 TTY를 요구해서 파이프로는 그 앞에서 멈춘다.
"""
from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    # 후보 검증 절차를 그대로 통과시킨다. 하나라도 어긋나면 다음 후보로 넘어간다.
    if args == ["--version"]:
        print("Python 3.12.0")
        return 0
    if args[:2] == ["-c", "import yaml"]:
        return 0
    line = sys.stdin.readline().strip()
    print(f"stdin-received: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
