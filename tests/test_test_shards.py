from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shard_policy import CLI_CACHED_ROUTING_TESTS, shard_for_test


ROOT = Path(__file__).resolve().parent.parent


def _runner_module():
    path = ROOT / "scripts" / "run-test-shards.py"
    spec = importlib.util.spec_from_file_location("agent_flow_test_shards", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shard_policy_separates_slow_boundaries() -> None:
    assert shard_for_test("tests/test_architecture_lint.py", "test_clean") == "fast"
    assert (
        shard_for_test(
            "tests/test_custom_skill_install.py",
            "test_install_materializes_authenticated_project_launcher",
        )
        == "integration"
    )
    assert (
        shard_for_test(
            "tests/test_custom_skill_install.py",
            "test_parity_checker_validates_external_installed_copy_from_managed_source_worktree",
        )
        == "parity"
    )
    assert (
        shard_for_test(
            "tests/test_runner_smoke.py",
            "test_compare_and_delete_preserves_branch_checked_out_during_deletion",
        )
        == "worktree-lifecycle"
    )
    assert (
        shard_for_test(
            "tests/test_cli.py",
            "test_export_apk_copies_workspace_artifact_to_downloads",
        )
        == "integration"
    )
    assert (
        shard_for_test(
            "tests/test_cli.py",
            "test_node_push_watch_blocks_protected_branches",
        )
        == "integration"
    )


def test_every_test_with_an_install_or_worktree_subprocess_is_slow() -> None:
    for path in (
        ROOT / "tests" / "test_cli.py",
        ROOT / "tests" / "test_custom_skill_install.py",
        ROOT / "tests" / "test_pinned_workspace_boundary.py",
        ROOT / "tests" / "test_runner_smoke.py",
    ):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if _has_slow_process_call(node):
                assert shard_for_test(path, node.name) != "fast", f"unclassified slow test: {node.name}"


def test_cached_routing_tests_use_the_isolated_template_factory() -> None:
    path = ROOT / "tests" / "test_cli.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in CLI_CACHED_ROUTING_TESTS
    }

    assert functions.keys() == CLI_CACHED_ROUTING_TESTS
    for name, node in functions.items():
        calls = {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        assert "_materialize_installed_node_project" in calls, name


def _has_slow_process_call(node: ast.AST) -> bool:
    for call in (value for value in ast.walk(node) if isinstance(value, ast.Call)):
        if isinstance(call.func, ast.Name) and call.func.id in {"_install", "_install_node_project"}:
            return True
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
            "Popen",
            "check_call",
            "check_output",
            "run",
        }:
            continue
        values = {
            value.value
            for value in ast.walk(call)
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        }
        if "install" in values or {"git", "worktree"} <= values or "update-ref" in values:
            return True
        if any("reference-transaction" in value for value in values):
            return True
    return False


def test_package_scripts_keep_changed_test_feedback_separate_from_release_gate() -> None:
    scripts = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]

    assert scripts["test:changed"] == "uv run --extra dev python scripts/run-test-shards.py related"
    assert scripts["test:final"] == "python3 scripts/run-test-shards.py full-final"


