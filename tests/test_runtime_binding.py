from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_flow.artifact import ACTIVE_MARKER, create_run, mark_inactive
from agent_flow.core.runtime_binding import bind_run_runtime
from agent_flow.core.state import RunRequest, start_run


def test_bind_run_runtime_writes_every_byte_after_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    run_path = tmp_path / "runs" / "same-run"
    run_path.mkdir(parents=True)
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    real_write = os.write
    write_count = 0

    def short_write(descriptor: int, content: memoryview) -> int:
        nonlocal write_count
        write_count += 1
        length = max(1, len(content) // 2)
        return real_write(descriptor, content[:length])

    monkeypatch.setattr("agent_flow.core.runtime_binding.os.write", short_write)

    target = bind_run_runtime(run_path, "a" * 64)

    assert write_count > 1
    assert json.loads(target.read_text(encoding="utf-8"))["runtime_digest"] == "a" * 64
    assert not list(target.parent.glob("*.tmp"))


def test_agent_flow_home_takes_precedence_over_legacy_shared_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    legacy_home = tmp_path / "legacy-home"
    run_path = tmp_path / "runs" / "same-run"
    run_path.mkdir(parents=True)
    monkeypatch.setenv("AGENT_FLOW_HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(legacy_home))

    target = bind_run_runtime(run_path, "a" * 64)

    assert target.parent == home / "run-bindings"
    assert not (legacy_home / "run-bindings").exists()


def test_bind_run_runtime_write_failure_cleans_staging_and_allows_new_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    run_path = tmp_path / "runs" / "same-run"
    run_path.mkdir(parents=True)
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    real_write = os.write
    write_count = 0

    def fail_after_short_write(descriptor: int, content: memoryview) -> int:
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            return real_write(descriptor, content[:7])
        raise OSError("binding write failed")

    monkeypatch.setattr(
        "agent_flow.core.runtime_binding.os.write", fail_after_short_write
    )

    with pytest.raises(OSError, match="binding write failed"):
        bind_run_runtime(run_path, "a" * 64)

    binding_root = shared_home / "run-bindings"
    assert not list(binding_root.iterdir())

    monkeypatch.setattr("agent_flow.core.runtime_binding.os.write", real_write)
    target = bind_run_runtime(run_path, "b" * 64)

    assert json.loads(target.read_text(encoding="utf-8"))["runtime_digest"] == "b" * 64
    assert not list(binding_root.glob("*.tmp"))


def test_create_run_requires_runtime_digest_without_publishing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "missing-digest"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))

    with pytest.raises(TypeError, match="hook_runtime_digest"):
        create_run(state_root, "default", "task", run_id="missing-digest")

    assert not run_path.exists()
    assert not (run_path / ACTIVE_MARKER).exists()
    assert not (shared_home / "run-bindings").exists()


@pytest.mark.parametrize(
    "digest",
    (None, "", "a" * 63, "g" * 64, "A" * 64),
)
def test_create_run_rejects_invalid_runtime_digest_without_publishing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: object
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "invalid-digest"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))

    with pytest.raises(ValueError, match="invalid hook runtime digest"):
        create_run(
            state_root,
            "default",
            "task",
            run_id="invalid-digest",
            hook_runtime_digest=digest,  # type: ignore[arg-type]
        )

    assert not run_path.exists()
    assert not (run_path / ACTIVE_MARKER).exists()
    assert not (shared_home / "run-bindings").exists()


def test_create_run_does_not_publish_active_marker_while_binding_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "ordered"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    real_bind = bind_run_runtime
    binding_started = threading.Event()
    allow_binding = threading.Event()

    def delayed_binding(path: Path, digest: str) -> Path:
        binding_started.set()
        if not allow_binding.wait(timeout=5):
            raise TimeoutError("binding test did not resume")
        return real_bind(path, digest)

    monkeypatch.setattr("agent_flow.artifact.bind_run_runtime", delayed_binding)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            create_run,
            state_root,
            "default",
            "task",
            run_id="ordered",
            hook_runtime_digest="c" * 64,
        )
        assert binding_started.wait(timeout=5)
        try:
            assert not (run_path / ACTIVE_MARKER).exists()
            assert not (shared_home / "run-bindings").exists()
        finally:
            allow_binding.set()
        created = future.result(timeout=5)

    assert created == run_path
    assert (run_path / ACTIVE_MARKER).is_file()
    assert len(list((shared_home / "run-bindings").iterdir())) == 1


