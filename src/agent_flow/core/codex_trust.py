from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import secrets
import signal
import shutil
import stat as stat_module
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib as _toml
except ModuleNotFoundError:
    import tomli as _toml

from agent_flow.cli_detect import detect_host_from_env


_MANAGED_HOOK_SCRIPTS = (
    "comment-checker.py",
    "guard-protected-branch.sh",
    "guard-worktree-write.py",
    "guard-worktree.sh",
    "show-phase-status.sh",
)
_WRITE_TOOL_MATCHER = (
    "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|"
    "edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$"
)
_EXPECTED_MANAGED_HOOK_PROJECTION = tuple(
    sorted(
        (
            ("PostToolUse", _WRITE_TOOL_MATCHER, "command", "comment-checker.py"),
            ("PreToolUse", "Bash", "command", "guard-protected-branch.sh"),
            ("PreToolUse", "Bash", "command", "guard-worktree-write.py"),
            ("PreToolUse", "Bash", "command", "guard-worktree.sh"),
            (
                "PreToolUse",
                _WRITE_TOOL_MATCHER,
                "command",
                "guard-worktree-write.py",
            ),
            ("Stop", "", "command", "show-phase-status.sh"),
        )
    )
)
_MANAGED_HOOK_CONTRACT_VERSION = 2
_MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION = 2
_MANAGED_HOOK_VERIFIER_TOKEN_SHA256 = (
    "6a31e12ab534093eeb9beffb0c9cb983e0893497cb2c70a7ef3dd2edb8e43191"
)
_MANAGED_HOOK_CONFIG_PATHS = (
    ".Codex/hooks.json",
    ".claude/settings.json",
    ".codex/hooks.json",
)
_MAX_INSTALLED_KIT_BYTES = 2 * 1024 * 1024
_TRUST_TRANSACTION_VERSION = 2
_TRUST_RECEIPT_VERSION = 1
_TRUST_TRANSACTION_FILE = "codex-worktree-trust.json"
_TRUST_LOCK_FILE = ".config.toml.agent-flow.lock.json"
_TRUST_TRANSACTION_DIR_PREFIX = ".config.toml.agent-flow-"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_RECEIPT_BYTES = 4 * _MAX_CONFIG_BYTES
_TRUST_CRASH_ENV = "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_WRITE"
_TRUST_DISPLACE_CRASH_ENV = (
    "AGENT_FLOW_TEST_HARD_KILL_AFTER_WORKTREE_CODEX_TRUST_DISPLACE"
)


def ensure_codex_worktree_hook_trust(
    *, leader_root: Path, worktree_root: Path
) -> dict[str, int | str]:
    _recover_interrupted_trust_transaction(leader_root)
    if os.environ.get("AGENT_FLOW_SKIP_CODEX_TRUST") == "1":
        return {"status": "skipped", "hooks": 0}

    codex_binary = _resolve_codex_binary()
    if codex_binary is None:
        if detect_host_from_env(os.environ) == "codex":
            raise RuntimeError(
                "blocked: Codex worktree hook trust unavailable: Codex CLI not found"
            )
        return {"status": "unavailable", "hooks": 0}

    config_path = _resolve_codex_config_path()
    if config_path is None:
        raise RuntimeError(
            "blocked: Codex worktree hook trust unavailable: CODEX_HOME or HOME is required"
        )
    recover_codex_config_transaction_lock(config_path)
    hook_contract = _managed_project_hook_contract(leader_root, worktree_root)

    original = _snapshot_config(config_path)
    expected = original
    created_directories: list[Path] = []
    transaction = _create_trust_transaction(leader_root, config_path, original)
    try:
        project_header = f"[projects.{_toml_string(str(worktree_root))}]"
        expected = _write_config(
            config_path,
            expected,
            _semantic_upsert_toml_value(
                _config_text(expected, config_path),
                project_header,
                "trust_level",
                '"trusted"',
                ("projects", str(worktree_root)),
                "trusted",
            ),
            created_directories,
            transaction,
            before_publish=lambda: _assert_managed_hook_contract_unchanged(
                hook_contract
            ),
        )

        _assert_managed_hook_contract_unchanged(hook_contract)
        discovered = _managed_project_hooks(
            _query_codex_hooks(codex_binary, worktree_root),
            leader_root,
            worktree_root,
            hook_contract,
        )

        trusted_config = _config_text(expected, config_path)
        for hook in discovered:
            hook_header = f"[hooks.state.{_toml_string(hook['key'])}]"
            trusted_config = _semantic_upsert_toml_value(
                trusted_config,
                hook_header,
                "trusted_hash",
                _toml_string(hook["current_hash"]),
                ("hooks", "state", hook["key"]),
                hook["current_hash"],
            )
        expected = _write_config(
            config_path,
            expected,
            trusted_config,
            created_directories,
            transaction,
            before_publish=lambda: _assert_managed_hook_contract_unchanged(
                hook_contract
            ),
        )

        _assert_managed_hook_contract_unchanged(hook_contract)
        verification_result = _query_codex_hooks(codex_binary, worktree_root)
        _assert_managed_hook_contract_unchanged(hook_contract)
        verified = _managed_project_hooks(
            verification_result,
            leader_root,
            worktree_root,
            hook_contract,
        )
        _assert_hooks_trusted(discovered, verified)
        _complete_trust_transaction(transaction)
        return {"status": "trusted", "hooks": len(discovered)}
    except BaseException as exc:
        rollback_errors: list[str] = []
        if transaction["started"]:
            try:
                _recover_interrupted_trust_transaction(
                    leader_root, allow_owner_pid=os.getpid()
                )
            except (OSError, RuntimeError) as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        else:
            _restore_config(config_path, original, expected, rollback_errors)
        for directory in reversed(created_directories):
            _remove_empty_directory(directory, rollback_errors)
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; Codex config rollback failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise


def load_managed_hook_command_policy(leader_root: Path) -> dict[str, Any]:
    return _installed_managed_hook_script_contract(leader_root)


def trusted_managed_hook_command(
    policy: dict[str, Any], command: Any
) -> dict[str, str] | None:
    if (
        not isinstance(policy, dict)
        or not isinstance(policy.get("leader_root"), Path)
        or not isinstance(policy.get("script_hashes"), dict)
    ):
        raise RuntimeError("blocked: managed hook command policy is unavailable")
    verifier = _managed_hook_verifier_details(command)
    if verifier is None:
        return None
    script_path, command_hash = verifier
    for script_name in _MANAGED_HOOK_SCRIPTS:
        relative = f".agent-flow/scripts/hooks/{script_name}"
        expected_path = policy["leader_root"].joinpath(*relative.split("/"))
        if (
            script_path.as_posix() == expected_path.as_posix()
            and command_hash == policy["script_hashes"].get(relative)
        ):
            return {
                "script_name": script_name,
                "command_path": str(expected_path),
                "sha256": command_hash,
            }
    return None


