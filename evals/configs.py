"""컨텍스트를 agent에게 전달하는 방식들.

Vercel이 비교한 네 구성과 같은 축이다. 차이는 **전달 방식 하나뿐**이어야 한다 —
과제도 seed도 host도 같고, agent가 규범을 만나는 경로만 다르다. 그래야 점수 차를
전달 방식에 귀속시킬 수 있다.
"""
from __future__ import annotations

from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]
NORM_SKILLS = ("code-generation-discipline", "comment-authoring-discipline")

_RETRIEVAL_LINE = (
    "IMPORTANT: 아래 파일이 기억보다 우선한다. 변경 대상을 먼저 훑고, "
    "scope가 걸리는 것만 읽는다."
)


def _install_skills(project: Path) -> list[str]:
    """규범 skill을 프로젝트 안에 복사한다. 어느 구성이든 파일은 같은 자리에 있다."""
    root = project / ".agent-flow" / "skills"
    names = []
    for name in sorted(p.name for p in (KIT_ROOT / "skills").iterdir() if (p / "SKILL.md").is_file()):
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            (KIT_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        names.append(name)
    return names


def baseline(project: Path) -> None:
    """문서 없음. 모델의 사전 지식만."""
    return None


def skill_ondemand(project: Path) -> None:
    """skill은 디스크에 있고, AGENTS.md는 "필요하면 index를 읽어라"라고만 한다.

    이 저장소가 이번 변경 전까지 쓰던 방식이다. 읽을지 말지가 agent의 판단이다.
    """
    names = _install_skills(project)
    index = {"skills": [{"name": name, "path": f".agent-flow/skills/{name}/SKILL.md"} for name in names]}
    import json

    (project / ".agent-flow" / "skills" / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (project / "AGENTS.md").write_text(
        "# AGENTS.md\n\n"
        "- 프로젝트 skill은 `.agent-flow/skills/<name>/SKILL.md`에 있다.\n"
        "- 필요하면 `.agent-flow/skills/index.json` metadata를 보고 필요한 skill만 읽는다. "
        "모든 SKILL.md 전문을 항상 읽지 않는다.\n",
        encoding="utf-8",
    )


def agents_index(project: Path) -> None:
    """AGENTS.md가 압축 인덱스를 직접 들고 있다. 읽을지 말지의 판단이 없다."""
    names = _install_skills(project)
    passive = [name for name in names if name in NORM_SKILLS]
    on_demand = [name for name in names if name not in NORM_SKILLS]
    (project / "AGENTS.md").write_text(
        "# AGENTS.md\n\n"
        "```text\n"
        "[agent-flow skill index]|root: .agent-flow/skills\n"
        f"|{_RETRIEVAL_LINE}\n"
        f"|always:{{{','.join(passive)}}}\n"
        f"|on-demand:{{{','.join(on_demand)}}}\n"
        "```\n",
        encoding="utf-8",
    )


def agents_index_noisy(project: Path) -> None:
    """같은 인덱스에 쓰지 않는 이름 200개를 섞는다.

    Vercel은 안 쓰는 skill이 test pass를 63%에서 58%로 **떨어뜨리는** 것을 봤다.
    잡음 비용이 우리 환경에도 있는지를 이 구성이 잰다.
    """
    agents_index(project)
    text = (project / "AGENTS.md").read_text(encoding="utf-8")
    noise = ",".join(f"vendor-guide-{index:03d}" for index in range(200))
    (project / "AGENTS.md").write_text(text.replace("```\n", f"|on-demand:{{{noise}}}\n```\n", 1), encoding="utf-8")


CONFIGS = {
    "baseline": baseline,
    "skill-ondemand": skill_ondemand,
    "agents-index": agents_index,
    "agents-index-noisy": agents_index_noisy,
}
