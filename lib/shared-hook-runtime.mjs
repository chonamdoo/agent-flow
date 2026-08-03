import crypto from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  MANAGED_HOOK_POLICY_SEQUENCES,
  MANAGED_HOOK_SCRIPTS,
} from "./managed-hooks.mjs";

export const SHARED_HOOK_PROTOCOL_VERSION = 1;
const RUNTIME_ENTRYPOINT = "agent-flow-hook.py";
const CLI_ENTRYPOINT = "agent-flow-cli.py";
const RUNTIME_MANIFEST = "runtime-manifest.json";
const SHARED_HOOK_LAUNCHER_RELATIVE = path.join("bin", "agent-flow-hook");
const SHARED_HOOK_STATE_RELATIVE = "hook-runtime.json";
const MANAGED_PROJECTS_RELATIVE = "managed-projects.json";
const SHARED_HOOK_LOCK_RELATIVE = "managed-runtime.lock";
const LOCK_WAIT_TIMEOUT_MS = 30_000;
const LOCK_HELPER_STOP_TIMEOUT_MS = 5_000;
const LOCK_RETRY_MS = 25;
const sleepBuffer = new Int32Array(new SharedArrayBuffer(4));

function sha256(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function sleepSync() {
  Atomics.wait(sleepBuffer, 0, 0, LOCK_RETRY_MS);
}

function publishFaultPoint(name) {
  const fault = process.env.AGENT_FLOW_TEST_PUBLISH_FAULT;
  if (fault === name) {
    process.exit(86);
  }
  if (fault === `throw:${name}`) {
    throw new Error(`injected publication failure: ${name}`);
  }
}

function canonicalSharedStatePath(homeDir) {
  return path.join(path.resolve(homeDir), SHARED_HOOK_STATE_RELATIVE);
}

function resolveHomePath(homeDir) {
  const target = path.resolve(homeDir);
  const suffix = [path.basename(target)];
  let current = path.dirname(target);
  for (;;) {
    try {
      const canonicalParent = fs.realpathSync.native(current);
      return path.join(canonicalParent, ...suffix);
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
      const parent = path.dirname(current);
      if (parent === current) {
        throw error;
      }
      suffix.unshift(path.basename(current));
      current = parent;
    }
  }
}

function assertSafeDirectoryChain(target, label) {
  let current = path.resolve(target);
  for (;;) {
    let identity;
    try {
      identity = fs.lstatSync(current);
    } catch (error) {
      if (error?.code === "ENOENT") {
        const parent = path.dirname(current);
        if (parent === current) {
          return;
        }
        current = parent;
        continue;
      }
      throw error;
    }
    if (!identity.isDirectory() || identity.isSymbolicLink()) {
      throw new Error(`${label} is not a regular directory: ${current}`);
    }
    if (
      typeof process.getuid === "function"
      && identity.uid !== process.getuid()
      && identity.uid !== 0
    ) {
      throw new Error(`${label} has a foreign-owned ancestor: ${current}`);
    }
    if (
      process.platform !== "win32"
      && (identity.mode & 0o022) !== 0
      && (identity.mode & 0o1000) === 0
    ) {
      throw new Error(`${label} has an unsafe writable ancestor: ${current}`);
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return;
    }
    current = parent;
  }
}


function assertOwnedDirectory(target, mode, label) {
  const identity = fs.lstatSync(target);
  if (!identity.isDirectory() || identity.isSymbolicLink()) {
    throw new Error(`${label} is not a regular owned directory: ${target}`);
  }
  if (typeof process.getuid === "function" && identity.uid !== process.getuid()) {
    throw new Error(`${label} is not owned by the current user: ${target}`);
  }
  if ((identity.mode & 0o777) !== mode) {
    throw new Error(`${label} has unsafe mode: ${target}`);
  }
}

function ensureOwnedDirectory(target, mode, label) {
  assertSafeDirectoryChain(path.dirname(path.resolve(target)), `${label} ancestor`);
  try {
    fs.mkdirSync(target, { recursive: true, mode });
  } catch (error) {
    if (error?.code !== "EEXIST") {
      throw error;
    }
  }
  let descriptor;
  try {
    descriptor = fs.openSync(
      target,
      fs.constants.O_RDONLY
        | (fs.constants.O_NOFOLLOW ?? 0)
        | (fs.constants.O_DIRECTORY ?? 0),
    );
  } catch {
    throw new Error(`${label} is not a regular owned directory: ${target}`);
  }
  try {
    const identity = fs.fstatSync(descriptor);
    if (!identity.isDirectory()) {
      throw new Error(`${label} is not a regular owned directory: ${target}`);
    }
    if (typeof process.getuid === "function" && identity.uid !== process.getuid()) {
      throw new Error(`${label} is not owned by the current user: ${target}`);
    }
    fs.fchmodSync(descriptor, mode);
  } finally {
    fs.closeSync(descriptor);
  }
  assertOwnedDirectory(target, mode, label);
  assertSafeDirectoryChain(target, label);
}

function ensureRegularProjectDirectory(target, label) {
  try {
    fs.mkdirSync(target, { mode: 0o700 });
  } catch (error) {
    if (error?.code !== "EEXIST") {
      throw error;
    }
  }
  const identity = fs.lstatSync(target);
  if (!identity.isDirectory() || identity.isSymbolicLink()) {
    throw new Error(`${label} is not a regular directory: ${target}`);
  }
  if (typeof process.getuid === "function" && identity.uid !== process.getuid()) {
    throw new Error(`${label} is not owned by the current user: ${target}`);
  }
  if (process.platform !== "win32" && (identity.mode & 0o022) !== 0) {
    throw new Error(`${label} has unsafe mode: ${target}`);
  }
}

function readOwnedFile(target, expectedMode, label) {
  const descriptor = fs.openSync(
    target,
    fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0),
  );
  try {
    const identity = fs.fstatSync(descriptor);
    if (!identity.isFile()) {
      throw new Error(`${label} is not a regular file: ${target}`);
    }
    if (identity.nlink !== 1) {
      throw new Error(`${label} has unsafe link count: ${target}`);
    }
    if (typeof process.getuid === "function" && identity.uid !== process.getuid()) {
      throw new Error(`${label} is not owned by the current user: ${target}`);
    }
    if ((identity.mode & 0o777) !== expectedMode) {
      throw new Error(`${label} has unsafe mode: ${target}`);
    }
    return fs.readFileSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function verifyFile(target, expectedDigest, expectedMode, label) {
  const content = readOwnedFile(target, expectedMode, label);
  if (sha256(content) !== expectedDigest) {
    throw new Error(`${label} digest mismatch: ${target}`);
  }
  return content;
}

function readOwnedJson(target, label) {
  let parsed;
  try {
    parsed = JSON.parse(readOwnedFile(target, 0o600, label).toString("utf8"));
  } catch (error) {
    throw new Error(`${label} is invalid: ${target}: ${error.message}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} is invalid: ${target}`);
  }
  return parsed;
}

function writeAtomic(target, content, mode) {
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  const staging = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  try {
    fs.writeFileSync(staging, content, { mode, flag: "wx" });
    fs.chmodSync(staging, mode);
    fs.renameSync(staging, target);
  } finally {
    try {
      fs.rmSync(staging, { force: true });
    } catch {
      // rename이 끝난 뒤 staging 정리만 실패했으므로 이미 반영된 파일은 유지한다.
    }
  }
}
function writeAtomicAtDirectory(directory, expectedIdentity, name, content, mode, label) {
  if (
    process.platform === "win32"
    || name !== path.basename(name)
    || name === "."
    || name === ".."
  ) {
    throw new Error(`${label} requires a safe directory-relative target`);
  }
  const descriptor = fs.openSync(
    directory,
    fs.constants.O_RDONLY
      | (fs.constants.O_DIRECTORY || 0)
      | (fs.constants.O_NOFOLLOW || 0),
  );
  let failure = null;
  try {
    const identity = fs.fstatSync(descriptor);
    if (!identity.isDirectory()) {
      throw new Error(`${label} is not a regular directory: ${directory}`);
    }
    if (
      typeof process.getuid === "function"
      && identity.uid !== process.getuid()
    ) {
      throw new Error(`${label} is not owned by the current user: ${directory}`);
    }
    if ((identity.mode & 0o022) !== 0) {
      throw new Error(`${label} is writable by group or other: ${directory}`);
    }
    if (
      identity.dev !== expectedIdentity.dev
      || identity.ino !== expectedIdentity.ino
    ) {
      throw new Error(`${label} changed before publication: ${directory}`);
    }
    const helper = String.raw`
import os
import secrets
import sys

name = sys.argv[1]
mode = int(sys.argv[2])
temporary = "." + name + "." + str(os.getpid()) + "." + secrets.token_hex(6) + ".tmp"
descriptor = None
try:
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=3,
    )
    content = sys.stdin.buffer.read()
    offset = 0
    while offset < len(content):
        offset += os.write(descriptor, content[offset:])
    os.fchmod(descriptor, mode)
    os.close(descriptor)
    descriptor = None
    os.replace(temporary, name, src_dir_fd=3, dst_dir_fd=3)
finally:
    if descriptor is not None:
        os.close(descriptor)
    try:
        os.unlink(temporary, dir_fd=3)
    except FileNotFoundError:
        pass
`;
    const result = spawnSync(
      trustedSystemPython(),
      ["-I", "-c", helper, name, String(mode)],
      {
        input: content,
        encoding: "utf8",
        stdio: ["pipe", "pipe", "pipe", descriptor],
        timeout: 30_000,
      },
    );
    if (result.error || result.status !== 0) {
      const detail = result.stderr?.trim() || result.error?.message || "unknown error";
      throw new Error(`${label} descriptor write failed: ${detail}`);
    }
    const current = fs.lstatSync(directory);
    if (
      !current.isDirectory()
      || current.isSymbolicLink()
      || current.dev !== identity.dev
      || current.ino !== identity.ino
    ) {
      throw new Error(`${label} changed during publication: ${directory}`);
    }
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try {
      fs.closeSync(descriptor);
    } catch (closeError) {
      if (failure === null) {
        throw closeError;
      }
    }
  }
}

function processAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

const LOCK_HELPER_SOURCE = String.raw`
import fcntl
import os
import stat
import sys
import time

lock_path, ready_path, error_path, parent_pid_text = sys.argv[1:]
try:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    identity = os.fstat(descriptor)
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.getuid()
        or stat.S_IMODE(identity.st_mode) != 0o600
    ):
        raise RuntimeError("shared hook lock has unsafe identity or mode")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    ready_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        ready_flags |= os.O_NOFOLLOW
    ready_descriptor = os.open(ready_path, ready_flags, 0o600)
    try:
        os.write(ready_descriptor, b"ready\n")
        os.fsync(ready_descriptor)
    finally:
        os.close(ready_descriptor)
    parent_pid = int(parent_pid_text)
    while os.getppid() == parent_pid:
        time.sleep(0.05)
except BaseException as exc:
    try:
        error_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            error_flags |= os.O_NOFOLLOW
        error_descriptor = os.open(error_path, error_flags, 0o600)
        try:
            os.write(error_descriptor, (str(exc) + "\n").encode("utf-8", "replace"))
        finally:
            os.close(error_descriptor)
    except OSError:
        pass
    raise
`;

const LOCK_PROBE_SOURCE = String.raw`
import fcntl
import os
import sys

flags = os.O_RDWR
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(sys.argv[1], flags)
fcntl.flock(descriptor, fcntl.LOCK_EX)
fcntl.flock(descriptor, fcntl.LOCK_UN)
`;

function lockSidecarPath(homeDir, kind, nonce) {
  return path.join(
    homeDir,
    `${SHARED_HOOK_LOCK_RELATIVE}.${kind}-${process.pid}-${nonce}`,
  );
}

function stopLockHelper(handle) {
  try {
    process.kill(handle.pid, handle.acquired ? "SIGTERM" : "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") {
      throw error;
    }
  }
  if (handle.acquired) {
    let probe = spawnSync(
      trustedSystemPython(),
      ["-I", "-c", LOCK_PROBE_SOURCE, handle.lock],
      {
        env: {},
        stdio: "ignore",
        timeout: LOCK_HELPER_STOP_TIMEOUT_MS,
      },
    );
    if (probe.error || probe.status !== 0) {
      try {
        process.kill(handle.pid, "SIGKILL");
      } catch (error) {
        if (error?.code !== "ESRCH") {
          throw error;
        }
      }
      probe = spawnSync(
        trustedSystemPython(),
        ["-I", "-c", LOCK_PROBE_SOURCE, handle.lock],
        {
          env: {},
          stdio: "ignore",
          timeout: LOCK_HELPER_STOP_TIMEOUT_MS,
        },
      );
      if (probe.error || probe.status !== 0) {
        throw new Error(`shared hook lock helper did not release: ${handle.lock}`);
      }
    }
  } else {
    sleepSync();
  }
  fs.rmSync(handle.ready, { force: true });
  fs.rmSync(handle.error, { force: true });
}

function acquireSharedHookLock(homeDir) {
  ensureOwnedDirectory(homeDir, 0o700, "shared hook home");
  const nonce = crypto.randomBytes(16).toString("hex");
  const lock = path.join(homeDir, SHARED_HOOK_LOCK_RELATIVE);
  const ready = lockSidecarPath(homeDir, "ready", nonce);
  const error = lockSidecarPath(homeDir, "error", nonce);
  const child = spawn(
    trustedSystemPython(),
    [
      "-I",
      "-c",
      LOCK_HELPER_SOURCE,
      lock,
      ready,
      error,
      String(process.pid),
    ],
    {
      env: {},
      stdio: "ignore",
    },
  );
  if (!Number.isSafeInteger(child.pid) || child.pid <= 0) {
    throw new Error("failed to start shared hook lock helper");
  }
  const handle = { pid: child.pid, lock, ready, error, acquired: false };
  const deadline = Date.now() + LOCK_WAIT_TIMEOUT_MS;
  try {
    while (!fs.existsSync(ready)) {
      if (fs.existsSync(error)) {
        const detail = readOwnedFile(
          error,
          0o600,
          "shared hook lock error",
        ).toString("utf8").trim();
        throw new Error(`failed to acquire shared hook lock: ${detail}`);
      }
      if (!processAlive(child.pid)) {
        const detail = fs.existsSync(error)
          ? readOwnedFile(error, 0o600, "shared hook lock error").toString("utf8").trim()
          : "lock helper exited before acquiring the lock";
        throw new Error(`failed to acquire shared hook lock: ${detail}`);
      }
      if (Date.now() >= deadline) {
        throw new Error(`timed out waiting for shared hook lock: ${lock}`);
      }
      sleepSync();
    }
    readOwnedFile(ready, 0o600, "shared hook lock readiness");
    if (!processAlive(child.pid)) {
      throw new Error("shared hook lock helper exited after reporting readiness");
    }
    handle.acquired = true;
    return handle;
  } catch (lockError) {
    stopLockHelper(handle);
    throw lockError;
  }
}

function releaseSharedHookLock(handle) {
  stopLockHelper(handle);
}

function withSharedHookLock(homeDir, callback) {
  const lock = acquireSharedHookLock(homeDir);
  try {
    return callback(homeDir);
  } finally {
    releaseSharedHookLock(lock);
  }
}

export function agentFlowHome() {
  const configured = process.env.AGENT_FLOW_HOME || process.env.AGENT_FLOW_SHARED_HOME;
  return resolveHomePath(
    configured ? path.resolve(configured) : path.join(os.homedir(), ".agent-flow"),
  );
}

export function withSharedHookMutation(callback, { homeDir = agentFlowHome() } = {}) {
  return withSharedHookLock(resolveHomePath(homeDir), callback);
}

function stableLauncherSource() {
  return `#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

PROTOCOL_VERSION = ${SHARED_HOOK_PROTOCOL_VERSION}
SAFE_DIGEST = re.compile(r"[0-9a-f]{64}")
RUNTIME_MANIFEST = "${RUNTIME_MANIFEST}"
ENTRYPOINT = "${RUNTIME_ENTRYPOINT}"
CLI_ENTRYPOINT = "${CLI_ENTRYPOINT}"
MANAGED_PROJECTS = "${MANAGED_PROJECTS_RELATIVE}"
RUN_BINDINGS = "run-bindings"


def _fail(message: str) -> NoReturn:
    print("agent-flow hook runtime error: " + message, file=sys.stderr)
    raise SystemExit(70)


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_owned(path: Path, *, mode: int, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail(label + " is unavailable: " + str(exc))
    identity = os.fstat(fd)
    if not stat.S_ISREG(identity.st_mode):
        os.close(fd)
        _fail(label + " is not a regular file")
    if identity.st_nlink != 1:
        os.close(fd)
        _fail(label + " has unsafe link count")
    if hasattr(os, "getuid") and identity.st_uid != os.getuid():
        os.close(fd)
        _fail(label + " is not owned by the current user")
    if stat.S_IMODE(identity.st_mode) != mode:
        os.close(fd)
        _fail(label + " has unsafe mode")
    return fd


def _read_json(path: Path, *, mode: int, label: str) -> dict[str, Any]:
    fd = _open_owned(path, mode=mode, label=label)
    try:
        value = json.loads(_read_fd(fd))
    except (OSError, ValueError, TypeError) as exc:
        os.close(fd)
        _fail(label + " is invalid: " + str(exc))
    os.close(fd)
    if not isinstance(value, dict):
        _fail(label + " is invalid")
    return value


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and SAFE_DIGEST.fullmatch(value) is not None

GIT_DISCOVERY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)


def _nearest_git_marker(candidate: Path) -> bool:
    try:
        candidate_mode = candidate.lstat().st_mode
    except FileNotFoundError:
        candidate_mode = None
    except OSError as exc:
        _fail("candidate identity is unreadable: " + str(exc))
    current = candidate if candidate_mode is not None and stat.S_ISDIR(candidate_mode) else candidate.parent
    while True:
        marker = current / ".git"
        try:
            marker.lstat()
            return True
        except FileNotFoundError:
            pass
        except OSError as exc:
            _fail("git marker is unreadable: " + str(exc))
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _git_leader(candidate: Path) -> Path | None:
    environment = dict(os.environ)
    for name in GIT_DISCOVERY_ENV:
        environment.pop(name, None)
    try:
        result = subprocess.run(
            (
                "/usr/bin/git",
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=candidate,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if _nearest_git_marker(candidate):
            _fail("cannot resolve git context: " + str(exc))
        return None
    if result.returncode != 0:
        if _nearest_git_marker(candidate):
            _fail("cannot resolve git context")
        return None
    common = Path(result.stdout.strip()).expanduser().resolve()
    if common.name != ".git":
        _fail("git common directory is unsupported")
    return common.parent.resolve()


def _registered_project(
    requested_root: Path | None,
    home: Path,
    payload: object,
) -> tuple[Path, dict[str, Any]] | None:
    registry = _read_json(
        home / MANAGED_PROJECTS,
        mode=0o600,
        label="managed project registry",
    )
    projects = registry.get("projects") if registry.get("protocol_version") == PROTOCOL_VERSION else None
    if not isinstance(projects, dict):
        _fail("managed project registry is invalid")
    if requested_root is not None:
        root = requested_root.resolve()
        if str(root) not in projects:
            _fail("project root is not registered")
    else:
        candidate = _payload_cwd(payload)
        if candidate is None:
            _fail("hook payload does not provide a routing cwd")
        direct = sorted(
            (
                Path(root_value).resolve()
                for root_value in projects
                if isinstance(root_value, str)
                and Path(root_value).is_absolute()
                and _is_within(candidate, Path(root_value).resolve())
            ),
            key=lambda value: len(value.parts),
            reverse=True,
        )
        leader = _git_leader(candidate)
        direct_root = direct[0] if direct else None
        if leader is None:
            if direct_root is None:
                return None
            root = direct_root
        elif direct_root is not None:
            if direct_root != leader:
                _fail("git context conflicts with the registered project root")
            root = direct_root
        elif str(leader) in projects:
            root = leader
        else:
            return None
    record = projects.get(str(root))
    accepted = record.get("accepted_kit_digests", []) if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("root") != str(root)
        or not _valid_digest(record.get("kit_digest"))
        or not isinstance(accepted, list)
        or any(not _valid_digest(value) for value in accepted)
    ):
        _fail("project root is not registered")
    fd = _open_owned(
        root / ".agent-flow" / "kit.json",
        mode=0o644,
        label="registered project manifest",
    )
    try:
        content = _read_fd(fd)
    finally:
        os.close(fd)
    digest = hashlib.sha256(content).hexdigest()
    if digest not in [record["kit_digest"], *accepted]:
        _fail("project manifest digest does not match the private registry")
    try:
        project = json.loads(content)
    except (ValueError, TypeError) as exc:
        _fail("registered project manifest is invalid: " + str(exc))
    if not isinstance(project, dict):
        _fail("registered project manifest is invalid")
    if Path(str(project.get("shared_hook_home", ""))).resolve() != home:
        _fail("project manifest selects a different shared hook home")
    return root, project


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _payload_cwd(payload: object) -> Path | None:
    if not isinstance(payload, dict):
        return None
    values = [payload.get("cwd"), payload.get("workdir"), payload.get("project_root")]
    tool_input = payload.get("tool_input") or payload.get("input") or payload.get("parameters")
    if isinstance(tool_input, dict):
        values.extend([tool_input.get("cwd"), tool_input.get("workdir")])
    for value in values:
        if isinstance(value, str) and value:
            return Path(value).expanduser().resolve()
    return None

def _validated_cli_cwd(root: Path) -> Path:
    candidate = Path.cwd().resolve()
    leader = _git_leader(candidate)
    if leader is None:
        if _is_within(candidate, root):
            return candidate
        _fail("CLI cwd does not belong to the registered project")
    if leader != root:
        _fail("CLI cwd does not belong to the registered project")
    return candidate


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        _fail("inode-bound runtime routing is unavailable")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_routing_directory(fd: int, *, label: str) -> None:
    identity = os.fstat(fd)
    if not stat.S_ISDIR(identity.st_mode):
        _fail(f"{label} is not a regular directory")
    if hasattr(os, "getuid") and identity.st_uid != os.getuid():
        _fail(f"{label} is not owned by the current user")
    if stat.S_IMODE(identity.st_mode) & 0o022:
        _fail(f"{label} is writable by group or other")


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
    missing_ok: bool = False,
) -> int | None:
    try:
        fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail(f"{label} is unavailable")
    except OSError as exc:
        _fail(f"{label} is unavailable: {exc}")
    try:
        _validate_routing_directory(fd, label=label)
    except (OSError, SystemExit):
        os.close(fd)
        raise
    return fd


def _open_directory_path(
    path: Path,
    *,
    label: str,
    missing_ok: bool = False,
) -> int | None:
    if not path.is_absolute():
        _fail(f"{label} path is not absolute")
    try:
        current_fd = os.open(path.anchor, _directory_flags())
    except OSError as exc:
        _fail(f"{label} ancestor is unavailable: {exc}")
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if missing_ok:
                    return None
                _fail(f"{label} is unavailable")
            except OSError as exc:
                _fail(f"{label} ancestor is unavailable: {exc}")
            os.close(current_fd)
            current_fd = next_fd
        _validate_routing_directory(current_fd, label=label)
        result = current_fd
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _open_directory_chain(
    parent_fd: int | None,
    parts: tuple[str, ...],
    *,
    label: str,
) -> int | None:
    if parent_fd is None:
        return None
    current_fd = os.dup(parent_fd)
    try:
        for component in parts:
            next_fd = _open_directory_at(
                current_fd,
                component,
                label=label,
                missing_ok=True,
            )
            if next_fd is None:
                return None
            os.close(current_fd)
            current_fd = next_fd
        result = current_fd
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _open_owned_at(
    parent_fd: int,
    name: str,
    *,
    mode: int | None,
    label: str,
    missing_ok: bool = False,
) -> int | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail(f"{label} is unavailable")
    except OSError as exc:
        _fail(f"{label} is unavailable: {exc}")
    identity = os.fstat(fd)
    if not stat.S_ISREG(identity.st_mode):
        os.close(fd)
        _fail(f"{label} is not a regular file")
    if identity.st_nlink != 1:
        os.close(fd)
        _fail(f"{label} has unsafe link count")
    if hasattr(os, "getuid") and identity.st_uid != os.getuid():
        os.close(fd)
        _fail(f"{label} is not owned by the current user")
    if mode is not None and stat.S_IMODE(identity.st_mode) != mode:
        os.close(fd)
        _fail(f"{label} has unsafe mode")
    return fd


def _file_snapshot(fd: int) -> tuple[int, int, int, int, int]:
    identity = os.fstat(fd)
    return (
        identity.st_dev,
        identity.st_ino,
        identity.st_size,
        identity.st_mtime_ns,
        identity.st_ctime_ns,
    )


def _read_json_at(
    parent_fd: int,
    name: str,
    *,
    mode: int,
    label: str,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    fd = _open_owned_at(
        parent_fd,
        name,
        mode=mode,
        label=label,
        missing_ok=missing_ok,
    )
    if fd is None:
        return None
    before = _file_snapshot(fd)
    try:
        content = _read_fd(fd)
        after = _file_snapshot(fd)
    except OSError as exc:
        _fail(f"{label} is invalid: {exc}")
    finally:
        os.close(fd)
    if after != before:
        _fail(f"{label} changed during selection")
    try:
        value = json.loads(content)
    except (ValueError, TypeError) as exc:
        _fail(f"{label} is invalid: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} is invalid")
    return value


def _listed_directory(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> int | None:
    try:
        identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _fail(f"{label} vanished during selection")
    except OSError as exc:
        _fail(f"{label} is unreadable: {exc}")
    if stat.S_ISLNK(identity.st_mode):
        _fail(f"{label} is a symbolic link")
    if not stat.S_ISDIR(identity.st_mode):
        return None
    return _open_directory_at(parent_fd, name, label=label)


def _directory_snapshot(fd: int) -> tuple[int, int, int, int]:
    identity = os.fstat(fd)
    return (
        identity.st_dev,
        identity.st_ino,
        identity.st_mtime_ns,
        identity.st_ctime_ns,
    )


def _active_run(runs_path: Path, runs_fd: int | None, *, label: str) -> Path | None:
    if runs_fd is None:
        return None
    before = _directory_snapshot(runs_fd)
    active: list[Path] = []
    try:
        children = os.listdir(runs_fd)
    except OSError as exc:
        _fail(f"{label} run directory is unreadable: {exc}")
    for run_name in children:
        run_fd = _listed_directory(
            runs_fd,
            run_name,
            label=f"{label} run identity",
        )
        if run_fd is None:
            continue
        run_path = runs_path / run_name
        run_before = _directory_snapshot(run_fd)
        try:
            active_fd = _open_owned_at(
                run_fd,
                "active",
                mode=None,
                label=f"{label} active run identity",
                missing_ok=True,
            )
            if active_fd is not None:
                os.close(active_fd)
                active.append(run_path)
                continue
            try:
                workflow_runs = os.listdir(run_fd)
            except OSError as exc:
                _fail(f"{label} workflow run directory is unreadable: {exc}")
            for workflow_name in workflow_runs:
                workflow_fd = _listed_directory(
                    run_fd,
                    workflow_name,
                    label=f"{label} workflow run identity",
                )
                if workflow_fd is None:
                    continue
                try:
                    workflow_before = _directory_snapshot(workflow_fd)
                    manifest = _read_json_at(
                        workflow_fd,
                        "manifest.json",
                        mode=0o644,
                        label=f"{label} workflow run manifest",
                        missing_ok=True,
                    )
                    if manifest is not None and manifest.get("status") == "running":
                        active.append(run_path / workflow_name)
                finally:
                    workflow_changed = (
                        _directory_snapshot(workflow_fd) != workflow_before
                    )
                    os.close(workflow_fd)
                    if workflow_changed:
                        _fail(f"{label} workflow run changed during selection")
        finally:
            run_changed = _directory_snapshot(run_fd) != run_before
            os.close(run_fd)
            if run_changed:
                _fail(f"{label} run identity changed during selection")
    if _directory_snapshot(runs_fd) != before:
        _fail(f"{label} run directory changed during selection")
    if len(active) > 1:
        _fail(f"{label} has multiple active runs")
    return active[0] if active else None


def _bound_runtime_digest(home: Path, run_path: Path, *, label: str) -> str:
    canonical = str(run_path)
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    binding_root = home / RUN_BINDINGS
    binding_fd = _open_directory_path(
        binding_root,
        label="private runtime binding store",
    )
    if binding_fd is None:
        _fail(f"{label} private runtime binding is missing")
    try:
        binding = _read_json_at(
            binding_fd,
            f"{key}.json",
            mode=0o600,
            label=f"{label} private runtime binding",
        )
    finally:
        os.close(binding_fd)
    if binding is None:
        _fail(f"{label} private runtime binding is missing")
    digest = binding.get("runtime_digest")
    if (
        binding.get("protocol_version") != 1
        or binding.get("run_path") != canonical
        or not _valid_digest(digest)
    ):
        _fail(f"{label} private runtime binding is invalid")
    return digest


def _runtime_runs(
    runtime_root_fd: int | None,
    runtime_root_path: Path,
    key: str,
) -> tuple[Path, int | None]:
    runs_path = runtime_root_path / key / ".agent-flow" / "runs"
    return (
        runs_path,
        _open_directory_chain(
            runtime_root_fd,
            (key, ".agent-flow", "runs"),
            label="worktree runtime run directory",
        ),
    )


def _checkout_active_digest(
    home: Path,
    checkout: Path,
    runtime_runs: tuple[Path, int | None],
    *,
    label: str,
    checkout_fd: int | None = None,
) -> str | None:
    if checkout_fd is None:
        checkout_fd = _open_directory_path(checkout, label=f"{label} checkout")
    if checkout_fd is None:
        _fail(f"{label} checkout is unavailable")
    local_runs_path = checkout / ".agent-flow" / "runs"
    try:
        local_runs_fd = _open_directory_chain(
            checkout_fd,
            (".agent-flow", "runs"),
            label=f"{label} local run directory",
        )
    finally:
        os.close(checkout_fd)
    runtime_runs_path, runtime_runs_fd = runtime_runs
    try:
        active = [
            run_path
            for run_path in (
                _active_run(local_runs_path, local_runs_fd, label=label),
                _active_run(runtime_runs_path, runtime_runs_fd, label=label),
            )
            if run_path is not None
        ]
    finally:
        if local_runs_fd is not None:
            os.close(local_runs_fd)
        if runtime_runs_fd is not None:
            os.close(runtime_runs_fd)
    if len(active) > 1:
        _fail(f"{label} has active runs in multiple state layouts")
    if not active:
        return None
    return _bound_runtime_digest(home, active[0], label=label)



def _read_registration_file(path: Path) -> tuple[os.stat_result, bytes] | None:
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        observed = path.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_nlink != 1
    ):
        return None
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        content = os.read(fd, 4097)
    finally:
        os.close(fd)
    if (
        opened.st_dev != observed.st_dev
        or opened.st_ino != observed.st_ino
        or len(content) > 4096
    ):
        return None
    return opened, content


def _adopted_checkout_identity(
    checkout: Path,
    root: Path,
) -> str | None:
    environment = dict(os.environ)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
    ):
        environment.pop(name, None)
    try:
        result = subprocess.run(
            (
                "/usr/bin/git",
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip()).expanduser()
    if not common.is_absolute():
        common = checkout / common
    if common.resolve() != (root / ".git").resolve():
        return None
    dot_git = checkout / ".git"
    pointer = _read_registration_file(dot_git)
    if pointer is None:
        return None
    try:
        pointer_line = pointer[1].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not pointer_line.startswith("gitdir:"):
        return None
    admin_path = Path(pointer_line.partition(":")[2].strip()).expanduser()
    if not admin_path.is_absolute():
        admin_path = checkout / admin_path
    admin_path = admin_path.resolve()
    try:
        admin_identity = admin_path.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(admin_identity.st_mode)
        or not stat.S_ISDIR(admin_identity.st_mode)
        or admin_identity.st_uid != os.getuid()
    ):
        return None
    backlink = _read_registration_file(admin_path / "gitdir")
    if backlink is None:
        return None
    backlink_identity, backlink_content = backlink
    backlink_path = Path(
        backlink_content.decode("utf-8", errors="replace").strip()
    ).expanduser()
    if not backlink_path.is_absolute():
        backlink_path = admin_path / backlink_path
    if backlink_path.resolve() != dot_git.resolve():
        return None
    identity_payload = json.dumps(
        (
            str(checkout.resolve()),
            str(admin_path),
            admin_identity.st_dev,
            admin_identity.st_ino,
            admin_identity.st_uid,
            admin_identity.st_mode,
            backlink_identity.st_dev,
            backlink_identity.st_ino,
            backlink_identity.st_uid,
            backlink_identity.st_mode,
            backlink_identity.st_nlink,
            backlink_identity.st_ctime_ns,
            backlink_content.hex(),
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity_payload).hexdigest()


def _selected_runtime_digest(
    root: Path,
    manifest: dict[str, Any],
    payload: object,
    home: Path | None = None,
) -> str:
    project = manifest.get("hook_runtime")
    project_digest = project.get("digest") if isinstance(project, dict) else None
    if not _valid_digest(project_digest):
        _fail("project hook runtime digest is invalid")
    if home is None:
        shared_home = manifest.get("shared_hook_home")
        if not isinstance(shared_home, str) or not shared_home:
            _fail("project hook runtime shared home is invalid")
        home = Path(shared_home).resolve()
    cwd = _payload_cwd(payload)
    routing_root = root / ".git" / "agent-flow"
    runtime_root_path = routing_root / "worktrees"
    routing_fd = _open_directory_path(
        routing_root,
        label="worktree routing state",
        missing_ok=True,
    )
    adopted_fd: int | None = None
    runtime_root_fd: int | None = None
    try:
        if routing_fd is not None:
            adopted_fd = _open_directory_at(
                routing_fd,
                "adopted",
                label="worktree adoption registry",
                missing_ok=True,
            )
            runtime_root_fd = _open_directory_at(
                routing_fd,
                "worktrees",
                label="worktree runtime registry",
                missing_ok=True,
            )
        records: list[tuple[str, dict[str, Any]]] = []
        if adopted_fd is not None:
            before = _directory_snapshot(adopted_fd)
            try:
                names = os.listdir(adopted_fd)
            except OSError as exc:
                _fail(f"worktree registry is unreadable: {exc}")
            for name in sorted(names):
                if not name.endswith(".json"):
                    continue
                record = _read_json_at(
                    adopted_fd,
                    name,
                    mode=0o600,
                    label="worktree adoption record",
                )
                if record is None:
                    _fail("worktree adoption record vanished during selection")
                records.append((name[:-5], record))
            if _directory_snapshot(adopted_fd) != before:
                _fail("worktree adoption registry changed during selection")
        for key, record in records:
            checkout_value = record.get("path")
            if not isinstance(checkout_value, str) or not checkout_value:
                _fail("worktree adoption record path is invalid")
            checkout = Path(checkout_value).expanduser()
            if not checkout.is_absolute() or ".." in checkout.parts:
                _fail("worktree adoption record path is invalid")
            if cwd is None or not _is_within(cwd, checkout):
                continue
            expected_identity = record.get("registration_identity")
            checkout_fd = _open_directory_path(
                checkout,
                label=f"worktree {key} checkout",
            )
            if checkout_fd is None:
                _fail("worktree checkout is unavailable")
            try:
                opened_checkout = os.fstat(checkout_fd)
                try:
                    current_checkout = os.stat(checkout, follow_symlinks=False)
                except OSError as exc:
                    _fail(f"worktree checkout identity is unreadable: {exc}")
                if (
                    not isinstance(expected_identity, str)
                    or not expected_identity
                    or _adopted_checkout_identity(checkout, root) != expected_identity
                    or not stat.S_ISDIR(current_checkout.st_mode)
                    or current_checkout.st_dev != opened_checkout.st_dev
                    or current_checkout.st_ino != opened_checkout.st_ino
                ):
                    _fail("worktree adoption identity does not match")
                pinned = _checkout_active_digest(
                    home,
                    checkout,
                    _runtime_runs(runtime_root_fd, runtime_root_path, key),
                    label=f"worktree {key}",
                    checkout_fd=os.dup(checkout_fd),
                )
            finally:
                os.close(checkout_fd)
            if pinned is not None:
                return pinned
            return project_digest
        if cwd is None or _is_within(cwd, root):
            pinned = _checkout_active_digest(
                home,
                root,
                _runtime_runs(runtime_root_fd, runtime_root_path, "leader"),
                label="leader",
            )
            if pinned is not None:
                return pinned
        return project_digest
    finally:
        if adopted_fd is not None:
            os.close(adopted_fd)
        if runtime_root_fd is not None:
            os.close(runtime_root_fd)
        if routing_fd is not None:
            os.close(routing_fd)


def _runtime_bundle_entries(runtime_dir: Path) -> list[str]:
    entries: list[str] = []

    def visit(current: Path, relative: Path) -> None:
        try:
            children = list(current.iterdir())
        except OSError as exc:
            _fail("runtime bundle directory is unreadable: " + str(exc))
        for child in children:
            child_relative = relative / child.name
            try:
                identity = child.lstat()
            except OSError as exc:
                _fail("runtime bundle entry is unreadable: " + str(exc))
            if stat.S_ISLNK(identity.st_mode):
                _fail("runtime bundle contains a symlink: " + child_relative.as_posix())
            if stat.S_ISDIR(identity.st_mode):
                if hasattr(os, "getuid") and identity.st_uid != os.getuid():
                    _fail("runtime bundle directory has another owner: " + child_relative.as_posix())
                if stat.S_IMODE(identity.st_mode) != 0o555:
                    _fail("runtime bundle directory has unsafe mode: " + child_relative.as_posix())
                visit(child, child_relative)
            elif stat.S_ISREG(identity.st_mode):
                entries.append(child_relative.as_posix())
            else:
                _fail("runtime bundle contains an unsupported entry: " + child_relative.as_posix())

    visit(runtime_dir, Path())
    return sorted(entries)


def _verify_runtime_bundle(home: Path, digest: str, selected_entrypoint: str) -> tuple[Path, str]:
    runtime_dir = home / "runtimes" / digest
    try:
        directory_identity = runtime_dir.lstat()
    except OSError as exc:
        _fail("selected runtime directory is unavailable: " + str(exc))
    if not stat.S_ISDIR(directory_identity.st_mode) or stat.S_ISLNK(directory_identity.st_mode):
        _fail("selected runtime directory is unsafe")
    if hasattr(os, "getuid") and directory_identity.st_uid != os.getuid():
        _fail("selected runtime directory is not owned by the current user")
    if stat.S_IMODE(directory_identity.st_mode) != 0o555:
        _fail("selected runtime directory has unsafe mode")
    bundle = _read_json(runtime_dir / RUNTIME_MANIFEST, mode=0o444, label="runtime manifest")
    if bundle.get("protocol_version") != PROTOCOL_VERSION or bundle.get("runtime_digest") != digest:
        _fail("runtime manifest identity mismatch")
    if (
        bundle.get("entrypoint") != ENTRYPOINT
        or bundle.get("cli_entrypoint") != CLI_ENTRYPOINT
        or not isinstance(bundle.get("files"), list)
        or not isinstance(bundle.get("policy_sequence"), dict)
    ):
        _fail("runtime manifest content is invalid")
    identity = {
        "protocol_version": bundle.get("protocol_version"),
        "entrypoint": bundle.get("entrypoint"),
        "cli_entrypoint": bundle.get("cli_entrypoint"),
        "policy_sequence": bundle.get("policy_sequence"),
        "files": bundle.get("files"),
    }
    canonical = json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != digest:
        _fail("runtime manifest digest mismatch")
    entry_digest: str | None = None
    recorded_paths: list[str] = []
    for item in bundle["files"]:
        if not isinstance(item, dict):
            _fail("runtime manifest file record is invalid")
        relative = item.get("path")
        file_digest = item.get("sha256")
        mode = item.get("mode")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not _valid_digest(file_digest)
            or mode not in (0o444, 0o555)
        ):
            _fail("runtime manifest file record is invalid")
        recorded_paths.append(relative)
        fd = _open_owned(runtime_dir / relative, mode=mode, label="runtime bundle file")
        try:
            actual = hashlib.sha256(_read_fd(fd)).hexdigest()
        finally:
            os.close(fd)
        if actual != file_digest:
            _fail("runtime bundle file digest mismatch: " + relative)
        if relative == selected_entrypoint:
            entry_digest = file_digest
    if _runtime_bundle_entries(runtime_dir) != sorted([RUNTIME_MANIFEST, *recorded_paths]):
        _fail("runtime bundle contains unrecorded files")
    if entry_digest is None:
        _fail("selected runtime entrypoint is not recorded")
    return runtime_dir / selected_entrypoint, entry_digest


def _restore_stdin(content: bytes):
    stream = tempfile.TemporaryFile()
    stream.write(content)
    stream.flush()
    stream.seek(0)
    os.dup2(stream.fileno(), 0)
    return stream


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root")
    parser.add_argument("--event")
    parser.add_argument("--cli", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    is_cli = args.cli is not None
    if is_cli == bool(args.event):
        _fail("select exactly one hook event or CLI dispatch")
    if is_cli and not args.root:
        _fail("CLI dispatch requires a registered project root")
    home = Path(__file__).resolve().parent.parent
    payload_bytes = b""
    payload: dict[str, Any] = {}
    if not is_cli:
        payload_bytes = sys.stdin.buffer.read()
        try:
            payload = json.loads(payload_bytes) if payload_bytes.strip() else {}
        except (ValueError, TypeError):
            payload = {}
    requested_root = Path(args.root).resolve() if args.root else None
    selected = _registered_project(requested_root, home, payload)
    if selected is None:
        return
    root, project = selected
    if not is_cli and project.get("hooks") is not True:
        return
    routing_payload = {"cwd": str(_validated_cli_cwd(root))} if is_cli else payload
    digest = _selected_runtime_digest(root, project, routing_payload, home)
    selected_entrypoint = CLI_ENTRYPOINT if is_cli else ENTRYPOINT
    entrypoint, entry_digest = _verify_runtime_bundle(
        home,
        digest,
        selected_entrypoint,
    )
    fd = _open_owned(
        entrypoint,
        mode=0o555,
        label="selected runtime entrypoint",
    )
    try:
        if hashlib.sha256(_read_fd(fd)).hexdigest() != entry_digest:
            _fail("selected runtime entrypoint changed after validation")
        os.set_inheritable(fd, True)
        env = dict(os.environ)
        env["AGENT_FLOW_EXECUTED_FD"] = str(fd)
        env["AGENT_FLOW_PROJECT_ROOT"] = str(root)
        env["AGENT_FLOW_RUNTIME_DIGEST"] = digest
        env["AGENT_FLOW_RUNTIME_ENTRYPOINT"] = str(entrypoint)
        env["AGENT_FLOW_RUNTIME_DIR"] = str(entrypoint.parent)
        env["AGENT_FLOW_SHARED_HOME"] = str(home)
        bootstrap = (
            "import os;"
            "fd=int(os.environ['AGENT_FLOW_EXECUTED_FD']);"
            "os.lseek(fd,0,0);"
            "source=os.fdopen(os.dup(fd),'rb').read();"
            "path=os.environ['AGENT_FLOW_RUNTIME_ENTRYPOINT'];"
            "exec(compile(source,path,'exec'),{'__name__':'__main__','__file__':path})"
        )
        if is_cli:
            argv = [sys.executable, "-I", "-c", bootstrap, *args.cli]
        else:
            _stdin_stream = _restore_stdin(payload_bytes)
            argv = [
                sys.executable,
                "-I",
                "-c",
                bootstrap,
                "--root",
                str(root),
                "--event",
                args.event,
            ]
        os.execve(sys.executable, argv, env)
    except OSError as exc:
        _fail("cannot execute selected runtime entrypoint: " + str(exc))

if __name__ == "__main__":
    main()
`;
}

function sourceFilesUnder(root, include = () => true) {
  const files = [];
  const visit = (current) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const candidate = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "__pycache__") {
          visit(candidate);
        }
      } else if (entry.isFile() && include(candidate)) {
        files.push(candidate);
      }
    }
  };
  visit(root);
  return files;
}