def test_start_run_binds_runtime_before_publishing_running_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "default" / "ordered"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    real_bind = bind_run_runtime
    binding_started = threading.Event()
    allow_binding = threading.Event()

    def delayed_binding(path: Path, digest: str) -> Path:
        binding_started.set()
        if not allow_binding.wait(timeout=5):
            raise TimeoutError("binding test did not resume")
        return real_bind(path, digest)

    monkeypatch.setattr("agent_flow.core.state.bind_run_runtime", delayed_binding)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            start_run,
            root=state_root,
            request=RunRequest(
                workflow_id="default",
                task="task",
                adapter="generic",
                profile="generic",
                hook_runtime_digest="d" * 64,
                run_id="ordered",
            ),
        )
        assert binding_started.wait(timeout=5)
        try:
            assert not (run_path / "manifest.json").exists()
        finally:
            allow_binding.set()
        state = future.result(timeout=5)

    assert state.run_dir == run_path
    assert (run_path / "manifest.json").is_file()
    assert len(list((shared_home / "run-bindings").iterdir())) == 1


def test_start_run_manifest_failure_removes_runtime_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "default" / "failed"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))

    def fail_manifest(*args, **kwargs) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr("agent_flow.core.state._write_json", fail_manifest)

    with pytest.raises(OSError, match="manifest write failed"):
        start_run(
            root=state_root,
            request=RunRequest(
                workflow_id="default",
                task="task",
                adapter="generic",
                profile="generic",
                hook_runtime_digest="e" * 64,
                run_id="failed",
            ),
        )

    assert not run_path.exists()
    assert not list((shared_home / "run-bindings").iterdir())


def test_create_run_marker_failure_removes_binding_after_run_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "failed"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    real_write_text = Path.write_text

    def fail_active_marker(path: Path, data: str, *args, **kwargs) -> int:
        if path == run_path / ACTIVE_MARKER:
            raise OSError("active marker failed")
        return real_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_active_marker)

    with pytest.raises(OSError, match="active marker failed"):
        create_run(
            state_root,
            "default",
            "task",
            run_id="failed",
            hook_runtime_digest="d" * 64,
        )

    assert not run_path.exists()
    assert not list((shared_home / "run-bindings").iterdir())


def test_create_run_cleanup_failure_preserves_binding_without_active_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    state_root = tmp_path / "state"
    run_path = state_root / ".agent-flow" / "runs" / "failed"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    real_write_text = Path.write_text

    def fail_active_marker(path: Path, data: str, *args, **kwargs) -> int:
        if path == run_path / ACTIVE_MARKER:
            raise OSError("active marker failed")
        return real_write_text(path, data, *args, **kwargs)

    def fail_run_cleanup(path: Path) -> None:
        raise OSError(f"cleanup failed: {path}")

    monkeypatch.setattr(Path, "write_text", fail_active_marker)
    monkeypatch.setattr("agent_flow.artifact.shutil.rmtree", fail_run_cleanup)

    with pytest.raises(OSError, match="active marker failed"):
        create_run(
            state_root,
            "default",
            "task",
            run_id="failed",
            hook_runtime_digest="e" * 64,
        )

    assert run_path.is_dir()
    assert not (run_path / ACTIVE_MARKER).exists()
    assert len(list((shared_home / "run-bindings").iterdir())) == 1


def test_mark_inactive_marker_failure_preserves_runtime_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    run_path = create_run(
        tmp_path / "state",
        "default",
        "task",
        run_id="active",
        hook_runtime_digest="f" * 64,
    )
    marker = run_path / ACTIVE_MARKER
    binding = next((shared_home / "run-bindings").iterdir())
    real_unlink = Path.unlink

    def fail_active_marker_unlink(path: Path, *args, **kwargs) -> None:
        if path == marker:
            raise OSError("marker unlink failed")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as marker_failure:
        marker_failure.setattr(Path, "unlink", fail_active_marker_unlink)
        with pytest.raises(OSError, match="marker unlink failed"):
            mark_inactive(run_path)

    assert marker.is_file()
    assert binding.is_file()

    mark_inactive(run_path)
    assert not marker.exists()
    assert not binding.exists()


def test_mark_inactive_binding_failure_leaves_only_stale_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_home = tmp_path / "shared"
    monkeypatch.setenv("AGENT_FLOW_SHARED_HOME", str(shared_home))
    run_path = create_run(
        tmp_path / "state",
        "default",
        "task",
        run_id="active",
        hook_runtime_digest="1" * 64,
    )
    binding = next((shared_home / "run-bindings").iterdir())

    def fail_binding_cleanup(path: Path) -> None:
        raise OSError(f"binding unlink failed: {path}")

    with monkeypatch.context() as binding_failure:
        binding_failure.setattr(
            "agent_flow.artifact.unbind_run_runtime", fail_binding_cleanup
        )
        with pytest.raises(OSError, match="binding unlink failed"):
            mark_inactive(run_path)

    assert not (run_path / ACTIVE_MARKER).exists()
    assert binding.is_file()

    mark_inactive(run_path)
    assert not binding.exists()
