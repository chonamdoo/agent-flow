from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "evals"))
    spec = importlib.util.spec_from_file_location(
        "phase_eval_web_verification", ROOT / "evals/phase_eval_web_verification.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_review_runs_typecheck_and_component_runtime(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "fixture_typecheck", lambda project: {"status": "PASS", "exit_code": 0})
    calls = []
    monkeypatch.setattr(verifier, "_executable", lambda name: "/tools/node")

    def run(args, cwd, env, **kwargs):
        calls.append((args, cwd, env))
        return {"command": args, "status": "PASS", "exit_code": 0,
                "output": "SSR, hydration, loaded/empty/error observed"}

    monkeypatch.setattr(verifier, "_command", run)
    checks = verifier.verify_web_review(tmp_path)
    assert [check["kind"] for check in checks] == ["typecheck", "component-runtime"]
    assert all(check["exit_code"] == 0 for check in checks)
    assert calls[0][0][0] == "/tools/node"
    assert Path(calls[0][0][1]).is_file()
    assert calls[0][0][2] == str(tmp_path.resolve())
    assert calls[0][2]["npm_config_cache"].startswith(str(tmp_path))
    assert "framework build" in checks[1]["scope"]


def test_web_review_preserves_real_failure_evidence(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "fixture_typecheck", lambda project: {
        "status": "FAIL", "exit_code": 2, "output": "TS2322 actual mismatch"})
    monkeypatch.setattr(verifier, "_executable", lambda name: "/tools/node")
    monkeypatch.setattr(verifier, "_command", lambda *a, **k: {
        "status": "FAIL", "exit_code": 1, "output": "hydration mismatch"})
    checks = verifier.verify_web_review(tmp_path)
    assert [(check["status"], check["exit_code"]) for check in checks] == [("FAIL", 2), ("FAIL", 1)]
    assert "TS2322" in checks[0]["output"]
    assert "hydration mismatch" in checks[1]["output"]


def test_missing_dependencies_never_produce_verification_pass(verifier, monkeypatch, tmp_path):
    checks = verifier.verify_web_review(tmp_path)
    assert checks[0]["status"] == "UNAVAILABLE"
    assert checks[1]["status"] == "FAIL"
    assert checks[1]["exit_code"] != 0


def test_review_scratch_preserves_dependency_executable_symlinks_and_isolation(verifier, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("phase_eval_scratch", ROOT / "evals/phase_eval.py")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    import agent_flow.adapters.hosted as hosted
    import agent_flow.artifact as artifact
    import agent_flow.core.local_skills as local_skills
    import agent_flow.runner as runner

    case = tmp_path / "case"
    case.mkdir()
    (case / "case.json").write_text(json.dumps({"stack": "web", "profile": "nextjs", "task": "review",
        "modes": {"team": {"install": []}}, "test_command": ["node", "--version"]}))
    project = tmp_path / "project"

    def prepare_dependencies(target, *args, **kwargs):
        binaries = target / "node_modules/.bin"
        compiler = target / "node_modules/typescript/bin"
        library = target / "node_modules/typescript/lib"
        for directory in (binaries, compiler, library):
            directory.mkdir(parents=True)
        (compiler / "tsc").write_text("require('../lib/tsc.js');\n")
        (library / "tsc.js").write_text(
            "require('fs').writeFileSync('compiler-output.txt', 'scratch only');\n"
            "console.log('TYPECHECK_READY');\n")
        (binaries / "tsc").symlink_to("../typescript/bin/tsc")
        return {}

    def create_run(target, *args, **kwargs):
        run = target / ".agent-flow/run"
        run.mkdir(parents=True)
        return run

    monkeypatch.setattr(evaluator, "prepare_fixture_environment", prepare_dependencies)
    monkeypatch.setattr(evaluator, "_git", lambda *args: None)
    monkeypatch.setattr(evaluator, "_run_group", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, "behavior test observed", ""))
    monkeypatch.setattr(evaluator, "_fixture_tooling", lambda *args:
                        {"test_command": ["node", "--version"], "tools": {}, "launcher": "/fixture/agent-flow"})
    monkeypatch.setattr(hosted, "HostedAdapter", lambda *args: SimpleNamespace())
    monkeypatch.setattr(hosted, "_profile_base_branch", lambda *args: "main")
    monkeypatch.setattr(hosted, "_write_review_input_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(hosted, "_reviewer_jobs", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(artifact, "create_run", create_run)
    monkeypatch.setattr(local_skills, "changed_files", lambda *args: [])
    monkeypatch.setattr(runner, "Runner", lambda *args, **kwargs: SimpleNamespace(
        profile_id="nextjs", profile={}, config_root=project, phases=[SimpleNamespace(id="multi-review")]))
    monkeypatch.setattr(verifier, "_command", lambda *args, **kwargs:
                        {"status": "PASS", "exit_code": 0, "output": "component runtime checked separately"})
    observed = []

    def verify_scratch(scratch):
        checks = verifier.verify_web_review(scratch)
        observed.append((scratch, checks))
        assert checks[0]["status"] == "PASS", checks[0]
        assert checks[0]["output"] == "TYPECHECK_READY"
        assert (scratch / "node_modules/.bin/tsc").is_symlink()
        assert (scratch / "node_modules/.bin/tsc").readlink().as_posix() == "../typescript/bin/tsc"
        assert (scratch / "compiler-output.txt").read_text() == "scratch only"
        return checks

    monkeypatch.setattr(evaluator, "verify_web_review", verify_scratch)
    evaluator.prepare(ROOT, case, "team", "review", "clean", project, review_providers=("codex",))
    assert len(observed) == 1
    assert not observed[0][0].exists()
    assert not (project / "compiler-output.txt").exists()
    evidence = (project / ".agent-flow/run/test-evidence.md").read_text()
    assert "TYPECHECK_READY" in evidence
    assert "status PASS" in evidence