def _resolve_codex_binary() -> str | None:
    configured = os.environ.get("CODEX_CLI_PATH", "").strip()
    candidates = (
        (configured,)
        if configured
        else ("/Applications/Codex.app/Contents/Resources/codex", "codex")
    )
    for candidate in candidates:
        executable = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if not executable:
            continue
        try:
            result = subprocess.run(
                (executable, "--version"),
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return executable
    return None


def _resolve_codex_config_path() -> Path | None:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    if configured_home:
        return Path(configured_home).expanduser().resolve(strict=False) / "config.toml"
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return Path(home).expanduser().resolve(strict=False) / ".codex/config.toml" if home else None


def _query_codex_hooks(codex_binary: str, worktree_root: Path) -> dict[str, Any]:
    try:
        process = subprocess.Popen(
            (codex_binary, "app-server", "--stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(
            f"blocked: Codex worktree hook discovery failed: {exc}"
        ) from exc

    responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()

    def read_responses() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    responses.put(value)
        except BaseException as exc:
            responses.put(exc)
        finally:
            responses.put(RuntimeError("Codex app-server exited before hooks/list completed"))

    reader = threading.Thread(target=read_responses, daemon=True)
    reader.start()
    try:
        _send_codex_request(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "agent-flow-worktree-bridge",
                        "title": None,
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            },
        )
        initialized = _wait_for_codex_response(responses, 1)
        if initialized.get("error") is not None:
            raise RuntimeError("Codex app-server initialize failed")
        _send_codex_request(
            process,
            {
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(worktree_root)]},
            },
        )
        response = _wait_for_codex_response(responses, 2)
        if response.get("error") is not None or not isinstance(response.get("result"), dict):
            raise RuntimeError("Codex app-server hooks/list failed")
        result = response["result"]
        if not isinstance(result.get("data"), list):
            raise RuntimeError("Codex app-server hooks/list returned an invalid schema")
        return result
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"blocked: Codex worktree hook discovery failed: {exc}"
        ) from exc
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def _send_codex_request(process: subprocess.Popen[str], request: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin is unavailable")
    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _wait_for_codex_response(
    responses: queue.Queue[dict[str, Any] | BaseException], request_id: int
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Codex app-server timed out")
        try:
            response = responses.get(timeout=remaining)
        except queue.Empty as exc:
            raise RuntimeError("Codex app-server timed out") from exc
        if isinstance(response, BaseException):
            raise RuntimeError(str(response)) from response
        if response.get("id") == request_id:
            return response


def _installed_managed_hook_script_contract(leader_root: Path) -> dict[str, Any]:
    kit_path = leader_root / ".agent-flow/kit.json"
    kit_snapshot = _snapshot_managed_hook_file(
        leader_root,
        kit_path,
        label="installed kit metadata",
        max_bytes=_MAX_INSTALLED_KIT_BYTES,
    )
    try:
        kit = json.loads(kit_snapshot["content"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("blocked: installed kit metadata is invalid") from exc
    if not isinstance(kit, dict):
        raise RuntimeError("blocked: installed kit metadata is invalid")
    if (
        not isinstance(kit.get("skill_plan_hash"), str)
        or re.fullmatch(r"[0-9a-f]{64}", kit["skill_plan_hash"]) is None
        or kit.get("managed_hook_contract_commitment_version")
        != _MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION
        or not isinstance(kit.get("managed_hook_contract_commitment"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", kit["managed_hook_contract_commitment"]
        )
        is None
    ):
        raise RuntimeError("blocked: installed managed hook commitment is invalid")
    normalized = _normalize_installed_managed_hook_contract(
        kit.get("managed_hook_contract")
    )
    commitment_payload = {
        "version": _MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION,
        "skill_plan_hash": kit["skill_plan_hash"],
        "configs": normalized["config_rows"],
        "scripts": normalized["script_rows"],
    }
    computed_commitment = hashlib.sha256(
        json.dumps(
            commitment_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if computed_commitment != kit["managed_hook_contract_commitment"]:
        raise RuntimeError(
            "blocked: installed managed hook commitment does not match provenance"
        )
    script_snapshots = [
        _snapshot_managed_hook_file(
            leader_root,
            leader_root.joinpath(*relative.split("/")),
            label=f"managed hook script {relative}",
            expected_hash=expected_hash,
            executable=True,
        )
        for relative, expected_hash, _mode in normalized["script_rows"]
    ]
    return {
        "config_hashes": {
            relative: expected_hash
            for relative, expected_hash in normalized["config_rows"]
        },
        "kit_snapshot": kit_snapshot,
        "leader_root": leader_root.resolve(strict=True),
        "script_hashes": {
            relative: expected_hash
            for relative, expected_hash, _mode in normalized["script_rows"]
        },
        "script_snapshots": script_snapshots,
    }


def _normalize_installed_managed_hook_contract(
    contract: object,
) -> dict[str, list[list[str]]]:
    if (
        not isinstance(contract, dict)
        or contract.get("version") != _MANAGED_HOOK_CONTRACT_VERSION
    ):
        raise RuntimeError("blocked: installed managed hook contract is invalid")

    def normalize(
        entries: object,
        expected_paths: tuple[str, ...],
        label: str,
        require_executable: bool = False,
    ) -> list[list[str]]:
        if not isinstance(entries, dict) or sorted(entries) != sorted(expected_paths):
            raise RuntimeError(
                f"blocked: installed managed hook {label} provenance is incomplete"
            )
        rows: list[list[str]] = []
        for relative in sorted(entries):
            entry = entries[relative]
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or (require_executable and entry.get("mode") != "executable")
            ):
                raise RuntimeError(
                    f"blocked: installed managed hook {label} provenance is invalid: {relative}"
                )
            row = [relative, entry["sha256"]]
            if require_executable:
                row.append("executable")
            rows.append(row)
        return rows

    script_paths = tuple(
        f".agent-flow/scripts/hooks/{name}" for name in _MANAGED_HOOK_SCRIPTS
    )
    return {
        "config_rows": normalize(
            contract.get("configs"), _MANAGED_HOOK_CONFIG_PATHS, "config"
        ),
        "script_rows": normalize(
            contract.get("scripts"),
            script_paths,
            "script",
            require_executable=True,
        ),
    }


def _snapshot_managed_hook_file(
    root: Path,
    path: Path,
    *,
    label: str,
    expected_hash: str | None = None,
    executable: bool = False,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        lexical_relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise RuntimeError(f"blocked: {label} escapes the leader install") from exc
    if not lexical_relative.parts:
        raise RuntimeError(f"blocked: {label} escapes the leader install")
    canonical_root = root.resolve(strict=True)
    absolute = canonical_root / lexical_relative
    relative = lexical_relative
    cursor = canonical_root
    for index, part in enumerate(relative.parts):
        cursor /= part
        metadata = _lstat_if_exists(cursor)
        final = index == len(relative.parts) - 1
        if (
            metadata is None
            or stat_module.S_ISLNK(metadata.st_mode)
            or (
                final
                and not stat_module.S_ISREG(metadata.st_mode)
            )
            or (
                not final
                and not stat_module.S_ISDIR(metadata.st_mode)
            )
        ):
            raise RuntimeError(
                f"blocked: {label} path is missing or unsafe: {cursor}"
            )
    before = absolute.lstat()
    descriptor = os.open(
        absolute,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(
                f"blocked: {label} must be a non-hard-linked regular file"
            )
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise RuntimeError(f"blocked: {label} exceeds the size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read()
        final_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = absolute.lstat()
    for current in (before, final_metadata, after):
        if (
            stat_module.S_ISLNK(current.st_mode)
            or not stat_module.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or stat_module.S_IMODE(current.st_mode)
            != stat_module.S_IMODE(metadata.st_mode)
            or current.st_size != metadata.st_size
            or current.st_mtime_ns != metadata.st_mtime_ns
            or current.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise RuntimeError(
                f"blocked: {label} changed while it was being authenticated"
            )
    mode = stat_module.S_IMODE(metadata.st_mode)
    if executable and os.name != "nt" and not mode & 0o111:
        raise RuntimeError(f"blocked: {label} is not executable")
    content_hash = hashlib.sha256(content).hexdigest()
    if expected_hash is not None and content_hash != expected_hash:
        raise RuntimeError(
            f"blocked: {label} does not match installed provenance"
        )
    return {
        "path": absolute,
        "content": content,
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
        "ctime_ns": metadata.st_ctime_ns,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": mode,
        "nlink": metadata.st_nlink,
        "size": metadata.st_size,
        "sha256": content_hash,
        "executable": executable,
        "max_bytes": max_bytes,
        "label": label,
    }


def _same_managed_hook_file_snapshot(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    return all(
        left[key] == right[key]
        for key in (
            "path",
            "dev",
            "ino",
            "ctime_ns",
            "mtime_ns",
            "mode",
            "nlink",
            "size",
            "sha256",
        )
    )


def _managed_project_hook_contract(
    leader_root: Path, worktree_root: Path
) -> dict[str, Any]:
    installed = _installed_managed_hook_script_contract(leader_root)
    config_specs = (
        (leader_root / ".Codex/hooks.json", ".Codex/hooks.json"),
        (leader_root / ".codex/hooks.json", ".codex/hooks.json"),
        (worktree_root / ".Codex/hooks.json", ".Codex/hooks.json"),
        (worktree_root / ".codex/hooks.json", ".codex/hooks.json"),
    )
    snapshots: list[dict[str, Any]] = []
    expected_hooks: list[dict[str, str]] | None = None
    for config_path, relative in config_specs:
        if any(
            _same_existing_path(snapshot["path"], config_path)
            for snapshot in snapshots
        ):
            continue
        metadata = _lstat_if_exists(config_path)
        if (
            metadata is None
            or stat_module.S_ISLNK(metadata.st_mode)
            or not stat_module.S_ISREG(metadata.st_mode)
        ):
            raise RuntimeError(
                f"blocked: bridged Codex hook config is missing or unsafe: {config_path}"
            )
        content = config_path.read_bytes()
        try:
            config = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"blocked: bridged Codex hook config is invalid: {config_path}"
            ) from exc
        parsed = _managed_hook_contract_from_config(config, config_path, installed)
        if parsed["projection"] != _EXPECTED_MANAGED_HOOK_PROJECTION:
            raise RuntimeError(
                "blocked: bridged Codex managed hook projection does not match "
                f"the exact hook contract: {config_path}"
            )
        projection_hash = hashlib.sha256(
            json.dumps(
                parsed["projection"],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if installed["config_hashes"].get(relative) != projection_hash:
            raise RuntimeError(
                "blocked: bridged Codex managed hook projection does not match "
                f"installed provenance: {config_path}"
            )
        if expected_hooks is None:
            expected_hooks = parsed["hooks"]
        snapshots.append({"path": config_path, "content": content})
    if expected_hooks is None or not snapshots:
        raise RuntimeError(
            "blocked: bridged Codex managed hook contract is unavailable"
        )
    contract = {
        "expected_identities": tuple(
            sorted(_managed_hook_identity(hook) for hook in expected_hooks)
        ),
        "installed": installed,
        "snapshots": snapshots,
    }
    _assert_managed_hook_contract_unchanged(contract)
    return contract


def _managed_hook_contract_from_config(
    config: object, config_path: Path, installed: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise RuntimeError(
            f"blocked: bridged Codex hook config is invalid: {config_path}"
        )
    raw_hooks = config.get("hooks")
    if not isinstance(raw_hooks, dict):
        raise RuntimeError(
            f"blocked: bridged Codex hook config has no managed hooks: {config_path}"
        )
    projection: list[tuple[str, str, str, str]] = []
    hooks: list[dict[str, str]] = []
    managed_matchers: set[tuple[str, str]] = set()
    for event, entries in raw_hooks.items():
        if not isinstance(event, str) or not isinstance(entries, list):
            raise RuntimeError(
                f"blocked: bridged Codex hook config has an invalid schema: {config_path}"
            )
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise RuntimeError(
                    f"blocked: bridged Codex hook config has an invalid schema: {config_path}"
                )
            if "matcher" in entry and not isinstance(entry["matcher"], str):
                raise RuntimeError(
                    f"blocked: bridged Codex hook config has an invalid matcher: {config_path}"
                )
            matcher = entry.get("matcher", "")
            managed_count = 0
            for hook in entry["hooks"]:
                if not isinstance(hook, dict):
                    raise RuntimeError(
                        f"blocked: bridged Codex hook config has an invalid schema: {config_path}"
                    )
                candidate_name = _managed_hook_candidate_name(hook.get("command"))
                trusted = trusted_managed_hook_command(installed, hook.get("command"))
                if trusted is None:
                    if candidate_name:
                        raise RuntimeError(
                            "blocked: bridged Codex managed hook command is not "
                            f"immutable: {config_path}"
                        )
                    continue
                script_name = trusted["script_name"]
                if hook.get("type") != "command":
                    raise RuntimeError(
                        "blocked: bridged Codex hook config has an invalid managed "
                        f"hook type: {config_path}"
                    )
                managed_count += 1
                projection.append((event, matcher, "command", script_name))
                hooks.append(
                    {
                        "script_name": script_name,
                        "command_path": trusted["command_path"],
                    }
                )
            if managed_count:
                matcher_key = (event, matcher)
                if matcher_key in managed_matchers:
                    raise RuntimeError(
                        "blocked: duplicate bridged Codex managed hook matcher: "
                        f"{config_path}"
                    )
                managed_matchers.add(matcher_key)
    return {"hooks": hooks, "projection": tuple(sorted(projection))}


def _managed_hook_identity(hook: dict[str, str]) -> str:
    return f"{hook['script_name']}\0{hook['command_path']}"


def _assert_managed_hook_contract_unchanged(contract: dict[str, Any]) -> None:
    installed_snapshots = (
        contract["installed"]["kit_snapshot"],
        *contract["installed"]["script_snapshots"],
    )
    for snapshot in installed_snapshots:
        current = _snapshot_managed_hook_file(
            contract["installed"]["leader_root"],
            snapshot["path"],
            label=snapshot["label"],
            expected_hash=snapshot["sha256"],
            executable=snapshot["executable"],
            max_bytes=snapshot["max_bytes"],
        )
        if not _same_managed_hook_file_snapshot(snapshot, current):
            raise RuntimeError(
                f"blocked: {snapshot['label']} changed during Codex hook trust"
            )
    for snapshot in contract["snapshots"]:
        path = snapshot["path"]
        metadata = _lstat_if_exists(path)
        if (
            metadata is None
            or stat_module.S_ISLNK(metadata.st_mode)
            or not stat_module.S_ISREG(metadata.st_mode)
            or path.read_bytes() != snapshot["content"]
        ):
            raise RuntimeError(
                f"blocked: bridged Codex hook config changed during trust discovery: {path}"
            )


def _managed_project_hooks(
    result: dict[str, Any],
    leader_root: Path,
    worktree_root: Path,
    contract: dict[str, Any],
) -> list[dict[str, str]]:
    _assert_managed_hook_contract_unchanged(contract)
    entry = next(
        (
            candidate
            for candidate in result["data"]
            if isinstance(candidate, dict) and candidate.get("cwd") == str(worktree_root)
        ),
        None,
    )
    if entry is None or not isinstance(entry.get("hooks"), list):
        raise RuntimeError("blocked: Codex worktree hook discovery omitted the worktree")
    if isinstance(entry.get("errors"), list) and entry["errors"]:
        raise RuntimeError(
            "blocked: Codex worktree hook discovery reported configuration errors"
        )

    source_paths = (
        leader_root / ".Codex/hooks.json",
        leader_root / ".codex/hooks.json",
        worktree_root / ".Codex/hooks.json",
        worktree_root / ".codex/hooks.json",
    )
    hooks: list[dict[str, str]] = []
    keys: set[str] = set()
    for hook in entry["hooks"]:
        if not isinstance(hook, dict) or not isinstance(hook.get("sourcePath"), str):
            continue
        if not any(
            _same_existing_path(source_path, hook["sourcePath"])
            for source_path in source_paths
        ):
            continue
        candidate_name = _managed_hook_candidate_name(hook.get("command"))
        trusted = trusted_managed_hook_command(
            contract["installed"], hook.get("command")
        )
        if trusted is None:
            if candidate_name:
                raise RuntimeError(
                    "blocked: Codex discovered a non-immutable agent-flow hook"
                )
            continue
        script_name = trusted["script_name"]
        key = hook.get("key")
        current_hash = hook.get("currentHash")
        trust_status = hook.get("trustStatus")
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(current_hash, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", current_hash) is None
            or not isinstance(trust_status, str)
        ):
            raise RuntimeError(
                "blocked: Codex worktree hook discovery returned invalid hook metadata"
            )
        if key in keys:
            raise RuntimeError(
                "blocked: Codex worktree hook discovery returned duplicate hook keys"
            )
        keys.add(key)
        hooks.append(
            {
                "key": key,
                "current_hash": current_hash,
                "trust_status": trust_status,
                "script_name": script_name,
                "command_path": trusted["command_path"],
            }
        )
    actual_identities = tuple(
        sorted(_managed_hook_identity(hook) for hook in hooks)
    )
    if actual_identities != contract["expected_identities"]:
        raise RuntimeError(
            "blocked: Codex worktree hook discovery returned an incomplete, extra, "
            "or duplicate managed hook set"
        )
    _assert_managed_hook_contract_unchanged(contract)
    return sorted(hooks, key=lambda hook: hook["key"])


def _assert_hooks_trusted(
    expected: list[dict[str, str]], actual: list[dict[str, str]]
) -> None:
    actual_by_key = {hook["key"]: hook for hook in actual}
    for hook in expected:
        verified = actual_by_key.get(hook["key"])
        if (
            verified is None
            or verified["current_hash"] != hook["current_hash"]
            or verified["trust_status"] != "trusted"
        ):
            raise RuntimeError(
                f"blocked: Codex worktree hook trust verification failed: {hook['key']}"
            )


def _managed_hook_candidate_name(command: Any) -> str:
    if not isinstance(command, str):
        return ""
    for match in re.finditer(
        r"(?:^|[\s'\"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'\"])", command
    ):
        try:
            decoded = base64.b64decode(match.group(1), validate=True)
            decoded_path = decoded.decode("utf-8")
            if base64.b64encode(decoded).decode("ascii") == match.group(1):
                script_name = Path(decoded_path).name
                if script_name in _MANAGED_HOOK_SCRIPTS:
                    return script_name
        except (UnicodeError, ValueError):
            pass
    if all(
        marker in command
        for marker in (
            "AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH",
            "agent-flow-managed-hook-",
            "descriptor execution unavailable",
        )
    ):
        return "__managed-verifier__"
    normalized = (
        _normalized_hook_command(command)
        .replace("\\", "/")
        .replace("'", "")
        .replace('"', "")
    )
    return next(
        (
            script_name
            for script_name in _MANAGED_HOOK_SCRIPTS
            if f"/.agent-flow/scripts/hooks/{script_name}" in normalized
            or f"/scripts/hooks/{script_name}" in normalized
        ),
        "",
    )


def _managed_hook_verifier_details(command: Any) -> tuple[Path, str] | None:
    prefix = "'/usr/bin/python3' -I -c "
    if not isinstance(command, str) or not command.startswith(prefix):
        return None
    match = re.search(r" '([A-Za-z0-9+/=]+)' '([0-9a-f]{64})'$", command)
    if match is None:
        return None
    try:
        verifier_token = command[len(prefix) : match.start()]
        if (
            hashlib.sha256(verifier_token.encode("utf-8")).hexdigest()
            != _MANAGED_HOOK_VERIFIER_TOKEN_SHA256
        ):
            return None
        decoded = base64.b64decode(match.group(1), validate=True)
        if base64.b64encode(decoded).decode("ascii") != match.group(1):
            return None
        script_path = Path(decoded.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return script_path, match.group(2)


def _normalized_hook_command(command: Any) -> str:
    normalized = str(command).strip()
    if normalized.startswith("'") and normalized.endswith("'"):
        normalized = normalized[1:-1].replace("'\\''", "'")
    elif normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    return normalized


def _config_text(snapshot: dict[str, Any], config_path: Path) -> str:
    if not snapshot["exists"]:
        return ""
    try:
        return snapshot["content"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"blocked: Codex config is not valid UTF-8: {config_path}"
        ) from exc


def _render_toml_value(
    text: str, table_header: str, key: str, value: str
) -> tuple[str, bool, bool]:
    table_pattern = re.compile(
        rf"(^|\n)[ \t]*{re.escape(table_header)}[ \t]*(?:#.*)?\r?\n"
        rf"([\s\S]*?)(?=\n[ \t]*\[[^\n]+\]|$)"
    )
    key_pattern = re.compile(rf"(^|\n)[ \t]*{re.escape(key)}[ \t]*=.*(?=\r?\n|$)")
    match = table_pattern.search(text)
    if match is None:
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{separator}{table_header}\n{key} = {value}\n", False, False

    leading, body = match.groups()
    key_matched = key_pattern.search(body) is not None
    if key_matched:
        next_body = key_pattern.sub(
            lambda key_match: f"{key_match.group(1)}{key} = {value}", body, count=1
        )
    else:
        separator = "" if not body or body.endswith("\n") else "\n"
        next_body = f"{body}{separator}{key} = {value}\n"
    rendered = (
        text[: match.start()]
        + f"{leading}{table_header}\n{next_body}"
        + text[match.end() :]
    )
    return rendered, True, key_matched


def _semantic_upsert_toml_value(
    text: str,
    table_header: str,
    key: str,
    value: str,
    table_path: tuple[str, ...],
    expected_value: str,
) -> str:
    table_exists, key_exists, value_matches = _inspect_toml_target(
        text, table_path, key, expected_value
    )
    if value_matches:
        return text
    rendered, table_matched, key_matched = _render_toml_value(
        text, table_header, key, value
    )
    if table_exists != table_matched or key_exists != key_matched:
        raise RuntimeError(
            "blocked: equivalent Codex config TOML target cannot be edited losslessly"
        )
    candidate = _inspect_toml_target(rendered, table_path, key, expected_value)
    if candidate != (True, True, True):
        raise RuntimeError(
            "blocked: rendered Codex config does not satisfy semantic trust target"
        )
    return rendered


def _inspect_toml_target(
    text: str,
    table_path: tuple[str, ...],
    key: str,
    expected_value: str,
) -> tuple[bool, bool, bool]:
    try:
        cursor: Any = _toml.loads(text)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("blocked: Codex config is not valid TOML") from exc
    table_exists = True
    for segment in table_path:
        if not isinstance(cursor, dict) or segment not in cursor:
            table_exists = False
            break
        cursor = cursor[segment]
    table_exists = table_exists and isinstance(cursor, dict)
    key_exists = table_exists and key in cursor
    value_matches = key_exists and cursor[key] == expected_value
    return table_exists, key_exists, value_matches


def _write_config(
    config_path: Path,
    expected: dict[str, Any],
    next_text: str,
    created_directories: list[Path],
    transaction: dict[str, Any],
    *,
    before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not next_text.endswith("\n"):
        next_text += "\n"
    next_content = next_text.encode("utf-8")
    if expected["exists"] and expected["content"] == next_content:
        return expected
    if len(next_content) > _MAX_CONFIG_BYTES:
        raise RuntimeError(
            "blocked: Codex config exceeds the 2 MiB trust transaction limit"
        )
    _assert_config_unchanged(config_path, expected)
    _ensure_config_parent(config_path, created_directories)
    if not transaction["started"]:
        transaction.update(
            acquire_codex_config_transaction_lock(
                leader_root=transaction["leader_root"],
                config_path=config_path,
                journal_path=transaction["transaction_path"],
                original=transaction["original"],
                created_directories=transaction["created_directories"],
            )
        )
    next_snapshot = {
        "exists": True,
        "content": next_content,
        "mode": int(expected["mode"]) if expected["exists"] else 0o600,
    }
    round_state = _prepare_trust_config_write(
        transaction, expected, next_snapshot
    )
    if before_publish is not None:
        before_publish()
    _publish_trust_config_round(transaction, round_state)
    transaction["mutation_count"] += 1
    if (
        transaction["mutation_count"] == 1
        and os.environ.get(_TRUST_CRASH_ENV) == "1"
    ):
        os.kill(os.getpid(), signal.SIGKILL)
    return next_snapshot


def _create_trust_transaction(
    leader_root: Path, config_path: Path, original: dict[str, Any]
) -> dict[str, Any]:
    canonical_leader = leader_root.resolve(strict=True)
    canonical_config = config_path.resolve(strict=False)
    return {
        "leader_root": canonical_leader,
        "config_path": canonical_config,
        "original": original,
        "created_directories": _missing_config_parents(canonical_config),
        "transaction_path": _trust_transaction_path(canonical_leader),
        "started": False,
        "mutation_count": 0,
        "journal_bytes": None,
    }


def acquire_codex_config_transaction_lock(
    *,
    leader_root: Path,
    config_path: Path,
    journal_path: Path,
    original: dict[str, Any],
    created_directories: list[Path] | None = None,
) -> dict[str, Any]:
    canonical_leader = leader_root.resolve(strict=True)
    canonical_config = config_path.resolve(strict=False)
    canonical_journal = journal_path.resolve(strict=False)
    if (
        not isinstance(original, dict)
        or not isinstance(original.get("exists"), bool)
        or not _valid_mode(original.get("mode"))
        or (
            original["exists"]
            and (
                not isinstance(original.get("content"), bytes)
                or len(original["content"]) > _MAX_CONFIG_BYTES
            )
        )
        or (not original["exists"] and original.get("content") is not None)
    ):
        raise RuntimeError("blocked: invalid Codex config transaction snapshot")
    recover_codex_config_transaction_lock(canonical_config)
    missing_directories = _missing_config_parents(canonical_config)
    actual_created_directories: list[Path] = []
    _ensure_config_parent(canonical_config, actual_created_directories)
    _assert_config_unchanged(canonical_config, original)
    token = secrets.token_hex(16)
    receipt_path = _codex_config_lock_path(canonical_config)
    transaction_dir = _codex_config_transaction_dir(canonical_config, token)
    original_path = transaction_dir / "original"
    rollback_path = transaction_dir / "rollback"
    normalized_directories = list(
        dict.fromkeys(
            directory.resolve(strict=False)
            for directory in [*(created_directories or []), *missing_directories]
        )
    )
    journal = _trust_journal_payload(
        status="pending",
        leader_root=canonical_leader,
        config_path=canonical_config,
        receipt_path=receipt_path,
        token=token,
        created_directories=normalized_directories,
    )
    journal_bytes = _json_bytes(journal)
    _durable_create_journal(canonical_journal, journal_bytes)
    receipt = {
        "version": _TRUST_RECEIPT_VERSION,
        "status": "open",
        "owner_pid": os.getpid(),
        "transaction_token": token,
        "leader_root": str(canonical_leader),
        "journal_path": str(canonical_journal),
        "config_path": str(canonical_config),
        "receipt_path": str(receipt_path),
        "transaction_dir": str(transaction_dir),
        "original_path": str(original_path) if original["exists"] else None,
        "rollback_path": str(rollback_path),
        "original": _encoded_original_snapshot(original),
        "created_directories": [str(path) for path in normalized_directories],
        "rounds": [],
    }
    receipt_bytes = _json_bytes(receipt)
    receipt_created = False
    try:
        _durable_create_journal(receipt_path, receipt_bytes)
        receipt_created = True
        transaction_dir.mkdir(mode=0o700)
        _fsync_directory(transaction_dir.parent)
        if original["exists"]:
            _write_durable_temporary(
                original_path, original["content"], int(original["mode"])
            )
            _fsync_directory(transaction_dir)
        authenticated = _trust_journal_payload(
            status="authenticated",
            leader_root=canonical_leader,
            config_path=canonical_config,
            receipt_path=receipt_path,
            token=token,
            created_directories=normalized_directories,
        )
        authenticated_bytes = _json_bytes(authenticated)
        _durable_replace_journal(
            canonical_journal, journal_bytes, authenticated_bytes
        )
        return {
            "leader_root": canonical_leader,
            "config_path": canonical_config,
            "transaction_path": canonical_journal,
            "created_directories": normalized_directories,
            "started": True,
            "mutation_count": 0,
            "journal_bytes": authenticated_bytes,
            "receipt": receipt,
            "receipt_bytes": receipt_bytes,
            "receipt_path": receipt_path,
            "transaction_dir": transaction_dir,
        }
    except BaseException as exc:
        if receipt_created:
            try:
                _recover_codex_receipt(
                    receipt, receipt_bytes, allow_owner_pid=os.getpid()
                )
            except (OSError, RuntimeError):
                pass
        else:
            try:
                _remove_journal_if_unchanged(canonical_journal, journal_bytes)
            except (OSError, RuntimeError):
                pass
        if isinstance(exc, FileExistsError):
            raise RuntimeError(
                f"blocked: Codex config transaction lock is already held: {receipt_path}"
            ) from exc
        raise


def _prepare_trust_config_write(
    transaction: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    index = len(transaction["receipt"]["rounds"]) + 1
    candidate_path = transaction["transaction_dir"] / f"candidate-{index}"
    displaced_path = transaction["transaction_dir"] / f"displaced-{index}"
    _write_durable_temporary(candidate_path, after["content"], int(after["mode"]))
    _fsync_directory(transaction["transaction_dir"])
    round_state = {
        "index": index,
        "before": _snapshot_state(before),
        "after": _snapshot_state(after),
        "candidate_path": str(candidate_path),
        "displaced_path": str(displaced_path),
    }
    _replace_trust_receipt(
        transaction,
        {
            **transaction["receipt"],
            "rounds": [*transaction["receipt"]["rounds"], round_state],
        },
    )
    return round_state


def _publish_trust_config_round(
    transaction: dict[str, Any], round_state: dict[str, Any]
) -> None:
    config_path = transaction["config_path"]
    before = round_state["before"]
    displaced_path = Path(round_state["displaced_path"])
    candidate_path = Path(round_state["candidate_path"])
    if before["exists"]:
        config_path.rename(displaced_path)
        _fsync_directory(config_path.parent)
        _fsync_directory(transaction["transaction_dir"])
        if os.environ.get(_TRUST_DISPLACE_CRASH_ENV) == "1":
            os.kill(os.getpid(), signal.SIGKILL)
        displaced = _snapshot_config(displaced_path)
        if not _snapshot_states_equal(_snapshot_state(displaced), before):
            _restore_external_displacement(config_path, displaced_path, displaced)
            raise RuntimeError(
                "blocked: Codex config changed at the no-clobber publish boundary"
            )
    elif _lstat_if_exists(config_path) is not None:
        raise RuntimeError(
            "blocked: Codex config changed at the no-clobber publish boundary"
        )
    try:
        os.link(candidate_path, config_path)
    except FileExistsError as exc:
        raise RuntimeError(
            "blocked: Codex config changed at the no-clobber publish boundary"
        ) from exc
    _fsync_directory(config_path.parent)
    applied = _snapshot_config(config_path)
    if not _snapshot_states_equal(
        _snapshot_state(applied), round_state["after"]
    ):
        raise RuntimeError(
            "blocked: Codex config changed immediately after no-clobber publish"
        )


def _restore_external_displacement(
    config_path: Path, displaced_path: Path, displaced: dict[str, Any]
) -> None:
    if _lstat_if_exists(config_path) is None:
        os.link(displaced_path, config_path)
        _fsync_directory(config_path.parent)
    current = _snapshot_config(config_path)
    if not _snapshot_states_equal(
        _snapshot_state(current), _snapshot_state(displaced)
    ):
        raise RuntimeError(
            f"blocked: concurrent Codex config bytes retained at {displaced_path}"
        )


def _complete_trust_transaction(transaction: dict[str, Any]) -> None:
    if not transaction["started"]:
        return
    rounds = transaction["receipt"]["rounds"]
    if rounds:
        current = _snapshot_config(transaction["config_path"])
        if not _snapshot_states_equal(
            _snapshot_state(current), rounds[-1]["after"]
        ):
            raise RuntimeError(
                "blocked: Codex config changed before trust transaction commit"
            )
    _replace_trust_receipt(
        transaction, {**transaction["receipt"], "status": "committed"}
    )
    _cleanup_codex_transaction(
        transaction["receipt"],
        transaction["receipt_bytes"],
        transaction["journal_bytes"],
    )
    transaction["started"] = False
    transaction["journal_bytes"] = None


def _recover_interrupted_trust_transaction(
    leader_root: Path, *, allow_owner_pid: int | None = None
) -> bool:
    canonical_leader = leader_root.resolve(strict=True)
    transaction_path = _trust_transaction_path(canonical_leader)
    metadata = _lstat_if_exists(transaction_path)
    if metadata is None:
        return False
    if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISREG(
        metadata.st_mode
    ):
        raise RuntimeError(
            f"blocked: unsafe Codex worktree trust transaction: {transaction_path}"
        )
    content = transaction_path.read_bytes()
    journal = _parse_trust_journal(content, canonical_leader, transaction_path)
    receipt_path = Path(journal["receipt_path"])
    receipt_metadata = _lstat_if_exists(receipt_path)
    if receipt_metadata is None:
        if journal["status"] != "pending":
            raise RuntimeError(
                "blocked: authenticated Codex config receipt is missing"
            )
        _remove_journal_if_unchanged(transaction_path, content)
        return True
    if (
        stat_module.S_ISLNK(receipt_metadata.st_mode)
        or not stat_module.S_ISREG(receipt_metadata.st_mode)
        or receipt_metadata.st_nlink != 1
    ):
        raise RuntimeError(
            f"blocked: unsafe Codex config transaction receipt: {receipt_path}"
        )
    receipt_bytes = receipt_path.read_bytes()
    receipt = _parse_trust_receipt(
        receipt_bytes,
        config_path=Path(journal["config_path"]),
        leader_root=canonical_leader,
        journal_path=transaction_path,
        token=journal["transaction_token"],
    )
    _recover_codex_receipt(
        receipt,
        receipt_bytes,
        allow_owner_pid=allow_owner_pid,
        journal_bytes=content,
    )
    return True


def recover_codex_config_transaction_lock(
    config_path: Path, *, allow_owner_pid: int | None = None
) -> bool:
    canonical_config = config_path.resolve(strict=False)
    receipt_path = _codex_config_lock_path(canonical_config)
    metadata = _lstat_if_exists(receipt_path)
    if metadata is None:
        return False
    if (
        stat_module.S_ISLNK(metadata.st_mode)
        or not stat_module.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(
            f"blocked: unsafe Codex config transaction receipt: {receipt_path}"
        )
    receipt_bytes = receipt_path.read_bytes()
    receipt = _parse_trust_receipt(
        receipt_bytes, config_path=canonical_config
    )
    _recover_codex_receipt(
        receipt, receipt_bytes, allow_owner_pid=allow_owner_pid
    )
    return True


def release_codex_config_transaction_lock(
    transaction: dict[str, Any], *, commit: bool = False
) -> None:
    if not transaction.get("started"):
        return
    if commit:
        _complete_trust_transaction(transaction)
        return
    _recover_codex_receipt(
        transaction["receipt"],
        transaction["receipt_bytes"],
        allow_owner_pid=os.getpid(),
        journal_bytes=transaction["journal_bytes"],
    )
    transaction["started"] = False
    transaction["journal_bytes"] = None


def _trust_transaction_path(leader_root: Path) -> Path:
    result = subprocess.run(
        ("git", "rev-parse", "--git-common-dir"),
        cwd=leader_root,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "blocked: git common directory is unavailable for Codex worktree trust recovery"
        )
    candidate = Path(result.stdout.strip())
    common_dir = (
        candidate.resolve(strict=True)
        if candidate.is_absolute()
        else (leader_root / candidate).resolve(strict=True)
    )
    common_metadata = common_dir.lstat()
    if stat_module.S_ISLNK(common_metadata.st_mode) or not stat_module.S_ISDIR(
        common_metadata.st_mode
    ):
        raise RuntimeError(
            "blocked: unsafe git common directory for Codex worktree trust recovery"
        )
    state_root = common_dir / "agent-flow"
    _ensure_trust_state_root(common_dir, state_root)
    return state_root / _TRUST_TRANSACTION_FILE


def _ensure_trust_state_root(common_dir: Path, state_root: Path) -> None:
    metadata = _lstat_if_exists(state_root)
    if metadata is not None:
        if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISDIR(
            metadata.st_mode
        ):
            raise RuntimeError(f"blocked: unsafe agent-flow git state root: {state_root}")
        return
    try:
        state_root.mkdir(mode=0o700)
        _fsync_directory(common_dir)
    except FileExistsError:
        raced = _lstat_if_exists(state_root)
        if raced is None or stat_module.S_ISLNK(
            raced.st_mode
        ) or not stat_module.S_ISDIR(raced.st_mode):
            raise


def _missing_config_parents(config_path: Path) -> list[Path]:
    missing: list[Path] = []
    current = config_path.parent
    while _lstat_if_exists(current) is None:
        missing.append(current.resolve(strict=False))
        if current.parent == current:
            break
        current = current.parent
    missing.reverse()
    return missing


def _encoded_original_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    content = snapshot["content"] if snapshot["exists"] else None
    return {
        "exists": bool(snapshot["exists"]),
        "mode": int(snapshot["mode"]),
        "sha256": _sha256(content) if content is not None else None,
        "content_base64": (
            base64.b64encode(content).decode("ascii") if content is not None else None
        ),
    }


def _decoded_original_snapshot(encoded: dict[str, Any]) -> dict[str, Any]:
    content = (
        base64.b64decode(encoded["content_base64"], validate=True)
        if encoded["exists"]
        else None
    )
    return {
        "exists": bool(encoded["exists"]),
        "mode": int(encoded["mode"]),
        "content": content,
    }


def _snapshot_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    content = snapshot["content"] if snapshot["exists"] else None
    return {
        "exists": bool(snapshot["exists"]),
        "mode": int(snapshot["mode"]),
        "sha256": _sha256(content) if content is not None else None,
    }


def _snapshot_states_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["exists"] == right["exists"]
        and left["mode"] == right["mode"]
        and left["sha256"] == right["sha256"]
    )


def _parse_trust_journal(
    content: bytes, leader_root: Path, transaction_path: Path
) -> dict[str, Any]:
    try:
        journal = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"blocked: invalid Codex worktree trust transaction: {exc}"
        ) from exc
    required_keys = {
        "config_path",
        "created_directories",
        "leader_root",
        "receipt_path",
        "status",
        "transaction_token",
        "version",
    }
    if (
        not isinstance(journal, dict)
        or set(journal) != required_keys
        or journal.get("version") != _TRUST_TRANSACTION_VERSION
        or journal.get("status") not in {"pending", "authenticated"}
        or journal.get("leader_root") != str(leader_root)
        or not isinstance(journal.get("config_path"), str)
        or not Path(journal["config_path"]).is_absolute()
        or Path(journal["config_path"]).name != "config.toml"
        or not isinstance(journal.get("receipt_path"), str)
        or not isinstance(journal.get("transaction_token"), str)
        or re.fullmatch(r"[0-9a-f]{32}", journal["transaction_token"]) is None
        or not isinstance(journal.get("created_directories"), list)
    ):
        raise RuntimeError(
            f"blocked: invalid Codex worktree trust transaction: {transaction_path}"
        )
    config_path = Path(journal["config_path"])
    if config_path.resolve(strict=False) != config_path:
        raise RuntimeError(
            f"blocked: invalid Codex worktree trust transaction: {transaction_path}"
        )
    if Path(journal["receipt_path"]) != _codex_config_lock_path(config_path):
        raise RuntimeError(
            f"blocked: invalid Codex worktree trust transaction: {transaction_path}"
        )
    seen: set[str] = set()
    for raw_directory in journal["created_directories"]:
        if not isinstance(raw_directory, str):
            raise RuntimeError(
                f"blocked: invalid Codex worktree trust transaction: {transaction_path}"
            )
        directory = Path(raw_directory)
        try:
            config_path.relative_to(directory)
        except ValueError as exc:
            raise RuntimeError(
                f"blocked: invalid Codex worktree trust transaction: {transaction_path}"
            ) from exc
        if (
            not directory.is_absolute()
            or directory.resolve(strict=False) != directory
            or directory == Path(directory.anchor)
            or raw_directory in seen
            or directory == config_path
        ):
            raise RuntimeError(
                f"blocked: invalid Codex worktree trust transaction: {transaction_path}"
            )
        seen.add(raw_directory)
    return journal


def _trust_journal_payload(
    *,
    status: str,
    leader_root: Path,
    config_path: Path,
    receipt_path: Path,
    token: str,
    created_directories: list[Path],
) -> dict[str, Any]:
    return {
        "version": _TRUST_TRANSACTION_VERSION,
        "status": status,
        "leader_root": str(leader_root),
        "config_path": str(config_path),
        "receipt_path": str(receipt_path),
        "transaction_token": token,
        "created_directories": [str(path) for path in created_directories],
    }


def _codex_config_lock_path(config_path: Path) -> Path:
    return config_path.parent / _TRUST_LOCK_FILE


def _codex_config_transaction_dir(config_path: Path, token: str) -> Path:
    return config_path.parent / f"{_TRUST_TRANSACTION_DIR_PREFIX}{token}"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _replace_trust_receipt(
    transaction: dict[str, Any], receipt: dict[str, Any]
) -> None:
    content = _json_bytes(receipt)
    _durable_replace_journal(
        transaction["receipt_path"], transaction["receipt_bytes"], content
    )
    transaction["receipt"] = receipt
    transaction["receipt_bytes"] = content


def _parse_trust_receipt(
    content: bytes,
    *,
    config_path: Path | None = None,
    leader_root: Path | None = None,
    journal_path: Path | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    if len(content) > _MAX_RECEIPT_BYTES:
        raise RuntimeError("blocked: invalid Codex config transaction receipt")
    try:
        receipt = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"blocked: invalid Codex config transaction receipt: {exc}"
        ) from exc
    required_keys = {
        "config_path",
        "created_directories",
        "journal_path",
        "leader_root",
        "original",
        "original_path",
        "owner_pid",
        "receipt_path",
        "rollback_path",
        "rounds",
        "status",
        "transaction_dir",
        "transaction_token",
        "version",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required_keys
        or receipt.get("version") != _TRUST_RECEIPT_VERSION
        or receipt.get("status") not in {"open", "committed", "rolled-back"}
        or not isinstance(receipt.get("owner_pid"), int)
        or isinstance(receipt.get("owner_pid"), bool)
        or receipt["owner_pid"] <= 0
        or not isinstance(receipt.get("transaction_token"), str)
        or re.fullmatch(r"[0-9a-f]{32}", receipt["transaction_token"]) is None
        or not isinstance(receipt.get("leader_root"), str)
        or not Path(receipt["leader_root"]).is_absolute()
        or Path(receipt["leader_root"]).resolve(strict=False)
        != Path(receipt["leader_root"])
        or not isinstance(receipt.get("journal_path"), str)
        or not Path(receipt["journal_path"]).is_absolute()
        or Path(receipt["journal_path"]).resolve(strict=False)
        != Path(receipt["journal_path"])
        or not isinstance(receipt.get("config_path"), str)
        or not Path(receipt["config_path"]).is_absolute()
        or Path(receipt["config_path"]).resolve(strict=False)
        != Path(receipt["config_path"])
        or Path(receipt["config_path"]).name != "config.toml"
        or not isinstance(receipt.get("receipt_path"), str)
        or Path(receipt["receipt_path"])
        != _codex_config_lock_path(Path(receipt["config_path"]))
        or not isinstance(receipt.get("transaction_dir"), str)
        or Path(receipt["transaction_dir"])
        != _codex_config_transaction_dir(
            Path(receipt["config_path"]), receipt["transaction_token"]
        )
        or not isinstance(receipt.get("rollback_path"), str)
        or Path(receipt["rollback_path"])
        != Path(receipt["transaction_dir"]) / "rollback"
        or not _valid_original_snapshot(receipt.get("original"))
        or receipt.get("original_path")
        != (
            str(Path(receipt["transaction_dir"]) / "original")
            if receipt["original"]["exists"]
            else None
        )
        or not isinstance(receipt.get("created_directories"), list)
        or not isinstance(receipt.get("rounds"), list)
        or len(receipt["rounds"]) > 8
    ):
        raise RuntimeError("blocked: invalid Codex config transaction receipt")
    if (
        config_path is not None
        and Path(receipt["config_path"]) != config_path.resolve(strict=False)
    ) or (
        leader_root is not None
        and receipt["leader_root"] != str(leader_root)
    ) or (
        journal_path is not None
        and Path(receipt["journal_path"]) != journal_path.resolve(strict=False)
    ) or (token is not None and receipt["transaction_token"] != token):
        raise RuntimeError(
            "blocked: Codex config receipt does not authenticate the repository journal"
        )
    original = receipt["original"]
    try:
        original_content = (
            base64.b64decode(original["content_base64"], validate=True)
            if original["exists"]
            else None
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "blocked: invalid Codex config transaction receipt"
        ) from exc
    if original["exists"] and (
        len(original_content) > _MAX_CONFIG_BYTES
        or _sha256(original_content) != original["sha256"]
    ):
        raise RuntimeError("blocked: invalid Codex config transaction receipt")
    _validate_receipt_created_directories(receipt)
    _validate_receipt_rounds(receipt)
    return receipt


def _validate_receipt_created_directories(receipt: dict[str, Any]) -> None:
    seen: set[str] = set()
    config_path = Path(receipt["config_path"])
    for raw_directory in receipt["created_directories"]:
        if not isinstance(raw_directory, str):
            raise RuntimeError("blocked: invalid Codex config transaction receipt")
        directory = Path(raw_directory)
        try:
            config_path.relative_to(directory)
        except ValueError as exc:
            raise RuntimeError(
                "blocked: invalid Codex config transaction receipt"
            ) from exc
        if (
            not directory.is_absolute()
            or directory.resolve(strict=False) != directory
            or directory == Path(directory.anchor)
            or raw_directory in seen
            or directory == config_path
        ):
            raise RuntimeError("blocked: invalid Codex config transaction receipt")
        seen.add(raw_directory)


def _validate_receipt_rounds(receipt: dict[str, Any]) -> None:
    transaction_dir = Path(receipt["transaction_dir"])
    for offset, round_state in enumerate(receipt["rounds"]):
        index = offset + 1
        if (
            not isinstance(round_state, dict)
            or set(round_state)
            != {"after", "before", "candidate_path", "displaced_path", "index"}
            or round_state.get("index") != index
            or not _valid_snapshot_state(round_state.get("before"))
            or not _valid_snapshot_state(round_state.get("after"))
            or round_state["after"]["exists"] is not True
            or Path(round_state["candidate_path"])
            != transaction_dir / f"candidate-{index}"
            or Path(round_state["displaced_path"])
            != transaction_dir / f"displaced-{index}"
        ):
            raise RuntimeError("blocked: invalid Codex config transaction receipt")


def _recover_codex_receipt(
    receipt: dict[str, Any],
    receipt_bytes: bytes,
    *,
    allow_owner_pid: int | None = None,
    journal_bytes: bytes | None = None,
) -> None:
    if receipt["owner_pid"] != allow_owner_pid and _process_is_alive(
        receipt["owner_pid"]
    ):
        raise RuntimeError(
            "blocked: Codex config transaction is active in process "
            f"{receipt['owner_pid']}"
        )
    current_receipt = receipt
    current_bytes = receipt_bytes
    if receipt["status"] == "open":
        _rollback_open_receipt(receipt)
        current_receipt = {**receipt, "status": "rolled-back"}
        current_bytes = _json_bytes(current_receipt)
        _durable_replace_journal(
            Path(receipt["receipt_path"]), receipt_bytes, current_bytes
        )
    _cleanup_codex_transaction(current_receipt, current_bytes, journal_bytes)


def _rollback_open_receipt(receipt: dict[str, Any]) -> None:
    original = _decoded_original_snapshot(receipt["original"])
    original_state = _snapshot_state(original)
    allowed_states = [original_state]
    for round_state in receipt["rounds"]:
        allowed_states.extend((round_state["before"], round_state["after"]))
    _recover_rollback_artifact(receipt, original, original_state, allowed_states)
    for round_state in reversed(receipt["rounds"]):
        displaced_path = Path(round_state["displaced_path"])
        if _lstat_if_exists(displaced_path) is None:
            continue
        displaced = _snapshot_config(displaced_path)
        displaced_state = _snapshot_state(displaced)
        if _snapshot_states_equal(displaced_state, round_state["before"]):
            continue
        _restore_external_displacement(
            Path(receipt["config_path"]), displaced_path, displaced
        )
    _restore_original_no_clobber(
        receipt, original, original_state, allowed_states
    )


def _recover_rollback_artifact(
    receipt: dict[str, Any],
    original: dict[str, Any],
    original_state: dict[str, Any],
    allowed_states: list[dict[str, Any]],
) -> None:
    rollback_path = Path(receipt["rollback_path"])
    if _lstat_if_exists(rollback_path) is None:
        return
    retained = _snapshot_config(rollback_path)
    retained_state = _snapshot_state(retained)
    config_path = Path(receipt["config_path"])
    current = _snapshot_config(config_path)
    if not current["exists"]:
        if _state_in(retained_state, allowed_states):
            _publish_original_artifact(receipt, original)
        else:
            os.link(rollback_path, config_path)
            _fsync_directory(config_path.parent)
    after = _snapshot_config(config_path)
    if not _snapshot_states_equal(
        _snapshot_state(after), retained_state
    ) and not _snapshot_states_equal(_snapshot_state(after), original_state):
        raise RuntimeError(
            f"blocked: concurrent Codex config bytes retained at {rollback_path}"
        )
    rollback_path.unlink()
    _fsync_directory(Path(receipt["transaction_dir"]))


def _restore_original_no_clobber(
    receipt: dict[str, Any],
    original: dict[str, Any],
    original_state: dict[str, Any],
    allowed_states: list[dict[str, Any]],
) -> None:
    config_path = Path(receipt["config_path"])
    current = _snapshot_config(config_path)
    current_state = _snapshot_state(current)
    if _snapshot_states_equal(current_state, original_state):
        return
    if not current["exists"]:
        _publish_original_artifact(receipt, original)
        return
    if not _state_in(current_state, allowed_states):
        return
    rollback_path = Path(receipt["rollback_path"])
    if _lstat_if_exists(rollback_path) is not None:
        raise RuntimeError(
            f"blocked: Codex rollback retention path already exists: {rollback_path}"
        )
    config_path.rename(rollback_path)
    _fsync_directory(config_path.parent)
    _fsync_directory(Path(receipt["transaction_dir"]))
    retained = _snapshot_config(rollback_path)
    retained_state = _snapshot_state(retained)
    if not _state_in(retained_state, allowed_states):
        _restore_external_displacement(config_path, rollback_path, retained)
        return
    _publish_original_artifact(receipt, original)
    current = _snapshot_config(config_path)
    if not _snapshot_states_equal(_snapshot_state(current), original_state):
        raise RuntimeError(
            "blocked: Codex config rollback did not restore the original state"
        )
    rollback_path.unlink()
    _fsync_directory(Path(receipt["transaction_dir"]))


def _publish_original_artifact(
    receipt: dict[str, Any], original: dict[str, Any]
) -> None:
    if not original["exists"]:
        return
    original_path = Path(receipt["original_path"])
    if _lstat_if_exists(original_path) is None:
        raise RuntimeError(
            "blocked: Codex config original rollback artifact is missing"
        )
    artifact = _snapshot_config(original_path)
    if not _snapshot_states_equal(
        _snapshot_state(artifact), _snapshot_state(original)
    ):
        raise RuntimeError(
            "blocked: Codex config original rollback artifact changed"
        )
    try:
        os.link(original_path, Path(receipt["config_path"]))
    except FileExistsError:
        return
    _fsync_directory(Path(receipt["config_path"]).parent)


def _state_in(candidate: dict[str, Any], states: list[dict[str, Any]]) -> bool:
    return any(_snapshot_states_equal(candidate, state) for state in states)


def _cleanup_codex_transaction(
    receipt: dict[str, Any],
    receipt_bytes: bytes,
    journal_bytes: bytes | None = None,
) -> None:
    journal_path = Path(receipt["journal_path"])
    if _lstat_if_exists(journal_path) is not None:
        content = journal_path.read_bytes()
        journal = _parse_trust_journal(
            content, Path(receipt["leader_root"]), journal_path
        )
        if (
            journal["config_path"] != receipt["config_path"]
            or journal["receipt_path"] != receipt["receipt_path"]
            or journal["transaction_token"] != receipt["transaction_token"]
            or (journal_bytes is not None and content != journal_bytes)
        ):
            raise RuntimeError(
                "blocked: Codex config receipt journal binding changed before cleanup"
            )
        _remove_journal_if_unchanged(journal_path, content)
    _remove_validated_transaction_directory(receipt)
    _remove_journal_if_unchanged(Path(receipt["receipt_path"]), receipt_bytes)
    if receipt["status"] == "rolled-back":
        errors: list[str] = []
        for directory in reversed(receipt["created_directories"]):
            _remove_empty_directory(Path(directory), errors)
        if errors:
            raise RuntimeError("; ".join(errors))


def _remove_validated_transaction_directory(receipt: dict[str, Any]) -> None:
    transaction_dir = Path(receipt["transaction_dir"])
    metadata = _lstat_if_exists(transaction_dir)
    if metadata is None:
        return
    if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISDIR(
        metadata.st_mode
    ):
        raise RuntimeError(
            f"blocked: unsafe Codex config transaction directory: {transaction_dir}"
        )
    allowed = {
        *( [Path(receipt["original_path"]).name] if receipt["original_path"] else [] ),
        Path(receipt["rollback_path"]).name,
    }
    for round_state in receipt["rounds"]:
        allowed.add(Path(round_state["candidate_path"]).name)
        allowed.add(Path(round_state["displaced_path"]).name)
    for child in transaction_dir.iterdir():
        if child.name not in allowed:
            raise RuntimeError(
                f"blocked: unknown Codex config transaction artifact: {child.name}"
            )
    shutil.rmtree(transaction_dir)
    _fsync_directory(transaction_dir.parent)


def _valid_original_snapshot(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "content_base64",
        "exists",
        "mode",
        "sha256",
    }:
        return False
    if not isinstance(value["exists"], bool) or not _valid_mode(value["mode"]):
        return False
    if value["exists"]:
        return isinstance(value["content_base64"], str) and bool(
            re.fullmatch(r"[0-9a-f]{64}", str(value["sha256"]))
        )
    return value["content_base64"] is None and value["sha256"] is None


def _valid_snapshot_state(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"exists", "mode", "sha256"}:
        return False
    if not isinstance(value["exists"], bool) or not _valid_mode(value["mode"]):
        return False
    if value["exists"]:
        return bool(re.fullmatch(r"[0-9a-f]{64}", str(value["sha256"])))
    return value["sha256"] is None


def _valid_mode(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0o777


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _durable_create_journal(path: Path, content: bytes) -> None:
    temporary = _durable_temporary_path(path)
    _write_durable_temporary(temporary, content)
    try:
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _durable_replace_journal(path: Path, expected: bytes, content: bytes) -> None:
    _assert_journal_unchanged(path, expected)
    temporary = _durable_temporary_path(path)
    _write_durable_temporary(temporary, content)
    try:
        _assert_journal_unchanged(path, expected)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _remove_journal_if_unchanged(path: Path, expected: bytes) -> None:
    _assert_journal_unchanged(path, expected)
    path.unlink()
    _fsync_directory(path.parent)


def _assert_journal_unchanged(path: Path, expected: bytes) -> None:
    metadata = _lstat_if_exists(path)
    if (
        metadata is None
        or stat_module.S_ISLNK(metadata.st_mode)
        or not stat_module.S_ISREG(metadata.st_mode)
        or not isinstance(expected, bytes)
        or path.read_bytes() != expected
    ):
        raise RuntimeError(
            f"blocked: Codex worktree trust transaction changed concurrently: {path}"
        )


def _write_durable_temporary(path: Path, content: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _durable_temporary_path(path: Path) -> Path:
    return path.with_name(
        f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _snapshot_config(config_path: Path) -> dict[str, Any]:
    current = _lstat_if_exists(config_path)
    if current is None:
        return {"exists": False, "content": None, "mode": 0o600}
    if stat_module.S_ISLNK(current.st_mode) or not stat_module.S_ISREG(current.st_mode):
        raise RuntimeError(f"blocked: unsafe Codex config path: {config_path}")
    if current.st_size > _MAX_CONFIG_BYTES:
        raise RuntimeError(
            "blocked: Codex config exceeds the 2 MiB trust transaction limit"
        )
    content = config_path.read_bytes()
    if len(content) > _MAX_CONFIG_BYTES:
        raise RuntimeError(
            "blocked: Codex config exceeds the 2 MiB trust transaction limit"
        )
    return {
        "exists": True,
        "content": content,
        "mode": current.st_mode & 0o777,
    }


def _assert_config_unchanged(config_path: Path, expected: dict[str, Any]) -> None:
    current = _lstat_if_exists(config_path)
    if not expected["exists"]:
        if current is not None:
            raise RuntimeError(
                f"blocked: Codex config changed during hook trust update: {config_path}"
            )
        return
    if (
        current is None
        or stat_module.S_ISLNK(current.st_mode)
        or not stat_module.S_ISREG(current.st_mode)
        or config_path.read_bytes() != expected["content"]
    ):
        raise RuntimeError(
            f"blocked: Codex config changed during hook trust update: {config_path}"
        )


def _restore_config(
    config_path: Path,
    original: dict[str, Any],
    expected: dict[str, Any],
    errors: list[str],
) -> None:
    if _snapshots_equal(original, expected):
        return
    try:
        _assert_config_unchanged(config_path, expected)
        if original["exists"]:
            _atomic_write(config_path, original["content"], int(original["mode"]))
        else:
            config_path.unlink()
    except OSError as exc:
        errors.append(f"{config_path}: {exc}")
    except RuntimeError as exc:
        errors.append(f"{config_path}: {exc}")


def _snapshots_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["exists"] != right["exists"]:
        return False
    if not left["exists"]:
        return True
    return left["mode"] == right["mode"] and left["content"] == right["content"]


def _ensure_config_parent(config_path: Path, created_directories: list[Path]) -> None:
    missing: list[Path] = []
    current = config_path.parent
    while _lstat_if_exists(current) is None:
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    anchor = _lstat_if_exists(current)
    if (
        anchor is None
        or stat_module.S_ISLNK(anchor.st_mode)
        or not stat_module.S_ISDIR(anchor.st_mode)
    ):
        raise RuntimeError(f"blocked: unsafe Codex config parent: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        created_directories.append(directory)


def _remove_empty_directory(directory: Path, errors: list[str]) -> None:
    try:
        current = _lstat_if_exists(directory)
        if (
            current is not None
            and stat_module.S_ISDIR(current.st_mode)
            and not stat_module.S_ISLNK(current.st_mode)
            and not any(directory.iterdir())
        ):
            directory.rmdir()
    except OSError as exc:
        errors.append(f"{directory}: {exc}")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(
        f"{path.name}.agent-flow-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o777)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _create_file_exclusive(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(
        f"{path.name}.agent-flow-{os.getpid()}-{secrets.token_hex(6)}.tmp"
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o777)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007F")


def _same_existing_path(left: Path | str, right: Path | str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def _lstat_if_exists(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