def test_changed_files_includes_committed_feature_scope_and_untracked_files(tmp_path: Path) -> None:
    runner = _runner_module()
    project = tmp_path / "project"
    project.mkdir()

    def git(*args: str) -> None:
        subprocess.run(("git", "-C", str(project), *args), check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    source = project / "src" / "agent_flow" / "cli.py"
    source.parent.mkdir(parents=True)
    source.write_text("before = True\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial")
    git("checkout", "-b", "feature")
    source.write_text("after = True\n", encoding="utf-8")
    git("commit", "-am", "feature change")
    (project / "scratch.py").write_text("scratch = True\n", encoding="utf-8")

    assert runner._changed_files(project) == ["scratch.py", "src/agent_flow/cli.py"]


def test_final_plan_runs_canonical_parity_once(tmp_path: Path) -> None:
    runner = _runner_module()

    plan = runner._plan(runner.FINAL_SHARDS, tmp_path, ())

    commands = [command for shard in plan["shards"].values() for command in shard]
    canonical = [command for command in commands if command[-1:] == ["scripts/check-agent-flow-parity.mjs"]]
    assert len(canonical) == 1
    assert not any("check-agent-flow-parity.mjs" in command for command in plan["shards"]["integration"])
    pytest_commands = [command for command in commands if "pytest" in command]
    assert pytest_commands and all("--maxfail=1" in command for command in pytest_commands)
    provider_registry = [
        command
        for command in commands
        if command[-2:] == ["--test", "tests/test_skill_provider_registry.mjs"]
    ]
    assert len(provider_registry) == 1


def test_canonical_parity_has_a_runtime_copy_wrapper() -> None:
    source = (ROOT / "scripts" / "check-agent-flow-parity.mjs").read_text(encoding="utf-8")

    assert "function assertFreshRuntimeCopies" in source
    assert "failures.push(...runtimeParityFailures(SOURCE_ROOT, installRoot, label))" in source


def test_resume_rejects_a_different_workspace_fingerprint(tmp_path: Path) -> None:
    runner = _runner_module()
    run_dir = tmp_path / "run"
    runner._load_or_create_state(run_dir, "run", "full-final", {"digest": "a"}, False)

    with pytest.raises(SystemExit, match="different workspace fingerprint"):
        runner._load_or_create_state(run_dir, "run", "full-final", {"digest": "b"}, True)


def test_resume_rejects_a_different_command_or_targeted_plan(tmp_path: Path) -> None:
    runner = _runner_module()
    fingerprint = {"digest": "same"}
    final_run = tmp_path / "final"
    runner._load_or_create_state(final_run, "final", "full-final", fingerprint, False)

    with pytest.raises(SystemExit, match="different command or shard order"):
        runner._load_or_create_state(final_run, "final", "fast", fingerprint, True)

    targeted_run = tmp_path / "targeted"
    runner._load_or_create_state(
        targeted_run,
        "targeted",
        "targeted",
        fingerprint,
        False,
        ("src/agent_flow/cli.py",),
    )
    with pytest.raises(SystemExit, match="different command plan"):
        runner._load_or_create_state(
            targeted_run,
            "targeted",
            "targeted",
            fingerprint,
            True,
            ("src/agent_flow/core/worktrees.py",),
        )


def test_related_python_and_android_changes_are_scoped() -> None:
    runner = _runner_module()

    assert runner._android_module("app/src/test/java/demo/Test.kt") == "app"
    assert runner._android_module("features/login/src/main/kotlin/Login.kt") == "features/login"
    assert runner._android_module("build.gradle.kts") == "."
    assert runner._android_module("settings.gradle") == "."
    assert runner._android_module("gradle/libs.versions.toml") == "."
    assert runner._android_module("app/proguard-rules.pro") == "app"
    assert runner._android_module("app/config/release-r8.pro") == "app"
    assert runner._android_module("app/src/main/res/raw/payload.bin") == "app"
    assert runner._android_module("app/src/debug/res/drawable/icon.png") == "app"
    assert runner._android_module("app/src/release/assets/model.bin") == "app"
    assert runner._android_module("src/main/kotlin/App.kt") == "."
    assert runner._android_module("docs/testing.md") is None
    assert runner._related_python_tests(("src/agent_flow/cli.py",)) == (
        "tests/test_cli.py::CliTest::test_gate_order_ignores_changed_file_kind_tokens",
        "tests/test_runner_smoke.py::test_stale_worktree_remove_handles_reference_hook_rejection",
    )
    assert runner._related_python_tests(("src/agent_flow/core/workspace_boundary.py",)) == (
        "tests/test_pinned_workspace_boundary.py",
    )
    assert "tests/test_runner_smoke.py::test_compare_and_delete_preserves_branch_checked_out_during_deletion" in runner._related_python_tests(
        ("src/agent_flow/core/worktrees.py",)
    )
    assert runner._related_python_tests(("tests/shard_policy.py",)) == ("tests/test_test_shards.py",)
    assert runner._related_python_tests(("tests/conftest.py",)) == ()
    assert runner._related_python_tests(("bin/agent-flow-kit.mjs",)) == (
        "tests/test_cli.py::CliTest::test_node_status_escapes_task_newlines_and_emits_json",
        "tests/test_custom_skill_install.py::test_install_materializes_authenticated_project_launcher",
        "tests/test_custom_skill_install.py::test_publish_artifact_exports_webp_from_owning_worktree",
        "tests/test_pinned_workspace_boundary.py::test_trusted_agent_flow_gate_remains_the_shell_execution_route",
    )
    exact = ("tests/test_cli.py::CliTest::test_node_status_escapes_task_newlines_and_emits_json",)
    targeted = runner._targeted_commands(ROOT / ".git" / "test", (), exact)
    pytest_command = next(command for command in targeted if command.pytest_report)
    assert pytest_command.argv[-1:] == exact
    for provider_path in (
        "lib/skill-provider-registry.mjs",
        "lib/skill-provider-registry-loader.mjs",
        "lib/portable-skill-name.mjs",
    ):
        provider_targeted = runner._targeted_commands(
            ROOT / ".git" / "test",
            (provider_path,),
            exact,
        )
        assert sum(
            command.argv == ("node", "--test", "tests/test_skill_provider_registry.mjs")
            for command in provider_targeted
        ) == 1
    for source_runtime_path in (
        "lib/skill-selection.mjs",
        "tests/test_skill_source_runtime.mjs",
    ):
        source_runtime_targeted = runner._targeted_commands(
            ROOT / ".git" / "test",
            (source_runtime_path,),
        )
        assert sum(
            command.argv == ("node", "--test", "tests/test_skill_source_runtime.mjs")
            for command in source_runtime_targeted
        ) == 1
        assert not any(command.pytest_report for command in source_runtime_targeted)


def test_javascript_targeted_plan_uses_related_tests_and_package_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_module()
    package = tmp_path / "apps" / "web"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "jest", "typecheck": "tsc --noEmit", "lint": "eslint ."},
                "devDependencies": {"jest": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    commands = runner._javascript_targeted_commands(("apps/web/src/button.tsx",))

    assert [command.argv for command in commands] == [
        (
            "npm",
            "--prefix",
            "apps/web",
            "test",
            "--",
            "--findRelatedTests",
            "src/button.tsx",
            "--runInBand",
        ),
        ("npm", "--prefix", "apps/web", "run", "typecheck"),
        ("npm", "--prefix", "apps/web", "run", "lint"),
    ]
    config_commands = runner._javascript_targeted_commands(("apps/web/package.json",))
    assert config_commands[0].argv == ("npm", "--prefix", "apps/web", "test")


def test_fingerprint_records_required_dimensions() -> None:
    runner = _runner_module()

    fingerprint = runner._fingerprint(ROOT)

    assert {
        "git_tree_hash",
        "workspace_tree_hash",
        "changed_files",
        "production_file_hash",
        "test_file_hash",
        "dependency_lockfile_hash",
        "build_test_configuration_hash",
        "catalog_fingerprint",
        "resolved_skill_lock_hash",
        "toolchain",
        "profile",
        "active_modules",
        "active_packages",
        "hosts",
        "active_host",
        "environment",
        "digest",
        "recorded_at",
    } <= fingerprint.keys()
    assert fingerprint["production_file_hash"] != hashlib.sha256().hexdigest()
    assert fingerprint["test_file_hash"] != hashlib.sha256().hexdigest()


def test_fingerprint_uses_explicit_scope_host_precedence_and_installed_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_module()
    monkeypatch.setenv("AGENT_FLOW_HOST", "claude")
    monkeypatch.setenv("AGENT_FLOW_ACTIVE_HOST", "omp")

    fingerprint = runner._fingerprint(ROOT, ("app/src/debug/res/icon.png",))

    assert fingerprint["active_modules"] == ["app"]
    assert fingerprint["active_host"] == "omp"
    installed_lock = tmp_path / ".agent-flow" / "skills" / "upstream-lock.json"
    installed_lock.parent.mkdir(parents=True)
    installed_lock.write_text("{}\n", encoding="utf-8")
    assert installed_lock in runner._resolved_skill_lock_files(tmp_path)


def test_fingerprint_digest_ignores_git_status_but_tracks_file_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_module()
    monkeypatch.setattr(runner, "_changed_files", lambda _root: ["first.py"])
    monkeypatch.setattr(runner, "_git_tree_hash", lambda _root: "before")
    first = runner._fingerprint(ROOT)
    monkeypatch.setattr(runner, "_changed_files", lambda _root: ["second.py"])
    monkeypatch.setattr(runner, "_git_tree_hash", lambda _root: "after")
    second = runner._fingerprint(ROOT)
    assert first["digest"] == second["digest"]

    executable = tmp_path / "tool"
    executable.write_text("content\n", encoding="utf-8")
    executable.chmod(0o644)
    regular_hash = runner._hash_files((executable,), tmp_path)
    executable.chmod(0o755)
    assert runner._hash_files((executable,), tmp_path) != regular_hash


def test_interrupted_shard_records_active_and_unrun_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner_module()
    report = tmp_path / "interrupted.json"
    report.write_text(
        json.dumps({"completed": ["stale"], "failed": [], "unrun": []}),
        encoding="utf-8",
    )
    commands = (
        runner.CommandSpec(
            "active",
            ("active", f"--agent-flow-report={report}"),
            pytest_report=True,
        ),
        runner.CommandSpec("later", ("later",)),
    )
    monkeypatch.setattr(runner, "_commands_for_shard", lambda *_args: commands)

    def interrupt(*_args, **_kwargs):
        assert not report.exists()
        report.write_text(
            json.dumps({"completed": ["passed"], "failed": ["failed"], "unrun": ["pending"]}),
            encoding="utf-8",
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.subprocess, "run", interrupt)
    state = {"shards": {"fast": {}}}

    with pytest.raises(KeyboardInterrupt):
        runner._run_shard("fast", tmp_path, (), state)

    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert stored["shards"]["fast"]["status"] == "interrupted"
    assert stored["shards"]["fast"]["results"][0]["status"] == "interrupted"
    assert stored["shards"]["fast"]["results"][0]["tests"]["failed"] == ["failed"]
    assert stored["shards"]["fast"]["unrun_commands"] == ["later"]


def test_default_run_id_is_unique_per_parallel_shard_invocation() -> None:
    runner = _runner_module()

    first = runner._default_run_id("targeted")
    second = runner._default_run_id("targeted")

    assert first != second
    assert f"-targeted-{os.getpid()}-" in first
    assert runner._validate_run_id(first) == first

def test_run_id_cannot_escape_the_artifact_root() -> None:
    runner = _runner_module()

    for value in ("../escape", "/tmp/escape", ".."):
        with pytest.raises(SystemExit, match="safe path segment"):
            runner._validate_run_id(value)


def test_changed_file_cannot_escape_the_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _runner_module()
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    for value in ("../outside/package.json", "/tmp/outside/package.json"):
        with pytest.raises(SystemExit, match="safe workspace-relative path"):
            runner._validate_changed_files((value,))
    with pytest.raises(SystemExit, match="workspace-relative path"):
        runner._validate_test_nodeids(("../../outside/test_bad.py::test_bad",))