function cliDependencyRoots(managedPython) {
  if (
    !managedPython
    || typeof managedPython.python !== "string"
    || typeof managedPython.flag !== "string"
  ) {
    return [];
  }
  const probe = spawnSync(
    managedPython.python,
    [
      managedPython.flag,
      "-c",
      "import click,json,pathlib,yaml;print(json.dumps({'click':str(pathlib.Path(click.__file__).resolve().parent),'yaml':str(pathlib.Path(yaml.__file__).resolve().parent)}))",
    ],
    { encoding: "utf8", timeout: 15_000 },
  );
  if (probe.error || probe.status !== 0) {
    throw new Error("cannot locate verified CLI dependencies");
  }
  let roots;
  try {
    roots = JSON.parse(probe.stdout || "");
  } catch (error) {
    throw new Error("verified CLI dependency probe returned invalid output", {
      cause: error,
    });
  }
  return ["click", "yaml"].map((name) => {
    const candidate = roots?.[name];
    if (typeof candidate !== "string" || !path.isAbsolute(candidate)) {
      throw new Error(`verified CLI dependency root is invalid: ${name}`);
    }
    const resolved = fs.realpathSync.native(candidate);
    const identity = fs.statSync(resolved);
    if (!identity.isDirectory()) {
      throw new Error(`verified CLI dependency root is not a directory: ${resolved}`);
    }
    return { name, root: resolved };
  });
}

