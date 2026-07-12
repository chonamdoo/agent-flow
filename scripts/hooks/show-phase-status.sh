#!/bin/bash
# agent-flow Stop hook: 세션 종료 시 현재 워크플로우 phase 표시
TRUSTED_PYTHON=/usr/bin/python3
if [ ! -f "$TRUSTED_PYTHON" ] || [ ! -x "$TRUSTED_PYTHON" ]; then
  printf '%s\n' '{"systemMessage":"[agent-flow] blocked: trusted Python interpreter is unavailable or unsafe"}'
  exit 0
fi
HOOK_SOURCE="${BASH_SOURCE[0]}"
if [[ "$HOOK_SOURCE" == /dev/fd/* ]] && [[ -n "${AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH:-}" ]]; then
  HOOK_SOURCE="$AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH"
fi
SCRIPT_DIR="$(cd "$(dirname "$HOOK_SOURCE")" && pwd)"
PROJECT_ROOT=""
if [ -e ".agent-flow/kit.json" ] || [ -L ".agent-flow/kit.json" ]; then
  PROJECT_ROOT="$PWD"
elif [ -e "$SCRIPT_DIR/../../kit.json" ] || [ -L "$SCRIPT_DIR/../../kit.json" ]; then
  PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
elif [ -e "$SCRIPT_DIR/../../.agent-flow/kit.json" ] || [ -L "$SCRIPT_DIR/../../.agent-flow/kit.json" ]; then
  PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
else
  exit 0
fi
AGENT_FLOW=""
PINNED_AGENT_FLOW_STATE="missing"
AGENT_FLOW_ROOT="$PROJECT_ROOT/.agent-flow"
PINNED_AGENT_FLOW_BIN="$AGENT_FLOW_ROOT/bin"
PINNED_AGENT_FLOW="$PROJECT_ROOT/.agent-flow/bin/agent-flow"
if [ -e "$AGENT_FLOW_ROOT" ] || [ -L "$AGENT_FLOW_ROOT" ]; then
  if [ -L "$AGENT_FLOW_ROOT" ] || [ ! -d "$AGENT_FLOW_ROOT" ]; then
    PINNED_AGENT_FLOW_STATE="unsafe"
  elif [ -e "$PINNED_AGENT_FLOW_BIN" ] || [ -L "$PINNED_AGENT_FLOW_BIN" ]; then
    if [ -L "$PINNED_AGENT_FLOW_BIN" ] || [ ! -d "$PINNED_AGENT_FLOW_BIN" ]; then
      PINNED_AGENT_FLOW_STATE="unsafe"
    elif [ -e "$PINNED_AGENT_FLOW" ] || [ -L "$PINNED_AGENT_FLOW" ]; then
      if [ -L "$PINNED_AGENT_FLOW" ] || [ ! -f "$PINNED_AGENT_FLOW" ] || [ ! -x "$PINNED_AGENT_FLOW" ]; then
        PINNED_AGENT_FLOW_STATE="unsafe"
      else
        PINNED_AGENT_FLOW_STATE="safe"
        AGENT_FLOW="$PINNED_AGENT_FLOW"
      fi
    fi
  fi
fi
AGENT_FLOW_PYTHON=""
PINNED_PYTHON_RUNTIME_STATE="missing"
PYTHON_RUNTIME_ROOT="$AGENT_FLOW_ROOT/runtime"
PINNED_PYTHON_RUNTIME="$PYTHON_RUNTIME_ROOT/python"
PINNED_PYTHON_PACKAGE="$PINNED_PYTHON_RUNTIME/agent_flow"
PINNED_PYTHON_INIT="$PINNED_PYTHON_PACKAGE/__init__.py"
PINNED_PYTHON_CLI="$PINNED_PYTHON_PACKAGE/cli.py"
if [ -L "$AGENT_FLOW_ROOT" ] || [ ! -d "$AGENT_FLOW_ROOT" ]; then
  PINNED_PYTHON_RUNTIME_STATE="unsafe"
elif [ -e "$PYTHON_RUNTIME_ROOT" ] || [ -L "$PYTHON_RUNTIME_ROOT" ]; then
  if [ -L "$PYTHON_RUNTIME_ROOT" ] || [ ! -d "$PYTHON_RUNTIME_ROOT" ]; then
    PINNED_PYTHON_RUNTIME_STATE="unsafe"
  elif [ -e "$PINNED_PYTHON_RUNTIME" ] || [ -L "$PINNED_PYTHON_RUNTIME" ]; then
    if [ -L "$PINNED_PYTHON_RUNTIME" ] || [ ! -d "$PINNED_PYTHON_RUNTIME" ]; then
      PINNED_PYTHON_RUNTIME_STATE="unsafe"
    elif [ -L "$PINNED_PYTHON_PACKAGE" ] || [ ! -d "$PINNED_PYTHON_PACKAGE" ]; then
      PINNED_PYTHON_RUNTIME_STATE="unsafe"
    elif [ -L "$PINNED_PYTHON_INIT" ] || [ ! -f "$PINNED_PYTHON_INIT" ] || [ -L "$PINNED_PYTHON_CLI" ] || [ ! -f "$PINNED_PYTHON_CLI" ]; then
      PINNED_PYTHON_RUNTIME_STATE="unsafe"
    else
      PINNED_PYTHON_RUNTIME_STATE="safe"
    fi
  fi
fi
# Stop hook stdout은 JSON으로 파싱된다. 평문 출력은 Claude Code의
# "invalid stop hook json output" 에러를 유발하고 사용자에게 표시되지도 않는다.
# macOS에는 GNU timeout이 없으므로 status 호출도 python subprocess timeout으로
# 감싸 hook이 세션 종료를 무기한 막지 않게 한다.
if ! AGENT_FLOW="$AGENT_FLOW" AGENT_FLOW_PYTHON="$AGENT_FLOW_PYTHON" AGENT_FLOW_PINNED_STATE="$PINNED_AGENT_FLOW_STATE" AGENT_FLOW_PYTHON_RUNTIME_STATE="$PINNED_PYTHON_RUNTIME_STATE" PROJECT_ROOT="$PROJECT_ROOT" "$TRUSTED_PYTHON" - <<'PY'
import hashlib, json, os, stat, subprocess, sys
from pathlib import Path

project_root = Path(os.environ["PROJECT_ROOT"]).resolve()
PROJECT_RUNTIME_CONTRACT_VERSION = 2
PROJECT_RUNTIME_COMMITMENT_VERSION = 2
PROJECT_LAUNCHER_RELATIVE = ".agent-flow/bin/agent-flow"
NODE_RUNTIME_ROOT_RELATIVE = ".agent-flow/runtime/node"
NODE_RUNTIME_ENTRYPOINT_RELATIVE = ".agent-flow/runtime/node/bin/agent-flow-kit.mjs"
PYTHON_RUNTIME_ROOT_RELATIVE = ".agent-flow/runtime/python"
def emit(message):
    print(json.dumps({"systemMessage": "[agent-flow] " + message}, ensure_ascii=False))

def require_runtime_descendant(relative, kind, label):
    cursor = project_root
    parts = relative.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise RuntimeError(f"{label} path is invalid")
    for index, part in enumerate(parts):
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} path may not use symlinks")
        final = index == len(parts) - 1
        valid = stat.S_ISREG(mode) if final and kind == "file" else stat.S_ISDIR(mode)
        if not valid:
            raise RuntimeError(f"{label} path has an invalid component")
        if final and kind == "file" and cursor.lstat().st_nlink != 1:
            raise RuntimeError(f"{label} may not be hard-linked")
    try:
        cursor.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes the project") from exc
    return cursor

def sha256_file(file):
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def hash_runtime_tree(root, label):
    files = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise RuntimeError(f"{label} is unreadable") from exc
        for entry in entries:
            try:
                mode = entry.lstat().st_mode
            except OSError as exc:
                raise RuntimeError(f"{label} is unreadable") from exc
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"{label} may not contain symlinks")
            if stat.S_ISDIR(mode):
                pending.append(entry)
            elif stat.S_ISREG(mode):
                if entry.lstat().st_nlink != 1:
                    raise RuntimeError(f"{label} may not contain hard-linked files")
                files.append(entry)
            else:
                raise RuntimeError(f"{label} may contain only regular files")
    digest = hashlib.sha256()
    for file in sorted(files, key=lambda candidate: candidate.as_posix()):
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

def normalize_project_runtime_contract(value):
    if not isinstance(value, dict) or set(value) != {"version", "launcher", "node_runtime", "python_runtime"}:
        raise RuntimeError("installed project runtime contract is invalid")
    launcher = value.get("launcher")
    node_runtime = value.get("node_runtime")
    python_runtime = value.get("python_runtime")
    sha256 = lambda candidate: isinstance(candidate, str) and len(candidate) == 64 and all(char in "0123456789abcdef" for char in candidate)
    if (
        value.get("version") != PROJECT_RUNTIME_CONTRACT_VERSION
        or not isinstance(launcher, dict)
        or set(launcher) != {"path", "sha256"}
        or launcher.get("path") != PROJECT_LAUNCHER_RELATIVE
        or not sha256(launcher.get("sha256"))
        or not isinstance(node_runtime, dict)
        or set(node_runtime) != {"root", "entrypoint", "tree_hash"}
        or node_runtime.get("root") != NODE_RUNTIME_ROOT_RELATIVE
        or node_runtime.get("entrypoint") != NODE_RUNTIME_ENTRYPOINT_RELATIVE
        or not sha256(node_runtime.get("tree_hash"))
        or not isinstance(python_runtime, dict)
        or set(python_runtime) != {"root", "tree_hash"}
        or python_runtime.get("root") != PYTHON_RUNTIME_ROOT_RELATIVE
        or not sha256(python_runtime.get("tree_hash"))
    ):
        raise RuntimeError("installed project runtime contract is invalid")
    return {
        "version": PROJECT_RUNTIME_CONTRACT_VERSION,
        "launcher": {"path": PROJECT_LAUNCHER_RELATIVE, "sha256": launcher["sha256"]},
        "node_runtime": {
            "root": NODE_RUNTIME_ROOT_RELATIVE,
            "entrypoint": NODE_RUNTIME_ENTRYPOINT_RELATIVE,
            "tree_hash": node_runtime["tree_hash"],
        },
        "python_runtime": {
            "root": PYTHON_RUNTIME_ROOT_RELATIVE,
            "tree_hash": python_runtime["tree_hash"],
        },
    }

def verify_project_runtime():
    kit_path = require_runtime_descendant(".agent-flow/kit.json", "file", "installed kit metadata")
    try:
        kit = json.loads(kit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed kit metadata is unreadable") from exc
    if not isinstance(kit, dict):
        raise RuntimeError("installed kit metadata is invalid")
    contract = normalize_project_runtime_contract(kit.get("project_runtime_contract"))
    commitment = kit.get("project_runtime_contract_commitment")
    if (
        kit.get("project_runtime_contract_commitment_version") != PROJECT_RUNTIME_COMMITMENT_VERSION
        or not isinstance(commitment, str)
        or len(commitment) != 64
        or any(char not in "0123456789abcdef" for char in commitment)
    ):
        raise RuntimeError("installed project runtime commitment is invalid")
    encoded = json.dumps(
        {"version": PROJECT_RUNTIME_COMMITMENT_VERSION, "contract": contract},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != commitment:
        raise RuntimeError("installed project runtime commitment does not match provenance")
    compatibility = kit.get("node_runtime")
    if (
        not isinstance(compatibility, dict)
        or set(compatibility) != {"path", "tree_hash"}
        or compatibility.get("path") != NODE_RUNTIME_ENTRYPOINT_RELATIVE
        or compatibility.get("tree_hash") != contract["node_runtime"]["tree_hash"]
    ):
        raise RuntimeError("installed Node runtime compatibility metadata is invalid")
    python_compatibility = kit.get("python_runtime")
    if (
        not isinstance(python_compatibility, dict)
        or set(python_compatibility) != {"path", "tree_hash"}
        or python_compatibility.get("path") != PYTHON_RUNTIME_ROOT_RELATIVE
        or python_compatibility.get("tree_hash") != contract["python_runtime"]["tree_hash"]
    ):
        raise RuntimeError("installed Python runtime compatibility metadata is invalid")
    launcher = require_runtime_descendant(contract["launcher"]["path"], "file", "pinned project launcher")
    if not os.access(launcher, os.X_OK):
        raise RuntimeError("pinned project launcher is not executable")
    if sha256_file(launcher) != contract["launcher"]["sha256"]:
        raise RuntimeError("pinned project launcher changed after install")
    runtime_root = require_runtime_descendant(contract["node_runtime"]["root"], "directory", "pinned Node runtime root")
    require_runtime_descendant(contract["node_runtime"]["entrypoint"], "file", "pinned Node runtime entrypoint")
    if hash_runtime_tree(runtime_root, "pinned Node runtime") != contract["node_runtime"]["tree_hash"]:
        raise RuntimeError("pinned Node runtime changed after install")
    python_runtime_root = require_runtime_descendant(
        contract["python_runtime"]["root"],
        "directory",
        "pinned Python runtime root",
    )
    if hash_runtime_tree(python_runtime_root, "pinned Python runtime") != contract["python_runtime"]["tree_hash"]:
        raise RuntimeError("pinned Python runtime changed after install")
    return launcher

def git_common_dir():
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git common directory is unavailable")
    value = Path(result.stdout.strip())
    return value.resolve() if value.is_absolute() else (project_root / value).resolve()

def worktree_branch(workspace):
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return ""
    for block in result.stdout.split("\n\n"):
        if not block.startswith(f"worktree {workspace}\n"):
            continue
        for line in block.splitlines():
            if line.startswith("branch refs/heads/"):
                return line.removeprefix("branch refs/heads/")
    return ""

def node_run_is_active(state):
    return state.get("status") not in {"complete", "aborted"} and state.get("phase") != "complete"

def read_node_run_state(path, label):
    try:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Node {label} is unsafe")
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Node {label} is unreadable") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("run_id"), str)
        or not state["run_id"]
        or not isinstance(state.get("workflow"), str)
        or not state["workflow"]
        or not isinstance(state.get("run_dir"), str)
        or not state["run_dir"]
        or not isinstance(state.get("status"), str)
    ):
        raise RuntimeError(f"Node {label} is invalid")
    return state

def state_roots(common):
    roots = [project_root]
    runtime_root = (common / "agent-flow" / "worktrees").resolve()
    if not runtime_root.exists():
        return roots
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise RuntimeError("runtime worktree root is unsafe")
    roots.extend(
        entry for entry in sorted(runtime_root.iterdir(), key=lambda item: item.name)
        if entry.is_dir() and not entry.is_symlink()
    )
    return roots

def node_run_manifests(common):
    manifests = []
    for state_root in state_roots(common):
        runs_root = state_root / ".agent-flow" / "runs"
        if not runs_root.exists():
            continue
        if runs_root.is_symlink() or not runs_root.is_dir():
            raise RuntimeError("Node runs root is unsafe")
        for workflow_dir in sorted(runs_root.iterdir(), key=lambda entry: entry.name):
            if workflow_dir.is_symlink() or not workflow_dir.is_dir():
                continue
            for run_dir in sorted(workflow_dir.iterdir(), key=lambda entry: entry.name):
                if run_dir.is_symlink() or not run_dir.is_dir():
                    continue
                manifest_path = run_dir / "manifest.json"
                if not manifest_path.exists() and not manifest_path.is_symlink():
                    continue
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Node run manifest is unreadable") from exc
                if isinstance(raw, dict) and "workflow_id" in raw and "workflow" not in raw:
                    continue
                manifest = read_node_run_state(manifest_path, "run manifest")
                raw_run_dir = Path(manifest["run_dir"])
                resolved_run_dir = raw_run_dir.resolve() if raw_run_dir.is_absolute() else (state_root / raw_run_dir).resolve()
                if (
                    manifest["workflow"] != workflow_dir.name
                    or manifest["run_id"] != run_dir.name
                    or resolved_run_dir != run_dir.resolve()
                ):
                    raise RuntimeError("Node run manifest identity mismatch")
                manifests.append({**manifest, "run_dir": str(run_dir.resolve())})
    return manifests

def same_node_run(left, right):
    left_run_dir = Path(left["run_dir"])
    right_run_dir = Path(right["run_dir"])
    return (
        left.get("run_id") == right.get("run_id")
        and left.get("workflow") == right.get("workflow")
        and (left_run_dir.resolve() if left_run_dir.is_absolute() else (project_root / left_run_dir).resolve())
        == (right_run_dir.resolve() if right_run_dir.is_absolute() else (project_root / right_run_dir).resolve())
    )

def node_active(common):
    pointers = []
    for state_root in state_roots(common):
        state_path = state_root / ".agent-flow" / "state" / "current-run.json"
        if state_path.exists() or state_path.is_symlink():
            pointer = read_node_run_state(state_path, "active run state")
            pointers.append({
                **pointer,
                "run_dir": str((state_root / pointer["run_dir"]).resolve()),
            })
    manifests = node_run_manifests(common)
    active_manifests = [manifest for manifest in manifests if node_run_is_active(manifest)]
    if len(active_manifests) > 1:
        raise RuntimeError("multiple Node run manifests are active")
    state = active_manifests[0] if active_manifests else None
    active_pointers = [pointer for pointer in pointers if node_run_is_active(pointer)]
    if len(active_pointers) > 1:
        raise RuntimeError("multiple Node active run pointers found")
    if active_pointers:
        if state is None or not same_node_run(active_pointers[0], state):
            raise RuntimeError("Node active run pointer does not match an active run manifest")
    if state is None:
        return None
    raw_workspace = state.get("workspace_root")
    if not isinstance(raw_workspace, str) or not raw_workspace:
        raise RuntimeError("Node active run workspace is invalid")
    workspace_path = Path(raw_workspace)
    workspace = workspace_path.resolve() if workspace_path.is_absolute() else (project_root / workspace_path).resolve()
    if workspace != project_root:
        branch = worktree_branch(workspace)
        if not branch or branch in {"main", "master", "develop"}:
            raise RuntimeError("Node active run worktree is not registered")
    return {"kind": "node", "workspace": workspace}

def runtime_worktree_identity(worktree_runtime):
    try:
        manifest_path = worktree_runtime / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError("Python worktree manifest is unsafe")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Python worktree manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != worktree_runtime.name:
        raise RuntimeError("Python worktree manifest name is invalid")
    raw_workspace = manifest.get("path")
    branch = manifest.get("branch")
    if not isinstance(raw_workspace, str) or not raw_workspace or not isinstance(branch, str):
        raise RuntimeError("Python worktree manifest is invalid")
    workspace_path = Path(raw_workspace)
    workspace = workspace_path.resolve() if workspace_path.is_absolute() else (project_root / workspace_path).resolve()
    if worktree_branch(workspace) != branch or branch in {"main", "master", "develop"}:
        raise RuntimeError("Python active run worktree is not registered")
    return workspace

def python_active(common):
    runtime_root = (common / "agent-flow" / "worktrees").resolve()
    if not runtime_root.exists():
        return None
    active = []
    for worktree_runtime in sorted(runtime_root.iterdir(), key=lambda entry: entry.name):
        if worktree_runtime.is_symlink() or not worktree_runtime.is_dir():
            continue
        workspace = runtime_worktree_identity(worktree_runtime)
        runs_root = worktree_runtime / ".agent-flow" / "runs"
        if not runs_root.exists():
            continue
        if runs_root.is_symlink() or not runs_root.is_dir():
            raise RuntimeError("Python runs root is unsafe")
        for active_file in sorted(runs_root.glob("*/active")):
            if active_file.is_symlink() or not active_file.is_file():
                raise RuntimeError("Python active marker is unsafe")
            run_dir = active_file.parent.resolve()
            try:
                meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Python active run metadata is unreadable") from exc
            if not isinstance(meta, dict) or meta.get("run_id") != run_dir.name or not isinstance(meta.get("workflow"), str):
                raise RuntimeError("Python active run identity is invalid")
            active.append({"kind": "python", "workspace": workspace, "worktree": worktree_runtime.name})
        for manifest_path in sorted(runs_root.glob("*/*/manifest.json")):
            run_dir = manifest_path.parent.resolve()
            try:
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    raise RuntimeError("Python run manifest is unsafe")
                state = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Python run manifest is unreadable") from exc
            if (
                isinstance(state, dict)
                and isinstance(state.get("workflow"), str)
                and "workflow_id" not in state
            ):
                continue
            if not isinstance(state, dict) or "workflow_id" not in state:
                raise RuntimeError("run manifest runtime is ambiguous")
            raw_run_dir = state.get("run_dir")
            resolved_run_dir = Path(raw_run_dir).resolve() if isinstance(raw_run_dir, str) and Path(raw_run_dir).is_absolute() else (worktree_runtime / str(raw_run_dir or "")).resolve()
            if (
                state.get("workflow_id") != manifest_path.parents[1].name
                or state.get("run_id") != run_dir.name
                or resolved_run_dir != run_dir
                or not isinstance(state.get("status"), str)
            ):
                raise RuntimeError("Python run manifest identity is invalid")
            if state["status"] not in {"complete", "aborted"}:
                active.append({"kind": "python", "workspace": workspace, "worktree": worktree_runtime.name})
    if len(active) > 1:
        raise RuntimeError("multiple Python runs are active")
    return active[0] if active else None

try:
    common = git_common_dir()
    node = node_active(common)
    python = python_active(common)
    if node is not None and python is not None:
        raise RuntimeError("Node and Python runs are active at the same time")
    active = node or python
except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
    emit("blocked: " + str(exc))
    raise SystemExit(0)

if active is None:
    raise SystemExit(0)

try:
    trusted_launcher = verify_project_runtime()
except (OSError, RuntimeError) as exc:
    emit("blocked: " + str(exc))
    raise SystemExit(0)

if active["kind"] == "node":
    command = [str(trusted_launcher), "status", "--format", "hook"]
    command_env = None
else:
    if os.environ.get("AGENT_FLOW_PYTHON_RUNTIME_STATE") == "unsafe":
        emit("blocked: pinned Python runtime path is unsafe")
        raise SystemExit(0)
    runtime = project_root / ".agent-flow" / "runtime" / "python"
    if os.environ.get("AGENT_FLOW_PYTHON_RUNTIME_STATE") == "safe":
        command = [sys.executable, "-m", "agent_flow.cli"]
        command_env = dict(os.environ)
        existing = command_env.get("PYTHONPATH", "")
        command_env["PYTHONPATH"] = str(runtime) + (os.pathsep + existing if existing else "")
    else:
        command = []
        command_env = None
    if command:
        command.extend([
            "status",
            "--root",
            str(project_root),
            "--worktree",
            active["worktree"],
        ])

if not command:
    emit(f"blocked: {active['kind']} status command is unavailable")
    raise SystemExit(0)

try:
    result = subprocess.run(
        command,
        cwd=active["workspace"],
        env=command_env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
except subprocess.TimeoutExpired:
    emit("blocked: phase status command timed out")
    raise SystemExit(0)
except OSError:
    emit("blocked: phase status command could not be started")
    raise SystemExit(0)
status = result.stdout.strip()
if result.returncode != 0:
    emit(f"blocked: phase status command failed with exit {result.returncode}")
    raise SystemExit(0)
if not status:
    emit("blocked: phase status command returned no status")
    raise SystemExit(0)
emit(status)
PY
then
  printf '%s\n' '{"systemMessage":"[agent-flow] blocked: phase-status hook validation failed"}'
fi
exit 0
