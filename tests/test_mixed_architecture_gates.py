from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_mixed_architecture_example import _lint, mixed_project


REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.cli import main


_RESOLVE_IMPORTS = """
import fs from 'node:fs';
import path from 'node:path';
import { ESLint } from 'eslint';

const [root, ...apps] = process.argv.slice(1);
const eslint = new ESLint({ cwd: process.cwd() });
const results = [];
for (const app of apps) {
  const importer = path.join(root, 'apps', app, 'src/shared/lib/format.ts');
  const config = await eslint.calculateConfigForFile(importer);
  const resolvers = config.settings['import-x/resolver-next'];
  for (const specifier of ['@/shared/lib/format', '@fixture/http']) {
    for (const resolver of resolvers) {
      const resolution = await resolver.resolve(specifier, importer);
      results.push({
        app, specifier,
        found: resolution.found,
        resolved: resolution.path ? fs.realpathSync(resolution.path) : resolution.path,
      });
    }
  }
}
console.log(JSON.stringify(results));
"""


def _lint_root_and_apps(project: Path, *, failing_app: str | None = None) -> list[dict]:
    root_result, root_diagnostics = _lint(project)
    assert root_result.returncode == (1 if failing_app else 0), (
        root_result.stdout + root_result.stderr
    )
    root_messages = {
        Path(item["filePath"]).resolve(): item["messages"] for item in root_diagnostics
    }
    for app in ("main", "bo"):
        result, diagnostics = _lint(project, app=app)
        assert result.returncode == (1 if app == failing_app else 0), (
            result.stdout + result.stderr
        )
        app_root = (project / "apps" / app / "src").resolve()
        assert {
            Path(item["filePath"]).resolve(): item["messages"] for item in diagnostics
        } == {
            filename: messages
            for filename, messages in root_messages.items()
            if filename.is_relative_to(app_root)
        }
    return root_diagnostics


def test_app_alias_resolution_and_root_app_lint_agree(mixed_project: Path) -> None:
    project = mixed_project
    for app in (None, "main", "bo"):
        cwd = project if app is None else project / "apps" / app
        apps = ["main", "bo"] if app is None else [app]
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", _RESOLVE_IMPORTS, str(project), *apps],
            cwd=cwd, text=True, capture_output=True, check=False, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        resolutions = json.loads(result.stdout)
        assert {(item["app"], item["specifier"]) for item in resolutions} == {
            (owner, specifier)
            for owner in apps
            for specifier in ("@/shared/lib/format", "@fixture/http")
        }
        for item in resolutions:
            assert item["found"] is True, item
            expected = project / (
                f"apps/{item['app']}/src/shared/lib/format.ts"
                if item["specifier"].startswith("@/")
                else "packages/http/src/index.ts"
            )
            assert Path(item["resolved"]) == expected.resolve(), item

    assert all(not item["messages"] for item in _lint_root_and_apps(project))
    importer = project / "apps/main/src/entities/product/model/product.ts"
    original = importer.read_text(encoding="utf-8")
    statements = [
        'import * as aliasProbe from "@/features/cart";',
        'import * as relativeProbe from "../../../features/cart";',
        "export { aliasProbe, relativeProbe };",
    ]
    first_line = len(original.rstrip().splitlines()) + 1
    try:
        importer.write_text(original.rstrip() + "\n" + "\n".join(statements) + "\n", encoding="utf-8")
        diagnostics = _lint_root_and_apps(project, failing_app="main")
        violations = {
            (Path(item["filePath"]).resolve(), message["line"], message["ruleId"])
            for item in diagnostics
            for message in item["messages"]
        }
        assert violations == {
            (importer.resolve(), line, "import-x/no-restricted-paths")
            for line in (first_line, first_line + 1)
        }
    finally:
        importer.write_text(original, encoding="utf-8")
    assert all(not item["messages"] for item in _lint_root_and_apps(project))


def _required_lint_result(
    project: Path,
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    passed: bool,
) -> dict:
    artifact = run_dir / "artifacts/gate-results-local-pre-commit.json"
    artifact.unlink(missing_ok=True)
    exit_code = main([
        "gates", "--root", str(project), "--profile", "generic",
        "--run-dir", str(run_dir), "--phase", "pre-commit", "--timeout", "60",
    ])
    output = capsys.readouterr()
    assert exit_code == (0 if passed else 1), output.out + output.err
    assert artifact.is_file(), output.out + output.err
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["passed"] is passed, payload
    assert payload["status"] == ("green" if passed else "request-changes"), payload
    gate = next(item for item in payload["results"] if item["gate_id"] == "lint-boundaries")
    assert gate["passed"] is passed, gate
    assert (gate["exit_code"] == 0) is passed, gate
    return gate


def _gate_diagnostics(project: Path, gate: dict) -> list[dict]:
    payloads = [line for line in gate["stdout"].splitlines() if line.startswith("[{")]
    assert len(payloads) == 1, gate
    diagnostics = json.loads(payloads[0])
    expected = {
        filename.resolve()
        for source in (project / "apps", project / "packages")
        for filename in source.rglob("*")
        if filename.suffix in {".ts", ".tsx"}
    }
    assert { (project / item["filePath"]).resolve() for item in diagnostics } == expected
    assert expected
    return diagnostics


def test_required_lint_gate_propagates_failure_and_recovers(
    mixed_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = mixed_project
    monkeypatch.chdir(project)
    run_dir = tmp_path / "gate-evidence"

    def run_lint(*, passed: bool) -> dict:
        return _required_lint_result(project, run_dir, capsys, passed=passed)

    assert all(not item["messages"] for item in _gate_diagnostics(project, run_lint(passed=True)))
    importer = project / "apps/main/src/entities/product/model/product.ts"
    original = importer.read_text(encoding="utf-8")
    try:
        importer.write_text(
            original + '\nimport * as gateProbe from "@/features/cart";\nexport { gateProbe };\n',
            encoding="utf-8",
        )
        diagnostics = _gate_diagnostics(project, run_lint(passed=False))
        assert {
            ((project / item["filePath"]).resolve(), message["ruleId"])
            for item in diagnostics
            for message in item["messages"]
        } == {(importer.resolve(), "import-x/no-restricted-paths")}
    finally:
        importer.write_text(original, encoding="utf-8")
    assert all(not item["messages"] for item in _gate_diagnostics(project, run_lint(passed=True)))

    package_path = project / "package.json"
    package_original = package_path.read_text(encoding="utf-8")
    package = json.loads(package_original)
    del package["scripts"]["lint"]
    try:
        package_path.write_text(json.dumps(package), encoding="utf-8")
        missing = run_lint(passed=False)
        assert "ERR_PNPM_NO_SCRIPT" in missing["stdout"] + missing["stderr"], missing
    finally:
        package_path.write_text(package_original, encoding="utf-8")
    assert all(not item["messages"] for item in _gate_diagnostics(project, run_lint(passed=True)))

    sources = [
        filename
        for source in (project / "apps", project / "packages")
        for filename in source.rglob("*")
        if filename.suffix in {".ts", ".tsx"}
    ]
    moved = []
    try:
        for filename in sources:
            hidden = filename.with_suffix(filename.suffix + ".unlinted")
            filename.rename(hidden)
            moved.append((filename, hidden))
        empty = run_lint(passed=False)
        assert empty["exit_code"] == 2, empty
    finally:
        for filename, hidden in moved:
            hidden.rename(filename)
    assert all(not item["messages"] for item in _gate_diagnostics(project, run_lint(passed=True)))