function runtimeBundleFiles(kitRoot, managedPython = null) {
  const files = [{
    path: RUNTIME_ENTRYPOINT,
    source: path.join(kitRoot, "scripts", "hook-runtime", RUNTIME_ENTRYPOINT),
    mode: 0o555,
  }, {
    path: CLI_ENTRYPOINT,
    source: path.join(kitRoot, "scripts", "hook-runtime", CLI_ENTRYPOINT),
    mode: 0o555,
  }];
  for (const scriptName of MANAGED_HOOK_SCRIPTS) {
    files.push({
      path: path.posix.join("hooks", scriptName),
      source: path.join(kitRoot, "scripts", "hooks", scriptName),
      mode: 0o444,
    });
  }
  const packageRoot = path.join(kitRoot, "src", "agent_flow");
  for (const source of sourceFilesUnder(
    packageRoot,
    (candidate) => !candidate.endsWith(".pyc"),
  )) {
    const relative = path.relative(path.join(kitRoot, "src"), source).split(path.sep).join(path.posix.sep);
    files.push({
      path: path.posix.join("runtime", "python", relative),
      source,
      mode: 0o444,
    });
  }
  const templateRoot = path.join(kitRoot, "templates");
  const packagedPaths = new Set(files.map((file) => file.path));
  for (const source of sourceFilesUnder(templateRoot)) {
    const relative = path.relative(templateRoot, source).split(path.sep).join(path.posix.sep);
    const target = path.posix.join(
      "runtime",
      "python",
      "agent_flow",
      "templates",
      relative,
    );
    if (packagedPaths.has(target)) {
      continue;
    }
    files.push({ path: target, source, mode: 0o444 });
    packagedPaths.add(target);
  }
  for (const dependency of cliDependencyRoots(managedPython)) {
    for (const source of sourceFilesUnder(
      dependency.root,
      (candidate) => candidate.endsWith(".py"),
    )) {
      const relative = path.relative(dependency.root, source).split(path.sep).join(path.posix.sep);
      files.push({
        path: path.posix.join("runtime", "python", dependency.name, relative),
        source,
        mode: 0o444,
      });
    }
  }
  return files
    .map((file) => {
      const content = fs.readFileSync(file.source);
      return { ...file, content, sha256: sha256(content) };
    })
    .sort((left, right) => left.path.localeCompare(right.path));
}


