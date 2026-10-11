from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time


WEB_VERIFICATION_DEPENDENCIES = {"jsdom": "26.1.0", "esbuild": "0.25.2"}
COMMAND_TIMEOUT = 120
DEPENDENCY_TIMEOUT = 600
CACHE_LOCK_TIMEOUT = 600
PROCESS_CLEANUP_GRACE = 16
PIPE_DRAIN_TIMEOUT = 2
PROCESS_REAP_TIMEOUT = 2
_CLEANUP_ENV = "_AGENT_FLOW_EVAL_CLEANUP_GRACE"
_OWNED_PROCESSES: dict[subprocess.Popen, float] = {}
_PROCESS_LOCK = threading.RLock()
_CANCELLED = threading.Event()
_CANCEL_SIGNAL = signal.SIGINT
_CANCELLATION_DEPTH = 0


def environment_for_project(project: Path | None, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    if project is None:
        return env
    state = Path(project).resolve() / ".phase-eval-environment"
    env.update({
        "DASH__SUPPRESS_ANALYTICS": "true",
        "ANALYZER_STATE_LOCATION_OVERRIDE": str(state / "analyzer-state"),
        "npm_config_cache": str(state / "npm-cache"),
    })
    return env


def _command(args: list[str], cwd: Path, env: dict[str, str], *, timeout: int = COMMAND_TIMEOUT) -> dict:
    result = _run_process(args, cwd=cwd, env=env, timeout=timeout)
    return {"command": args, "exit_code": result.returncode,
            "status": "PASS" if result.returncode == 0 else "FAIL",
            "output": (result.stdout + result.stderr).strip()[-4000:]}


def _raise_cancelled() -> None:
    if _CANCELLED.is_set():
        if _CANCEL_SIGNAL == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + _CANCEL_SIGNAL)


def _signal_process_group(process: subprocess.Popen, signum: int) -> None:
    process.poll()
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass


def _signal_owned(signum: int) -> None:
    with _PROCESS_LOCK:
        for process in _OWNED_PROCESSES:
            _signal_process_group(process, signum)


def _wait_owned_cleanup() -> None:
    deadline = time.monotonic() + PROCESS_CLEANUP_GRACE + PIPE_DRAIN_TIMEOUT + PROCESS_REAP_TIMEOUT + 1
    while time.monotonic() < deadline:
        with _PROCESS_LOCK:
            if not _OWNED_PROCESSES:
                break
        time.sleep(0.05)
    _signal_owned(signal.SIGKILL)


@contextmanager
def _cancellation_scope():
    global _CANCELLATION_DEPTH, _CANCEL_SIGNAL
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    if _CANCELLATION_DEPTH:
        try:
            yield
        except (KeyboardInterrupt, SystemExit):
            _CANCELLED.set()
            raise
        return
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    cancel_received = False

    # Worker loops own cleanup; signal callbacks must not re-enter subprocess locks.
    def cancel(signum, frame):
        global _CANCEL_SIGNAL
        nonlocal cancel_received
        cancel_received = True
        _CANCEL_SIGNAL = signum
        _CANCELLED.set()

    _CANCELLATION_DEPTH += 1
    try:
        for signum in previous:
            signal.signal(signum, cancel)
        try:
            yield
            _raise_cancelled()
        except BaseException:
            _CANCELLED.set()
            _wait_owned_cleanup()
            raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _CANCELLATION_DEPTH -= 1
        try:
            if cancel_received:
                _wait_owned_cleanup()
                _raise_cancelled()
        finally:
            with _PROCESS_LOCK:
                if not _OWNED_PROCESSES:
                    _CANCELLED.clear()
                    _CANCEL_SIGNAL = signal.SIGINT


def _output_text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def _stop_process(process: subprocess.Popen, grace: float) -> tuple[str, str]:
    for signum, budget in ((signal.SIGTERM, grace), (signal.SIGKILL, PIPE_DRAIN_TIMEOUT)):
        if process.poll() is not None:
            budget = min(budget, PIPE_DRAIN_TIMEOUT)
        _signal_process_group(process, signum)
        try:
            stdout, stderr = process.communicate(timeout=budget)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = _output_text(error.output), _output_text(error.stderr)
        else:
            # 그룹의 leader가 끝나도 같은 그룹의 다른 자손은 남을 수 있다.
            _signal_process_group(process, signal.SIGKILL)
            return stdout, stderr
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()
    process.wait(timeout=PROCESS_REAP_TIMEOUT)
    return stdout, stderr


