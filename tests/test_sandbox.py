"""#409 v3.14 A: the run sandbox, tried adversarially from inside, and a sandboxed drive end to end with a fake CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
import arm_compare  # noqa: E402
import sandbox  # noqa: E402
import snapshot  # noqa: E402

pytestmark = pytest.mark.skipif(not sandbox.available(), reason="needs unshare and root")


def _box(tmp_path: Path, timeout: float = 30) -> tuple[Path, dict]:
    run = tmp_path / "r-test"
    (run / "project").mkdir(parents=True)
    layout = sandbox.prepare_layout(run)
    box = sandbox.spec(expose=[{"path": str(run), "mode": "ro"}, {"path": str(run / "project"), "mode": "rw"}],
                       cwd=str(run / "project"), env={"PATH": os.environ["PATH"], "HOME": "/root"}, timeout=timeout,
                       home=str(layout["home"]), overlay_root=str(layout["overlay"]))
    return run, box


def _script(run: Path, text: str) -> list[str]:
    path = run / "project" / "probe.sh"
    path.write_text(text, encoding="utf-8")
    return ["bash", str(path)]


def test_pkill_inside_cannot_see_or_kill_a_process_outside(tmp_path):
    run, box = _box(tmp_path)
    outside = subprocess.Popen(["sleep", "301"])
    try:
        result = sandbox.run(box, _script(run, "pgrep -f 'sleep 301' >/dev/null; echo seen=$?\n"
                                               "pkill -f 'sleep 301'; echo killed=$?\nps -e | wc -l\n"), capture=True)
        assert "seen=1" in result["stdout"] and "killed=1" in result["stdout"]
        assert outside.poll() is None
    finally:
        outside.kill()


def test_the_host_is_read_only_and_the_run_directory_too(tmp_path):
    run, box = _box(tmp_path)
    result = sandbox.run(box, _script(run, "touch /usr/local/bin/x 2>/dev/null; echo usr=$?\n"
                                           "touch /etc/x 2>/dev/null; echo etc=$?\n"
                                           "touch ../x 2>/dev/null; echo run=$?\n"
                                           "echo hello > made.txt && echo project=$?\n"
                                           "touch /tmp/scratch && echo tmp=$?\n"), capture=True)
    out = result["stdout"]
    assert "usr=1" in out and "etc=1" in out and "run=1" in out and "project=0" in out and "tmp=0" in out
    assert not Path("/usr/local/bin/x").exists() and not (run / "x").exists()
    assert (run / "project" / "made.txt").read_text(encoding="utf-8") == "hello\n"
    assert not Path("/tmp/scratch").exists()  # the sandbox's /tmp is its own
    assert result["checks"]["host_read_only"] is True


def test_hidden_paths_are_empty_and_only_the_runs_own_directory_is_back(tmp_path):
    run, box = _box(tmp_path)
    sibling = tmp_path / "r-other"
    sibling.mkdir()
    (sibling / "result.json").write_text("{}", encoding="utf-8")
    probe = ("ls -A /home | wc -l; ls -A /mnt | wc -l; ls -A /root/.codex 2>/dev/null | wc -l\n"
             f"ls -A {tmp_path}\n"
             f"cat {sibling}/result.json 2>/dev/null || echo sibling-hidden\n"
             "ls /root/.claude/projects 2>/dev/null | wc -l\n")
    result = sandbox.run(box, _script(run, probe), capture=True)
    lines = result["stdout"].split()
    assert lines[:3] == ["0", "0", "0"], result["stdout"]
    assert "r-test" in lines and "r-other" not in lines and "sibling-hidden" in lines
    for hidden in ("/home/user/agent-flow-evals-private", "/home/user/agent-flow"):
        assert hidden not in result["stdout"]


def test_a_background_child_left_running_is_killed_at_exit_and_reported(tmp_path):
    run, box = _box(tmp_path)
    marker = "sleep 307"
    result = sandbox.run(box, _script(run, f"(nohup {marker} >/dev/null 2>&1 &)\necho started\n"), capture=True)
    assert result["returncode"] == 0 and "started" in result["stdout"]
    assert any(marker in item["cmdline"] for item in result["killed_at_exit"])
    assert result["survivors"] == []
    time.sleep(0.3)
    assert subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 1


def test_the_deadline_kills_a_command_that_does_not_end(tmp_path):
    run, box = _box(tmp_path, timeout=2)
    started = time.monotonic()
    result = sandbox.run(box, ["sleep", "60"], capture=True)
    assert result["timed_out"] is True and time.monotonic() - started < 30 and result["survivors"] == []


def test_the_home_is_the_runs_own_with_the_ca_bundle_read_only(tmp_path):
    run, box = _box(tmp_path)
    result = sandbox.run(box, _script(run, "echo $HOME; test -r /root/.ccr/ca-bundle.crt && echo ca=ok\n"
                                           "touch /root/.ccr/x 2>/dev/null; echo ca-write=$?\n"
                                           "mkdir -p /root/.agent-flow/worktrees/w && echo wt=ok\n"), capture=True)
    assert "ca-write=1" in result["stdout"] and "wt=ok" in result["stdout"]
    if Path("/root/.ccr/ca-bundle.crt").exists():
        assert "ca=ok" in result["stdout"]
    assert (run / "sandbox" / "home" / ".agent-flow" / "worktrees" / "w").is_dir()
    assert sandbox.to_host("/root/.agent-flow/worktrees/w", run / "sandbox" / "home") == \
        run / "sandbox" / "home" / ".agent-flow" / "worktrees" / "w"
    assert sandbox.to_inside(run / "sandbox" / "home" / "x", run / "sandbox" / "home") == Path("/root/x")


def test_the_tripwire_sees_a_change_in_a_system_directory(tmp_path, monkeypatch):
    watched = tmp_path / "bin"
    watched.mkdir()
    monkeypatch.setattr(sandbox, "TRIPWIRE", (str(watched),))
    before = sandbox.tripwire()
    assert sandbox.tripwire_changed(before, sandbox.tripwire()) == []
    (watched / "agent-flow").write_text("#!/bin/sh\n", encoding="utf-8")
    assert sandbox.tripwire_changed(before, sandbox.tripwire()) == [str(watched)]


def test_a_submission_wrapper_runs_eval_json_commands_in_their_own_sandbox(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    (build / "eval.json").write_text(json.dumps({"install": "true", "start": "echo started > out.txt; "
                                                                           "touch /usr/local/bin/y 2>/dev/null; "
                                                                           "echo $? >> out.txt"}), encoding="utf-8")
    (build / "gradlew").write_text("#!/bin/sh\necho gradle \"$@\" > gradle.txt\n", encoding="utf-8")
    (build / "gradlew").chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    wrapped = arm_compare.wrap_submission(build, work)
    assert wrapped["eval_json"] == ["install", "start"] and wrapped["gradlew"] is True
    spec = json.loads((build / "eval.json").read_text(encoding="utf-8"))
    assert "sandbox.py" in spec["start"] and " exec " in spec["start"]
    subprocess.run(spec["start"], shell=True, cwd=build, check=True)
    assert (build / "out.txt").read_text(encoding="utf-8").split() == ["started", "1"]
    subprocess.run(["./gradlew", "test"], cwd=build, check=True)
    assert (build / "gradle.txt").read_text(encoding="utf-8").strip() == "gradle test"
    rows = [json.loads(line) for line in Path(wrapped["log"]).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and all(row["survivors"] == [] for row in rows)


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, subprocess, sys
if "--version" in sys.argv:
    print("2.1.310 (Claude Code)")
    sys.exit(0)
session = "sess-1"
folder = os.path.join(os.environ["HOME"], ".claude", "projects", "-p")
os.makedirs(folder, exist_ok=True)
probe = subprocess.run(["sh", "-c", "touch /usr/local/bin/zz 2>/dev/null; echo $?"], capture_output=True, text=True)
open("work.txt", "w").write("done " + probe.stdout.strip() + "\\n")
subprocess.Popen(["sh", "-c", "sleep 311"], start_new_session=True)
row = {"type": "assistant", "timestamp": "2026-10-25T00:00:00Z", "message": {"id": "m1", "model": "claude-opus-5-5",
       "content": [{"type": "text", "text": "Done.\\n\\nEVAL-STATUS: complete"}],
       "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
open(os.path.join(folder, session + ".jsonl"), "w").write(json.dumps(row) + "\\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5",
                  "tools": [], "skills": %s}))
print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.5, "is_error": False,
                  "result": "Done.\\n\\nEVAL-STATUS: complete"}))
""" % json.dumps(list(arm_compare.BASE_SKILLS))