function runtimeBundleManifest(files) {
  const identity = {
    protocol_version: SHARED_HOOK_PROTOCOL_VERSION,
    entrypoint: RUNTIME_ENTRYPOINT,
    cli_entrypoint: CLI_ENTRYPOINT,
    policy_sequence: MANAGED_HOOK_POLICY_SEQUENCES,
    files: files.map((file) => ({ path: file.path, sha256: file.sha256, mode: file.mode })),
  };
  const runtimeDigest = sha256(Buffer.from(JSON.stringify(identity), "utf8"));
  const manifest = { ...identity, runtime_digest: runtimeDigest };
  return {
    runtimeDigest,
    content: Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, "utf8"),
  };
}

function listRuntimeBundleEntries(runtimeDir) {
  const entries = [];
  const visit = (current, relative) => {
    assertOwnedDirectory(current, 0o555, "runtime bundle directory");
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const childRelative = relative ? path.posix.join(relative, entry.name) : entry.name;
      const candidate = path.join(current, entry.name);
      const identity = fs.lstatSync(candidate);
      if (identity.isSymbolicLink()) {
        throw new Error(`runtime bundle contains a symlink: ${candidate}`);
      }
      if (identity.isDirectory()) {
        visit(candidate, childRelative);
      } else if (identity.isFile()) {
        entries.push(childRelative);
      } else {
        throw new Error(`runtime bundle contains an unsupported entry: ${candidate}`);
      }
    }
  };
  visit(runtimeDir, "");
  return entries.sort();
}