def _run_process(args: list[str], *, cwd: Path | None, env: dict[str, str], timeout: int,
                 input: str | None = None, timeout_result: bool = False) -> subprocess.CompletedProcess:
    grace = min(PROCESS_CLEANUP_GRACE, max(1.0, float(env.get(_CLEANUP_ENV, PROCESS_CLEANUP_GRACE))))
    child_env = dict(env, **{_CLEANUP_ENV: str(max(1.0, grace - PIPE_DRAIN_TIMEOUT - PROCESS_REAP_TIMEOUT - 1))})
    with _cancellation_scope():
        with _PROCESS_LOCK:
            _raise_cancelled()
            process = subprocess.Popen(
                args, cwd=cwd, env=child_env, stdin=subprocess.PIPE if input is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            _OWNED_PROCESSES[process] = grace
        deadline = time.monotonic() + timeout
        try:
            while True:
                _raise_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(input, timeout=min(remaining, 0.2))
                    break
                except subprocess.TimeoutExpired:
                    input = None
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = _stop_process(process, grace)
            if timeout_result:
                return subprocess.CompletedProcess(
                    args, process.returncode or -signal.SIGKILL, stdout, f"timeout after {timeout}s\n{stderr}",
                )
            error.timeout, error.output, error.stderr = timeout, stdout, stderr
            raise
        except BaseException:
            _stop_process(process, grace)
            raise
        finally:
            with _PROCESS_LOCK:
                del _OWNED_PROCESSES[process]


def _executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"evaluation tool unavailable: {name}")
    return str(Path(executable).absolute())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _cache_lock(path: Path, *, timeout: float = CACHE_LOCK_TIMEOUT):
    import fcntl

    with path.open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            _raise_cancelled()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"evaluation dependency cache lock timed out after {timeout}s: {path}")
                _CANCELLED.wait(min(remaining, 0.05))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _dependency_manifest(seed: Path, stack: str) -> dict:
    manifest = json.loads((seed / "package.json").read_text(encoding="utf-8"))
    if stack == "web":
        manifest.setdefault("devDependencies", {}).update(WEB_VERIFICATION_DEPENDENCIES)
    for group in ("dependencies", "devDependencies"):
        for name, version in manifest.get(group, {}).items():
            if not isinstance(version, str) or not version or version[0] not in "0123456789":
                raise RuntimeError(f"evaluation dependency must use an exact version: {name}")
            if any(char in version for char in "^~* <>=|"):
                raise RuntimeError(f"evaluation dependency must use an exact version: {name}")
    return manifest


def _installed_versions(root: Path, manifest: dict) -> dict[str, str]:
    versions = {}
    for group in ("dependencies", "devDependencies"):
        for name, expected in manifest.get(group, {}).items():
            package = root / "node_modules" / name / "package.json"
            if not package.is_file():
                raise RuntimeError(f"evaluation dependency unavailable: {name}")
            actual = json.loads(package.read_text(encoding="utf-8")).get("version")
            if actual != expected:
                raise RuntimeError(f"evaluation dependency version mismatch: {name}")
            versions[name] = actual
    return versions


