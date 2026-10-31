from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from agent_flow.core.command_evidence import (
    COMMANDS_RUN_LOG,
    is_test_command_execution,
    missing_test_evidence_markers,
    read_command_evidence,
    read_red_reference,
)
from agent_flow.core.design_ledger import capture_design_ledger
from agent_flow.core.design_value_check import missing_spec_item_evidence

SELECTOR = "com.stockchat.conversation.application.RememberedPeopleTest"
PROFILE_TOKENS = ("./gradlew", "test")


def _wrapper(tail, *, scope="targeted", question="Does the selected regression execute?"):
    return shlex.join([
        "python3", str(Path.home() / ".agents/skills/gradle-run/scripts/gradle_run.py"),
        "run", "--workflow", "a1" * 16, "--scope", scope, "--question", question,
        "--", *tail,
    ])


@pytest.mark.parametrize("scope", ["targeted", "broad"])
@pytest.mark.parametrize("task", ["test", ":test", ":application:test"])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("profile_options", [(), ("--console", "plain"), ("--tests", SELECTOR)])
def test_supported_wrapper_matches_module_test_and_exact_selector(scope, task, wrapped, profile_options):
    tail = ["./gradlew", task, "--tests", SELECTOR, "--no-scan", "--console=plain"]
    command = _wrapper(tail, scope=scope) if wrapped else shlex.join(tail)
    assert is_test_command_execution(command, (*PROFILE_TOKENS, *profile_options), SELECTOR)
    assert is_test_command_execution(command, ("./gradlew", task, *profile_options), SELECTOR)


@pytest.mark.parametrize("mutation", [
    "local", "foreign", "suffix", "python", "python-option", "module", "code",
    "script", "nested", "create", "finish", "help", "version", "unknown",
    "duplicate", "missing", "blank", "workflow", "separator", "tail", "mask",
    "composition", "substitution", "env-option",
])
def test_wrapper_identity_and_grammar_fail_closed(mutation):
    argv = shlex.split(_wrapper(["./gradlew", ":application:test", "--tests", SELECTOR]))
    if mutation in {"local", "foreign", "suffix", "script"}:
        argv[1] = {"local": "gradle_run.py", "foreign": "/tmp/gradle_run.py",
                   "suffix": argv[1] + ".bak", "script": "/tmp/other.py"}[mutation]
    elif mutation == "python":
        argv[0] = "python"
    elif mutation in {"python-option", "module", "code"}:
        argv.insert(1, {"python-option": "-I", "module": "-m", "code": "-c"}[mutation])
    elif mutation == "nested":
        argv[10:10] = ["python3", argv[1], "run"]
    elif mutation in {"create", "finish", "help", "version"}:
        argv[2] = mutation
    elif mutation == "unknown":
        argv[3:3] = ["--root", "/tmp"]
    elif mutation == "duplicate":
        argv[3:3] = ["--scope", "targeted"]
    elif mutation == "missing":
        del argv[6]
    elif mutation == "blank":
        argv[8] = "   "
    elif mutation == "workflow":
        argv[4] = "not-an-opaque-id"
    elif mutation == "separator":
        del argv[9]
    elif mutation == "tail":
        del argv[10:]
    command = shlex.join(argv)
    if mutation == "mask":
        command += " || true"
    elif mutation == "composition":
        command += " ; echo done"
    elif mutation == "substitution":
        command += " $(echo test)"
    elif mutation == "env-option":
        command = "env --unknown " + command
    assert not is_test_command_execution(command, PROFILE_TOKENS, SELECTOR)


@pytest.mark.parametrize("tail,expected", [
    (["assembleDebug"], PROFILE_TOKENS),
    (["testClasses"], PROFILE_TOKENS),
    (["testDebugUnitTest"], PROFILE_TOKENS),
    (["contest"], PROFILE_TOKENS),
    ([":application:Test"], PROFILE_TOKENS),
    ([":other:test"], ("./gradlew", ":application:test")),
    (["assembleDebug", "--tests", "test"], PROFILE_TOKENS),
    (["assembleDebug", "--console", "test"], PROFILE_TOKENS),
    (["assembleDebug", "-Ptask=test"], PROFILE_TOKENS),
    (["assembleDebug", "--project-dir", "test"], PROFILE_TOKENS),
    (["test", "--dry-run"], PROFILE_TOKENS),
    (["test", "-m"], PROFILE_TOKENS),
    (["test", "-x", "test"], PROFILE_TOKENS),
    (["test", "--exclude-task=test"], PROFILE_TOKENS),
    (["test", "--continue"], PROFILE_TOKENS),
    (["test", "--help"], PROFILE_TOKENS),
    (["tasks"], PROFILE_TOKENS),
    (["test", "--unknown"], PROFILE_TOKENS),
    (["test", "--console"], PROFILE_TOKENS),
    ([":application:test", ":other:test"], ("./gradlew", ":application:test")),
    (["--tests", SELECTOR, ":application:test"], PROFILE_TOKENS),
    ([":application:test"], (*PROFILE_TOKENS, "--console", "verbose")),
    (["test", "--warning-mode", "all"], (*PROFILE_TOKENS, "--tests", "all")),
    (["test", "--console=rich", "--tests", "plain"], (*PROFILE_TOKENS, "--console", "plain")),
    (["test", "--tests", "all"], (*PROFILE_TOKENS, "--warning-mode", "all")),
    (["test", "--console=plain", "--console=rich"], (*PROFILE_TOKENS, "--console", "plain")),
])
def test_task_scope_does_not_match_metadata_or_option_values(tail, expected):
    command = _wrapper(["./gradlew", *tail, "--tests", SELECTOR], question="test " + SELECTOR)
    assert not is_test_command_execution(command, expected, SELECTOR)


