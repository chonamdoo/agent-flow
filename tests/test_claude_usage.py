"""#409 v3.14 B: the kit reviewer usage wrapper (`evals/claude_usage.py`) and the ledger that prices the kit's
reviewers from it. A fake real CLI only; no model is called."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src"))
import arm_compare  # noqa: E402
import claude_usage  # noqa: E402

FAKE = r'''#!/usr/bin/python3 -I
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as out:
        out.write(json.dumps(args) + "\n")
prompt = args[-1] if args else ""
result = "review of: " + prompt + "\n  - must-fix: ünïcode ✓\n"
code = int(os.environ.get("FAKE_EXIT", "0"))
if args[:2] == ["--output-format", "json"]:
    if os.environ.get("FAKE_BROKEN"):
        sys.stdout.write("partial {not json")
        sys.exit(code)
    sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": code != 0, "result": result,
                                 "total_cost_usd": 0.0123, "usage": {"input_tokens": 5, "output_tokens": 7},
                                 "modelUsage": {os.environ.get("FAKE_MODEL", "claude-opus-5-5"): {"costUSD": 0.0123}},
                                 "duration_ms": 10}) + "\n")
elif "--output-format" in args:
    sys.stdout.write('{"type":"system"}\n')
else:
    sys.stdout.buffer.write((result + "\n").encode("utf-8"))
sys.exit(code)
'''
REVIEWER = ["-p", "--model", "claude-opus-5-5", "--effort", "low", "--safe-mode", "--permission-mode", "plan",
            "--no-session-persistence", "Review the diff.\nList must-fix items."]


@pytest.fixture
def cli(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "claude").write_text(FAKE, encoding="utf-8")
    (real / "claude").chmod(0o755)
    name = f"test-usage-{secrets.token_hex(6)}"
    wrapper = claude_usage.install(tmp_path / "bin", name)
    log = tmp_path / "ledger" / claude_usage.LOG
    argv_log = tmp_path / "argv.jsonl"
    env = claude_usage.with_wrapper({"PATH": f"{real}:/usr/bin:/bin", "FAKE_ARGV_LOG": str(argv_log)},
                                    tmp_path / "bin")

    def run(args: list[str], **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["claude", *args], env={**env, **extra}, capture_output=True, cwd=tmp_path,
                              stdin=subprocess.DEVNULL)

    def direct(args: list[str], **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(real / "claude"), *args], env={**env, **extra}, capture_output=True, cwd=tmp_path,
                              stdin=subprocess.DEVNULL)

    def argv_rows() -> list[list[str]]:
        lines = argv_log.read_text(encoding="utf-8").splitlines() if argv_log.exists() else []
        return [json.loads(line) for line in lines]

    return type("Cli", (), {"run": staticmethod(run), "direct": staticmethod(direct), "argv": staticmethod(argv_rows),
                            "name": name, "log": log, "wrapper": wrapper, "real": real})


def test_the_kits_reviewer_launch_is_what_the_wrapper_recognizes(tmp_path):
    from agent_flow.cli_detect import CliInfo
    from agent_flow.multi_review import _reviewer_cli_args
    claude = CliInfo(name="claude", binaries=("claude",), invoke=("-p",))
    for model, effort in (("claude-opus-5-5", "low"), (None, None)):
        args = list(_reviewer_cli_args(claude, prompt="p", project_root=tmp_path, model=model, effort=effort))
        assert claude_usage.is_kit_reviewer(args)
    controller = arm_compare.controller_argv("task", "claude-opus-5-5", 3.0)[1:]
    assert not claude_usage.is_kit_reviewer(controller)
    assert not claude_usage.is_kit_reviewer(["--version"])
    assert not claude_usage.is_kit_reviewer([*REVIEWER[:-1], "--output-format", "json", "p"])
    assert not claude_usage.is_kit_reviewer([item for item in REVIEWER if item != "--safe-mode"])
    assert not claude_usage.is_kit_reviewer(["-p", "--safe-mode", "--permission-mode", "auto",
                                             "--no-session-persistence", "p"])


def test_a_reviewer_sees_the_text_mode_bytes_and_exit_and_the_ledger_gets_its_usage(cli):
    expected = cli.direct(REVIEWER, FAKE_EXIT="2")
    with claude_usage.Collector(cli.name, cli.log) as collector:
        seen = cli.run(REVIEWER, FAKE_EXIT="2")
    assert seen.stdout == expected.stdout and seen.returncode == expected.returncode == 2
    assert "✓".encode() in seen.stdout and collector.received == 1
    # the real CLI got the same arguments with the JSON output format first
    assert cli.argv()[-1] == ["--output-format", "json", *REVIEWER]
    [row] = claude_usage.read_rows(cli.log)
    assert row["kind"] == "kit-reviewer" and row["status"] == "priced" and row["exit"] == 2
    assert row["argv_digest"] == claude_usage.argv_digest(REVIEWER) and row["total_cost_usd"] == 0.0123
    assert row["usage"] == {"input_tokens": 5, "output_tokens": 7} and row["model"] == "claude-opus-5-5"
    assert row["requested_model"] == "claude-opus-5-5" and row["effort"] == "low" and row["text_exact"] is True
    assert row["duration_s"] >= 0
    # an error result keeps the CLI's own text, which `outage` reads for a usage limit
    assert row["error_text"].startswith("review of: Review the diff.")


def test_every_other_call_reaches_the_real_cli_unchanged(cli):
    calls = [arm_compare.controller_argv("do the task", "claude-opus-5-5", 3.0)[1:], ["--version"],
             [*REVIEWER[:-1], "--output-format", "json", "already json"],
             ["-p", "--model", "claude-opus-5-5", "--no-session-persistence", "router prompt"]]
    with claude_usage.Collector(cli.name, cli.log) as collector:
        for args in calls:
            seen, expected = cli.run(args, FAKE_EXIT="3"), cli.direct(args, FAKE_EXIT="3")
            assert seen.stdout == expected.stdout and seen.returncode == expected.returncode == 3
    logged = cli.argv()
    assert logged[0::2] == calls and logged[1::2] == calls  # wrapped call, then the direct one: the same argv
    assert collector.received == 0 and claude_usage.read_rows(cli.log) == []


def test_unparsable_json_prints_the_raw_output_and_records_an_unpriced_row(cli):
    with claude_usage.Collector(cli.name, cli.log):
        seen = cli.run(REVIEWER, FAKE_BROKEN="1", FAKE_EXIT="1")
    assert seen.stdout == b"partial {not json" and seen.returncode == 1
    [row] = claude_usage.read_rows(cli.log)
    assert row["status"] == "unpriced" and row["total_cost_usd"] is None and row["error"].startswith("json")
    assert claude_usage.summary([row])["unpriced"] == 1


def test_an_error_without_a_result_is_flagged_as_not_text_exact():
    text, exact = claude_usage.text_of({"subtype": "error_max_turns", "is_error": True, "result": None,
                                        "errors": ["Reached maximum turns"]})
    assert text == b"Error: Reached maximum turns" and exact is False
    assert claude_usage.text_of({"result": "OK"}) == (b"OK\n", True)


def test_a_wrapper_under_the_kits_landlock_still_reports_its_row(cli, tmp_path):
    from agent_flow.providers import landlock_launch
    if landlock_launch.abi_version() < landlock_launch.MIN_ABI:
        pytest.skip("Landlock is not available on this kernel")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = claude_usage.with_wrapper({"PATH": f"{cli.real}:/usr/bin:/bin"}, cli.wrapper.parent)
    with claude_usage.Collector(cli.name, cli.log):
        seen = subprocess.run([sys.executable, "-I", landlock_launch.__file__, "--write", str(scratch), "--",
                               str(cli.wrapper.resolve()), *REVIEWER], env=env, capture_output=True, cwd=tmp_path)
    assert seen.returncode == 0, seen.stderr
    assert seen.stdout == cli.direct(REVIEWER).stdout
    assert [row["status"] for row in claude_usage.read_rows(cli.log)] == ["priced"]


def _metrics(project: Path, jobs: int) -> None:
    runs = project / ".git" / "agent-flow" / "worktrees" / "w" / ".agent-flow" / "runs" / "r1"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "review-metrics-1.json").write_text(json.dumps({"jobs": [
        {"job_id": f"j{index}", "provider": "claude", "model": "claude-opus-5-5", "effort": "low", "status": "ok",
         "duration_s": 10, "prompt_utf8_bytes": 4_000, "stdout_utf8_bytes": 400} for index in range(jobs)]}),
        encoding="utf-8")


def _rows(run: Path, *rows: dict) -> None:
    with (run / claude_usage.LOG).open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps({"kind": "kit-reviewer", "status": "priced", "requested_model": "claude-opus-5-5",
                                  "effort": "low", "models": ["claude-opus-5-5"], **row}) + "\n")


def test_the_ledger_adds_the_kit_reviewers_when_every_job_has_a_priced_row(tmp_path):
    project = tmp_path / "project"
    _metrics(project, 2)
    meta = {"arm": "C", "project": str(project)}
    _rows(tmp_path, {"total_cost_usd": 0.25}, {"total_cost_usd": 0.5})
    ledger = arm_compare.budget_ledger(tmp_path, meta, 2.0, 0.1, project, project)
    assert ledger["cost_complete"] is True and ledger["unpriced"] == []
    assert ledger["components"]["kit_reviewers"] == 0.75 and ledger["total_cost_usd"] == 2.85
    assert ledger["enforced_usd"] == 2.85 and ledger["kit_reviewer_rows"] == ledger["kit_reviewer_jobs"] == 2
    pins = arm_compare.verify_pins(tmp_path, meta, project, project, {"model": "claude-opus-5-5"}, [])
    assert pins["mismatches"] == [] and pins["kit_reviewer_calls_checked"] == 2


def test_a_missing_or_unpriced_row_leaves_the_total_null_and_the_cap_uses_what_was_seen(tmp_path):
    project = tmp_path / "project"
    _metrics(project, 2)
    meta = {"arm": "C", "project": str(project)}
    _rows(tmp_path, {"total_cost_usd": 0.25})
    short = arm_compare.budget_ledger(tmp_path, meta, 2.0, 0.0, project, project)
    assert short["cost_complete"] is False and short["total_cost_usd"] is None
    assert short["unpriced"] == ["kit_reviewers"]
    assert short["kit_reviewer_rows"] == 1 and short["enforced_usd"] == 2.25
    _rows(tmp_path, {"total_cost_usd": 0.25}, {"status": "unpriced", "total_cost_usd": None})
    unpriced = arm_compare.budget_ledger(tmp_path, meta, 2.0, 0.0, project, project)
    assert unpriced["cost_complete"] is False and unpriced["kit_reviewer_rows_unpriced"] == 1
    _rows(tmp_path, {"total_cost_usd": 0.25}, {"total_cost_usd": 0.25}, {"total_cost_usd": 0.25})
    extra = arm_compare.budget_ledger(tmp_path, meta, 2.0, 0.0, project, project)
    assert extra["cost_complete"] is False and extra["enforced_usd"] == 2.75  # more rows than jobs: not understood
    (tmp_path / claude_usage.LOG).unlink()
    bare = arm_compare.budget_ledger(tmp_path, {"arm": "A", "project": str(project)}, 2.0, 0.0, project, project)
    assert bare["cost_complete"] is True and bare["total_cost_usd"] == 2.0


def test_a_reviewer_row_on_another_model_or_effort_is_a_pin_mismatch(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _rows(tmp_path, {"effort": "high"}, {"models": ["claude-opus-5-5", "claude-haiku-x"]})
    pins = arm_compare.verify_pins(tmp_path, {"arm": "A", "project": str(project)}, project, project,
                                   {"model": "claude-opus-5-5"}, [])
    assert pins["mismatches"] == ["kit reviewer call 1: claude-opus-5-5/high",
                                  "kit reviewer call 2 (served): claude-haiku-x/None"]


def test_the_run_sandbox_puts_the_wrapper_first_on_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    meta = {"arm": "C", "project": str(project)}
    assert not arm_compare.run_box(tmp_path, meta, project, 60, "x")["env"]["PATH"].startswith(
        str(arm_compare.usage_bin(tmp_path)))
    arm_compare.install_usage_wrapper(tmp_path)
    box = arm_compare.run_box(tmp_path, meta, project, 60, "x")
    assert box["env"]["PATH"].split(os.pathsep)[0] == str(arm_compare.usage_bin(tmp_path))
    wrapper = arm_compare.usage_bin(tmp_path) / "claude"
    mode = wrapper.stat().st_mode
    assert wrapper.is_file() and not wrapper.is_symlink() and mode & 0o111 and not mode & 0o022
