from __future__ import annotations

import importlib.util
import concurrent.futures
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import threading
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
                    package.parent.mkdir(parents=True, exist_ok=True)
                    package.write_text(json.dumps({"version": version}))
            binaries = cwd / "node_modules/.bin"
            binaries.mkdir(exist_ok=True)
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
    with pytest.raises(RuntimeError):
        environment._node_dependencies(after, case, tmp_path / "cache", "web")
    assert not (after / "node_modules").exists()


@pytest.mark.parametrize("tamper", ["manifest", "installed-version"])
def test_new_dependency_receipt_requires_validated_install(environment, monkeypatch, tmp_path, tamper):
    case, _ = _fake_dependency_install(environment, monkeypatch, tmp_path)
    command = environment._command

    def changed_install(args, cwd, env, **kwargs):
        result = command(args, cwd, env, **kwargs)
        if "install" in args:
            if tamper == "manifest":
                (cwd / "package.json").write_text("{}")
            else:
                (cwd / "node_modules/react/package.json").write_text('{"version":"18.3.1"}')
        return result

    monkeypatch.setattr(environment, "_command", changed_install)
    project = tmp_path / "project"
    (project / ".phase-eval-environment").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        environment._node_dependencies(project, case, tmp_path / "cache", "web")
    assert not list((tmp_path / "cache").glob("*/readiness.json"))
    assert not (project / "node_modules").exists()


def test_dependency_cache_hit_never_rewrites_committed_receipt(environment, monkeypatch, tmp_path):
    case, installations = _fake_dependency_install(environment, monkeypatch, tmp_path)
    before = tmp_path / "before"
    (before / ".phase-eval-environment").mkdir(parents=True)
    report = environment._node_dependencies(before, case, tmp_path / "cache", "web")
    receipt = Path(report["cache_path"]) / "readiness.json"
    original, inode = receipt.read_bytes(), receipt.stat().st_ino
    write_text = Path.write_text

    def interrupted_write(path, data, *args, **kwargs):
        if path == receipt:
            with path.open("w"):
                pass
            raise KeyboardInterrupt
        return write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", interrupted_write)
    after = tmp_path / "after"
    (after / ".phase-eval-environment").mkdir(parents=True)
    reused = environment._node_dependencies(after, case, tmp_path / "cache", "web")
    assert reused["cache_key"] == report["cache_key"]
    assert receipt.read_bytes() == original
    assert receipt.stat().st_ino == inode
    assert len(installations) == 1