function verifyRuntimeBundle(homeDir, runtimeDigest) {
  const runtimeDir = path.join(homeDir, "runtimes", runtimeDigest);
  const manifestContent = readOwnedFile(path.join(runtimeDir, RUNTIME_MANIFEST), 0o444, "runtime manifest");
  let manifest;
  try {
    manifest = JSON.parse(manifestContent.toString("utf8"));
  } catch (error) {
    throw new Error(`runtime manifest is invalid: ${error.message}`);
  }
  if (
    manifest?.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
    || manifest?.runtime_digest !== runtimeDigest
    || manifest?.entrypoint !== RUNTIME_ENTRYPOINT
    || manifest?.cli_entrypoint !== CLI_ENTRYPOINT
    || !manifest?.policy_sequence
    || typeof manifest.policy_sequence !== "object"
    || Array.isArray(manifest.policy_sequence)
    || !Array.isArray(manifest?.files)
  ) {
    throw new Error(`runtime manifest identity mismatch: ${runtimeDir}`);
  }
  const identity = {
    protocol_version: manifest.protocol_version,
    entrypoint: manifest.entrypoint,
    cli_entrypoint: manifest.cli_entrypoint,
    policy_sequence: manifest.policy_sequence,
    files: manifest.files,
  };
  if (sha256(Buffer.from(JSON.stringify(identity), "utf8")) !== runtimeDigest) {
    throw new Error(`runtime manifest digest mismatch: ${runtimeDir}`);
  }
  const expected = [RUNTIME_MANIFEST];
  for (const item of manifest.files) {
    if (
      !item
      || typeof item.path !== "string"
      || !item.path
      || path.posix.isAbsolute(item.path)
      || item.path.split("/").includes("..")
      || !/^[0-9a-f]{64}$/.test(item.sha256)
      || ![0o444, 0o555].includes(item.mode)
    ) {
      throw new Error(`runtime manifest file record is invalid: ${runtimeDir}`);
    }
    verifyFile(path.join(runtimeDir, ...item.path.split("/")), item.sha256, item.mode, "runtime bundle file");
    expected.push(item.path);
  }
  const actual = listRuntimeBundleEntries(runtimeDir);
  expected.sort();
  if (actual.length !== expected.length || actual.some((value, index) => value !== expected[index])) {
    throw new Error(`runtime bundle contains unrecorded files: ${runtimeDir}`);
  }
  return { runtimeDir, runtimePath: path.join(runtimeDir, RUNTIME_ENTRYPOINT), manifest };
}

