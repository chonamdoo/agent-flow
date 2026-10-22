"""#427: C′는 full-feature의 절차 문장만 남기고 runner와의 대화를 지운 프롬프트다. 모델을 부르지 않는다."""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cprime():
    spec = importlib.util.spec_from_file_location("c_prime", ROOT / "evals/c_prime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("cprime") / "project"
    root.mkdir()
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["node", str(ROOT / "bin/agent-flow-kit.mjs"), "install", "--root", str(root), "--profile", "python",
                    "--architecture-mode", "clean", "--no-hooks"], cwd=root, check=True, capture_output=True)
    return root


def test_the_prompt_is_deterministic_and_leaves_the_project_untouched(cprime, project):
    before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*") if ".git/" not in path.as_posix())
    first = cprime.build(ROOT, project, "Add a hello endpoint")
    assert first == cprime.build(ROOT, project, "Add a hello endpoint")
    after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*") if ".git/" not in path.as_posix())
    assert before == after


def test_runner_talk_is_removed_and_paths_are_relative(cprime, project):
    prompt = cprime.build(ROOT, project, "Add a hello endpoint")
    assert str(project) not in prompt and "af-cprime" not in prompt
    for pattern in (r"agent-flow\s+(status|continue|run|start|run-command|gates)", r"--approve", r"next_command",
                    r"run_command_prefix", r"\*\*Run id\*\*", r"## When complete",
                    r"(?i)\brunner\b", r"record-command-run", r"blocks advancement"):
        assert not re.search(pattern, prompt), pattern


def test_phases_follow_full_feature_without_runner_owned_conditional_or_review_phases(cprime, project):
    phases = re.findall(r"(?m)^# Phase: (\S+)$", cprime.build(ROOT, project, "Add a hello endpoint"))
    assert phases[:3] == ["domain-grill", "product-brief", "prd"]
    assert not set(phases) & cprime.EXCLUDED_PHASES
    assert phases.index("red") < phases.index("green") < phases.index("commit")


def test_pauses_become_visible_self_approval_and_the_declaration_is_arm_common(cprime, project):
    prompt = cprime.build(ROOT, project, "Add a hello endpoint")
    assert prompt.count("record `approval: agent`") == 3  # domain-grill, prd, merge-approval
    assert prompt.rstrip().endswith("Nothing checks your phase artifacts for you in this run.")
    assert "EVAL-STATUS: complete" in prompt and "EVAL-STATUS: blocked: <reason>" in prompt


def test_repeated_sections_point_back_and_stop_after_cuts_the_tail(cprime, project):
    prompt = cprime.build(ROOT, project, "Add a hello endpoint")
    assert prompt.count("## Active profile: `python`\n\nthis is") == 0
    assert "(Same as in phase `domain-grill`.)" in prompt
    short = cprime.build(ROOT, project, "Add a hello endpoint", stop_after="commit")
    assert re.findall(r"(?m)^# Phase: (\S+)$", short)[-1] == "commit"
    with pytest.raises(ValueError, match="unknown phase"):
        cprime.build(ROOT, project, "Add a hello endpoint", stop_after="nope")
    with pytest.raises(ValueError, match="leaves out"):
        cprime.build(ROOT, project, "Add a hello endpoint", stop_after="gates")
