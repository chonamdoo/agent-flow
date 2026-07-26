"""`no_narration` 기계 oracle.

`comment-authoring-discipline`의 첫 줄은 **"Default to adding no comments."**다.
이 과제에는 코드가 스스로 설명하지 못하는 제약이 없다 - 정렬 키 두 개와 잘라내기가
전부다. 그러므로 규범을 지킨 결과는 **새 주석 0줄**이다.

이건 모델의 기본 행동과 정면으로 어긋난다. 모델은 습관적으로 `# 점수 내림차순
정렬` 같은 서술 주석을 단다. 훈련 데이터에 없는 것을 재야 문서 접근이 실제로
효과를 내는지 보인다는 것이 Vercel eval의 요지였고, 임의 규약은 정의상 훈련
데이터 밖이다.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tokenize
from pathlib import Path

TARGET = "ranking.py"


def score(project: Path) -> dict[str, bool]:
    return {"behavior": _tests_pass(project), "norm": _added_no_narration(project)}


def _tests_pass(project: Path) -> bool:
    result = subprocess.run(
        (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="),
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0


def _added_no_narration(project: Path) -> bool:
    target = project / TARGET
    if not target.is_file():
        return False
    seed = Path(__file__).parent / "seed" / TARGET
    return not (_comments(target.read_text(encoding="utf-8")) - _comments(seed.read_text(encoding="utf-8")))


def _comments(source: str) -> set[str]:
    """`#` 주석만 센다.

    docstring은 규범이 명시적으로 남기라고 한 것이다 - "Python: keep public API
    docstrings". 그걸 서술 주석으로 세면 올바른 구현이 감점된다.

    tokenize를 쓰는 이유는 문자열 리터럴 안의 `#`를 주석으로 세지 않기 위해서다.
    """
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return {token.string.strip() for token in tokens if token.type == tokenize.COMMENT}
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
