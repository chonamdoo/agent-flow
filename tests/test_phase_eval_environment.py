from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def environment():
    spec = importlib.util.spec_from_file_location("phase_eval_environment", ROOT / "evals/phase_eval_environment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_environment_preserves_home_credentials_and_proxy(environment, tmp_path):
    original = {"HOME": "/read-only/home", "CODEX_HOME": "/private/auth", "HTTPS_PROXY": "http://proxy",
                "DART_SUPPRESS_ANALYTICS": "true", "PATH": "/tools/bin"}
    actual = environment.environment_for_project(tmp_path, original)
    assert original == {key: actual[key] for key in original}
    assert actual["DASH__SUPPRESS_ANALYTICS"] == "true"
    assert Path(actual["ANALYZER_STATE_LOCATION_OVERRIDE"]).is_relative_to(tmp_path)
    assert Path(actual["npm_config_cache"]).is_relative_to(tmp_path)
    assert environment.environment_for_project(None, original) == original


def test_dart_readiness_requires_format_analyze_and_execution(environment, monkeypatch, tmp_path):
    (tmp_path / ".phase-eval-environment").mkdir()
    commands = []
    monkeypatch.setattr(environment, "_executable", lambda name: "/tools/dart")

    def run(args, cwd, env, **kwargs):
        commands.append(args)
        assert env["DASH__SUPPRESS_ANALYTICS"] == "true"
        assert Path(env["ANALYZER_STATE_LOCATION_OVERRIDE"]).is_relative_to(tmp_path)
        return {"command": args, "exit_code": 0, "status": "PASS", "output": "observed"}

    monkeypatch.setattr(environment, "_command", run)
    report = environment._dart_readiness(tmp_path)
    assert report["status"] == "PASS"
    assert set(report["checks"]) == {"format", "analyze", "file_execution"}
    assert [command[1] for command in commands[:3]] == ["--version", "format", "analyze"]
    assert report["flutter_test"]["status"] == "NOT_RUN"


def test_dart_failed_analysis_never_reports_readiness_pass(environment, monkeypatch, tmp_path):
    (tmp_path / ".phase-eval-environment").mkdir()
    monkeypatch.setattr(environment, "_executable", lambda name: "/tools/dart")
    monkeypatch.setattr(environment, "_command", lambda args, *a, **k: {
        "exit_code": 255 if args[1] == "analyze" else 0, "output": "read-only analytics state",
    })
    with pytest.raises(RuntimeError, match="Dart format/analyze/execution"):
        environment._dart_readiness(tmp_path)


def test_exact_seed_versions_are_preserved_with_scoped_verification_dependencies(environment, tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"react": "19.1.0", "next": "15.3.0"},
        "devDependencies": {"typescript": "5.8.3"},
    }))
    web = environment._dependency_manifest(tmp_path, "web")
    rn = environment._dependency_manifest(tmp_path, "rn")
    assert web["dependencies"] == rn["dependencies"] == {"react": "19.1.0", "next": "15.3.0"}
    assert web["devDependencies"]["typescript"] == "5.8.3"
    assert web["devDependencies"]["jsdom"] == "26.1.0"
    assert "jsdom" not in rn["devDependencies"]
    assert "jsdom" not in json.loads((tmp_path / "package.json").read_text())["devDependencies"]


