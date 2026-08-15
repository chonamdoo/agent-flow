from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agent_flow.core.atomic_io import atomic_write_text


CONTRACT_DIRS = ("sources", "tool_outputs", "scratch")
MAX_CONTEXT_CHARS = 12000
MAX_BRIEF_CHARS = 4000


def context_root(*, root: Path | None = None, run_dir: Path | None = None) -> Path:
    # run 안에서는 run_dir이 sink를 온전히 결정한다. `root`를 그때도 요구하면
    # 호출자가 쓰이지 않는 leader 경로를 들고 다니게 되고, run_dir이 빠지는 날
    # 관측이 조용히 leader에 쓰인다.
    if run_dir is not None:
        return run_dir / "context"
    if root is None:
        raise ValueError("context_root needs either root or run_dir")
    return root / ".agent-flow" / "context"


def ensure_context_contract(*, root: Path | None = None, run_dir: Path | None = None) -> Path:
    base = context_root(root=root, run_dir=run_dir)
    base.mkdir(parents=True, exist_ok=True)
    for name in CONTRACT_DIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    context_md = base / "context.md"
    if not context_md.exists():
        context_md.write_text("# Context\n\nLarge outputs stay in files; prompts reference paths only.\n", encoding="utf-8")
    events = base / "events.jsonl"
    if not events.exists():
        events.write_text("", encoding="utf-8")
    return base


def append_context_event(
    *,
    event: str,
    details: dict[str, object],
    root: Path | None = None,
    run_dir: Path | None = None,
) -> Path:
    base = ensure_context_contract(root=root, run_dir=run_dir)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "details": details,
    }
    events = base / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")
    return events


def offload_tool_output(
    *,
    name: str,
    content: str,
    root: Path | None = None,
    run_dir: Path | None = None,
    record_event: bool = True,
) -> Path:
    """큰 출력을 content-addressed 파일로 내리고 경로를 돌려준다.

    ``record_event=False``는 호출자가 같은 offload를 더 풍부한 event로 이미
    기록하는 경우다. 둘 다 적으면 trace가 같은 사실을 두 줄로 말하고, 읽는 쪽은
    어느 줄이 정본인지 알 수 없다.
    """
    base = ensure_context_contract(root=root, run_dir=run_dir)
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in name).strip("-") or "output"
    output_path = base / "tool_outputs" / f"{safe_name}-{digest[:12]}.txt"
    # content-addressed라 같은 내용이면 같은 파일이다. 제자리 쓰기 중에 죽으면
    # 그 주소가 반쪽 내용으로 굳어 이후 모든 참조가 조용히 틀린 것을 가리킨다.
    atomic_write_text(output_path, content)
    if record_event:
        append_context_event(
            root=root,
            run_dir=run_dir,
            event="tool_output_offloaded",
            details={
                # run_dir 기준 상대 경로만 남긴다. 절대 경로는 호스트 레이아웃을
                # artifact에 새겨 넣고, 그 artifact는 PR과 archive로 나간다.
                "path": run_relative_path(output_path, run_dir),
                "sha256": digest,
                "bytes": len(encoded),
            },
        )
    return output_path


def run_relative_path(path: Path, run_dir: Path | None) -> str:
    """run artifact에 남길 경로 표기. 호스트 절대 경로는 남기지 않는다.

    trace를 쓰는 쪽(`core.observation`)과 여기가 규칙을 각각 구현하면, 한쪽만
    고친 날 같은 파일의 두 event가 서로 다른 표기를 쓴다.
    """
    if run_dir is None:
        return path.name
    try:
        return path.relative_to(run_dir).as_posix()
    except ValueError:
        return path.name


def write_system_invariants(*, root: Path, invariants: list[str]) -> Path:
    path = root / ".agent-flow" / "system-invariants.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# System Invariants", ""]
    lines.extend(f"- {item}" for item in invariants)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def check_system_invariants(*, root: Path, run_dir: Path | None = None) -> list[str]:
    failures: list[str] = []
    base = ensure_context_contract(root=root, run_dir=run_dir)
    context_md = base / "context.md"
    if context_md.exists() and len(context_md.read_text(encoding="utf-8")) > MAX_CONTEXT_CHARS:
        failures.append("context.md exceeds length limit; move detail to sources/ or tool_outputs/ and reference paths")
    brief_md = base / "brief.md"
    if brief_md.exists() and len(brief_md.read_text(encoding="utf-8")) > MAX_BRIEF_CHARS:
        failures.append("brief.md exceeds length limit; move detail to files and reference paths")
    invariants = root / ".agent-flow" / "system-invariants.md"
    if invariants.exists():
        text = invariants.read_text(encoding="utf-8")
        for marker in ("status/next_command", "worktree", "path-only"):
            if marker not in text:
                failures.append(f"system-invariants.md missing marker: {marker}")
    return failures
