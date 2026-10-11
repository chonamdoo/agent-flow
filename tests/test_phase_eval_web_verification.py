from __future__ import annotations

import importlib.util
from pathlib import Path

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