def test_a_sandboxed_drive_kills_what_the_run_left_freezes_the_tree_and_records_it(tmp_path, monkeypatch):
    run = tmp_path / "runs" / "r-abc"
    arm_compare.prepare(ROOT, "A", "devflow", 1, run)
    fake = run / "bin"
    fake.mkdir()
    (fake / "claude").write_text(FAKE_CLAUDE, encoding="utf-8")
    (fake / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv(arm_compare.SANDBOX_ENV, "required")
    monkeypatch.setenv(arm_compare.LOCK_ENV, str(tmp_path / "lock"))
    record = arm_compare.drive(run, "claude-opus-5-5", 120, 5.0)
    assert record["sandbox"]["mode"] == "required" and record["sandbox"]["survivors"] == []
    assert any("sleep 311" in item["cmdline"] for item in record["sandbox"]["killed_at_exit"])
    assert record["harness_error"] is None, record["harness_error"]
    assert (run / "project" / "work.txt").read_text(encoding="utf-8") == "done 1\n"  # /usr/local/bin was read-only
    assert record["transcript"] == str(run / "sandbox" / "home" / ".claude" / "projects" / "-p" / "sess-1.jsonl")
    assert record["snapshot"]["has_uncommitted"] is True and "work.txt" in record["snapshot"]["uncommitted"]
    assert record["budget_ledger"]["total_cost_usd"] == 0.5 and record["budget_ledger"]["cost_complete"] is True
    assert record["base_skills"]["matches_pin"] is True and record["pins"]["mismatches"] == []
    assert record["tripwire_changed"] == [] and record["end_to_end_seconds"] >= record["controller_seconds"]
    assert subprocess.run(["pgrep", "-f", "sleep 311"], capture_output=True).returncode == 1
    (run / "project" / "work.txt").write_text("changed later\n", encoding="utf-8")
    result = arm_compare.score(run, None)
    assert result["isolation"] == "enforced" and result["uncommitted"] is True
    assert (run / "graded" / "tree" / "work.txt").read_text(encoding="utf-8") == "done 1\n"
    assert result["artifact_sha256"] == snapshot.verify(run)["tree_sha256"]


# ─── the simulated user's oracle stays outside the sandbox ───

ORACLE_TOKEN = "ORACLE-TOKEN-7f3a91"


def _simulated_run(base: Path) -> tuple[Path, dict]:
    from tests.test_user_sim import _case
    case_dir = _case(base)
    oracle = case_dir / "task" / "prd.md"
    oracle.write_text(oracle.read_text(encoding="utf-8") + f"- The secret coupon code is {ORACLE_TOKEN}.\n",
                      encoding="utf-8")
    run = base / "runs" / "r-sim"
    meta = arm_compare.prepare(ROOT, "A", "x", 1, run, case_dir=case_dir, layer="underspec")
    return run, meta


@pytest.mark.parametrize("where", ["tmp", "visible-host-path"])
def test_inside_the_sandbox_the_oracle_cannot_be_read_but_ask_user_answers(tmp_path, monkeypatch, where):
    import shutil
    import user_sim
    from tests.test_user_sim import _staged
    base = tmp_path if where == "tmp" else Path("/srv") / f"arm-compare-test-{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        run, meta = _simulated_run(base)
        private = user_sim.private_dir(run)
        assert ORACLE_TOKEN in (private / user_sim.ORACLE).read_text(encoding="utf-8")
        box = arm_compare.run_box(run, meta, run / "project", 60, "controller")
        if where != "tmp":  # a run dir outside /tmp and /home: its private sibling is hidden by name
            assert str(private) in box["hide"]
        head, tail = ORACLE_TOKEN[:6], ORACLE_TOKEN[6:]  # the probe itself must not hold the token
        probe = (f"t={head}; grep -rs \"$t{tail}\" {run} {run.parent} /tmp /home --exclude-dir=sandbox --exclude-dir=kit; echo grep=$?\n"
                 f"cat {private / user_sim.ORACLE} >/dev/null 2>&1; echo cat=$?\n"
                 f"ls -A {private} 2>/dev/null | wc -l\n"
                 f"{meta['user_sim']['ask_user']} '장바구니에 최대 몇 개까지 담나요?'; echo ask=$?\n")
        (run / "project" / "probe.sh").write_text(probe, encoding="utf-8")
        with user_sim.Broker(run, call=_staged('{"fact_ids": ["F01"]}', "최대 5개입니다."), environ={}):
            result = sandbox.run(box, ["bash", str(run / "project" / "probe.sh")], capture=True)
        out = result["stdout"]
        assert "grep=1" in out and ORACLE_TOKEN not in out, out
        # the same search on the host finds it: the probe would have seen the oracle had it been mounted
        assert subprocess.run(["grep", "-rqs", ORACLE_TOKEN, str(run.parent)]).returncode == 0
        assert "cat=1" in out and "최대 5개입니다." in out and "ask=0" in out, out + result["stderr"]
        assert result["checks"]["host_read_only"] is True and result["checks"]["hidden_leaks"] == []
        (row,) = user_sim.read_log(user_sim.log_path(run))
        assert row["disclosed_fact_ids"] == ["F01"] and row["answer"] == "최대 5개입니다."
        assert not (run / user_sim.DIR / user_sim.CLIENT).exists()  # the broker's address ends with the drive
    finally:
        if where != "tmp":
            shutil.rmtree(base, ignore_errors=True)
