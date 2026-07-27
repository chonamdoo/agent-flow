#!/usr/bin/env python3
"""`preferredPython()` 후보로 위장해 -m 뒤의 argv를 되돌려주는 stub 인터프리터.

어떤 명령이 Python CLI에 닿았는지는 wrapper 바깥에서 관측할 수 없다. 진짜
python을 쓰면 그 명령이 실제로 실행돼 버려서 "닿았다"와 "성공했다"가 섞인다.
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
    print(f"argv-received: {' '.join(args)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