def test_interrupted_dependency_cache_reuse_preserves_receipt_and_next_reuse(environment, monkeypatch, tmp_path):
    case, installations = _fake_dependency_install(environment, monkeypatch, tmp_path)
    before = tmp_path / "before"
    (before / ".phase-eval-environment").mkdir(parents=True)
    report = environment._node_dependencies(before, case, tmp_path / "cache", "web")
    receipt = Path(report["cache_path"]) / "readiness.json"
    original = receipt.read_bytes()
    interrupted = tmp_path / "interrupted"
    (interrupted / ".phase-eval-environment").mkdir(parents=True)
    with monkeypatch.context() as patch:
        patch.setattr(environment.shutil, "copytree", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            environment._node_dependencies(interrupted, case, tmp_path / "cache", "web")
    assert receipt.read_bytes() == original
    after = tmp_path / "after"
    (after / ".phase-eval-environment").mkdir(parents=True)
    reused = environment._node_dependencies(after, case, tmp_path / "cache", "web")
    assert reused["lock_sha256"] == report["lock_sha256"]
    assert (after / "node_modules/react/package.json").is_file()
    assert receipt.read_bytes() == original
    assert len(installations) == 1


@pytest.mark.parametrize("boundary", ["flush", "replace"])
def test_interrupted_initial_dependency_receipt_is_not_ready(environment, monkeypatch, tmp_path, boundary):
    case, installations = _fake_dependency_install(environment, monkeypatch, tmp_path)
    project = tmp_path / "project"
    (project / ".phase-eval-environment").mkdir(parents=True)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        if boundary == "flush":
            patch.setattr(environment.os, "fsync", interrupt)
        else:
            patch.setattr(Path, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            environment._node_dependencies(project, case, tmp_path / "cache", "web")
    assert not list((tmp_path / "cache").glob("*/readiness.json"))
    assert not list((tmp_path / "cache").glob("*/.readiness-*.json"))
    assert not (project / "node_modules").exists()
    report = environment._node_dependencies(project, case, tmp_path / "cache", "web")
    assert report["status"] == "PASS"
    assert len(installations) == 2
    assert json.loads((Path(report["cache_path"]) / "readiness.json").read_text())["cache_key"] == report["cache_key"]


@pytest.mark.parametrize("tamper", ["manifest", "installed-version", "tool-fingerprint"])
def test_dependency_cache_validation_never_hides_tampering(environment, monkeypatch, tmp_path, tamper):
    case, _ = _fake_dependency_install(environment, monkeypatch, tmp_path)
    before = tmp_path / "before"
    (before / ".phase-eval-environment").mkdir(parents=True)
    report = environment._node_dependencies(before, case, tmp_path / "cache", "web")
    cache = Path(report["cache_path"])
    if tamper == "manifest":
        (cache / "package.json").write_text("{}")
    elif tamper == "installed-version":
        (cache / "node_modules/react/package.json").write_text('{"version":"18.3.1"}')
    else:
        receipt = json.loads((cache / "readiness.json").read_text())
        receipt["cache_key"] = "wrong-toolchain"
        (cache / "readiness.json").write_text(json.dumps(receipt))
    after = tmp_path / "after"
    (after / ".phase-eval-environment").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        environment._node_dependencies(after, case, tmp_path / "cache", "web")
    assert not (after / "node_modules").exists()


def test_dependency_cache_lock_wait_is_bounded_and_does_not_unlock_owner(environment, tmp_path):
    path = tmp_path / "cache.lock"
    with path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with environment._cache_lock(path, timeout=0.1):
                pytest.fail("the competing lock was acquired")
        assert time.monotonic() - started < 2
        with path.open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with environment._cache_lock(path, timeout=0.1):
        pass


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


@pytest.mark.parametrize("helper,cancellation", [
    ("environment", "timeout"), ("environment", "sigterm"), ("environment", "keyboard"),
    ("environment", "worker-sigterm"),
    ("web", "sigterm"), ("rn", "sigterm"), ("app", "sigterm"), ("backend", "sigterm"),
])
def test_outer_cancellation_reaps_nested_owned_sessions_before_return(
    environment, monkeypatch, tmp_path, helper, cancellation,
):
    monkeypatch.syspath_prepend(str(ROOT / "evals"))
    monkeypatch.setitem(sys.modules, "phase_eval_environment", environment)
    spec = importlib.util.spec_from_file_location("cancellation_evaluator", ROOT / "evals/phase_eval.py")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    ready, gate = tmp_path / "ready.fifo", tmp_path / "gate.fifo"
    os.mkfifo(ready)
    os.mkfifo(gate)
    ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import os,pathlib,signal,sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "gate=os.open(sys.argv[2],os.O_RDWR)\n"
        "pathlib.Path(sys.argv[4]).write_text(str(os.getpid()))\n"
        "ready=os.open(sys.argv[1],os.O_WRONLY)\n"
        "os.write(ready,b'READY');os.close(ready)\n"
        "os.read(gate,1)\n"
        "pathlib.Path(sys.argv[3]).write_text('escaped writer')\n",
    )
    writer_args = [sys.executable, str(writer), str(ready), str(gate),
                   str(tmp_path / "escaped.txt"), str(tmp_path / "writer.pid")]
    middle = tmp_path / "middle.py"
    middle.write_text(
        "import importlib.util,os,pathlib,sys\n"
        f"sys.path.insert(0,{str(ROOT / 'evals')!r})\n"
        f"pathlib.Path({str(tmp_path / 'middle.pid')!r}).write_text(str(os.getpid()))\n"
        f"args={writer_args!r}\n"
        + ("from phase_eval_environment import _run_process,environment_for_project\n"
           f"_run_process(args,cwd=pathlib.Path({str(tmp_path)!r}),env=environment_for_project(None),timeout=120)\n"
           if helper == "environment" else
           f"spec=importlib.util.spec_from_file_location('owned_oracle',{str(ROOT / 'evals/phase-cases' / helper / 'oracle.py')!r})\n"
           "oracle=importlib.util.module_from_spec(spec);spec.loader.exec_module(oracle)\n"
           + (f"oracle._run_tests(pathlib.Path({str(tmp_path)!r}),args)\n" if helper == "backend" else
              f"oracle._run(args,pathlib.Path({str(tmp_path)!r}))\n" if helper == "rn" else
              f"oracle.run(args,pathlib.Path({str(tmp_path)!r}))\n")),
    )
    outer = tmp_path / "outer.py"
    outer.write_text(
        "import os,pathlib,sys\n"
        f"sys.path.insert(0,{str(ROOT / 'evals')!r})\n"
        "from phase_eval import _run_group\n"
        f"pathlib.Path({str(tmp_path / 'outer.pid')!r}).write_text(str(os.getpid()))\n"
        f"result=_run_group({[sys.executable, str(middle)]!r},timeout=120)\n"
        "raise SystemExit(result.returncode)\n",
    )
    popen = environment.subprocess.Popen
    synchronized = False

    def spawn(*args, **kwargs):
        nonlocal synchronized
        process = popen(*args, **kwargs)
        communicate = process.communicate

        def observe_ready(*args, **kwargs):
            nonlocal synchronized
            if not synchronized:
                assert select.select([ready_fd], [], [], 10)[0], "the owned writer did not reach its gate"
                assert os.read(ready_fd, 5) == b"READY"
                synchronized = True
                if cancellation in {"sigterm", "worker-sigterm"}:
                    os.kill(os.getpid(), signal.SIGTERM)
                elif cancellation == "keyboard":
                    raise KeyboardInterrupt
            return communicate(*args, **kwargs)

        process.communicate = observe_ready
        return process

    monkeypatch.setattr(environment.subprocess, "Popen", spawn)
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    started = time.monotonic()
    try:
        if cancellation == "timeout":
            result = evaluator._run_group([sys.executable, str(outer)], timeout=1)
            assert result.returncode != 0
        else:
            with pytest.raises(KeyboardInterrupt if cancellation == "keyboard" else SystemExit) as interruption:
                if cancellation == "worker-sigterm":
                    with concurrent.futures.ThreadPoolExecutor(1) as pool:
                        with environment._cancellation_scope():
                            future = pool.submit(evaluator._run_group, [sys.executable, str(outer)], timeout=120)
                            future.result(timeout=24)
                else:
                    evaluator._run_group([sys.executable, str(outer)], timeout=120)
            if cancellation in {"sigterm", "worker-sigterm"}:
                assert interruption.value.code == 128 + signal.SIGTERM
        assert synchronized
        assert time.monotonic() - started < 24
        assert not (tmp_path / "escaped.txt").exists()
        try:
            release_fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as no_writer:
            assert no_writer.errno == errno.ENXIO
        else:
            os.close(release_fd)
            pytest.fail("an owned writer still waits behind the post-cancellation gate")
        assert {signum: signal.getsignal(signum) for signum in handlers} == handlers
        assert not environment._OWNED_PROCESSES
    finally:
        os.close(ready_fd)
        for name in ("outer", "middle", "writer"):
            pid_file = tmp_path / f"{name}.pid"
            if pid_file.exists():
                try:
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_process_worker_runs_without_installing_signal_handlers(environment, monkeypatch, tmp_path):
    signal_handler = environment.signal.signal
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    def main_thread_signal(*args):
        assert threading.current_thread() is threading.main_thread()
        return signal_handler(*args)

    monkeypatch.setattr(environment.signal, "signal", main_thread_signal)
    with environment._cancellation_scope():
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(environment._run_process, [sys.executable, "-c", "print('worker evidence')"],
                                 cwd=tmp_path, env=environment.environment_for_project(tmp_path), timeout=10)
            result = future.result(timeout=12)
            assert result.returncode == 0
            assert result.stdout.strip() == "worker evidence"
    assert {signum: signal.getsignal(signum) for signum in handlers} == handlers