def test_non_exact_seed_dependency_is_rejected(environment, tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^19.1.0"}}))
    with pytest.raises(RuntimeError, match="exact version"):
        environment._dependency_manifest(tmp_path, "rn")


def test_installed_dependency_mismatch_is_rejected(environment, tmp_path):
    manifest = {"dependencies": {"react": "19.1.0"}}
    package = tmp_path / "node_modules/react/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"18.3.1"}')
    with pytest.raises(RuntimeError, match="version mismatch"):
        environment._installed_versions(tmp_path, manifest)
    package.unlink()
    with pytest.raises(RuntimeError, match="unavailable"):
        environment._installed_versions(tmp_path, manifest)


def test_typecheck_unavailable_is_not_a_pass(environment, tmp_path):
    assert environment.fixture_typecheck(tmp_path)["status"] == "UNAVAILABLE"


def test_typecheck_records_actual_exit_code_and_scoped_environment(environment, monkeypatch, tmp_path):
    compiler = tmp_path / "node_modules/.bin/tsc"
    compiler.parent.mkdir(parents=True)
    compiler.touch()
    monkeypatch.setattr(environment, "_executable", lambda name: "/tools/node")

    def run(args, **kwargs):
        assert args == ["/tools/node", str(compiler), "--noEmit", "--project", str(tmp_path / "tsconfig.json")]
        assert kwargs["env"]["DASH__SUPPRESS_ANALYTICS"] == "true"
        return subprocess.CompletedProcess(args, 2, "TS2322: mismatch", "")

    monkeypatch.setattr(environment, "_run_process", run)
    report = environment.fixture_typecheck(tmp_path)
    assert report["status"] == "FAIL"
    assert report["exit_code"] == 2
    assert "TS2322" in report["output"]


def _fake_dependency_install(environment, monkeypatch, root):
    case = root / "case"
    (case / "seed").mkdir(parents=True)
    (case / "seed/package.json").write_text(json.dumps({
        "dependencies": {"react": "19.1.0", "next": "15.3.0"},
        "devDependencies": {"typescript": "5.8.3"},
    }))
    monkeypatch.setattr(environment, "_executable", lambda name: f"/tools/{name}")
    installations = []

    def command(args, cwd, env, **kwargs):
        if "install" in args:
            installations.append(args)
            manifest = json.loads((cwd / "package.json").read_text())
            (cwd / "package-lock.json").write_text('{"lockfileVersion":3}')
            for group in ("dependencies", "devDependencies"):
                for name, version in manifest.get(group, {}).items():
                    package = cwd / "node_modules" / name / "package.json"
                    package.parent.mkdir(parents=True)
                    package.write_text(json.dumps({"version": version}))
            binaries = cwd / "node_modules/.bin"
            binaries.mkdir()
            (binaries / "tsc").touch()
            (binaries / "next").touch()
        return {"command": args, "exit_code": 0, "status": "PASS", "output": "ready"}

    monkeypatch.setattr(environment, "_command", command)
    return case, installations


def test_dependency_cache_is_reused_without_changing_seed_manifest(environment, monkeypatch, tmp_path):
    case, installations = _fake_dependency_install(environment, monkeypatch, tmp_path)
    seed = (case / "seed/package.json").read_bytes()
    reports = []
    for name in ("before", "after"):
        project = tmp_path / name
        (project / ".phase-eval-environment").mkdir(parents=True)
        reports.append(environment._node_dependencies(project, case, tmp_path / "cache", "web"))
    assert len(installations) == 1
    assert reports[0]["lock_sha256"] == reports[1]["lock_sha256"]
    assert reports[0]["cache_key"] == reports[1]["cache_key"]
    assert (tmp_path / "before/node_modules/react/package.json").is_file()
    assert (tmp_path / "after/node_modules/react/package.json").is_file()
    assert (case / "seed/package.json").read_bytes() == seed
    assert all(report["fixture_typecheck"]["status"] == "NOT_RUN" for report in reports)
    assert all(report["framework_build"]["status"] == "NOT_RUN" for report in reports)


def test_dependency_cache_lock_tampering_is_rejected(environment, monkeypatch, tmp_path):
    case, _ = _fake_dependency_install(environment, monkeypatch, tmp_path)
    before = tmp_path / "before"
    (before / ".phase-eval-environment").mkdir(parents=True)
    report = environment._node_dependencies(before, case, tmp_path / "cache", "web")
    (Path(report["cache_path"]) / "package-lock.json").write_text('{"lockfileVersion":2}')
    after = tmp_path / "after"
    (after / ".phase-eval-environment").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="lock fingerprint changed"):
        environment._node_dependencies(after, case, tmp_path / "cache", "web")
    assert not (after / "node_modules").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc records the descendant termination state")
def test_command_timeout_kills_descendants(environment, tmp_path):
    pid_path = tmp_path / "descendant.pid"
    script = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "child.wait()"
    )
    descendant = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            environment._command([sys.executable, "-c", script, str(pid_path)], tmp_path,
                                 environment.environment_for_project(tmp_path), timeout=2)
        assert pid_path.is_file(), "the subprocess never started the descendant"
        descendant = int(pid_path.read_text())
        state_path = Path(f"/proc/{descendant}/stat")
        assert not state_path.exists() or state_path.read_text().split(")", 1)[1].split()[0] == "Z"
    finally:
        if descendant is None and pid_path.is_file():
            descendant = int(pid_path.read_text())
        if descendant is not None:
            try:
                os.kill(descendant, 9)
            except ProcessLookupError:
                pass


def test_app_review_requires_flutter_executable_without_claiming_test_pass(environment, monkeypatch, tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    (case / "case.json").write_text('{"stack":"app"}')
    project = tmp_path / "project"
    project.mkdir()

    def executable(name):
        if name == "flutter":
            raise RuntimeError("evaluation tool unavailable: flutter")
        return f"/tools/{name}"

    monkeypatch.setattr(environment, "_executable", executable)
    monkeypatch.setattr(environment, "_command", lambda args, *a, **k: {
        "command": args, "exit_code": 0, "status": "PASS", "output": "ready",
    })
    with pytest.raises(RuntimeError, match="unavailable: flutter"):
        environment.prepare_fixture_environment(project, case, tmp_path / "cache", scenario="review")
    monkeypatch.setattr(environment, "_executable", lambda name: f"/tools/{name}")
    report = environment.prepare_fixture_environment(project, case, tmp_path / "cache", scenario="review")
    assert report["readiness"]["tool_paths"]["flutter"] == "/tools/flutter"
    assert report["readiness"]["flutter_test"]["status"] == "NOT_RUN"


@pytest.mark.skipif(sys.platform != "linux", reason="the child explicitly escapes its POSIX session")
def test_command_timeout_does_not_wait_for_escaped_pipe_holder(environment, tmp_path):
    pid_path = tmp_path / "escaped.pid"
    script = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            environment._command([sys.executable, "-c", script, str(pid_path)], tmp_path,
                                 environment.environment_for_project(tmp_path), timeout=2)
        assert time.monotonic() - started < 15
        assert pid_path.is_file()
    finally:
        if pid_path.is_file():
            try:
                os.kill(int(pid_path.read_text()), 9)
            except ProcessLookupError:
                pass