function publishRuntimeBundle(homeDir, files, manifest) {
  const runtimesDir = path.join(homeDir, "runtimes");
  ensureOwnedDirectory(runtimesDir, 0o700, "shared hook runtimes");
  const runtimeDir = path.join(runtimesDir, manifest.runtimeDigest);
  if (fs.existsSync(runtimeDir)) {
    return verifyRuntimeBundle(homeDir, manifest.runtimeDigest);
  }
  const staging = path.join(runtimesDir, `.${manifest.runtimeDigest}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`);
  fs.mkdirSync(staging, { mode: 0o700 });
  try {
    for (const file of files) {
      const target = path.join(staging, ...file.path.split("/"));
      fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
      fs.writeFileSync(target, file.content, { mode: file.mode, flag: "wx" });
      fs.chmodSync(target, file.mode);
    }
    fs.writeFileSync(path.join(staging, RUNTIME_MANIFEST), manifest.content, { mode: 0o444, flag: "wx" });
    fs.chmodSync(path.join(staging, RUNTIME_MANIFEST), 0o444);
    const directories = [staging];
    const collect = (current) => {
      for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
        if (entry.isDirectory()) {
          const child = path.join(current, entry.name);
          directories.push(child);
          collect(child);
        }
      }
    };
    collect(staging);
    for (const directory of directories.reverse()) {
      fs.chmodSync(directory, 0o555);
    }
    publishFaultPoint("before-runtime-publish");
    try {
      fs.renameSync(staging, runtimeDir);
    } catch (error) {
      if (error?.code !== "EEXIST" && error?.code !== "ENOTEMPTY") {
        throw error;
      }
    }
  } finally {
    if (fs.existsSync(staging)) {
      fs.chmodSync(staging, 0o700);
      fs.rmSync(staging, { recursive: true, force: true });
    }
  }
  return verifyRuntimeBundle(homeDir, manifest.runtimeDigest);
}

function writeLauncher(homeDir, launcherSource) {
  const launcherPath = path.join(homeDir, SHARED_HOOK_LAUNCHER_RELATIVE);
  ensureOwnedDirectory(path.dirname(launcherPath), 0o700, "shared hook bin");
  writeAtomic(launcherPath, launcherSource, 0o755);
  return launcherPath;
}

function verifyRuntimeInstall({
  homeDir,
  runtimeDigest,
  launcherDigest,
  statePath,
  python = null,
}) {
  const runtime = verifyRuntimeBundle(homeDir, runtimeDigest);
  const launcherPath = path.join(homeDir, SHARED_HOOK_LAUNCHER_RELATIVE);
  verifyFile(launcherPath, launcherDigest, 0o755, "shared hook launcher");
  const state = readOwnedJson(statePath, "shared hook runtime state");
  const runtimeDigests = Array.isArray(state.runtime_digests)
    ? state.runtime_digests
    : [state.active_runtime_digest];
  const launcherDigests = state.launcher_digests;
  const statePython = selectedStatePython(state);
  if (
    state.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
    || state.active_runtime_digest !== runtimeDigest
    || state.launcher_digest !== launcherDigest
    || !runtimeDigests.includes(runtimeDigest)
    || !Array.isArray(launcherDigests)
    || launcherDigests.length !== 1
    || launcherDigests[0] !== launcherDigest
    || (python !== null && statePython !== python)
    || runtimeDigests.some((digest) => !validDigest(digest))
  ) {
    throw new Error(`shared hook state does not match installed files: ${statePath}`);
  }
  return {
    ...runtime,
    launcherPath,
    statePath,
    python: statePython,
    runtimeDigest,
    launcherDigest,
    runtimeDigests,
    launcherDigests,
  };
}

function validDigest(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function canonicalProjectRoot(root) {
  return fs.realpathSync.native(path.resolve(root));
}

function emptyManagedProjectRegistry() {
  return {
    protocol_version: SHARED_HOOK_PROTOCOL_VERSION,
    projects: {},
  };
}


function readManagedProjectRegistry(homeDir) {
  const registryPath = path.join(homeDir, MANAGED_PROJECTS_RELATIVE);
  if (!fs.existsSync(registryPath)) {
    return { registryPath, registry: emptyManagedProjectRegistry() };
  }
  const content = readOwnedFile(
    registryPath,
    0o600,
    "managed project registry",
  ).toString("utf8");
  let registry;
  try {
    registry = JSON.parse(content);
  } catch (error) {
    throw new Error(`managed project registry is invalid: ${registryPath}`, {
      cause: error,
    });
  }
  if (
    !registry
    || typeof registry !== "object"
    || Array.isArray(registry)
    || registry.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
    || !registry.projects
    || typeof registry.projects !== "object"
    || Array.isArray(registry.projects)
  ) {
    throw new Error(`managed project registry is invalid: ${registryPath}`);
  }
  return { registryPath, registry };
}

function writeManagedProjectRegistry(registryPath, registry) {
  writeAtomic(registryPath, `${JSON.stringify(registry, null, 2)}\n`, 0o600);
}

function validatedStablePython(candidate, label) {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate)) {
    throw new Error(`${label} is not an absolute path`);
  }
  const normalized = path.resolve(candidate);
  const resolved = fs.realpathSync.native(normalized);
  if (resolved !== normalized) {
    throw new Error(`${label} is not a stable realpath: ${candidate}`);
  }
  const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
  const trustedIdentity = (identity) => (
    currentUid === null
    || (
      (identity.uid === 0 || identity.uid === currentUid)
      && (identity.mode & 0o022) === 0
    )
  );
  const executable = fs.statSync(resolved);
  if (
    !executable.isFile()
    || !trustedIdentity(executable)
    || (process.platform !== "win32" && (executable.mode & 0o111) === 0)
  ) {
    throw new Error(`${label} is not a trusted executable: ${resolved}`);
  }
  if (process.platform !== "win32") {
    let current = path.dirname(resolved);
    for (;;) {
      const directory = fs.statSync(current);
      if (!directory.isDirectory() || !trustedIdentity(directory)) {
        throw new Error(`${label} ancestor is unsafe: ${current}`);
      }
      const parent = path.dirname(current);
      if (parent === current) {
        break;
      }
      current = parent;
    }
  }
  fs.accessSync(resolved, fs.constants.X_OK);
  return resolved;
}

function legacyPythonOnlyFailsAncestorTrust(candidate) {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate)) {
    return false;
  }
  try {
    const normalized = path.resolve(candidate);
    const resolved = fs.realpathSync.native(normalized);
    if (resolved !== normalized) {
      return false;
    }
    const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
    const trustedIdentity = (identity) => (
      currentUid === null
      || (
        (identity.uid === 0 || identity.uid === currentUid)
        && (identity.mode & 0o022) === 0
      )
    );
    const executable = fs.statSync(resolved);
    if (
      !executable.isFile()
      || !trustedIdentity(executable)
      || (process.platform !== "win32" && (executable.mode & 0o111) === 0)
    ) {
      return false;
    }
    let unsafeAncestor = false;
    if (process.platform !== "win32") {
      let current = path.dirname(resolved);
      for (;;) {
        const directory = fs.statSync(current);
        if (!directory.isDirectory()) {
          return false;
        }
        unsafeAncestor ||= !trustedIdentity(directory);
        const parent = path.dirname(current);
        if (parent === current) {
          break;
        }
        current = parent;
      }
    }
    fs.accessSync(resolved, fs.constants.X_OK);
    return unsafeAncestor;
  } catch {
    return false;
  }
}

function selectedStatePython(state) {
  if (!Object.hasOwn(state, "python")) {
    // 이 필드가 없던 이전 state는 당시 유일하게 허용한 system Python으로만 복구한다.
    return trustedSystemPython();
  }
  return validatedStablePython(state.python, "shared hook state Python");
}

function trustedSystemPython() {
  for (const candidate of ["/usr/bin/python3", "/usr/local/bin/python3"]) {
    try {
      const resolved = fs.realpathSync.native(candidate);
      const identity = fs.statSync(resolved);
      if (
        !identity.isFile()
        || identity.uid !== 0
        || (identity.mode & 0o022) !== 0
      ) {
        continue;
      }
      let current = path.dirname(resolved);
      for (;;) {
        const directory = fs.statSync(current);
        if (
          !directory.isDirectory()
          || directory.uid !== 0
          || (directory.mode & 0o022) !== 0
        ) {
          throw new Error(`system Python ancestor is unsafe: ${current}`);
        }
        const parent = path.dirname(current);
        if (parent === current) {
          break;
        }
        current = parent;
      }
      fs.accessSync(resolved, fs.constants.X_OK);
      return resolved;
    } catch {
      // 실패한 후보를 건너뛰고 다음 system-owned 경로를 검사한다.
    }
  }
  throw new Error("system-owned managed hook Python runtime is unavailable");
}

function verifiedLauncherBootstrapSource(homeDir) {
  return `from __future__ import annotations
import sys
sys.path = [entry for entry in sys.path if entry not in ("", ".")]
import os
cwd = os.getcwd()
sys.path = [entry for entry in sys.path if entry != cwd]
import hashlib
import json
import stat
from pathlib import Path

HOME = Path(${JSON.stringify(resolveHomePath(homeDir))})

def fail(message):
    print("agent-flow launcher verification failed: " + message, file=sys.stderr)
    raise SystemExit(70)

def read_owned(path, mode, label):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        fail(label + " is unavailable: " + str(exc))
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            fail(label + " is not a regular file")
        if hasattr(os, "getuid") and identity.st_uid != os.getuid():
            fail(label + " is not owned by the current user")
        if identity.st_nlink != 1:
            fail(label + " has an unsafe link count")
        if stat.S_IMODE(identity.st_mode) != mode:
            fail(label + " has an unsafe mode")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)

state_path = HOME / ${JSON.stringify(SHARED_HOOK_STATE_RELATIVE)}
launcher_path = HOME / ${JSON.stringify(SHARED_HOOK_LAUNCHER_RELATIVE)}
try:
    state = json.loads(read_owned(state_path, 0o600, "shared hook state"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    fail("shared hook state is invalid: " + str(exc))
source = read_owned(launcher_path, 0o755, "shared hook launcher")
primary_digest = state.get("launcher_digest") if isinstance(state, dict) else None
accepted_digests = state.get("launcher_digests") if isinstance(state, dict) else None

def valid_digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

if (
    not isinstance(state, dict)
    or state.get("protocol_version") != ${SHARED_HOOK_PROTOCOL_VERSION}
    or not valid_digest(primary_digest)
    or not isinstance(accepted_digests, list)
    or not accepted_digests
    or any(not valid_digest(value) for value in accepted_digests)
    or len(set(accepted_digests)) != len(accepted_digests)
    or primary_digest not in accepted_digests
):
    fail("shared hook launcher digest state is invalid")
if hashlib.sha256(source).hexdigest() not in accepted_digests:
    fail("shared hook launcher digest does not match runtime state")
sys.argv = [str(launcher_path), *sys.argv[1:]]
exec(compile(source, str(launcher_path), "exec"), {
    "__name__": "__main__",
    "__file__": str(launcher_path),
})
`;
}