def _node_dependencies(project: Path, case_dir: Path, cache_root: Path, stack: str) -> dict:
    node, npm = _executable("node"), _executable("npm")
    env = environment_for_project(project)
    tool_versions = {tool: _command([path, "--version"], project, env)
                     for tool, path in (("node", node), ("npm", npm))}
    if any(item["exit_code"] != 0 for item in tool_versions.values()):
        raise RuntimeError("evaluation Node toolchain version preflight failed")
    manifest = _dependency_manifest(case_dir / "seed", stack)
    encoded = json.dumps({"package": manifest, "tools": tool_versions}, sort_keys=True).encode()
    key = hashlib.sha256(encoded).hexdigest()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / key
    with _cache_lock(cache_root / f"{key}.lock"):
        cache.mkdir(exist_ok=True)
        receipt_path = cache / "readiness.json"
        cached = receipt_path.is_file()
        for staging in cache.glob(".readiness-*.json"):
            staging.unlink()
        if cached:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["cache_key"] != key:
                raise RuntimeError("evaluation dependency cache tool fingerprint changed")
            if receipt["lock_sha256"] != _sha(cache / "package-lock.json"):
                raise RuntimeError("evaluation dependency cache lock fingerprint changed")
        else:
            (cache / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            install_env = dict(env, npm_config_cache=str(cache_root / "npm-cache"))
            install = _command([npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
                               cache, install_env, timeout=DEPENDENCY_TIMEOUT)
            if install["exit_code"] != 0:
                raise RuntimeError(f"evaluation dependency preparation failed: npm exit {install['exit_code']}")
            receipt = {"cache_key": key, "lock_sha256": _sha(cache / "package-lock.json")}
        if json.loads((cache / "package.json").read_text(encoding="utf-8")) != manifest:
            raise RuntimeError("evaluation dependency cache manifest changed")
        versions = _installed_versions(cache, manifest)
        if not cached:
            staging = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".readiness-",
                                                 suffix=".json", dir=cache, delete=False) as receipt_file:
                    staging = Path(receipt_file.name)
                    receipt_file.write(json.dumps(receipt, sort_keys=True) + "\n")
                    receipt_file.flush()
                    os.fsync(receipt_file.fileno())
                staging.replace(receipt_path)
            finally:
                if staging is not None:
                    staging.unlink(missing_ok=True)
        target = project / "node_modules"
        if target.exists():
            raise RuntimeError("evaluation fixture already contains node_modules")
        shutil.copytree(cache / "node_modules", target, symlinks=True)
    tsc = project / "node_modules" / ".bin" / "tsc"
    if not tsc.is_file():
        raise RuntimeError("evaluation TypeScript compiler unavailable")
    with tempfile.TemporaryDirectory(prefix="typecheck-", dir=project / ".phase-eval-environment") as scratch:
        probe = Path(scratch) / "probe.tsx"
        probe.write_text("import * as React from 'react';\nexport const probe = <React.Fragment />;\n", encoding="utf-8")
        args = [node, str(tsc), "--noEmit", "--strict", "--skipLibCheck", "--jsx", "react-jsx",
                "--moduleResolution", "bundler", "--module", "esnext", str(probe)]
        typecheck = _command(args, project, env)
    if typecheck["exit_code"] != 0:
        raise RuntimeError(f"evaluation TypeScript/React readiness failed: {typecheck['output']}")
    paths = {"node": node, "npm": npm, "tsc": str(tsc)}
    if stack == "web":
        next_bin = project / "node_modules" / ".bin" / "next"
        if not next_bin.is_file():
            raise RuntimeError("evaluation Next executable unavailable")
        paths["next"] = str(next_bin)
    return {"status": "PASS", "cache_path": str(cache), **receipt,
            "seed_manifest_sha256": _sha(case_dir / "seed" / "package.json"),
            "package_versions": versions, "tool_paths": paths, "tool_versions": tool_versions,
            "typecheck_toolchain_probe": typecheck,
            "fixture_typecheck": {"status": "NOT_RUN", "owner": "evaluation harness after implementation"},
            "framework_build": {"status": "NOT_RUN", "owner": "CI integration/release verification"}}


def _dart_readiness(project: Path, *, require_flutter: bool = False) -> dict:
    dart = _executable("dart")
    paths = {"dart": dart}
    if require_flutter:
        paths["flutter"] = _executable("flutter")
    env = environment_for_project(project)
    version = _command([dart, "--version"], project, env)
    with tempfile.TemporaryDirectory(prefix="dart-", dir=project / ".phase-eval-environment") as scratch:
        probe = Path(scratch) / "probe.dart"
        probe.write_text("void main(){print('DART_READY');}\n", encoding="utf-8")
        checks = {"format": _command([dart, "format", str(probe)], project, env),
                  "analyze": _command([dart, "analyze", str(probe)], project, env),
                  "file_execution": _command([dart, str(probe)], project, env)}
    if version["exit_code"] != 0 or any(check["exit_code"] != 0 for check in checks.values()):
        raise RuntimeError("evaluation Dart format/analyze/execution readiness failed")
    return {"status": "PASS", "tool_paths": paths, "tool_versions": {"dart": version},
            "checks": checks, "flutter_test": {"status": "NOT_RUN", "owner": "review evidence harness"}}


def prepare_fixture_environment(project: Path, case_dir: Path, cache_root: Path,
                                *, scenario: str = "author") -> dict:
    project, case_dir, cache_root = project.resolve(), case_dir.resolve(), cache_root.resolve()
    (project / ".phase-eval-environment").mkdir(exist_ok=True)
    ignore = project / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    entries = [item for item in ("node_modules/", ".phase-eval-environment/") if item not in existing.splitlines()]
    if entries:
        ignore.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "") +
                          "\n".join(entries) + "\n", encoding="utf-8")
    stack = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))["stack"]
    if stack in ("web", "rn"):
        readiness = _node_dependencies(project, case_dir, cache_root, stack)
    elif stack == "app":
        readiness = _dart_readiness(project, require_flutter=scenario == "review")
    else:
        readiness = {"status": "NOT_APPLICABLE", "tool_paths": {}}
    report = {"stack": stack, "environment_overrides": {
        name: environment_for_project(project)[name] for name in (
            "DASH__SUPPRESS_ANALYTICS", "ANALYZER_STATE_LOCATION_OVERRIDE", "npm_config_cache")},
        "readiness": readiness}
    (project / ".phase-eval-environment" / "readiness.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def fixture_typecheck(project: Path) -> dict:
    project = project.resolve()
    compiler = project / "node_modules" / ".bin" / "tsc"
    if not compiler.is_file():
        return {"status": "UNAVAILABLE", "reason": "fixture TypeScript compiler missing"}
    return _command([_executable("node"), str(compiler), "--noEmit", "--project", str(project / "tsconfig.json")],
                    project, environment_for_project(project))