@pytest.mark.parametrize("selector_args,accepted", [
    (["--tests", SELECTOR], True),
    (["--tests=" + SELECTOR], True),
    (["--tests", SELECTOR.lower()], False),
    (["--tests", SELECTOR + "Other"], False),
    (["--tests", "*.RememberedPeopleTest"], False),
    (["--tests", "*"], False),
    (["--tests", "RememberedPeopleTest"], False),
    ([], False),
    (["-Pselector=" + SELECTOR], False),
    (["--console", SELECTOR], False),
    ([":other:test", "--tests", SELECTOR], False),
    (["--tests", "OtherTest", ":other:test", "--tests", SELECTOR], False),
    (["--tests", SELECTOR, ":other:test"], False),
])
def test_gradle_selector_must_be_an_exact_tests_option(selector_args, accepted):
    command = _wrapper(["./gradlew", ":application:test", *selector_args], question=SELECTOR)
    assert is_test_command_execution(command, PROFILE_TOKENS, SELECTOR) is accepted


@pytest.mark.parametrize("case", [
    "green", "failed", "unknown", "latest-failed", "latest-unknown", "wrong-selector",
    "wrong-case", "sibling", "stale", "build-only", "no-selector",
])
@pytest.mark.parametrize("profile_options", [(), ("--console", "plain")])
def test_wrapped_phase_and_spec_use_observed_results(tmp_path, case, profile_options):
    root = tmp_path / "project"
    root.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    profile = {"gates": [{"id": "test", "command": [*PROFILE_TOKENS, *profile_options]}]}
    spec = f"## Spec Items\nSPEC-1: Selected regression executes.\nverify: test:{SELECTOR}\n"
    (run_dir / "design.md").write_text(spec, encoding="utf-8")
    capture_design_ledger(run_dir, "design", spec)
    tail = ["./gradlew", ":application:test", "--tests", SELECTOR]
    if case == "wrong-selector":
        tail[-1] += "Other"
    elif case == "wrong-case":
        tail[-1] = SELECTOR.lower()
    elif case == "build-only":
        tail[1] = "assembleDebug"
    elif case == "no-selector":
        tail = tail[:2]
    tail.append("--console=plain")
    command = _wrapper(tail, question="test " + SELECTOR)
    entry = {
        "command": command, "exit_code": 1 if case == "failed" else None if case == "unknown" else 0,
        "at": 5 if case == "stale" else 20,
        "cwd": str(tmp_path / "sibling") if case == "sibling" else str(root),
        "code_baseline": "entry-baseline", "host": "omp",
    }
    entries = [entry]
    if case.startswith("latest-"):
        entries.append({**entry, "exit_code": 1 if case == "latest-failed" else None, "at": 21})
    log_path = root / COMMANDS_RUN_LOG
    log_path.parent.mkdir()
    raw = "".join(json.dumps(row) + "\n" for row in entries)
    log_path.write_text(raw, encoding="utf-8")
    phase_text = (
        f"## Completion Gate\nregression-test: {SELECTOR}\n"
        "change-kind: behavior-preserving\nbehavior-preserving-reason: evidence compatibility\n"
        "test-run-evidence: verified\n"
    )
    related = missing_test_evidence_markers(
        root, "implement", phase_text, profile=profile, since=10, cwd_root=root,
    )
    spec_missing = missing_spec_item_evidence(
        root, run_dir, "final-review", "## Completion Gate\nverdict: approve\n",
        profile=profile, since=10,
    )
    assert (related == []) is (case == "green")
    assert (spec_missing == []) is (case == "green")
    ordinary = missing_test_evidence_markers(
        root, "red", "## Completion Gate\n", profile=profile, since=10, cwd_root=root,
    )
    if case == "build-only":
        assert any("no test command was observed" in value for value in ordinary)
    elif case == "no-selector":
        assert any("every observed test command exited 0" in value for value in ordinary)
    elif case == "failed":
        assert ordinary == []
        reference = read_red_reference(
            root, text=phase_text, profile=profile, code_baseline="entry-baseline", cwd_root=root,
        )
        assert reference is not None
        assert json.loads(reference[1]) == {
            key: entry[key] for key in ("command", "cwd", "at", "exit_code", "code_baseline")
        } | {"test": SELECTOR}
        assert read_red_reference(
            root, text=phase_text, profile=profile, code_baseline="other-baseline", cwd_root=root,
        ) is None
        assert read_red_reference(
            root, text=phase_text.replace(SELECTOR, SELECTOR + "Other"),
            profile=profile, code_baseline="entry-baseline", cwd_root=root,
        ) is None
    observed = read_command_evidence(root).runs[0]
    assert (observed.command, observed.exit_code, observed.cwd, observed.at,
            observed.code_baseline, observed.host) == (
        entry["command"], entry["exit_code"], entry["cwd"], entry["at"],
        entry["code_baseline"], "omp",
    )
    assert log_path.read_text(encoding="utf-8") == raw
