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


WEB_VERIFICATION_DEPENDENCIES = {"jsdom": "26.1.0", "esbuild": "0.25.2"}


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


def _command(args: list[str], cwd: Path, env: dict[str, str], *, timeout: int = 120) -> dict:
    result = _run_process(args, cwd=cwd, env=env, timeout=timeout)
    return {"command": args, "exit_code": result.returncode,
            "status": "PASS" if result.returncode == 0 else "FAIL",
            "output": (result.stdout + result.stderr).strip()[-4000:]}


def _run_process(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            process.wait(timeout=2)
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"evaluation tool unavailable: {name}")
    return str(Path(executable).absolute())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _cache_lock(path: Path):
    import fcntl

    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["lock_sha256"] != _sha(cache / "package-lock.json"):
                raise RuntimeError("evaluation dependency cache lock fingerprint changed")
            if json.loads((cache / "package.json").read_text(encoding="utf-8")) != manifest:
                raise RuntimeError("evaluation dependency cache manifest changed")
        else:
            (cache / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            install_env = dict(env, npm_config_cache=str(cache_root / "npm-cache"))
            install = _command([npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
                               cache, install_env, timeout=600)
            if install["exit_code"] != 0:
                raise RuntimeError(f"evaluation dependency preparation failed: npm exit {install['exit_code']}")
            receipt = {"cache_key": key, "lock_sha256": _sha(cache / "package-lock.json")}
        versions = _installed_versions(cache, manifest)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
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