export function sharedHookLauncherInvocation({ homeDir = agentFlowHome() } = {}) {
  const resolvedHome = resolveHomePath(homeDir);
  const statePath = canonicalSharedStatePath(resolvedHome);
  const bootstrap = verifiedLauncherBootstrapSource(resolvedHome);
  const bootstrapDigests = new Set([
    sha256(Buffer.from(bootstrap, "utf8")),
  ]);
  let python = trustedSystemPython();
  if (fs.existsSync(statePath)) {
    const state = readOwnedJson(statePath, "shared hook runtime state");
    if (state.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION) {
      throw new Error(`shared hook runtime state is invalid: ${statePath}`);
    }
    python = selectedStatePython(state);
    for (const digest of [
      state.bootstrap_digest,
      ...(Array.isArray(state.bootstrap_digests) ? state.bootstrap_digests : []),
    ]) {
      if (validDigest(digest)) {
        bootstrapDigests.add(digest);
      }
    }
  }
  return {
    python,
    launcher: path.join(resolvedHome, SHARED_HOOK_LAUNCHER_RELATIVE),
    bootstrap,
    bootstrapDigests,
  };
}

export function installSharedHookRuntime({
  kitRoot,
  homeDir = agentFlowHome(),
  managedPython = null,
  force = false,
}) {
  const resolvedHome = resolveHomePath(homeDir);
  const selectedPython = managedPython?.realpath
    ? validatedStablePython(managedPython.realpath, "managed runtime Python")
    : trustedSystemPython();
  const files = runtimeBundleFiles(path.resolve(kitRoot), managedPython);
  const manifest = runtimeBundleManifest(files);
  const launcherSource = Buffer.from(stableLauncherSource(), "utf8");
  const launcherDigest = sha256(launcherSource);
  const bootstrapDigest = sha256(
    Buffer.from(verifiedLauncherBootstrapSource(resolvedHome), "utf8"),
  );
  const statePath = canonicalSharedStatePath(resolvedHome);
  return withSharedHookLock(resolvedHome, () => {
    const runtime = publishRuntimeBundle(resolvedHome, files, manifest);
    let existingState = null;
    if (fs.existsSync(statePath)) {
      existingState = readOwnedJson(statePath, "shared hook runtime state");
      if (
        existingState.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
        || !validDigest(existingState.active_runtime_digest)
        || !validDigest(existingState.launcher_digest)
      ) {
        throw new Error(`shared hook runtime state is invalid: ${statePath}`);
      }
    }
    const launcherPath = path.join(resolvedHome, SHARED_HOOK_LAUNCHER_RELATIVE);
    let currentLauncher = null;
    let currentLauncherMode = 0o755;
    try {
      const identity = fs.lstatSync(launcherPath);
      if (!identity.isFile() || identity.isSymbolicLink()) {
        throw new Error(`shared hook launcher is not a regular file: ${launcherPath}`);
      }
      if (
        typeof process.getuid === "function"
        && identity.uid !== process.getuid()
      ) {
        throw new Error(`shared hook launcher is not owned by the current user: ${launcherPath}`);
      }
      currentLauncher = fs.readFileSync(launcherPath);
      currentLauncherMode = identity.mode & 0o777;
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
    const currentLauncherDigest = currentLauncher === null
      ? null
      : sha256(currentLauncher);
    const recordedLauncherDigests = new Set([
      existingState?.launcher_digest,
      ...(Array.isArray(existingState?.launcher_digests)
        ? existingState.launcher_digests
        : []),
    ].filter(validDigest));
    const recordedBootstrapDigests = new Set([
      existingState?.bootstrap_digest,
      ...(Array.isArray(existingState?.bootstrap_digests)
        ? existingState.bootstrap_digests
        : []),
    ].filter(validDigest));
    const currentLauncherOwnedByState = (
      existingState !== null
      && currentLauncherDigest !== null
      && recordedLauncherDigests.has(currentLauncherDigest)
    );
    if (existingState !== null) {
      try {
        selectedStatePython(existingState);
      } catch (error) {
        if (
          !currentLauncherOwnedByState
          || !legacyPythonOnlyFailsAncestorTrust(existingState.python)
        ) {
          throw error;
        }
      }
    }
    if (
      currentLauncher !== null
      && !recordedLauncherDigests.has(currentLauncherDigest)
      && existingState === null
    ) {
      if (!force) {
        throw new Error(
          `agent-flow: ${launcherPath} is not owned by the shared hook runtime state; `
          + "refusing to replace it. Re-run with --force-managed to replace it.",
        );
      }
      backupAdapterConflict(launcherPath, currentLauncher, currentLauncherMode);
    }
    const runtimeDigests = [...new Set([
      ...(Array.isArray(existingState?.runtime_digests)
        ? existingState.runtime_digests.filter(validDigest)
        : []),
      ...(validDigest(existingState?.active_runtime_digest)
        ? [existingState.active_runtime_digest]
        : []),
      manifest.runtimeDigest,
    ])];
    const finalLauncherDigests = [launcherDigest];
    const finalBootstrapDigests = [...new Set([
      ...recordedBootstrapDigests,
      bootstrapDigest,
    ])];
    const nextState = {
      protocol_version: SHARED_HOOK_PROTOCOL_VERSION,
      active_runtime_digest: manifest.runtimeDigest,
      runtime_digests: runtimeDigests,
      launcher_digest: launcherDigest,
      launcher_digests: finalLauncherDigests,
      bootstrap_digest: bootstrapDigest,
      bootstrap_digests: finalBootstrapDigests,
      python: selectedPython,
      ...(existingState?.omp_adapter ? { omp_adapter: existingState.omp_adapter } : {}),
    };
    const previousLauncherDigest = currentLauncherDigest !== null
      && recordedLauncherDigests.has(currentLauncherDigest)
      ? currentLauncherDigest
      : null;
    const transitionState = previousLauncherDigest === null
      ? nextState
      : {
        ...nextState,
        launcher_digest: previousLauncherDigest,
        launcher_digests: [...new Set([previousLauncherDigest, launcherDigest])],
      };
    writeAtomic(
      statePath,
      `${JSON.stringify(transitionState, null, 2)}\n`,
      0o600,
    );
    publishFaultPoint("after-transition-state");
    writeLauncher(resolvedHome, launcherSource);
    publishFaultPoint("after-launcher-publish");
    writeAtomic(statePath, `${JSON.stringify(nextState, null, 2)}\n`, 0o600);
    publishFaultPoint("after-final-state");
    verifyRuntimeInstall({
      homeDir: resolvedHome,
      runtimeDigest: manifest.runtimeDigest,
      launcherDigest,
      statePath,
      python: selectedPython,
    });
    return {
      protocol_version: SHARED_HOOK_PROTOCOL_VERSION,
      digest: manifest.runtimeDigest,
      path: runtime.runtimePath,
      manifest_path: path.join(runtime.runtimeDir, RUNTIME_MANIFEST),
      launcher_path: launcherPath,
      bootstrap_digest: bootstrapDigest,
      python: selectedPython,
    };
  });
}

function selectedRuntimeRecord(manifest) {
  const hookRuntime = manifest?.hook_runtime;
  if (
    !hookRuntime
    || typeof hookRuntime !== "object"
    || hookRuntime.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
    || !validDigest(hookRuntime.digest)
    || typeof hookRuntime.path !== "string"
    || !path.isAbsolute(hookRuntime.path)
    || typeof hookRuntime.launcher_path !== "string"
    || !path.isAbsolute(hookRuntime.launcher_path)
    || typeof hookRuntime.python !== "string"
  ) {
    throw new Error("project manifest hook_runtime record is invalid");
  }
  validatedStablePython(
    hookRuntime.python,
    "project manifest historical Python",
  );
  const inferredHome = path.dirname(path.dirname(path.resolve(hookRuntime.launcher_path)));
  const statePath = canonicalSharedStatePath(inferredHome);
  const state = readOwnedJson(statePath, "shared hook runtime state");
  selectedStatePython(state);
  if (
    state.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
    || state.active_runtime_digest !== hookRuntime.digest
  ) {
    throw new Error("project manifest hook_runtime record does not match shared state");
  }
  return hookRuntime;
}

export function publishManagedProject({ root, manifest, deferCommit = false }) {
  const resolvedRoot = canonicalProjectRoot(root);
  assertSafeDirectoryChain(resolvedRoot, "project root");
  const agentFlowDir = path.join(resolvedRoot, ".agent-flow");
  ensureRegularProjectDirectory(agentFlowDir, "project agent-flow directory");
  const agentFlowIdentity = fs.lstatSync(agentFlowDir);
  const target = path.join(agentFlowDir, "kit.json");
  const hookRuntime = selectedRuntimeRecord(manifest);
  const inferredHome = path.dirname(path.dirname(path.resolve(hookRuntime.launcher_path)));
  const home = resolveHomePath(manifest.shared_hook_home || inferredHome);
  const content = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  const kitDigest = sha256(content);
  return withSharedHookLock(home, () => {
    let previousManifest = null;
    try {
      const identity = fs.lstatSync(target);
      if (!identity.isFile() || identity.isSymbolicLink() || identity.nlink !== 1) {
        throw new Error(`project manifest is not a safe regular file: ${target}`);
      }
      previousManifest = {
        content: fs.readFileSync(target),
        mode: identity.mode & 0o777,
      };
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
    const runtime = verifyRuntimeBundle(home, hookRuntime.digest);
    if (path.resolve(hookRuntime.path) !== path.resolve(runtime.runtimePath)) {
      throw new Error("project manifest hook_runtime path does not match its digest bundle");
    }
    const launcherPath = path.join(home, SHARED_HOOK_LAUNCHER_RELATIVE);
    if (path.resolve(hookRuntime.launcher_path) !== path.resolve(launcherPath)) {
      throw new Error("project manifest hook runtime launcher path is invalid");
    }
    const statePath = canonicalSharedStatePath(home);
    const state = readOwnedJson(statePath, "shared hook runtime state");
    selectedStatePython(state);
    if (
      state.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
      || !validDigest(state.active_runtime_digest)
      || !validDigest(state.launcher_digest)
    ) {
      throw new Error(`shared hook runtime state is invalid: ${statePath}`);
    }
    verifyFile(launcherPath, state.launcher_digest, 0o755, "shared hook launcher");

    const { registryPath, registry } = readManagedProjectRegistry(home);
    const previous = registry.projects[resolvedRoot] ?? null;
    const previousDigest = validDigest(previous?.kit_digest)
      ? previous.kit_digest
      : kitDigest;
    const accepted = new Set([kitDigest]);
    if (Array.isArray(previous?.accepted_kit_digests)) {
      for (const digest of previous.accepted_kit_digests) {
        if (validDigest(digest)) {
          accepted.add(digest);
        }
      }
    }
    const transitionRecord = {
      root: resolvedRoot,
      kit_digest: previousDigest,
      accepted_kit_digests: [...accepted].sort(),
    };
    const committedRecord = { root: resolvedRoot, kit_digest: kitDigest };
    const transition = {
      ...registry,
      projects: {
        ...registry.projects,
        [resolvedRoot]: transitionRecord,
      },
    };
    writeManagedProjectRegistry(registryPath, transition);
    publishFaultPoint("after-transition-registry");
    let manifestPublished = false;
    const manifestMatchesPrevious = () => {
      const identity = fs.lstatSync(target, { throwIfNoEntry: false });
      if (previousManifest === null) {
        return identity === undefined;
      }
      return (
        identity !== undefined
        && identity.isFile()
        && !identity.isSymbolicLink()
        && identity.nlink === 1
        && (identity.mode & 0o777) === previousManifest.mode
        && fs.readFileSync(target).equals(previousManifest.content)
      );
    };
    const publishManifest = () => {
      if (!manifestMatchesPrevious()) {
        throw new Error("project manifest changed before publication cutover");
      }
      writeAtomicAtDirectory(
        agentFlowDir,
        agentFlowIdentity,
        "kit.json",
        content,
        0o644,
        "project agent-flow directory",
      );
      manifestPublished = true;
      publishFaultPoint("after-manifest");
    };

    const recordMatches = (left, right) => (
      JSON.stringify(left) === JSON.stringify(right)
    );
    const writeCommittedRecord = () => {
      const { registry: current } = readManagedProjectRegistry(home);
      if (!recordMatches(current.projects[resolvedRoot], transitionRecord)) {
        throw new Error("managed project registry changed before publication commit");
      }
      writeManagedProjectRegistry(registryPath, {
        ...current,
        projects: {
          ...current.projects,
          [resolvedRoot]: committedRecord,
        },
      });
    };
    const rollbackPublication = () => {
      const { registry: current } = readManagedProjectRegistry(home);
      const currentRecord = current.projects[resolvedRoot];
      if (
        !recordMatches(currentRecord, transitionRecord)
        && !recordMatches(currentRecord, committedRecord)
      ) {
        throw new Error("managed project registry changed before publication rollback");
      }
      if (manifestPublished) {
        const identity = fs.lstatSync(target);
        if (
          !identity.isFile()
          || identity.isSymbolicLink()
          || identity.nlink !== 1
          || !fs.readFileSync(target).equals(content)
        ) {
          throw new Error("project manifest changed before publication rollback");
        }
        if (previousManifest === null) {
          fs.unlinkSync(target);
        } else {
          writeAtomicAtDirectory(
            agentFlowDir,
            agentFlowIdentity,
            "kit.json",
            previousManifest.content,
            previousManifest.mode,
            "project agent-flow directory",
          );
        }
        manifestPublished = false;
      } else if (!manifestMatchesPrevious()) {
        throw new Error("project manifest changed before publication rollback");
      }
      const { registry: transitioned } = readManagedProjectRegistry(home);
      if (!recordMatches(transitioned.projects[resolvedRoot], transitionRecord)) {
        throw new Error("managed project registry changed during publication rollback");
      }
      const projects = { ...transitioned.projects };
      if (previous === null) {
        delete projects[resolvedRoot];
      } else {
        projects[resolvedRoot] = previous;
      }
      writeManagedProjectRegistry(registryPath, { ...transitioned, projects });
      return target;
    };
    if (!deferCommit) {
      publishManifest();
      writeCommittedRecord();
      publishFaultPoint("after-project-registry-commit");
      return target;
    }

    const commit = (applyCutover = null) => withSharedHookLock(home, () => {
      const { registry: current } = readManagedProjectRegistry(home);
      if (!recordMatches(current.projects[resolvedRoot], transitionRecord)) {
        throw new Error("managed project registry changed before publication cutover");
      }
      if (applyCutover !== null && typeof applyCutover !== "function") {
        throw new TypeError("publication cutover must be a function");
      }
      let cutover = null;
      let registryCommitted = false;
      try {
        cutover = applyCutover === null ? null : applyCutover();
        if (
          cutover !== null
          && (
            typeof cutover !== "object"
            || typeof cutover.commit !== "function"
            || typeof cutover.rollback !== "function"
          )
        ) {
          throw new TypeError("publication cutover must return a commit/rollback transaction");
        }
        publishFaultPoint("after-cutover");
        publishManifest();
        writeCommittedRecord();
        registryCommitted = true;
        publishFaultPoint("after-project-registry-commit");
      } catch (error) {
        if (registryCommitted) {
          throw error;
        }
        const rollbackErrors = [];
        if (cutover !== null && typeof cutover?.rollback === "function") {
          try {
            cutover.rollback();
          } catch (rollbackError) {
            rollbackErrors.push(
              `cutover rollback failed: ${
                rollbackError instanceof Error ? rollbackError.message : String(rollbackError)
              }`,
            );
          }
        }
        try {
          rollbackPublication();
        } catch (rollbackError) {
          rollbackErrors.push(
            `project publication rollback failed: ${
              rollbackError instanceof Error ? rollbackError.message : String(rollbackError)
            }`,
          );
        }
        if (rollbackErrors.length > 0) {
          throw new Error(
            `${error instanceof Error ? error.message : String(error)}; ${rollbackErrors.join("; ")}`,
          );
        }
        throw error;
      }
      if (cutover !== null) {
        cutover.commit();
      }
      return target;
    });
    const rollback = () => withSharedHookLock(home, rollbackPublication);
    return { path: target, commit, rollback };
  });
}

function backupAdapterConflict(target, content, mode) {
  for (let index = 1; ; index += 1) {
    const backup = `${target}.bak.${index}`;
    try {
      fs.writeFileSync(backup, content, { mode, flag: "wx" });
      fs.chmodSync(backup, mode);
      return backup;
    } catch (error) {
      if (error?.code !== "EEXIST") {
        throw error;
      }
    }
  }
}

export function recordOmpAdapter({
  adapterPath,
  content,
  force = false,
  homeDir = agentFlowHome(),
}) {
  const resolvedHome = resolveHomePath(homeDir);
  const statePath = canonicalSharedStatePath(resolvedHome);
  const resolvedAdapter = path.resolve(adapterPath);
  const nextContent = Buffer.isBuffer(content) ? content : Buffer.from(content, "utf8");
  const digest = sha256(nextContent);
  ensureOwnedDirectory(
    path.dirname(resolvedAdapter),
    0o700,
    "global OMP adapter directory",
  );
  return withSharedHookLock(resolvedHome, () => {
    const state = readOwnedJson(statePath, "shared hook runtime state");
    if (
      state.protocol_version !== SHARED_HOOK_PROTOCOL_VERSION
      || !validDigest(state.active_runtime_digest)
      || !validDigest(state.launcher_digest)
    ) {
      throw new Error(`shared hook runtime state is invalid: ${statePath}`);
    }
    let current = null;
    let currentMode = 0o644;
    try {
      const identity = fs.lstatSync(resolvedAdapter);
      if (!identity.isFile() || identity.isSymbolicLink()) {
        throw new Error(`global OMP adapter is not a regular file: ${resolvedAdapter}`);
      }
      if (identity.nlink !== 1) {
        throw new Error(`global OMP adapter has unsafe link count: ${resolvedAdapter}`);
      }
      if (
        typeof process.getuid === "function"
        && identity.uid !== process.getuid()
      ) {
        throw new Error(`global OMP adapter is not owned by the current user: ${resolvedAdapter}`);
      }
      current = fs.readFileSync(resolvedAdapter);
      currentMode = identity.mode & 0o777;
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
    const currentDigest = current === null ? null : sha256(current);
    const recordedDigests = new Set([
      state.omp_adapter?.digest,
      ...(Array.isArray(state.omp_adapter?.accepted_digests)
        ? state.omp_adapter.accepted_digests
        : []),
    ].filter(validDigest));
    const kitOwned = current?.toString("utf8").startsWith(
      "// agent-flow: managed omp extension\n",
    ) ?? true;
    if (!kitOwned && !force) {
      throw new Error(
        `agent-flow: ${resolvedAdapter} is not kit-managed; refusing to replace it. `
        + "Re-run with --force-managed to replace it.",
      );
    }
    if (!kitOwned && current !== null) {
      backupAdapterConflict(resolvedAdapter, current, currentMode);
    }
    if (
      kitOwned
      && current !== null
      && currentDigest !== digest
      && !recordedDigests.has(currentDigest)
    ) {
      if (!force) {
        throw new Error(
          `agent-flow: ${resolvedAdapter} belongs to a different managed installation; `
          + "re-run with --force-managed to migrate it.",
        );
      }
      backupAdapterConflict(resolvedAdapter, current, currentMode);
    }
    const previousDigest = state.omp_adapter?.digest;
    const acceptedDigests = [...new Set([
      ...recordedDigests,
      digest,
    ])];
    const transition = {
      ...state,
      omp_adapter: {
        path: resolvedAdapter,
        digest: validDigest(previousDigest) ? previousDigest : digest,
        accepted_digests: acceptedDigests,
      },
    };
    writeAtomic(statePath, `${JSON.stringify(transition, null, 2)}\n`, 0o600);
    publishFaultPoint("after-omp-transition");
    const changed = (
      current === null
      || !current.equals(nextContent)
      || currentMode !== 0o644
    );
    if (changed) {
      writeAtomic(resolvedAdapter, nextContent, 0o644);
    }
    ensureOwnedDirectory(
      path.dirname(resolvedAdapter),
      0o700,
      "global OMP adapter directory",
    );
    publishFaultPoint("after-omp-adapter");
    const nextState = {
      ...transition,
      omp_adapter: { path: resolvedAdapter, digest },
    };
    writeAtomic(statePath, `${JSON.stringify(nextState, null, 2)}\n`, 0o600);
    verifyRuntimeInstall({
      homeDir: resolvedHome,
      runtimeDigest: nextState.active_runtime_digest,
      launcherDigest: nextState.launcher_digest,
      statePath,
    });
    verifyFile(resolvedAdapter, digest, 0o644, "global OMP adapter");
    return { ...nextState.omp_adapter, installed: changed };
  });
}
