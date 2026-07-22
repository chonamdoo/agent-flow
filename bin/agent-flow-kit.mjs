#!/usr/bin/env node

import fs from "node:fs";
import crypto from "node:crypto";
import path from "node:path";
import process from "node:process";
import nodeOs from "node:os";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  CODE_SKILL_PHASES,
  SKILL_DEPENDENCIES,
  canonicalizeRuntimeSkillProviderMetadata,
  canonicalizeInstallSelectionCompatibility,
  discoverAutomaticExternalSkillNames,
  hashDirectoryTree,
  hashSkillTree,
  readSkillDocument,
  mergeInstallSelectionWithPrevious,
  profileManagedHostOnlySkillNames,
  isPortableSkillName,
  portableSkillCasefold,
  mergeResolvedSkillClosure,
  resolveInstallSelection,
  resolveProfileSkillSources,
  resolveSkillProviderIndex,
  resolveRuntimeSkillPlan,
} from "../lib/skill-selection.mjs";
import {
  canonicalizeSkillCompatibilitySelection,
  normalizeSkillCompatibility,
  SkillResolutionError,
  validateConcreteSkillCompatibility,
} from "../lib/skill-compatibility.mjs";
import {
  canonicalizeHostNeutralSkillProviderClaim,
  createSkillProviderAdapterRegistry,
} from "../lib/skill-provider-registry.mjs";
import { detectActiveHost } from "../lib/host-detection.mjs";
import { evaluateDeclaredArtifacts, evaluatePhaseContract } from "../lib/phase-contract.mjs";

const command = process.argv[2];
const configuredAgentFlowCommand = process.env.AGENT_FLOW_PROJECT_LAUNCHER;
let AGENT_FLOW_COMMAND = configuredAgentFlowCommand && path.isAbsolute(configuredAgentFlowCommand)
  ? shellSingleQuote(configuredAgentFlowCommand)
  : "agent-flow";
const HOME = process.env.HOME || process.env.USERPROFILE || "";
const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const RUNTIME_PYTHON_RELATIVE = path.join(".agent-flow", "runtime", "python");
const RUNTIME_NODE_RELATIVE = path.join(".agent-flow", "runtime", "node");
const PROJECT_LAUNCHER_RELATIVE = path.join(".agent-flow", "bin", "agent-flow");
const NODE_RUN_SUBCOMMANDS = new Set(["start", "status", "next", "advance", "push-watch", "push-watch-tick"]);
let installArgs = process.argv.slice(3);
const forceManaged = installArgs.includes("--force-managed");
let cachedFullFeatureWorkflow = null;
let cachedProjectPythonPath = null;
let cachedProjectGitPath = null;
let activeManagedInstallTransaction = null;
const PROJECT_SKILL_HOSTS = Object.freeze(["claude", "codex", "omp"]);
const SKILL_PROVIDER_ADAPTER_REGISTRY = createSkillProviderAdapterRegistry();
const SKILL_LINKS_COMMITMENT_VERSION = 2;
const MANAGED_HOST_FILES_VERSION = 1;
const MANAGED_HOST_FILES_COMMITMENT_VERSION = 1;
const MANAGED_HOOK_CONTRACT_VERSION = 3;
const MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION = 2;
const MANAGED_HOOK_SCRIPT_NAMES = Object.freeze([
  "guard-worktree.sh",
  "guard-worktree-write.py",
  "guard-protected-branch.sh",
  "show-phase-status.sh",
  "comment-checker.py",
]);
const MANAGED_HOOK_CONFIG_PATHS = Object.freeze([
  ".Codex/hooks.json",
  ".codex/hooks.json",
  ".claude/settings.json",
]);
const CANONICAL_HOOK_POLICY = Object.freeze({
  bashPre: Object.freeze([
    "guard-worktree.sh",
    "guard-protected-branch.sh",
  ]),
  writePre: Object.freeze([]),
  writePost: Object.freeze(["comment-checker.py"]),
  stop: Object.freeze(["show-phase-status.sh"]),
});
const REQUIRED_MANAGED_HOST_FILES = Object.freeze([
  ".Codex/agents/code-reviewer.md",
  ".claude/agents/code-reviewer.md",
  ".omp/agents/code-reviewer.md",
  ".omp/extensions/agent-flow-hooks.ts",
]);
const WRITE_TOOL_MATCHER = "^(apply_patch|Write|Edit|MultiEdit|NotebookEdit|Eval|Python|Notebook|write|edit|multi_edit|multiedit|notebook_edit|notebookedit|eval|python|notebook)$";
const BUNDLED_HOST_SKILL_NAMES = new Set([
  "agent-flow",
  "android-appshell-error-handling",
  "comment-authoring-discipline",
  "comment-checker",
  "ios-app-shell-error-handling",
  "react-app-shell-error-handling",
  "react-native-app-shell-error-handling",
]);
const PROFILE_MANAGED_HOST_ONLY_SKILLS = profileManagedHostOnlySkillNames(
  path.join(KIT_ROOT, "profiles"),
);
const GENERATED_PROJECT_SKILL_NAMES = new Set([
  "agent-flow",
  "architecture-reviewer",
  "full-feature-workflow",
  "plan-reviewer",
  "product-brief",
  "push-watch",
]);
const BUNDLED_SKILL_ROOT_FILE_NAMES = new Set(
  fs.readdirSync(path.join(KIT_ROOT, "skills"), { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name),
);
const PROJECT_COMMAND_SKILL_NAMES = new Set(["agent-flow", "full-feature-workflow", "push-watch"]);
const PROJECT_SKILL_DISCOVERY_IGNORED_NAMES = new Set([
  ...PROFILE_MANAGED_HOST_ONLY_SKILLS,
]);
function installProject(rootOverride = null) {
  const requestedRoot = rootOverride ? path.resolve(rootOverride) : process.cwd();
  const managedWorktreeRoot = resolveManagedWorktreeRoot(requestedRoot);
  if (
    managedWorktreeRoot
  ) {
    if (fs.existsSync(path.join(managedWorktreeRoot, ".agent-flow", "kit.json"))) {
      console.log(`agent-flow already installed root=${managedWorktreeRoot}`);
      console.log("worktree install skipped; reinstall from the leader checkout if needed");
    } else {
      throw new Error("managed worktree install blocked; install from the leader checkout first");
    }
    return;
  }
  const root = resolveInstallRoot(requestedRoot);
  assertLeaderMutationSource(root, "install");
  const agentFlowDir = path.join(root, ".agent-flow");
  if (pathHasSymlink(root, agentFlowDir)) {
    throw new Error(`managed install root contains a symlink: ${agentFlowDir}`);
  }
  fs.mkdirSync(agentFlowDir, { recursive: true });
  const lock = acquireProjectInstallLock(root, agentFlowDir);
  const context = { transaction: null };
  try {
    recoverInterruptedSkillTransaction(root, agentFlowDir, lock.recovery_token);
    installProjectUnlocked(root, context, lock);
    commitSkillInstallTransaction(context.transaction);
  } catch (error) {
    let rollbackError = null;
    try {
      sealManagedInstallMutations(context.transaction);
      rollbackSkillInstallTransaction(context.transaction);
    } catch (failure) {
      rollbackError = failure;
    }
    if (rollbackError) {
      throw new Error(`${error instanceof Error ? error.message : String(error)}; rollback failed: ${rollbackError.message}`);
    }
    throw error;
  } finally {
    if (!fs.existsSync(path.join(agentFlowDir, "install-transaction"))) {
      releaseProjectInstallLock(lock);
    }
  }
}

function syncProject(rootOverride = null) {
  const requestedRoot = rootOverride ? path.resolve(rootOverride) : process.cwd();
  if (resolveManagedWorktreeRoot(requestedRoot)) {
    throw new Error("managed worktree sync blocked; run sync from the leader checkout");
  }
  const root = resolveInstallRoot(requestedRoot);
  const agentFlowDir = path.join(root, ".agent-flow");
  fs.mkdirSync(agentFlowDir, { recursive: true });
  const lock = acquireProjectInstallLock(root, agentFlowDir);
  try {
    syncProjectAgentDocumentsTransactional(root, agentFlowDir);
    console.log(`agent-flow documents synced root=${root}`);
  } finally {
    releaseProjectInstallLock(lock);
  }
}

function installProjectPythonRuntime(root) {
  const runtimeRoot = path.join(root, RUNTIME_PYTHON_RELATIVE);
  withManagedInstallMutation(runtimeRoot, (managedRuntime) => {
    const boundary = managedWriteBoundary(managedRuntime);
    withManagedDirectoryCwd(boundary, path.dirname(managedRuntime), true, () => {
      const name = path.basename(managedRuntime);
      if (lstatIfExists(name)) fs.rmSync(name, { recursive: true, force: true });
      fs.mkdirSync(name, { mode: 0o755 });
      fs.chmodSync(name, 0o755);
    });
    copyRuntimeTree(
      path.join(KIT_ROOT, "src", "agent_flow"),
      managedRuntime,
      "agent_flow",
    );
  });
}

function installProjectNodeRuntime(root) {
  const runtimeRoot = path.join(root, RUNTIME_NODE_RELATIVE);
  if (!samePath(runtimeRoot, KIT_ROOT)) {
    const bundledDirectories = [
      ["lib", "lib"],
      ["workflows", "workflows"],
      ["profiles", "profiles"],
      ["skills", "skills"],
      ["templates", "templates"],
      ["scripts", "scripts"],
      ["bootstrap", "bootstrap"],
      [path.join("src", "agent_flow"), path.join("src", "agent_flow")],
      [path.join(".Codex", "agents"), path.join(".Codex", "agents")],
      [path.join(".Codex", "rules"), path.join(".Codex", "rules")],
      [path.join(".Codex", "context"), path.join(".Codex", "context")],
      [path.join(".claude", "agents"), path.join(".claude", "agents")],
    ];
    withManagedInstallMutation(runtimeRoot, (managedRuntime) => {
      const boundary = managedWriteBoundary(managedRuntime);
      withManagedDirectoryCwd(boundary, path.dirname(managedRuntime), true, () => {
        const name = path.basename(managedRuntime);
        if (lstatIfExists(name)) fs.rmSync(name, { recursive: true, force: true });
        fs.mkdirSync(name, { mode: 0o755 });
        fs.chmodSync(name, 0o755);
      });
      for (const [source, destination] of bundledDirectories) {
        copyRuntimeTree(path.join(KIT_ROOT, source), managedRuntime, destination);
      }
      copyRuntimeTree(path.join(KIT_ROOT, "bin"), managedRuntime, "bin");
    });
  }
  const portableAuthority = {
    version: 1,
    runtime_integrity: treeIntegrity(runtimeRoot),
    python_runtime_integrity: treeIntegrity(path.join(root, RUNTIME_PYTHON_RELATIVE)),
  };
  writeManagedExecutableFile(
    path.join(root, PROJECT_LAUNCHER_RELATIVE),
    projectLauncherSource(root, portableAuthority),
  );
}

function copyRuntimeTree(sourceRoot, runtimeRoot, destinationRelative) {
  const source = fs.lstatSync(sourceRoot);
  if (!source.isDirectory() || source.isSymbolicLink()) {
    throw new Error(`project runtime source is unsafe: ${sourceRoot}`);
  }
  const destinationRoot = path.join(runtimeRoot, destinationRelative);
  ensureManagedDirectory(destinationRoot, runtimeRoot, 0o755);
  const visit = (sourceDirectory, destinationDirectory) => {
    for (const entry of fs.readdirSync(sourceDirectory, { withFileTypes: true })) {
      if (entry.name === "__pycache__" || entry.name.endsWith(".pyc")) continue;
      const sourcePath = path.join(sourceDirectory, entry.name);
      const destinationPath = path.join(destinationDirectory, entry.name);
      const stat = fs.lstatSync(sourcePath);
      if (stat.isSymbolicLink()) {
        throw new Error(`project runtime source contains a symlink: ${sourcePath}`);
      }
      if (entry.isDirectory()) {
        ensureManagedDirectory(destinationPath, runtimeRoot, 0o755);
        visit(sourcePath, destinationPath);
      } else if (entry.isFile()) {
        writeManagedRegularFile(
          destinationPath,
          fs.readFileSync(sourcePath),
          runtimeRoot,
          stat.mode & 0o777,
        );
      } else {
        throw new Error(`project runtime source contains a special file: ${sourcePath}`);
      }
    }
  };
  visit(sourceRoot, destinationRoot);
}

function projectLauncherSource(root, portableAuthority) {
  const nodeContract = originalNodeAuthority();
  const pythonPath = shellSingleQuote(projectPythonPath());
  const gitPath = shellSingleQuote(projectGitPath());
  const launcherPath = shellSingleQuote(path.join(root, PROJECT_LAUNCHER_RELATIVE));
  const authenticatedNode = authenticatedExecutableShellCommand(
    nodeContract,
    '"$launcher_dir/../runtime/node/bin/agent-flow-kit.mjs" "$@"',
  );
  const portableRuntimeBootstrap = portableRuntimeBootstrapShellCommand(nodeContract, portableAuthority);
  return `#!/bin/sh
unset NODE_OPTIONS NODE_PATH BASH_ENV ENV LD_AUDIT LD_BIND_NOW LD_DEBUG LD_DEBUG_OUTPUT LD_DYNAMIC_WEAK LD_HWCAP_MASK LD_LIBRARY_PATH LD_ORIGIN_PATH LD_PRELOAD LD_PROFILE LD_SHOW_AUXV LD_TRACE_LOADED_OBJECTS LD_USE_LOAD_BIAS LD_VERBOSE LD_WARN DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH DYLD_FALLBACK_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_ROOT_PATH DYLD_IMAGE_SUFFIX DYLD_SHARED_REGION PYTHONPATH PYTHONHOME PYTHONSTARTUP
PYTHON=${pythonPath}
PYTHON_EXECUTABLE=${pythonPath}
AGENT_FLOW_PYTHON_EXECUTABLE=${pythonPath}
AGENT_FLOW_GIT_EXECUTABLE=${gitPath}
AGENT_FLOW_PROJECT_LAUNCHER=${launcherPath}
export PYTHON PYTHON_EXECUTABLE AGENT_FLOW_PYTHON_EXECUTABLE AGENT_FLOW_GIT_EXECUTABLE AGENT_FLOW_PROJECT_LAUNCHER
launcher_dir=\${0%/*}
if [ "$launcher_dir" = "$0" ]; then launcher_dir=.; fi
${portableRuntimeBootstrap}
exec ${authenticatedNode}
`;
}

function shellSingleQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function portableRuntimeBootstrapShellCommand(nodeContract, portableAuthority) {
  const contractedNode = shellSingleQuote(nodeContract.path);
  const encodedAuthority = shellSingleQuote(
    Buffer.from(JSON.stringify(portableAuthority), "utf8").toString("base64"),
  );
  return `if [ ! -e ${contractedNode} ] || [ -L ${contractedNode} ] || [ "$0" != "$AGENT_FLOW_PROJECT_LAUNCHER" ]; then
  ${shellSingleQuote("/usr/bin/python3")} -I -B -c ${shellSingleQuote(PORTABLE_RUNTIME_BOOTSTRAP)} "$0" "$launcher_dir/../runtime/node/bin/agent-flow-kit.mjs" ${encodedAuthority} "$@"
  portable_status=$?
  if [ "$portable_status" -ne 126 ]; then exit "$portable_status"; fi
fi`;
}

function isRuntimeRecoveryCommand() {
  return command === "install"
    || command === "sync"
    || command === "status"
    || command === "continue"
    || command === "abort"
    || (command === "run" && process.argv[3] === "install")
    || (command === "worktree" && process.argv[3] === "repin")
    || (command === "__sandboxed-mutation" && process.argv[3] === "install");
}

function assertRecoveryTargetsProject(args, root) {
  const rootArguments = [];
  for (let index = 0; index < args.length; index += 1) {
    const option = args[index].split("=", 1)[0];
    if (option !== "--root" && "--root".startsWith(option)) {
      throw new Error("abbreviated recovery --root is not allowed");
    }
    if (args[index] === "--root") {
      if (index + 1 >= args.length) throw new Error("recovery --root is missing a value");
      rootArguments.push(args[index + 1]);
    } else if (args[index].startsWith("--root=")) {
      rootArguments.push(args[index].slice("--root=".length));
    }
  }
  const expected = fs.realpathSync(root);
  for (const requested of rootArguments) {
    const candidate = fs.realpathSync(path.resolve(root, requested));
    if (candidate !== expected) {
      throw new Error("recovery --root must target the launcher project");
    }
  }
}


function configuredPythonContractIsCurrent(contract) {
  const configured = String(contract.path || "");
  const resolved = String(contract.resolved_path || "");
  const launcherConfigured = process.env.AGENT_FLOW_PYTHON_EXECUTABLE;
  return path.isAbsolute(configured)
    && fs.realpathSync(configured) === resolved
    && (!launcherConfigured || launcherConfigured === configured);
}


function currentHostGitPath() {
  return fs.existsSync("/usr/bin/git") ? "/usr/bin/git" : resolveExecutablePath("git");
}


function configuredGitPath() {
  if (isRuntimeRecoveryCommand()) return currentHostGitPath();
  return process.env.AGENT_FLOW_GIT_EXECUTABLE || currentHostGitPath();
}


function currentHostPythonPath() {
  return resolveExecutablePath(preferredPython());
}


function reusableConfiguredPythonPath(contract) {
  try {
    if (configuredPythonContractIsCurrent(contract)) return String(contract.path);
  } catch {
    if (isRuntimeRecoveryCommand()) return null;
    throw new Error("project runtime Python contract is invalid");
  }
  if (isRuntimeRecoveryCommand()) return null;
  throw new Error("project runtime Python contract is invalid");
}


function projectPythonPath() {
  if (cachedProjectPythonPath === null) {
    const root = resolveAgentFlowRoot(process.cwd());
    const contract = root
      ? readJsonIfExists(path.join(root, ".agent-flow", "kit.json"))?.project_runtime_contract?.python
      : null;
    cachedProjectPythonPath = contract
      ? reusableConfiguredPythonPath(contract)
      : null;
    if (cachedProjectPythonPath === null) {
      cachedProjectPythonPath = currentHostPythonPath();
    }
  }
  return cachedProjectPythonPath;
}


function projectGitPath() {
  if (cachedProjectGitPath === null) {
    const configured = configuredGitPath();
    const resolved = fs.realpathSync(configured);
    const stat = fs.lstatSync(resolved);
    if (
      !path.isAbsolute(configured)
      || !stat.isFile()
      || stat.isSymbolicLink()
      || (process.platform !== "win32" && stat.uid !== 0)
      || (stat.mode & 0o022) !== 0
    ) {
      throw new Error("trusted git executable is invalid");
    }
    fs.accessSync(resolved, fs.constants.X_OK);
    cachedProjectGitPath = resolved;
  }
  return cachedProjectGitPath;
}


function resolveExecutablePath(candidate) {
  const value = String(candidate);
  const hasPathComponent = path.isAbsolute(value) || value.includes("/") || value.includes("\\");
  const directories = hasPathComponent
    ? [""]
    : String(process.env.PATH || "").split(path.delimiter).filter(Boolean);
  const extensions = process.platform === "win32" && path.extname(value) === ""
    ? String(process.env.PATHEXT || ".EXE;.CMD;.BAT;.COM").split(";").filter(Boolean)
    : [""];
  for (const directory of directories) {
    for (const extension of extensions) {
      const pathName = path.resolve(directory || ".", `${value}${extension}`);
      try {
        const stat = fs.statSync(pathName);
        fs.accessSync(pathName, fs.constants.X_OK);
        if (stat.isFile()) return pathName;
      } catch {
        // 다음 PATH 후보를 확인한다.
      }
    }
  }
  throw new Error(`validated Python executable cannot be resolved: ${candidate}`);
}


function projectRuntimeContract(root) {
  const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  const runtime = path.join(root, RUNTIME_NODE_RELATIVE);
  const pythonRuntime = path.join(root, RUNTIME_PYTHON_RELATIVE);
  if (!fs.existsSync(launcher) || !fs.existsSync(runtime) || !fs.existsSync(pythonRuntime)) {
    throw new Error("project runtime installation is incomplete");
  }
  return {
    version: 3,
    launcher: {
      path: PROJECT_LAUNCHER_RELATIVE.split(path.sep).join("/"),
      sha256: crypto.createHash("sha256").update(fs.readFileSync(launcher)).digest("hex"),
    },
    node: originalNodeAuthority(),
    git: executableContract(projectGitPath()),
    python: {
      path: projectPythonPath(),
      resolved_path: fs.realpathSync(projectPythonPath()),
      ...executableIdentity(projectPythonPath()),
      dependencies: executableDependencyContracts(projectPythonPath()),
    },
    runtime: {
      path: RUNTIME_NODE_RELATIVE.split(path.sep).join("/"),
      integrity: treeIntegrity(runtime),
    },
    python_runtime: {
      path: RUNTIME_PYTHON_RELATIVE.split(path.sep).join("/"),
      integrity: treeIntegrity(pythonRuntime),
    },
  };
}

function executableIdentity(pathName) {
  const resolved = fs.realpathSync(pathName);
  const stat = fs.lstatSync(resolved);
  if (
    !stat.isFile()
    || stat.isSymbolicLink()
    || (stat.mode & 0o022) !== 0
  ) {
    throw new Error(`project runtime executable is unsafe: ${pathName}`);
  }
  return {
    sha256: crypto.createHash("sha256").update(fs.readFileSync(resolved)).digest("hex"),
    device: String(stat.dev),
    inode: String(stat.ino),
    links: String(stat.nlink),
    mode: stat.mode & 0o777,
  };
}

function executableContract(pathName) {
  return {
    path: fs.realpathSync(pathName),
    ...executableIdentity(pathName),
    dependencies: executableDependencyContracts(pathName),
  };
}

function executableDependencyContracts(pathName) {
  const resolved = fs.realpathSync(pathName);
  const libraryRoot = path.resolve(path.dirname(resolved), "..", "lib");
  const dependencies = machoDependencyClosure(resolved);
  if (fs.existsSync(libraryRoot)) {
    for (const name of fs.readdirSync(libraryRoot)) {
      if (
        (name.startsWith("libpython") || name.startsWith("libnode"))
        && name.endsWith(".dylib")
      ) {
        const loadPath = path.join(libraryRoot, name);
        const dependency = fs.realpathSync(loadPath);
        if (!dependencies.has(dependency)) {
          dependencies.set(dependency, { loadPaths: new Set(), loadCommands: new Set() });
        }
        dependencies.get(dependency).loadPaths.add(loadPath);
        dependencies.get(dependency).loadCommands.add(loadPath);
      }
    }
  }
  const paths = [...dependencies.keys()].sort(compareCodePoints);
  const names = paths.map((dependency) => path.basename(dependency));
  if (new Set(names).size !== names.length) {
    throw new Error(`project runtime executable dependency names collide: ${pathName}`);
  }
  return paths.map((dependency) => {
    const metadata = dependencies.get(dependency);
    const loadPaths = [...metadata.loadPaths].sort(compareCodePoints);
    const loadCommands = [...metadata.loadCommands].sort(compareCodePoints);
    const frameworkRelative = machoFrameworkRelativePath(dependency, loadPaths, loadCommands);
    return {
      name: path.basename(dependency),
      path: dependency,
      load_paths: loadPaths,
      load_commands: loadCommands,
      stage_kind: frameworkRelative ? "framework" : "library",
      stage_relative: frameworkRelative || path.basename(dependency),
      ...executableIdentity(dependency),
    };
  });
}

function machoDependencyClosure(pathName) {
  if (process.platform !== "darwin") return new Map();
  const executable = fs.realpathSync(pathName);
  const discovered = new Map();
  const queue = [{ dependency: executable, inheritedRpaths: [] }];
  while (queue.length) {
    const { dependency, inheritedRpaths } = queue.shift();
    const rpaths = [...machoRpaths(dependency, executable), ...inheritedRpaths];
    for (const loadPath of machoLoadPaths(dependency)) {
      if (machoSystemPath(loadPath)) continue;
      const candidate = resolveMachoLoadPath(loadPath, dependency, executable, rpaths);
      if (!candidate) {
        throw new Error(`project runtime Mach-O dependency is unresolved: ${loadPath}`);
      }
      const resolved = fs.realpathSync(candidate);
      if (machoSystemPath(resolved)) continue;
      if (!discovered.has(resolved)) {
        discovered.set(resolved, { loadPaths: new Set(), loadCommands: new Set() });
        queue.push({ dependency: resolved, inheritedRpaths: rpaths });
      }
      discovered.get(resolved).loadPaths.add(candidate);
      discovered.get(resolved).loadCommands.add(loadPath);
    }
  }
  return discovered;
}

function machoLoadPaths(pathName) {
  if (process.env.AGENT_FLOW_TEST_OTOOL_FAILURE === "1") {
    throw new Error(`project runtime Mach-O dependency inspection failed: ${pathName}`);
  }
  const result = safeSpawnSync("/usr/bin/otool", ["-L", pathName], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error || result.status !== 0) {
    throw new Error(`project runtime Mach-O dependency inspection failed: ${pathName}`);
  }
  return String(result.stdout || "")
    .split(/\r?\n/)
    .slice(1)
    .map((line) => line.match(/^\s+(.+?)\s+\(compatibility version/)?.[1])
    .filter(Boolean);
}

function machoRpaths(pathName, executable) {
  const result = safeSpawnSync("/usr/bin/otool", ["-l", pathName], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error || result.status !== 0) {
    throw new Error(`project runtime Mach-O rpath inspection failed: ${pathName}`);
  }
  const lines = String(result.stdout || "").split(/\r?\n/);
  const rpaths = [];
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index].trim() !== "cmd LC_RPATH") continue;
    for (let cursor = index + 1; cursor < Math.min(lines.length, index + 5); cursor += 1) {
      const match = lines[cursor].match(/^\s*path\s+(.+?)\s+\(offset\s+\d+\)$/);
      if (match) {
        const expanded = expandMachoPath(match[1], pathName, executable);
        if (expanded) rpaths.push(expanded);
        break;
      }
    }
  }
  return rpaths;
}

function expandMachoPath(value, loader, executable) {
  if (value === "@loader_path") return path.dirname(loader);
  if (value.startsWith("@loader_path/")) return path.resolve(path.dirname(loader), value.slice(13));
  if (value === "@executable_path") return path.dirname(executable);
  if (value.startsWith("@executable_path/")) return path.resolve(path.dirname(executable), value.slice(17));
  return path.isAbsolute(value) ? path.resolve(value) : null;
}

function resolveMachoLoadPath(value, loader, executable, rpaths) {
  if (value.startsWith("@rpath/")) {
    const suffix = value.slice(7);
    return rpaths.map((root) => path.join(root, suffix)).find((candidate) => fs.existsSync(candidate)) || null;
  }
  const expanded = expandMachoPath(value, loader, executable);
  return expanded && fs.existsSync(expanded) ? expanded : null;
}

function machoSystemPath(value) {
  return value.startsWith("/usr/lib/") || value.startsWith("/System/Library/");
}

function machoFrameworkRelativePath(dependency, loadPaths, loadCommands) {
  for (const value of [dependency, ...loadPaths, ...loadCommands]) {
    const match = String(value).replaceAll("\\", "/").match(/(?:^|\/)([^/]+\.framework\/(?:Versions\/[^/]+\/)?[^/]+)$/);
    if (match) return match[1];
  }
  return null;
}

function assertExecutableDependencies(pathName, expected) {
  if (canonicalJson(executableDependencyContracts(pathName)) !== canonicalJson(expected || [])) {
    throw new Error(`project runtime executable dependencies changed: ${pathName}`);
  }
}

function originalNodeAuthority() {
  const encoded = process.env.AGENT_FLOW_ORIGINAL_NODE_AUTHORITY;
  if (encoded) {
    let expected = null;
    try {
      expected = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
      const current = executableIdentity(expected.path);
      if (
        current.sha256 !== expected.sha256
        || current.device !== expected.device
        || current.inode !== expected.inode
        || current.mode !== expected.mode
        || Number.parseInt(current.links, 10) < Number.parseInt(expected.links, 10)
      ) throw new Error("identity mismatch");
      if (expected.portable_selected === true) {
        const root = resolveAgentFlowRoot(process.cwd());
        if (!root || !portableRecoveryAuthorityIsValid(root)) {
          throw new Error("portable authority mismatch");
        }
        return executableContract(expected.path);
      }
      assertExecutableDependencies(expected.path, expected.dependencies);
    } catch {
      throw new Error("project runtime original Node authority is invalid");
    }
    return expected;
  }
  return executableContract(process.execPath);
}

function assertExecutableIdentity(pathName, expected, allowAdditionalLinks = false) {
  const current = executableIdentity(pathName);
  if (
    current.sha256 !== expected.sha256
    || current.device !== expected.device
    || (allowAdditionalLinks
      ? Number.parseInt(current.links, 10) < Number.parseInt(expected.links, 10)
      : current.links !== expected.links)
    || ((allowAdditionalLinks || current.links === "1") && current.inode !== expected.inode)
    || current.mode !== expected.mode
  ) {
    throw new Error(`project runtime executable identity changed: ${pathName}`);
  }
}

function projectRuntimeContractCommitment(contract) {
  return crypto.createHash("sha256").update(JSON.stringify([
    contract.version,
    contract.launcher.path,
    contract.launcher.sha256,
    contract.node.path,
    contract.node.sha256,
    contract.node.device,
    contract.node.inode,
    contract.node.links,
    contract.node.mode,
    canonicalJson(contract.node.dependencies || []),
    contract.git.path,
    contract.git.sha256,
    contract.git.device,
    contract.git.inode,
    contract.git.links,
    contract.git.mode,
    canonicalJson(contract.git.dependencies || []),
    contract.python.path,
    contract.python.resolved_path,
    contract.python.sha256,
    contract.python.device,
    contract.python.inode,
    contract.python.links,
    contract.python.mode,
    canonicalJson(contract.python.dependencies || []),
    contract.runtime.path,
    contract.runtime.integrity,
    contract.python_runtime.path,
    contract.python_runtime.integrity,
  ])).digest("hex");
}

function assertProjectRuntimeContract(root) {
  const kit = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"));
  const contract = kit?.project_runtime_contract;
  if (
    !contract
    || contract.version !== 3
    || kit.project_runtime_contract_commitment_version !== 1
    || kit.project_runtime_contract_commitment !== projectRuntimeContractCommitment(contract)
  ) {
    throw new Error("project runtime contract commitment is invalid");
  }
  const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  const nodeRuntime = path.join(root, RUNTIME_NODE_RELATIVE);
  const pythonRuntime = path.join(root, RUNTIME_PYTHON_RELATIVE);
  if (
    crypto.createHash("sha256").update(fs.readFileSync(launcher)).digest("hex") !== contract.launcher.sha256
    || treeIntegrity(nodeRuntime) !== contract.runtime.integrity
    || treeIntegrity(pythonRuntime) !== contract.python_runtime.integrity
    || fs.realpathSync(contract.git.path) !== contract.git.path
    || fs.realpathSync(contract.python.path) !== contract.python.resolved_path
  ) {
    throw new Error("project runtime contract no longer matches installed files");
  }
  const runningNode = executableIdentity(process.execPath);
  const originalNode = originalNodeAuthority();
  if (
    runningNode.sha256 !== contract.node.sha256
    || runningNode.mode !== contract.node.mode
    || originalNode.path !== contract.node.path
    || originalNode.sha256 !== contract.node.sha256
    || originalNode.device !== contract.node.device
    || originalNode.inode !== contract.node.inode
    || originalNode.mode !== contract.node.mode
    || Number.parseInt(originalNode.links, 10) < Number.parseInt(contract.node.links, 10)
  ) throw new Error("project runtime running Node identity changed");
  assertExecutableIdentity(contract.node.path, contract.node, true);
  assertExecutableIdentity(contract.git.path, contract.git);
  assertExecutableIdentity(contract.python.resolved_path, contract.python);
  assertExecutableDependencies(contract.node.path, contract.node.dependencies);
  assertExecutableDependencies(contract.git.path, contract.git.dependencies);
  assertExecutableDependencies(contract.python.resolved_path, contract.python.dependencies);
  return contract;
}

function projectRuntimeContentMatchesContract(root) {
  try {
    const kit = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"));
    const contract = kit?.project_runtime_contract;
    return Boolean(
      contract
      && contract.version === 3
      && kit.project_runtime_contract_commitment_version === 1
      && kit.project_runtime_contract_commitment === projectRuntimeContractCommitment(contract)
      && contract.launcher.path === PROJECT_LAUNCHER_RELATIVE.split(path.sep).join("/")
      && contract.runtime.path === RUNTIME_NODE_RELATIVE.split(path.sep).join("/")
      && contract.python_runtime.path === RUNTIME_PYTHON_RELATIVE.split(path.sep).join("/")
      && crypto.createHash("sha256")
        .update(fs.readFileSync(path.join(root, PROJECT_LAUNCHER_RELATIVE)))
        .digest("hex") === contract.launcher.sha256
      && treeIntegrity(path.join(root, RUNTIME_NODE_RELATIVE)) === contract.runtime.integrity
      && treeIntegrity(path.join(root, RUNTIME_PYTHON_RELATIVE)) === contract.python_runtime.integrity
    );
  } catch {
    return false;
  }
}


function contractedExecutableIsMissing(expected, pathKey = "path") {
  try {
    fs.lstatSync(String(expected[pathKey]));
    return false;
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") return true;
    throw error;
  }
}


function validateExistingRuntimeExecutable(expected, pathKey = "path", allowAdditionalLinks = false) {
  if (contractedExecutableIsMissing(expected, pathKey)) return false;
  const configured = String(expected[pathKey]);
  if (
    !path.isAbsolute(configured)
    || fs.realpathSync(configured) !== configured
    || (pathKey === "resolved_path" && fs.realpathSync(String(expected.path)) !== configured)
  ) {
    throw new Error(`project runtime executable identity changed: ${configured}`);
  }
  assertExecutableIdentity(configured, expected, allowAdditionalLinks);
  assertExecutableDependencies(configured, expected.dependencies);
  return true;
}
function validateExistingPythonRuntimeExecutable(expected) {
  const configured = String(expected.path);
  const resolved = String(expected.resolved_path);
  if (!path.isAbsolute(configured) || !path.isAbsolute(resolved)) {
    throw new Error(`project runtime executable identity changed: ${configured}`);
  }
  const configuredMissing = contractedExecutableIsMissing(expected, "path");
  const resolvedMissing = contractedExecutableIsMissing(expected, "resolved_path");
  if (!configuredMissing) {
    let currentResolved;
    try {
      currentResolved = fs.realpathSync(configured);
    } catch (error) {
      if (
        resolvedMissing
        && (error?.code === "ENOENT" || error?.code === "ENOTDIR")
      ) return false;
      throw error;
    }
    if (currentResolved !== resolved) {
      throw new Error(`project runtime executable identity changed: ${configured}`);
    }
  }
  if (resolvedMissing) return false;
  assertExecutableIdentity(resolved, expected);
  assertExecutableDependencies(resolved, expected.dependencies);
  return !configuredMissing;
}




function runtimeContractRequiresPortableRepin(root) {
  if (!projectRuntimeContentMatchesContract(root)) return false;
  const contract = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"))?.project_runtime_contract;
  let missing = false;
  try {
    missing = !validateExistingRuntimeExecutable(contract.node, "path", true) || missing;
    missing = !validateExistingRuntimeExecutable(contract.git) || missing;
    missing = !validateExistingPythonRuntimeExecutable(contract.python) || missing;
  } catch {
    return false;
  }
  return missing;
}


function assertExistingRuntimeExecutablesUntampered(root) {
  const kit = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"));
  const contract = kit?.project_runtime_contract;
  if (
    !contract
    || contract.version !== 3
    || kit.project_runtime_contract_commitment_version !== 1
    || kit.project_runtime_contract_commitment !== projectRuntimeContractCommitment(contract)
  ) return;
  validateExistingRuntimeExecutable(contract.node, "path", true);
  validateExistingRuntimeExecutable(contract.git);
  validateExistingPythonRuntimeExecutable(contract.python);
}


function assertProjectRuntimeReady(root) {
  try {
    return assertProjectRuntimeContract(root);
  } catch (error) {
    if (!runtimeContractRequiresPortableRepin(root)) throw error;
    throw new Error(
      "project runtime re-pin required for this machine; run: agent-flow-kit install --force-managed",
    );
  }
}


const PORTABLE_RUNTIME_BOOTSTRAP = [
  "import base64,hashlib,json,os,shutil,stat,subprocess,sys,tempfile",
  "launcher=os.path.realpath(sys.argv[1]); runtime=os.path.realpath(sys.argv[2]); authority=json.loads(base64.b64decode(sys.argv[3],validate=True)); command=sys.argv[4:]",
  "def digest_fd(descriptor):",
  " os.lseek(descriptor,0,os.SEEK_SET); value=hashlib.sha256()",
  " while True:",
  "  chunk=os.read(descriptor,1024*1024)",
  "  if not chunk: break",
  "  value.update(chunk)",
  " os.lseek(descriptor,0,os.SEEK_SET); return value.hexdigest()",
  "def digest(path):",
  " descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))",
  " try: return digest_fd(descriptor)",
  " finally: os.close(descriptor)",
  "def tree_integrity(root):",
  " entries=[]",
  " def visit(current,relative):",
  "  metadata=os.lstat(current)",
  "  if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode): raise OSError('unsafe runtime root')",
  "  entries.append({'path':relative,'type':'directory','mode':stat.S_IMODE(metadata.st_mode)})",
  "  for name in sorted(os.listdir(current)):",
  "   child=os.path.join(current,name); child_relative=relative+'/'+name if relative else name; child_metadata=os.lstat(child)",
  "   if stat.S_ISLNK(child_metadata.st_mode): raise OSError('runtime symlink')",
  "   if stat.S_ISDIR(child_metadata.st_mode): visit(child,child_relative)",
  "   elif stat.S_ISREG(child_metadata.st_mode): entries.append({'path':child_relative,'type':'file','mode':stat.S_IMODE(child_metadata.st_mode),'sha256':digest(child)})",
  "   else: raise OSError('runtime special file')",
  " visit(root,''); entries.sort(key=lambda entry:entry['path'])",
  " payload=json.dumps({'version':1,'entries':entries},ensure_ascii=False,separators=(',',':')).encode()",
  " return hashlib.sha256(payload).hexdigest()",
  "def commitment(contract):",
  " payload=[contract['version'],contract['launcher']['path'],contract['launcher']['sha256'],contract['node']['path'],contract['node']['sha256'],contract['node']['device'],contract['node']['inode'],contract['node']['links'],contract['node']['mode'],json.dumps(contract['node'].get('dependencies',[]),ensure_ascii=False,separators=(',',':'),sort_keys=True),contract['git']['path'],contract['git']['sha256'],contract['git']['device'],contract['git']['inode'],contract['git']['links'],contract['git']['mode'],json.dumps(contract['git'].get('dependencies',[]),ensure_ascii=False,separators=(',',':'),sort_keys=True),contract['python']['path'],contract['python']['resolved_path'],contract['python']['sha256'],contract['python']['device'],contract['python']['inode'],contract['python']['links'],contract['python']['mode'],json.dumps(contract['python'].get('dependencies',[]),ensure_ascii=False,separators=(',',':'),sort_keys=True),contract['runtime']['path'],contract['runtime']['integrity'],contract['python_runtime']['path'],contract['python_runtime']['integrity']]",
  " return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()",
  "def explicit_roots(arguments):",
  " roots=[]",
  " for index,value in enumerate(arguments):",
  "  option=value.split('=',1)[0]",
  "  if option!='--root' and '--root'.startswith(option): raise ValueError('abbreviated recovery root')",
  "  if value=='--root':",
  "   if index+1>=len(arguments): raise ValueError('recovery root is missing')",
  "   roots.append(arguments[index+1])",
  "  elif value.startswith('--root='): roots.append(value[len('--root='):])",
  " return roots",
  "def freeze_tree(root,frozen):",
  " if not hasattr(os,'chflags'): return",
  " immutable=getattr(stat,'UF_IMMUTABLE',2)",
  " paths=[]",
  " for current,directories,files in os.walk(root,topdown=False,followlinks=False):",
  "  paths.extend(os.path.join(current,name) for name in files)",
  "  paths.extend(os.path.join(current,name) for name in directories)",
  " paths.append(root)",
  " for target in paths:",
  "  metadata=os.lstat(target)",
  "  if stat.S_ISLNK(metadata.st_mode): raise OSError('staging symlink')",
  "  os.chflags(target,metadata.st_flags|immutable,follow_symlinks=False); frozen.append(target)",
  "try:",
  " recovery=bool(command) and (command[0] in {'install','sync','status','continue','abort'} or command[:2]==['run','install'] or command[:2]==['worktree','repin'])",
  " if not recovery: raise ValueError('invalid recovery command')",
  " if set(authority)!=set(['version','runtime_integrity','python_runtime_integrity']) or authority['version']!=1: raise ValueError('invalid launcher authority')",
  " root=os.path.dirname(os.path.dirname(os.path.dirname(launcher))); kit_path=os.path.join(root,'.agent-flow','kit.json')",
  " for requested_root in explicit_roots(command):",
  "  if os.path.realpath(os.path.join(root,requested_root))!=root: raise ValueError('recovery root mismatch')",
  " with open(kit_path,encoding='utf-8') as stream: kit=json.load(stream)",
  " contract=kit['project_runtime_contract']",
  " if contract['version']!=3 or kit['project_runtime_contract_commitment_version']!=1 or kit['project_runtime_contract_commitment']!=commitment(contract): raise ValueError('invalid runtime commitment')",
  " if contract['runtime']['integrity']!=authority['runtime_integrity'] or contract['python_runtime']['integrity']!=authority['python_runtime_integrity']: raise ValueError('runtime authority mismatch')",
  " expected_launcher=os.path.join(root,contract['launcher']['path']); expected_runtime_root=os.path.join(root,contract['runtime']['path']); expected_python_root=os.path.join(root,contract['python_runtime']['path'])",
  " launcher_fd=os.open(expected_launcher,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)); launcher_metadata=os.fstat(launcher_fd)",
  " if launcher!=expected_launcher or not stat.S_ISREG(launcher_metadata.st_mode) or digest_fd(launcher_fd)!=contract['launcher']['sha256']: raise OSError('launcher drift')",
  " if contract['launcher']['path']!='.agent-flow/bin/agent-flow' or contract['runtime']['path']!='.agent-flow/runtime/node' or contract['python_runtime']['path']!='.agent-flow/runtime/python': raise ValueError('invalid runtime paths')",
  " if runtime!=os.path.join(expected_runtime_root,'bin','agent-flow-kit.mjs') or tree_integrity(expected_runtime_root)!=authority['runtime_integrity'] or tree_integrity(expected_python_root)!=authority['python_runtime_integrity']: raise OSError('runtime drift')",
  " contracted=str(contract['node']['path']); contracted_exists=os.path.lexists(contracted)",
  " selected=contracted if contracted_exists else shutil.which('node')",
  " if selected is None: raise OSError('current Node is unavailable')",
  " selected=os.path.realpath(selected); source_fd=os.open(selected,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)); selected_metadata=os.fstat(source_fd)",
  " if not os.path.isabs(selected) or not stat.S_ISREG(selected_metadata.st_mode) or selected_metadata.st_uid not in {0,os.geteuid()} or selected_metadata.st_mode&0o022 or not selected_metadata.st_mode&0o111: raise OSError('current Node is unsafe')",
  " if contracted_exists:",
  "  expected=contract['node']",
  "  if selected!=contracted or digest_fd(source_fd)!=expected['sha256'] or selected_metadata.st_dev!=int(expected['device']) or selected_metadata.st_ino!=int(expected['inode']) or stat.S_IMODE(selected_metadata.st_mode)!=expected['mode'] or selected_metadata.st_nlink<int(expected['links']): raise OSError('contracted Node identity changed')",
  " elif selected_metadata.st_nlink!=1: raise OSError('current Node link count is unsafe')",
  " staging_parent=os.path.join(root,'.agent-flow','bootstrap-staging'); os.makedirs(staging_parent,mode=0o700,exist_ok=True); staging_parent_metadata=os.lstat(staging_parent)",
  " if stat.S_ISLNK(staging_parent_metadata.st_mode) or not stat.S_ISDIR(staging_parent_metadata.st_mode) or staging_parent_metadata.st_uid!=os.geteuid() or staging_parent_metadata.st_mode&0o077: raise OSError('bootstrap staging root is unsafe')",
  " stage=tempfile.mkdtemp(prefix='portable-',dir=staging_parent); stage_runtime=os.path.join(stage,'runtime'); stage_node=os.path.join(stage,'node'); frozen=[]",
  " try:",
  "  shutil.copytree(expected_runtime_root,stage_runtime,symlinks=True)",
  "  if tree_integrity(stage_runtime)!=authority['runtime_integrity']: raise OSError('staged runtime drift')",
  "  stage_fd=os.open(stage_node,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o700)",
  "  try:",
  "   while True:",
  "    chunk=os.read(source_fd,1024*1024)",
  "    if not chunk: break",
  "    view=memoryview(chunk)",
  "    while view:",
  "     written=os.write(stage_fd,view)",
  "     if written<=0: raise OSError('staged Node write failed')",
  "     view=view[written:]",
  "   os.fsync(stage_fd)",
  "  finally: os.close(stage_fd)",
  "  source_digest=digest_fd(source_fd)",
  "  if digest(stage_node)!=source_digest: raise OSError('staged Node drift')",
  "  freeze_tree(stage_runtime,frozen); freeze_tree(stage_node,frozen)",
  "  if digest(stage_node)!=source_digest or tree_integrity(stage_runtime)!=authority['runtime_integrity']: raise OSError('frozen runtime drift')",
  "  os.set_inheritable(launcher_fd,True)",
  "  original={'path':selected,'sha256':source_digest,'device':str(selected_metadata.st_dev),'inode':str(selected_metadata.st_ino),'links':str(selected_metadata.st_nlink),'mode':stat.S_IMODE(selected_metadata.st_mode),'portable_selected':True}",
  "  env=dict(os.environ); env['AGENT_FLOW_ORIGINAL_NODE_AUTHORITY']=base64.b64encode(json.dumps(original,separators=(',',':')).encode()).decode(); env.pop('AGENT_FLOW_AUTH_EXEC_ROOT',None); env['AGENT_FLOW_PROJECT_LAUNCHER']=launcher; env['AGENT_FLOW_PORTABLE_BOOTSTRAP']='1'; env['AGENT_FLOW_PORTABLE_BOOTSTRAP_FD']=str(launcher_fd); env['AGENT_FLOW_PORTABLE_AUTHORITY']=sys.argv[3]",
  "  completed=subprocess.run([stage_node,os.path.join(stage_runtime,'bin','agent-flow-kit.mjs'),*command],env=env,pass_fds=(launcher_fd,),check=False)",
  " finally:",
  "  if hasattr(os,'chflags'):",
  "   immutable=getattr(stat,'UF_IMMUTABLE',2)",
  "   for target in reversed(frozen):",
  "    try: os.chflags(target,os.lstat(target).st_flags&~immutable,follow_symlinks=False)",
  "    except FileNotFoundError: pass",
  "  shutil.rmtree(stage,ignore_errors=False)",
  " os.close(source_fd); os.close(launcher_fd); raise SystemExit(completed.returncode)",
  "except (KeyError,OSError,TypeError,UnicodeError,ValueError,json.JSONDecodeError):",
  " print('agent-flow: blocked because portable runtime bootstrap authentication failed',file=sys.stderr)",
  " raise SystemExit(126)",
].join("\n");


const AUTHENTICATED_EXEC_VERIFIER = [
  "import base64,hashlib,json,os,stat,subprocess,sys,time",
  "expected=json.loads(base64.b64decode(sys.argv[1],validate=True))",
  "target=expected.get('resolved_path') or expected['path']; staging_root=expected['staging_root']",
  "expected_dependencies={entry['name']:entry for entry in expected.get('dependencies',[])}",
  "if len(expected_dependencies)!=len(expected.get('dependencies',[])): raise OSError('dependency name collision')",
  "source_fd=stage_fd=staging_fd=stage_dir_fd=bin_fd=lib_fd=frameworks_fd=None; stage_dir=stage_path=None; created_files=[]; created_directories=[]; parent_fds=[]; frozen_paths=[]; dependency_records=[]",
  "def digest_fd(descriptor):",
  " os.lseek(descriptor,0,os.SEEK_SET); digest=hashlib.sha256()",
  " while True:",
  "  chunk=os.read(descriptor,1024*1024)",
  "  if not chunk: break",
  "  digest.update(chunk)",
  " os.lseek(descriptor,0,os.SEEK_SET); return digest.hexdigest()",
  "def same_object(left,right): return (left.st_dev,left.st_ino,left.st_mode,left.st_nlink,left.st_size)==(right.st_dev,right.st_ino,right.st_mode,right.st_nlink,right.st_size)",
  "def secure_staging_descriptor():",
  " project=expected['project_root']",
  " if not os.path.isabs(project) or os.path.realpath(project)!=project: raise OSError('invalid project root')",
  " if os.path.normpath(staging_root)!=os.path.join(project,'.agent-flow','exec-staging'): raise OSError('invalid staging root')",
  " project_fd=os.open(project,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)); parent_fd=project_fd",
  " opened_fds=[project_fd]",
  " project_metadata=os.fstat(project_fd); named_project=os.lstat(project)",
  " if (project_metadata.st_dev,project_metadata.st_ino)!=(named_project.st_dev,named_project.st_ino) or not stat.S_ISDIR(project_metadata.st_mode): raise OSError('project root changed')",
  " for component in ('.agent-flow','exec-staging'):",
  "  try: next_fd=os.open(component,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=parent_fd)",
  "  except FileNotFoundError:",
  "   os.mkdir(component,0o700,dir_fd=parent_fd); next_fd=os.open(component,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=parent_fd)",
  "  metadata=os.fstat(next_fd)",
  "  if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid() or metadata.st_mode&0o022: os.close(next_fd); raise OSError('unsafe staging root')",
  "  opened_fds.append(next_fd)",
  "  if parent_fd!=project_fd: os.close(parent_fd)",
  "  parent_fd=next_fd",
  " for descriptor in opened_fds[:-1]:",
  "  if descriptor!=parent_fd and descriptor!=project_fd:",
  "   try: os.close(descriptor)",
  "   except OSError: pass",
  " if project_fd!=parent_fd: os.close(project_fd)",
  " return parent_fd",
  "def copy_descriptor(source,parent_descriptor,name,mode):",
  " destination=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),mode,dir_fd=parent_descriptor)",
  " try:",
  "  os.fchmod(destination,mode); os.lseek(source,0,os.SEEK_SET)",
  "  while True:",
  "   chunk=os.read(source,1024*1024)",
  "   if not chunk: break",
  "   view=memoryview(chunk)",
  "   while view: view=view[os.write(destination,view):]",
  "  os.fsync(destination)",
  " finally: os.close(destination)",
  " return os.open(name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=parent_descriptor)",
  "def copy_dependency(source,parent_descriptor,name,dependency_expected):",
  " load_paths=dependency_expected.get('load_paths',[dependency_expected.get('path')])",
  " if not isinstance(load_paths,list) or not load_paths or any(not os.path.isabs(item) or os.path.realpath(item)!=dependency_expected.get('path') for item in load_paths): raise OSError('dependency load path mismatch')",
  " dependency_source=os.open(os.path.realpath(source),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))",
  " try:",
  "  before=os.fstat(dependency_source)",
  "  if not stat.S_ISREG(before.st_mode) or before.st_mode&0o022: raise OSError('unsafe dependency')",
  "  actual={'sha256':digest_fd(dependency_source),'device':str(before.st_dev),'inode':str(before.st_ino),'links':str(before.st_nlink),'mode':stat.S_IMODE(before.st_mode)}",
  "  if os.path.realpath(source)!=dependency_expected.get('path') or any(actual[key]!=dependency_expected[key] for key in actual): raise OSError('dependency identity mismatch')",
  "  digest=digest_fd(dependency_source); copied=copy_descriptor(dependency_source,parent_descriptor,name,stat.S_IMODE(before.st_mode)); after=os.fstat(dependency_source)",
  "  if not same_object(before,after) or digest_fd(dependency_source)!=digest or digest_fd(copied)!=digest: os.close(copied); raise OSError('dependency changed')",
  "  return copied",
  " finally: os.close(dependency_source)",
  "def nested_parent(root_descriptor,root_path,components):",
  " if any(not item or item in ('.','..') or '/' in item or '\\\\' in item for item in components): raise OSError('invalid dependency stage path')",
  " parent=os.dup(root_descriptor); parent_fds.append(parent); current_path=root_path",
  " for component in components:",
  "  current_path=os.path.join(current_path,component)",
  "  try: os.mkdir(component,0o700,dir_fd=parent); created_directories.append(current_path)",
  "  except FileExistsError: pass",
  "  child=os.open(component,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=parent)",
  "  metadata=os.fstat(child)",
  "  if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid() or metadata.st_mode&0o077: os.close(child); raise OSError('unsafe dependency stage directory')",
  "  parent=child; parent_fds.append(parent)",
  " return parent,current_path",
  "def freeze(path):",
  " flags=getattr(stat,'UF_IMMUTABLE',0); chflags=getattr(os,'chflags',None)",
  " if sys.platform=='darwin':",
  "  if not flags or chflags is None: raise OSError('immutable staging is unavailable')",
  "  chflags(path,flags)",
  "  metadata=os.lstat(path); frozen_paths.append((path,metadata.st_dev,metadata.st_ino))",
  "try:",
  " hold_name='AGENT_FLOW_TEST_HOLD_BEFORE_AUTHENTICATED_PYTHON_OPEN_MS' if expected.get('resolved_path') else 'AGENT_FLOW_TEST_HOLD_BEFORE_AUTHENTICATED_EXEC_OPEN_MS'",
  " hold=int(os.environ.get(hold_name,'0'))",
  " if 0<hold<=10000:",
  "  marker='authenticated-python-ready' if expected.get('resolved_path') else 'authenticated-exec-ready'",
  "  print('agent-flow:test-'+marker,file=sys.stderr,flush=True); time.sleep(hold/1000)",
  " source_fd=os.open(target,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))",
  " before=os.fstat(source_fd)",
  " if not stat.S_ISREG(before.st_mode): raise OSError('not regular')",
  " source_digest=digest_fd(source_fd); after=os.fstat(source_fd)",
  " actual={'sha256':source_digest,'device':str(before.st_dev),'inode':str(before.st_ino),'links':str(before.st_nlink),'mode':stat.S_IMODE(before.st_mode)}",
  " if not same_object(before,after) or any(actual[key]!=expected[key] for key in actual): raise OSError('identity mismatch')",
  " staging_fd=secure_staging_descriptor(); stage_name=os.urandom(24).hex(); os.mkdir(stage_name,0o700,dir_fd=staging_fd)",
  " stage_dir=os.path.join(staging_root,stage_name); stage_dir_fd=os.open(stage_name,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=staging_fd)",
  " os.mkdir('bin',0o700,dir_fd=stage_dir_fd); created_directories.append(os.path.join(stage_dir,'bin')); bin_fd=os.open('bin',os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=stage_dir_fd)",
  " stage_fd=copy_descriptor(source_fd,bin_fd,'executable',expected['mode']); stage_metadata=os.fstat(stage_fd); created_files.append((bin_fd,'executable',stage_metadata.st_dev,stage_metadata.st_ino))",
  " if not stat.S_ISREG(stage_metadata.st_mode) or stage_metadata.st_nlink!=1 or stat.S_IMODE(stage_metadata.st_mode)!=expected['mode'] or digest_fd(stage_fd)!=expected['sha256']: raise OSError('staged identity mismatch')",
  " if not same_object(before,os.fstat(source_fd)) or digest_fd(source_fd)!=source_digest: raise OSError('source changed while staging')",
  " names=sorted(expected_dependencies)",
  " library_names=[name for name in names if expected_dependencies[name].get('stage_kind','library')=='library']",
  " framework_names=[name for name in names if expected_dependencies[name].get('stage_kind')=='framework']",
  " if len(library_names)+len(framework_names)!=len(names): raise OSError('invalid dependency stage kind')",
  " if library_names:",
  "  os.mkdir('lib',0o700,dir_fd=stage_dir_fd); created_directories.append(os.path.join(stage_dir,'lib')); lib_fd=os.open('lib',os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=stage_dir_fd)",
  " if framework_names:",
  "  os.mkdir('frameworks',0o700,dir_fd=stage_dir_fd); created_directories.append(os.path.join(stage_dir,'frameworks')); frameworks_fd=os.open('frameworks',os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=stage_dir_fd)",
  " for name in names:",
  "  dependency_expected=expected_dependencies[name]; stage_kind=dependency_expected.get('stage_kind','library'); stage_relative=dependency_expected.get('stage_relative',name)",
  "  if stage_kind=='framework':",
  "   components=stage_relative.split('/')",
  "   if not components[0].endswith('.framework') or components[-1]!=name or not (len(components)==2 or (len(components)>=4 and components[1]=='Versions')): raise OSError('invalid framework dependency path')",
  "   dependency_parent,parent_path=nested_parent(frameworks_fd,os.path.join(stage_dir,'frameworks'),components[:-1]); dependency_path=os.path.join(parent_path,components[-1])",
  "  else:",
  "   if stage_relative!=name: raise OSError('invalid library dependency path')",
  "   dependency_parent=lib_fd; dependency_path=os.path.join(stage_dir,'lib',name)",
  "  dependency_fd=copy_dependency(dependency_expected['path'],dependency_parent,components[-1] if stage_kind=='framework' else name,dependency_expected); dependency_metadata=os.fstat(dependency_fd); dependency_records.append((dependency_path,dependency_fd,dependency_metadata.st_dev,dependency_metadata.st_ino,digest_fd(dependency_fd))); created_files.append((dependency_parent,components[-1] if stage_kind=='framework' else name,dependency_metadata.st_dev,dependency_metadata.st_ino))",
  " stage_path=os.path.join(stage_dir,'bin','executable')",
  " hold=int(os.environ.get('AGENT_FLOW_TEST_HOLD_AFTER_AUTHENTICATED_STAGE_MS','0'))",
  " if 0<hold<=10000: print('agent-flow:test-authenticated-stage-ready:'+stage_path,file=sys.stderr,flush=True); time.sleep(hold/1000)",
  " named_root=os.lstat(staging_root); named_dir=os.lstat(stage_dir); named_stage=os.lstat(stage_path)",
  " if (named_root.st_dev,named_root.st_ino)!=(os.fstat(staging_fd).st_dev,os.fstat(staging_fd).st_ino) or (named_dir.st_dev,named_dir.st_ino)!=(os.fstat(stage_dir_fd).st_dev,os.fstat(stage_dir_fd).st_ino) or (named_stage.st_dev,named_stage.st_ino)!=(stage_metadata.st_dev,stage_metadata.st_ino) or digest_fd(stage_fd)!=expected['sha256']: raise OSError('staging path changed')",
  " for dependency_path,dependency_fd,dependency_device,dependency_inode,dependency_digest in dependency_records:",
  "  dependency_named=os.lstat(dependency_path); dependency_current=os.fstat(dependency_fd)",
  "  if (dependency_named.st_dev,dependency_named.st_ino)!=(dependency_device,dependency_inode) or (dependency_current.st_dev,dependency_current.st_ino)!=(dependency_device,dependency_inode) or digest_fd(dependency_fd)!=dependency_digest: raise OSError('staging dependency changed')",
  "  freeze(dependency_path)",
  " freeze(stage_path)",
  " for directory in sorted(set(created_directories),key=lambda item:item.count(os.sep),reverse=True): freeze(directory)",
  " freeze(stage_dir)",
  " env=dict(os.environ); env.pop('AGENT_FLOW_TEST_HOLD_BEFORE_AUTHENTICATED_EXEC_OPEN_MS',None); env.pop('AGENT_FLOW_TEST_HOLD_BEFORE_AUTHENTICATED_PYTHON_OPEN_MS',None)",
  " env.pop('AGENT_FLOW_TEST_HOLD_AFTER_AUTHENTICATED_STAGE_MS',None)",
  " [env.pop(name,None) for name in tuple(env) if name.startswith(('DYLD_','LD_'))]",
  " if lib_fd is not None:",
  "  library_path=os.path.join(stage_dir,'lib'); env['DYLD_LIBRARY_PATH']=library_path if sys.platform=='darwin' else env.get('DYLD_LIBRARY_PATH',''); env['LD_LIBRARY_PATH']=library_path if sys.platform!='darwin' else env.get('LD_LIBRARY_PATH','')",
  " if frameworks_fd is not None and sys.platform=='darwin': env['DYLD_FRAMEWORK_PATH']=os.path.join(stage_dir,'frameworks')",
  " if expected.get('python_home'): env['PYTHONHOME']=expected['python_home']",
  " if expected.get('python_site_packages'): env['PYTHONPATH']=os.pathsep.join([*expected['python_site_packages'],env.get('PYTHONPATH','')]).rstrip(os.pathsep)",
  " original=dict(expected); [original.pop(key,None) for key in ('staging_root','project_root','python_home','python_site_packages')]; env['AGENT_FLOW_ORIGINAL_NODE_AUTHORITY']=base64.b64encode(json.dumps(original,separators=(',',':')).encode()).decode()",
  " inherited=env.get('AGENT_FLOW_INSTALL_FLOCK_FD',''); pass_descriptors=(int(inherited),) if inherited.isdigit() else ()",
  " completed=subprocess.run([stage_path,*sys.argv[2:]],env=env,pass_fds=pass_descriptors,check=False)",
  " raise SystemExit(completed.returncode)",
  "except (KeyError,OSError,UnicodeError,ValueError,json.JSONDecodeError):",
  " print('agent-flow: blocked because project runtime executable identity changed',file=sys.stderr)",
  " raise SystemExit(126)",
  "finally:",
  " for path,device,inode in reversed(frozen_paths):",
  "  try:",
  "   metadata=os.lstat(path)",
  "   if (metadata.st_dev,metadata.st_ino)==(device,inode): os.chflags(path,0)",
  "  except OSError: pass",
  " for parent,name,device,inode in reversed(created_files):",
  "  try:",
  "   current=os.stat(name,dir_fd=parent,follow_symlinks=False)",
  "   if (current.st_dev,current.st_ino)==(device,inode): os.unlink(name,dir_fd=parent)",
  "  except OSError: pass",
  " for descriptor in (source_fd,stage_fd,bin_fd,lib_fd,frameworks_fd,*[record[1] for record in dependency_records],*parent_fds):",
  "  if descriptor is not None:",
  "   try: os.close(descriptor)",
  "   except OSError: pass",
  " for directory in sorted(set(created_directories),key=lambda item:item.count(os.sep),reverse=True):",
  "  try: os.rmdir(directory)",
  "  except OSError: pass",
  " if stage_dir_fd is not None:",
  "  try: os.close(stage_dir_fd)",
  "  except OSError: pass",
  " if staging_fd is not None and stage_dir is not None:",
  "  try: os.rmdir(os.path.basename(stage_dir),dir_fd=staging_fd)",
  "  except OSError: pass",
  " if staging_fd is not None:",
  "  try: os.close(staging_fd)",
  "  except OSError: pass",
].join("\n");

function executableAuthority(kind) {
  const root = installedRuntimeRootWithoutGit(process.cwd());
  const kit = root ? readJsonIfExists(path.join(root, ".agent-flow", "kit.json")) : null;
  const contract = kit?.project_runtime_contract;
  if (
    contract
    && contract.version === 3
    && kit.project_runtime_contract_commitment_version === 1
    && kit.project_runtime_contract_commitment === projectRuntimeContractCommitment(contract)
    && contract[kind]
  ) {
    return contract[kind];
  }
  if (kind === "git") return executableContract(projectGitPath());
  if (kind === "python") {
    return {
      path: projectPythonPath(),
      resolved_path: fs.realpathSync(projectPythonPath()),
      ...executableIdentity(projectPythonPath()),
      dependencies: executableDependencyContracts(projectPythonPath()),
    };
  }
  throw new Error(`unsupported executable authority: ${kind}`);
}

function installedRuntimeRootWithoutGit(start) {
  const managed = resolveManagedWorktreeRoot(path.resolve(start));
  if (managed && fs.existsSync(path.join(managed, ".agent-flow", "kit.json"))) return managed;
  let current = path.resolve(start);
  while (true) {
    if (fs.existsSync(path.join(current, ".agent-flow", "kit.json"))) return current;
    const parent = path.dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

function authenticatedExecutableArgs(expected, args) {
  const root = process.env.AGENT_FLOW_AUTH_EXEC_ROOT
    || installedRuntimeRootWithoutGit(process.cwd())
    || process.cwd();
  const authority = {
    ...expected,
    staging_root: path.join(root, ".agent-flow", "exec-staging"),
    project_root: fs.realpathSync(root),
    ...pythonStagingMetadata(expected),
  };
  return [
    "-I",
    "-B",
    "-c",
    AUTHENTICATED_EXEC_VERIFIER,
    Buffer.from(JSON.stringify(authority), "utf8").toString("base64"),
    ...args,
  ];
}

function safeSpawnAuthenticatedSync(expected, args, options = {}) {
  return safeSpawnSync("/usr/bin/python3", authenticatedExecutableArgs(expected, args), options);
}

function authenticatedExecutableShellCommand(expected, trailingArguments) {
  const root = process.env.AGENT_FLOW_AUTH_EXEC_ROOT
    || installedRuntimeRootWithoutGit(process.cwd())
    || process.cwd();
  const authority = {
    ...expected,
    staging_root: path.join(root, ".agent-flow", "exec-staging"),
    project_root: fs.realpathSync(root),
    ...pythonStagingMetadata(expected),
  };
  return [
    shellSingleQuote("/usr/bin/python3"),
    "-I",
    "-B",
    "-c",
    shellSingleQuote(AUTHENTICATED_EXEC_VERIFIER),
    shellSingleQuote(Buffer.from(JSON.stringify(authority), "utf8").toString("base64")),
    trailingArguments,
  ].join(" ");
}

function pythonStagingMetadata(expected) {
  if (!expected.resolved_path) return {};
  const descriptor = fs.openSync(expected.resolved_path, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const prefix = Buffer.alloc(2);
    fs.readSync(descriptor, prefix, 0, prefix.length, 0);
    if (prefix.toString("utf8") === "#!") return {};
  } finally {
    fs.closeSync(descriptor);
  }
  return {
    python_home: path.dirname(path.dirname(expected.resolved_path)),
    python_site_packages: pythonVenvSitePackages(expected.path),
  };
}

function pythonVenvSitePackages(configuredPath) {
  const venvRoot = path.dirname(path.dirname(configuredPath));
  if (!fs.existsSync(path.join(venvRoot, "pyvenv.cfg"))) return [];
  const libraryRoot = path.join(venvRoot, "lib");
  if (!fs.existsSync(libraryRoot)) return [];
  return fs.readdirSync(libraryRoot)
    .filter((name) => /^python[0-9.]+t?$/.test(name))
    .map((name) => path.join(libraryRoot, name, "site-packages"))
    .filter((candidate) => fs.existsSync(candidate));
}

function projectGuardVerifierSource(contract) {
  const source = fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", "guard-worktree-write.py"), "utf8");
  const commitment = projectRuntimeContractCommitment(contract);
  const rendered = source
    .replace("__AGENT_FLOW_PROJECT_RUNTIME_CONTRACT_SHA256__", commitment)
    .replace("__AGENT_FLOW_PYTHON_RUNTIME_INTEGRITY__", contract.python_runtime.integrity);
  if (rendered === source) {
    throw new Error("project guard verifier placeholders are missing");
  }
  return rendered;
}

function installProjectGuardVerifier(root, contract) {
  const guardPath = path.join(root, ".agent-flow", "scripts", "hooks", "guard-worktree-write.py");
  const rendered = projectGuardVerifierSource(contract);
  writeManagedFile(guardPath, rendered);
}

function installProjectUnlocked(root, context, lock) {
  AGENT_FLOW_COMMAND = shellSingleQuote(path.join(root, PROJECT_LAUNCHER_RELATIVE));
  const agentFlowDir = path.join(root, ".agent-flow");
  const profile = detectProfile(root);
  let installSelection = resolveInstallSelection({ args: installArgs, detectedProfile: profile, kitRoot: KIT_ROOT, projectRoot: root });
  const existingPayload = readExistingKit(agentFlowDir);
  const previousLauncherRoot = authenticatedInstalledLauncherRoot(root);
  const managedHookCommandRoots = previousLauncherRoot && !samePath(previousLauncherRoot, root)
    ? [root, previousLauncherRoot]
    : [root];
  const previousIndexRecord = readAuthenticatedSkillIndex(agentFlowDir, existingPayload);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_INDEX_AUTH_MS", "index-authenticated");
  const previousSkillIndex = previousIndexRecord?.payload || null;
  installSelection = mergeInstallSelectionWithPrevious(installSelection, previousSkillIndex, KIT_ROOT, root);
  const activeHost = detectActiveHost(process.env);
  const automaticSkillNames = discoverAutomaticExternalSkillNames({
    home: HOME,
    activeHost,
    env: process.env,
    previousIndex: previousSkillIndex,
  });
  installSelection = canonicalizeInstallSelectionCompatibility(
    installSelection,
    readBundledSkillCompatibility(KIT_ROOT),
    KIT_ROOT,
    root,
    {
      home: HOME,
      activeHost,
      env: process.env,
      previousIndex: previousSkillIndex,
      automaticSkillNames,
    },
  );
  const externallyResolvedExplicitSkillNames = (installSelection.explicitSkills || [])
    .filter((name) => !GENERATED_PROJECT_SKILL_NAMES.has(name));
  const sourceNames = new Set([
    ...[...automaticSkillNames].filter((name) => !GENERATED_PROJECT_SKILL_NAMES.has(name)),
    ...externallyResolvedExplicitSkillNames,
  ]);
  const skillSourcePlan = resolveProfileSkillSources({
    skillNames: sourceNames,
    kitRoot: KIT_ROOT,
    projectRoot: root,
    projectSkillsRoot: path.join(agentFlowDir, "skills"),
    home: HOME,
    activeHost,
    env: process.env,
    previousIndex: previousSkillIndex,
    automaticSkillNames,
    explicitSkillNames: externallyResolvedExplicitSkillNames,
  });
  for (const name of installSelection.explicitSkills || []) {
    if (skillSourcePlan.missing.includes(name)) throw new Error(`explicit skill not found: ${name}`);
  }
  installSelection = mergeResolvedSkillClosure(installSelection, skillSourcePlan);
  const plannedSkillEntries = new Set([
    ...GENERATED_PROJECT_SKILL_NAMES,
    ...BUNDLED_SKILL_ROOT_FILE_NAMES,
    ...(installSelection.copyRootNames || []),
    ...(installSelection.skillNames || []),
  ]);
  rejectReplacedManagedSkillEntries(
    agentFlowDir,
    previousSkillIndex,
    plannedSkillEntries,
  );
  context.transaction = beginSkillInstallTransaction(root, agentFlowDir, previousIndexRecord, lock.token);
  setSkillTransactionPlannedEntries(context.transaction, plannedSkillEntries);
  const phases = fullFeaturePhases();

  for (const name of ["runs", "state", "handoffs", "team", "worktrees", "skills"]) {
    fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
  }
  fs.mkdirSync(path.join(agentFlowDir, "local-skills"), { recursive: true });


  const payload = {
    install_scope: "project",
    profile,
    profiles: installSelection.profiles,
    profile_selection: installSelection.profileSelection || "auto",
    selected_skills: installSelection.skillNames ? [...installSelection.skillNames].sort() : "all",
    root: ".",
    installed_at: existingPayload?.installed_at || new Date().toISOString(),
  };

  writeManagedFile(path.join(agentFlowDir, "workflows", "full-feature.yaml"), fullFeatureWorkflowYaml());
  copyBundledDirIfMissingOrSame(
    path.join(KIT_ROOT, "workflows"),
    path.join(agentFlowDir, "workflows"),
    true,
    new Set(),
    true,
    true,
  );
  materializeGeneratedSkillEntry(
    context.transaction,
    "agent-flow",
    agentFlowSkillMarkdown(),
  );
  materializeGeneratedSkillEntry(
    context.transaction,
    "full-feature-workflow",
    fullFeatureSkillMarkdown(),
  );
  materializeGeneratedSkillEntry(
    context.transaction,
    "product-brief",
    productBriefSkillMarkdown(),
  );
  materializeGeneratedSkillEntry(
    context.transaction,
    "plan-reviewer",
    planReviewerSkillMarkdown(),
  );
  materializeGeneratedSkillEntry(
    context.transaction,
    "architecture-reviewer",
    architectureReviewerSkillMarkdown(),
  );
  materializeGeneratedSkillEntry(
    context.transaction,
    "push-watch",
    pushWatchSkillMarkdown(),
  );
  materializeBundledSkillEntries(
    context.transaction,
    installSelection.copyRootNames,
  );
  materializeResolvedSkillSources(agentFlowDir, skillSourcePlan, context.transaction);
  if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_SKILL_MATERIALIZATION === "1") {
    throw new Error("injected failure after skill materialization");
  }
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "profiles"), path.join(agentFlowDir, "profiles"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "templates"), path.join(agentFlowDir, "templates"), forceManaged, new Set(), true, forceManaged);
  const skillIndex = installProjectSkills(
    root,
    agentFlowDir,
    previousSkillIndex,
    forceManaged,
    installSelection,
    skillSourcePlan,
    context.transaction,
  );
  updateSkillTransactionPlannedStates(context.transaction, ["index.json"]);
  preserveUnmanagedSkillEntries(context.transaction, previousSkillIndex, skillIndex);
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILL_INDEX === "1") {
    sealManagedInstallMutations(context.transaction);
    process.exit(87);
  }
  if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_SKILL_INDEX === "1") {
    throw new Error("injected failure after skill index");
  }
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, "scripts"), path.join(agentFlowDir, "scripts"), forceManaged);
  removeStaleContextDocsScripts(agentFlowDir, forceManaged);
  installProjectPythonRuntime(root);
  installProjectNodeRuntime(root);
  const runtimeContract = projectRuntimeContract(root);
  installProjectGuardVerifier(root, runtimeContract);
  if (!samePath(root, KIT_ROOT)) {
    removeManagedDirIfSame(path.join(KIT_ROOT, "scripts"), path.join(root, "scripts"), forceManaged);
  }
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "agents"), path.join(root, ".Codex", "agents"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".claude", "agents"), path.join(root, ".claude", "agents"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".claude", "agents"), path.join(root, ".omp", "agents"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "rules", "context"), path.join(root, ".Codex", "rules", "context"), forceManaged);
  copyBundledDirIfMissingOrSame(path.join(KIT_ROOT, ".Codex", "context"), path.join(root, ".Codex", "context"), forceManaged);
  installCodexHooks(root, managedHookCommandRoots);
  writeManagedFileIfMissingOrSame(
    path.join(root, ".Codex", "rules", "codebase-rubric.md"),
    fs.readFileSync(path.join(KIT_ROOT, ".Codex", "rules", "codebase-rubric.md"), "utf8"),
    forceManaged,
  );
  writeManagedFileIfMissingOrSame(
    path.join(root, ".Codex", "rules", "concise-output.md"),
    fs.readFileSync(path.join(KIT_ROOT, "skills", "agent-flow-concise-output", "concise-output.md"), "utf8"),
    forceManaged,
  );
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch.md"), pushWatchPromptMarkdown());
  writeManagedFile(path.join(agentFlowDir, "prompts", "push-watch-tick.md"), pushWatchTickPromptMarkdown());
  for (const phase of phases) {
    writeManagedFile(
      path.join(agentFlowDir, "prompts", `${phase.id}.md`),
      phasePrompt(phase, root),
    );
  }
  writeManagedFile(path.join(agentFlowDir, "rules", "workflow-contract.md"), workflowContract());
  const agentFlowBlock = canonicalAgentFlowBlock();
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "agent-flow.md"), agentFlowBlock);
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "AGENTS.md"), bootstrapMarkdown("AGENTS.md", agentFlowBlock));
  writeManagedFile(path.join(agentFlowDir, "bootstrap", "CLAUDE.md"), bootstrapMarkdown("CLAUDE.md", agentFlowBlock));
  const gitignorePath = path.join(root, ".gitignore");
  upsertGitignore(gitignorePath, [
    ".agent-flow/",
    ".agent-flow/local-skills/",
    ".codex/",
    ".Codex/",
    ".claude/",
    ".omp/",
    "AGENTS.md",
    "CLAUDE.md",
    "AGENTS/",
    "CLAUDE/",
    "agent-flow/",
  ]);
  removeGitignoreEntries(gitignorePath, [
    "scripts/check-context-docs.*",
    "graphify/",
    "graphify-out/manifest.json",
    "graphify-out/cost.json",
  ]);
  syncProjectAgentDocuments(root, agentFlowBlock);
  makeHooksExecutable(root);
  installClaudeHooks(root, managedHookCommandRoots);
  installOmpHooks(root);

  payload.skill_index = {
    path: ".agent-flow/skills/index.json",
    skills: skillIndex.skills.length,
    conflicts: skillIndex.conflicts.length,
    warnings: skillIndex.warnings.length,
  };

  const indexBytes = readRegularFileSnapshotNoFollow(
    path.join(agentFlowDir, "skills", "index.json"),
    agentFlowDir,
    "installed skill index",
  ).bytes;
  payload.skill_index_hash_version = 1;
  payload.skill_index_hash = crypto.createHash("sha256").update(indexBytes).digest("hex");
  payload.skill_plan_hash_version = 2;
  payload.skill_plan_hash = computeSkillPlanHash(skillIndex, root, true);
  payload.skill_links_commitment_version = SKILL_LINKS_COMMITMENT_VERSION;
  payload.skill_links_commitment = skillLinksCommitment(payload.skill_plan_hash, skillIndex.links);
  payload.managed_host_files = managedHostFileManifest(root);
  payload.managed_host_files_commitment_version = MANAGED_HOST_FILES_COMMITMENT_VERSION;
  payload.managed_host_files_commitment = managedHostFilesCommitment(payload);
  payload.managed_hook_contract = managedHookContract(root, runtimeContract);
  payload.managed_hook_contract_commitment_version = MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION;
  payload.managed_hook_contract_commitment = managedHookContractCommitment(payload);
  payload.project_runtime_contract = runtimeContract;
  payload.project_runtime_contract_commitment_version = 1;
  payload.project_runtime_contract_commitment = projectRuntimeContractCommitment(runtimeContract);

  writeManagedFile(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_BEFORE_MANAGED_INSTALL_SEAL_MS", "managed-install-before-seal");
  sealManagedInstallMutations(context.transaction);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_INSTALL_SEAL_MS", "managed-install-sealed");
  if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_MANAGED_INSTALL === "1") {
    throw new Error("injected failure after managed install");
  }
  console.log(`agent-flow installed profile=${profile}`);
}

function runWorkflowCommand(args) {
  const subcommand = args[0];
  const root = resolveAgentFlowRoot(process.cwd());
  refreshSkillCatalogAtBoundary(root);
  if (subcommand === "start") {
    const task = optionValue(args, "--task");
    if (!task) {
      throw new Error("run start requires --task");
    }
    const workflow = optionValue(args, "--workflow") ?? "full-feature";
    const runId = optionValue(args, "--run-id") ?? newRunId();
    assertInstalled(root);
    const phases = workflowPhases(workflow);
    const runDir = path.join(root, ".agent-flow", "runs", workflow, runId);
    const runDirRel = path.join(".agent-flow", "runs", workflow, runId);
    if (fs.existsSync(runDir)) {
      throw new Error(`run already exists: ${runId}`);
    }
    const workspace = captureNodeWorkspaceIdentity(process.cwd(), root);
    const execution = currentExecutionIdentity();
    const skillPlan = currentNodeSkillPlan(root);
    const startedAt = new Date().toISOString();
    const state = {
      run_id: runId,
      workflow,
      task,
      phase_index: 0,
      phase: phases[0].id,
      status: "running",
      publication_status: "starting",
      run_dir: runDirRel,
      started_at: startedAt,
      phase_entered_at: startedAt,
      workspace_root: workspace.workspace_root,
      ...(workspace.identity ? { workspace: workspace.identity } : {}),
      ...(execution ? { execution } : {}),
      ...skillPlan,
    };
    const workspaceClaim = acquireNodeWorkspaceStartClaim(
      root,
      workspace.identity,
      runId,
      { runDir, execution },
    );
    try {
      if (workspaceClaim) state.start_claim_token = workspaceClaim.token;
      if (workspace.identity && !samePath(workspace.identity.workspace_root, root)) {
        state.workspace_generation = nextNodeWorkspaceGeneration(root, workspace.identity);
      }
      assertNodeExecutionStartAvailable(root, execution, workspace);
      fs.mkdirSync(path.join(runDir, "artifacts"), { recursive: true });
      fs.mkdirSync(path.join(runDir, "logs"), { recursive: true });
      writeOwnedJson(path.join(runDir, "manifest.json"), state);
      if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_NODE_START_MANIFEST === "1") {
        process.exit(86);
      }
      if (execution && workspace.identity) {
        bindNodeExecution(root, execution, state);
        holdInstallForTest(
          "AGENT_FLOW_TEST_HOLD_AFTER_NODE_START_BINDING_MS",
          "node-start-binding-published",
        );
      }
      writeCurrentRun(root, state);
      if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_NODE_START_CURRENT === "1") {
        process.exit(87);
      }
      if (process.env.AGENT_FLOW_TEST_FAIL_AFTER_NODE_START_CURRENT === "1") {
        throw new Error("injected failure after Node start current state");
      }
      const activeState = { ...state, publication_status: "active" };
      writeOwnedJson(path.join(runDir, "manifest.json"), activeState);
      writeCurrentRun(root, activeState);
    } catch (error) {
      if (workspaceClaim?.payload) recoverIncompleteNodeStart(root, workspaceClaim.payload);
      else recoverIncompleteNodeStart(root, {
        run_id: runId,
        run_dir: runDir,
        token: undefined,
        execution,
      });
      throw error;
    } finally {
      releaseNodeWorkspaceStartClaim(workspaceClaim);
    }
    printNext(state, root);
    return;
  }

  if (subcommand === "status") {
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    printStatus(state, root);
    return;
  }

  if (subcommand === "next") {
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    printNext(state, root);
    return;
  }

  if (subcommand === "push-watch") {
    assertInstalled(root);
    let current;
    try {
      current = readCurrentRun(root);
    } catch (error) {
      const invocationBranch = currentBranch(process.cwd());
      if (["main", "master", "develop"].includes(invocationBranch)) {
        throw new Error(`blocked: protected branch ${invocationBranch}`);
      }
      throw error;
    }
    const active = assertNodeRunBoundary(current, root);
    assertNodeSkillPlanPinned(active, root);
    const branch = currentBranch(active.workspace_root ?? process.cwd());
    if (["main", "master", "develop"].includes(branch)) {
      throw new Error(`blocked: protected branch ${branch}`);
    }
    const state = {
      status: "watching",
      branch,
      iterations: 0,
      updated_at: new Date().toISOString(),
    };
    writeJson(pushWatchStatePath(root), state);
    console.log(`push-watch watching branch=${branch}`);
    return;
  }

  if (subcommand === "push-watch-tick") {
    assertInstalled(root);
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    if (state.phase !== "pr-watch") {
      throw new Error(`blocked: push-watch-tick requires current phase pr-watch, got ${state.phase}`);
    }
    const runDir = resolveRunDir(root, state.run_dir);
    const pr = readPullRequestStatus(state.workspace_root ?? process.cwd());
    const watchStatus = pullRequestWatchStatus(pr);
    const artifact = path.join(runDir, "artifacts", "pr-watch.md");
    writeManagedFile(
      artifact,
      [`status: ${watchStatus}`, `pr: ${pr.url ?? "unknown"}`, `recorded_at: ${new Date().toISOString()}`, ""].join("\n"),
    );
    const previous = fs.existsSync(pushWatchStatePath(root))
      ? JSON.parse(fs.readFileSync(pushWatchStatePath(root), "utf8"))
      : {};
    writeJson(pushWatchStatePath(root), {
      ...previous,
      status: watchStatus,
      pr: pr.url ?? null,
      iterations: Number(previous.iterations ?? 0) + 1,
      updated_at: new Date().toISOString(),
    });
    console.log(`push-watch status=${watchStatus}`);
    return;
  }

  if (subcommand === "advance") {
    const state = assertNodeRunBoundary(readCurrentRun(root), root);
    assertNodeSkillPlanPinned(state, root);
    const runDir = resolveRunDir(root, state.run_dir);
    if (state.status === "complete" || state.phase === "complete") {
      finalizeCompletedNodeRun(root, state, runDir, { persistState: false });
      console.log(`workflow already complete: ${state.run_id}`);
      return;
    }
    const phases = workflowPhases(state.workflow);
    const phase = nodeContractPhase(root, state, phases[state.phase_index]);
    const artifact = path.join(runDir, phase.artifact);
    if (!fs.existsSync(artifact)) {
      throw new Error(`blocked: missing artifact ${artifact}`);
    }
    assertDeclaredArtifacts(state, phase, runDir);
    assertFreshArtifact(state, phase, artifact);
    assertCompletionMarkers(phase, artifact, root);
    const nextIndex = nextPhaseIndex(state, phases, phase, artifact);
    syncRouteArtifacts(runDir, phases, state.phase_index, nextIndex);
    const nextPhase = phases[nextIndex];
    const transitionedAt = new Date().toISOString();
    const fixLoopRounds = nextFixLoopRounds(state, phase, nextPhase);
    const nextState = {
      ...state,
      phase_index: nextIndex,
      phase: nextPhase?.id ?? "complete",
      status: nextPhase ? "running" : "complete",
      updated_at: transitionedAt,
      phase_entered_at: transitionedAt,
      fix_loop_rounds: fixLoopRounds,
      ...(!nextPhase ? { completed_at: transitionedAt } : {}),
    };
    if (nextPhase) {
      writeJson(path.join(runDir, "manifest.json"), nextState);
      writeCurrentRun(root, nextState);
      printNext(nextState, root);
    } else {
      finalizeCompletedNodeRun(root, nextState, runDir);
      console.log(`workflow complete: ${state.run_id}`);
    }
    return;
  }

  throw new Error("usage: agent-flow-kit run <install|start|status|next|advance|push-watch|push-watch-tick>");
}

function loadWorkflowDefinition(name) {
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    throw new Error(`unsafe workflow name: ${name}`);
  }
  const workflowPath = path.join(KIT_ROOT, "workflows", `${name}.yaml`);
  const text = fs.readFileSync(workflowPath, "utf8");
  const definition = exportWorkflowDefinition(name);
  return {
    id: definition.id,
    text,
    phases: normalizeExportedPhases(definition, name),
  };
}

function fullFeatureWorkflow() {
  if (cachedFullFeatureWorkflow === null) {
    cachedFullFeatureWorkflow = loadWorkflowDefinition("full-feature");
  }
  return cachedFullFeatureWorkflow;
}

function fullFeaturePhases() {
  return fullFeatureWorkflow().phases;
}

function workflowPhases(name) {
  return name === "full-feature" ? fullFeaturePhases() : loadWorkflowDefinition(name).phases;
}

function exportWorkflowDefinition(name) {
  const result = safeSpawnSync(preferredPython(), [
    "-m",
    "agent_flow.cli",
    "workflow",
    "export",
    "--workflow",
    name,
    "--format",
    "json",
  ], {
    cwd: KIT_ROOT,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONPATH: [path.join(KIT_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    timeout: 10_000,
  });
  if (result.error || result.status !== 0) {
    const detail = result.error?.message || result.stderr?.trim() || `exit ${result.status}`;
    throw new Error(`workflow export failed for ${name}: ${detail}`);
  }
  try {
    return JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`workflow export returned invalid JSON for ${name}: ${error.message}`);
  }
}

function normalizeExportedPhases(definition, name) {
  // Node wrapper는 phase schema를 재해석하지 않고 Python 정규화 결과만 검증한다.
  if (!definition || typeof definition !== "object") {
    throw new Error(`workflow export ${name}: expected object`);
  }
  if (typeof definition.id !== "string" || !definition.id) {
    throw new Error(`workflow export ${name}: missing id`);
  }
  if (!Array.isArray(definition.phases) || definition.phases.length === 0) {
    throw new Error(`workflow export ${name}: missing phases`);
  }
  return definition.phases.map((phase, index) => normalizeExportedPhase(phase, name, index));
}

function normalizeExportedPhase(phase, name, index) {
  if (!phase || typeof phase !== "object") {
    throw new Error(`workflow export ${name}: phase ${index} must be an object`);
  }
  const id = requireExportedString(phase.id, name, index, "id");
  const description = optionalExportedString(phase.description, name, index, "description", "");
  const prompt = phase.prompt === null
    ? null
    : optionalExportedString(phase.prompt, name, index, "prompt", "");
  const artifact = requireExportedString(phase.artifact, name, index, "artifact");
  const requiredMarkers = normalizeExportedStringList(phase.required_markers, name, index, "required_markers");
  const requiredSkills = normalizeExportedStringList(phase.required_skills, name, index, "required_skills");
  const requirements = normalizeExportedStringList(phase.requirements, name, index, "requirements");
  const artifacts = normalizeExportedStringList(phase.artifacts, name, index, "artifacts");
  if (artifacts.length === 0 || artifacts[0] !== artifact) {
    throw new Error(`workflow export ${name}: phase ${index} artifacts must begin with artifact`);
  }
  return {
    id,
    artifact,
    description,
    instruction: prompt || description,
    required_markers: requiredMarkers,
    required_skills: requiredSkills,
    requirements,
    artifacts,
    multi_review: normalizeExportedBoolean(phase.multi_review, name, index, "multi_review"),
    routes: normalizeExportedRoutes(phase.routes, name, index),
  };
}

function requireExportedString(value, name, index, field) {
  if (typeof value !== "string" || !value) {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a non-empty string`);
  }
  return value;
}

function optionalExportedString(value, name, index, field, fallback) {
  if (value === undefined) {
    return fallback;
  }
  if (typeof value !== "string") {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a string`);
  }
  return value;
}

function normalizeExportedBoolean(value, name, index, field) {
  if (value === undefined) {
    return false;
  }
  if (typeof value !== "boolean") {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a boolean`);
  }
  return value;
}

function normalizeExportedStringList(value, name, index, field) {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`workflow export ${name}: phase ${index} ${field} must be a string array`);
  }
  return value;
}

function normalizeExportedRoutes(value, name, index) {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`workflow export ${name}: phase ${index} routes must be an object`);
  }
  for (const [key, target] of Object.entries(value)) {
    if (typeof key !== "string" || typeof target !== "string") {
      throw new Error(`workflow export ${name}: phase ${index} routes must map strings to strings`);
    }
  }
  return value;
}

function detectProfile(rootDir) {
  // 설치 배너도 Python CLI·install.mjs와 같은 profile을 보여줘야 agent가 다른 guide를 고르지 않는다.
  if (fs.existsSync(path.join(rootDir, "next.config.js")) ||
      fs.existsSync(path.join(rootDir, "next.config.mjs")) ||
      fs.existsSync(path.join(rootDir, "next.config.ts"))) {
    return "nextjs";
  }
  if (
    fs.existsSync(path.join(rootDir, "Package.swift")) ||
    hasChildWithSuffix(rootDir, ".xcodeproj") ||
    hasChildWithSuffix(rootDir, ".xcworkspace")
  ) {
    return "ios";
  }
  if (fs.existsSync(path.join(rootDir, "pyproject.toml")) ||
      fs.existsSync(path.join(rootDir, "requirements.txt"))) {
    return "python";
  }
  const earlyPackagePath = path.join(rootDir, "package.json");
  if (fs.existsSync(earlyPackagePath)) {
    const packageText = fs.readFileSync(earlyPackagePath, "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
  }
  if (
    fs.existsSync(path.join(rootDir, "build.gradle")) ||
    fs.existsSync(path.join(rootDir, "settings.gradle")) ||
    fs.existsSync(path.join(rootDir, "build.gradle.kts")) ||
    fs.existsSync(path.join(rootDir, "settings.gradle.kts"))
  ) {
    return "android";
  }
  const packagePath = path.join(rootDir, "package.json");
  if (fs.existsSync(packagePath)) {
    const packageText = fs.readFileSync(packagePath, "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
    if (packageText.includes("\"next\"")) {
      return "nextjs";
    }
    // 일반 TypeScript 프로젝트는 node보다 좁은 profile을 써야 gate와 skill routing이 맞다.
    if (fs.existsSync(path.join(rootDir, "tsconfig.json"))) {
      return "typescript";
    }
    return "node";
  }
  // npm gate를 실행할 수 없는 tsconfig 단독 프로젝트는 generic으로 둔다.
  return "generic";
}

function hasChildWithSuffix(rootDir, suffix) {
  if (!fs.existsSync(rootDir)) {
    return false;
  }
  return fs.readdirSync(rootDir).some((name) => name.endsWith(suffix));
}

function resolveAgentFlowRoot(start) {
  const worktreeRoot = resolveManagedWorktreeRoot(start);
  if (worktreeRoot && fs.existsSync(path.join(worktreeRoot, ".agent-flow", "kit.json"))) {
    return worktreeRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot && fs.existsSync(path.join(gitCommonRoot, ".agent-flow", "kit.json"))) {
    return gitCommonRoot;
  }
  const parts = start.split(path.sep);
  const markerIndex = parts.lastIndexOf(".agent-flow");
  if (markerIndex !== -1) {
    const root = parts.slice(0, markerIndex).join(path.sep) || path.sep;
    if (fs.existsSync(path.join(root, ".agent-flow", "kit.json"))) {
      return root;
    }
  }
  let current = start;
  while (true) {
    if (fs.existsSync(path.join(current, ".agent-flow", "kit.json"))) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return start;
    }
    current = parent;
  }
}

function resolveInstallRoot(start) {
  const worktreeRoot = resolveManagedWorktreeRoot(start);
  if (worktreeRoot) {
    return worktreeRoot;
  }
  const gitCommonRoot = resolveGitCommonWorktreeRoot(start);
  if (gitCommonRoot) {
    return gitCommonRoot;
  }
  const parts = start.split(path.sep);
  const markerIndex = parts.lastIndexOf(".agent-flow");
  if (markerIndex !== -1) {
    return parts.slice(0, markerIndex).join(path.sep) || path.sep;
  }
  return start;
}

function resolveManagedWorktreeRoot(start) {
  const parts = start.split(path.sep);
  const markers = new Set([".agent-flow", ".codex", ".Codex", ".omp"]);
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    if (parts[index + 1] !== "worktrees") continue;
    if (!markers.has(parts[index])) continue;
    const root = parts.slice(0, index).join(path.sep) || path.sep;
    // 홈의 전역 Codex/OMP worktree는 프로젝트 내부 worktree가 아니다.
    if (HOME && samePath(root, HOME) && (parts[index] === ".codex" || parts[index] === ".Codex" || parts[index] === ".omp")) {
      continue;
    }
    return root;
  }
  return null;
}

function assertLeaderMutationSource(root, action) {
  const sourceLeaderRoot = resolveManagedWorktreeRoot(KIT_ROOT);
  if (sourceLeaderRoot && samePath(root, sourceLeaderRoot)) {
    throw new Error(
      `managed worktree source ${action} blocked; run ${action} from the leader checkout`,
    );
  }
}

function samePath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    // 심볼릭 링크가 섞인 임시 경로에서도 홈 비교는 보수적으로 처리한다.
    return path.resolve(left) === path.resolve(right);
  }
}

function resolveGitCommonWorktreeRoot(start) {
  const topLevel = gitOutput(start, ["rev-parse", "--show-toplevel"]);
  const commonDir = gitOutput(start, ["rev-parse", "--git-common-dir"]);
  if (!topLevel || !commonDir) {
    return null;
  }
  const resolvedCommonDir = path.resolve(topLevel, commonDir);
  if (path.basename(resolvedCommonDir) !== ".git") {
    return null;
  }
  return path.dirname(resolvedCommonDir);
}

function gitOutput(cwd, args) {
  const result = safeSpawnSync(projectGitPath(), args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.error || result.status !== 0) {
    return null;
  }
  const output = result.stdout.trim();
  return output || null;
}

function captureNodeWorkspaceIdentity(cwd, root) {
  const topLevel = gitOutput(cwd, ["rev-parse", "--show-toplevel"]);
  if (!topLevel) {
    return { workspace_root: path.resolve(root), identity: null };
  }
  const workspaceRoot = fs.realpathSync(topLevel);
  const commonDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  const gitDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-dir"]);
  const branch = gitOutput(workspaceRoot, ["branch", "--show-current"]);
  const head = gitOutput(workspaceRoot, ["rev-parse", "HEAD"]);
  if (!commonDir || !gitDir || !branch || !head) {
    throw new Error(`blocked: cannot capture pinned workspace identity: ${workspaceRoot}`);
  }
  const canonicalCommon = authenticatedDirectoryRoot(commonDir);
  if (path.basename(canonicalCommon) !== ".git" || !samePath(path.dirname(canonicalCommon), root)) {
    throw new Error(`blocked: pinned workspace belongs to a different repository: ${workspaceRoot}`);
  }
  if (["main", "master", "develop"].includes(branch)) {
    throw new Error(`blocked: pinned workspace uses protected branch ${branch}`);
  }
  const metadata = fs.statSync(workspaceRoot);
  const identity = {
    workspace_root: workspaceRoot,
    git_common_dir: canonicalCommon,
    git_dir: authenticatedDirectoryRoot(gitDir),
    branch,
    head,
    device: metadata.dev,
    inode: metadata.ino,
  };
  if (!samePath(workspaceRoot, root) && !registeredNodeWorkspaceIdentity(root, workspaceRoot)) {
    registerNodeWorkspaceIdentity(root, identity);
  }
  validateNodeWorkspaceIdentity(identity, root);
  return { workspace_root: workspaceRoot, identity };
}

function validateNodeWorkspaceIdentity(identity, root, requireRegistration = true) {
  if (!identity || typeof identity !== "object") {
    throw new Error("blocked: pinned workspace identity is missing");
  }
  const configured = path.resolve(String(identity.workspace_root ?? ""));
  let workspaceRoot;
  try {
    workspaceRoot = fs.realpathSync(configured);
  } catch {
    throw new Error(`blocked: pinned workspace is missing: ${configured}`);
  }
  if (workspaceRoot !== String(identity.workspace_root)) {
    throw new Error(`blocked: pinned workspace canonical path changed: ${configured}`);
  }
  const metadata = fs.statSync(workspaceRoot);
  if (metadata.dev !== identity.device || metadata.ino !== identity.inode) {
    throw new Error(`blocked: pinned workspace filesystem identity changed: ${workspaceRoot}; from the leader checkout run: agent-flow worktree repin --name ${path.basename(workspaceRoot)}`);
  }
  const commonDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  const gitDir = gitOutput(workspaceRoot, ["rev-parse", "--path-format=absolute", "--git-dir"]);
  const branch = gitOutput(workspaceRoot, ["branch", "--show-current"]);
  const head = gitOutput(workspaceRoot, ["rev-parse", "HEAD"]);
  if (
    !commonDir
    || !gitDir
    || !head
    || !samePath(commonDir, identity.git_common_dir)
    || !samePath(gitDir, identity.git_dir)
  ) {
    throw new Error(`blocked: pinned workspace git identity changed: ${workspaceRoot}`);
  }
  if (branch !== identity.branch) {
    throw new Error(`blocked: pinned workspace branch changed: ${workspaceRoot}`);
  }
  const ancestor = safeSpawnSync(
    projectGitPath(),
    ["merge-base", "--is-ancestor", String(identity.head), head],
    { cwd: workspaceRoot, stdio: "ignore" },
  );
  if (ancestor.error || ancestor.status !== 0) {
    throw new Error(`blocked: pinned workspace HEAD diverged: ${workspaceRoot}`);
  }
  const canonicalCommon = authenticatedDirectoryRoot(commonDir);
  if (path.basename(canonicalCommon) !== ".git" || !samePath(path.dirname(canonicalCommon), root)) {
    throw new Error(`blocked: pinned workspace repository identity changed: ${workspaceRoot}`);
  }
  if (!samePath(workspaceRoot, root) && requireRegistration) {
    const registered = registeredNodeWorkspaceIdentity(root, workspaceRoot);
    if (!registered) {
      throw new Error(`blocked: pinned workspace is not registered: ${workspaceRoot}`);
    }
    validateNodeWorkspaceIdentity(registered, root, false);
  }
  return workspaceRoot;
}

function registeredNodeWorkspaceIdentity(root, workspaceRoot) {
  const registrations = nodeGitPrivateDirectory(root, ["agent-flow", "worktrees"]);
  if (!fs.existsSync(registrations)) return null;
  for (const entry of fs.readdirSync(registrations, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) {
      throw new Error("blocked: worktree registration is a symbolic link");
    }
    if (!entry.isDirectory()) continue;
    const registration = nodeGitPrivateDirectory(
      root,
      ["agent-flow", "worktrees", entry.name],
    );
    const manifestPath = path.join(registration, "manifest.json");
    if (!fs.existsSync(manifestPath)) continue;
    const manifest = readOwnedJson(manifestPath);
    const identity = manifest?.identity;
    if (identity?.workspace_root && samePath(identity.workspace_root, workspaceRoot)) {
      return identity;
    }
  }
  return null;
}

function registerNodeWorkspaceIdentity(root, identity) {
  const digest = crypto.createHash("sha256").update(identity.workspace_root).digest("hex").slice(0, 12);
  const name = `node-${digest}`;
  const runtime = nodeGitPrivateDirectory(
    root,
    ["agent-flow", "worktrees", name],
    true,
  );
  const manifestPath = path.join(runtime, "manifest.json");
  const existing = fs.existsSync(manifestPath) ? readOwnedJson(manifestPath) : null;
  if (existing?.identity?.workspace_root && !samePath(existing.identity.workspace_root, identity.workspace_root)) {
    throw new Error(`blocked: pinned workspace registration collision: ${identity.workspace_root}`);
  }
  writeOwnedJson(manifestPath, {
    name,
    branch: identity.branch,
    path: identity.workspace_root,
    identity,
  });
}

function assertNodeRunBoundary(state, root) {
  const workspaceRoot = path.resolve(state.workspace_root ?? root);
  if (state.workspace) {
    const pinned = validateNodeWorkspaceIdentity(state.workspace, root);
    if (!samePath(pinned, workspaceRoot)) {
      throw new Error("blocked: run workspace_root differs from its pinned identity");
    }
    const invocation = gitOutput(process.cwd(), ["rev-parse", "--show-toplevel"])
      ?? path.resolve(process.cwd());
    if (!samePath(invocation, root) && !samePath(invocation, pinned)) {
      throw new Error(
        `blocked: active run ${state.run_id} is pinned to ${pinned}; current workspace is ${invocation}`,
      );
    }
    return state;
  }
  if (gitOutput(root, ["rev-parse", "--show-toplevel"])) {
    throw new Error("blocked: active run is missing its pinned workspace identity");
  }
  return state;
}

function managedHostSourceSpecs(root) {
  return [
    [".Codex/agents/code-reviewer.md", ".Codex/agents/code-reviewer.md", path.join(KIT_ROOT, ".Codex", "agents", "code-reviewer.md")],
    [".claude/agents/code-reviewer.md", ".claude/agents/code-reviewer.md", path.join(KIT_ROOT, ".claude", "agents", "code-reviewer.md")],
    [".omp/agents/code-reviewer.md", ".claude/agents/code-reviewer.md", path.join(KIT_ROOT, ".claude", "agents", "code-reviewer.md")],
    [".omp/extensions/agent-flow-hooks.ts", "generated:omp-hooks-extension", Buffer.from(ompHooksExtensionSource(root), "utf8")],
  ].map(([relative, source, sourceValue]) => ({
    relative,
    source,
    sourceBytes: Buffer.isBuffer(sourceValue) ? sourceValue : fs.readFileSync(sourceValue),
    destination: path.join(root, ...relative.split("/")),
  }));
}

function requireManagedRegularFile(root, relative) {
  let cursor = path.resolve(root);
  for (const [index, part] of relative.split("/").entries()) {
    cursor = path.join(cursor, part);
    const metadata = lstatIfExists(cursor);
    if (!metadata || metadata.isSymbolicLink()) {
      throw new Error(`blocked: managed host file is missing or unsafe: ${relative}`);
    }
    const final = index === relative.split("/").length - 1;
    if ((final && !metadata.isFile()) || (!final && !metadata.isDirectory())) {
      throw new Error(`blocked: managed host file has an invalid path: ${relative}`);
    }
    if (final && metadata.nlink !== 1) {
      throw new Error(`blocked: managed host file may not be hard-linked: ${relative}`);
    }
  }
  ensureChildPath(root, cursor);
  return fs.readFileSync(cursor);
}

function managedReviewerBody(content) {
  const text = content.toString("utf8");
  if (!text.startsWith("---\n")) return text;
  const end = text.indexOf("\n---\n", 4);
  return end === -1 ? text : text.slice(end + 5).replace(/^\n/, "");
}

function managedHostFileManifest(root) {
  const specs = managedHostSourceSpecs(root);
  const codex = specs.find((spec) => spec.relative.startsWith(".Codex/"));
  const claude = specs.find((spec) => spec.relative.startsWith(".claude/"));
  const omp = specs.find((spec) => spec.relative.startsWith(".omp/agents/"));
  if (
    !codex
    || !claude
    || !omp
    || managedReviewerBody(codex.sourceBytes) !== managedReviewerBody(claude.sourceBytes)
    || !omp.sourceBytes.equals(claude.sourceBytes)
  ) {
    throw new Error("blocked: Claude, Codex, and OMP managed reviewers are not equivalent");
  }
  const files = {};
  for (const spec of specs.sort((left, right) => compareCodePoints(left.relative, right.relative))) {
    const installed = requireManagedRegularFile(root, spec.relative);
    if (!installed.equals(spec.sourceBytes)) {
      throw new Error(`blocked: managed host file differs from authenticated source: ${spec.relative}`);
    }
    files[spec.relative] = { source: spec.source, sha256: sha256Bytes(installed) };
  }
  return { version: MANAGED_HOST_FILES_VERSION, files };
}

function normalizedManagedHostFiles(manifest) {
  if (
    !manifest
    || typeof manifest !== "object"
    || Array.isArray(manifest)
    || manifest.version !== MANAGED_HOST_FILES_VERSION
    || !manifest.files
    || typeof manifest.files !== "object"
    || Array.isArray(manifest.files)
  ) throw new Error("blocked: installed managed host file provenance is invalid");
  const rows = [];
  for (const relative of Object.keys(manifest.files).sort(compareCodePoints)) {
    const entry = manifest.files[relative];
    if (
      !REQUIRED_MANAGED_HOST_FILES.includes(relative)
      || !entry
      || typeof entry.source !== "string"
      || !entry.source.trim()
      || typeof entry.sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(entry.sha256)
    ) throw new Error(`blocked: installed managed host file provenance is invalid: ${relative}`);
    rows.push([relative, entry.source, entry.sha256]);
  }
  for (const relative of REQUIRED_MANAGED_HOST_FILES) {
    if (!manifest.files[relative]) {
      throw new Error(`blocked: installed managed host file provenance is missing: ${relative}`);
    }
  }
  return rows;
}

function managedHostFilesCommitment(payload) {
  const body = {
    version: MANAGED_HOST_FILES_COMMITMENT_VERSION,
    skill_plan_hash: payload.skill_plan_hash,
    files: normalizedManagedHostFiles(payload.managed_host_files),
  };
  return sha256Bytes(Buffer.from(JSON.stringify(body), "utf8"));
}

function expectedManagedHookProjection() {
  return [
    ...CANONICAL_HOOK_POLICY.bashPre.map((script) => ["PreToolUse", "Bash", "command", script]),
    ...CANONICAL_HOOK_POLICY.writePre.map((script) => ["PreToolUse", WRITE_TOOL_MATCHER, "command", script]),
    ...CANONICAL_HOOK_POLICY.writePost.map((script) => ["PostToolUse", WRITE_TOOL_MATCHER, "command", script]),
    ...CANONICAL_HOOK_POLICY.stop.map((script) => ["Stop", "", "command", script]),
  ].sort(compareHookProjectionRows);
}

function compareHookProjectionRows(left, right) {
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const compared = compareCodePoints(left[index], right[index]);
    if (compared !== 0) return compared;
  }
  return left.length - right.length;
}

function managedHookProjection(
  root,
  settings,
  label,
  expectedScriptHashes,
  commandRoots = [root],
) {
  if (!settings?.hooks || typeof settings.hooks !== "object" || Array.isArray(settings.hooks)) {
    throw new Error(`blocked: managed hook settings are missing: ${label}`);
  }
  const rows = [];
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) throw new Error(`blocked: invalid managed hook settings: ${label}`);
    for (const entry of entries) {
      if (!entry || !Array.isArray(entry.hooks)) throw new Error(`blocked: invalid managed hook settings: ${label}`);
      const matcher = typeof entry.matcher === "string" ? entry.matcher : "";
      for (const hook of entry.hooks) {
        const scriptName = trustedManagedHookScriptName(
          root,
          hook?.command,
          expectedScriptHashes,
          commandRoots,
        );
        if (scriptName) rows.push([event, matcher, hook.type ?? "", scriptName]);
        else if (managedHookScriptName(hook?.command)) {
          throw new Error(`blocked: managed hook command is not immutable: ${label}`);
        }
      }
    }
  }
  return rows.sort(compareHookProjectionRows);
}

function authenticatedInstalledLauncherRoot(root) {
  if (!projectRuntimeContentMatchesContract(root)) return null;
  const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  const assignments = fs.readFileSync(launcher, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.startsWith("AGENT_FLOW_PROJECT_LAUNCHER="));
  if (assignments.length !== 1) return null;
  const launcherPath = parseCanonicalShellSingleQuote(
    assignments[0].slice("AGENT_FLOW_PROJECT_LAUNCHER=".length),
  );
  if (!launcherPath || !path.isAbsolute(launcherPath)) return null;
  const installedRoot = path.dirname(path.dirname(path.dirname(launcherPath)));
  if (
    path.resolve(path.join(installedRoot, PROJECT_LAUNCHER_RELATIVE))
    !== path.resolve(launcherPath)
  ) return null;
  return installedRoot;
}


function managedHookContract(root, runtimeContract = null) {
  const scripts = {};
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    const content = requireManagedRegularFile(root, relative);
    const source = scriptName === "guard-worktree-write.py"
      ? Buffer.from(projectGuardVerifierSource(
        runtimeContract ?? readExistingKit(path.join(root, ".agent-flow"))?.project_runtime_contract,
      ), "utf8")
      : fs.readFileSync(path.join(KIT_ROOT, "scripts", "hooks", scriptName));
    if (!content.equals(source)) throw new Error(`blocked: managed hook script differs from authenticated source: ${relative}`);
    if (process.platform !== "win32" && !(fs.statSync(path.join(root, ...relative.split("/"))).mode & 0o111)) {
      throw new Error(`blocked: managed hook script is not executable: ${relative}`);
    }
    scripts[relative] = { sha256: sha256Bytes(content), mode: "executable" };
  }
  const scriptHashes = new Map(Object.entries(scripts).map(([relative, entry]) => [relative, entry.sha256]));
  const installedRoot = authenticatedInstalledLauncherRoot(root);
  const commandRoots = installedRoot && !samePath(installedRoot, root)
    ? [root, installedRoot]
    : [root];
  const expected = expectedManagedHookProjection();
  const configs = {};
  for (const relative of MANAGED_HOOK_CONFIG_PATHS) {
    let settings;
    try {
      settings = JSON.parse(requireManagedRegularFile(root, relative).toString("utf8"));
    } catch (error) {
      throw new Error(`blocked: managed hook settings are unreadable: ${relative}: ${error.message}`);
    }
    const projection = managedHookProjection(
      root,
      settings,
      relative,
      scriptHashes,
      commandRoots,
    );
    if (JSON.stringify(projection) !== JSON.stringify(expected)) {
      throw new Error(
        `blocked: managed hook settings do not match required contract: ${relative}; `
        + `actual=${JSON.stringify(projection)} expected=${JSON.stringify(expected)}`,
      );
    }
    configs[relative] = { sha256: sha256Bytes(Buffer.from(JSON.stringify(projection), "utf8")) };
  }
  return { version: MANAGED_HOOK_CONTRACT_VERSION, configs, scripts };
}

function normalizedManagedHookContract(contract) {
  if (!contract || contract.version !== MANAGED_HOOK_CONTRACT_VERSION) {
    throw new Error("blocked: installed managed hook contract is invalid");
  }
  const normalize = (entries, expectedPaths, label, requiredMode = null) => {
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is invalid`);
    }
    const actual = Object.keys(entries).sort(compareCodePoints);
    const expected = [...expectedPaths].sort(compareCodePoints);
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error(`blocked: installed managed hook ${label} provenance is incomplete`);
    }
    return actual.map((relative) => {
      const entry = entries[relative];
      if (!entry || !/^[0-9a-f]{64}$/.test(entry.sha256) || (requiredMode && entry.mode !== requiredMode)) {
        throw new Error(`blocked: installed managed hook ${label} provenance is invalid: ${relative}`);
      }
      return requiredMode ? [relative, entry.sha256, requiredMode] : [relative, entry.sha256];
    });
  };
  return {
    configs: normalize(contract.configs, MANAGED_HOOK_CONFIG_PATHS, "config"),
    scripts: normalize(
      contract.scripts,
      MANAGED_HOOK_SCRIPT_NAMES.map((name) => `.agent-flow/scripts/hooks/${name}`),
      "script",
      "executable",
    ),
  };
}

function managedHookContractCommitment(payload) {
  const normalized = normalizedManagedHookContract(payload.managed_hook_contract);
  return sha256Bytes(Buffer.from(JSON.stringify({
    version: MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION,
    skill_plan_hash: payload.skill_plan_hash,
    configs: normalized.configs,
    scripts: normalized.scripts,
  }), "utf8"));
}

function assertManagedHostFilesInstalled(root, kit) {
  if (
    kit.managed_host_files_commitment_version !== MANAGED_HOST_FILES_COMMITMENT_VERSION
    || kit.managed_host_files_commitment !== managedHostFilesCommitment(kit)
  ) throw new Error("blocked: installed managed host file commitment is invalid");
  for (const [relative, _source, expectedHash] of normalizedManagedHostFiles(kit.managed_host_files)) {
    if (sha256Bytes(requireManagedRegularFile(root, relative)) !== expectedHash) {
      throw new Error(`blocked: installed managed host file changed: ${relative}`);
    }
  }
  if (
    kit.managed_hook_contract_commitment_version !== MANAGED_HOOK_CONTRACT_COMMITMENT_VERSION
    || kit.managed_hook_contract_commitment !== managedHookContractCommitment(kit)
  ) throw new Error("blocked: installed managed hook commitment is invalid");
  const live = managedHookContract(root);
  if (JSON.stringify(live) !== JSON.stringify(kit.managed_hook_contract)) {
    throw new Error("blocked: installed managed hook contract changed");
  }
}

function currentNodeSkillPlan(root) {
  const agentFlowDir = path.join(root, ".agent-flow");
  const kit = readExistingKit(agentFlowDir);
  const authenticated = readAuthenticatedSkillIndex(agentFlowDir, kit);
  if (
    !authenticated
    || kit?.skill_plan_hash_version !== 2
    || typeof kit.skill_plan_hash !== "string"
  ) {
    throw new Error("blocked: installed skill plan commitment is missing");
  }
  if (computeSkillPlanHash(authenticated.payload, root, true) !== kit.skill_plan_hash) {
    throw new Error("blocked: installed skill snapshot no longer matches kit commitment");
  }
  assertManagedHostFilesInstalled(root, kit);
  return {
    skill_plan_hash_version: 2,
    skill_plan_hash: kit.skill_plan_hash,
  };
}

function assertNodeSkillPlanPinned(state, root) {
  const current = currentNodeSkillPlan(root);
  if (
    state.skill_plan_hash_version !== current.skill_plan_hash_version
    || state.skill_plan_hash !== current.skill_plan_hash
  ) {
    const previousHash = state.skill_plan_hash ?? null;
    Object.assign(state, current, {
      skill_plan_repin_at: new Date().toISOString(),
      skill_plan_repin_from: previousHash,
    });
    writeJson(path.join(authenticatedNodeRunDir(root, state), "manifest.json"), state);
    writeCurrentRun(root, state);
  }
}

function safeSpawnSync(commandName, args, options = {}) {
  // 외부 CLI는 자동 relay를 멈추지 않도록 기본 timeout을 둔다.
  return spawnSync(commandName, args, {
    timeout: options.timeout ?? 30_000,
    ...options,
  });
}

function readCurrentRun(root) {
  const execution = currentExecutionIdentity();
  const scopedPath = execution ? executionRunStatePath(root, execution) : null;
  const legacyPath = currentRunPath(root);
  let pathName = legacyPath;
  if (execution) {
    if (scopedPath && fs.existsSync(scopedPath)) {
      pathName = scopedPath;
    } else if (fs.existsSync(legacyPath)) {
      const legacy = readOwnedJson(legacyPath);
      if (
        legacy.execution
        && executionIdentityDigest(legacy.execution) === executionIdentityDigest(execution)
      ) {
        writeOwnedJson(scopedPath, legacy);
        pathName = scopedPath;
      } else {
        throw new Error("no active run is bound to this execution");
      }
    } else {
      throw new Error("no active run is bound to this execution");
    }
  } else {
    const activeExecutions = activeNodeExecutionRunStates(root);
    if (activeExecutions.length > 0) {
      const reason = activeExecutions.length > 1
        ? "multiple active runs require an execution identity"
        : "active run requires an execution identity";
      throw new Error(`blocked: ${reason}`);
    }
  }
  if (!fs.existsSync(pathName)) {
    throw new Error('no active run. start one with: agent-flow run "<task>"');
  }
  const state = execution
    ? readOwnedJson(pathName)
    : readOwnedJson(pathName);
  authenticatedNodeRunDir(root, state);
  if (
    nodeRunStateIsActive(state)
    && state.publication_status !== undefined
    && !nodeStatePublicationIsActive(root, state)
  ) {
    throw new Error(`blocked: ${nodeStatePublicationError(root, state)}`);
  }
  if (
    execution
    && state.execution
    && executionIdentityDigest(state.execution) !== executionIdentityDigest(execution)
  ) {
    throw new Error("no active run is bound to this execution");
  }
  return normalizeRunState(root, state);
}

function resolveRunDir(root, runDir) {
  return path.isAbsolute(runDir) ? runDir : path.join(root, runDir);
}

function authenticatedNodeRunDir(root, state) {
  const workflow = String(state?.workflow || "");
  const runId = String(state?.run_id || "");
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(workflow)
    || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(runId)
  ) {
    throw new Error("blocked: active Node run identity is invalid");
  }
  const expected = path.join(root, ".agent-flow", "runs", workflow, runId);
  const configured = resolveRunDir(root, String(state?.run_dir || ""));
  if (path.resolve(configured) !== path.resolve(expected)) {
    throw new Error("blocked: active Node run directory is outside its authenticated root");
  }
  ensureChildPath(root, expected);
  assertNoSymlinkComponents(root, expected);
  return expected;
}

function assertInstalled(root) {
  const phases = fullFeaturePhases();
  const skillIndex = readJsonIfExists(path.join(root, ".agent-flow", "skills", "index.json"));
  const selectedSkillPaths = Array.isArray(skillIndex?.skills)
    ? skillIndex.skills
        .map((skill) => selectedSkillPath(root, skill))
        .filter(Boolean)
    : [];
  const required = [
    path.join(root, ".agent-flow", "kit.json"),
    path.join(root, ".agent-flow", "workflows", "full-feature.yaml"),
    path.join(root, ".agent-flow", "skills", "index.json"),
    path.join(root, ".agent-flow", "skills", "full-feature-workflow", "SKILL.md"),
    ...phases.map((phase) => path.join(root, ".agent-flow", "prompts", `${phase.id}.md`)),
    path.join(root, ".agent-flow", "skills", "domain-modeling", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "product-brief", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "plan-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "architecture-reviewer", "SKILL.md"),
    path.join(root, ".agent-flow", "skills", "push-watch", "SKILL.md"),
    ...selectedSkillPaths,
    path.join(root, ".agent-flow", "prompts", "push-watch.md"),
    path.join(root, ".agent-flow", "prompts", "push-watch-tick.md"),
    path.join(root, ".agent-flow", "bootstrap", "AGENTS.md"),
    path.join(root, ".agent-flow", "bootstrap", "CLAUDE.md"),
    path.join(root, ".Codex", "agents", "code-reviewer.md"),
    path.join(root, ".claude", "agents", "code-reviewer.md"),
    path.join(root, ".omp", "agents", "code-reviewer.md"),
  ];
  const missing = required.filter((pathName) => !fs.existsSync(pathName));
  if (missing.length > 0) {
    throw new Error(
      `agent-flow is not installed. run: agent-flow-kit install; missing: `
      + missing.map((pathName) => path.relative(root, pathName)).join(", "),
    );
  }
  for (const codeReviewer of [
    path.join(root, ".Codex", "agents", "code-reviewer.md"),
    path.join(root, ".claude", "agents", "code-reviewer.md"),
    path.join(root, ".omp", "agents", "code-reviewer.md"),
  ]) {
    if (!fs.readFileSync(codeReviewer, "utf8").trim()) {
      throw new Error(`agent-flow is not installed correctly: ${path.relative(root, codeReviewer)} is empty`);
    }
  }
}

function selectedSkillPath(root, skill) {
  if (!skill || typeof skill !== "object") {
    return null;
  }
  if (typeof skill.path === "string" && skill.path) {
    return path.isAbsolute(skill.path) ? skill.path : path.join(root, skill.path);
  }
  if (typeof skill.name === "string" && skill.name) {
    return path.join(root, ".agent-flow", "skills", skill.name, "SKILL.md");
  }
  return null;
}

function normalizeRunState(root, state) {
  if (state.status === "complete" || state.phase === "complete") {
    return state;
  }
  const index = workflowPhases(state.workflow).findIndex((phase) => phase.id === state.phase);
  if (index === -1 || index === state.phase_index) {
    return state;
  }
  const normalized = {
    ...state,
    phase_index: index,
  };
  writeOwnedJson(path.join(authenticatedNodeRunDir(root, normalized), "manifest.json"), normalized);
  writeCurrentRun(root, normalized);
  return normalized;
}

function currentRunPath(root) {
  return path.join(root, ".agent-flow", "state", "current-run.json");
}

function executionRunStatePath(root, execution) {
  return path.join(
    nodeGitPrivateDirectory(root, ["agent-flow", "current-runs"], true),
    `${executionIdentityDigest(execution)}.json`,
  );
}

function executionBindingPath(root, execution) {
  return path.join(
    nodeGitPrivateDirectory(root, ["agent-flow", "executions"], true),
    `${executionIdentityDigest(execution)}.json`,
  );
}

function activeNodeExecutionRunStates(root) {
  const statesRoot = nodeGitPrivateDirectory(root, ["agent-flow", "current-runs"]);
  if (!fs.existsSync(statesRoot)) return [];
  const states = [];
  for (const entry of fs.readdirSync(statesRoot, { withFileTypes: true })) {
    if (!entry.name.endsWith(".json")) continue;
    if (entry.isSymbolicLink()) {
      throw new Error("blocked: Node run state is a symbolic link");
    }
    if (entry.isFile()) states.push(readOwnedJson(path.join(statesRoot, entry.name)));
  }
  return states.filter((state) => nodeStatePublicationIsActive(root, state));
}

function activeExecutionBindings(root) {
  const bindingsRoot = nodeGitPrivateDirectory(root, ["agent-flow", "executions"]);
  if (!fs.existsSync(bindingsRoot)) return [];
  const bindings = [];
  for (const entry of fs.readdirSync(bindingsRoot, { withFileTypes: true })) {
    if (!entry.name.endsWith(".json")) continue;
    if (entry.isSymbolicLink()) {
      throw new Error("blocked: execution binding is a symbolic link");
    }
    if (entry.isFile()) bindings.push(readOwnedJson(path.join(bindingsRoot, entry.name)));
  }
  return bindings.filter((binding) => nodeBindingIsActive(binding, root));
}

function activePythonWorkspaceRuns(root) {
  const runtimeRoot = nodeGitPrivateDirectory(root, ["agent-flow", "worktrees"]);
  if (!fs.existsSync(runtimeRoot)) return [];
  const active = [];
  for (const worktreeEntry of fs.readdirSync(runtimeRoot, { withFileTypes: true })) {
    if (worktreeEntry.isSymbolicLink()) {
      throw new Error("blocked: Python worktree runtime metadata is a symbolic link");
    }
    if (!worktreeEntry.isDirectory()) continue;
    const runsRoot = path.join(runtimeRoot, worktreeEntry.name, ".agent-flow", "runs");
    assertNoSymlinkComponents(runtimeRoot, runsRoot);
    if (!fs.existsSync(runsRoot)) continue;
    for (const runEntry of fs.readdirSync(runsRoot, { withFileTypes: true })) {
      if (runEntry.isSymbolicLink()) {
        throw new Error("blocked: Python run metadata is a symbolic link");
      }
      if (!runEntry.isDirectory()) continue;
      const runDir = path.join(runsRoot, runEntry.name);
      const activePath = path.join(runDir, "active");
      if (!fs.existsSync(activePath)) continue;
      const activeMetadata = fs.lstatSync(activePath);
      if (!activeMetadata.isFile() || activeMetadata.isSymbolicLink()) {
        throw new Error("blocked: Python active marker is not regular");
      }
      const state = readJsonIfExists(path.join(runDir, "meta.json"));
      const runtimeManifest = readJsonIfExists(
        path.join(runtimeRoot, worktreeEntry.name, "manifest.json"),
      );
      const workspace = state?.workspace ?? runtimeManifest?.identity;
      if (!workspace?.workspace_root) {
        throw new Error(
          `blocked: incomplete active Python run has no pinned workspace: ${runDir}`,
        );
      }
      active.push({
        ...state,
        workspace,
        run_id: state?.run_id ?? runEntry.name,
        run_dir: runDir,
      });
    }
  }
  return active;
}

function nodeRunStateIsActive(state) {
  return Boolean(
    state
    && typeof state === "object"
    && state.status !== "complete"
    && state.status !== "aborted"
    && state.phase !== "complete",
  );
}

function nodeStatePublicationIsActive(root, state) {
  if (!nodeRunStateIsActive(state)) return false;
  if (state.publication_status === undefined) return true;
  if (state.publication_status !== "active") return false;
  const manifestPath = path.join(resolveRunDir(root, state.run_dir), "manifest.json");
  if (!fs.existsSync(manifestPath)) return false;
  const manifest = readOwnedJson(manifestPath);
  if (state.start_claim_token === undefined) {
    return !state.workspace
      && manifest.publication_status === "active"
      && manifest.run_id === state.run_id
      && manifest.start_claim_token === undefined;
  }
  if (
    typeof state.start_claim_token !== "string"
    || !state.start_claim_token
    || manifest.publication_status !== "active"
    || manifest.run_id !== state.run_id
    || manifest.start_claim_token !== state.start_claim_token
  ) return false;
  if (!state.execution || !state.workspace) return true;
  const bindingPath = executionBindingPath(root, state.execution);
  if (!fs.existsSync(bindingPath)) return false;
  const binding = readOwnedJson(bindingPath);
  return binding.run_id === state.run_id
    && binding.start_claim_token === state.start_claim_token
    && binding.run_dir === fs.realpathSync(resolveRunDir(root, state.run_dir));
}

function nodeStatePublicationError(root, state) {
  if (state?.publication_status === "active" && state?.execution && state?.workspace) {
    if (
      typeof state.workspace.workspace_root === "string"
      && !fs.existsSync(state.workspace.workspace_root)
    ) {
      return `pinned workspace is missing: ${state.workspace.workspace_root}`;
    }
    const bindingPath = executionBindingPath(root, state.execution);
    if (!fs.existsSync(bindingPath)) {
      return "execution_binding_missing: active execution lost its worktree binding";
    }
  }
  return "execution_binding_incomplete: execution run publication is incomplete";
}

function gitCommonAgentFlowRoot(root) {
  const common = gitOutput(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  if (!common) return path.join(root, ".agent-flow", "state");
  return path.join(authenticatedDirectoryRoot(common), "agent-flow");
}

function nodeGitPrivateDirectory(root, components, create = false) {
  const common = gitOutput(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  const boundary = authenticatedDirectoryRoot(common || path.resolve(root));
  const normalizedComponents = (
    !common && components[0] === "agent-flow"
      ? [".agent-flow", "state", ...components.slice(1)]
      : components
  );
  let cursor = boundary;
  for (const component of normalizedComponents) {
    if (!component || path.basename(component) !== component || [".", ".."].includes(component)) {
      throw new Error("blocked: git-private metadata path is invalid");
    }
    cursor = path.join(cursor, component);
    let metadata = lstatIfExists(cursor);
    if (!metadata && create) {
      try {
        fs.mkdirSync(cursor, { mode: 0o700 });
      } catch (error) {
        if (error?.code !== "EEXIST") throw error;
      }
      metadata = fs.lstatSync(cursor);
    }
    if (!metadata) return cursor;
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`blocked: git-private metadata path is not an owned directory: ${cursor}`);
    }
  }
  return cursor;
}

function authenticatedDirectoryRoot(directory) {
  const absolute = path.resolve(directory);
  const parsed = path.parse(absolute);
  let cursor = parsed.root;
  const relative = path.relative(parsed.root, absolute);
  for (const component of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, component);
    const metadata = fs.lstatSync(cursor);
    if (metadata.isSymbolicLink()) {
      const trustedAliases = process.platform === "darwin"
        ? new Map([
            ["/tmp", "/private/tmp"],
            ["/var", "/private/var"],
          ])
        : new Map();
      const expectedAlias = trustedAliases.get(cursor);
      if (expectedAlias) {
        try {
          const resolvedAlias = fs.realpathSync(cursor);
          const privateMetadata = fs.lstatSync("/private");
          const targetMetadata = fs.lstatSync(expectedAlias);
          const followedMetadata = fs.statSync(cursor);
          if (
            metadata.uid === 0
            && resolvedAlias === expectedAlias
            && privateMetadata.uid === 0
            && privateMetadata.isDirectory()
            && targetMetadata.uid === 0
            && targetMetadata.isDirectory()
            && followedMetadata.dev === targetMetadata.dev
            && followedMetadata.ino === targetMetadata.ino
          ) {
            cursor = resolvedAlias;
            continue;
          }
        } catch {}
      }
      throw new Error(`blocked: git-private metadata root is not an owned directory: ${cursor}`);
    }
    if (!metadata.isDirectory()) {
      throw new Error(`blocked: git-private metadata root is not an owned directory: ${cursor}`);
    }
  }
  return cursor;
}

function acquireNodeWorkspaceStartClaim(
  root,
  identity,
  runId = "pending",
  startDetails = null,
) {
  if (!identity?.workspace_root) return null;
  const claimsRoot = nodeGitPrivateDirectory(
    root,
    ["agent-flow", "workspace-start-claims"],
    true,
  );
  const digest = crypto.createHash("sha256").update(identity.workspace_root).digest("hex");
  const claimPath = path.join(claimsRoot, `${digest}.lock`);
  const token = crypto.randomBytes(16).toString("hex");
  const processStartId = processStartIdentity(process.pid);
  if (!processStartId) {
    throw new Error("blocked: execution_binding_conflict: process start identity is unavailable");
  }
  const claimPayload = {
    version: 2,
    pid: process.pid,
    process_start_id: processStartId,
    token,
    run_id: runId,
    ...(startDetails?.runDir ? {
      start_kind: "node-run",
      run_dir: path.resolve(startDetails.runDir),
      execution: startDetails.execution || null,
    } : {}),
    ...nodeWorkspaceClaimIdentity(identity),
    acquired_at: new Date().toISOString(),
  };
  const temporary = path.join(
    claimsRoot,
    `.${digest}.${process.pid}.${token}.tmp`,
  );
  fs.writeFileSync(temporary, JSON.stringify(claimPayload), { flag: "wx", mode: 0o600 });
  const temporaryMetadata = fs.lstatSync(temporary);
  let acquired = false;
  try {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        fs.linkSync(temporary, claimPath);
        acquired = true;
        break;
      } catch (error) {
        if (
          error?.code === "EEXIST"
          && attempt === 0
          && recoverStaleNodeWorkspaceStartClaim(root, claimPath, identity)
        ) {
          continue;
        }
        if (error?.code === "EEXIST") {
          throw new Error("blocked: execution_binding_conflict: workspace start is already in progress");
        }
        throw error;
      }
    }
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
  if (!acquired) throw new Error("blocked: execution_binding_conflict: workspace start claim is unavailable");
  const metadata = fs.lstatSync(claimPath);
  if (
    !metadata.isFile()
    || metadata.isSymbolicLink()
    || metadata.dev !== temporaryMetadata.dev
    || metadata.ino !== temporaryMetadata.ino
  ) {
    throw new Error("blocked: workspace start claim publication changed");
  }
  return {
    path: claimPath,
    dev: metadata.dev,
    ino: metadata.ino,
    token,
    pid: process.pid,
    processStartId,
    runId,
    payload: claimPayload,
  };
}

function nodeWorkspaceClaimIdentity(identity) {
  const leaderRoot = fs.realpathSync(path.dirname(fs.realpathSync(identity.git_common_dir)));
  const leaderMetadata = fs.statSync(leaderRoot);
  return {
    leader_root: leaderRoot,
    leader_device: leaderMetadata.dev,
    leader_inode: leaderMetadata.ino,
    workspace_root: identity.workspace_root,
    workspace_git_dir: identity.git_dir,
    workspace_branch: identity.branch,
    workspace_head: identity.head,
    workspace_device: identity.device,
    workspace_inode: identity.inode,
  };
}

function nodeWorkspaceClaimMatchesIdentity(claim, identity) {
  const expected = nodeWorkspaceClaimIdentity(identity);
  return Object.entries(expected).every(([key, value]) => claim?.[key] === value);
}

function recoverStaleNodeWorkspaceStartClaim(root, claimPath, identity) {
  let metadata;
  try {
    metadata = fs.lstatSync(claimPath);
  } catch (error) {
    if (error?.code === "ENOENT") return true;
    throw error;
  }
  if (!metadata.isFile() || metadata.isSymbolicLink()) return false;
  const claim = readJsonIfExists(claimPath);
  if (
    ![1, 2].includes(claim?.version)
    || !Number.isInteger(claim.pid)
    || claim.pid <= 0
    || typeof claim.token !== "string"
    || !claim.token
    || claim.workspace_root !== identity.workspace_root
  ) {
    return false;
  }
  if (claim.version === 2) {
    if (
      typeof claim.process_start_id !== "string"
      || !claim.process_start_id
      || typeof claim.run_id !== "string"
      || !claim.run_id
      || !nodeWorkspaceClaimMatchesIdentity(claim, identity)
    ) {
      return false;
    }
    if (processIsAlive(claim.pid)) {
      const currentStartId = processStartIdentity(claim.pid);
      if (!currentStartId || currentStartId === claim.process_start_id) return false;
    }
  } else if (processIsAlive(claim.pid)) {
    return false;
  }
  const quarantine = path.join(
    path.dirname(claimPath),
    `.${path.basename(claimPath)}.stale-${process.pid}-${crypto.randomBytes(8).toString("hex")}`,
  );
  try {
    fs.renameSync(claimPath, quarantine);
  } catch (error) {
    if (error?.code === "ENOENT") return true;
    throw error;
  }
  const moved = fs.lstatSync(quarantine);
  const movedClaim = readJsonIfExists(quarantine);
  if (
    moved.dev !== metadata.dev
    || moved.ino !== metadata.ino
    || JSON.stringify(movedClaim) !== JSON.stringify(claim)
  ) {
    if (!fs.existsSync(claimPath)) fs.renameSync(quarantine, claimPath);
    throw new Error("blocked: workspace start claim changed during stale recovery");
  }
  try {
    if (movedClaim.start_kind === "node-run") {
      recoverIncompleteNodeStart(root, movedClaim);
    }
  } catch (error) {
    if (!fs.existsSync(claimPath)) fs.renameSync(quarantine, claimPath);
    throw error;
  }
  fs.unlinkSync(quarantine);
  return true;
}

function recoverIncompleteNodeStart(root, claim) {
  const runsRoot = path.resolve(root, ".agent-flow", "runs");
  const runDir = path.resolve(String(claim.run_dir || ""));
  const relative = path.relative(runsRoot, runDir);
  if (
    !relative
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
    || path.basename(runDir) !== claim.run_id
  ) {
    throw new Error("blocked: stale Node start claim has an invalid run directory");
  }
  const manifestPath = path.join(runDir, "manifest.json");
  const manifest = fs.existsSync(manifestPath) ? readOwnedJson(manifestPath) : null;
  if (manifest && (
    manifest.run_id !== claim.run_id
    || manifest.start_claim_token !== claim.token
  )) {
    throw new Error("blocked: stale Node start manifest ownership changed");
  }
  const execution = claim.execution;
  const statePath = execution ? executionRunStatePath(root, execution) : currentRunPath(root);
  const state = fs.existsSync(statePath)
    ? (execution ? readOwnedJson(statePath) : readJsonIfExists(statePath))
    : null;
  const bindingPath = execution ? executionBindingPath(root, execution) : null;
  const binding = bindingPath && fs.existsSync(bindingPath) ? readOwnedJson(bindingPath) : null;
  const fullyPublished = Boolean(
    manifest?.publication_status === "active"
    && state?.publication_status === "active"
    && state.run_id === claim.run_id
    && state.start_claim_token === claim.token
    && (!execution || (
      binding?.run_id === claim.run_id
      && binding.start_claim_token === claim.token
    )),
  );
  if (fullyPublished) return;
  if (state?.run_id === claim.run_id && state.start_claim_token === claim.token) {
    if (execution) removeOwnedJsonAtomic(statePath, state);
    else fs.unlinkSync(statePath);
  }
  if (
    bindingPath
    && binding?.run_id === claim.run_id
    && binding.start_claim_token === claim.token
  ) {
    removeOwnedJsonAtomic(bindingPath, binding);
  }
  if (!fs.existsSync(runDir)) return;
  const entries = fs.readdirSync(runDir, { withFileTypes: true });
  if (entries.some((entry) => !["artifacts", "logs", "manifest.json"].includes(entry.name))) {
    throw new Error("blocked: incomplete Node run contains unowned files");
  }
  for (const directoryName of ["artifacts", "logs"]) {
    const directory = path.join(runDir, directoryName);
    if (!fs.existsSync(directory)) continue;
    const metadata = fs.lstatSync(directory);
    if (!metadata.isDirectory() || metadata.isSymbolicLink() || fs.readdirSync(directory).length) {
      throw new Error("blocked: incomplete Node run directory is not empty and owned");
    }
    fs.rmdirSync(directory);
  }
  if (fs.existsSync(manifestPath)) removeOwnedJsonAtomic(manifestPath, manifest);
  fs.rmdirSync(runDir);
}

function releaseNodeWorkspaceStartClaim(claim) {
  if (!claim) return;
  const metadata = fs.lstatSync(claim.path);
  if (
    !metadata.isFile()
    || metadata.isSymbolicLink()
    || metadata.dev !== claim.dev
    || metadata.ino !== claim.ino
  ) {
    throw new Error("blocked: workspace start claim identity changed");
  }
  const owner = readJsonIfExists(claim.path);
  if (
    owner?.version !== 2
    || owner.token !== claim.token
    || owner.pid !== claim.pid
    || owner.process_start_id !== claim.processStartId
    || owner.run_id !== claim.runId
  ) {
    throw new Error("blocked: workspace start claim ownership changed");
  }
  fs.unlinkSync(claim.path);
}

function processStartIdentity(pid) {
  if (process.platform === "linux") {
    try {
      const statText = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
      const fields = statText.slice(statText.lastIndexOf(")") + 2).trim().split(/\s+/);
      const bootId = fs.readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
      if (fields.length > 19 && bootId) return `linux:${bootId}:${fields[19]}`;
    } catch {
      // procfs를 사용할 수 없는 Linux host에서는 ps fallback을 사용한다.
    }
  }
  const result = spawnSync("/bin/ps", ["-o", "lstart=", "-p", String(pid)], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    env: { LC_ALL: "C", LANG: "C", TZ: "UTC0", PATH: "/usr/bin:/bin" },
  });
  if (result.error || result.status !== 0) return null;
  return String(result.stdout || "").trim() || null;
}

function currentExecutionIdentity(env = process.env) {
  const host = String(
    env.AGENT_FLOW_ACTIVE_HOST
    || env.AGENT_FLOW_HOST
    || detectActiveHost(env)
    || "unknown",
  ).trim().toLowerCase();
  const hostSession = host === "codex"
    ? env.CODEX_THREAD_ID || env.CODEX_SESSION_ID
    : host === "claude"
      ? env.CLAUDE_SESSION_ID
      : host === "omp"
        ? env.OMP_SESSION_ID
        : "";
  const sessionId = String(
    env.AGENT_FLOW_EXECUTION_ID
    || env.AGENT_FLOW_SESSION_ID
    || hostSession
    || "",
  ).trim();
  if (!sessionId) return null;
  const hostAgent = host === "codex"
    ? env.CODEX_AGENT_ID
    : host === "claude"
      ? env.CLAUDE_AGENT_ID
      : host === "omp"
        ? env.OMP_AGENT_ID
        : "";
  const agentId = String(env.AGENT_FLOW_AGENT_ID || hostAgent || "").trim();
  if (!/^[a-z0-9_-]{1,32}$/.test(host) || sessionId.length > 512 || agentId.length > 512) {
    throw new Error("blocked: execution identity is invalid");
  }
  return { host, session_id: sessionId, agent_id: agentId };
}

function executionIdentityDigest(execution) {
  const canonical = JSON.stringify({
    agent_id: String(execution.agent_id || ""),
    host: String(execution.host || ""),
    session_id: String(execution.session_id || ""),
  });
  return crypto.createHash("sha256").update(canonical).digest("hex");
}

function writeCurrentRun(root, state) {
  if (state.execution) {
    writeOwnedJson(executionRunStatePath(root, state.execution), state);
  } else {
    writeOwnedJson(currentRunPath(root), state);
  }
}

function assertNodeExecutionStartAvailable(root, execution, workspace) {
  const activeStates = activeNodeExecutionRunStates(root);
  const activeBindings = activeExecutionBindings(root);
  const activePythonRuns = activePythonWorkspaceRuns(root);
  const legacy = readJsonIfExists(currentRunPath(root));
  if (!execution) {
    if (
      activeStates.length > 0
      || activeBindings.length > 0
      || activePythonRuns.length > 0
      || nodeRunStateIsActive(legacy)
    ) {
      throw new Error("blocked: execution_identity_missing: active runs require a stable execution identity");
    }
    return;
  }
  if (nodeRunStateIsActive(legacy)) {
    throw new Error(`blocked: execution_binding_conflict: legacy active run ${legacy.run_id} already exists`);
  }
  const statePath = executionRunStatePath(root, execution);
  if (fs.existsSync(statePath)) {
    const state = readOwnedJson(statePath);
    if (nodeRunStateIsActive(state)) {
      if (!nodeStatePublicationIsActive(root, state)) {
        throw new Error(`blocked: ${nodeStatePublicationError(root, state)}`);
      }
      const bindingPath = executionBindingPath(root, execution);
      if (state.workspace && !fs.existsSync(bindingPath)) {
        throw new Error("blocked: execution_binding_missing: active execution lost its worktree binding");
      }
      if (state.workspace && !nodeBindingIsActive(readOwnedJson(bindingPath), root)) {
        throw new Error("blocked: execution_binding_stale: active execution binding is not usable");
      }
      throw new Error(`blocked: execution_binding_conflict: execution already owns active run ${state.run_id}`);
    }
  }
  const bindingPath = executionBindingPath(root, execution);
  if (fs.existsSync(bindingPath)) {
    const binding = readOwnedJson(bindingPath);
    if (nodeBindingIsActive(binding, root)) {
      throw new Error(`blocked: execution_binding_conflict: execution already owns active run ${binding.run_id}`);
    }
  }
  if (!workspace.identity) return;
  const executionDigest = executionIdentityDigest(execution);
  const stateOwner = activeStates.find((state) => (
    state.workspace?.workspace_root
    && samePath(state.workspace.workspace_root, workspace.identity.workspace_root)
    && (!state.execution || executionIdentityDigest(state.execution) !== executionDigest)
  ));
  if (stateOwner) {
    throw new Error(
      `blocked: execution_binding_conflict: workspace already owns active run ${stateOwner.run_id}`,
    );
  }
  const bindingOwner = activeBindings.find((binding) => (
    binding.workspace?.workspace_root
    && samePath(binding.workspace.workspace_root, workspace.identity.workspace_root)
    && (!binding.execution || executionIdentityDigest(binding.execution) !== executionDigest)
  ));
  if (bindingOwner) {
    throw new Error(
      `blocked: execution_binding_conflict: workspace already owns active run ${bindingOwner.run_id}`,
    );
  }
  const pythonOwner = activePythonRuns.find((state) => (
    state.workspace?.workspace_root
    && samePath(state.workspace.workspace_root, workspace.identity.workspace_root)
  ));
  if (pythonOwner) {
    throw new Error(
      `blocked: execution_binding_conflict: workspace already owns active run ${pythonOwner.run_id}`,
    );
  }
}

function bindNodeExecution(root, execution, state) {
  const bindingPath = executionBindingPath(root, execution);
  const runDir = resolveRunDir(root, state.run_dir);
  const payload = {
    version: 2,
    execution,
    workspace: state.workspace,
    workspace_name: path.basename(state.workspace.workspace_root),
    run_id: state.run_id,
    run_dir: fs.realpathSync(runDir),
    ...(state.start_claim_token ? { start_claim_token: state.start_claim_token } : {}),
    bound_at: new Date().toISOString(),
  };
  const temporary = path.join(
    path.dirname(bindingPath),
    `.${path.basename(bindingPath)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, { flag: "wx", mode: 0o600 });
  const temporaryMetadata = fs.lstatSync(temporary);
  try {
    let published = false;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        fs.linkSync(temporary, bindingPath);
        published = true;
        break;
      } catch (error) {
        if (error?.code !== "EEXIST") throw error;
        const existing = readOwnedJson(bindingPath);
        if (nodeBindingIsActive(existing, root)) {
          if (sameNodeExecutionBinding(existing, payload)) return;
          throw new Error("blocked: execution is already bound to an active worktree");
        }
        if (attempt === 0) {
          removeOwnedJsonAtomic(bindingPath, existing);
          continue;
        }
        throw new Error("blocked: execution binding publication raced");
      }
    }
    if (!published) throw new Error("blocked: execution binding is unavailable");
    const metadata = fs.lstatSync(bindingPath);
    if (
      !metadata.isFile()
      || metadata.isSymbolicLink()
      || metadata.dev !== temporaryMetadata.dev
      || metadata.ino !== temporaryMetadata.ino
    ) {
      throw new Error("blocked: execution binding publication changed");
    }
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

function readOwnedJson(pathName) {
  const metadata = fs.lstatSync(pathName);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error(`blocked: git-private metadata file is not regular: ${pathName}`);
  }
  const payload = JSON.parse(fs.readFileSync(pathName, "utf8"));
  const repeated = fs.lstatSync(pathName);
  if (repeated.dev !== metadata.dev || repeated.ino !== metadata.ino) {
    throw new Error(`blocked: git-private metadata changed while reading: ${pathName}`);
  }
  return payload;
}

function writeOwnedJson(pathName, payload) {
  if (fs.existsSync(pathName)) {
    const metadata = fs.lstatSync(pathName);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      throw new Error(`blocked: git-private metadata file is not regular: ${pathName}`);
    }
  }
  const temporary = path.join(
    path.dirname(pathName),
    `.${path.basename(pathName)}.write-${process.pid}-${crypto.randomBytes(8).toString("hex")}`,
  );
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
    flag: "wx",
    mode: 0o600,
  });
  try {
    fs.renameSync(temporary, pathName);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

function removeOwnedJsonAtomic(pathName, payload) {
  const metadata = fs.lstatSync(pathName);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error(`blocked: git-private metadata file is not regular: ${pathName}`);
  }
  const quarantine = path.join(
    path.dirname(pathName),
    `.${path.basename(pathName)}.remove-${process.pid}-${crypto.randomBytes(8).toString("hex")}`,
  );
  fs.renameSync(pathName, quarantine);
  const moved = fs.lstatSync(quarantine);
  const movedPayload = readOwnedJson(quarantine);
  if (
    moved.dev !== metadata.dev
    || moved.ino !== metadata.ino
    || JSON.stringify(movedPayload) !== JSON.stringify(payload)
  ) {
    if (!fs.existsSync(pathName)) fs.renameSync(quarantine, pathName);
    throw new Error(`blocked: git-private metadata changed before removal: ${pathName}`);
  }
  fs.unlinkSync(quarantine);
}

function sameNodeExecutionBinding(existing, expected) {
  return ["execution", "workspace", "run_id", "run_dir"].every(
    (key) => canonicalJson(existing[key]) === canonicalJson(expected[key]),
  );
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

function nextNodeWorkspaceGeneration(root, workspace) {
  const workspaceName = path.basename(workspace.workspace_root);
  const runtimeRoot = nodeGitPrivateDirectory(
    root,
    ["agent-flow", "worktrees", workspaceName],
  );
  if (!fs.existsSync(runtimeRoot)) return 1;
  const finalizerPath = path.join(runtimeRoot, "finalizer.json");
  if (!fs.existsSync(finalizerPath)) return 1;
  const existing = readOwnedJson(finalizerPath);
  if (!Number.isInteger(existing.generation) || existing.generation < 1) {
    throw new Error("blocked: worktree finalizer generation is invalid");
  }
  return existing.generation + 1;
}

function recordNodeWorkspaceFinalizer(root, state, completedAt) {
  const workspaceName = path.basename(state.workspace.workspace_root);
  const runtimeRoot = nodeGitPrivateDirectory(
    root,
    ["agent-flow", "worktrees", workspaceName],
  );
  if (!fs.existsSync(runtimeRoot)) {
    throw new Error("blocked: completed worktree runtime metadata is missing");
  }
  const finalizerPath = path.join(runtimeRoot, "finalizer.json");
  const generation = state.workspace_generation;
  if (!Number.isInteger(generation) || generation < 1) {
    throw new Error("blocked: completed run workspace generation is invalid");
  }
  if (fs.existsSync(finalizerPath)) {
    const existing = readOwnedJson(finalizerPath);
    if (!Number.isInteger(existing.generation) || existing.generation < 1) {
      throw new Error("blocked: completed worktree finalizer generation is invalid");
    }
    const sameCompletion = (
      canonicalJson(existing.execution) === canonicalJson(state.execution)
      && canonicalJson(existing.workspace) === canonicalJson(state.workspace)
      && existing.run_id === state.run_id
      && existing.run_dir === fs.realpathSync(resolveRunDir(root, state.run_dir))
      && existing.generation === generation
    );
    if (sameCompletion) return finalizerPath;
    if (generation !== existing.generation + 1) {
      throw new Error("blocked: execution_finalizer_stale: a newer worktree completion already exists");
    }
  } else if (generation !== 1) {
    throw new Error("blocked: execution_finalizer_stale: worktree completion history is missing");
  }
  const payload = {
    version: 1,
    generation,
    execution: state.execution,
    workspace: state.workspace,
    workspace_name: workspaceName,
    run_id: state.run_id,
    run_dir: fs.realpathSync(resolveRunDir(root, state.run_dir)),
    completed_at: completedAt,
  };
  const temporary = path.join(
    runtimeRoot,
    `.finalizer.json.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
    flag: "wx",
    mode: 0o600,
  });
  try {
    fs.renameSync(temporary, finalizerPath);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
  return finalizerPath;
}

function finalizeCompletedNodeRun(root, state, runDir, { persistState = true } = {}) {
  const completedAt = String(state.completed_at || state.updated_at || "");
  if (!completedAt) throw new Error("blocked: completed Node run timestamp is missing");
  const completionClaim = (
    state.execution
    && state.workspace
    && !samePath(state.workspace.workspace_root, root)
      ? acquireNodeWorkspaceStartClaim(
          root,
          state.workspace,
          `finalize:${state.run_id}`,
        )
      : null
  );
  try {
    if (completionClaim) {
      recordNodeWorkspaceFinalizer(root, state, completedAt);
      if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_NODE_FINALIZER === "1") {
        process.exit(92);
      }
    }
    if (persistState) {
      writeOwnedJson(path.join(runDir, "manifest.json"), state);
      writeCurrentRun(root, state);
    }
    removeNodeGateCache(root, runDir);
    if (state.execution && state.workspace) {
      releaseNodeExecution(root, state.execution, state.run_dir, {
        allowReusedExecution: !persistState,
      });
    }
  } finally {
    releaseNodeWorkspaceStartClaim(completionClaim);
  }
}

function removeNodeGateCache(root, runDir) {
  const cacheRoot = nodeGitPrivateDirectory(root, ["agent-flow", "gate-cache"]);
  if (!fs.existsSync(cacheRoot)) return;
  const resolvedRun = fs.realpathSync(runDir);
  const metadata = fs.lstatSync(resolvedRun);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error("blocked: gate cache run directory is not regular");
  }
  const digest = crypto.createHash("sha256")
    .update(`${resolvedRun}\0${metadata.dev}\0${metadata.ino}`)
    .digest("hex");
  const cachePath = path.join(cacheRoot, `${digest}.json`);
  if (!fs.existsSync(cachePath)) return;
  removeOwnedJsonAtomic(cachePath, readOwnedJson(cachePath));
}

function releaseNodeExecution(
  root,
  execution,
  runDir,
  { allowReusedExecution = false } = {},
) {
  const bindingPath = executionBindingPath(root, execution);
  if (!fs.existsSync(bindingPath)) return;
  const binding = readOwnedJson(bindingPath);
  const expectedRunDir = fs.realpathSync(resolveRunDir(root, runDir));
  if (binding.run_dir !== expectedRunDir) {
    if (allowReusedExecution) {
      const statePath = executionRunStatePath(root, execution);
      if (fs.existsSync(statePath)) {
        const current = readOwnedJson(statePath);
        const currentRunDir = fs.realpathSync(resolveRunDir(root, current.run_dir));
        if (
          nodeStatePublicationIsActive(root, current)
          && executionIdentityDigest(current.execution) === executionIdentityDigest(execution)
          && currentRunDir === binding.run_dir
        ) return;
      }
    }
    throw new Error("blocked: execution binding run identity changed");
  }
  if (executionIdentityDigest(binding.execution) !== executionIdentityDigest(execution)) {
    throw new Error("blocked: execution binding identity changed");
  }
  removeOwnedJsonAtomic(bindingPath, binding);
}

function nodeBindingIsActive(binding, root) {
  if (typeof binding?.run_dir !== "string") return false;
  const activePath = path.join(binding.run_dir, "active");
  if (fs.existsSync(activePath)) {
    const active = fs.lstatSync(activePath);
    if (active.isSymbolicLink() || !active.isFile()) return false;
    if (binding.start_claim_token === undefined) return true;
  }
  const manifest = path.join(binding.run_dir, "manifest.json");
  if (!fs.existsSync(manifest)) return false;
  const state = readOwnedJson(manifest);
  if (!nodeRunStateIsActive(state)) return false;
  if (binding.start_claim_token === undefined) return true;
  if (
    typeof binding.start_claim_token !== "string"
    || !binding.start_claim_token
    || state.start_claim_token !== binding.start_claim_token
    || state.run_id !== binding.run_id
  ) return false;
  if (state.publication_status === "starting") {
    return nodeStartingBindingClaimIsLive(binding, root);
  }
  if (state.publication_status !== "active") return false;
  const currentPath = executionRunStatePath(root, binding.execution);
  if (!fs.existsSync(currentPath)) return false;
  const current = readOwnedJson(currentPath);
  return current.publication_status === "active"
    && current.start_claim_token === binding.start_claim_token
    && current.run_id === binding.run_id;
}

function nodeStartingBindingClaimIsLive(binding, root) {
  const workspace = binding?.workspace;
  if (typeof workspace?.workspace_root !== "string" || !workspace.workspace_root) return false;
  const claimsRoot = nodeGitPrivateDirectory(root, ["agent-flow", "workspace-start-claims"]);
  const digest = crypto.createHash("sha256").update(workspace.workspace_root).digest("hex");
  const claimPath = path.join(claimsRoot, `${digest}.lock`);
  const metadata = lstatIfExists(claimPath);
  if (!metadata?.isFile() || metadata.isSymbolicLink()) return false;
  const claim = readJsonIfExists(claimPath);
  if (
    claim?.version !== 2
    || claim.token !== binding.start_claim_token
    || claim.run_id !== binding.run_id
    || !nodeWorkspaceClaimMatchesIdentity(claim, workspace)
    || !Number.isInteger(claim.pid)
    || claim.pid <= 0
    || typeof claim.process_start_id !== "string"
    || !claim.process_start_id
    || !processIsAlive(claim.pid)
  ) return false;
  const currentStartId = processStartIdentity(claim.pid);
  return !currentStartId || currentStartId === claim.process_start_id;
}

function pushWatchStatePath(root) {
  return path.join(root, ".agent-flow", "state", "push-watch.json");
}

function currentBranch(root) {
  const result = safeSpawnSync(projectGitPath(), ["branch", "--show-current"], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  const branch = result.stdout?.trim() ?? "";
  if (result.error || result.status !== 0 || !branch) {
    throw new Error("blocked: push-watch requires a named git branch");
  }
  return branch;
}

function readPullRequestStatus(root) {
  const result = safeSpawnSync("gh", ["pr", "view", "--json", "url,reviewDecision,statusCheckRollup"], {
    cwd: root,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    const detail = result.error?.message ?? result.stderr?.trim() ?? "unknown error";
    throw new Error(`blocked: gh pr view failed: ${detail}`);
  }
  return JSON.parse(result.stdout);
}

function pullRequestWatchStatus(pr) {
  const reviewDecision = String(pr.reviewDecision ?? "").toUpperCase();
  const checks = Array.isArray(pr.statusCheckRollup) ? pr.statusCheckRollup : [];
  const hasFailedCheck = checks.some((check) =>
    ["FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"].includes(
      String(check.conclusion ?? check.state ?? "").toUpperCase(),
    ),
  );
  if (hasFailedCheck) {
    return "ci-failed";
  }
  if (reviewDecision === "CHANGES_REQUESTED") {
    return "comments";
  }
  const hasPendingCheck =
    checks.length === 0 ||
    checks.some((check) => {
      const status = String(check.status ?? "").toUpperCase();
      const conclusion = String(check.conclusion ?? "").toUpperCase();
      const state = String(check.state ?? "").toUpperCase();
      if (state) {
        return state !== "SUCCESS";
      }
      return status !== "COMPLETED" || (conclusion !== "SUCCESS" && conclusion !== "SKIPPED");
    });
  if (hasPendingCheck || reviewDecision !== "APPROVED") {
    return "pending";
  }
  return "green";
}

function printNext(state, root = null) {
  const phase = workflowPhases(state.workflow)[state.phase_index];
  if (!phase) {
    console.log(`workflow complete: ${state.run_id}`);
    return;
  }
  const localSkillBlock = root ? localSkillPromptBlock(root, phase.id) : "";
  const phaseSkillBlock = root ? nodePhaseSkillBlock(root, state, phase) : "";
  console.log(`Current phase: ${phase.id}`);
  console.log(`Run: ${state.run_id}`);
  console.log(`Workspace root: ${state.workspace_root ?? root ?? process.cwd()}`);
  console.log(`Required artifact: ${path.join(state.run_dir, phase.artifact)}`);
  console.log(`Instruction: ${phase.instruction}${phaseSkillBlock}${localSkillBlock}`);
}

function nodePhaseSkillBlock(root, state, phase) {
  const requiredSkills = phase.required_skills ?? [];
  const codePhase = CODE_SKILL_PHASES.has(phase.id);
  if (!codePhase && requiredSkills.length === 0) return "";
  const plan = nodePhaseSkillPlan(root, state, phase);
  const contractEnabled = codePhase || requiredSkills.length > 0 || (phase.requirements ?? []).length > 0;
  const contract = contractEnabled
    ? [
      "",
      "## Runtime phase contract",
      "",
      "Record this exact machine-readable contract line in the artifact, changing only requirement values to `fail` when necessary:",
      "",
      `\`phase-contract: ${JSON.stringify({
        applied_skills: plan.skills.map((skill) => skill.name),
        requirements: Object.fromEntries((phase.requirements ?? []).map((requirement) => [requirement, "pass"])),
      })}\``,
      "",
    ]
    : [];
  if (plan.skills.length === 0) return contract.join("\n");
  return [
    "",
    "",
    "## Selected canonical skills",
    "",
    ...plan.skills.map((skill) => `- \`${skill.name}\`: \`${skill.path}\` (tree \`${skill.tree_hash ?? "unavailable"}\`)`),
    ...contract,
  ].join("\n");
}

function nodePhaseSkillPlan(root, state, phase) {
  const requiredSkills = phase.required_skills ?? [];
  const agentFlowDir = path.join(root, ".agent-flow");
  const kit = readExistingKit(agentFlowDir);
  const authenticated = readAuthenticatedSkillIndex(agentFlowDir, kit);
  if (!authenticated) throw new Error("blocked: installed skill index is missing");
  const plan = resolveRuntimeSkillPlan(authenticated.payload, {
    phaseId: phase.id,
    changedFiles: nodeRuntimeChangedFiles(state),
    taskScope: state.task ?? "",
    requiredSkills,
    indexRoot: root,
    activeHost: detectActiveHost(process.env),
  });
  if (plan.resolution_errors.length > 0) {
    throw new SkillResolutionError(plan.resolution_errors);
  }
  if (plan.missing_profiles.length > 0) {
    throw new Error(`missing required skill profiles in project snapshot: ${plan.missing_profiles.join(", ")}`);
  }
  if (plan.missing.length > 0) {
    throw new Error(`missing required profile skills in project snapshot: ${plan.missing.join(", ")}`);
  }
  return {
    ...plan,
    skill_compatibility: normalizeSkillCompatibility(authenticated.payload.compatibility),
  };
}

function nodeContractPhase(root, state, phase) {
  if (!phase || (
    !CODE_SKILL_PHASES.has(phase.id)
    && (phase.required_skills ?? []).length === 0
    && (phase.requirements ?? []).length === 0
  )) {
    return phase;
  }
  const plan = nodePhaseSkillPlan(root, state, phase);
  return {
    ...phase,
    required_skills: plan.skills.map((skill) => skill.name),
    skill_compatibility: plan.skill_compatibility,
  };
}

function nodeRuntimeChangedFiles(state) {
  const workspace = state.workspace_root;
  if (typeof workspace !== "string" || !fs.existsSync(workspace)) return [];
  const base = state.workspace?.head;
  const tracked = base
    ? gitOutput(workspace, ["diff", "--name-only", base]) ?? ""
    : gitOutput(workspace, ["diff", "--name-only", "HEAD"]) ?? "";
  const untracked = gitOutput(workspace, ["ls-files", "--others", "--exclude-standard"]) ?? "";
  return [...new Set(`${tracked}\n${untracked}`.split(/\r?\n/).filter(Boolean))].sort();
}

function printStatus(state, root) {
  const phase = nodeContractPhase(
    root,
    state,
    workflowPhases(state.workflow)[state.phase_index],
  );
  const resolvedRunDir = resolveRunDir(root, state.run_dir);
  const complete = state.status === "complete" || state.phase === "complete" || !phase;
  const requiredArtifact = phase ? path.join(state.run_dir, phase.artifact) : null;
  const resolvedRequiredArtifact = phase ? path.join(resolvedRunDir, phase.artifact) : null;
  let status = complete ? "complete" : state.status;
  let reason = complete ? "workflow_complete" : "in_progress";
  if (!complete && resolvedRequiredArtifact && !fs.existsSync(resolvedRequiredArtifact)) {
    status = "awaiting_host";
    reason = "missing_phase_artifact";
  } else if (!complete && requiredArtifact) {
    const missing = missingMarkersForPhase(
      fs.readFileSync(resolvedRequiredArtifact, "utf8"),
      phase,
      root,
      resolvedRunDir,
      state,
    );
    status = "blocked";
    if (artifactIsStale(state, resolvedRequiredArtifact)) {
      reason = "stale_artifact";
    } else if (missing.length > 0) {
      reason = "missing_completion_markers";
    } else {
      try {
        nextPhaseIndex(state, workflowPhases(state.workflow), phase, resolvedRequiredArtifact);
        reason = "phase_artifact_written_advance_required";
      } catch (_error) {
        reason = "route_blocked";
      }
    }
  }
  const nextCommand = complete
    ? "none"
    : reason === "route_blocked"
      ? `${AGENT_FLOW_COMMAND} run next`
      : `${AGENT_FLOW_COMMAND} run advance`;
  const payload = {
    status,
    run: `${state.workflow}/${state.run_id}`,
    task: state.task ?? "",
    current_phase: phase?.id ?? "-",
    workspace_root: state.workspace_root ?? root,
    reason,
    required_artifact: requiredArtifact,
    next_command: nextCommand,
  };
  console.log(`${state.workflow} ${state.run_id} ${status} phase=${phase?.id ?? "-"}`);
  console.log(`status: ${statusValue(status)}`);
  console.log(`run: ${statusValue(payload.run)}`);
  console.log(`task: ${statusValue(payload.task)}`);
  console.log(`current_phase: ${statusValue(payload.current_phase)}`);
  console.log(`workspace_root: ${statusValue(payload.workspace_root)}`);
  console.log(`reason: ${statusValue(reason)}`);
  if (requiredArtifact) {
    console.log(`required_artifact: ${statusValue(requiredArtifact)}`);
  }
  console.log(`next_command: ${statusValue(nextCommand)}`);
  console.log(`status_json: ${JSON.stringify(payload)}`);
}

function statusValue(value) {
  return String(value).replace(/\r/g, "\\r").replace(/\n/g, "\\n");
}

function optionValue(args, name) {
  const index = args.indexOf(name);
  if (index === -1) {
    return undefined;
  }
  return args[index + 1];
}

function writeJson(pathName, payload) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  fs.writeFileSync(pathName, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

function readExistingKit(agentFlowDir) {
  const kitPath = path.join(agentFlowDir, "kit.json");
  try {
    fs.lstatSync(kitPath);
  } catch (error) {
    if (error?.code === "ENOENT") return undefined;
    throw error;
  }
  try {
    const payload = readRegularJsonNoFollow(
      kitPath,
      agentFlowDir,
      "existing kit metadata",
    );
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("kit metadata must be an object");
    }
    return payload;
  } catch (error) {
    throw new Error(`invalid existing kit metadata: ${kitPath}`, { cause: error });
  }
}

function writeFileIfMissing(pathName, content) {
  fs.mkdirSync(path.dirname(pathName), { recursive: true });
  if (!fs.existsSync(pathName)) {
    fs.writeFileSync(pathName, content, "utf8");
  }
}

function managedWriteBoundary(pathName, boundaryRoot = null) {
  if (boundaryRoot) {
    ensureChildPath(boundaryRoot, pathName);
    return path.resolve(boundaryRoot);
  }
  if (activeManagedInstallTransaction) {
    for (const candidate of [
      activeManagedInstallTransaction.root,
      activeManagedInstallTransaction.transactionRoot,
    ]) {
      try {
        ensureChildPath(candidate, pathName);
        return path.resolve(candidate);
      } catch {
        // 다음 인증된 transaction 경계를 확인한다.
      }
    }
    throw new Error(`managed install write escapes transaction: ${pathName}`);
  }
  return path.dirname(path.resolve(pathName));
}

function assertNoSymlinkComponents(boundaryRoot, pathName) {
  const boundary = path.resolve(boundaryRoot);
  const target = path.resolve(pathName);
  ensureChildPath(boundary, target);
  const relative = path.relative(boundary, target);
  let cursor = boundary;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    const stat = lstatIfExists(cursor);
    if (stat?.isSymbolicLink()) {
      throw new Error(`managed install path contains a symlink: ${cursor}`);
    }
  }
}

function sameDirectoryIdentity(left, right) {
  return left?.isDirectory() && right?.isDirectory()
    && left.dev === right.dev
    && left.ino === right.ino;
}

function withManagedDirectoryCwd(boundaryRoot, pathName, create, callback) {
  const boundary = path.resolve(boundaryRoot);
  const target = path.resolve(pathName);
  ensureChildPath(boundary, target);
  const savedCwd = process.cwd();
  const savedIdentity = fs.statSync(".");
  let callbackError = null;
  let result;
  try {
    const boundaryIdentity = fs.lstatSync(boundary);
    if (!boundaryIdentity.isDirectory() || boundaryIdentity.isSymbolicLink()) {
      throw new Error(`managed install boundary is unsafe: ${boundary}`);
    }
    process.chdir(boundary);
    if (!sameDirectoryIdentity(boundaryIdentity, fs.statSync("."))) {
      throw new Error(`managed install boundary changed while entering: ${boundary}`);
    }
    for (const part of path.relative(boundary, target).split(path.sep).filter(Boolean)) {
      let identity = lstatIfExists(part);
      if (!identity && create) {
        fs.mkdirSync(part);
        identity = fs.lstatSync(part);
      }
      if (!identity?.isDirectory() || identity.isSymbolicLink()) {
        throw new Error(`managed install path contains a symlink or non-directory: ${path.join(process.cwd(), part)}`);
      }
      process.chdir(part);
      if (!sameDirectoryIdentity(identity, fs.statSync("."))) {
        throw new Error(`managed install ancestor changed while entering: ${target}`);
      }
    }
    const holdSuffix = process.env.AGENT_FLOW_TEST_HOLD_MANAGED_PARENT_SUFFIX;
    if (holdSuffix && target.endsWith(holdSuffix)) {
      holdInstallForTest("AGENT_FLOW_TEST_HOLD_MANAGED_PARENT_MS", "managed-parent-anchored");
    }
    const anchoredPath = fs.realpathSync(".");
    ensureChildPath(boundary, anchoredPath);
    if (!samePath(anchoredPath, target)) {
      throw new Error(`managed install ancestor moved outside boundary: ${target}`);
    }
    result = callback();
    const completedPath = fs.realpathSync(".");
    ensureChildPath(boundary, completedPath);
    if (!samePath(completedPath, target)) {
      throw new Error(`managed install ancestor moved during mutation: ${target}`);
    }
  } catch (error) {
    callbackError = error;
  }
  process.chdir(savedCwd);
  if (!sameDirectoryIdentity(savedIdentity, fs.statSync("."))) {
    throw new Error(`managed install working directory changed while restoring: ${savedCwd}`);
  }
  if (callbackError) throw callbackError;
  return result;
}

function ensureManagedDirectory(pathName, boundaryRoot = null, mode = null) {
  const boundary = managedWriteBoundary(pathName, boundaryRoot);
  withManagedDirectoryCwd(boundary, pathName, true, () => {
    if (mode !== null) fs.chmodSync(".", mode);
  });
}

function writeManagedRegularFile(pathName, content, boundaryRoot = null, mode = null) {
  const boundary = managedWriteBoundary(pathName, boundaryRoot);
  withManagedDirectoryCwd(boundary, path.dirname(pathName), true, () => {
    const noFollow = fs.constants.O_NOFOLLOW || 0;
    const filename = path.basename(pathName);
    const descriptor = fs.openSync(filename, fs.constants.O_WRONLY | fs.constants.O_CREAT | noFollow, 0o666);
    try {
      const stat = fs.fstatSync(descriptor);
      if (!stat.isFile() || stat.nlink !== 1) {
        throw new Error(`managed install target is not an owned regular file: ${pathName}`);
      }
      fs.ftruncateSync(descriptor, 0);
      fs.writeFileSync(descriptor, content, "utf8");
      if (mode !== null) fs.fchmodSync(descriptor, mode);
    } finally {
      fs.closeSync(descriptor);
    }
  });
}

function writeManagedFile(pathName, content, boundaryRoot = null) {
  withManagedInstallMutation(pathName, (managedPath) => {
    writeManagedRegularFile(managedPath, content, boundaryRoot);
  });
}

function writeManagedExecutableFile(pathName, content) {
  withManagedInstallMutation(pathName, (managedPath) => {
    writeManagedRegularFile(managedPath, content, null, 0o755);
  });
}

function writeManagedFileIfMissingOrSame(pathName, content, force = false, track = true, mode = null) {
  const write = (managedPath = pathName) => {
    const boundary = managedWriteBoundary(managedPath);
    assertNoSymlinkComponents(boundary, managedPath);
    if (fs.existsSync(managedPath)) {
      const current = fs.readFileSync(managedPath, "utf8");
      if (force) {
        writeManagedRegularFile(managedPath, content, boundary, mode);
        return true;
      }
      if (current !== content) {
        return false;
      }
      if (mode !== null && (fs.lstatSync(managedPath).mode & 0o777) !== mode) {
        return false;
      }
      return true;
    }
    writeManagedRegularFile(managedPath, content, boundary, mode);
    return true;
  };
  return track ? withManagedInstallMutation(pathName, write) : write();
}

function copyBundledDirIfMissingOrSame(
  src,
  dest,
  force = false,
  excludedRootDirs = new Set(),
  isRoot = true,
  pruneExtraneous = false,
  preservedExtraneousRootNames = new Set(),
  allowedRootDirs = null,
) {
  if (!fs.existsSync(src)) {
    return;
  }
  const copy = (managedDest = dest) => {
    const sourceDirectory = fs.lstatSync(src);
    if (!sourceDirectory.isDirectory() || sourceDirectory.isSymbolicLink()) {
      throw new Error(`managed install source is not a regular directory: ${src}`);
    }
    ensureManagedDirectory(managedDest, null);
    const sourceNames = new Set();
    for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
      sourceNames.add(entry.name);
      const srcPath = path.join(src, entry.name);
      const destPath = path.join(managedDest, entry.name);
      if (entry.isDirectory()) {
        if (isRoot && allowedRootDirs && !allowedRootDirs.has(entry.name)) {
          removeManagedDirIfSame(srcPath, destPath, force, false);
          continue;
        }
        if (isRoot && excludedRootDirs.has(entry.name)) {
          removeManagedDirIfSame(srcPath, destPath, force, false);
          continue;
        }
        copyBundledDirIfMissingOrSame(srcPath, destPath, force, excludedRootDirs, false, pruneExtraneous, preservedExtraneousRootNames, null);
        continue;
      }
      if (!entry.isFile()) {
        continue;
      }
      const content = fs.readFileSync(srcPath, "utf8");
      writeManagedFileIfMissingOrSame(
        destPath,
        content,
        force,
        false,
      );
    }
    if (force && pruneExtraneous) {
      for (const entry of fs.readdirSync(managedDest, { withFileTypes: true })) {
        if (!sourceNames.has(entry.name) && !(isRoot && preservedExtraneousRootNames.has(entry.name))) {
          fs.rmSync(path.join(managedDest, entry.name), { recursive: true, force: true });
        }
      }
    }
  };
  if (isRoot) withManagedInstallMutation(dest, copy);
  else copy();
}

function removeManagedDirIfSame(src, dest, force = false, track = true) {
  if (!fs.existsSync(dest)) {
    return;
  }
  if (!force && !dirContentsMatch(src, dest)) {
    return;
  }
  const remove = (managedDest = dest) => fs.rmSync(managedDest, { recursive: true, force: true });
  if (track) withManagedInstallMutation(dest, remove);
  else remove();
}

function removeStaleContextDocsScripts(agentFlowDir, force = false) {
  if (!force) {
    return;
  }
  withManagedInstallMutation(path.join(agentFlowDir, "scripts"), (managedScripts) => {
    for (const filename of ["check-context-docs.mjs", "check-context-docs.ts"]) {
      fs.rmSync(path.join(managedScripts, filename), { force: true });
    }
  });
}

function dirContentsMatch(src, dest) {
  if (!fs.existsSync(src) || !fs.existsSync(dest)) {
    return false;
  }
  const srcEntries = fs.readdirSync(src, { withFileTypes: true });
  const destEntries = fs.readdirSync(dest, { withFileTypes: true });
  if (srcEntries.length !== destEntries.length) {
    return false;
  }
  const destByName = new Map(destEntries.map((entry) => [entry.name, entry]));
  for (const srcEntry of srcEntries) {
    const destEntry = destByName.get(srcEntry.name);
    if (!destEntry || srcEntry.isDirectory() !== destEntry.isDirectory() || srcEntry.isFile() !== destEntry.isFile()) {
      return false;
    }
    const srcPath = path.join(src, srcEntry.name);
    const destPath = path.join(dest, srcEntry.name);
    if (srcEntry.isDirectory()) {
      if (!dirContentsMatch(srcPath, destPath)) {
        return false;
      }
      continue;
    }
    if (srcEntry.isFile() && fs.readFileSync(srcPath, "utf8") !== fs.readFileSync(destPath, "utf8")) {
      return false;
    }
  }
  return true;
}

function materializePlannedSkillEntry(
  transaction,
  name,
  kind,
  populateStage,
  hooks = {},
) {
  if (!transaction?.journal) {
    throw new Error("skill materialization requires an install transaction");
  }
  if (!["directory", "file"].includes(kind)) {
    throw new Error(`unsupported planned skill entry kind: ${name}`);
  }
  const stagingRoot = path.join(transaction.transactionRoot, "materialized-staging");
  ensureManagedDirectory(stagingRoot, transaction.transactionRoot);
  const operationId = crypto.randomBytes(12).toString("hex");
  const stageRelative = path.join("materialized-staging", `${name}-${operationId}`);
  const stage = path.join(transaction.transactionRoot, stageRelative);
  const displacedName = `.agent-flow-displaced-${name}-${operationId}`;
  const displaced = path.join(transaction.live, displacedName);
  const destination = path.join(transaction.live, name);
  const before = managedPathStateWithIdentity(destination);
  if (
    !["absent", kind].includes(before.kind)
    || !sameManagedPathStateWithIdentity(
      before,
      transaction.journal.planned_live_states?.[name],
    )
  ) {
    throw new Error(`installed skill destination changed before materialization: ${name}`);
  }
  if (kind === "directory") {
    fs.mkdirSync(stage);
  } else {
    fs.writeFileSync(stage, "", { flag: "wx" });
  }
  transaction.journal.pending_skill_materialization = {
    name,
    phase: "copying",
    before,
    stage: stageRelative,
    stage_filesystem_identity: hostFilesystemIdentity(stage),
    displaced: displacedName,
  };
  writeInstallJournal(transaction.journalPath, transaction.journal);
  hooks.beforePopulate?.();
  populateStage(stage, stagingRoot);
  const after = managedPathStateWithIdentity(stage);
  if (after.kind !== kind) {
    throw new Error(`materialized skill stage kind changed: ${name}`);
  }
  transaction.journal.pending_skill_materialization.after = after;
  transaction.journal.pending_skill_materialization.phase = "ready";
  writeInstallJournal(transaction.journalPath, transaction.journal);
  hooks.afterReady?.();
  withManagedDirectoryCwd(transaction.root, transaction.live, true, () => {
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(name),
        before,
      )
    ) {
      throw new Error(`installed skill destination changed before replacement: ${name}`);
    }
    if (managedPathStateWithIdentity(displacedName).kind !== "absent") {
      throw new Error(`installed skill displaced path already exists: ${name}`);
    }
    if (before.kind !== "absent") {
      renameManagedNoReplace(name, displacedName);
      if (
        !sameManagedPathStateWithIdentity(
          managedPathStateWithIdentity(displacedName),
          before,
        )
      ) {
        throw new Error(`installed skill destination changed during replacement: ${name}`);
      }
    }
    hooks.afterDisplace?.();
    renameManagedNoReplace(stage, name);
    hooks.afterRename?.();
    if (
      !sameManagedPathStateWithIdentity(
        after,
        managedPathStateWithIdentity(name),
      )
    ) {
      throw new Error(`installed skill destination changed after replacement: ${name}`);
    }
  });
  checkpointSkillTransactionPlannedState(transaction, name, after);
  hooks.afterCheckpoint?.();
  if (before.kind !== "absent") {
    quarantineMaterializationPath(
      transaction,
      displaced,
      before,
      `materialized displaced skill ${name}`,
    );
  }
  delete transaction.journal.pending_skill_materialization;
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function materializeGeneratedSkillEntry(transaction, name, content) {
  materializePlannedSkillEntry(
    transaction,
    name,
    "directory",
    (stage) => {
      fs.writeFileSync(path.join(stage, "SKILL.md"), content);
    },
    {
      afterRename: () => {
        if (process.env.AGENT_FLOW_TEST_PLANNED_SKILL_NAME === name) {
          holdInstallForTest(
            "AGENT_FLOW_TEST_HOLD_AFTER_PLANNED_SKILL_RENAME_MS",
            "planned-skill-renamed",
          );
        }
      },
    },
  );
}

function materializeBundledSkillEntries(transaction, allowedDirectoryNames) {
  const sourceRoot = path.join(KIT_ROOT, "skills");
  for (
    const entry of fs.readdirSync(sourceRoot, { withFileTypes: true })
      .sort((left, right) => compareCodePoints(left.name, right.name))
  ) {
    if (
      GENERATED_PROJECT_SKILL_NAMES.has(entry.name)
      || PROFILE_MANAGED_HOST_ONLY_SKILLS.has(entry.name)
    ) continue;
    if (entry.isDirectory() && !allowedDirectoryNames?.has(entry.name)) continue;
    if (!entry.isDirectory() && !entry.isFile()) continue;
    const source = path.join(sourceRoot, entry.name);
    const kind = entry.isDirectory() ? "directory" : "file";
    materializePlannedSkillEntry(
      transaction,
      entry.name,
      kind,
      (stage) => {
        if (kind === "directory") {
          fs.cpSync(source, stage, {
            recursive: true,
            dereference: false,
            errorOnExist: true,
            force: false,
          });
        } else {
          fs.copyFileSync(source, stage);
        }
        fs.chmodSync(stage, fs.lstatSync(source).mode & 0o777);
      },
      {
        afterRename: () => {
          if (process.env.AGENT_FLOW_TEST_PLANNED_SKILL_NAME === entry.name) {
            holdInstallForTest(
              "AGENT_FLOW_TEST_HOLD_AFTER_PLANNED_SKILL_RENAME_MS",
              "planned-skill-renamed",
            );
          }
        },
      },
    );
  }
}

function materializeResolvedSkillSources(agentFlowDir, sourcePlan, transaction = null) {
  if (!transaction?.journal) {
    throw new Error("skill materialization requires an install transaction");
  }
  const stagingRoot = path.join(transaction.transactionRoot, "materialized-staging");
  for (const entry of sourcePlan?.entries || []) {
    if (!["host-bootstrap", "shared"].includes(entry.source_kind)) continue;
    const absoluteDestination = path.join(agentFlowDir, "skills", entry.name);
    let sourcePath = entry.source_path;
    if (samePath(sourcePath, absoluteDestination)) {
      const backupSource = path.join(transaction.backup, entry.name);
      if (!fs.existsSync(backupSource)) continue;
      sourcePath = backupSource;
    }
    if (
      process.env.AGENT_FLOW_TEST_PREPOPULATE_MATERIALIZE_SKILL === entry.name
      && managedPathStateWithIdentity(absoluteDestination).kind === "absent"
    ) {
      materializePlannedSkillEntry(
        transaction,
        entry.name,
        "directory",
        (stage) => {
          fs.cpSync(sourcePath, stage, {
            recursive: true,
            dereference: false,
            errorOnExist: true,
            force: false,
          });
          if (
            hashSkillTree(stage, {
              authorityRoot: stagingRoot,
              skillName: path.basename(stage),
            }) !== entry.tree_hash
          ) {
            throw new Error(`skill source changed while copying: ${entry.name}`);
          }
        },
      );
    }
    try {
      materializePlannedSkillEntry(
        transaction,
        entry.name,
        "directory",
        (stage) => {
          fs.cpSync(sourcePath, stage, {
            recursive: true,
            dereference: false,
            errorOnExist: true,
            force: false,
          });
          if (
            hashSkillTree(stage, {
              authorityRoot: stagingRoot,
              skillName: path.basename(stage),
            }) !== entry.tree_hash
          ) {
            throw new Error(`skill source changed while copying: ${entry.name}`);
          }
        },
        {
          beforePopulate: () => holdInstallForTest(
            "AGENT_FLOW_TEST_HOLD_BEFORE_MATERIALIZE_SOURCE_COPY_MS",
            "materialize-source-copy-ready",
          ),
          afterReady: () => holdInstallForTest(
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_STAGE_HASH_MS",
            "materialize-stage-hashed",
          ),
          afterDisplace: () => holdInstallForTest(
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_DISPLACE_MS",
            "materialize-source-displaced",
          ),
          afterRename: () => {
            holdInstallForTest(
              "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_RENAME_MS",
              "materialize-source-renamed",
            );
            if (
              process.env.AGENT_FLOW_TEST_FAIL_AFTER_MATERIALIZE_RENAME
              === entry.name
            ) {
              throw new Error(`injected failure after materialization rename: ${entry.name}`);
            }
          },
          afterCheckpoint: () => holdInstallForTest(
            "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_CHECKPOINT_MS",
            "materialize-source-checkpointed",
          ),
        },
      );
    } catch (error) {
      if (error?.code === "ENOENT") {
        throw new Error(`skill source disappeared while copying: ${entry.name}`);
      }
      throw error;
    }
  }
}

function installProjectSkills(
  root,
  agentFlowDir,
  previousIndex,
  force = false,
  installSelection = null,
  sourcePlan = null,
  transaction = null,
) {
  const selected = selectProjectSkills(root, agentFlowDir, installSelection, sourcePlan);
  const links = [];
  for (const skill of selected.skills) {
    // bundled skill 중 host 디렉토리 link 대상은 BUNDLED_HOST_SKILL_NAMES뿐이다.
    // 나머지 bundled skill은 index에만 노출해 agent가 발견할 수 있게 한다.
    if (skill.source === "bundled" && !BUNDLED_HOST_SKILL_NAMES.has(skill.name)) {
      continue;
    }
    for (const host of skill.hosts) {
      links.push(linkProjectSkill(root, skill, host, previousIndex, force, transaction));
      if (host === "codex") {
        const canonicalRoot = hostSkillRoot(root, host);
        const compatibilityRoot = path.join(root, ".codex", "skills");
        if (!samePath(canonicalRoot, compatibilityRoot)) {
          links.push(linkProjectSkill(
            root,
            skill,
            host,
            previousIndex,
            force,
            transaction,
            compatibilityRoot,
          ));
        }
      }
    }
  }
  links.push(...removeStaleProjectSkillLinks(root, selected.skills, previousIndex, force, transaction));
  const index = { ...selected, links };
  index.managed_ownership = managedSkillOwnership(
    root,
    selected.skills,
    transaction?.journal?.planned_live_entries || [],
  );
  const activeHost = detectActiveHost(process.env);
  const catalogHosts = new Set(
    (sourcePlan?.entries || [])
      .map((entry) => entry.source_host)
      .filter((host) => PROJECT_SKILL_HOSTS.includes(host)),
  );
  if (process.env.AGENT_FLOW_AUTO_EXTERNAL_SKILLS === "1" && activeHost) {
    catalogHosts.add(activeHost);
  }
  index.catalog_hosts = [...catalogHosts].sort(compareCodePoints);
  index.catalog_active_host = index.catalog_hosts.includes(activeHost)
    ? activeHost
    : index.catalog_hosts[0] ?? null;
  index.catalog_fingerprint = skillCatalogFingerprint(root, HOME, index.catalog_hosts, process.env);
  if (!transaction?.liveIdentity) {
    throw new Error("pinned skill index directory identity is missing");
  }
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_INDEX_WRITE_MS",
    "skill-index-write-ready",
  );
  transaction.liveIndexIdentity = writePinnedDirectoryFile(
    transaction.live,
    transaction.liveIdentity,
    "index.json",
    `${JSON.stringify(index, null, 2)}\n`,
    transaction.liveIndexIdentity,
    "skill index",
  );
  return index;
}

function refreshSkillCatalogAtBoundary(root) {
  if (!root) return;
  const leaderRoot = resolveManagedWorktreeRoot(root) || root;
  const indexPath = path.join(leaderRoot, ".agent-flow", "skills", "index.json");
  const index = readJsonIfExists(indexPath);
  if (!index?.catalog_fingerprint) return;
  const catalogHosts = Array.isArray(index.catalog_hosts)
    ? index.catalog_hosts.filter((host) => PROJECT_SKILL_HOSTS.includes(host))
    : [detectActiveHost(process.env)].filter(Boolean);
  const current = skillCatalogFingerprint(leaderRoot, HOME, catalogHosts, process.env);
  if (current === index.catalog_fingerprint && installedSkillLinksMatchIndex(leaderRoot, index)) return;
  const invocationWorktree = resolveManagedWorktreeRoot(process.cwd());
  if (invocationWorktree && samePath(invocationWorktree, leaderRoot)) {
    throw new Error(
      "blocked: skill catalog drift must be refreshed from the leader checkout; "
      + `refresh without advancing a run: cd ${leaderRoot} && agent-flow status`,
    );
  }
  const previousHost = process.env.AGENT_FLOW_HOST;
  if (PROJECT_SKILL_HOSTS.includes(index.catalog_active_host)) {
    process.env.AGENT_FLOW_HOST = index.catalog_active_host;
  }
  let status;
  try {
    status = runInstallSandbox(leaderRoot);
  } finally {
    if (previousHost === undefined) delete process.env.AGENT_FLOW_HOST;
    else process.env.AGENT_FLOW_HOST = previousHost;
  }
  if (status !== 0) throw new Error(`skill catalog refresh failed with exit ${status}`);
}

function installedSkillLinksMatchIndex(root, index) {
  try {
    for (const link of index?.links || []) {
      if (!["linked", "copied"].includes(link?.status)) continue;
      const target = path.resolve(root, String(link.path || ""));
      ensureChildPath(root, target);
      const metadata = lstatIfExists(target);
      if (!metadata || !sameHostFilesystemIdentity(target, link.filesystem_identity)) return false;
      if (link.status === "linked") {
        if (
          !metadata.isSymbolicLink()
          || typeof link.target !== "string"
          || fs.readlinkSync(target) !== link.target
          || !fs.existsSync(path.join(target, "SKILL.md"))
        ) return false;
        continue;
      }
      if (
        metadata.isSymbolicLink()
        || !metadata.isDirectory()
        || typeof link.tree_hash !== "string"
        || typeof link.tree_integrity !== "string"
        || hashSkillTree(target) !== link.tree_hash
        || treeIntegrity(target) !== link.tree_integrity
      ) return false;
    }
    return true;
  } catch {
    return false;
  }
}

function acquireProjectInstallLock(root, agentFlowDir) {
  const lockPath = path.join(agentFlowDir, "install.lock");
  let recoveryToken = null;
  if (fs.existsSync(lockPath)) {
    const expectedLock = fs.lstatSync(lockPath);
    if (!expectedLock.isDirectory() || expectedLock.isSymbolicLink()) {
      throw new Error(`project install lock is unsafe: ${lockPath}`);
    }
    const ownerPath = path.join(lockPath, "owner.json");
    const owner = readRegularJsonNoFollow(ownerPath, agentFlowDir);
    const validOwner = owner?.version === 1
      && owner.root === fs.realpathSync(root)
      && Number.isInteger(owner.pid)
      && typeof owner.token === "string";
    if (!validOwner || processIsAlive(owner.pid)) {
      throw new Error(`project install lock is held: ${lockPath}`);
    }
    recoveryToken = owner.token;
    holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_STALE_LOCK_AUTH_MS", "stale-lock-authenticated");
    const quarantine = path.join(agentFlowDir, `.agent-flow-stale-lock-${crypto.randomBytes(12).toString("hex")}`);
    renameManagedNoReplace(lockPath, quarantine);
    const moved = fs.lstatSync(quarantine);
    const movedOwner = readLockOwnerIfSafe(
      path.join(quarantine, "owner.json"),
      agentFlowDir,
    );
    if (
      !sameDirectoryIdentity(expectedLock, moved)
      || movedOwner?.token !== owner.token
      || movedOwner?.pid !== owner.pid
      || lstatIfExists(lockPath)
    ) {
      if (!fs.existsSync(lockPath)) renameManagedNoReplace(quarantine, lockPath);
      throw new Error(`project install lock changed during stale recovery: ${lockPath}`);
    }
    fs.rmSync(quarantine, { recursive: true, force: true });
  }
  fs.mkdirSync(lockPath);
  const identity = fs.lstatSync(lockPath);
  const lock = {
    version: 1,
    root: fs.realpathSync(root),
    pid: process.pid,
    token: recoveryToken || crypto.randomBytes(24).toString("hex"),
    acquired_at: new Date().toISOString(),
  };
  fs.writeFileSync(path.join(lockPath, "owner.json"), `${JSON.stringify(lock, null, 2)}\n`, { flag: "wx" });
  return {
    ...lock,
    path: lockPath,
    recovery_token: recoveryToken,
    device: String(identity.dev),
    inode: String(identity.ino),
  };
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== "ESRCH";
  }
}

function readLockOwnerIfSafe(pathName, authorityRoot) {
  try {
    return readRegularJsonNoFollow(pathName, authorityRoot);
  } catch {
    return null;
  }
}

function holdInstallForTest(name, marker) {
  const milliseconds = Number.parseInt(process.env[name] || "0", 10);
  if (!Number.isInteger(milliseconds) || milliseconds <= 0 || milliseconds > 10_000) return;
  process.stderr.write(`agent-flow:test-${marker}\n`);
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}

function releaseProjectInstallLock(lock) {
  if (!lock || !fs.existsSync(lock.path)) return;
  const current = fs.lstatSync(lock.path);
  const owner = readJsonIfExists(path.join(lock.path, "owner.json"));
  if (
    !current.isDirectory()
    || String(current.dev) !== lock.device
    || String(current.ino) !== lock.inode
    || owner?.token !== lock.token
    || owner?.pid !== process.pid
  ) {
    throw new Error(`project install lock ownership changed: ${lock.path}`);
  }
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_BEFORE_INSTALL_LOCK_RELEASE_MS", "install-lock-release-ready");
  const quarantine = path.join(path.dirname(lock.path), `.agent-flow-release-lock-${crypto.randomBytes(12).toString("hex")}`);
  fs.renameSync(lock.path, quarantine);
  const moved = fs.lstatSync(quarantine);
  const movedOwner = readLockOwnerIfSafe(
    path.join(quarantine, "owner.json"),
    path.dirname(lock.path),
  );
  if (
    String(moved.dev) !== lock.device
    || String(moved.ino) !== lock.inode
    || movedOwner?.token !== lock.token
    || movedOwner?.pid !== process.pid
  ) {
    if (!fs.existsSync(lock.path)) renameManagedNoReplace(quarantine, lock.path);
    throw new Error(`project install lock changed during release: ${lock.path}`);
  }
  fs.rmSync(quarantine, { recursive: true, force: true });
}

function readAuthenticatedSkillIndex(agentFlowDir, existingKit = null) {
  const indexPath = path.join(agentFlowDir, "skills", "index.json");
  try {
    fs.lstatSync(indexPath);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
  const snapshot = readRegularFileSnapshotNoFollow(
    indexPath,
    agentFlowDir,
    "previous skill index",
  );
  const { bytes } = snapshot;
  let payload;
  try {
    payload = JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new Error(`invalid previous skill index JSON: ${indexPath}`);
  }
  if (!Array.isArray(payload?.skills) || !Array.isArray(payload?.links)) {
    throw new Error(`unsupported previous skill index: ${indexPath}`);
  }
  if (payload.version === undefined) payload = { version: 1, selection: {}, ...payload };
  if (![1, 2].includes(payload.version)) throw new Error(`unsupported previous skill index: ${indexPath}`);
  const hasIndexCommitment = existingKit?.skill_index_hash_version === 1
    && typeof existingKit?.skill_index_hash === "string";
  if (hasIndexCommitment) {
    const indexHash = crypto.createHash("sha256").update(bytes).digest("hex");
    if (indexHash !== existingKit.skill_index_hash) {
      throw new Error("previous skill index does not match kit commitment");
    }
    if (
      existingKit.skill_plan_hash_version !== 2
      || computeSkillPlanHash(payload, path.dirname(agentFlowDir), false) !== existingKit.skill_plan_hash
    ) {
      throw new Error("previous skill plan does not match kit commitment");
    }
    if (
      ![1, SKILL_LINKS_COMMITMENT_VERSION].includes(existingKit.skill_links_commitment_version)
      || skillLinksCommitment(
        existingKit.skill_plan_hash,
        payload.links,
        existingKit.skill_links_commitment_version,
      )
        !== existingKit.skill_links_commitment
    ) {
      throw new Error("previous skill links do not match kit commitment");
    }
  } else {
    payload = { ...payload, links: [] };
  }
  return {
    payload,
    bytes,
    hash: crypto.createHash("sha256").update(bytes).digest("hex"),
    identity: snapshot.metadata,
  };
}

function beginSkillInstallTransaction(root, agentFlowDir, previousIndexRecord, lockToken) {
  const transactionRoot = path.join(agentFlowDir, "install-transaction");
  if (fs.existsSync(transactionRoot)) throw new Error(`open skill install transaction: ${transactionRoot}`);
  const live = path.join(agentFlowDir, "skills");
  if (fs.existsSync(live) && !previousIndexRecord) {
    throw new Error("existing skills directory has no authenticated index");
  }
  fs.mkdirSync(transactionRoot);
  const backup = path.join(transactionRoot, "skills-backup");
  const marker = path.join(live, ".agent-flow-transaction-owner");
  const journalPath = path.join(transactionRoot, "journal.json");
  const transactionRootIdentity = hostFilesystemIdentity(transactionRoot);
  const transaction = {
    root,
    transactionRoot,
    live,
    backup,
    marker,
    journalPath,
    token: crypto.randomBytes(24).toString("hex"),
    previous: previousIndexRecord,
    transactionRootIdentity,
    liveIdentity: null,
    liveIndexIdentity: null,
  };
  const journal = {
    version: 9,
    root: fs.realpathSync(root),
    token: transaction.token,
    lock_token: lockToken,
    stage: "prepared",
    previous_index_hash: previousIndexRecord?.hash || null,
    previous_index_bytes: previousIndexRecord?.bytes.toString("base64") || null,
    had_live_skills: fs.existsSync(live),
    host_mutations: [],
    managed_mutations: [],
  };
  bindInstallJournalAuthority(journal, transactionRootIdentity);
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_BEFORE_INSTALL_JOURNAL_WRITE_MS",
    "install-journal-write-ready",
  );
  writeInstallJournal(journalPath, journal);
  if (journal.had_live_skills) {
    if (!previousIndexRecord) {
      throw new Error("existing skills directory has no authenticated index");
    }
    journal.stage = "moving-skills";
    journal.backup_state = managedPathStateWithIdentity(live);
    writeInstallJournal(journalPath, journal);
    fs.renameSync(live, backup);
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(backup),
        journal.backup_state,
      )
    ) {
      throw new Error("skill backup changed while moving");
    }
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_RENAME === "1") process.exit(88);
    journal.stage = "skills-moved";
    writeInstallJournal(journalPath, journal);
    if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILLS_MOVE === "1") process.exit(86);
    const backupIndex = readRegularFileSnapshotNoFollow(
      path.join(backup, "index.json"),
      agentFlowDir,
      "backup skill index",
    ).bytes;
    const backupHash = crypto.createHash("sha256").update(backupIndex).digest("hex");
    if (backupHash !== previousIndexRecord.hash || !backupIndex.equals(previousIndexRecord.bytes)) {
      throw new Error("previous skill index changed after authentication; backup was not adopted");
    }
  }
  fs.mkdirSync(live, { recursive: true });
  fs.writeFileSync(marker, `${transaction.token}\n`, { flag: "wx" });
  transaction.liveIdentity = hostFilesystemIdentity(live);
  journal.initial_live_state = managedPathStateWithIdentity(live);
  journal.planned_live_entries = [];
  journal.planned_live_states = Object.fromEntries(
    [".agent-flow-transaction-owner", "index.json"].map((name) => [
      name,
      managedPathStateWithIdentity(path.join(live, name)),
    ]),
  );
  journal.stage = "live-created";
  writeInstallJournal(journalPath, journal);
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_SKILL_LIVE_CREATE === "1") process.exit(85);
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_INSTALL_LOCK_MS", "install-lock-held");
  transaction.journal = journal;
  snapshotManagedInstallPaths(transaction);
  activeManagedInstallTransaction = transaction;
  return transaction;
}

const INSTALL_JOURNAL_DIRECTORY_IDENTITY = Symbol("install journal directory identity");
const INSTALL_JOURNAL_FILE_IDENTITY = Symbol("install journal file identity");

function bindInstallJournalAuthority(journal, directoryIdentity, fileIdentity = null) {
  Object.defineProperty(journal, INSTALL_JOURNAL_DIRECTORY_IDENTITY, {
    configurable: true,
    value: directoryIdentity,
    writable: true,
  });
  Object.defineProperty(journal, INSTALL_JOURNAL_FILE_IDENTITY, {
    configurable: true,
    value: fileIdentity,
    writable: true,
  });
}

function pinnedSkillLiveEntryNames(journal) {
  return [...new Set([
    ".agent-flow-transaction-owner",
    "index.json",
    ...(journal?.planned_live_entries || []),
  ])].sort(compareCodePoints);
}
function updateSkillTransactionPlannedStates(transaction, names = null) {
  const expectedNames = new Set(pinnedSkillLiveEntryNames(transaction.journal));
  const selectedNames = names === null ? [...expectedNames] : [...new Set(names)];
  if (selectedNames.length === 0) return;
  const states = {
    ...(transaction.journal.planned_live_states || {}),
  };
  for (const name of selectedNames) {
    if (!expectedNames.has(name)) {
      throw new Error(`invalid planned skill live entry: ${name}`);
    }
    states[name] = managedPathStateWithIdentity(path.join(transaction.live, name));
  }
  transaction.journal.planned_live_states = Object.fromEntries(
    [...expectedNames]
      .sort(compareCodePoints)
      .map((name) => [name, states[name]]),
  );
  writeInstallJournal(transaction.journalPath, transaction.journal);
}
function checkpointSkillTransactionPlannedState(transaction, name, state) {
  if (!transaction?.journal) return;
  const expectedNames = new Set(pinnedSkillLiveEntryNames(transaction.journal));
  if (!expectedNames.has(name)) {
    throw new Error(`invalid planned skill live entry: ${name}`);
  }
  if (
    !sameManagedPathStateWithIdentity(
      state,
      managedPathStateWithIdentity(path.join(transaction.live, name)),
    )
  ) {
    throw new Error(`planned skill live entry changed before checkpoint: ${name}`);
  }
  const pending = transaction.journal.pending_skill_materialization;
  if (
    !pending
    || pending.name !== name
    || pending.phase !== "ready"
    || !sameManagedPathStateWithIdentity(pending.after, state)
  ) {
    throw new Error(`invalid pending skill materialization checkpoint: ${name}`);
  }
  pending.phase = "completed";
  const states = {
    ...(transaction.journal.planned_live_states || {}),
    [name]: state,
  };
  transaction.journal.planned_live_states = Object.fromEntries(
    [...expectedNames]
      .sort(compareCodePoints)
      .map((entry) => [entry, states[entry]]),
  );
  writeInstallJournal(transaction.journalPath, transaction.journal);
}
function quarantineMaterializationPath(transaction, pathName, expected, label) {
  const pending = transaction.journal?.pending_skill_materialization;
  if (!pending) {
    throw new Error(`${label} cleanup is not journaled`);
  }
  const target = path.join(transaction.live, pending.name);
  const displaced = path.join(transaction.live, pending.displaced);
  const cleanupSource = samePath(pathName, target)
    ? "target"
    : samePath(pathName, displaced)
      ? "displaced"
      : null;
  if (!cleanupSource) {
    throw new Error(`${label} cleanup path is not registered`);
  }
  const cleanupRoot = path.join(transaction.transactionRoot, "materialized-cleanup");
  ensureManagedDirectory(cleanupRoot, transaction.transactionRoot);
  if (pending.phase !== "cleaning") {
    pending.phase = "cleaning";
    pending.cleanup_source = cleanupSource;
    pending.cleanup = path.join(
      "materialized-cleanup",
      `materialized-${crypto.randomBytes(12).toString("hex")}`,
    );
    writeInstallJournal(transaction.journalPath, transaction.journal);
  } else if (pending.cleanup_source !== cleanupSource) {
    throw new Error(`${label} cleanup phase changed`);
  }
  const quarantine = path.resolve(transaction.transactionRoot, pending.cleanup);
  ensureChildPath(transaction.transactionRoot, quarantine);
  assertNoSymlinkComponents(transaction.transactionRoot, path.dirname(quarantine));
  withManagedDirectoryCwd(transaction.root, path.dirname(pathName), false, () => {
    const name = path.basename(pathName);
    const current = managedPathStateWithIdentity(name);
    const quarantined = managedPathStateWithIdentity(quarantine);
    if (
      sameManagedPathStateWithIdentity(current, expected)
      && quarantined.kind === "absent"
    ) {
      holdInstallForTest(
        "AGENT_FLOW_TEST_HOLD_BEFORE_MATERIALIZE_CLEANUP_RENAME_MS",
        "materialize-cleanup-ready",
      );
      renameManagedNoReplace(name, quarantine);
      if (
        !sameManagedPathStateWithIdentity(
          managedPathStateWithIdentity(quarantine),
          expected,
        )
        || managedPathStateWithIdentity(name).kind !== "absent"
      ) {
        if (
          managedPathStateWithIdentity(name).kind === "absent"
          && managedPathStateWithIdentity(quarantine).kind !== "absent"
        ) {
          renameManagedNoReplace(quarantine, name);
        }
        throw new Error(`${label} changed during cleanup`);
      }
      holdInstallForTest(
        "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_CLEANUP_RENAME_MS",
        "materialize-cleanup-renamed",
      );
      return;
    }
    if (
      current.kind === "absent"
      && sameManagedPathStateWithIdentity(quarantined, expected)
    ) {
      return;
    }
    throw new Error(`${label} changed before cleanup`);
  });
}

function restoreMaterializationDisplaced(transaction, pending, displaced) {
  const target = path.join(transaction.live, pending.name);
  withManagedDirectoryCwd(transaction.root, transaction.live, false, () => {
    if (managedPathStateWithIdentity(pending.name).kind !== "absent") {
      throw new Error(`materialized skill target changed before restore: ${pending.name}`);
    }
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(path.basename(displaced)),
        pending.before,
      )
    ) {
      throw new Error(`materialized skill displaced path changed: ${pending.name}`);
    }
    renameManagedNoReplace(path.basename(displaced), pending.name);
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(pending.name),
        pending.before,
      )
    ) {
      throw new Error(`materialized skill restore identity changed: ${pending.name}`);
    }
  });
  if (!sameManagedPathStateWithIdentity(managedPathStateWithIdentity(target), pending.before)) {
    throw new Error(`materialized skill restore changed: ${pending.name}`);
  }
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_AFTER_MATERIALIZE_RESTORE_MS",
    "materialize-displaced-restored",
  );
}

function reconcilePendingSkillMaterialization(transaction) {
  const pending = transaction.journal?.pending_skill_materialization;
  if (!pending) return;
  const stage = path.resolve(transaction.transactionRoot, pending.stage);
  ensureChildPath(transaction.transactionRoot, stage);
  assertNoSymlinkComponents(transaction.transactionRoot, path.dirname(stage));
  const displaced = path.join(transaction.live, pending.displaced);
  ensureChildPath(transaction.live, displaced);
  assertNoSymlinkComponents(transaction.root, path.dirname(displaced));
  const target = path.join(transaction.live, pending.name);
  const targetState = managedPathStateWithIdentity(target);
  const displacedState = managedPathStateWithIdentity(displaced);
  const stageState = managedPathStateWithIdentity(stage);
  if (pending.phase === "copying") {
    const validStage = ["directory", "file"].includes(stageState.kind)
      && sameHostFilesystemIdentity(stage, pending.stage_filesystem_identity);
    if (
      !validStage
      || !sameManagedPathStateWithIdentity(targetState, pending.before)
      || displacedState.kind !== "absent"
    ) {
      throw new Error(`materialized skill copy state changed: ${pending.name}`);
    }
  } else if (pending.phase === "ready") {
    const notStarted = sameManagedPathStateWithIdentity(targetState, pending.before)
      && displacedState.kind === "absent"
      && sameManagedPathStateWithIdentity(stageState, pending.after);
    const interrupted = pending.before.kind !== "absent"
      && targetState.kind === "absent"
      && sameManagedPathStateWithIdentity(displacedState, pending.before)
      && sameManagedPathStateWithIdentity(stageState, pending.after);
    const completed = sameManagedPathStateWithIdentity(targetState, pending.after)
      && sameManagedPathStateWithIdentity(displacedState, pending.before)
      && stageState.kind === "absent";
    if (!notStarted && !interrupted && !completed) {
      throw new Error(`materialized skill swap state changed: ${pending.name}`);
    }
    if (completed) {
      quarantineMaterializationPath(
        transaction,
        target,
        pending.after,
        `materialized skill ${pending.name}`,
      );
      if (pending.before.kind !== "absent") {
        restoreMaterializationDisplaced(transaction, pending, displaced);
      }
    } else if (interrupted) {
      restoreMaterializationDisplaced(transaction, pending, displaced);
    }
  } else if (pending.phase === "completed") {
    if (
      !sameManagedPathStateWithIdentity(targetState, pending.after)
      || stageState.kind !== "absent"
    ) {
      throw new Error(`completed materialized skill changed: ${pending.name}`);
    }
    if (displacedState.kind !== "absent") {
      if (!sameManagedPathStateWithIdentity(displacedState, pending.before)) {
        throw new Error(`completed materialized displaced skill changed: ${pending.name}`);
      }
      quarantineMaterializationPath(
        transaction,
        displaced,
        pending.before,
        `materialized displaced skill ${pending.name}`,
      );
    }
  } else if (pending.phase === "cleaning") {
    if (pending.cleanup_source === "target") {
      const cleanup = path.resolve(transaction.transactionRoot, pending.cleanup);
      ensureChildPath(transaction.transactionRoot, cleanup);
      assertNoSymlinkComponents(transaction.transactionRoot, path.dirname(cleanup));
      const alreadyRestored = (
        stageState.kind === "absent"
        && sameManagedPathStateWithIdentity(
          managedPathStateWithIdentity(cleanup),
          pending.after,
        )
        && displacedState.kind === "absent"
        && sameManagedPathStateWithIdentity(
          targetState,
          pending.before,
        )
      );
      if (!alreadyRestored) {
        quarantineMaterializationPath(
          transaction,
          target,
          pending.after,
          `materialized skill ${pending.name}`,
        );
        if (pending.before.kind !== "absent") {
          restoreMaterializationDisplaced(transaction, pending, displaced);
        }
      }
    } else if (pending.cleanup_source === "displaced") {
      if (
        !sameManagedPathStateWithIdentity(
          managedPathStateWithIdentity(target),
          pending.after,
        )
        || managedPathStateWithIdentity(stage).kind !== "absent"
      ) {
        throw new Error(`completed materialized skill changed: ${pending.name}`);
      }
      quarantineMaterializationPath(
        transaction,
        displaced,
        pending.before,
        `materialized displaced skill ${pending.name}`,
      );
    } else {
      throw new Error(`invalid materialized skill cleanup: ${pending.name}`);
    }
  } else {
    throw new Error(`invalid materialized skill phase: ${pending.name}`);
  }
  delete transaction.journal.pending_skill_materialization;
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function verifyPinnedSkillLiveStates(live, journal) {
  const expectedNames = pinnedSkillLiveEntryNames(journal);
  const states = journal?.planned_live_states;
  if (!states || typeof states !== "object" || Array.isArray(states)) {
    throw new Error("planned skill live entries are unauthenticated");
  }
  for (const name of expectedNames) {
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(path.join(live, name)),
        states[name],
      )
    ) {
      throw new Error(`planned skill live entry changed outside transaction: ${name}`);
    }
  }
}
function setSkillTransactionPlannedEntries(transaction, entries) {
  transaction.journal.planned_live_entries = [...entries]
    .filter((entry) => isPortableSkillName(entry))
    .sort(compareCodePoints);
  updateSkillTransactionPlannedStates(transaction);
}

function writeInstallJournal(journalPath, journal) {
  const directoryIdentity = journal?.[INSTALL_JOURNAL_DIRECTORY_IDENTITY];
  if (!directoryIdentity) {
    throw new Error("pinned install journal directory identity is missing");
  }
  journal[INSTALL_JOURNAL_FILE_IDENTITY] = writePinnedDirectoryFile(
    path.dirname(journalPath),
    directoryIdentity,
    path.basename(journalPath),
    `${JSON.stringify(journal, null, 2)}\n`,
    journal[INSTALL_JOURNAL_FILE_IDENTITY],
    "install journal",
  );
}

function hostPathState(pathName) {
  const stat = lstatIfExists(pathName);
  if (!stat) return { kind: "absent" };
  const filesystemIdentity = {
    device: String(stat.dev),
    inode: String(stat.ino),
    links: String(stat.nlink),
    mode: stat.mode & 0o777,
  };
  if (stat.isSymbolicLink()) {
    return { kind: "symlink", target: fs.readlinkSync(pathName), filesystem_identity: filesystemIdentity };
  }
  if (stat.isDirectory()) {
    return { kind: "directory", tree_hash: hashDirectoryTree(pathName), filesystem_identity: filesystemIdentity };
  }
  if (stat.isFile()) {
    return {
      kind: "file",
      file_hash: crypto.createHash("sha256").update(fs.readFileSync(pathName)).digest("hex"),
      filesystem_identity: filesystemIdentity,
    };
  }
  throw new Error(`unsupported host skill path kind: ${pathName}`);
}

function hostFilesystemIdentity(pathName) {
  const stat = fs.lstatSync(pathName);
  return {
    device: String(stat.dev),
    inode: String(stat.ino),
    links: String(stat.nlink),
    mode: stat.mode & 0o777,
  };
}

function validHostFilesystemIdentity(identity) {
  return Boolean(
    identity
    && typeof identity === "object"
    && !Array.isArray(identity)
    && /^[0-9]+$/.test(String(identity.device || ""))
    && /^[0-9]+$/.test(String(identity.inode || ""))
    && /^[0-9]+$/.test(String(identity.links || ""))
    && Number.isInteger(identity.mode),
  );
}

function sameHostFilesystemIdentity(pathName, expected) {
  if (!validHostFilesystemIdentity(expected)) return false;
  try {
    return JSON.stringify(hostFilesystemIdentity(pathName)) === JSON.stringify(expected);
  } catch {
    return false;
  }
}

function sameHostPathState(left, right, requireIdentity = false) {
  if (!left || !right || left.kind !== right.kind) return false;
  if (left.kind === "absent") return true;
  let contentMatches = left.kind === "absent";
  if (left.kind === "symlink") contentMatches = left.target === right.target;
  if (left.kind === "directory") contentMatches = left.tree_hash === right.tree_hash;
  if (left.kind === "file") contentMatches = left.file_hash === right.file_hash;
  if (!contentMatches || !requireIdentity) return contentMatches;
  if (!right.filesystem_identity) return false;
  return JSON.stringify(left.filesystem_identity) === JSON.stringify(right.filesystem_identity);
}

function swapHostPath(root, target, staged, incoming, displaced, expectedBefore, expectedAfter) {
  withManagedDirectoryCwd(root, path.dirname(target), expectedBefore.kind === "absent", () => {
    const targetName = path.basename(target);
    const incomingName = path.basename(incoming);
    const displacedName = path.basename(displaced);
    if (lstatIfExists(incomingName) || lstatIfExists(displacedName)) {
      throw new Error(`host skill swap path already exists: ${target}`);
    }
    const holdSuffix = process.env.AGENT_FLOW_TEST_HOLD_HOST_TARGET_SUFFIX;
    if (!holdSuffix || target.endsWith(holdSuffix)) {
      holdInstallForTest("AGENT_FLOW_TEST_HOLD_BEFORE_HOST_SWAP_MS", "host-swap-ready");
    }
    if (expectedAfter.kind !== "absent") {
      if (!sameHostPathState(hostPathState(staged), expectedAfter, true)) {
        throw new Error(`host skill staging integrity mismatch: ${target}`);
      }
      renameManagedNoReplace(staged, incomingName);
      if (!sameHostPathState(hostPathState(incomingName), expectedAfter, true)) {
        throw new Error(`host skill incoming integrity mismatch: ${target}`);
      }
    }
    if (!sameHostPathState(hostPathState(targetName), expectedBefore, true)) {
      throw new Error(`host skill path changed outside install transaction: ${target}`);
    }
    if (expectedBefore.kind !== "absent") {
      fs.renameSync(targetName, displacedName);
      if (!sameHostPathState(hostPathState(displacedName), expectedBefore, true)) {
        if (!lstatIfExists(targetName)) renameManagedNoReplace(displacedName, targetName);
        throw new Error(`host skill path changed during swap: ${target}`);
      }
    }
    holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_HOST_DISPLACE_MS", "host-target-displaced");
    if (expectedAfter.kind !== "absent") renameManagedNoReplace(incomingName, targetName);
    if (!sameHostPathState(hostPathState(targetName), expectedAfter, true)) {
      throw new Error(`host skill staged mutation mismatch: ${target}`);
    }
  });
}

function removeHostTemporaryPath(root, transactionRoot, pathName, expected) {
  const cleanupRoot = path.join(transactionRoot, "cleanup");
  ensureManagedDirectory(cleanupRoot, transactionRoot);
  withManagedDirectoryCwd(root, path.dirname(pathName), false, () => {
    const name = path.basename(pathName);
    const current = hostPathState(name);
    if (current.kind === "absent") return;
    if (!sameHostPathState(current, expected, true)) {
      throw new Error(`host skill temporary path changed: ${pathName}`);
    }
    const quarantine = path.join(cleanupRoot, `host-${crypto.randomBytes(12).toString("hex")}`);
    renameManagedNoReplace(name, quarantine);
    if (!sameHostPathState(hostPathState(quarantine), expected, true)) {
      if (hostPathState(name).kind === "absent") renameManagedNoReplace(quarantine, name);
      throw new Error(`host skill temporary path changed during cleanup: ${pathName}`);
    }
  });
}

function withHostPathMutation(transaction, target, allowedAfter, callback) {
  if (!transaction) return callback(target);
  ensureChildPath(transaction.root, target);
  const relative = path.relative(transaction.root, target);
  const before = hostPathState(target);
  const operationId = transaction.journal.host_mutations.length;
  if (["directory", "file"].includes(before.kind)) {
    const backupRelative = path.join("host-backups", String(operationId));
    const backupPath = path.join(transaction.transactionRoot, backupRelative);
    fs.mkdirSync(path.dirname(backupPath), { recursive: true });
    fs.cpSync(target, backupPath, {
      recursive: true,
      dereference: false,
      verbatimSymlinks: true,
      errorOnExist: true,
      force: false,
    });
    if (!sameHostPathState(before, hostPathState(backupPath))) {
      throw new Error(`host skill backup integrity mismatch: ${relative}`);
    }
    before.backup = backupRelative;
  }
  const operation = {
    path: relative,
    before,
    allowed_after: allowedAfter,
    after: null,
    original: null,
    pending: null,
  };
  transaction.journal.host_mutations.push(operation);
  writeInstallJournal(transaction.journalPath, transaction.journal);
  const stagingRoot = path.join(transaction.transactionRoot, "host-staging", String(operationId));
  const stagedTarget = path.join(stagingRoot, "next");
  fs.mkdirSync(stagingRoot, { recursive: true });
  if (before.kind === "symlink") {
    fs.symlinkSync(before.target, stagedTarget, "dir");
  } else if (before.kind !== "absent") {
    fs.cpSync(target, stagedTarget, {
      recursive: true,
      dereference: false,
      verbatimSymlinks: true,
      errorOnExist: true,
      force: false,
    });
    if (!sameHostPathState(before, hostPathState(stagedTarget))) {
      throw new Error(`host skill staging integrity mismatch: ${relative}`);
    }
  }
  let result;
  let callbackError = null;
  try {
    result = callback(stagedTarget);
  } catch (error) {
    callbackError = error;
  }
  if (callbackError) throw callbackError;
  const after = hostPathState(stagedTarget);
  if (!allowedAfter.some((state) => sameHostPathState(state, after))) {
    throw new Error(`unexpected host skill mutation result: ${relative}`);
  }
  const prefix = `.agent-flow-host-swap-${transaction.token}-${operationId}`;
  const incoming = path.join(path.dirname(target), `${prefix}-next`);
  const displaced = path.join(path.dirname(target), `${prefix}-previous`);
  operation.pending = {
    after,
    staging: path.relative(transaction.transactionRoot, stagedTarget),
    incoming: path.relative(transaction.root, incoming),
    displaced: path.relative(transaction.root, displaced),
  };
  writeInstallJournal(transaction.journalPath, transaction.journal);
  swapHostPath(transaction.root, target, stagedTarget, incoming, displaced, before, after);
  const holdSuffix = process.env.AGENT_FLOW_TEST_HOLD_HOST_TARGET_SUFFIX;
  if (!holdSuffix || target.endsWith(holdSuffix)) {
    holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_HOST_SWAP_MS", "host-swap-complete");
  }
  operation.pending.completed = true;
  writeInstallJournal(transaction.journalPath, transaction.journal);
  removeHostTemporaryPath(transaction.root, transaction.transactionRoot, incoming, after);
  if (
    before.kind !== "absent"
    && !sameHostPathState(hostPathState(displaced), before, true)
  ) {
    throw new Error(`host skill original path changed after swap: ${relative}`);
  }
  const committed = hostPathState(target);
  if (!sameHostPathState(committed, after, true)) {
    throw new Error(`host skill path changed after swap: ${relative}`);
  }
  operation.after = after;
  operation.original = before.kind === "absent"
    ? null
    : path.relative(transaction.root, displaced);
  operation.pending = null;
  writeInstallJournal(transaction.journalPath, transaction.journal);
  return result;
}

function hostMutationTarget(root, relative) {
  if (typeof relative !== "string" || path.isAbsolute(relative)) {
    throw new Error("invalid host skill mutation path");
  }
  const target = path.resolve(root, relative);
  ensureChildPath(root, target);
  return target;
}

function restoreHostPathState(root, target, state, transactionRoot, originalPath = null) {
  const token = crypto.randomBytes(24).toString("hex");
  const stagingRoot = path.join(transactionRoot, "host-restore", token);
  let staged = path.join(stagingRoot, "next");
  fs.mkdirSync(stagingRoot, { recursive: true });
  if (originalPath !== null) {
    ensureChildPath(root, originalPath);
    assertNoSymlinkComponents(root, path.dirname(originalPath));
    if (!sameHostPathState(state, hostPathState(originalPath), true)) {
      throw new Error(`host skill original authentication failed: ${target}`);
    }
    staged = originalPath;
  } else if (state.kind === "symlink") {
    fs.symlinkSync(state.target, staged, "dir");
  } else if (["directory", "file"].includes(state.kind) && typeof state.backup === "string") {
    const backupPath = path.resolve(transactionRoot, state.backup);
    ensureChildPath(transactionRoot, backupPath);
    assertNoSymlinkComponents(transactionRoot, backupPath);
    if (!sameHostPathState(state, hostPathState(backupPath))) {
      throw new Error(`host skill backup authentication failed: ${target}`);
    }
    fs.cpSync(backupPath, staged, {
      recursive: true,
      dereference: false,
      verbatimSymlinks: true,
      errorOnExist: true,
      force: false,
    });
  } else if (state.kind !== "absent") {
    throw new Error(`invalid host skill backup state: ${target}`);
  }
  const current = hostPathState(target);
  const restored = hostPathState(staged);
  if (!sameHostPathState(restored, state, originalPath !== null)) {
    throw new Error(`host skill restore staging integrity mismatch: ${target}`);
  }
  const incoming = path.join(path.dirname(target), `.agent-flow-host-restore-${token}-next`);
  const displaced = path.join(path.dirname(target), `.agent-flow-host-restore-${token}-previous`);
  swapHostPath(root, target, staged, incoming, displaced, current, restored);
  removeHostTemporaryPath(root, transactionRoot, incoming, restored);
  removeHostTemporaryPath(root, transactionRoot, displaced, current);
}

function rollbackRecordedHostMutations(root, transactionRoot, journal) {
  const operations = Array.isArray(journal?.host_mutations) ? journal.host_mutations : [];
  for (let index = operations.length - 1; index >= 0; index -= 1) {
    const operation = operations[index];
    const target = hostMutationTarget(root, operation.path);
    const current = hostPathState(target);
    if (operation.pending) {
      const incoming = path.resolve(root, operation.pending.incoming);
      const displaced = path.resolve(root, operation.pending.displaced);
      ensureChildPath(root, incoming);
      ensureChildPath(root, displaced);
      const completed = sameHostPathState(current, operation.pending.after, true)
        && (
          operation.pending.completed === true
          || (
            hostPathState(incoming).kind === "absent"
            && sameHostPathState(hostPathState(displaced), operation.before, true)
          )
        );
      const interrupted = current.kind === "absent"
        && sameHostPathState(hostPathState(incoming), operation.pending.after, true)
        && sameHostPathState(hostPathState(displaced), operation.before, true);
      const notStarted = sameHostPathState(current, operation.before, true)
        && hostPathState(displaced).kind === "absent";
      if (!completed && !interrupted && !notStarted) {
        throw new Error(`host skill path changed outside install transaction: ${operation.path}`);
      }
      if (completed || interrupted) {
        const original = sameHostPathState(hostPathState(displaced), operation.before, true)
          ? displaced
          : null;
        restoreHostPathState(root, target, operation.before, transactionRoot, original);
      }
      removeHostTemporaryPath(root, transactionRoot, incoming, operation.pending.after);
      removeHostTemporaryPath(root, transactionRoot, displaced, operation.before);
      operation.pending = null;
      if (completed || interrupted) {
        operation.rolled_back = true;
        continue;
      }
    }
    if (sameHostPathState(current, operation.before, true)) {
      operation.rolled_back = true;
      continue;
    }
    const committedByTransaction = operation.after
      ? sameHostPathState(current, operation.after, true)
      : false;
    if (!committedByTransaction) {
      throw new Error(`host skill path changed outside install transaction: ${operation.path}`);
    }
    const original = operation.original
      ? path.resolve(root, operation.original)
      : null;
    restoreHostPathState(root, target, operation.before, transactionRoot, original);
    if (!sameHostPathState(hostPathState(target), operation.before)) {
      throw new Error(`host skill rollback integrity mismatch: ${operation.path}`);
    }
    operation.rolled_back = true;
  }
  if (fs.existsSync(transactionRoot)) {
    writeInstallJournal(path.join(transactionRoot, "journal.json"), journal);
    if (
      operations.length > 0
      && process.env.AGENT_FLOW_TEST_CRASH_AFTER_HOST_ROLLBACK === "1"
    ) {
      process.exit(93);
    }
  }
}

function verifyCommittedHostMutations(root, journal, requireOriginal = true) {
  for (const operation of journal?.host_mutations || []) {
    if (!operation.after || operation.pending) throw new Error(`incomplete host skill mutation: ${operation.path}`);
    const target = hostMutationTarget(root, operation.path);
    if (!sameHostPathState(hostPathState(target), operation.after, true)) {
      throw new Error(`host skill mutation commitment changed: ${operation.path}`);
    }
    if (
      requireOriginal
      && operation.before.kind !== "absent"
      && (
        typeof operation.original !== "string"
        || !sameHostPathState(
          hostPathState(path.resolve(root, operation.original)),
          operation.before,
          true,
        )
      )
    ) {
      throw new Error(`host skill original commitment changed: ${operation.path}`);
    }
  }
}

function cleanupCommittedHostOriginals(root, transactionRoot, journal) {
  for (const operation of journal?.host_mutations || []) {
    if (typeof operation.original !== "string") continue;
    const original = path.resolve(root, operation.original);
    ensureChildPath(root, original);
    removeHostTemporaryPath(root, transactionRoot, original, operation.before);
  }
}

function managedInstallPaths(root) {
  const agentFlowDir = path.join(root, ".agent-flow");
  return [
    ...["workflows", "profiles", "templates", "scripts", "prompts", "rules", "bootstrap"]
      .map((name) => path.join(agentFlowDir, name)),
    path.join(root, RUNTIME_PYTHON_RELATIVE),
    path.join(root, RUNTIME_NODE_RELATIVE),
    path.join(root, PROJECT_LAUNCHER_RELATIVE),
    path.join(agentFlowDir, "kit.json"),
    path.join(root, "scripts"),
    path.join(root, ".Codex", "agents"),
    path.join(root, ".claude", "agents"),
    path.join(root, ".omp", "agents"),
    path.join(root, ".Codex", "rules", "context"),
    path.join(root, ".Codex", "context"),
    path.join(root, ".Codex", "rules", "codebase-rubric.md"),
    path.join(root, ".Codex", "rules", "concise-output.md"),
    path.join(root, ".Codex", "hooks.json"),
    path.join(root, ".codex", "hooks.json"),
    path.join(root, ".claude", "settings.json"),
    path.join(root, ".omp", "extensions", "agent-flow-hooks.ts"),
    path.join(root, ".gitignore"),
    path.join(root, "AGENTS.md"),
    path.join(root, "CLAUDE.md"),
  ];
}

function managedPathState(pathName) {
  const stat = lstatIfExists(pathName);
  if (!stat) return { kind: "absent" };
  if (stat.isSymbolicLink()) {
    return { kind: "symlink", target: fs.readlinkSync(pathName) };
  }
  if (stat.isFile()) {
    return {
      kind: "file",
      mode: stat.mode & 0o777,
      hash: crypto.createHash("sha256").update(fs.readFileSync(pathName)).digest("hex"),
    };
  }
  if (!stat.isDirectory()) throw new Error(`unsupported managed install path kind: ${pathName}`);
  const entries = [];
  const visit = (current, relative) => {
    const currentStat = fs.lstatSync(current);
    entries.push({ path: relative, kind: "directory", mode: currentStat.mode & 0o777 });
    for (const name of fs.readdirSync(current).sort()) {
      const child = path.join(current, name);
      const childRelative = relative ? `${relative}/${name}` : name;
      const childStat = fs.lstatSync(child);
      if (childStat.isSymbolicLink()) {
        entries.push({ path: childRelative, kind: "symlink", target: fs.readlinkSync(child) });
      } else if (childStat.isDirectory()) {
        visit(child, childRelative);
      } else if (childStat.isFile()) {
        entries.push({
          path: childRelative,
          kind: "file",
          mode: childStat.mode & 0o777,
          hash: crypto.createHash("sha256").update(fs.readFileSync(child)).digest("hex"),
        });
      } else {
        throw new Error(`unsupported managed install tree entry: ${child}`);
      }
    }
  };
  visit(pathName, "");
  return {
    kind: "directory",
    commitment: crypto.createHash("sha256").update(JSON.stringify(entries)).digest("hex"),
  };
}

function sameManagedPathState(left, right) {
  if (!left || !right || left.kind !== right.kind) return false;
  if (left.kind === "symlink") return left.target === right.target;
  if (left.kind === "file") return left.mode === right.mode && left.hash === right.hash;
  if (left.kind === "directory") return left.commitment === right.commitment;
  return left.kind === "absent";
}

function managedPathStateWithIdentity(pathName) {
  const state = managedPathState(pathName);
  if (state.kind === "absent") return state;
  return {
    ...state,
    filesystem_identity: hostFilesystemIdentity(pathName),
  };
}

function sameManagedPathStateWithIdentity(left, right) {
  return sameManagedPathState(left, right)
    && (
      left.kind === "absent"
      || JSON.stringify(left.filesystem_identity) === JSON.stringify(right.filesystem_identity)
    );
}

function managedInstallMutationOperation(transaction, requestedPath) {
  if (!transaction?.journal) return null;
  const requested = path.resolve(requestedPath);
  const caseInsensitive = managedRootIsCaseInsensitive(transaction.root);
  const matches = (transaction.journal.managed_mutations || []).filter((operation) => {
    const target = path.resolve(transaction.root, operation.path);
    if (caseInsensitive) {
      const foldedTarget = target.toLowerCase();
      const foldedRequested = requested.toLowerCase();
      return foldedRequested === foldedTarget || foldedRequested.startsWith(`${foldedTarget}${path.sep}`);
    }
    const relative = path.relative(target, requested);
    return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
  });
  matches.sort((left, right) => right.path.length - left.path.length);
  return matches[0] ?? null;
}

function managedRootIsCaseInsensitive(root) {
  const canonical = path.join(root, ".agent-flow");
  const alternate = path.join(root, ".AGENT-FLOW");
  try {
    return fs.realpathSync.native(canonical) === fs.realpathSync.native(alternate);
  } catch {
    return process.platform === "win32";
  }
}

const PINNED_DIRECTORY_ATOMIC_WRITE = [
  "import json, os, stat, sys",
  "directory, expected_dev, expected_ino, target, expected_kind, target_dev, target_ino, payload_size_text = sys.argv[1:]",
  "if os.path.basename(target) != target or target in ('', '.', '..'): raise SystemExit(70)",
  "if not payload_size_text.isdigit(): raise SystemExit(70)",
  "payload_size = int(payload_size_text)",
  "flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)",
  "try:",
  " directory_fd = os.open(directory, flags)",
  "except OSError:",
  " raise SystemExit(75)",
  "temporary = f'.agent-flow-write-{os.getpid()}-{os.urandom(12).hex()}'",
  "try:",
  " directory_stat = os.fstat(directory_fd)",
  " if str(directory_stat.st_dev) != expected_dev or str(directory_stat.st_ino) != expected_ino:",
  "  raise SystemExit(75)",
  " try:",
  "  target_stat = os.stat(target, dir_fd=directory_fd, follow_symlinks=False)",
  " except FileNotFoundError:",
  "  target_stat = None",
  " if expected_kind == 'absent':",
  "  if target_stat is not None: raise SystemExit(76)",
  " elif expected_kind == 'file':",
  "  if target_stat is None or not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:",
  "   raise SystemExit(76)",
  "  if str(target_stat.st_dev) != target_dev or str(target_stat.st_ino) != target_ino:",
  "   raise SystemExit(76)",
  " else:",
  "  raise SystemExit(70)",
  " descriptor = os.open(",
  "  temporary,",
  "  os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),",
  "  0o600,",
  "  dir_fd=directory_fd,",
  " )",
  " try:",
  "  payload = sys.stdin.buffer.read(payload_size)",
  "  if len(payload) != payload_size: raise SystemExit(78)",
  "  with os.fdopen(descriptor, 'wb', closefd=False) as stream:",
  "   stream.write(payload)",
  "   stream.flush()",
  "   os.fsync(descriptor)",
  "  os.fchmod(descriptor, 0o644)",
  "  temporary_stat = os.fstat(descriptor)",
  " finally:",
  "  os.close(descriptor)",
  " os.rename(temporary, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)",
  " current = os.stat(target, dir_fd=directory_fd, follow_symlinks=False)",
  " if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:",
  "  raise SystemExit(77)",
  " if current.st_dev != temporary_stat.st_dev or current.st_ino != temporary_stat.st_ino:",
  "  raise SystemExit(77)",
  " os.fsync(directory_fd)",
  " print(json.dumps({'device': str(current.st_dev), 'inode': str(current.st_ino), 'links': str(current.st_nlink), 'mode': current.st_mode & 0o777}))",
  "finally:",
  " try:",
  "  os.unlink(temporary, dir_fd=directory_fd)",
  " except FileNotFoundError:",
  "  pass",
  " os.close(directory_fd)",
].join("\n");

function writePinnedDirectoryFile(
  directory,
  directoryIdentity,
  target,
  content,
  expectedTargetIdentity,
  label,
) {
  const expectedKind = expectedTargetIdentity ? "file" : "absent";
  const result = safeSpawnSync(
    "/usr/bin/python3",
    [
      "-I",
      "-c",
      PINNED_DIRECTORY_ATOMIC_WRITE,
      directory,
      String(directoryIdentity.device),
      String(directoryIdentity.inode),
      target,
      expectedKind,
      String(expectedTargetIdentity?.device ?? ""),
      String(expectedTargetIdentity?.inode ?? ""),
      String(Buffer.byteLength(content, "utf8")),
    ],
    {
      encoding: "utf8",
      input: Buffer.from(content, "utf8"),
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 120_000,
    },
  );
  if (!result.error && result.status === 0) {
    try {
      return JSON.parse(result.stdout);
    } catch {
      throw new Error(`pinned ${label} writer returned invalid identity`);
    }
  }
  if (result.status === 75) {
    throw new Error(`pinned ${label} directory changed`);
  }
  if (result.status === 76) {
    throw new Error(`pinned ${label} target changed`);
  }
  const detail = result.error?.message || result.stderr?.trim() || `exit ${result.status}`;
  throw new Error(`pinned ${label} write failed: ${detail}`);
}

const MANAGED_NOREPLACE_RENAME = [
  "import ctypes, errno, os, sys",
  "source, target = sys.argv[1], sys.argv[2]",
  "if os.name == 'nt':",
  " kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)",
  " ok = kernel32.MoveFileW(source, target)",
  " code = 0 if ok else ctypes.get_last_error()",
  " raise SystemExit(0 if ok else (73 if code in (80, 183) else 74))",
  "libc = ctypes.CDLL(None, use_errno=True)",
  "encoded_source, encoded_target = os.fsencode(source), os.fsencode(target)",
  "if sys.platform == 'darwin':",
  " result = libc.renamex_np(encoded_source, encoded_target, 4)",
  "elif sys.platform.startswith('linux'):",
  " try:",
  "  renameat2 = libc.renameat2",
  " except AttributeError:",
  "  raise SystemExit(74)",
  " result = renameat2(-100, encoded_source, -100, encoded_target, 1)",
  "else:",
  " raise SystemExit(74)",
  "if result == 0:",
  " raise SystemExit(0)",
  "code = ctypes.get_errno()",
  "raise SystemExit(73 if code in (errno.EEXIST, errno.ENOTEMPTY) else 74)",
].join("\n");

function renameManagedNoReplace(sourceName, targetName) {
  const result = safeSpawnSync(
    "/usr/bin/python3",
    ["-I", "-c", MANAGED_NOREPLACE_RENAME, sourceName, targetName],
    { stdio: ["ignore", "ignore", "pipe"], encoding: "utf8", timeout: 10_000 },
  );
  if (!result.error && result.status === 0) return;
  if (result.status === 73) {
    throw new Error(`managed install target appeared during swap: ${targetName}`);
  }
  const detail = result.error?.message || result.stderr?.trim() || `exit ${result.status}`;
  throw new Error(`managed install no-replace rename failed: ${detail}`);
}

function swapManagedPath(
  boundaryRoot,
  target,
  staged,
  incoming,
  displaced,
  expectedBefore,
  expectedAfter,
  crashDuringSwap = false,
) {
  const targetParent = path.dirname(target);
  if (path.dirname(incoming) !== targetParent || path.dirname(displaced) !== targetParent) {
    throw new Error(`managed install swap endpoints are not co-located: ${target}`);
  }
  withManagedDirectoryCwd(
    boundaryRoot,
    path.dirname(target),
    expectedBefore.kind === "absent",
    () => {
      const targetName = path.basename(target);
      const incomingName = path.basename(incoming);
      const displacedName = path.basename(displaced);
      if (lstatIfExists(incomingName) || lstatIfExists(displacedName)) {
        throw new Error(`managed install swap path already exists: ${target}`);
      }
      holdInstallForTest("AGENT_FLOW_TEST_HOLD_BEFORE_MANAGED_SWAP_MS", "managed-swap-ready");
      if (expectedAfter.kind !== "absent") {
        if (!sameManagedPathState(managedPathState(staged), expectedAfter)) {
          throw new Error(`managed install staging integrity mismatch: ${target}`);
        }
        renameManagedNoReplace(staged, incomingName);
        if (!sameManagedPathState(managedPathState(incomingName), expectedAfter)) {
          throw new Error(`managed install incoming integrity mismatch: ${target}`);
        }
      }
      if (!sameManagedPathState(managedPathState(targetName), expectedBefore)) {
        throw new Error(`managed install path changed outside transaction: ${target}`);
      }
      if (expectedBefore.kind !== "absent") {
        fs.renameSync(targetName, displacedName);
        if (!sameManagedPathState(managedPathState(displacedName), expectedBefore)) {
          if (!lstatIfExists(targetName)) fs.renameSync(displacedName, targetName);
          throw new Error(`managed install path changed during swap: ${target}`);
        }
      }
      holdInstallForTest("AGENT_FLOW_TEST_HOLD_AFTER_MANAGED_DISPLACE_MS", "managed-target-displaced");
      if (crashDuringSwap && process.env.AGENT_FLOW_TEST_CRASH_DURING_MANAGED_SWAP === "1") {
        process.exit(90);
      }
      if (expectedAfter.kind !== "absent") {
        renameManagedNoReplace(incomingName, targetName);
      }
      if (!sameManagedPathState(managedPathState(targetName), expectedAfter)) {
        throw new Error(`managed install staged mutation mismatch: ${target}`);
      }
    },
  );
}

function managedSwapRelativePaths(transaction, operation, operationIndex, mutationId) {
  const parent = path.dirname(operation.path);
  const prefix = `.agent-flow-swap-${transaction.token}-${operationIndex}-${mutationId}`;
  return {
    incoming: path.join(parent, `${prefix}-next`),
    displaced: path.join(parent, `${prefix}-previous`),
  };
}

function withManagedInstallMutation(pathName, callback) {
  const transaction = activeManagedInstallTransaction;
  const operation = managedInstallMutationOperation(transaction, pathName);
  if (!operation) return callback(pathName);
  const target = path.resolve(transaction.root, operation.path);
  const expected = operation.after ?? operation.before;
  if (!sameManagedPathState(managedPathState(target), expected)) {
    throw new Error(`managed install path changed outside transaction: ${operation.path}`);
  }
  const operationIndex = transaction.journal.managed_mutations.indexOf(operation);
  const mutationId = operation.mutation_count;
  const stagingRelative = path.join("managed-staging", `${operationIndex}-${mutationId}`);
  const stagingRoot = path.join(transaction.transactionRoot, stagingRelative);
  const stagedTarget = path.join(stagingRoot, "next");
  const swapPaths = managedSwapRelativePaths(transaction, operation, operationIndex, mutationId);
  const incomingTarget = path.join(transaction.root, swapPaths.incoming);
  const displacedTarget = path.join(transaction.root, swapPaths.displaced);
  fs.mkdirSync(stagingRoot, { recursive: true });
  if (lstatIfExists(target)) {
    fs.cpSync(target, stagedTarget, { recursive: true, dereference: false, errorOnExist: true, force: false });
    restoreManagedCopyRootMode(target, stagedTarget);
    let stagedState = managedPathState(stagedTarget);
    if (!sameManagedPathState(stagedState, expected)) {
      restoreManagedCopyModes(target, stagedTarget);
      stagedState = managedPathState(stagedTarget);
    }
    if (!sameManagedPathState(stagedState, expected)) {
      throw new Error(`managed install staging integrity mismatch: ${operation.path}`);
    }
  }
  const requested = path.resolve(pathName);
  const requestedRelative = managedRootIsCaseInsensitive(transaction.root)
    && requested.toLowerCase().startsWith(target.toLowerCase())
      ? requested.slice(target.length).replace(new RegExp(`^${escapeRegex(path.sep)}+`), "")
      : path.relative(target, requested);
  const stagedRequested = path.resolve(stagedTarget, requestedRelative);
  ensureChildPath(stagedTarget, stagedRequested);
  let result;
  try {
    result = callback(stagedRequested);
  } catch (error) {
    fs.rmSync(stagingRoot, { recursive: true, force: true });
    throw error;
  }
  if (!sameManagedPathState(managedPathState(target), expected)) {
    fs.rmSync(stagingRoot, { recursive: true, force: true });
    throw new Error(`managed install path changed outside transaction: ${operation.path}`);
  }
  const next = managedPathState(stagedTarget);
  operation.mutation_count += 1;
  operation.pending = {
    mutation_id: mutationId,
    before: managedPathState(target),
    after: next,
    staging: path.relative(transaction.transactionRoot, stagedTarget),
    incoming: swapPaths.incoming,
    displaced: swapPaths.displaced,
  };
  writeInstallJournal(transaction.journalPath, transaction.journal);
  if (!sameManagedPathState(managedPathState(target), operation.pending.before)) {
    throw new Error(`managed install path changed outside transaction: ${operation.path}`);
  }
  swapManagedPath(
    transaction.root,
    target,
    stagedTarget,
    incomingTarget,
    displacedTarget,
    operation.pending.before,
    operation.pending.after,
    true,
  );
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_SWAP_BEFORE_COMMITMENT === "1") process.exit(91);
  operation.pending.completed = true;
  writeInstallJournal(transaction.journalPath, transaction.journal);
  if (process.env.AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_CALLBACK === "1") process.exit(89);
  const current = managedPathState(target);
  if (!sameManagedPathState(current, next)) {
    throw new Error(`managed install staged mutation mismatch: ${operation.path}`);
  }
  removeManagedTemporaryPath(transaction.root, transaction.transactionRoot, incomingTarget, operation.pending.after);
  removeManagedTemporaryPath(transaction.root, transaction.transactionRoot, displacedTarget, operation.pending.before);
  operation.after = current;
  operation.pending = null;
  writeInstallJournal(transaction.journalPath, transaction.journal);
  return result;
}

function snapshotManagedInstallPaths(transaction) {
  const seen = new Set();
  const caseInsensitive = managedRootIsCaseInsensitive(transaction.root);
  for (const target of managedInstallPaths(transaction.root)) {
    ensureChildPath(transaction.root, target);
    assertNoSymlinkComponents(transaction.root, target);
    const key = caseInsensitive ? path.resolve(target).toLowerCase() : path.resolve(target);
    if (seen.has(key)) continue;
    seen.add(key);
    const before = managedPathState(target);
    const operation = {
      path: path.relative(transaction.root, target),
      before,
      after: null,
      mutation_count: 0,
      pending: null,
    };
    if (["directory", "file"].includes(before.kind)) {
      const backupRelative = path.join("managed-backups", String(transaction.journal.managed_mutations.length));
      const backupPath = path.join(transaction.transactionRoot, backupRelative);
      fs.mkdirSync(path.dirname(backupPath), { recursive: true });
      fs.cpSync(target, backupPath, { recursive: true, dereference: false, errorOnExist: true, force: false });
      restoreManagedCopyRootMode(target, backupPath);
      let backupState = managedPathState(backupPath);
      if (!sameManagedPathState(before, backupState)) {
        restoreManagedCopyModes(target, backupPath);
        backupState = managedPathState(backupPath);
      }
      if (!sameManagedPathState(before, backupState)) {
        throw new Error(`managed install backup integrity mismatch: ${operation.path}`);
      }
      before.backup = backupRelative;
      before.backup_filesystem_identity = hostFilesystemIdentity(backupPath);
    }
    transaction.journal.managed_mutations.push(operation);
  }
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function restoreManagedCopyRootMode(source, destination) {
  const sourceMode = fs.lstatSync(source).mode & 0o777;
  const destinationMode = fs.lstatSync(destination).mode & 0o777;
  if (sourceMode !== destinationMode) fs.chmodSync(destination, sourceMode);
}

function restoreManagedCopyModes(source, destination) {
  const sourceMetadata = fs.lstatSync(source);
  if (sourceMetadata.isSymbolicLink()) return;
  const sourceMode = sourceMetadata.mode & 0o777;
  if ((fs.lstatSync(destination).mode & 0o777) !== sourceMode) {
    fs.chmodSync(destination, sourceMode);
  }
  if (!sourceMetadata.isDirectory()) return;
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    restoreManagedCopyModes(
      path.join(source, entry.name),
      path.join(destination, entry.name),
    );
  }
}

function sealManagedInstallMutations(transaction) {
  if (!transaction || !transaction.journal || !fs.existsSync(transaction.transactionRoot)) return;
  for (const operation of transaction.journal.managed_mutations || []) {
    if (operation.pending) {
      continue;
    }
    const current = managedPathState(hostMutationTarget(transaction.root, operation.path));
    const expected = operation.after ?? operation.before;
    if (!sameManagedPathState(expected, current)) {
      throw new Error(`managed install path changed outside transaction: ${operation.path}`);
    }
    operation.after = current;
  }
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function restoreManagedPathState(root, target, state, transactionRoot) {
  let staged = target;
  let stagingRoot = null;
  if (state.kind !== "absent") {
    if (
      !["directory", "file"].includes(state.kind)
      || typeof state.backup !== "string"
      || !validateFilesystemIdentity(state.backup_filesystem_identity)
    ) {
      throw new Error(`invalid managed install backup state: ${target}`);
    }
    const backupPath = path.resolve(transactionRoot, state.backup);
    ensureChildPath(transactionRoot, backupPath);
    assertNoSymlinkComponents(transactionRoot, backupPath);
    if (
      !sameManagedPathState(state, managedPathState(backupPath))
      || !sameHostFilesystemIdentity(
        backupPath,
        state.backup_filesystem_identity,
      )
    ) {
      throw new Error(`managed install backup authentication failed: ${target}`);
    }
    const token = crypto.randomBytes(24).toString("hex");
    stagingRoot = path.join(transactionRoot, "managed-restore-staging", token);
    staged = path.join(stagingRoot, "next");
    fs.mkdirSync(stagingRoot, { recursive: true });
    fs.cpSync(backupPath, staged, {
      recursive: true,
      dereference: false,
      errorOnExist: true,
      force: false,
    });
    restoreManagedCopyRootMode(backupPath, staged);
    let stagedState = managedPathState(staged);
    if (!sameManagedPathState(state, stagedState)) {
      restoreManagedCopyModes(backupPath, staged);
      stagedState = managedPathState(staged);
    }
    if (
      !sameManagedPathState(state, stagedState)
      || !sameHostFilesystemIdentity(
        backupPath,
        state.backup_filesystem_identity,
      )
    ) {
      throw new Error(`managed install backup authentication failed: ${target}`);
    }
  }
  try {
    const token = crypto.randomBytes(24).toString("hex");
    const incoming = path.join(path.dirname(target), `.agent-flow-restore-${token}-next`);
    const displaced = path.join(path.dirname(target), `.agent-flow-restore-${token}-previous`);
    const current = managedPathState(target);
    swapManagedPath(
      root,
      target,
      staged,
      incoming,
      displaced,
      current,
      state,
    );
    removeManagedTemporaryPath(root, transactionRoot, incoming, state);
    removeManagedTemporaryPath(root, transactionRoot, displaced, current);
  } finally {
    if (stagingRoot && lstatIfExists(stagingRoot)) {
      fs.rmSync(stagingRoot, { recursive: true, force: true });
    }
  }
}

function removeManagedTemporaryPath(root, transactionRoot, pathName, expected) {
  const cleanupRoot = path.join(transactionRoot, "cleanup");
  ensureManagedDirectory(cleanupRoot, transactionRoot);
  withManagedDirectoryCwd(root, path.dirname(pathName), false, () => {
    const name = path.basename(pathName);
    const current = managedPathState(name);
    if (current.kind === "absent") return;
    if (!sameManagedPathState(current, expected)) {
      throw new Error(`managed install temporary path changed: ${pathName}`);
    }
    const quarantine = path.join(cleanupRoot, `managed-${crypto.randomBytes(12).toString("hex")}`);
    renameManagedNoReplace(name, quarantine);
    if (!sameManagedPathState(managedPathState(quarantine), expected)) {
      if (managedPathState(name).kind === "absent") renameManagedNoReplace(quarantine, name);
      throw new Error(`managed install temporary path changed during cleanup: ${pathName}`);
    }
  });
}

function rollbackRecordedManagedMutations(root, transactionRoot, journal) {
  const operations = Array.isArray(journal?.managed_mutations) ? journal.managed_mutations : [];
  let restoredCount = 0;
  const recordRestoredMutation = () => {
    restoredCount += 1;
    if (
      process.env.AGENT_FLOW_TEST_CRASH_AFTER_MANAGED_ROLLBACK_COUNT
      === String(restoredCount)
    ) {
      process.exit(92);
    }
  };
  for (let index = operations.length - 1; index >= 0; index -= 1) {
    const operation = operations[index];
    const target = hostMutationTarget(root, operation.path);
    const current = managedPathState(target);
    if (operation.pending) {
      const staged = path.resolve(transactionRoot, operation.pending.staging);
      const incoming = path.resolve(root, operation.pending.incoming);
      const displaced = path.resolve(root, operation.pending.displaced);
      ensureChildPath(transactionRoot, staged);
      ensureChildPath(root, incoming);
      ensureChildPath(root, displaced);
      assertNoSymlinkComponents(transactionRoot, staged);
      assertNoSymlinkComponents(root, path.dirname(incoming));
      assertNoSymlinkComponents(root, path.dirname(displaced));
      const swapCompleted = sameManagedPathState(current, operation.pending.after)
        && (
          operation.pending.completed === true
          || (
            managedPathState(incoming).kind === "absent"
            && sameManagedPathState(managedPathState(displaced), operation.pending.before)
          )
        );
      const swapInterrupted = current.kind === "absent"
        && sameManagedPathState(managedPathState(displaced), operation.pending.before)
        && sameManagedPathState(managedPathState(incoming), operation.pending.after);
      const swapNotStarted = sameManagedPathState(current, operation.pending.before)
        && managedPathState(displaced).kind === "absent"
        && (
          managedPathState(incoming).kind === "absent"
          || sameManagedPathState(managedPathState(incoming), operation.pending.after)
        );
      if (!swapCompleted && !swapInterrupted && !swapNotStarted) {
        throw new Error(`managed install path changed outside transaction: ${operation.path}`);
      }
      if (swapCompleted || swapInterrupted) {
        restoreManagedPathState(root, target, operation.before, transactionRoot);
        if (!sameManagedPathState(managedPathState(target), operation.before)) {
          throw new Error(`managed install rollback integrity mismatch: ${operation.path}`);
        }
        recordRestoredMutation();
      }
      removeManagedTemporaryPath(root, transactionRoot, incoming, operation.pending.after);
      removeManagedTemporaryPath(root, transactionRoot, displaced, operation.pending.before);
      operation.pending = null;
      if (swapCompleted || swapInterrupted) continue;
    }
    if (sameManagedPathState(current, operation.before)) continue;
    if (!operation.after || !sameManagedPathState(current, operation.after)) {
      throw new Error(`managed install path changed outside transaction: ${operation.path}`);
    }
    restoreManagedPathState(root, target, operation.before, transactionRoot);
    if (!sameManagedPathState(managedPathState(target), operation.before)) {
      throw new Error(`managed install rollback integrity mismatch: ${operation.path}`);
    }
    recordRestoredMutation();
  }
  if (fs.existsSync(transactionRoot)) {
    writeInstallJournal(path.join(transactionRoot, "journal.json"), journal);
  }
}

function verifyCommittedManagedMutations(root, journal) {
  for (const operation of journal?.managed_mutations || []) {
    if (!operation.after || operation.pending) {
      throw new Error(`incomplete managed install mutation: ${operation.path}`);
    }
    const target = hostMutationTarget(root, operation.path);
    if (!sameManagedPathState(managedPathState(target), operation.after)) {
      throw new Error(`managed install commitment changed: ${operation.path}`);
    }
  }
}

function commitSkillInstallTransaction(transaction) {
  if (!transaction) return;
  sealManagedInstallMutations(transaction);
  verifySkillTransactionLiveState(transaction);
  const indexPath = path.join(transaction.live, "index.json");
  if (!fs.existsSync(indexPath)) throw new Error("skill install transaction produced no index");
  const marker = readRegularFileSnapshotNoFollow(
    transaction.marker,
    path.dirname(transaction.transactionRoot),
    "skill transaction marker",
  ).bytes.toString("utf8").trim();
  if (marker !== transaction.token) throw new Error("skill install transaction ownership changed");
  verifyCommittedHostMutations(transaction.root, transaction.journal);
  verifyCommittedManagedMutations(transaction.root, transaction.journal);
  transaction.journal.stage = "sealed";
  writeInstallJournal(transaction.journalPath, transaction.journal);
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_BEFORE_COMMIT_FINAL_VERIFY_MS",
    "commit-final-verify-ready",
  );
  verifySkillTransactionLiveState(transaction);
  const finalMarker = readRegularFileSnapshotNoFollow(
    transaction.marker,
    path.dirname(transaction.transactionRoot),
    "skill transaction marker",
  ).bytes.toString("utf8").trim();
  if (finalMarker !== transaction.token) {
    throw new Error("skill install transaction ownership changed before commit");
  }
  verifyCommittedHostMutations(transaction.root, transaction.journal);
  verifyCommittedManagedMutations(transaction.root, transaction.journal);
  transaction.journal.stage = "committed";
  const committedIndex = readRegularFileSnapshotNoFollow(
    indexPath,
    path.dirname(transaction.transactionRoot),
    "committed skill index",
  ).bytes;
  transaction.journal.committed_index_hash = crypto.createHash("sha256")
    .update(committedIndex)
    .digest("hex");
  writeInstallJournal(transaction.journalPath, transaction.journal);
  cleanupCommittedHostOriginals(
    transaction.root,
    transaction.transactionRoot,
    transaction.journal,
  );
  fs.unlinkSync(transaction.marker);
  removeAuthenticatedTransactionRoot(
    transaction.transactionRoot,
    transaction.transactionRootIdentity,
  );
  if (activeManagedInstallTransaction === transaction) activeManagedInstallTransaction = null;
}

function authenticateSkillBackup(
  backup,
  authorityRoot,
  previous,
  expectedState = null,
  failureMessage = "cannot restore unauthenticated skill index backup",
  allowPinnedDrift = false,
) {
  if (!fs.existsSync(backup)) return null;
  if (!previous) throw new Error(failureMessage);
  const state = managedPathStateWithIdentity(backup);
  if (
    state.kind !== "directory"
    || (allowPinnedDrift && !expectedState)
    || (
      expectedState
      && !sameManagedPathStateWithIdentity(state, expectedState)
    )
  ) {
    throw new Error(failureMessage);
  }
  const index = readRegularFileSnapshotNoFollow(
    path.join(backup, "index.json"),
    authorityRoot,
    "backup skill index",
  ).bytes;
  const hash = crypto.createHash("sha256").update(index).digest("hex");
  if (hash !== previous.hash || !index.equals(previous.bytes)) {
    throw new Error(failureMessage);
  }
  const payload = previous.payload ?? JSON.parse(index.toString("utf8"));
  const ownershipEntries = payload?.managed_ownership?.version === 1
    && payload.managed_ownership.entries
    && typeof payload.managed_ownership.entries === "object"
    && !Array.isArray(payload.managed_ownership.entries)
    ? payload.managed_ownership.entries
    : null;
  const relocatedOwnershipEntries = relocatedManagedOwnershipEntries(
    path.dirname(authorityRoot),
    backup,
    payload,
  );
  const indexedEntries = new Set();
  for (const skill of payload?.skills || []) {
    const relative = String(skill?.path || "").replaceAll("\\", "/");
    const prefix = ".agent-flow/skills/";
    if (!relative.startsWith(prefix) || typeof skill.tree_hash !== "string") continue;
    const entry = relative.slice(prefix.length).split("/")[0];
    if (!isPortableSkillName(entry)) continue;
    indexedEntries.add(entry);
    const current = hostPathState(path.join(backup, entry));
    const ownership = ownershipEntries?.[entry];
    if (
      ownershipEntries !== null
      && (
        !ownership
        || typeof ownership !== "object"
        || Array.isArray(ownership)
        || ownership.tree_hash !== skill.tree_hash
        || !ownership.filesystem_identity
      )
    ) {
      throw new Error(failureMessage);
    }
    if (
      current.kind !== "directory"
      || (!allowPinnedDrift && current.tree_hash !== skill.tree_hash)
    ) {
      throw new Error(failureMessage);
    }
  }
  if (ownershipEntries !== null) {
    for (const [entry, ownership] of Object.entries(ownershipEntries)) {
      if (
        !isPortableSkillName(entry)
        || !ownership
        || typeof ownership !== "object"
        || Array.isArray(ownership)
        || typeof ownership.tree_hash !== "string"
        || !ownership.filesystem_identity
      ) {
        throw new Error(failureMessage);
      }
      const target = path.join(backup, entry);
      const current = hostPathState(target);
      if (
        current.kind !== "directory"
        || (
          !sameHostFilesystemIdentity(target, ownership.filesystem_identity)
          && !relocatedOwnershipEntries.has(entry)
        )
        || (
          !indexedEntries.has(entry)
          && current.tree_hash !== ownership.tree_hash
        )
      ) {
        throw new Error(failureMessage);
      }
    }
  }
  return { index, state };
}

function removeAuthenticatedSkillLive(live, expected, label) {
  if (
    expected?.kind !== "directory"
    || !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(live),
      expected,
    )
  ) {
    throw new Error(`${label} changed before delete`);
  }
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_LIVE_DELETE_MS",
    "skill-live-delete-ready",
  );

  const quarantine = path.join(
    path.dirname(live),
    `.skills-live-quarantine-${crypto.randomBytes(24).toString("hex")}`,
  );
  renameManagedNoReplace(live, quarantine);
  const quarantinedState = managedPathStateWithIdentity(quarantine);
  if (
    !sameManagedPathStateWithIdentity(quarantinedState, expected)
    || lstatIfExists(live)
  ) {
    if (!lstatIfExists(live) && lstatIfExists(quarantine)) {
      renameManagedNoReplace(quarantine, live);
    }
    throw new Error(`${label} changed during delete`);
  }
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(quarantine),
      expected,
    )
  ) {
    if (!lstatIfExists(live) && lstatIfExists(quarantine)) {
      renameManagedNoReplace(quarantine, live);
    }
    throw new Error(`${label} changed in quarantine`);
  }
  fs.rmSync(quarantine, { recursive: true });
}
function sameDirectoryFilesystemIdentity(pathName, expected) {
  try {
    const current = fs.lstatSync(pathName);
    return current.isDirectory()
      && !current.isSymbolicLink()
      && String(current.dev) === expected?.device
      && String(current.ino) === expected?.inode;
  } catch {
    return false;
  }
}

function removeAuthenticatedTransactionRoot(transactionRoot, expectedIdentity) {
  if (!lstatIfExists(transactionRoot)) return;
  if (!sameDirectoryFilesystemIdentity(transactionRoot, expectedIdentity)) {
    throw new Error(`install transaction root changed before cleanup: ${transactionRoot}`);
  }
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_BEFORE_TRANSACTION_CLEANUP_MS",
    "transaction-cleanup-ready",
  );
  const quarantine = path.join(
    path.dirname(transactionRoot),
    `.install-transaction-quarantine-${crypto.randomBytes(24).toString("hex")}`,
  );
  renameManagedNoReplace(transactionRoot, quarantine);
  if (
    !sameDirectoryFilesystemIdentity(quarantine, expectedIdentity)
    || lstatIfExists(transactionRoot)
  ) {
    if (!lstatIfExists(transactionRoot) && lstatIfExists(quarantine)) {
      renameManagedNoReplace(quarantine, transactionRoot);
    }
    throw new Error(`install transaction root changed during cleanup: ${transactionRoot}`);
  }
  fs.rmSync(quarantine, { recursive: true });
}


function preflightRecordedRollbackAuthorities(root, transactionRoot, journal) {
  for (const operation of journal?.managed_mutations || []) {
    const target = hostMutationTarget(root, operation.path);
    if (["directory", "file"].includes(operation.before?.kind)) {
      const backupPath = path.resolve(transactionRoot, String(operation.before.backup || ""));
      ensureChildPath(transactionRoot, backupPath);
      assertNoSymlinkComponents(transactionRoot, backupPath);
      if (
        !sameManagedPathState(operation.before, managedPathState(backupPath))
        || !sameHostFilesystemIdentity(
          backupPath,
          operation.before.backup_filesystem_identity,
        )
      ) {
        throw new Error(`managed install backup authentication failed: ${target}`);
      }
    }
  }
  for (const operation of journal?.host_mutations || []) {
    const target = hostMutationTarget(root, operation.path);
    if (["directory", "file"].includes(operation.before?.kind)) {
      const backupPath = path.resolve(transactionRoot, String(operation.before.backup || ""));
      ensureChildPath(transactionRoot, backupPath);
      assertNoSymlinkComponents(transactionRoot, backupPath);
      if (!sameHostPathState(operation.before, hostPathState(backupPath))) {
        throw new Error(`host skill backup authentication failed: ${target}`);
      }
    }
    if (operation.rolled_back === true) {
      if (!sameHostPathState(operation.before, hostPathState(target), true)) {
        throw new Error(`rolled back host skill path changed: ${target}`);
      }
    } else if (operation.original) {
      const original = path.resolve(root, operation.original);
      ensureChildPath(root, original);
      assertNoSymlinkComponents(root, path.dirname(original));
      if (!sameHostPathState(operation.before, hostPathState(original), true)) {
        throw new Error(`host skill original authentication failed: ${target}`);
      }
    }
    if (operation.pending?.displaced) {
      const displaced = path.resolve(root, operation.pending.displaced);
      ensureChildPath(root, displaced);
      assertNoSymlinkComponents(root, path.dirname(displaced));
      const displacedState = hostPathState(displaced);
      if (
        displacedState.kind !== "absent"
        && !sameHostPathState(operation.before, displacedState, true)
      ) {
        throw new Error(`host skill original authentication failed: ${target}`);
      }
    }
  }
}
function rollbackSkillInstallTransaction(transaction) {

  if (!transaction || !fs.existsSync(transaction.transactionRoot)) return;
  if (transaction.journal.unmanaged_conflict) {
    throw new Error(transaction.journal.unmanaged_conflict);
  }
  reconcilePendingSkillMaterialization(transaction);
  verifySkillTransactionLiveState(transaction);
  const authenticatedBackup = authenticateSkillBackup(
    transaction.backup,
    path.dirname(transaction.transactionRoot),
    transaction.previous,
    transaction.journal.backup_state,
    "cannot restore unauthenticated skill index backup",
    transaction.journal.version >= 9,
  );
  preflightRecordedRollbackAuthorities(
    transaction.root,
    transaction.transactionRoot,
    transaction.journal,
  );
  let hostRollbackError = null;
  let managedRollbackError = null;
  try {
    rollbackRecordedManagedMutations(transaction.root, transaction.transactionRoot, transaction.journal);
  } catch (error) {
    managedRollbackError = error;
  }
  try {
    rollbackRecordedHostMutations(transaction.root, transaction.transactionRoot, transaction.journal);
  } catch (error) {
    hostRollbackError = error;
  }
  verifySkillTransactionLiveState(transaction);
  const liveMarker = fs.existsSync(transaction.marker)
    ? readRegularFileSnapshotNoFollow(
      transaction.marker,
      path.dirname(transaction.transactionRoot),
      "skill transaction marker",
    ).bytes.toString("utf8").trim()
    : null;
  if (fs.existsSync(transaction.live)) {
    if (liveMarker !== transaction.token) {
      throw new Error(`cannot roll back unowned live skills directory: ${transaction.live}`);
    }
    removeAuthenticatedSkillLive(
      transaction.live,
      transaction.journal.live_state
        ?? transaction.journal.pending_live_state
        ?? transaction.journal.initial_live_state,
      "skill transaction live tree",
    );
  }
  if (authenticatedBackup) {
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(transaction.backup),
        authenticatedBackup.state,
      )
    ) {
      throw new Error("cannot restore changed skill index backup");
    }
    fs.renameSync(transaction.backup, transaction.live);
    const restoredState = managedPathStateWithIdentity(transaction.live);
    const restoredIndex = readRegularFileSnapshotNoFollow(
      path.join(transaction.live, "index.json"),
      path.dirname(transaction.transactionRoot),
      "restored skill index",
    ).bytes;
    if (
      !sameManagedPathStateWithIdentity(restoredState, authenticatedBackup.state)
      || !restoredIndex.equals(authenticatedBackup.index)
    ) {
      throw new Error("skill transaction restore mismatch");
    }
  }
  if (!managedRollbackError) {
    assertInterruptedSkillIndexCommitment(
      path.dirname(transaction.transactionRoot),
      transaction.previous?.hash,
    );
  }
  if (hostRollbackError || managedRollbackError) {
    delete transaction.journal.live_state;
    transaction.journal.stage = "rollback-blocked";
    writeInstallJournal(transaction.journalPath, transaction.journal);
    throw hostRollbackError || managedRollbackError;
  }
  removeAuthenticatedTransactionRoot(
    transaction.transactionRoot,
    transaction.transactionRootIdentity,
  );
  if (activeManagedInstallTransaction === transaction) activeManagedInstallTransaction = null;
}
function rejectReplacedManagedSkillEntries(
  agentFlowDir,
  previousIndex,
  plannedEntries,
) {
  const entries = previousIndex?.managed_ownership?.version === 1
    && previousIndex.managed_ownership.entries
    && typeof previousIndex.managed_ownership.entries === "object"
    && !Array.isArray(previousIndex.managed_ownership.entries)
    ? previousIndex.managed_ownership.entries
    : null;
  if (!entries) return;
  const relocatedOwnershipEntries = relocatedManagedOwnershipEntries(
    path.dirname(agentFlowDir),
    path.join(agentFlowDir, "skills"),
    previousIndex,
  );
  const indexedTreeHashes = new Map();
  for (const skill of previousIndex?.skills || []) {
    const relative = String(skill?.path || "").replaceAll("\\", "/");
    const prefix = ".agent-flow/skills/";
    if (!relative.startsWith(prefix)) continue;
    const rootName = relative.slice(prefix.length).split("/")[0];
    if (!isPortableSkillName(rootName) || typeof skill.tree_hash !== "string") continue;
    if (
      indexedTreeHashes.has(rootName)
      && indexedTreeHashes.get(rootName) !== skill.tree_hash
    ) {
      indexedTreeHashes.set(rootName, null);
    } else {
      indexedTreeHashes.set(rootName, skill.tree_hash);
    }
  }
  for (const [entry, ownership] of Object.entries(entries)) {
    if (!plannedEntries.has(entry)) continue;
    const target = path.join(agentFlowDir, "skills", entry);
    const current = hostPathState(target);
    if (current.kind === "absent") continue;
    if (
      current.kind !== "directory"
      || (
        indexedTreeHashes.has(entry)
          ? indexedTreeHashes.get(entry) !== ownership?.tree_hash
          : current.tree_hash !== ownership?.tree_hash
      )
      || (
        !sameHostFilesystemIdentity(target, ownership?.filesystem_identity)
        && !relocatedOwnershipEntries.has(entry)
      )
    ) {
      throw new Error(`unmanaged skill entry conflicts with installed skill: ${entry}`);
    }
  }
}


function managedSkillOwnership(root, skills, plannedEntries = []) {
  const entries = Object.create(null);
  for (const skill of skills) {
    const relative = String(skill?.path || "").replaceAll("\\", "/");
    const prefix = ".agent-flow/skills/";
    if (!relative.startsWith(prefix)) continue;
    const rootName = relative.slice(prefix.length).split("/")[0];
    if (!isPortableSkillName(rootName)) continue;
    const state = hostPathState(path.join(root, ".agent-flow", "skills", rootName));
    if (state.kind !== "directory" || state.tree_hash !== skill.tree_hash) continue;
    entries[rootName] = {
      tree_hash: state.tree_hash,
      filesystem_identity: state.filesystem_identity,
    };
  }
  for (const rootName of plannedEntries) {
    if (!isPortableSkillName(rootName) || entries[rootName]) continue;
    const state = hostPathState(path.join(root, ".agent-flow", "skills", rootName));
    if (state.kind !== "directory") continue;
    entries[rootName] = {
      tree_hash: state.tree_hash,
      filesystem_identity: state.filesystem_identity,
    };
  }
  return { version: 1, entries };
}

function blockSkillInstallRollback(transaction, message) {
  transaction.journal.stage = "rollback-blocked";
  transaction.journal.unmanaged_conflict = message;
  writeInstallJournal(transaction.journalPath, transaction.journal);
  throw new Error(message);
}

function copyUnmanagedSkillEntry(transaction, entry, source, destination) {
  const sourceState = managedPathStateWithIdentity(source);
  const stagingRoot = path.join(transaction.transactionRoot, "unmanaged-staging");
  const staged = path.join(stagingRoot, entry);
  fs.mkdirSync(stagingRoot, { recursive: true });
  if (lstatIfExists(staged)) {
    blockSkillInstallRollback(
      transaction,
      `unmanaged skill staging already exists: ${entry}`,
    );
  }
  fs.cpSync(source, staged, {
    recursive: true,
    dereference: false,
    errorOnExist: true,
    force: false,
  });
  const stagedState = managedPathStateWithIdentity(staged);
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(source),
      sourceState,
    )
    || !sameManagedPathState(stagedState, sourceState)
  ) {
    blockSkillInstallRollback(
      transaction,
      `unmanaged skill source changed while copying: ${entry}`,
    );
  }
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_AFTER_UNMANAGED_SKILL_COPY_MS",
    `unmanaged-skill-copy-ready:${entry}`,
  );
  if (!sameManagedPathStateWithIdentity(managedPathStateWithIdentity(source), sourceState)) {
    blockSkillInstallRollback(
      transaction,
      `unmanaged skill source changed while copying: ${entry}`,
    );
  }
  if (!sameManagedPathStateWithIdentity(managedPathStateWithIdentity(staged), stagedState)) {
    blockSkillInstallRollback(
      transaction,
      `unmanaged skill staging changed while copying: ${entry}`,
    );
  }
  if (lstatIfExists(destination)) {
    blockSkillInstallRollback(
      transaction,
      `unmanaged skill destination changed while copying: ${entry}`,
    );
  }
  try {
    renameManagedNoReplace(staged, destination);
  } catch (error) {
    if (lstatIfExists(destination)) {
      blockSkillInstallRollback(
        transaction,
        `unmanaged skill destination changed while copying: ${entry}`,
      );
    }
    throw error;
  }
  if (!sameManagedPathStateWithIdentity(managedPathStateWithIdentity(destination), stagedState)) {
    blockSkillInstallRollback(
      transaction,
      `unmanaged skill destination changed while copying: ${entry}`,
    );
  }
  return stagedState;
}

function snapshotSkillLiveEntries(live, allowed = null) {
  const entries = new Map();
  for (const entry of fs.readdirSync(live)) {
    if (
      entry === "index.json"
      || (
        allowed
        && entry !== ".agent-flow-transaction-owner"
        && !allowed.has(entry)
      )
    ) continue;
    entries.set(entry, managedPathStateWithIdentity(path.join(live, entry)));
  }
  return entries;
}

function verifySkillLiveEntries(transaction, initialEntries, preservedEntries, currentIndex) {
  const expectedNames = new Set([
    "index.json",
    ...initialEntries.keys(),
    ...preservedEntries.keys(),
  ]);
  for (const entry of fs.readdirSync(transaction.live)) {
    if (!expectedNames.has(entry)) {
      blockSkillInstallRollback(
        transaction,
        `skill transaction live tree changed outside transaction: ${entry}`,
      );
    }
  }
  for (const [entry, expected] of [...initialEntries, ...preservedEntries]) {
    if (
      !sameManagedPathStateWithIdentity(
        managedPathStateWithIdentity(path.join(transaction.live, entry)),
        expected,
      )
    ) {
      blockSkillInstallRollback(
        transaction,
        `skill transaction live tree changed outside transaction: ${entry}`,
      );
    }
  }
  for (const [entry, ownership] of Object.entries(
    currentIndex?.managed_ownership?.entries || {},
  )) {
    if (!sameHostPathState(hostPathState(path.join(transaction.live, entry)), {
      kind: "directory",
      tree_hash: ownership.tree_hash,
      filesystem_identity: ownership.filesystem_identity,
    }, true)) {
      blockSkillInstallRollback(
        transaction,
        `managed skill ownership changed before commit: ${entry}`,
      );
    }
  }
}

function sealSkillTransactionLiveState(transaction, expected) {
  if (
    expected?.kind !== "directory"
    || !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(transaction.live),
      expected,
    )
  ) {
    blockSkillInstallRollback(
      transaction,
      "skill transaction live tree changed while sealing",
    );
  }
  transaction.journal.live_state = expected;
  delete transaction.journal.pending_live_state;
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function unsealedSkillTransactionLiveState(live, journal) {
  if (!lstatIfExists(live)) return null;
  const initial = journal?.initial_live_state;
  if (
    initial?.kind !== "directory"
    || !sameDirectoryFilesystemIdentity(live, initial.filesystem_identity)
  ) {
    throw new Error("skill transaction live directory identity changed");
  }
  verifyPinnedSkillLiveStates(live, journal);
  const allowed = new Set([
    ".agent-flow-transaction-owner",
    "index.json",
    ...(journal.planned_live_entries || []),
  ]);
  for (const entry of fs.readdirSync(live)) {
    if (!allowed.has(entry)) {
      throw new Error(`skill transaction live tree changed outside transaction: ${entry}`);
    }
  }
  const marker = path.join(live, ".agent-flow-transaction-owner");
  if (
    !lstatIfExists(marker)
    || readRegularFileSnapshotNoFollow(
      marker,
      path.dirname(live),
      "skill transaction marker",
    ).bytes.toString("utf8").trim() !== journal.token
  ) {
    throw new Error("skill transaction live marker is not owned");
  }
  const state = managedPathStateWithIdentity(live);
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(live),
      state,
    )
  ) {
    throw new Error("skill transaction live tree changed while authenticating");
  }
  return state;
}

function verifySkillTransactionLiveState(transaction) {
  const expected = transaction?.journal?.live_state
    ?? transaction?.journal?.pending_live_state;
  if (!expected) {
    let pending;
    try {
      pending = unsealedSkillTransactionLiveState(
        transaction.live,
        transaction.journal,
      );
    } catch (error) {
      blockSkillInstallRollback(transaction, String(error?.message || error));
    }
    if (!pending) return null;
    transaction.journal.pending_live_state = pending;
    writeInstallJournal(transaction.journalPath, transaction.journal);
    return pending;
  }
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(transaction.live),
      expected,
    )
  ) {
    blockSkillInstallRollback(
      transaction,
      "skill transaction live tree changed outside transaction",
    );
  }
  return expected;
}

function verifyInterruptedSkillLiveState(live, journal) {
  if (journal.stage === "committed" || !lstatIfExists(live)) return null;
  const expected = journal.live_state ?? journal.pending_live_state;
  if (!expected) {
    if (!journal.initial_live_state) {
      if (["prepared", "moving-skills", "skills-moved", "rollback-blocked"].includes(journal.stage)) {
        return null;
      }
      throw new Error("legacy live tree is unauthenticated");
    }
    return unsealedSkillTransactionLiveState(live, journal);
  }
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(live),
      expected,
    )
  ) {
    throw new Error("skill transaction live tree changed outside transaction");
  }
  return expected;
}
function preserveUnmanagedSkillEntries(transaction, previousIndex, currentIndex) {
  if (!transaction) return;
  const managed = new Set(["index.json"]);
  for (const entry of fs.readdirSync(path.join(KIT_ROOT, "skills"), { withFileTypes: true })) {
    if (!entry.isDirectory()) managed.add(entry.name);
  }
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_BEFORE_SKILL_LIVE_SNAPSHOT_MS",
    "skill-live-snapshot-ready",
  );
  verifyPinnedSkillLiveStates(transaction.live, transaction.journal);
  const plannedEntries = new Set(transaction.journal.planned_live_entries || []);
  const snapshotEntries = new Set([...managed, ...plannedEntries]);
  const liveEntriesBefore = snapshotSkillLiveEntries(transaction.live, snapshotEntries);
  const preservedUnmanaged = new Map();
  const previouslyManaged = new Map();
  const ownershipEntries = previousIndex?.managed_ownership?.version === 1
    && previousIndex.managed_ownership.entries
    && typeof previousIndex.managed_ownership.entries === "object"
    && !Array.isArray(previousIndex.managed_ownership.entries)
    ? previousIndex.managed_ownership.entries
    : null;
  const relocatedOwnershipEntries = relocatedManagedOwnershipEntries(
    transaction.root,
    transaction.backup,
    previousIndex,
  );
  for (const skill of previousIndex?.skills || []) {
    const relative = String(skill?.path || "").replaceAll("\\", "/");
    const prefix = ".agent-flow/skills/";
    if (!relative.startsWith(prefix)) continue;
    const rootName = relative.slice(prefix.length).split("/")[0];
    if (!isPortableSkillName(rootName)) continue;
    if (
      ownershipEntries === null
      && GENERATED_PROJECT_SKILL_NAMES.has(rootName)
      && relative === `.agent-flow/skills/${rootName}/SKILL.md`
      && skill?.name === rootName
    ) {
      const source = path.join(transaction.backup, rootName);
      try {
        const state = hostPathState(source);
        if (state.kind === "directory") previouslyManaged.set(rootName, state);
      } catch {
        continue;
      }
      continue;
    }
    if (typeof skill.tree_hash !== "string") continue;
    const ownership = ownershipEntries
      && Object.prototype.hasOwnProperty.call(ownershipEntries, rootName)
      ? ownershipEntries[rootName]
      : null;
    const validOwnership = ownership
      && typeof ownership === "object"
      && !Array.isArray(ownership)
      && ownership.tree_hash === skill.tree_hash
      && ownership.filesystem_identity;
    const source = path.join(transaction.backup, rootName);
    try {
      const state = hostPathState(source);
      if (
        validOwnership
        && state.kind === "directory"
        && (
          sameHostFilesystemIdentity(source, ownership.filesystem_identity)
          || relocatedOwnershipEntries.has(rootName)
        )
        && (state.tree_hash === ownership.tree_hash || plannedEntries.has(rootName))
      ) {
        previouslyManaged.set(rootName, state);
      }
    } catch {
      continue;
    }
  }
  if (ownershipEntries !== null) {
    for (const [rootName, ownership] of Object.entries(ownershipEntries)) {
      if (
        previouslyManaged.has(rootName)
        || !isPortableSkillName(rootName)
        || !ownership
        || typeof ownership !== "object"
        || Array.isArray(ownership)
        || typeof ownership.tree_hash !== "string"
        || !ownership.filesystem_identity
      ) continue;
      const source = path.join(transaction.backup, rootName);
      try {
        const state = hostPathState(source);
        if (
          state.kind === "directory"
          && state.tree_hash === ownership.tree_hash
          && sameHostFilesystemIdentity(source, ownership.filesystem_identity)
        ) {
          previouslyManaged.set(rootName, state);
        }
      } catch {
        continue;
      }
    }
  }
  for (const entry of (
    fs.existsSync(transaction.backup)
      ? fs.readdirSync(transaction.backup)
      : []
  )) {
    if (managed.has(entry) || entry === ".agent-flow-transaction-owner") continue;
    const source = path.join(transaction.backup, entry);
    const previousState = previouslyManaged.get(entry);
    if (
      previousState
      && sameHostPathState(hostPathState(source), previousState, true)
    ) continue;
    const destination = path.join(transaction.live, entry);
    if (lstatIfExists(destination)) {
      const sourceState = hostPathState(source);
      const destinationState = hostPathState(destination);
      if (
        ownershipEntries === null
        && !GENERATED_PROJECT_SKILL_NAMES.has(entry)
        && sourceState.kind === "directory"
        && destinationState.kind === "directory"
        && sourceState.tree_hash === destinationState.tree_hash
      ) {
        continue;
      }
      throw new Error(`unmanaged skill entry conflicts with installed skill: ${entry}`);
    }
    preservedUnmanaged.set(
      entry,
      copyUnmanagedSkillEntry(transaction, entry, source, destination),
    );
  }
  const indexed = new Set((currentIndex?.skills || []).map((skill) => skill.name));
  for (const entry of fs.readdirSync(transaction.live, { withFileTypes: true })) {
    if (managed.has(entry.name) || entry.name.startsWith(".")) continue;
    if (
      entry.isDirectory()
      && fs.existsSync(path.join(transaction.live, entry.name, "SKILL.md"))
      && (preservedUnmanaged.has(entry.name) || !indexed.has(entry.name))
    ) {
      currentIndex.warnings.push(`${entry.name}: preserved unmanaged skill entry without adopting ownership`);
    }
  }
  transaction.liveIndexIdentity = writePinnedDirectoryFile(
    transaction.live,
    transaction.liveIdentity,
    "index.json",
    `${JSON.stringify(currentIndex, null, 2)}\n`,
    transaction.liveIndexIdentity,
    "skill index",
  );
  verifySkillLiveEntries(
    transaction,
    liveEntriesBefore,
    preservedUnmanaged,
    currentIndex,
  );
  const pendingLiveState = managedPathStateWithIdentity(transaction.live);
  verifySkillLiveEntries(
    transaction,
    liveEntriesBefore,
    preservedUnmanaged,
    currentIndex,
  );
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(transaction.live),
      pendingLiveState,
    )
  ) {
    blockSkillInstallRollback(
      transaction,
      "skill transaction live tree changed while preparing seal",
    );
  }
  transaction.journal.pending_live_state = pendingLiveState;
  writeInstallJournal(transaction.journalPath, transaction.journal);
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_AFTER_SKILL_LIVE_VERIFY_MS",
    "skill-live-verified",
  );
  verifySkillLiveEntries(
    transaction,
    liveEntriesBefore,
    preservedUnmanaged,
    currentIndex,
  );
  sealSkillTransactionLiveState(transaction, pendingLiveState);
  holdInstallForTest(
    "AGENT_FLOW_TEST_HOLD_AFTER_SKILL_LIVE_SEAL_MS",
    "skill-live-sealed",
  );
}

function validateJournalManagedState(state, backup = null) {
  if (!state || typeof state !== "object" || Array.isArray(state)) return false;
  if (state.kind === "absent") return state.backup === undefined;
  if (state.kind === "file") {
    return Number.isInteger(state.mode)
      && /^[0-9a-f]{64}$/.test(String(state.hash || ""))
      && (backup === null ? state.backup === undefined : state.backup === backup);
  }
  if (state.kind === "directory") {
    return /^[0-9a-f]{64}$/.test(String(state.commitment || ""))
      && (backup === null ? state.backup === undefined : state.backup === backup);
  }
  return false;
}

function validateFilesystemIdentity(identity) {
  return Boolean(
    identity
    && typeof identity === "object"
    && !Array.isArray(identity)
    && /^[0-9]+$/.test(String(identity.device || ""))
    && /^[0-9]+$/.test(String(identity.inode || ""))
    && /^[0-9]+$/.test(String(identity.links || ""))
    && Number.isInteger(identity.mode),
  );
}

function validateJournalHostState(state, backup = null, requireIdentity = false) {
  if (!state || typeof state !== "object" || Array.isArray(state)) return false;
  if (state.kind === "absent") return state.backup === undefined;
  const validIdentity = validateFilesystemIdentity(state.filesystem_identity);
  if (requireIdentity && !validIdentity) return false;
  if (state.filesystem_identity !== undefined && !validIdentity) return false;
  if (state.kind === "symlink") {
    return typeof state.target === "string" && state.backup === undefined;
  }
  if (state.kind === "file") {
    return /^[0-9a-f]{64}$/.test(String(state.file_hash || ""))
      && (backup === null ? state.backup === undefined : state.backup === backup);
  }
  if (state.kind === "directory") {
    return /^[0-9a-f]{64}$/.test(String(state.tree_hash || ""))
      && (backup === null ? state.backup === undefined : state.backup === backup);
  }
  return false;
}

function interruptedManagedKitUpgradeIsAuthenticated(
  root,
  agentFlowDir,
  journal,
  kit,
) {
  if (
    journal.stage === "committed"
    || kit?.skill_index_hash_version !== 1
  ) {
    return false;
  }
  const kitPath = path.join(agentFlowDir, "kit.json");
  const kitRelative = path.relative(root, kitPath);
  const operation = journal.managed_mutations.find((candidate) => (
    candidate.path === kitRelative
  ));
  if (
    !operation?.after
    || operation.pending
    || !sameManagedPathState(managedPathState(kitPath), operation.after)
  ) {
    return false;
  }
  const indexBytes = readRegularFileSnapshotNoFollow(
    path.join(agentFlowDir, "skills", "index.json"),
    agentFlowDir,
    "interrupted upgraded skill index",
  ).bytes;
  return crypto.createHash("sha256").update(indexBytes).digest("hex")
    === kit.skill_index_hash;
}

function assertInterruptedSkillIndexCommitment(agentFlowDir, expectedHash) {
  const kit = readExistingKit(agentFlowDir);
  if (
    kit?.skill_index_hash_version === 1
    && kit.skill_index_hash !== expectedHash
  ) {
    throw new Error("interrupted skill transaction index commitment changed");
  }
}


function validateInterruptedInstallJournal(root, agentFlowDir, transactionRoot, journal, recoveryLockToken) {
  const validLiveState = journal?.live_state === undefined || (
    journal.live_state?.kind === "directory"
    && validateJournalManagedState(journal.live_state)
    && validateFilesystemIdentity(journal.live_state.filesystem_identity)
  );
  const validInitialLiveState = journal?.initial_live_state === undefined || (
    journal.initial_live_state?.kind === "directory"
    && validateJournalManagedState(journal.initial_live_state)
    && validateFilesystemIdentity(journal.initial_live_state.filesystem_identity)
  );
  const validBackupState = journal?.backup_state === undefined || (
    journal.backup_state?.kind === "directory"
    && validateJournalManagedState(journal.backup_state)
    && validateFilesystemIdentity(journal.backup_state.filesystem_identity)
  );
  const validPendingLiveState = journal?.pending_live_state === undefined || (
    journal.pending_live_state?.kind === "directory"
    && validateJournalManagedState(journal.pending_live_state)
    && validateFilesystemIdentity(journal.pending_live_state.filesystem_identity)
  );
  const validUnmanagedConflict = journal?.unmanaged_conflict === undefined || (
    typeof journal.unmanaged_conflict === "string"
    && journal.unmanaged_conflict.length > 0
    && journal.unmanaged_conflict.length <= 512
    && journal.stage === "rollback-blocked"
  );
  const plannedLiveEntries = journal?.planned_live_entries;
  const validPlannedLiveEntries = plannedLiveEntries === undefined || (
    Array.isArray(plannedLiveEntries)
    && plannedLiveEntries.every((entry) => isPortableSkillName(entry))
    && new Set(plannedLiveEntries).size === plannedLiveEntries.length
    && plannedLiveEntries.every(
      (entry, index) => index === 0
        || compareCodePoints(plannedLiveEntries[index - 1], entry) < 0,
    )
  );
  const plannedLiveStates = journal?.planned_live_states;
  const pinnedLiveEntryNames = pinnedSkillLiveEntryNames(journal);
  const validPlannedLiveStates = plannedLiveStates === undefined || (
    plannedLiveStates
    && typeof plannedLiveStates === "object"
    && !Array.isArray(plannedLiveStates)
    && Object.keys(plannedLiveStates).length === pinnedLiveEntryNames.length
    && pinnedLiveEntryNames.every((name) => {
      const state = plannedLiveStates[name];
      return validateJournalManagedState(state)
        && (
          state.kind === "absent"
          || validateFilesystemIdentity(state.filesystem_identity)
        );
    })
  );
  const pendingSkillMaterialization = journal?.pending_skill_materialization;
  const validPendingSkillMaterialization = pendingSkillMaterialization === undefined || (
    pendingSkillMaterialization
    && typeof pendingSkillMaterialization === "object"
    && !Array.isArray(pendingSkillMaterialization)
    && isPortableSkillName(pendingSkillMaterialization.name)
    && pinnedLiveEntryNames.includes(pendingSkillMaterialization.name)
    && ["copying", "ready", "completed", "cleaning"].includes(
      pendingSkillMaterialization.phase,
    )
    && ["absent", "directory", "file"].includes(
      pendingSkillMaterialization.before?.kind,
    )
    && validateJournalManagedState(pendingSkillMaterialization.before)
    && (
      pendingSkillMaterialization.before.kind === "absent"
      || validateFilesystemIdentity(
        pendingSkillMaterialization.before.filesystem_identity,
      )
    )
    && typeof pendingSkillMaterialization.stage === "string"
    && /^materialized-staging\/[A-Za-z0-9._-]+$/.test(
      pendingSkillMaterialization.stage.replaceAll("\\", "/"),
    )
    && validateFilesystemIdentity(
      pendingSkillMaterialization.stage_filesystem_identity,
    )
    && typeof pendingSkillMaterialization.displaced === "string"
    && /^\.agent-flow-displaced-[A-Za-z0-9._-]+-[0-9a-f]{24}$/.test(
      pendingSkillMaterialization.displaced,
    )
    && (
      pendingSkillMaterialization.phase === "cleaning"
        ? ["target", "displaced"].includes(
            pendingSkillMaterialization.cleanup_source,
          )
          && typeof pendingSkillMaterialization.cleanup === "string"
          && /^materialized-cleanup\/materialized-[0-9a-f]{24}$/.test(
            pendingSkillMaterialization.cleanup.replaceAll("\\", "/"),
          )
        : pendingSkillMaterialization.cleanup_source === undefined
          && pendingSkillMaterialization.cleanup === undefined
    )
    && (
      pendingSkillMaterialization.phase === "copying"
        ? pendingSkillMaterialization.after === undefined
          && plannedLiveStates?.[pendingSkillMaterialization.name]
          && sameManagedPathStateWithIdentity(
            plannedLiveStates?.[pendingSkillMaterialization.name],
            pendingSkillMaterialization.before,
          )
        : ["directory", "file"].includes(pendingSkillMaterialization.after?.kind)
          && validateJournalManagedState(pendingSkillMaterialization.after)
          && validateFilesystemIdentity(
            pendingSkillMaterialization.after.filesystem_identity,
          )
          && plannedLiveStates?.[pendingSkillMaterialization.name]
          && sameManagedPathStateWithIdentity(
            plannedLiveStates?.[pendingSkillMaterialization.name],
            pendingSkillMaterialization.phase === "completed"
              || (
                pendingSkillMaterialization.phase === "cleaning"
                && pendingSkillMaterialization.cleanup_source === "displaced"
              )
              ? pendingSkillMaterialization.after
              : pendingSkillMaterialization.before,
          )
    )
  );
  if (
    ![3, 4, 5, 6, 7, 8, 9].includes(journal?.version)
    || journal.root !== fs.realpathSync(root)
    || !/^[0-9a-f]{48}$/.test(String(journal.token || ""))
    || !/^[0-9a-f]{48}$/.test(String(recoveryLockToken || ""))
    || journal.lock_token !== recoveryLockToken
    || typeof journal.had_live_skills !== "boolean"
    || !["prepared", "moving-skills", "skills-moved", "live-created", "sealed", "committed", "recovered", "rollback-blocked"].includes(journal.stage)
    || !Array.isArray(journal.managed_mutations)
    || !Array.isArray(journal.host_mutations)
    || pathHasSymlink(root, transactionRoot)
    || !validLiveState
    || !validInitialLiveState
    || !validPendingLiveState
    || !validBackupState
    || !validUnmanagedConflict
    || !validPlannedLiveEntries
    || !validPlannedLiveStates
    || !validPendingSkillMaterialization
    || (
      journal.version >= 9
      && ["live-created", "sealed", "committed", "recovered", "rollback-blocked"].includes(journal.stage)
      && plannedLiveStates === undefined
    )
    || (
      journal.version >= 9
      && journal.had_live_skills
      && ["moving-skills", "skills-moved", "live-created", "sealed", "committed", "recovered", "rollback-blocked"].includes(journal.stage)
      && journal.backup_state === undefined
    )
    || (
      journal.version < 6
      && (
        journal.live_state !== undefined
        || journal.unmanaged_conflict !== undefined
      )
    )
    || (
      journal.version < 7
      && (
        journal.initial_live_state !== undefined
        || journal.pending_live_state !== undefined
      )
    )
    || (
      journal.version >= 7
      && ["live-created", "committed", "recovered"].includes(journal.stage)
      && journal.initial_live_state === undefined
    )
    || (
      journal.stage === "committed"
      && !/^[0-9a-f]{64}$/.test(String(journal.committed_index_hash || ""))
    )
  ) {
    throw new Error(`invalid interrupted skill transaction: ${transactionRoot}`);
  }
  if (journal.version < 4 && journal.host_mutations.length > 0) {
    throw new Error(`invalid interrupted skill transaction: legacy host identity is unauthenticated: ${transactionRoot}`);
  }
  const allowedManagedPaths = new Set(
    managedInstallPaths(root).map((target) => path.relative(root, target)),
  );
  const seenManaged = new Set();
  for (let index = 0; index < journal.managed_mutations.length; index += 1) {
    const operation = journal.managed_mutations[index];
    if (
      !operation
      || typeof operation.path !== "string"
      || !allowedManagedPaths.has(operation.path)
      || seenManaged.has(operation.path)
      || !Number.isInteger(operation.mutation_count)
      || operation.mutation_count < 0
      || (operation.pending != null && (typeof operation.pending !== "object" || Array.isArray(operation.pending)))
    ) {
      throw new Error(`invalid interrupted managed mutation: ${operation?.path ?? index}`);
    }
    seenManaged.add(operation.path);
    const target = hostMutationTarget(root, operation.path);
    assertNoSymlinkComponents(root, target);
    const beforeBackup = ["file", "directory"].includes(operation.before?.kind)
      ? path.join("managed-backups", String(index))
      : null;
    if (
      !validateJournalManagedState(operation.before, beforeBackup)
      || (operation.after !== null && !validateJournalManagedState(operation.after))
    ) {
      throw new Error(`invalid interrupted managed mutation state: ${operation.path}`);
    }
    if (
      journal.version >= 8
      && beforeBackup
      && !validateFilesystemIdentity(operation.before.backup_filesystem_identity)
    ) {
      throw new Error(`invalid interrupted managed backup identity: ${operation.path}`);
    }
    if (operation.pending) {
      const expectedPrefix = path.join("managed-staging", `${index}-${operation.pending.mutation_id}`);
      const swapPrefix = `.agent-flow-swap-${journal.token}-${index}-${operation.pending.mutation_id}`;
      const expectedIncoming = path.join(path.dirname(operation.path), `${swapPrefix}-next`);
      const expectedDisplaced = path.join(path.dirname(operation.path), `${swapPrefix}-previous`);
      if (
        operation.pending.mutation_id !== operation.mutation_count - 1
        || operation.pending.staging !== path.join(expectedPrefix, "next")
        || operation.pending.incoming !== expectedIncoming
        || operation.pending.displaced !== expectedDisplaced
        || ![undefined, true].includes(operation.pending.completed)
        || !validateJournalManagedState(operation.pending.before)
        || !validateJournalManagedState(operation.pending.after)
      ) {
        throw new Error(`invalid interrupted managed mutation intent: ${operation.path}`);
      }
      const stagedPath = path.resolve(transactionRoot, operation.pending.staging);
      ensureChildPath(transactionRoot, stagedPath);
      assertNoSymlinkComponents(transactionRoot, stagedPath);
      for (const relative of [operation.pending.incoming, operation.pending.displaced]) {
        const pendingPath = path.resolve(root, relative);
        ensureChildPath(root, pendingPath);
        assertNoSymlinkComponents(root, path.dirname(pendingPath));
      }
    }
    if (beforeBackup) {
      const backupPath = path.resolve(transactionRoot, beforeBackup);
      ensureChildPath(transactionRoot, backupPath);
      assertNoSymlinkComponents(transactionRoot, backupPath);
    }
  }
  const seenHost = new Set();
  const hostPathPattern = /^(?:\.Codex|\.codex|\.claude|\.omp|\.gemini|\.gemini\/antigravity)\/skills\/[A-Za-z0-9._-]+$/;
  for (let index = 0; index < journal.host_mutations.length; index += 1) {
    const operation = journal.host_mutations[index];
    const normalized = String(operation?.path || "").replaceAll("\\", "/");
    const skillName = normalized.split("/").at(-1) || "";
    if (
      !hostPathPattern.test(normalized)
      || normalized.split("/").some((part) => part === "..")
      || safeSkillName(skillName) !== skillName
      || !Array.isArray(operation.allowed_after)
    ) {
      throw new Error(`invalid interrupted host mutation: ${normalized || index}`);
    }
    const target = hostMutationTarget(root, operation.path);
    const hostKey = managedRootIsCaseInsensitive(root)
      ? path.resolve(target).toLowerCase()
      : path.resolve(target);
    if (seenHost.has(hostKey)) {
      throw new Error(`invalid duplicate interrupted host mutation: ${normalized}`);
    }
    seenHost.add(hostKey);
    assertNoSymlinkComponents(root, path.dirname(target));
    const beforeBackup = ["file", "directory"].includes(operation.before?.kind)
      ? path.join("host-backups", String(index))
      : null;
    if (
      !validateJournalHostState(operation.before, beforeBackup, journal.version >= 4)
      || (operation.after !== null && !validateJournalHostState(operation.after, null, journal.version >= 4))
      || !operation.allowed_after.every((state) => validateJournalHostState(state))
      || (operation.pending !== null && (typeof operation.pending !== "object" || Array.isArray(operation.pending)))
      || ![undefined, true].includes(operation.rolled_back)
    ) {
      throw new Error(`invalid interrupted host mutation state: ${normalized}`);
    }
    const prefix = `.agent-flow-host-swap-${journal.token}-${index}`;
    const expectedOriginal = path.join(path.dirname(operation.path), `${prefix}-previous`);
    if (
      journal.version >= 5
      && ![null, expectedOriginal].includes(operation.original)
    ) {
      throw new Error(`invalid interrupted host original: ${normalized}`);
    }
    if (operation.pending) {
      const expectedIncoming = path.join(path.dirname(operation.path), `${prefix}-next`);
      const expectedDisplaced = path.join(path.dirname(operation.path), `${prefix}-previous`);
      if (
        operation.pending.staging !== path.join("host-staging", String(index), "next")
        || operation.pending.incoming !== expectedIncoming
        || operation.pending.displaced !== expectedDisplaced
        || ![undefined, true].includes(operation.pending.completed)
        || !validateJournalHostState(operation.pending.after, null, journal.version >= 4)
      ) {
        throw new Error(`invalid interrupted host mutation intent: ${normalized}`);
      }
    } else if (
      journal.version >= 5
      && operation.after !== null
      && operation.before.kind !== "absent"
      && operation.original !== expectedOriginal
    ) {
      throw new Error(`invalid interrupted host original commitment: ${normalized}`);
    }
    if (beforeBackup) {
      const backupPath = path.resolve(transactionRoot, beforeBackup);
      ensureChildPath(transactionRoot, backupPath);
      assertNoSymlinkComponents(transactionRoot, backupPath);
    }
  }
  if (journal.had_live_skills) {
    const kit = readExistingKit(agentFlowDir);
    const expectedIndexHash = journal.stage === "committed"
      ? journal.committed_index_hash
      : journal.previous_index_hash;
    if (
      kit?.skill_index_hash_version === 1
      && kit.skill_index_hash !== expectedIndexHash
      && !interruptedManagedKitUpgradeIsAuthenticated(
        root,
        agentFlowDir,
        journal,
        kit,
      )
    ) {
      throw new Error("interrupted skill transaction index commitment changed");
    }
  }
}

function sameJsonAuthorityIdentity(left, right) {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.mode === right.mode
    && left.nlink === right.nlink
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs;
}

function pinJsonAuthorityDirectories(authorityRoot, pathName) {
  const authority = path.resolve(authorityRoot);
  const target = path.resolve(pathName);
  ensureChildPath(authority, target);
  const parts = path.relative(authority, target).split(path.sep).filter(Boolean);
  const directoryParts = parts.slice(0, -1);
  const paths = [authority];
  let cursor = authority;
  for (const part of directoryParts) {
    cursor = path.join(cursor, part);
    paths.push(cursor);
  }
  return paths.map((directoryPath) => {
    const metadata = fs.lstatSync(directoryPath, { bigint: true });
    if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
      throw new Error(`unsafe JSON authority directory: ${directoryPath}`);
    }
    return { path: directoryPath, metadata };
  });
}

function assertJsonAuthorityDirectories(directories, label = "JSON authority") {
  for (const directory of directories) {
    const current = fs.lstatSync(directory.path, { bigint: true });
    if (
      !current.isDirectory()
      || !sameJsonAuthorityIdentity(directory.metadata, current)
    ) {
      throw new Error(`${label} directory changed while reading: ${directory.path}`);
    }
  }
}

function readRegularFileSnapshotNoFollow(
  pathName,
  authorityRoot,
  label = "JSON authority",
) {
  const directories = pinJsonAuthorityDirectories(authorityRoot, pathName);
  assertJsonAuthorityDirectories(directories, label);
  const initial = fs.lstatSync(pathName, { bigint: true });
  if (
    initial.isSymbolicLink()
    || !initial.isFile()
    || initial.nlink !== 1n
  ) {
    throw new Error(`unsafe ${label} file: ${pathName}`);
  }
  const noFollow = fs.constants.O_NOFOLLOW || 0;
  const nonBlocking = fs.constants.O_NONBLOCK || 0;
  const descriptor = fs.openSync(
    pathName,
    fs.constants.O_RDONLY | noFollow | nonBlocking,
  );
  try {
    const heldAuthorityPath = process.env.AGENT_FLOW_TEST_HOLD_JSON_AUTH_PATH;
    if (
      !heldAuthorityPath
      || path.resolve(pathName) === path.resolve(heldAuthorityPath)
    ) {
      holdInstallForTest(
        "AGENT_FLOW_TEST_HOLD_AFTER_JSON_AUTH_OPEN_MS",
        "json-authority-opened",
      );
    }
    const before = fs.fstatSync(descriptor, { bigint: true });
    assertJsonAuthorityDirectories(directories, label);
    if (
      !before.isFile()
      || before.nlink !== 1n
      || !sameJsonAuthorityIdentity(initial, before)
    ) {
      throw new Error(`${label} file changed while reading: ${pathName}`);
    }
    const bytes = fs.readFileSync(descriptor);
    const after = fs.fstatSync(descriptor, { bigint: true });
    const current = fs.lstatSync(pathName, { bigint: true });
    assertJsonAuthorityDirectories(directories, label);
    if (
      !after.isFile()
      || !current.isFile()
      || after.nlink !== 1n
      || current.nlink !== 1n
      || !sameJsonAuthorityIdentity(before, after)
      || !sameJsonAuthorityIdentity(after, current)
    ) {
      throw new Error(`${label} file changed while reading: ${pathName}`);
    }
    return { bytes, metadata: before, directories };
  } finally {
    fs.closeSync(descriptor);
  }
}

function readRegularJsonNoFollow(
  pathName,
  authorityRoot,
  label = "JSON authority",
) {
  const snapshot = readRegularFileSnapshotNoFollow(
    pathName,
    authorityRoot,
    label,
  );
  return JSON.parse(snapshot.bytes.toString("utf8"));
}

function recoverInterruptedSkillTransaction(root, agentFlowDir, recoveryLockToken) {
  const transactionRoot = path.join(agentFlowDir, "install-transaction");
  if (!fs.existsSync(transactionRoot)) return;
  const journalPath = path.join(transactionRoot, "journal.json");
  if (pathHasSymlink(root, transactionRoot)) {
    throw new Error(`invalid interrupted skill transaction: ${transactionRoot}`);
  }
  assertNoSymlinkComponents(transactionRoot, journalPath);
  const journalSnapshot = readRegularFileSnapshotNoFollow(
    journalPath,
    agentFlowDir,
    "install journal",
  );
  const journal = JSON.parse(journalSnapshot.bytes.toString("utf8"));
  const transactionMetadata = journalSnapshot.directories.at(-1).metadata;
  bindInstallJournalAuthority(
    journal,
    {
      device: String(transactionMetadata.dev),
      inode: String(transactionMetadata.ino),
      links: String(transactionMetadata.nlink),
      mode: Number(transactionMetadata.mode & 0o777n),
    },
    {
      device: String(journalSnapshot.metadata.dev),
      inode: String(journalSnapshot.metadata.ino),
      links: String(journalSnapshot.metadata.nlink),
      mode: Number(journalSnapshot.metadata.mode & 0o777n),
    },
  );
  validateInterruptedInstallJournal(root, agentFlowDir, transactionRoot, journal, recoveryLockToken);
  const live = path.join(agentFlowDir, "skills");
  const backup = path.join(transactionRoot, "skills-backup");
  const marker = path.join(live, ".agent-flow-transaction-owner");
  if (journal.unmanaged_conflict) {
    throw new Error(journal.unmanaged_conflict);
  }
  reconcilePendingSkillMaterialization({
    root,
    transactionRoot,
    live,
    journal,
    journalPath,
  });
  const interruptedLiveState = verifyInterruptedSkillLiveState(live, journal);
  if (
    interruptedLiveState
    && !journal.live_state
    && !journal.pending_live_state
  ) {
    journal.pending_live_state = interruptedLiveState;
    writeInstallJournal(journalPath, journal);
  }
  if (journal.stage === "rollback-blocked") {
    preflightRecordedRollbackAuthorities(root, transactionRoot, journal);
    rollbackRecordedManagedMutations(root, transactionRoot, journal);
    rollbackRecordedHostMutations(root, transactionRoot, journal);
    if (journal.had_live_skills) {
      assertInterruptedSkillIndexCommitment(
        agentFlowDir,
        journal.previous_index_hash,
      );
    }
    if (!journal.had_live_skills) {
      if (fs.existsSync(live)) {
        throw new Error("blocked initial skill transaction unexpectedly has a live directory");
      }
      removeAuthenticatedTransactionRoot(
        transactionRoot,
        journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
      );
      return;
    }
    const authenticatedBytes = Buffer.from(String(journal.previous_index_bytes || ""), "base64");
    const liveIndex = readRegularFileSnapshotNoFollow(
      path.join(live, "index.json"),
      agentFlowDir,
      "live skill index",
    ).bytes;
    if (!liveIndex.equals(authenticatedBytes)) {
      throw new Error("blocked skill transaction live index is not authenticated");
    }
    removeAuthenticatedTransactionRoot(
      transactionRoot,
      journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
    );
    return;
  }
  if (journal.stage === "committed") {
    const committedIndex = readRegularFileSnapshotNoFollow(
      path.join(live, "index.json"),
      agentFlowDir,
      "committed skill index",
    ).bytes;
    const committedHash = crypto.createHash("sha256").update(committedIndex).digest("hex");
    if (committedHash !== journal.committed_index_hash) {
      throw new Error("committed skill transaction index is not authenticated");
    }
    verifyCommittedHostMutations(root, journal, false);
    verifyCommittedManagedMutations(root, journal);
    cleanupCommittedHostOriginals(root, transactionRoot, journal);
    if (fs.existsSync(marker)) {
      if (
        readRegularFileSnapshotNoFollow(
          marker,
          agentFlowDir,
          "skill transaction marker",
        ).bytes.toString("utf8").trim() !== journal.token
      ) {
        throw new Error("committed skill transaction marker is not owned");
      }
      fs.unlinkSync(marker);
    }
    removeAuthenticatedTransactionRoot(
      transactionRoot,
      journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
    );
    return;
  }
  if (journal.stage === "prepared") {
    if (
      journal.had_live_skills
      && !fs.existsSync(path.join(live, "index.json"))
      && fs.existsSync(path.join(backup, "index.json"))
    ) {
      journal.stage = "skills-moved";
    } else {
      if (journal.had_live_skills && !fs.existsSync(path.join(live, "index.json"))) {
        throw new Error("prepared skill transaction lost its live index");
      }
      removeAuthenticatedTransactionRoot(
        transactionRoot,
        journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
      );
      return;
    }
  }
  if (journal.stage === "moving-skills") {
    if (journal.version >= 9) {
      const liveState = managedPathStateWithIdentity(live);
      const backupState = managedPathStateWithIdentity(backup);
      if (
        sameManagedPathStateWithIdentity(liveState, journal.backup_state)
        && backupState.kind === "absent"
      ) {
        removeAuthenticatedTransactionRoot(
          transactionRoot,
          journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
        );
        return;
      }
      if (
        liveState.kind === "absent"
        && sameManagedPathStateWithIdentity(backupState, journal.backup_state)
      ) {
        journal.stage = "skills-moved";
      } else {
        throw new Error("interrupted skill move has ambiguous live and backup state");
      }
    } else {
      const liveExists = fs.existsSync(path.join(live, "index.json"));
      const backupExists = fs.existsSync(path.join(backup, "index.json"));
      if (liveExists && !backupExists) {
        removeAuthenticatedTransactionRoot(
          transactionRoot,
          journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
        );
        return;
      }
      if (!liveExists && backupExists) {
        journal.stage = "skills-moved";
      } else {
        throw new Error("interrupted skill move has ambiguous live and backup state");
      }
    }
  }
  if (!journal.had_live_skills) {
    preflightRecordedRollbackAuthorities(root, transactionRoot, journal);
    rollbackRecordedManagedMutations(root, transactionRoot, journal);
    rollbackRecordedHostMutations(root, transactionRoot, journal);
    verifyInterruptedSkillLiveState(live, journal);
    if (fs.existsSync(live)) {
      const liveToken = fs.existsSync(marker)
        ? readRegularFileSnapshotNoFollow(
          marker,
          agentFlowDir,
          "skill transaction marker",
        ).bytes.toString("utf8").trim()
        : null;
      if (liveToken !== journal.token) {
        throw new Error("interrupted initial skill transaction live directory is not owned");
      }
      removeAuthenticatedSkillLive(
        live,
        journal.live_state ?? journal.pending_live_state ?? journal.initial_live_state,
        "interrupted initial skill transaction live tree",
      );
    }
    journal.stage = "recovered";
    writeInstallJournal(journalPath, journal);
    removeAuthenticatedTransactionRoot(
      transactionRoot,
      journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
    );
    return;
  }
  if (!fs.existsSync(backup) || typeof journal.previous_index_hash !== "string") {
    throw new Error("interrupted skill transaction backup is incomplete");
  }
  const authenticatedBytes = Buffer.from(String(journal.previous_index_bytes || ""), "base64");
  const authenticatedBackup = authenticateSkillBackup(
    backup,
    agentFlowDir,
    {
      hash: journal.previous_index_hash,
      bytes: authenticatedBytes,
    },
    journal.backup_state,
    "interrupted skill transaction backup is not authenticated",
    journal.version >= 9,
  );
  preflightRecordedRollbackAuthorities(root, transactionRoot, journal);
  rollbackRecordedManagedMutations(root, transactionRoot, journal);
  rollbackRecordedHostMutations(root, transactionRoot, journal);
  assertInterruptedSkillIndexCommitment(
    agentFlowDir,
    journal.previous_index_hash,
  );
  if (
    !sameManagedPathStateWithIdentity(
      managedPathStateWithIdentity(backup),
      authenticatedBackup.state,
    )
  ) {
    throw new Error("interrupted skill transaction backup changed before restore");
  }
  verifyInterruptedSkillLiveState(live, journal);
  if (fs.existsSync(live)) {
    const liveToken = fs.existsSync(marker)
      ? readRegularFileSnapshotNoFollow(
        marker,
        agentFlowDir,
        "skill transaction marker",
      ).bytes.toString("utf8").trim()
      : null;
    if (liveToken !== journal.token) {
      throw new Error("interrupted skill transaction live directory is not owned");
    }
    removeAuthenticatedSkillLive(
      live,
      journal.live_state ?? journal.pending_live_state ?? journal.initial_live_state,
      "interrupted skill transaction live tree",
    );
  }
  fs.renameSync(backup, live);
  const restoredState = managedPathStateWithIdentity(live);
  const restored = readRegularFileSnapshotNoFollow(
    path.join(live, "index.json"),
    agentFlowDir,
    "restored skill index",
  ).bytes;
  if (
    !sameManagedPathStateWithIdentity(restoredState, authenticatedBackup.state)
    || !restored.equals(authenticatedBytes)
  ) {
    throw new Error("interrupted skill transaction restore mismatch");
  }
  journal.stage = "recovered";
  writeInstallJournal(journalPath, journal);
  removeAuthenticatedTransactionRoot(
    transactionRoot,
    journal[INSTALL_JOURNAL_DIRECTORY_IDENTITY],
  );
}

function skillCatalogFingerprint(root, home, catalogHosts, env) {
  const resolvedHome = path.resolve(home || ".");
  const configured = {
    codex: configuredCatalogRoot(env.CODEX_HOME, resolvedHome, ".codex"),
    claude: configuredCatalogRoot(env.CLAUDE_CONFIG_DIR, resolvedHome, ".claude"),
    omp: configuredCatalogRoot(env.PI_CODING_AGENT_DIR, resolvedHome, path.join(".omp", "agent")),
  };
  const roots = [
    ["project-local", path.join(root, ".agent-flow", "local-skills")],
    ["project", samePath(root, KIT_ROOT) ? null : path.join(root, "skills")],
    ...[...new Set(catalogHosts || [])]
      .filter((host) => PROJECT_SKILL_HOSTS.includes(host))
      .sort(compareCodePoints)
      .map((host) => [`host:${host}`, path.join(configured[host], "skills")]),
    ["shared", path.join(resolvedHome, ".agents", "skills")],
    ["bundled", path.join(KIT_ROOT, "skills")],
  ];
  const manifest = {
    catalog_hosts: [...new Set(catalogHosts || [])]
      .filter((host) => PROJECT_SKILL_HOSTS.includes(host))
      .sort(compareCodePoints),
    roots: [],
  };
  for (const [source, catalogRoot] of roots) {
    if (!catalogRoot) continue;
    manifest.roots.push({
      source,
      root: source === "bundled" ? "<bundled>" : path.resolve(catalogRoot),
      entries: catalogTreeManifest(catalogRoot),
    });
  }
  return crypto.createHash("sha256").update(JSON.stringify(manifest)).digest("hex");
}

function configuredCatalogRoot(value, home, fallback) {
  if (typeof value !== "string" || !value.trim()) return path.join(home, fallback);
  if (value.trim() === "~") return home;
  if (value.trim().startsWith("~/") || value.trim().startsWith("~\\")) {
    return path.resolve(home, value.trim().slice(2));
  }
  return path.resolve(value.trim());
}

function catalogTreeManifest(root) {
  const stat = lstatIfExists(root);
  if (!stat) return [];
  return catalogTreeEntries(root, root).filter((entry) => entry.path !== "");
}

function catalogTreeEntries(root, current) {
  const stat = fs.lstatSync(current);
  const relative = path.relative(root, current).split(path.sep).join("/");
  if (stat.isSymbolicLink()) {
    return [{ path: relative, kind: "symlink", target: fs.readlinkSync(current) }];
  }
  if (stat.isFile()) {
    return [{
      path: relative,
      kind: "file",
      hash: crypto.createHash("sha256").update(fs.readFileSync(current)).digest("hex"),
    }];
  }
  if (!stat.isDirectory()) return [{ path: relative, kind: "other", mode: stat.mode }];
  const result = [{ path: relative, kind: "directory" }];
  for (const entry of fs.readdirSync(current).sort()) {
    if (current === root && entry.startsWith(".")) continue;
    result.push(...catalogTreeEntries(root, path.join(current, entry)));
  }
  return result;
}

function readBundledSkillCompatibility(agentFlowDir) {
  const metadataPath = path.join(agentFlowDir, "skills", "compatibility.json");
  let payload;
  try {
    payload = readRegularJsonNoFollow(metadataPath, agentFlowDir);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw new Error(`invalid skill compatibility metadata: ${metadataPath}`, { cause: error });
  }
  try {
    return normalizeSkillCompatibility(payload);
  } catch (error) {
    throw new Error(`invalid skill compatibility metadata: ${error.message}`, { cause: error });
  }
}

function selectProjectSkills(root, agentFlowDir, installSelection = null, sourcePlan = null) {
  const compatibility = readBundledSkillCompatibility(agentFlowDir);
  const resolvedExternalNames = new Set(
    (sourcePlan?.entries || []).map((entry) => entry.name),
  );
  const ignoredInstalledNames = new Set(
    [...PROFILE_MANAGED_HOST_ONLY_SKILLS]
      .filter((name) => !resolvedExternalNames.has(name)),
  );
  const discovered = [
    ...discoverSkills(path.join(agentFlowDir, "local-skills"), "local", root, PROFILE_MANAGED_HOST_ONLY_SKILLS),
    ...discoverProjectSkills(root),
    ...discoverSkills(
      path.join(agentFlowDir, "skills"),
      "bundled",
      root,
      new Set(["compatibility.json", "provider-registry.json", "index.json", ...ignoredInstalledNames]),
    ),
  ];
  const byName = new Map();
  const warnings = [];
  for (const skill of discovered) {
    const current = byName.get(skill.name);
    if (!current || skill.priority < current.priority) {
      byName.set(skill.name, skill);
    }
    warnings.push(...skill.warnings);
  }
  const requestedAllowed = installSelection?.skillNames || null;
  const allowed = requestedAllowed && compatibility
    ? canonicalizeSkillCompatibilitySelection(compatibility, requestedAllowed)
    : requestedAllowed;
  const sourceByName = new Map((sourcePlan?.entries || []).map((entry) => [entry.name, entry]));
  if (compatibility) {
    validateConcreteSkillCompatibility(compatibility, [...byName.keys()]);
  }
  const skills = [...byName.values()]
    .filter((skill) => !allowed || allowed.has(skill.name))
    .map((skill) => {
      const resolved = sourceByName.get(skill.name);
      const materializedTreeHash = !resolved || PROJECT_COMMAND_SKILL_NAMES.has(skill.name)
        ? hashSkillTree(path.dirname(path.join(root, skill.path)), {
            authorityRoot: root,
            expectedDocumentHash: skill._document_hash,
            skillName: skill.name,
          })
        : null;
      if (!resolved) {
        return {
          ...skill,
          tree_hash: materializedTreeHash,
        };
      }
      return {
        ...skill,
        source: resolved.source_kind,
        source_host: resolved.source_host,
        tree_hash: materializedTreeHash ?? resolved.tree_hash,
        activation: resolved.automatic_on_demand && !skill.activationDeclared ? "on-demand" : skill.activation,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
  warnings.push(...validateSkillDependencies(skills));
  const conflicts = [];
  for (const skill of skills) {
    const ignored = discovered
      .filter((candidate) => candidate.name === skill.name && candidate.path !== skill.path)
      .sort((a, b) => a.priority - b.priority)
      .map((candidate) => candidate.path);
    if (ignored.length > 0) {
      conflicts.push({ name: skill.name, selected: skill.path, ignored });
    }
  }
  const selection = {
      mode: allowed ? "filtered" : "all",
      profile_selection: installSelection?.profileSelection || "auto",
      profiles: installSelection?.profiles || [],
      skill_profiles: installSelection?.skillProfiles || installSelection?.profiles || [],
      explicit_skills: installSelection?.explicitSkills || [],
      external_exposure_skills: (sourcePlan?.entries || [])
        .filter((entry) => entry.automatic_on_demand)
        .map((entry) => entry.name)
        .sort(),
      required_review: installSelection?.requiredReview || {},
      conditional_skills: installSelection?.conditionalSkills || {},
      profile_routing: installSelection?.profileRouting || { version: 1, profiles: {}, escalations: {} },
  };
  const indexedSkills = skills.map(({
    priority,
    warnings: _warnings,
    _document_hash: _documentHash,
    ...skill
  }) => skill);
  const providerIndex = resolveSkillProviderIndex({
    skills: indexedSkills,
    activeProfiles: selection.profiles,
    activeHost: detectActiveHost(process.env),
    profilesRoot: path.join(agentFlowDir, "profiles"),
    registryPath: path.join(agentFlowDir, "skills", "provider-registry.json"),
    authorityRoot: agentFlowDir,
    compatibility,
    adapterRegistry: SKILL_PROVIDER_ADAPTER_REGISTRY,
    authenticateCandidate: (skill) => authenticateIndexedProviderCandidate(
      root,
      agentFlowDir,
      skill,
      sourceByName.get(skill.name) ?? null,
    ),
  });
  const revisionProviderIndex = projectNeutralRevisionProviderIndex(
    root,
    indexedSkills,
    providerIndex,
  );
  const revision = crypto.createHash("sha256").update(JSON.stringify({
    selection,
    ...(compatibility ? { compatibility } : {}),
    provider_registry: {
      version: revisionProviderIndex.version,
      fingerprint: revisionProviderIndex.fingerprint,
      quarantined: revisionProviderIndex.quarantined,
    },
    skill_providers: revisionProviderIndex.claims,
    skills: indexedSkills.map((skill) => ({
      name: skill.name,
      source: skill.source,
      tree_hash: revisionSkillTreeHash(root, skill),
      activation: skill.activation || "on-demand",
      workflowPhases: skill.workflowPhases,
      taskTerms: skill.taskTerms,
      pathGlobs: skill.pathGlobs,
      requires: skill.requires,
    })),
  })).digest("hex");
  return {
    version: 2,
    revision,
    selection: {
      ...selection,
    },
    ...(compatibility ? { compatibility } : {}),
    provider_registry: {
      version: providerIndex.version,
      fingerprint: providerIndex.fingerprint,
      quarantined: providerIndex.quarantined,
    },
    skill_providers: providerIndex.claims,
    skills: indexedSkills,
    conflicts,
    warnings,
  };
}

function revisionSkillTreeHash(root, skill) {
  if (
    !PROJECT_COMMAND_SKILL_NAMES.has(skill.name)
    || skill.source !== "bundled"
  ) return skill.tree_hash;
  const skillPath = path.join(root, skill.path);
  const content = fs.readFileSync(skillPath, "utf8");
  const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  return crypto.createHash("sha256")
    .update(content.replaceAll(launcher, "<project-root>/.agent-flow/bin/agent-flow"))
    .digest("hex");
}
function generatedProjectSkillMarkdown(name, agentFlowCommand = AGENT_FLOW_COMMAND) {
  if (name === "agent-flow") return agentFlowSkillMarkdown(agentFlowCommand);
  if (name === "architecture-reviewer") return architectureReviewerSkillMarkdown();
  if (name === "full-feature-workflow") return fullFeatureSkillMarkdown(agentFlowCommand);
  if (name === "plan-reviewer") return planReviewerSkillMarkdown();
  if (name === "product-brief") return productBriefSkillMarkdown();
  if (name === "push-watch") return pushWatchSkillMarkdown(agentFlowCommand);
  throw new Error(`unsupported generated project skill: ${name}`);
}


function projectCommandSkillMarkdown(name, agentFlowCommand = AGENT_FLOW_COMMAND) {
  if (!PROJECT_COMMAND_SKILL_NAMES.has(name)) {
    throw new Error(`unsupported project command skill: ${name}`);
  }
  return generatedProjectSkillMarkdown(name, agentFlowCommand);
}

function authenticateCanonicalProjectCommandSkill(
  skillRoot,
  expectedCollectionRoot,
  skill,
  concreteId,
  agentFlowCommand = AGENT_FLOW_COMMAND,
) {
  const entries = fs.readdirSync(skillRoot, { withFileTypes: true });
  const expectedDocument = projectCommandSkillMarkdown(concreteId, agentFlowCommand);
  const expectedHash = crypto.createHash("sha256")
    .update(expectedDocument)
    .digest("hex");
  if (
    skill.hash !== expectedHash
    || entries.length !== 1
    || entries[0].name !== "SKILL.md"
    || !entries[0].isFile()
  ) {
    throw new Error("generated project command skill changed");
  }
  const sourceHash = hashSkillTree(skillRoot, {
    authorityRoot: expectedCollectionRoot,
    expectedDocumentHash: skill.hash,
    skillName: concreteId,
  });
  if (sourceHash !== skill.tree_hash) {
    throw new Error("installed provider candidate source hash changed");
  }
  return sourceHash;
}

function authenticateCanonicalGeneratedProjectSkill(
  skillRoot,
  expectedCollectionRoot,
  skill,
  concreteId,
  agentFlowCommand = AGENT_FLOW_COMMAND,
) {
  const entries = fs.readdirSync(skillRoot, { withFileTypes: true });
  const expectedDocument = generatedProjectSkillMarkdown(concreteId, agentFlowCommand);
  const expectedHash = crypto.createHash("sha256")
    .update(expectedDocument)
    .digest("hex");
  if (
    skill.hash !== expectedHash
    || entries.length !== 1
    || entries[0].name !== "SKILL.md"
    || !entries[0].isFile()
  ) {
    throw new Error("generated project skill changed");
  }
  const sourceHash = hashSkillTree(skillRoot, {
    authorityRoot: expectedCollectionRoot,
    expectedDocumentHash: skill.hash,
    skillName: concreteId,
  });
  if (sourceHash !== skill.tree_hash) {
    throw new Error("installed provider candidate source hash changed");
  }
  return sourceHash;
}

function parseCanonicalShellSingleQuote(command) {
  if (
    typeof command !== "string"
    || command.length < 2
    || command[0] !== "'"
    || command.at(-1) !== "'"
  ) return null;
  const escapedQuote = `'"'"'`;
  const inner = command.slice(1, -1);
  let decoded = "";
  for (let offset = 0; offset < inner.length;) {
    if (inner.startsWith(escapedQuote, offset)) {
      decoded += "'";
      offset += escapedQuote.length;
    } else {
      if (inner[offset] === "'") return null;
      decoded += inner[offset];
      offset += 1;
    }
  }
  return shellSingleQuote(decoded) === command ? decoded : null;
}

function indexedAgentFlowLauncherCommand(document) {
  const prefix = "1. From the project root, run `";
  const suffix = " status` for `/agent-flow` with no task";
  const start = document.indexOf(prefix);
  if (start < 0 || document.indexOf(prefix, start + prefix.length) >= 0) return null;
  const commandStart = start + prefix.length;
  const end = document.indexOf(suffix, commandStart);
  if (end < 0) return null;
  const command = document.slice(commandStart, end);
  const launcher = parseCanonicalShellSingleQuote(command);
  if (!launcher || !path.isAbsolute(launcher)) return null;
  return { command, launcher };
}

function relocatedManagedOwnershipEntries(root, skillsRoot, previousIndex) {
  const relocated = new Set();
  try {
    const ownershipEntries = previousIndex?.managed_ownership?.version === 1
      && previousIndex.managed_ownership.entries
      && typeof previousIndex.managed_ownership.entries === "object"
      && !Array.isArray(previousIndex.managed_ownership.entries)
      ? previousIndex.managed_ownership.entries
      : null;
    if (!ownershipEntries) return relocated;
    const candidates = (previousIndex?.skills || []).filter(
      (skill) => portableSkillCasefold(skill?.name) === "agent-flow",
    );
    if (candidates.length !== 1) return relocated;
    const skill = candidates[0];
    const ownership = ownershipEntries["agent-flow"];
    if (
      skill?.name !== "agent-flow"
      || skill.source !== "bundled"
      || (skill.source_host ?? null) !== null
      || skill.path !== ".agent-flow/skills/agent-flow/SKILL.md"
      || typeof skill.hash !== "string"
      || typeof skill.tree_hash !== "string"
      || !ownership
      || typeof ownership !== "object"
      || Array.isArray(ownership)
      || ownership.tree_hash !== skill.tree_hash
      || !ownership.filesystem_identity
    ) return relocated;
    const agentFlowRoot = path.join(skillsRoot, "agent-flow");
    const document = readRegularFileSnapshotNoFollow(
      path.join(agentFlowRoot, "SKILL.md"),
      skillsRoot,
      "relocated agent-flow skill",
    ).bytes.toString("utf8");
    const launcherCommand = indexedAgentFlowLauncherCommand(document);
    if (!launcherCommand) return relocated;
    const priorRoot = path.dirname(path.dirname(path.dirname(launcherCommand.launcher)));
    if (
      path.resolve(path.join(priorRoot, PROJECT_LAUNCHER_RELATIVE))
        !== path.resolve(launcherCommand.launcher)
      || samePath(priorRoot, root)
    ) return relocated;
    authenticateCanonicalProjectCommandSkill(
      agentFlowRoot,
      skillsRoot,
      skill,
      "agent-flow",
      launcherCommand.command,
    );
    for (const [entry, entryOwnership] of Object.entries(ownershipEntries)) {
      if (
        !isPortableSkillName(entry)
        || !entryOwnership
        || typeof entryOwnership !== "object"
        || Array.isArray(entryOwnership)
        || typeof entryOwnership.tree_hash !== "string"
        || !validHostFilesystemIdentity(entryOwnership.filesystem_identity)
      ) continue;
      const current = hostPathState(path.join(skillsRoot, entry));
      if (
        current.kind !== "directory"
        || current.tree_hash !== entryOwnership.tree_hash
        || current.filesystem_identity.links !== entryOwnership.filesystem_identity.links
        || current.filesystem_identity.mode !== entryOwnership.filesystem_identity.mode
      ) continue;
      let canonical = false;
      if (GENERATED_PROJECT_SKILL_NAMES.has(entry)) {
        const matchingSkills = (previousIndex.skills || []).filter(
          (candidate) => candidate?.name === entry
            && candidate.source === "bundled"
            && (candidate.source_host ?? null) === null
            && candidate.path === `.agent-flow/skills/${entry}/SKILL.md`
            && typeof candidate.hash === "string"
            && candidate.tree_hash === entryOwnership.tree_hash,
        );
        if (matchingSkills.length === 1) {
          authenticateCanonicalGeneratedProjectSkill(
            path.join(skillsRoot, entry),
            skillsRoot,
            matchingSkills[0],
            entry,
            launcherCommand.command,
          );
          canonical = true;
        }
      } else {
        const source = hostPathState(path.join(KIT_ROOT, "skills", entry));
        canonical = source.kind === current.kind && source.tree_hash === current.tree_hash;
      }
      if (canonical) relocated.add(entry);
    }
  } catch {
    return new Set();
  }
  return relocated;
}

function authenticateIndexedProviderCandidate(root, agentFlowDir, skill, sourceEntry) {
  if (
    !skill
    || typeof skill.path !== "string"
    || typeof skill.hash !== "string"
    || typeof skill.tree_hash !== "string"
  ) {
    throw new Error("invalid installed provider candidate");
  }
  const concreteId = portableSkillCasefold(skill.name);
  const skillRoot = path.dirname(path.resolve(root, skill.path));
  const projectCommandSkill = (
    PROJECT_COMMAND_SKILL_NAMES.has(concreteId)
    && skill.source === "bundled"
    && sourceEntry === null
  );
  let expectedCollectionRoot;
  if (sourceEntry !== null) {
    if (
      skill.source !== sourceEntry.source_kind
      || (skill.source_host ?? null) !== (sourceEntry.source_host ?? null)
      || (!projectCommandSkill && skill.tree_hash !== sourceEntry.tree_hash)
    ) {
      throw new Error("installed provider candidate source metadata changed");
    }
  }
  const sourceHost = skill.source_host ?? null;
  if (skill.source === "project" && sourceHost === null) {
    expectedCollectionRoot = path.resolve(root, "skills");
  } else if (skill.source === "local" && sourceHost === null) {
    expectedCollectionRoot = path.resolve(agentFlowDir, "local-skills");
  } else if (
    ["bundled", "project-snapshot", "shared"].includes(skill.source)
    && sourceHost === null
  ) {
    expectedCollectionRoot = path.resolve(agentFlowDir, "skills");
  } else if (skill.source === "host-bootstrap" && typeof sourceHost === "string") {
    expectedCollectionRoot = path.resolve(agentFlowDir, "skills");
  } else {
    throw new Error("installed provider candidate source is not registered");
  }
  const requiresCanonicalDirectory = skill.source !== "project" && skill.source !== "local";
  if (
    !samePath(path.dirname(skillRoot), expectedCollectionRoot)
    || (requiresCanonicalDirectory && portableSkillCasefold(path.basename(skillRoot)) !== concreteId)
  ) {
    throw new Error("installed provider candidate source path changed");
  }
  const sourceHash = projectCommandSkill
    ? authenticateCanonicalProjectCommandSkill(
      skillRoot,
      expectedCollectionRoot,
      skill,
      concreteId,
    )
    : hashSkillTree(skillRoot, {
      authorityRoot: expectedCollectionRoot,
      expectedDocumentHash: skill.hash,
      skillName: concreteId,
    });
  if (!projectCommandSkill && sourceHash !== skill.tree_hash) {
    throw new Error("installed provider candidate source hash changed");
  }
  return {
    concrete_id: concreteId,
    source_kind: skill.source,
    source_hash: sourceHash,
    source_host: sourceHost,
    source_locator: sourceHost === null
      ? `project://${skill.path.split(path.sep).join("/")}`
      : `host://${sourceHost}/skills/${concreteId}`,
  };
}

function projectNeutralRevisionProviderIndex(root, skills, providerIndex) {
  const sourceHashes = new Map(
    skills.map((skill) => [skill.name, revisionSkillTreeHash(root, skill)]),
  );
  const claims = providerIndex.claims.map(({
    registry_fingerprint: _registryFingerprint,
    ...claim
  }) => canonicalizeHostNeutralSkillProviderClaim({
    ...claim,
    source_hash: sourceHashes.get(claim.concrete_id) ?? claim.source_hash,
  }));
  const fingerprint = crypto.createHash("sha256")
    .update(canonicalJson({
      claims,
      quarantined: providerIndex.quarantined,
      registry_fingerprint: providerIndex.source_registry_fingerprint,
    }))
    .digest("hex");
  return {
    version: providerIndex.version,
    fingerprint,
    quarantined: providerIndex.quarantined,
    claims: claims.map((claim) => ({
      ...claim,
      registry_fingerprint: fingerprint,
    })),
  };
}


function computeSkillPlanHash(index, root, verifyTrees = false) {
  const skills = (index?.skills || []).map((skill) => {
    const skillPath = path.resolve(root, String(skill.path || ""));
    const relative = path.relative(root, skillPath);
    if (path.basename(skillPath) !== "SKILL.md" || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`invalid installed skill path: ${skill.name}`);
    }
    if (typeof skill.tree_hash !== "string" || !/^[0-9a-f]{64}$/.test(skill.tree_hash)) {
      throw new Error(`installed skill has no whole-tree hash: ${skill.name}`);
    }
    const liveHash = verifyTrees
      ? hashSkillTree(path.dirname(skillPath), {
          authorityRoot: root,
          skillName: skill.name,
        })
      : skill.tree_hash;
    if (verifyTrees && skill.tree_hash !== liveHash) {
      throw new Error(`installed skill snapshot changed: ${skill.name}`);
    }
    const record = [
      skill.name,
      relative.split(path.sep).join("/"),
      skill.source,
      skill.source_host ?? null,
      liveHash,
      [...(skill.profiles || [])].sort(compareCodePoints),
    ];
    if (
      Object.hasOwn(skill, "activation")
      || Object.hasOwn(skill, "taskTerms")
      || Object.hasOwn(skill, "pathGlobs")
    ) {
      record.push({
        activation: skill.activation ?? null,
        workflowPhases: normalizedRoutingHashStrings(skill, "workflowPhases"),
        taskTerms: normalizedRoutingHashStrings(skill, "taskTerms"),
        pathGlobs: normalizedRoutingHashStrings(skill, "pathGlobs"),
      });
    }
    return record;
  }).sort((left, right) => compareCodePoints(left[0], right[0]));
  const compatibility = Object.hasOwn(index || {}, "compatibility")
    ? validateConcreteSkillCompatibility(
      index.compatibility,
      skills.map((skill) => skill[0]),
    )
    : null;
  const providerMetadata = (
    Object.hasOwn(index || {}, "provider_registry")
    || Object.hasOwn(index || {}, "skill_providers")
  )
    ? canonicalizeRuntimeSkillProviderMetadata(index)
    : {};
  const selection = index?.selection || {};
  const normalized = {
    profiles: [...(selection.profiles || [])].sort(compareCodePoints),
    skill_profiles: [...(selection.skill_profiles || [])].sort(compareCodePoints),
    explicit_skills: [...(selection.explicit_skills || [])].sort(compareCodePoints),
    ...(Object.hasOwn(selection, "external_exposure_skills")
      ? { external_exposure_skills: [...selection.external_exposure_skills].sort(compareCodePoints) }
      : {}),
    ...(Object.hasOwn(selection, "profile_selection")
      ? { profile_selection: selection.profile_selection }
      : {}),
    required_review: Object.fromEntries(
      Object.entries(selection.required_review || {})
        .sort(([left], [right]) => compareCodePoints(left, right))
        .map(([profile, names]) => [profile, [...names].sort(compareCodePoints)]),
    ),
    conditional_skills: selection.conditional_skills || {},
    profile_routing: selection.profile_routing || {},
    ...(compatibility ? { compatibility } : {}),
    ...providerMetadata,
    skills,
  };
  return crypto.createHash("sha256").update(JSON.stringify(normalized)).digest("hex");
}

function normalizedRoutingHashStrings(skill, key) {
  const value = skill[key] ?? [];
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    throw new Error(`installed skill has invalid ${key}: ${skill.name}`);
  }
  return [...value].sort(compareCodePoints);
}

function sha256Bytes(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function compareCodePoints(left, right) {
  const first = Array.from(String(left), (character) => character.codePointAt(0));
  const second = Array.from(String(right), (character) => character.codePointAt(0));
  for (let index = 0; index < Math.min(first.length, second.length); index += 1) {
    if (first[index] !== second[index]) return first[index] - second[index];
  }
  return first.length - second.length;
}

function treeIntegrity(root) {
  const entries = [];
  const visit = (current, relative) => {
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      throw new Error(`skill tree integrity root is unsafe: ${current}`);
    }
    entries.push({ path: relative, type: "directory", mode: stat.mode & 0o777 });
    for (const name of fs.readdirSync(current).sort()) {
      const child = path.join(current, name);
      const childRelative = relative ? `${relative}/${name}` : name;
      const childStat = fs.lstatSync(child);
      if (childStat.isSymbolicLink()) {
        throw new Error(`skill tree integrity contains a symlink: ${child}`);
      }
      if (childStat.isDirectory()) {
        visit(child, childRelative);
      } else if (childStat.isFile()) {
        entries.push({
          path: childRelative,
          type: "file",
          mode: childStat.mode & 0o777,
          sha256: crypto.createHash("sha256").update(fs.readFileSync(child)).digest("hex"),
        });
      } else {
        throw new Error(`skill tree integrity contains a special file: ${child}`);
      }
    }
  };
  visit(root, "");
  entries.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return crypto.createHash("sha256")
    .update(JSON.stringify({ version: 1, entries }))
    .digest("hex");
}

function skillLinksCommitment(skillPlanHash, links, version = SKILL_LINKS_COMMITMENT_VERSION) {
  if (typeof skillPlanHash !== "string" || !/^[0-9a-f]{64}$/.test(skillPlanHash)) {
    throw new Error("skill link commitment has an invalid skill plan hash");
  }
  const owned = (links || []).filter((link) => [
    "linked",
    "copied",
    "removed-stale-linked",
    "removed-stale-copied",
  ].includes(link?.status));
  const rows = owned.map((link) => [
    link.name,
    link.host,
    String(link.path).replaceAll("\\", "/"),
    link.status,
    link.tree_integrity ?? null,
    ...(version >= 2 ? [link.filesystem_identity ?? null] : []),
  ]).sort((left, right) => compareCodePoints(JSON.stringify(left), JSON.stringify(right)));
  return crypto.createHash("sha256").update(JSON.stringify({
    version,
    skill_plan_hash: skillPlanHash,
    links: rows,
  })).digest("hex");
}

function validateSkillDependencies(skills) {
  const names = new Set(skills.map((skill) => skill.name));
  const warnings = [];
  for (const skill of skills) {
    for (const required of skill.requires || []) {
      if (!names.has(required)) {
        warnings.push(`${skill.name}: missing required skill ${required}`);
      }
    }
  }
  return warnings;
}

function isProjectKitSourceRoot(root) {
  if (samePath(root, KIT_ROOT)) return true;
  return [
    path.join(root, "bin", "agent-flow-kit.mjs"),
    path.join(root, "lib", "skill-selection.mjs"),
    path.join(root, "profiles", "_schema.yaml"),
    path.join(root, "bootstrap", "agent-flow.md"),
    path.join(root, "src", "agent_flow", "cli.py"),
  ].every((candidate) => {
    try {
      return fs.lstatSync(candidate).isFile();
    } catch {
      return false;
    }
  });
}

function discoverProjectSkills(root) {
  if (isProjectKitSourceRoot(root)) {
    return [];
  }
  return discoverSkills(
    path.join(root, "skills"),
    "project",
    root,
    PROJECT_SKILL_DISCOVERY_IGNORED_NAMES,
  );
}

function discoverSkills(baseDir, source, root, ignoredNames = new Set(), allowedNames = null) {
  if (!fs.existsSync(baseDir)) {
    return [];
  }
  const priority = { local: 0, project: 1, bundled: 2 }[source] ?? 99;
  const skills = [];
  for (const entry of fs.readdirSync(baseDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || ignoredNames.has(entry.name)) {
      continue;
    }
    if (allowedNames && !allowedNames.has(entry.name)) {
      continue;
    }
    const skillRoot = path.join(baseDir, entry.name);
    const document = readSkillDocument(skillRoot, entry.name);
    const skillPath = document.path;
    const text = document.text;
    const metadata = parseSkillMetadata(text, entry.name);
    if (ignoredNames.has(metadata.name)) {
      continue;
    }
    const relativePath = path.relative(root, skillPath);
    skills.push({
      id: metadata.id,
      name: metadata.name,
      provider: metadata.provider,
      provider_id: metadata.provider_id,
      title: metadata.title,
      path: relativePath,
      source,
      hosts: metadata.hosts,
      requires: metadata.requires,
      dependencies: metadata.dependencies,
      optionalDependencies: metadata.optionalDependencies,
      platforms: metadata.platforms,
      stacks: metadata.stacks,
      references: metadata.references,
      hostSupport: metadata.hostSupport,
      workflowPhases: metadata.workflowPhases,
      reviewAngles: metadata.reviewAngles,
      installGroup: metadata.installGroup,
      excludes: metadata.excludes,
      tags: metadata.tags,
      description: metadata.description,
      trigger: metadata.trigger,
      triggers: metadata.triggers,
      activation: metadata.activation,
      activationDeclared: metadata.activationDeclared,
      taskTerms: metadata.taskTerms,
      pathGlobs: metadata.pathGlobs,
      hash: crypto.createHash("sha256").update(text).digest("hex"),
      _document_hash: document.sha256,
      priority,
      warnings: metadata.warnings.map((message) => `${relativePath}: ${message}`),
    });
  }
  return skills;
}

function parseSkillMetadata(text, fallbackName) {
  const frontmatter = splitSkillFrontmatter(text);
  const metadata = frontmatter ? parseSimpleYaml(frontmatter) : {};
  const warnings = [];
  const parsedName = String(metadata.name || fallbackName);
  const name = safeSkillName(parsedName);
  if (name !== parsedName) {
    warnings.push(`unsafe skill name ignored: ${parsedName}`);
  }
  const hostValues = Array.isArray(metadata.hosts) ? metadata.hosts : [];
  const knownHosts = new Set(PROJECT_SKILL_HOSTS);
  const hosts = [];
  for (const host of hostValues) {
    const normalized = String(host).trim().toLowerCase();
    if (knownHosts.has(normalized)) {
      hosts.push(normalized);
    } else if (normalized) {
      warnings.push(`unknown host ignored: ${normalized}`);
    }
  }
  const body = text.replace(/^---\n[\s\S]*?\n---\n?/, "");
  const useWhen = body.split(/\r?\n/).find((line) => /^\s*use when\b/i.test(line));
  return {
    id: String(metadata.id || name),
    name,
    provider: metadata.provider,
    provider_id: metadata.provider_id,
    title: String(metadata.title || ""),
    description: String(metadata.description || useWhen || ""),
    hosts: hostValues.length > 0 ? [...new Set(hosts)] : [...PROJECT_SKILL_HOSTS],
    tags: Array.isArray(metadata.tags) ? metadata.tags.map(String) : [],
    trigger: String(metadata.trigger || metadata.description || useWhen || ""),
    triggers: arrayValue(metadata.triggers),
    activation: ["always", "conditional", "on-demand"].includes(String(metadata.activation))
      ? String(metadata.activation)
      : "on-demand",
    activationDeclared: typeof metadata.activation === "string" && metadata.activation.length > 0,
    taskTerms: arrayValue(metadata.taskTerms),
    pathGlobs: arrayValue(metadata.pathGlobs),
    platforms: arrayValue(metadata.platforms),
    stacks: arrayValue(metadata.stacks),
    dependencies: uniqueStrings([...arrayValue(metadata.dependencies), ...arrayValue(metadata.requires)]),
    requires: uniqueStrings([...skillRequires(name), ...arrayValue(metadata.dependencies), ...arrayValue(metadata.requires)]),
    optionalDependencies: arrayValue(metadata.optionalDependencies),
    references: arrayValue(metadata.references),
    hostSupport: arrayValue(metadata.hostSupport),
    workflowPhases: arrayValue(metadata.workflowPhases),
    reviewAngles: arrayValue(metadata.reviewAngles),
    installGroup: String(metadata.installGroup || ""),
    excludes: arrayValue(metadata.excludes || metadata.conflicts),
    warnings,
  };
}

function skillRequires(name) {
  return SKILL_DEPENDENCIES.get(name) || [];
}

function arrayValue(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

function uniqueStrings(values) {
  return [...new Set(values.map(String).filter(Boolean))];
}

function safeSkillName(value) {
  const candidate = String(value).trim();
  return /^[A-Za-z0-9._-]+$/.test(candidate) && !candidate.startsWith(".") && !candidate.includes("..") && candidate !== "."
    ? candidate
    : String(candidate || "skill")
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-")
        .replace(/^-+|-+$/g, "") || "skill";
}

function removeStaleProjectSkillLinks(root, skills, previousIndex, force = false, transaction = null) {
  if (!previousIndex || !Array.isArray(previousIndex.links)) {
    return [];
  }
  const desired = new Set(skills.flatMap((skill) => skill.hosts.map((host) => `${host}:${skill.name}`)));
  const removed = [];
  for (const link of previousIndex.links) {
    if (!link || !link.name || !link.host || !link.path) {
      continue;
    }
    const key = `${link.host}:${link.name}`;
    if (desired.has(key)) {
      continue;
    }
    const target = path.join(root, link.path);
    // 과거 index는 .codex(소문자) 경로를 기록했다. case-sensitive FS에서
    // ensureChildPath가 .Codex와 어긋나 throw하지 않도록 기록된 casing을 따른다.
    const hostRoot = legacyHostSkillRoot(root, link.path) ?? hostSkillRoot(root, link.host);
    if (pathHasSymlink(root, hostRoot)) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "skipped-host-root-symlink" });
      continue;
    }
    ensureChildPath(hostRoot, target);
    const stat = lstatIfExists(target);
    if (!stat) {
      continue;
    }
    if (link.status === "linked" && (!stat.isSymbolicLink() || link.filesystem_kind !== "symlink")) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-kind-mismatch" });
      continue;
    }
    if (link.status === "copied" && (!stat.isDirectory() || link.filesystem_kind !== "directory")) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-unverified-ownership" });
      continue;
    }
    if (!sameHostFilesystemIdentity(target, link.filesystem_identity)) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-identity-mismatch" });
      continue;
    }
    if (link.status === "linked") {
      if (typeof link.target !== "string") {
        removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-kind-mismatch" });
        continue;
      }
      if (fs.readlinkSync(target) !== link.target) {
        removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-target-mismatch" });
        continue;
      }
      withHostPathMutation(
        transaction,
        target,
        [{ kind: "absent" }],
        (stagedTarget) => fs.unlinkSync(stagedTarget),
      );
      removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale-linked" });
      continue;
    }
    if (link.status !== "copied") {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-unverified-ownership" });
      continue;
    }
    if (
      typeof link.tree_hash !== "string"
      || typeof link.tree_integrity !== "string"
      || hashSkillTree(target) !== link.tree_hash
      || treeIntegrity(target) !== link.tree_integrity
    ) {
      removed.push({ name: link.name, host: link.host, path: link.path, status: "preserved-integrity-mismatch" });
      continue;
    }
    withHostPathMutation(
      transaction,
      target,
      [{ kind: "absent" }],
      (stagedTarget) => fs.rmSync(stagedTarget, { recursive: true, force: true }),
    );
    removed.push({ name: link.name, host: link.host, path: link.path, status: "removed-stale-copied" });
  }
  return removed;
}

function lstatIfExists(pathName) {
  try {
    return fs.lstatSync(pathName);
  } catch (error) {
    if (error && error.code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

function splitSkillFrontmatter(text) {
  if (!text.startsWith("---\n")) {
    return null;
  }
  const end = text.indexOf("\n---\n", 4);
  if (end === -1) {
    return null;
  }
  return text.slice(4, end);
}

function parseSimpleYaml(text) {
  const metadata = {};
  let listKey = null;
  for (const line of text.split(/\r?\n/)) {
    const listItem = line.match(/^\s+-\s*(.+)$/);
    if (listItem && listKey) {
      metadata[listKey].push(listItem[1].trim().replace(/^['"]|['"]$/g, ""));
      continue;
    }
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) {
      listKey = null;
      continue;
    }
    const key = match[1];
    const raw = match[2].trim();
    if (raw.startsWith("[") && raw.endsWith("]")) {
      metadata[key] = raw.slice(1, -1).split(",").map((item) => item.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean);
      listKey = null;
    } else if (raw === "") {
      metadata[key] = [];
      listKey = key;
    } else {
      metadata[key] = raw.replace(/^['"]|['"]$/g, "");
      listKey = null;
    }
  }
  return metadata;
}

function linkProjectSkill(
  root,
  skill,
  host,
  previousIndex,
  force = false,
  transaction = null,
  hostRootOverride = null,
) {
  const srcDir = path.dirname(path.join(root, skill.path));
  const hostRoot = hostRootOverride ?? hostSkillRoot(root, host);
  if (pathHasSymlink(root, hostRoot)) {
    return { name: skill.name, host, path: path.relative(root, hostRoot), status: "skipped-host-root-symlink" };
  }
  const destDir = path.join(hostRoot, skill.name);
  ensureChildPath(hostRoot, destDir);
  const destSkill = path.join(destDir, "SKILL.md");
  const previousLink = previousIndex?.links?.find((link) => (
    link?.name === skill.name && link?.host === host && path.resolve(root, link.path) === destDir
  ));
  let replaceExisting = false;
  if (fs.existsSync(destDir)) {
    const stat = fs.lstatSync(destDir);
    if (stat.isSymbolicLink()) {
      if (
        previousLink?.status !== "linked"
        || previousLink.filesystem_kind !== "symlink"
        || typeof previousLink.target !== "string"
        || fs.readlinkSync(destDir) !== previousLink.target
      ) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-unverified-existing" };
      }
      if (!sameHostFilesystemIdentity(destDir, previousLink.filesystem_identity)) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-identity-mismatch" };
      }
      replaceExisting = true;
    } else if (fs.existsSync(destSkill)) {
      if (previousLink?.status === "linked") {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-kind-mismatch" };
      }
      if (
        previousLink?.status !== "copied"
        || previousLink.filesystem_kind !== "directory"
        || typeof previousLink.tree_hash !== "string"
        || typeof previousLink.tree_integrity !== "string"
      ) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-unverified-existing" };
      }
      if (
        hashSkillTree(destDir) !== previousLink.tree_hash
        || treeIntegrity(destDir) !== previousLink.tree_integrity
      ) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-user-modified" };
      }
      if (!sameHostFilesystemIdentity(destDir, previousLink.filesystem_identity)) {
        return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-identity-mismatch" };
      }
      replaceExisting = true;
    } else if (force) {
      replaceExisting = true;
    } else {
      return { name: skill.name, host, path: path.relative(root, destDir), status: "skipped-existing" };
    }
  }
  fs.mkdirSync(path.dirname(destDir), { recursive: true });
  const relTarget = path.relative(path.dirname(destDir), srcDir);
  const installed = withHostPathMutation(
    transaction,
    destDir,
    [
      { kind: "absent" },
      { kind: "symlink", target: relTarget },
      { kind: "directory", tree_hash: skill.tree_hash },
    ],
    (stagedDest) => {
      if (replaceExisting) fs.rmSync(stagedDest, { recursive: true, force: true });
      try {
        if (process.env.AGENT_FLOW_TEST_FORCE_COPY_HOST_SKILLS === "1") {
          throw new Error("injected host skill copy fallback");
        }
        fs.symlinkSync(relTarget, stagedDest, "dir");
        return {
          name: skill.name,
          host,
          path: path.relative(root, destDir),
          status: "linked",
          filesystem_kind: "symlink",
          target: relTarget,
          tree_hash: skill.tree_hash,
          tree_integrity: treeIntegrity(srcDir),
        };
      } catch {
        copyBundledDirIfMissingOrSame(srcDir, stagedDest, true);
        return {
          name: skill.name,
          host,
          path: path.relative(root, destDir),
          status: "copied",
          filesystem_kind: "directory",
          tree_hash: skill.tree_hash,
          tree_integrity: treeIntegrity(srcDir),
        };
      }
    },
  );
  if (["linked", "copied"].includes(installed?.status)) {
    installed.filesystem_identity = hostFilesystemIdentity(destDir);
    if (installed.status === "copied") installed.tree_integrity = treeIntegrity(destDir);
  }
  return installed;
}

function hostSkillRoot(root, host) {
  // case-sensitive FS에서 .codex/.Codex가 갈라지지 않도록 .Codex로 고정한다.
  if (host === "codex") {
    return path.join(root, ".Codex", "skills");
  }
  if (host === "omp") {
    return path.join(root, ".omp", "skills");
  }
  return path.join(root, `.${host}`, "skills");
}

function legacyHostSkillRoot(root, linkPath) {
  const normalized = String(linkPath).replaceAll("\\", "/");
  if (normalized.startsWith(".codex/skills/")) {
    return path.join(root, ".codex", "skills");
  }
  // gemini/antigravity host는 제거됐지만 과거 index가 기록한 link 정리는
  // 계속돼야 한다. hostSkillRoot로 유도하면 .antigravity/skills처럼 실제
  // 경로와 어긋나 ensureChildPath가 throw하며 install이 중단된다.
  if (normalized.startsWith(".gemini/antigravity/skills/")) {
    return path.join(root, ".gemini", "antigravity", "skills");
  }
  if (normalized.startsWith(".gemini/skills/")) {
    return path.join(root, ".gemini", "skills");
  }
  return null;
}

function readJsonIfExists(pathName) {
  if (!fs.existsSync(pathName)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
}

function ensureChildPath(parent, child) {
  const parentResolved = path.resolve(parent);
  const childResolved = path.resolve(child);
  const relative = path.relative(parentResolved, childResolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`path escapes parent: ${child}`);
  }
}

function pathHasSymlink(root, target) {
  const relative = path.relative(root, target);
  const parts = relative.split(path.sep).filter(Boolean);
  let cursor = root;
  for (const part of parts) {
    cursor = path.join(cursor, part);
    const stat = lstatIfExists(cursor);
    if (stat && stat.isSymbolicLink()) {
      return true;
    }
  }
  return false;
}

function preferredPython() {
  const virtualEnvPython = process.env.VIRTUAL_ENV
    ? path.join(process.env.VIRTUAL_ENV, process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  // HOME이 바뀌면 user-site의 yaml을 잃는 시스템 python 대신 kit 자체 venv를 우선한다.
  const kitVenvPython = path.join(KIT_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const leaderRoot = resolveManagedWorktreeRoot(KIT_ROOT);
  const leaderVenvPython = leaderRoot
    ? path.join(leaderRoot, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    : null;
  const candidates = [
    process.env.PYTHON,
    process.env.PYTHON_EXECUTABLE,
    virtualEnvPython,
    fs.existsSync(kitVenvPython) ? kitVenvPython : null,
    leaderVenvPython && fs.existsSync(leaderVenvPython) ? leaderVenvPython : null,
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
    "python",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const authority = executableCandidateAuthority(candidate);
      const result = safeSpawnAuthenticatedSync(authority, ["--version"], {
        stdio: "ignore",
        timeout: 5_000,
      });
      if (!result.error && result.status === 0 && pythonSupportsWorkflowExport(authority)) {
        return authority.path;
      }
    } catch {
      continue;
    }
  }
  throw new Error("no Python with PyYAML available for workflow export");
}

function executableCandidateAuthority(candidate) {
  const configured = resolveExecutablePath(candidate);
  const resolved = fs.realpathSync(configured);
  const metadata = fs.lstatSync(resolved);
  if (
    !path.isAbsolute(resolved)
    || metadata.isSymbolicLink()
    || !metadata.isFile()
    || metadata.nlink !== 1
    || (process.platform !== "win32" && ![0, process.getuid()].includes(metadata.uid))
    || (metadata.mode & 0o022) !== 0
  ) throw new Error("unsafe executable candidate");
  fs.accessSync(resolved, fs.constants.X_OK);
  return {
    path: configured,
    resolved_path: resolved,
    ...executableIdentity(resolved),
    dependencies: executableDependencyContracts(resolved),
  };
}


function pythonSupportsWorkflowExport(authority) {
  const result = safeSpawnAuthenticatedSync(authority, ["-c", "import yaml"], {
    stdio: "ignore",
    timeout: 5_000,
  });
  return !result.error && result.status === 0;
}

function assertFreshArtifact(state, phase, artifact) {
  if (!artifactIsStale(state, artifact)) {
    return;
  }
  throw new Error(`blocked: stale artifact ${artifact}`);
}

function artifactIsStale(state, artifact) {
  const enteredAt = Date.parse(state.phase_entered_at ?? state.updated_at ?? state.started_at ?? "");
  if (!Number.isFinite(enteredAt)) {
    return false;
  }
  const artifactMtime = fs.statSync(artifact).mtimeMs;
  return artifactMtime < enteredAt;
}

function assertCompletionMarkers(phase, artifact, root) {
  const content = fs.readFileSync(artifact, "utf8");
  const missing = missingMarkersForPhase(content, phase, root);
  if (missing.length > 0) {
    throw new Error(`blocked: ${phase.id} artifact missing completion markers: ${missing.join(", ")}`);
  }
}

function assertDeclaredArtifacts(state, phase, runDir) {
  const issues = declaredArtifactIssues(state, phase, runDir);
  if (issues.length > 0) {
    throw new Error(`blocked: ${issues[0]}`);
  }
}

function declaredArtifactIssues(state, phase, runDir) {
  const records = (phase.artifacts ?? []).slice(1).map((relative) => {
    const artifact = path.join(runDir, relative);
    const exists = fs.existsSync(artifact);
    const metadata = exists ? fs.statSync(artifact) : null;
    return {
      path: relative,
      exists,
      is_file: metadata?.isFile() ?? false,
      mtime_ms: metadata?.mtimeMs,
    };
  });
  return evaluateDeclaredArtifacts(
    phase,
    records,
    state.phase_entered_at ?? state.updated_at ?? state.started_at,
  );
}

function missingMarkers(content, markers) {
  const lines = completionGateLines(content);
  return markers.filter((marker) => {
    const normalized = marker.trim().toLowerCase();
    return !markerPresent(content, lines, normalized);
  });
}

const CODE_REVIEW_LOCAL_SKILL_PHASES = CODE_SKILL_PHASES;
const PROJECT_LOCAL_SKILL_APPLIED_MARKER = "project-local-skill-docs: applied";
const PROJECT_LOCAL_SKILL_INCLUDE_TERMS = [
  "code development",
  "code generation",
  "code review",
  "development or review",
  "developing or reviewing",
  "implementing or reviewing",
  "writing or reviewing",
  "modifying or reviewing",
  "architecture review",
  "android code",
  "kotlin implementation",
  "compose implementation",
  "코드 개발",
  "코드 작성",
  "코드 수정",
  "코드 리뷰",
  "코드리뷰",
  "구현·리뷰",
  "개발/수정/리뷰",
  "작성·리뷰",
];
const PROJECT_LOCAL_SKILL_EXCLUDE_TERMS = [
  "figma",
  "screen-spec",
  "screen spec",
  "design link",
  "figma.com/design",
  "git commit",
  "git push",
  "pull request",
  "pull-request",
  "pr-review",
  "pr review",
  "branch-pr",
  "branch base",
  "branch creation",
  "branch review",
  "release branch",
  "worktree",
  "cleanup",
  "merge cleanup",
  "merge review",
  "release-first",
  "pretooluse",
  "posttooluse",
  "guard-worktree",
  "guard-protected-branch",
  "comment-checker",
  "claude hook",
  "codex hook",
  "agent-flow lifecycle",
  "workflow lifecycle",
];
const PROJECT_LOCAL_SKILL_EXCLUDE_TOKEN_PATTERN = /(^|[^a-z0-9])(pr|branch|merge)([^a-z0-9]|$)/;

function missingMarkersForPhase(content, phase, root, runDir = null, state = null) {
  const missing = missingMarkers(content, phase.required_markers ?? []);
  missing.push(...missingProjectLocalSkillMarkers(content, root, phase.id));
  missing.push(...evaluatePhaseContract(phase, content).issues);
  if (runDir && state) {
    missing.push(...declaredArtifactIssues(state, phase, runDir));
  }
  return missing;
}

function localSkillPromptBlock(root, phaseId) {
  const docs = applicableProjectLocalSkillDocs(root, phaseId);
  if (docs.length === 0) {
    return "";
  }
  return [
    "",
    "",
    "## Project-local code/review skills",
    "",
    "Project-local markdown skill docs that apply to code generation or code review were found.",
    "Read only the applicable docs before completing this phase. Design/Figma, hook, branch, PR, merge, and cleanup skills are intentionally excluded here.",
    "",
    "Applicable docs:",
    "",
    ...docs.map((doc) => localSkillPromptLine(root, doc)),
    "",
    "When this block appears, the `## Completion Gate` must include:",
    "",
    "```text",
    "project-local-skills: checked",
    "project-local-skills-used: <comma-separated applicable skill names>",
    PROJECT_LOCAL_SKILL_APPLIED_MARKER,
    "```",
    "",
    "If this block is absent, `project-local-skills: n/a` remains valid.",
    "",
  ].join("\n");
}

function localSkillPromptLine(root, doc) {
  const absolutePath = path.isAbsolute(doc.path)
    ? doc.path
    : path.join(root, doc.path);
  return `- \`${doc.path}\` (\`${doc.name}\`) — \`${absolutePath}\``;
}

function missingProjectLocalSkillMarkers(content, root, phaseId) {
  const docs = applicableProjectLocalSkillDocs(root, phaseId);
  if (docs.length === 0) {
    return [];
  }
  const values = completionGateMarkerValues(content);
  const missing = [];
  if (values.get("project-local-skills") !== "checked") {
    missing.push("project-local-skills: checked");
  }
  const used = (values.get("project-local-skills-used") ?? "").trim();
  const usedNames = new Set(
    used
      .split(",")
      .map((name) => name.trim().replace(/^`|`$/g, "").toLowerCase())
      .filter(Boolean),
  );
  if (["", "n/a", "none", "optional"].includes(used)) {
    missing.push("project-local-skills-used: <applicable local skill list>");
  } else if (!docs.every((doc) => usedNames.has(doc.name.toLowerCase()))) {
    missing.push("project-local-skills-used: <applicable local skill list>");
  }
  if (values.get("project-local-skill-docs") !== "applied") {
    missing.push(PROJECT_LOCAL_SKILL_APPLIED_MARKER);
  }
  return missing;
}

function applicableProjectLocalSkillDocs(root, phaseId) {
  if (!CODE_REVIEW_LOCAL_SKILL_PHASES.has(phaseId)) {
    return [];
  }
  return projectLocalSkillDocs(root)
    .filter((doc) => isCodeReviewLocalSkill(doc))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function projectLocalSkillDocs(root) {
  const indexDocs = localSkillDocsFromIndex(root);
  if (indexDocs.length > 0) {
    return dedupeLocalSkillDocs(indexDocs);
  }
  return dedupeLocalSkillDocs(localSkillDocsFromTree(root));
}

function localSkillDocsFromIndex(root) {
  const indexPath = path.join(root, ".agent-flow", "skills", "index.json");
  if (!fs.existsSync(indexPath)) {
    return [];
  }
  let payload;
  try {
    payload = JSON.parse(
      readRegularFileSnapshotNoFollow(
        indexPath,
        path.join(root, ".agent-flow"),
        "project-local skill index",
      ).bytes.toString("utf8"),
    );
  } catch (_error) {
    return [];
  }
  if (!Array.isArray(payload.skills)) {
    return [];
  }
  return payload.skills
    .filter((skill) => skill && ["local", "project"].includes(skill.source))
    .filter((skill) => isProjectLocalSkillPath(String(skill.path ?? "")))
    .map((skill) => ({
      name: String(skill.name || path.basename(path.dirname(String(skill.path ?? "")))),
      path: String(skill.path ?? ""),
      description: [
        skill.description,
        skill.trigger,
        ...(Array.isArray(skill.tags) ? skill.tags : []),
        ...(Array.isArray(skill.workflowPhases) ? skill.workflowPhases : []),
        ...(Array.isArray(skill.reviewAngles) ? skill.reviewAngles : []),
      ].filter(Boolean).join(" "),
    }));
}

function localSkillDocsFromTree(root) {
  const base = path.join(root, ".agent-flow", "local-skills");
  if (!fs.existsSync(base)) {
    return [];
  }
  return fs.readdirSync(base, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const skillPath = path.join(base, entry.name, "SKILL.md");
      if (!fs.existsSync(skillPath)) {
        return null;
      }
      return {
        name: entry.name,
        path: path.relative(root, skillPath).split(path.sep).join("/"),
        description: localSkillMetadataText(skillPath),
      };
    })
    .filter(Boolean);
}

function localSkillMetadataText(skillPath) {
  try {
    const text = fs.readFileSync(skillPath, "utf8");
    const frontmatter = text.match(/^---\n([\s\S]*?)\n---/);
    return frontmatter ? frontmatter[1] : text.split(/\r?\n/).slice(0, 20).join("\n");
  } catch (_error) {
    return "";
  }
}

function isProjectLocalSkillPath(relPath) {
  const normalized = relPath.replaceAll("\\", "/");
  return (
    normalized.endsWith("/SKILL.md") &&
    (normalized.startsWith(".agent-flow/local-skills/") || normalized.startsWith("skills/"))
  );
}

function isCodeReviewLocalSkill(doc) {
  const haystack = `${doc.name} ${doc.path} ${doc.description}`.toLowerCase();
  if (
    PROJECT_LOCAL_SKILL_EXCLUDE_TERMS.some((term) => haystack.includes(term)) ||
    PROJECT_LOCAL_SKILL_EXCLUDE_TOKEN_PATTERN.test(haystack)
  ) {
    return false;
  }
  return PROJECT_LOCAL_SKILL_INCLUDE_TERMS.some((term) => haystack.includes(term));
}

function dedupeLocalSkillDocs(docs) {
  const byName = new Map();
  for (const doc of docs) {
    if (!byName.has(doc.name)) {
      byName.set(doc.name, doc);
    }
  }
  return [...byName.values()];
}

function markerPresent(content, gateLines, marker) {
  if (marker.startsWith("#")) {
    return headingPresent(content, marker);
  }
  return gateLines.some((line) => lineMatchesMarker(line, marker));
}

function headingPresent(content, marker) {
  let inFence = false;
  for (const line of content.split(/\r?\n/)) {
    if (line.startsWith("    ") || line.startsWith("\t")) {
      continue;
    }
    const stripped = line.trim();
    const lowered = stripped.toLowerCase();
    if (lowered.startsWith("```") || lowered.startsWith("~~~")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      continue;
    }
    if (lowered.startsWith("#") && lowered === marker) {
      return true;
    }
  }
  return false;
}

function completionGateLines(content) {
  const lines = content.split(/\r?\n/);
  const out = [];
  let inGate = false;
  let inFence = false;
  for (const line of lines) {
    if (line.startsWith("    ") || line.startsWith("\t")) {
      continue;
    }
    const stripped = line.trim();
    const lowered = stripped.toLowerCase();
    if (lowered.startsWith("```") || lowered.startsWith("~~~")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      continue;
    }
    if (lowered.startsWith("#")) {
      const heading = lowered.replace(/^#+/, "").trim();
      if (heading === "completion gate") {
        inGate = true;
        continue;
      }
      if (inGate) {
        break;
      }
    }
    if (inGate) {
      out.push(normalizeCompletionMarkerLine(stripped).toLowerCase());
    }
  }
  return out;
}

function completionGateMarkerValues(content) {
  const values = new Map();
  for (const line of completionGateLines(content)) {
    const separator = line.indexOf(":");
    if (separator !== -1) {
      values.set(line.slice(0, separator).trim(), line.slice(separator + 1).trim());
    }
  }
  return values;
}

function normalizeCompletionMarkerLine(line) {
  let candidate = line.trim();
  if (candidate.startsWith("+")) {
    candidate = candidate.slice(1).trim();
  }
  const lowered = candidate.toLowerCase();
  for (const prefix of ["- [x] ", "- [ ] ", "- ", "* "]) {
    if (lowered.startsWith(prefix)) {
      return candidate.slice(prefix.length).trim();
    }
  }
  return candidate;
}

function lineMatchesMarker(line, marker) {
  if (marker.endsWith(":")) {
    return line.startsWith(marker) && line.slice(marker.length).trim().length > 0;
  }
  const separator = marker.indexOf(":");
  if (separator !== -1 && marker.slice(separator + 1).includes("|")) {
    const lineSeparator = line.indexOf(":");
    if (lineSeparator === -1) {
      return false;
    }
    const lineKey = line.slice(0, lineSeparator).trim();
    const markerKey = marker.slice(0, separator).trim();
    const allowed = marker
      .slice(separator + 1)
      .split("|")
      .map((value) => value.trim())
      .filter(Boolean);
    // n/a 마커는 artifact에서 optional로 써도 같은 비적용 상태로 인정한다.
    if (allowed.includes("n/a")) {
      allowed.push("optional");
    }
    return lineKey === markerKey && allowed.includes(line.slice(lineSeparator + 1).trim());
  }
  return line === marker;
}

function artifactHasFailureMarkers(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  return completionGateLines(content).some((line) => {
    const separator = line.indexOf(":");
    if (separator === -1) {
      return false;
    }
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (value === "fail") {
      return true;
    }
    return key === "missing-required-profile-skills" && !["", "none", "n/a"].includes(value);
  });
}

const FIX_LOOP_MAX_ROUNDS = 3;

function nextPhaseIndex(state, phases, phase, artifact) {
  if (!phase.routes) {
    return state.phase_index + 1;
  }
  const key = nodeRouteKey(phase, artifact, state);
  const target = phase.routes[key] ?? phase.routes.default;
  if (!target) {
    throw new Error(`blocked: ${phase.id} artifact has no route for ${key}`);
  }
  if (target === "block") {
    if (phase.id === "pr-watch" && key === "pending") {
      throw new Error("blocked: PR watch is pending");
    }
    throw new Error(`blocked: ${phase.id} route ${key}`);
  }
  // gates뿐 아니라 fix-loop로 라우팅하는 모든 phase에 같은 상한을 적용한다 (Python runner와 동일).
  if (target === "fix-loop") {
    const rounds = (state.fix_loop_rounds ?? 0) + 1;
    if (rounds > FIX_LOOP_MAX_ROUNDS) {
      throw new Error(`blocked: fix-loop exceeded ${FIX_LOOP_MAX_ROUNDS} rounds — escalate to user`);
    }
  }
  return phaseIndex(phases, target);
}

function syncRouteArtifacts(runDir, phases, currentIndex, nextIndex) {
  if (nextIndex <= currentIndex) {
    for (const phase of phases.slice(nextIndex, currentIndex + 1)) {
      for (const relative of phase.artifacts?.length ? phase.artifacts : [phase.artifact]) {
        const artifact = path.join(runDir, relative);
        if (fs.existsSync(artifact) && fs.statSync(artifact).isFile()) {
          fs.unlinkSync(artifact);
        }
      }
    }
    return;
  }
  if (nextIndex <= currentIndex + 1) {
    return;
  }
  for (const phase of phases.slice(currentIndex + 1, nextIndex)) {
    const artifact = path.join(runDir, phase.artifact);
    if (fs.existsSync(artifact)) {
      continue;
    }
    fs.mkdirSync(path.dirname(artifact), { recursive: true });
    fs.writeFileSync(
      artifact,
      `# ${phase.id}\n\nstatus: skipped\nreason: route_to_${phases[nextIndex].id}\n`,
      "utf8",
    );
  }
}

function nextFixLoopRounds(state, phase, nextPhase) {
  const routesToFixLoop = Boolean(phase.routes) && Object.values(phase.routes).includes("fix-loop");
  if (routesToFixLoop && nextPhase?.id === "fix-loop") {
    return (state.fix_loop_rounds ?? 0) + 1;
  }
  if (phase.id === "gates" && routesToFixLoop && state.fix_loop_rounds !== undefined) {
    return undefined;
  }
  return state.fix_loop_rounds;
}

function nodeRouteKey(phase, artifact, state = {}) {
  const contract = evaluatePhaseContract(phase, fs.readFileSync(artifact, "utf8"));
  if (contract.route === "failure" && phase.routes?.failure) {
    return contract.route;
  }
  if (contract.route === "success" && phase.routes?.success) {
    return contract.route;
  }
  if (phase.id === "gates") {
    return readGatesRouteKey(
      artifact,
      state.workspace_root ?? state.workspace?.workspace_root ?? null,
    );
  }
  if (phase.multi_review) {
    if (readArtifactVerdict(artifact) === "blocked" && phase.routes?.blocked) {
      return "blocked";
    }
    const verdict = readMultiReviewVerdict(artifact, phase.id);
    if (verdict === "approve" || verdict === "request-changes") {
      if (verdict === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return verdict;
    }
    throw new Error("blocked: multi-review artifact must include verdict: approve or verdict: request-changes");
  }
  if (phase.id === "pr-watch") {
    const status = readArtifactStatus(artifact);
    if (["green", "merged", "skipped", "comments", "has_comments", "ci-failed", "ci_failed", "pending", "closed", "error"].includes(status)) {
      return status;
    }
    return "default";
  }
  if (phase.id === "plan-review" || phase.id === "architecture-review" || phase.id === "merge-approval") {
    const verdict = readArtifactVerdict(artifact);
    if (["approve", "request-changes", "blocked"].includes(verdict)) {
      if (verdict === "approve" && phase.routes?.["request-changes"] && artifactHasFailureMarkers(artifact)) {
        return "request-changes";
      }
      return verdict;
    }
    return "default";
  }
  return readArtifactStatus(artifact) ?? readArtifactVerdict(artifact) ?? "default";
}

function phaseIndex(phases, id) {
  const index = phases.findIndex((phase) => phase.id === id);
  if (index === -1) {
    throw new Error(`unknown phase: ${id}`);
  }
  return index;
}

function readArtifactStatus(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const match = content.match(/^status:\s*([a-z_-]+)\s*$/im);
  return match?.[1]?.toLowerCase();
}

function readArtifactVerdict(pathName) {
  const content = fs.readFileSync(pathName, "utf8");
  const match = content.match(/^verdict:\s*([a-z-]+)\s*$/im);
  return match?.[1]?.toLowerCase();
}

function assertMinReviewerCount(pathName, minimum) {
  const content = fs.readFileSync(pathName, "utf8");
  const reviewers = parseReviewerVerdicts(content);
  if (reviewers.size >= minimum) {
    return;
  }
  throw new Error(`blocked: multi-review artifact must contain at least ${minimum} independent reviewer verdicts`);
}

function readMultiReviewVerdict(pathName, phaseId = "") {
  const content = fs.readFileSync(pathName, "utf8");
  const overall = readMultiReviewOverallVerdict(content);
  if (overall && !["approve", "request-changes"].includes(overall)) {
    throw new Error("blocked: multi-review artifact overall verdict must be approve or request-changes");
  }
  const reviewers = parseReviewerVerdicts(content);
  if (reviewers.size < 1) {
    throw new Error("blocked: multi-review artifact must contain at least 1 independent sub-agent reviewer verdict");
  }
  const verdicts = [...reviewers.values()];
  if (overall === "request-changes" || verdicts.includes("request-changes")) {
    return "request-changes";
  }
  if (reviewers.size < 2) {
    throw new Error("blocked: multi-review artifact must contain at least 2 independent sub-agent reviewer verdicts");
  }
  if (overall === "approve" && verdicts.every((verdict) => verdict === "approve")) {
    return "approve";
  }
  throw new Error("blocked: multi-review artifact must include matching reviewer verdicts and overall verdict");
}

function readMultiReviewOverallVerdict(content) {
  // Python runner와 같은 heading alias(Overall/Final [Verdict])를 인정한다.
  const sections = content.split(/^##[ \t]+(?:Overall|Final)(?:[ \t]+Verdict)?[ \t]*$/im);
  if (sections.length < 2) {
    return undefined;
  }
  if (sections.length > 2) {
    return "invalid-verdict";
  }
  const overallBlock = sections[sections.length - 1].split(/^#{1,6}[ \t]+/m, 1)[0] ?? "";
  const verdicts = [...overallBlock.matchAll(/^verdict:\s*([a-z-]+)\s*$/gim)]
    .map((match) => match[1]);
  if (verdicts.length === 0) {
    return undefined;
  }
  if (verdicts.length !== 1) {
    return "invalid-verdict";
  }
  return verdicts[0];
}

function parseReviewerVerdicts(content) {
  // reviewer id를 키로 정규화해 한 reviewer가 여러 번 approve를 찍어도 독립 리뷰로 세지 않는다.
  const reviewers = new Map();
  const stateFor = (reviewerId) => {
    if (!reviewers.has(reviewerId)) {
      reviewers.set(reviewerId, { subagent: false, verdict: undefined });
    }
    return reviewers.get(reviewerId);
  };
  const sourcePattern = /^(reviewer[-_ ]?[a-z0-9-]+)\s+reviewer[-_ ]?source:\s*(.+)$/gim;
  for (const match of content.matchAll(sourcePattern)) {
    const reviewerId = normalizeReviewerId(match[1]);
    if (reviewerId && isSubagentSource(match[2])) {
      stateFor(reviewerId).subagent = true;
    }
  }
  const linePattern = /^reviewer[-_ ]?([a-z0-9-]*)[^\n]*verdict:\s*(approve|request-changes)\s*$/gim;
  for (const match of content.matchAll(linePattern)) {
    const reviewerId = normalizeReviewerId(match[1]);
    if (!reviewerId) {
      continue;
    }
    if (!["approve", "request-changes"].includes(match[2])) {
      continue;
    }
    stateFor(reviewerId).verdict = match[2];
  }
  const sections = content.split(/^##[ \t]+Reviewer[ \t]*([^\n]*)/im);
  for (let index = 1; index < sections.length; index += 2) {
    const reviewerId = normalizeReviewerHeadingId(sections[index]);
    if (!reviewerId) {
      continue;
    }
    const reviewerBlock = sections[index + 1]?.split(/\n##[ \t]+(?:Reviewer|Overall|Final)\b/i, 1)[0] ?? "";
    if (hasSubagentSource(reviewerBlock)) {
      stateFor(reviewerId).subagent = true;
    }
    const verdict = reviewerBlock.match(/^\s*verdict:\s*(approve|request-changes)\s*$/im)?.[1];
    if (verdict && !["approve", "request-changes"].includes(verdict)) {
      continue;
    }
    if (verdict) {
      stateFor(reviewerId).verdict = verdict;
    }
  }
  return new Map(
    [...reviewers.entries()]
      .filter(([, state]) => state.subagent && state.verdict)
      .map(([reviewerId, state]) => [reviewerId, state.verdict])
  );
}

function hasSubagentSource(value) {
  const sourcePattern = /(?:^|\n)\s*reviewer[-_ ]?source\s*:\s*([^\n]+)/gi;
  return [...String(value).matchAll(sourcePattern)].some((match) => isSubagentSource(match[1]));
}

function isSubagentSource(value) {
  const normalized = String(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return [
    "sub agent",
    "subagent",
    "host sub agent",
    "host subagent",
    "active host sub agent",
    "active host subagent",
  ].includes(normalized);
}

function normalizeReviewerId(value) {
  // 섹션 라벨과 종합 verdict는 독립 reviewer id로 세지 않는다.
  const genericLabels = new Set([
    "verdict",
    "verdicts",
    "overall",
    "final",
    "summary",
    "review",
    "reviews",
    "feedback",
    "report",
    "reports",
    "assessment",
    "assessments",
    "analysis",
    "analyses",
    "decision",
    "decisions",
    "conclusion",
    "conclusions",
    "status",
    "statuses",
    "approval",
    "approvals",
    "note",
    "notes",
    "finding",
    "findings",
    "comment",
    "comments",
    "output",
    "outputs",
    "result",
    "results",
    "scope",
    "check",
    "checks",
    "checklist",
    "details",
    "detail",
  ]);
  const key = String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/^reviewer\b/, "")
    .trim();
  if (!key || key.split(/\s+/).some((part) => genericLabels.has(part))) {
    return "";
  }
  return key;
}

function normalizeReviewerHeadingId(value) {
  // 명시적 구분자 뒤의 전문 분야 설명은 reviewer identity에 포함하지 않는다.
  const identity = String(value ?? "").split(/\s+[—–-]\s+|\s*:\s+/, 1)[0];
  // Reviewer heading은 1-2 단어 id(claude, agent 1 등)만 독립 id로 인정한다.
  // 구분자 없는 긴 서술형 heading은 reviewer가 아니라 prose일 가능성이 높아 제외한다.
  const key = normalizeReviewerId(identity);
  return /^[a-z0-9]+(?: [a-z0-9]+)?$/.test(key) ? key : "";
}

function readGatesPassed(pathName, projectRoot = null) {
  return readGatesRouteKey(pathName, projectRoot) === "green";
}

function readGatesRouteKey(pathName, projectRoot = null) {
  try {
    const content = fs.readFileSync(pathName, "utf8");
    const data = JSON.parse(content);
    if (data.verification_mode !== undefined && data.verification_mode !== "full") {
      return data.passed === false ? "request-changes" : "default";
    }
    if (
      data.passed !== undefined
      && typeof data.passed !== "boolean"
    ) return "default";
    if (!Array.isArray(data.results) || data.results.length === 0) {
      if (typeof data.passed === "boolean" && typeof data.status === "string") {
        const status = data.status.trim().toLowerCase().replace(/_/g, "-");
        if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
          return status;
        }
      }
      return data.passed === false ? "request-changes" : "default";
    }
    // 완료 보고는 실제 실행한 gate command와 결과 evidence가 함께 있을 때만 허용한다.
    const requiredResults = data.results.filter((r) => r && r.required !== false);
    const resultsPass =
      requiredResults.length > 0 &&
      requiredResults.every((r) =>
        r &&
        typeof r.command === "string" &&
        r.command.trim().length > 0 &&
        hasGateEvidence(r) &&
        (r.passed === true || r.status === "pass" || r.status === "ok"),
      );
    if (typeof data.status === "string") {
      const status = data.status.trim().toLowerCase().replace(/_/g, "-");
      if (data.passed !== false && ["green", "approve"].includes(status)) {
        return resultsPass && gateArtifactFingerprintMatchesCurrent(pathName, projectRoot)
          ? status
          : "default";
      }
      if (data.passed === false && ["request-changes", "blocked", "error", "pending"].includes(status)) {
        return status;
      }
    }
    if (data.passed === true) {
      return resultsPass && gateArtifactFingerprintMatchesCurrent(pathName, projectRoot)
        ? "green"
        : "default";
    }
    if (data.passed === undefined) return "default";
    return "request-changes";
  } catch {
    return "default";
  }
}

function gateArtifactFingerprintMatchesCurrent(pathName, projectRoot) {
  // Parser-only parity calls omit projectRoot; real workflow routing always supplies it.
  if (projectRoot === null) return true;
  if (typeof projectRoot !== "string" || projectRoot.length === 0) return false;
  const script = [
    "import json,sys",
    "from pathlib import Path",
    "from agent_flow.core.artifacts import gate_fingerprint_matches_current",
    "payload=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))",
    "raise SystemExit(0 if gate_fingerprint_matches_current(Path(sys.argv[1]), payload) else 1)",
  ].join("; ");
  const result = spawnSync(projectPythonPath(), ["-c", script, projectRoot, pathName], {
    cwd: projectRoot,
    encoding: "utf8",
    env: gateFingerprintPythonEnvironment(projectRoot),
    timeout: 30000,
  });
  return result.status === 0;
}

function gateFingerprintPythonEnvironment(projectRoot) {
  const env = { ...process.env };
  const candidates = [
    path.join(projectRoot, ".agent-flow", "runtime", "python"),
    path.join(KIT_ROOT, "src"),
  ].filter((candidate) => fs.existsSync(candidate));
  if (env.PYTHONPATH) candidates.push(env.PYTHONPATH);
  env.PYTHONPATH = candidates.join(path.delimiter);
  return env;
}

function hasGateEvidence(result) {
  for (const key of ["output", "stdout", "stderr", "artifact", "path"]) {
    if (typeof result[key] === "string" && result[key].trim().length > 0) {
      return true;
    }
  }
  for (const key of ["exit_code", "exitCode"]) {
    if (Number.isInteger(result[key]) && result[key] === 0) {
      return true;
    }
  }
  return false;
}

function canonicalAgentFlowBlock() {
  const sourcePath = path.join(KIT_ROOT, "bootstrap", "agent-flow.md");
  const block = fs.readFileSync(sourcePath, "utf8");
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  if (
    countOccurrences(block, start) !== 1
    || countOccurrences(block, end) !== 1
    || block.indexOf(start) > block.indexOf(end)
  ) {
    throw new Error(`invalid canonical agent-flow block: ${sourcePath}`);
  }
  return block;
}

function syncProjectAgentDocuments(root, canonicalBlock = canonicalAgentFlowBlock()) {
  const planned = planProjectAgentDocuments(root, canonicalBlock);
  for (const entry of planned) {
    writeManagedFile(entry.pathName, entry.content, root);
  }
}

function planProjectAgentDocuments(root, canonicalBlock = canonicalAgentFlowBlock()) {
  const paths = [path.join(root, "AGENTS.md"), path.join(root, "CLAUDE.md")];
  return paths.map((pathName) => ({
    pathName,
    content: planBootstrapBlockUpsert(pathName, canonicalBlock),
  }));
}

function syncProjectAgentDocumentsTransactional(root, agentFlowDir) {
  const planned = planProjectAgentDocuments(root);
  const token = crypto.randomBytes(24).toString("hex");
  const transactionRoot = path.join(agentFlowDir, `document-sync-${token}`);
  const transaction = {
    root,
    transactionRoot,
    journalPath: path.join(transactionRoot, "journal.json"),
    token,
    journal: {
      version: 1,
      root: fs.realpathSync(root),
      token,
      stage: "prepared",
      managed_mutations: [],
      host_mutations: [],
    },
  };
  try {
    fs.mkdirSync(transactionRoot);
    transaction.transactionRootIdentity = hostFilesystemIdentity(transactionRoot);
    bindInstallJournalAuthority(
      transaction.journal,
      transaction.transactionRootIdentity,
    );
    writeInstallJournal(transaction.journalPath, transaction.journal);
    for (const entry of planned) snapshotDocumentSyncPath(transaction, entry.pathName);
    activeManagedInstallTransaction = transaction;
    for (const entry of planned) writeManagedFile(entry.pathName, entry.content, root);
    sealManagedInstallMutations(transaction);
    verifyCommittedManagedMutations(root, transaction.journal);
    transaction.journal.stage = "committed";
    writeInstallJournal(transaction.journalPath, transaction.journal);
    removeAuthenticatedTransactionRoot(
      transactionRoot,
      transaction.transactionRootIdentity,
    );
  } catch (error) {
    preflightRecordedRollbackAuthorities(root, transactionRoot, transaction.journal);
    rollbackRecordedManagedMutations(root, transactionRoot, transaction.journal);
    removeAuthenticatedTransactionRoot(
      transactionRoot,
      transaction.transactionRootIdentity,
    );
    throw error;
  } finally {
    if (activeManagedInstallTransaction === transaction) activeManagedInstallTransaction = null;
  }
}

function snapshotDocumentSyncPath(transaction, target) {
  ensureChildPath(transaction.root, target);
  assertNoSymlinkComponents(transaction.root, target);
  const before = managedPathState(target);
  const operation = {
    path: path.relative(transaction.root, target),
    before,
    after: null,
    mutation_count: 0,
    pending: null,
  };
  if (["directory", "file"].includes(before.kind)) {
    const backupRelative = path.join("managed-backups", String(transaction.journal.managed_mutations.length));
    const backupPath = path.join(transaction.transactionRoot, backupRelative);
    fs.mkdirSync(path.dirname(backupPath), { recursive: true });
    fs.cpSync(target, backupPath, { recursive: true, dereference: false, errorOnExist: true, force: false });
    if (!sameManagedPathState(before, managedPathState(backupPath))) {
      throw new Error(`document sync backup integrity mismatch: ${operation.path}`);
    }
    before.backup = backupRelative;
  }
  transaction.journal.managed_mutations.push(operation);
  writeInstallJournal(transaction.journalPath, transaction.journal);
}

function planBootstrapBlockUpsert(pathName, canonicalBlock) {
  const start = "<!-- agent-flow:start -->";
  const end = "<!-- agent-flow:end -->";
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const startCount = countOccurrences(current, start);
  const endCount = countOccurrences(current, end);
  if (startCount !== endCount || startCount > 1) {
    throw new Error(`invalid agent-flow markers: ${pathName}`);
  }
  if (startCount === 1 && current.indexOf(start) > current.indexOf(end)) {
    throw new Error(`invalid agent-flow marker order: ${pathName}`);
  }
  const newline = current.includes("\r\n") ? "\r\n" : "\n";
  const block = canonicalBlock.replace(/\r?\n/g, newline).replace(/(?:\r?\n)+$/, "");
  if (startCount === 1) {
    const before = current.slice(0, current.indexOf(start));
    const after = current.slice(current.indexOf(end) + end.length);
    return `${before}${block}${after}`;
  }
  if (!current) {
    return `${block}${newline}`;
  }
  const separator = current.endsWith(`${newline}${newline}`)
    ? ""
    : current.endsWith(newline) ? newline : `${newline}${newline}`;
  const finalNewline = current.endsWith(newline) ? newline : "";
  return `${current}${separator}${block}${finalNewline}`;
}

function countOccurrences(text, marker) {
  return text.split(marker).length - 1;
}

function upsertGitignore(pathName, entries) {
  const current = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const lines = current.split(/\r?\n/);
  const existing = new Set(lines.map((line) => line.trim()));
  const missing = entries.filter((entry) => !isGitignoreEntryCovered(entry, existing));
  if (missing.length === 0) {
    return;
  }
  const prefix = current.trimEnd();
  const next = `${prefix}${prefix ? "\n" : ""}${missing.join("\n")}\n`;
  writeManagedFile(pathName, next);
}

function removeGitignoreEntries(pathName, entries) {
  if (!fs.existsSync(pathName)) return;
  const removals = new Set(entries);
  const current = fs.readFileSync(pathName, "utf8");
  const lines = current.split(/\r?\n/);
  const filtered = lines.filter((line) => !removals.has(line.trim()));
  if (filtered.length === lines.length) return;
  const next = `${filtered.join("\n").replace(/\n*$/, "")}\n`;
  writeManagedFile(pathName, next);
}

function isGitignoreEntryCovered(entry, existing) {
  if (existing.has(entry)) {
    return true;
  }
  const normalized = entry.replace(/^\/+/, "");
  const parts = normalized.split("/");
  for (let index = 1; index < parts.length; index += 1) {
    const parent = `${parts.slice(0, index).join("/")}/`;
    const parentWithoutSlash = parent.replace(/\/$/, "");
    if (
      existing.has(parent) ||
      existing.has(parentWithoutSlash) ||
      existing.has(`/${parent}`) ||
      existing.has(`/${parentWithoutSlash}`)
    ) {
      return true;
    }
  }
  return false;
}

function bootstrapMarkdown(label, canonicalBlock = canonicalAgentFlowBlock()) {
  return `# ${label} Agent Flow Bootstrap\n\n${canonicalBlock}`;
}

function newRunId() {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `${stamp}-${Math.random().toString(16).slice(2, 10)}`;
}

function fullFeatureWorkflowYaml() {
  return fullFeatureWorkflow().text;
}

function agentFlowSkillMarkdown(command = AGENT_FLOW_COMMAND) {
  return `---
name: agent-flow
description: Runs the project-local agent-flow lifecycle from slash-triggered tasks, status checks, and phase next commands. Use when the user types /agent-flow, asks to start or continue the project workflow, or wants Claude, Codex, or OMP to drive the agent-flow lifecycle.
---

# Agent Flow

Use this skill as the common entry point for the project-local agent-flow workflow.

## Quick start

1. From the project root, run \`${command} status\` for \`/agent-flow\` with no task, or \`${command} run "<task>"\` for \`/agent-flow <task>\`.
2. Treat the command output as the source of truth and follow its \`next_command\`.
3. Do not reinstall agent-flow or infer missing setup unless an agent-flow command exits non-zero with that setup error.

## Slash Trigger

When the user types \`/agent-flow <task>\`, run:

\`\`\`bash
${command} run "<task>"
\`\`\`

Do not reinstall agent-flow for each task. Install is project setup, not the normal task entry.
In a git repo, \`${command} run "<task>"\` starts the run inside \`.agent-flow/worktrees/feat-<slug>/\` on branch \`feat/<slug>\`.

When the user types \`/agent-flow\` with no task:

- Run \`${command} status\` from the project root.
- Treat the status command output as the only source of truth.
- If status exits 0 and reports an active run, follow the \`next_command\` from status.
- If status exits non-zero with \`no active run\`, ask for a task using \`/agent-flow <task>\`.
- Do not infer npm, npx, or install failure unless the command actually exits non-zero with that error.
- Do not run install just because a new session started.

When the user types \`/agent-flow status\`, run:

\`\`\`bash
${command} status
\`\`\`

## Behavior

- Treat \`/agent-flow\` as a project-local workflow trigger, not as a shell path.
- Keep git-project runtime state private under the repository git dir, such as \`.git/agent-flow/worktrees/feat-<slug>/\`; expose it only for status, debugging, or artifact inspection.
- On a new session, always check \`${command} status\` first and continue from that result.
- After a phase writes its artifact, run the \`next_command\` printed by status or the current phase output.
- Run direct build, test, typecheck, and lint commands from the pinned worktree through \`${command} gate -- <command ...>\`; do not run unsandboxed gate launchers.
- For Android gates, run \`${command} gates --run-dir <run-dir>\` first so changed production modules, unit tests, and instrumented tests are selected independently. A targeted pass is recorded separately and cannot advance the workflow; after targeted success on frozen final code, run the same command with \`--full\` exactly once.
- Reuse a successful run-scoped gate result only when its command, Git scope, production/test/dependency/config hashes, toolchain, profile, host, and relevant environment fingerprint match exactly.
- Reviewer sub-agents inspect the existing gate result and fingerprint evidence instead of rerunning test suites. Request only the targeted test needed for a new finding.
- If the workflow pauses for design or slice review, summarize the relevant artifact and wait for user approval before continuing.
- During code generation, modification, and code review phases, apply \`code-generation-discipline\`. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope. Load only the touched profile skill union. If a required local skill is missing, report it and wait for install or explicit override.
- Keep user-facing replies short Korean by default. Keep code, commands, paths, and identifiers in English.
- Do not paste long logs or whole files. Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
}

function fullFeatureSkillMarkdown(command = AGENT_FLOW_COMMAND) {
  return `---\nname: full-feature-workflow\ndescription: Use this skill for feature work in this project.\n---\n\n# Full Feature Workflow\n\nUse this skill for feature work in this project.\n\nAlways drive progress through the runner output. Run \`${command} status\`, then execute the printed \`next_command\` exactly.\n\nDo not skip phases. If existing docs satisfy a phase, write the required artifact and reference those docs. If a gate, review, PR comment, or PR check fails, complete the matching fix phase and push again before merge/handoff.\n\nApply \`code-generation-discipline\` during code and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code. Do not claim completion until review, build, typecheck, lint, and targeted tests are green.\n`;
}

function productBriefSkillMarkdown() {
  return `---\nname: product-brief\ndescription: Use during the full-feature product-brief phase.\n---\n\n# Product Brief\n\nUse during the full-feature product-brief phase.\n\nAsk YC-style forcing questions before implementation:\n\n1. Demand Reality: what behavior proves people want this?\n2. Status Quo: how do they solve it today?\n3. Desperate Specificity: who is the most painful target user?\n4. Narrowest Wedge: what is the smallest version worth using now?\n5. Observation: what concrete user behavior was observed?\n6. Future Fit: why is now the right time?\n\nArtifact template:\n\n# Product Brief\n\n## Mode\nstartup | builder | internal\n\n## Demand Evidence\n\n## Status Quo\n\n## Target User\n\n## Narrowest Wedge\n\n## Observed Behavior\n\n## Why Now\n\n## Cut List\n\n## Assignment\n\n## Decision\nbuild | defer | cut\n`;
}

function planReviewerSkillMarkdown() {
  return `---\nname: plan-reviewer\ndescription: Use during the full-feature plan-review phase.\n---\n\n# Plan Reviewer\n\nUse during the full-feature plan-review phase.\n\nReview only. Do not rewrite the plan.\n\nCheck:\n\n- Missing data collection steps.\n- Missing validation steps.\n- Wrong implementation order.\n- Oversized slices that should be split.\n- Missing state/storage steps.\n- Test coverage gaps.\n- Architecture risks before coding.\n\nArtifact template:\n\n# Plan Review\n\nverdict: approve | request-changes\n\n## Scope Checked\n\n## Missing Steps\n\n## Wrong Order\n\n## Oversized Slices\n\n## Validation Gaps\n\n## Data/State Gaps\n\n## Architecture Risks\n\n## Required Changes\n\n## Approval Notes\n`;
}

function architectureReviewerSkillMarkdown() {
  return `---\nname: architecture-reviewer\ndescription: Use during the full-feature architecture-review phase.\n---\n\n# Architecture Reviewer\n\nUse during the full-feature architecture-review phase.\n\nReview implemented code against domain decisions and DDD/Clean Architecture. Run two independent active-host reviewer sub-agents before approve. Each reviewer section must include \`reviewer-source: sub-agent\`; optional cross-host reviewers are extra evidence and do not replace active-host reviewers.\n\nArtifact template:\n\n# Architecture Review\n\n## Reviewer 1\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Domain Alignment\n\n## Layer Violations\n\n## Repository Boundary Issues\n\n## Dependency Direction Issues\n\n## Required Refactors\n\n## Approved Exceptions\n\n## Reviewer 2\nreviewer-source: sub-agent\nverdict: approve | request-changes\n\n## Findings\n\n## Overall\nverdict: approve | request-changes\n\n## Completion Gate\nskills_checked: true\nprofile-skill-selection: applied\nactive-profiles: <profile list>\nchanged-file-skill-resolution: applied\nrequired-profile-skills: checked\nmissing-required-profile-skills: none|<list>\narchitecture-contract-check: pass|fail|n/a\ncodex-claude-parity-check: pass|fail\nhook-parity-check: pass|fail\nclean-architecture: applied\nproject-local-skills: checked|n/a\nproject-local-skills-used: <skill list or n/a>\ndependency-rule: pass|fail\nusecase-boundary: pass|fail|n/a\nusecase-calls-usecase: pass|fail\nrepository-boundary: pass|fail\ncache-boundary: pass|fail|n/a\nmemory-disk-cache-separated: pass|fail|n/a\nmapping-boundary: pass|fail|n/a\ndto-entity-domain-ui-separated: pass|fail\nsolid-boundary-check: pass|fail\npresentation-skill: android|react|react-native|ios|n/a\npresentation-state-review: pass|fail|n/a\nui-state-modeling: explicit|n/a\npresentation-mapping-boundary: domain-to-uimodel|n/a\ndi-boundary: hilt|context-provider|tsyringe|swift-environment|factory|swift-dependencies|swinject|needle|direct|existing|n/a\n`;
}

function pushWatchSkillMarkdown(command = AGENT_FLOW_COMMAND) {
  return `---\nname: push-watch\ndescription: Use this skill after local verification is complete and the branch is ready to publish.\n---\n\n# Push Watch\n\nUse this skill after local verification is complete and the branch is ready to publish.\n\nRun:\n\n\`\`\`bash\n${command} run push-watch\n\`\`\`\n\nFlow:\n\n1. Sanity check the branch and working tree.\n2. Commit and push the current branch.\n3. Open or record the pull request.\n4. Watch PR checks and review threads.\n5. Route failures through \`pr-comment-fix\` or \`pr-ci-fix\`; comment fixes must also resolve the corresponding GitHub review threads.\n6. Push again and return to \`pr-watch\`.\n7. When checks and comments are green, route to \`merge\`.\n\nRules:\n\n- Protected branches are blocked.\n- Do not merge without explicit approval.\n`;
}

function pushWatchPromptMarkdown() {
  return `# push-watch\n\nCommit, push, open a PR, and start the PR watch loop.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch\`.\n\nDo not run on protected branches. Do not merge without explicit approval.\n`;
}

function pushWatchTickPromptMarkdown() {
  return `# push-watch-tick\n\nPoll the current PR checks and review threads.\n\nUse \`${AGENT_FLOW_COMMAND} run push-watch-tick\`.\n\nWrite \`artifacts/pr-watch.md\` with one status line: \`status: green\`, \`status: comments\`, \`status: ci-failed\`, or \`status: pending\`.\n`;
}

function phasePrompt(phase, root = null) {
  const renderedMarkers = (phase.required_markers ?? []).filter(
    (marker) => !phase.instruction.includes(marker),
  );
  const markers = renderedMarkers.length
    ? `\n\n## Completion markers\n\nThe runner blocks this phase until the artifact includes a \`## Completion Gate\` section with these marker lines:\n\n${renderedMarkers.map((marker) => `- \`${marker}\``).join("\n")}\n`
    : "";
  const localSkillBlock = root ? localSkillPromptBlock(root, phase.id) : "";
  const contract = phase.required_skills?.length || phase.requirements?.length
    ? "\n\n## Phase contract\n\nThe runtime phase packet supplies the exact machine-readable contract after resolving profile skills and dependency closure. Use that single runtime contract line; each requirement value must be `pass` or `fail`.\n"
    : "";
  return `# ${phase.id}\n\n${phase.instruction}${markers}${contract}${localSkillBlock}\n\nSave the required artifact before running:\n\n\`\`\`bash\n${AGENT_FLOW_COMMAND} run advance\n\`\`\`\n`;
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function hookScriptCommand(root, scriptName, host) {
  const scriptPath = path.join(root, ".agent-flow", "scripts", "hooks", scriptName);
  return `${shellQuote(scriptPath)} --host ${shellQuote(host)}`;
}

function unquoteShellWord(value) {
  if (typeof value !== "string") {
    return "";
  }
  if (value.startsWith("'") && value.endsWith("'")) {
    return value.slice(1, -1).replaceAll("'\\''", "'");
  }
  return value;
}

function managedHookScriptName(command) {
  if (typeof command !== "string") return null;
  for (const match of command.matchAll(/(?:^|[\s'"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'"])/g)) {
    const decoded = Buffer.from(match[1], "base64");
    if (decoded.toString("base64") === match[1]) {
      const decodedPath = decoded.toString("utf8");
      if (Buffer.from(decodedPath, "utf8").equals(decoded) && MANAGED_HOOK_SCRIPT_NAMES.includes(path.basename(decodedPath))) {
        return path.basename(decodedPath);
      }
    }
  }
  if (
    command.includes("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH")
    && command.includes("agent-flow-managed-hook-")
    && command.includes("descriptor execution unavailable")
  ) return "__managed-verifier__";
  const normalized = unquoteShellWord(command).replaceAll("\\", "/").replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    if (
      normalized === `.agent-flow/scripts/hooks/${scriptName}` ||
      normalized === `scripts/hooks/${scriptName}` ||
      normalized.endsWith(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.endsWith(`/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/.agent-flow/scripts/hooks/${scriptName}`) ||
      normalized.includes(`/scripts/hooks/${scriptName}`)
    ) {
      return scriptName;
    }
  }
  return null;
}

function legacyManagedHookScriptName(root, command) {
  if (typeof command !== "string") return null;
  const normalizedRoot = path.resolve(root);
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    const legacySource = path.join(normalizedRoot, "scripts", "hooks", scriptName);
    const managedSource = path.join(normalizedRoot, ".agent-flow", "scripts", "hooks", scriptName);
    if (
      command === `scripts/hooks/${scriptName}`
      || command === `.agent-flow/scripts/hooks/${scriptName}`
      || command === shellQuote(legacySource)
      || command === shellQuote(managedSource)
      || command === `cd ${shellQuote(normalizedRoot)} && ${shellQuote(managedSource)}`
    ) return scriptName;
  }
  return null;
}

function trustedManagedHookScriptName(
  root,
  command,
  expectedScriptHashes = null,
  commandRoots = [root],
) {
  const normalizedRoot = path.resolve(root).replaceAll("\\", "/");
  const normalizedCommandRoots = uniqueStrings(
    commandRoots.map((candidate) => path.resolve(candidate).replaceAll("\\", "/")),
  );
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    const expected = `${normalizedRoot}/.agent-flow/scripts/hooks/${scriptName}`;
    const relative = `.agent-flow/scripts/hooks/${scriptName}`;
    if (!normalizedCommandRoots.some(
      (commandRoot) => ["codex", "claude"].some(
        (host) => command === hookScriptCommand(commandRoot, scriptName, host),
      ),
    )) continue;
    const metadata = lstatIfExists(expected);
    if (!metadata?.isFile() || metadata.isSymbolicLink()) continue;
    const expectedSha = expectedScriptHashes instanceof Map
      ? expectedScriptHashes.get(relative)
      : sha256Bytes(fs.readFileSync(expected));
    if (typeof expectedSha === "string" && expectedSha === sha256Bytes(fs.readFileSync(expected))) return scriptName;
  }
  return null;
}

function trustedManagedHookScriptNameAtAnyRoot(command, expectedScriptHashes) {
  if (typeof command !== "string" || !(expectedScriptHashes instanceof Map)) return null;
  for (const scriptName of MANAGED_HOOK_SCRIPT_NAMES) {
    for (const host of ["codex", "claude"]) {
      const suffix = ` --host ${shellQuote(host)}`;
      if (!command.endsWith(suffix)) continue;
      const scriptPath = parseCanonicalShellSingleQuote(command.slice(0, -suffix.length));
      if (!scriptPath || !path.isAbsolute(scriptPath)) continue;
      const normalized = path.resolve(scriptPath);
      const expectedSuffix = path.join(".agent-flow", "scripts", "hooks", scriptName);
      if (!normalized.endsWith(expectedSuffix)) continue;
      const inferredRoot = normalized.slice(0, -expectedSuffix.length).replace(/[\\/]$/, "");
      if (hookScriptCommand(inferredRoot, scriptName, host) !== command) continue;
      const metadata = lstatIfExists(normalized);
      const expectedSha = expectedScriptHashes.get(
        path.join(".agent-flow", "scripts", "hooks", scriptName).split(path.sep).join("/"),
      );
      if (
        metadata?.isFile()
        && !metadata.isSymbolicLink()
        && typeof expectedSha === "string"
        && expectedSha === sha256Bytes(fs.readFileSync(normalized))
      ) return scriptName;
    }
  }
  return null;
}

function managedHostHookSettings(root, host) {
  const commands = (scripts) => scripts.map((scriptName) => ({
    type: "command",
    command: hookScriptCommand(root, scriptName, host),
  }));
  return {
    hooks: {
      PreToolUse: [
        {
          matcher: "Bash",
          hooks: commands(CANONICAL_HOOK_POLICY.bashPre),
        },
        {
          matcher: WRITE_TOOL_MATCHER,
          hooks: commands(CANONICAL_HOOK_POLICY.writePre),
        },
      ],
      PostToolUse: [
        {
          matcher: WRITE_TOOL_MATCHER,
          hooks: commands(CANONICAL_HOOK_POLICY.writePost),
        },
      ],
      Stop: [
        {
          hooks: commands(CANONICAL_HOOK_POLICY.stop),
        },
      ],
    },
  };
}

function codexHooksSettings(root) {
  return managedHostHookSettings(root, "codex");
}

function mergeHookSettings(settings, desired) {
  if (!settings.hooks) {
    settings.hooks = {};
  }
  for (const [event, entries] of Object.entries(desired)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = [];
    }
    for (const entry of entries) {
      const existing = settings.hooks[event].find((e) => (e.matcher ?? "") === (entry.matcher ?? ""));
      if (existing) {
        if (!existing.hooks) {
          existing.hooks = [];
        }
        for (const hook of entry.hooks) {
          if (!existing.hooks.some((candidate) => candidate.command === hook.command)) {
            existing.hooks.push(hook);
          }
        }
      } else {
        settings.hooks[event].push(entry);
      }
    }
  }
}

function readHookSettings(settingsPath) {
  if (!fs.existsSync(settingsPath)) {
    return {};
  }
  try {
    return JSON.parse(fs.readFileSync(settingsPath, "utf8"));
  } catch {
    const backupPath = `${settingsPath}.bak`;
    fs.copyFileSync(settingsPath, backupPath);
    console.error(`warning: could not parse ${settingsPath}; backed up to ${backupPath} before overwriting`);
    return {};
  }
}

function mergeHookConfig(settings, source) {
  if (!source || typeof source !== "object") {
    return;
  }
  for (const [key, value] of Object.entries(source)) {
    if (key !== "hooks" && settings[key] === undefined) {
      settings[key] = value;
    }
  }
  if (source.hooks) {
    mergeHookSettings(settings, source.hooks);
  }
}

function removeManagedHookCommands(
  settings,
  expectedScriptHashes,
  root = null,
  commandRoots = [],
) {
  if (!settings.hooks || typeof settings.hooks !== "object" || Array.isArray(settings.hooks)) {
    return;
  }
  for (const [event, entries] of Object.entries(settings.hooks)) {
    if (!Array.isArray(entries)) continue;
    settings.hooks[event] = entries.filter((entry) => {
      if (!entry || typeof entry !== "object" || !Array.isArray(entry.hooks)) return true;
      entry.hooks = entry.hooks.filter((hook) => {
        const trustedAtInstalledRoot = root && trustedManagedHookScriptName(
          root,
          hook?.command,
          expectedScriptHashes,
          commandRoots,
        );
        return !trustedAtInstalledRoot
          && !(root && legacyManagedHookScriptName(root, hook?.command))
          && !trustedManagedHookScriptNameAtAnyRoot(
            hook?.command,
            expectedScriptHashes,
          );
      });
      return entry.hooks.length > 0;
    });
  }
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function tomlBasicString(value) {
  return String(value).replaceAll("\\", "\\\\").replaceAll("\"", "\\\"");
}

function upsertTomlValue(text, tableHeader, key, value) {
  const tableName = tableHeader.slice(1, -1);
  const tablePattern = new RegExp(`(^|\\n)\\s*\\[\\s*${escapeRegex(tableName)}\\s*\\]\\s*(?:#.*)?\\n([\\s\\S]*?)(?=\\n\\s*\\[[^\\n]+\\]|$)`);
  const keyPattern = new RegExp(`(^|\\n)\\s*${escapeRegex(key)}\\s*=.*(?=\\n|$)`);
  const match = text.match(tablePattern);
  if (!match) {
    const prefix = text.trim() ? `${text.replace(/\n*$/, "\n\n")}` : "";
    return `${prefix}${tableHeader}\n${key} = ${value}\n`;
  }
  return text.replace(tablePattern, (full, leading, body) => {
    const nextBody = keyPattern.test(body)
      ? body.replace(keyPattern, `$1${key} = ${value}`)
      : `${body.replace(/\n*$/, "")}\n${key} = ${value}\n`;
    return `${leading}${tableHeader}\n${nextBody}`;
  });
}

function codexConfigPath() {
  if (!HOME) {
    return null;
  }
  return path.join(HOME, ".codex", "config.toml");
}

function upsertCodexConfigTableValue(tableHeader, key, value) {
  const configPath = codexConfigPath();
  if (!configPath) {
    return false;
  }
  const current = fs.existsSync(configPath) ? fs.readFileSync(configPath, "utf8") : "";
  const next = upsertTomlValue(current, tableHeader, key, value);
  if (next === current) {
    return true;
  }
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  fs.writeFileSync(configPath, next.endsWith("\n") ? next : `${next}\n`, "utf8");
  return true;
}

function resolveCodexBinary() {
  const candidates = [
    process.env.CODEX_CLI_PATH,
    "/Applications/Codex.app/Contents/Resources/codex",
    "codex",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = spawnSync(candidate, ["--version"], { encoding: "utf8", timeout: 3000 });
    if (!result.error && result.status === 0) {
      return candidate;
    }
  }
  return null;
}

function queryCodexProjectHookHashes(root) {
  const codexBinary = resolveCodexBinary();
  if (!codexBinary) {
    return [];
  }
  const helper = String.raw`
const { spawn } = require("child_process");
const codexBinary = process.argv[1];
const root = process.argv[2];
const responses = [];
let finished = false;
let stdoutBuffer = "";
const proc = spawn(codexBinary, ["app-server", "--stdio"], { stdio: ["pipe", "pipe", "pipe"] });
proc.stdout.on("data", (chunk) => {
  stdoutBuffer += chunk.toString();
  let newlineIndex;
  while ((newlineIndex = stdoutBuffer.indexOf("\n")) !== -1) {
    handleLine(stdoutBuffer.slice(0, newlineIndex));
    stdoutBuffer = stdoutBuffer.slice(newlineIndex + 1);
  }
});
proc.stderr.on("data", () => {});
function handleLine(line) {
  const trimmed = line.trim();
  if (!trimmed) {
    return;
  }
  try {
    const response = JSON.parse(trimmed);
    responses.push(response);
    if (response.id === 2) {
      finish(response);
    }
  } catch {
    // Ignore non-JSON app-server output.
  }
}
function send(id, method, params) {
  proc.stdin.write(JSON.stringify({ id, method, params }) + "\n");
}
setTimeout(() => {
  send(1, "initialize", {
    clientInfo: { name: "agent-flow-install", title: null, version: "1" },
    capabilities: { experimentalApi: true, requestAttestation: false },
  });
  setTimeout(() => send(2, "hooks/list", { cwds: [root] }), 250);
}, 50);
function finish(response) {
  if (finished) {
    return;
  }
  finished = true;
  if (!response || response.error) {
    proc.kill("SIGTERM");
    process.exit(1);
  }
  const entry = response.result?.data?.find((item) => item.cwd === root);
  const sourcePaths = new Set([root + "/.Codex/hooks.json", root + "/.codex/hooks.json"]);
  const hooks = (entry?.hooks ?? [])
    .filter((hook) => sourcePaths.has(hook.sourcePath) && hook.key && hook.currentHash)
    .map((hook) => ({ key: hook.key, trustedHash: hook.currentHash, command: hook.command ?? "" }));
  console.log(JSON.stringify(hooks));
  proc.kill("SIGTERM");
}
const timer = setTimeout(() => finish(responses.find((item) => item.id === 2)), 3000);
proc.on("exit", () => {
  if (stdoutBuffer.trim()) {
    handleLine(stdoutBuffer);
    stdoutBuffer = "";
  }
  clearTimeout(timer);
  if (!finished) {
    finish(responses.find((item) => item.id === 2));
  }
});
`;
  const result = spawnSync(process.execPath, ["-e", helper, codexBinary, root], {
    encoding: "utf8",
    timeout: 8000,
  });
  if (result.error || result.status !== 0) {
    return [];
  }
  try {
    const parsed = JSON.parse(result.stdout.trim());
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function installCodexTrustState(root) {
  if (process.env.AGENT_FLOW_SKIP_CODEX_TRUST === "1") {
    return;
  }
  const projectHeader = `[projects."${tomlBasicString(root)}"]`;
  if (!upsertCodexConfigTableValue(projectHeader, "trust_level", "\"trusted\"")) {
    console.error("warning: Codex project trust not registered; HOME is unavailable");
    return;
  }
  const hooks = queryCodexProjectHookHashes(root);
  const managedHooks = hooks.filter((hook) => trustedManagedHookScriptName(root, hook.command));
  if (managedHooks.length === 0) {
    console.error("warning: Codex hook trust not registered; codex app-server did not return project hooks");
    return;
  }
  for (const hook of managedHooks) {
    const hookHeader = `[hooks.state."${tomlBasicString(hook.key)}"]`;
    upsertCodexConfigTableValue(hookHeader, "trusted_hash", `"${tomlBasicString(hook.trustedHash)}"`);
  }
}

function managedHookScriptHashes(root) {
  return new Map(MANAGED_HOOK_SCRIPT_NAMES.map((scriptName) => {
    const relative = path.join(".agent-flow", "scripts", "hooks", scriptName)
      .split(path.sep)
      .join("/");
    return [
      relative,
      sha256Bytes(fs.readFileSync(path.join(root, relative))),
    ];
  }));
}


function installCodexHooks(root, commandRoots = [root]) {
  const settingsPaths = [
    path.join(root, ".Codex", "hooks.json"),
    path.join(root, ".codex", "hooks.json"),
  ];
  const settings = {};
  for (const settingsPath of settingsPaths) {
    mergeHookConfig(settings, readHookSettings(settingsPath));
  }
  removeManagedHookCommands(settings, managedHookScriptHashes(root), root, commandRoots);
  mergeHookSettings(settings, codexHooksSettings(root).hooks);
  for (const settingsPath of settingsPaths) {
    writeManagedFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
  }
}

function claudeHooksSettings(root) {
  return managedHostHookSettings(root, "claude");
}

function installClaudeHooks(root, commandRoots = [root]) {
  const settingsPath = path.join(root, ".claude", "settings.json");
  const settings = readHookSettings(settingsPath);
  removeManagedHookCommands(settings, managedHookScriptHashes(root), root, commandRoots);
  mergeHookSettings(settings, claudeHooksSettings(root).hooks);
  writeManagedFile(settingsPath, `${JSON.stringify(settings, null, 2)}\n`);
}
function ompHooksExtensionSource(root) {
  return String.raw`import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const HOOK_DIR = path.join(ROOT, ".agent-flow", "scripts", "hooks");
const WRITE_TOOL_RE = new RegExp(${JSON.stringify(WRITE_TOOL_MATCHER)}, "i");
const BASH_PRE_HOOKS = Object.freeze(${JSON.stringify(CANONICAL_HOOK_POLICY.bashPre)});
const WRITE_PRE_HOOKS = Object.freeze(${JSON.stringify(CANONICAL_HOOK_POLICY.writePre)});
const WRITE_POST_HOOKS = Object.freeze(${JSON.stringify(CANONICAL_HOOK_POLICY.writePost)});
const STOP_HOOKS = Object.freeze(${JSON.stringify(CANONICAL_HOOK_POLICY.stop)});

export default function agentFlowHooks(pi) {
  if (typeof pi.setLabel === "function") {
    pi.setLabel("agent-flow hooks");
  }


  pi.on("context", async (event) => {
    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const filtered = messages.filter((message) => {
      if (message?.customType === "agent-flow-model-context" || message?.details?.source === "agent-flow-omp-model-context") {
        return false;
      }
      if (message?.role === "user") {
        return true;
      }
      const text = messageText(message).trim();
      return !(text.startsWith("<context>") && text.endsWith("</context>") && /<file\b[^>]*\bsource="agent-flow-omp-model-context"/.test(text));
    });
    if (filtered.length !== messages.length) {
      return { messages: filtered };
    }
  });
  pi.on("tool_call", async (event, ctx) => {
    const bashTool = isBashTool(event?.toolName);
    if (bashTool) {
      const payload = hookPayload(event, ctx);
      for (const scriptName of BASH_PRE_HOOKS) {
        const result = await runHook(scriptName, payload, ctx);
        if (result.block) {
          return { block: true, reason: result.reason };
        }
      }
      forwardOmpExecutionIdentity(event, ctx);
    }
    if (WRITE_TOOL_RE.test(String(event?.toolName || "")) && !bashTool) {
      const payload = hookPayload(event, ctx);
      for (const scriptName of WRITE_PRE_HOOKS) {
        const result = await runHook(scriptName, payload, ctx);
        if (result.block) {
          return { block: true, reason: result.reason };
        }
      }
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    if (!WRITE_TOOL_RE.test(String(event?.toolName || ""))) {
      return;
    }
    const syncError = syncRootContextFiles(event, ctx);
    if (syncError) {
      return {
        content: [{ type: "text", text: syncError }],
        details: { agentFlowHook: "sync-root-context" },
        isError: true,
      };
    }
    const payload = hookPayload(event, ctx);
    for (const scriptName of WRITE_POST_HOOKS) {
      const result = await runHook(scriptName, payload, ctx);
      if (result.block) {
        return {
          content: [{ type: "text", text: result.reason }],
          details: { agentFlowHook: scriptName },
          isError: true,
        };
      }
    }
  });

  pi.on("session_shutdown", (_event, ctx) => {
    setTimeout(() => {
      void runHook(STOP_HOOKS[0], hookPayload({ type: "session_shutdown" }, ctx), ctx)
        .then((result) => {
          const message = parseSystemMessage(result.reason);
          if (message && ctx?.hasUI && typeof ctx.ui?.notify === "function") {
            return ctx.ui.notify(message, "info");
          }
          return undefined;
        })
        .catch(() => {});
    }, 0);
  });
}


function hookPayload(event, ctx) {
  const rawInput = event?.input || {};
  const toolName = String(event?.toolName || "");
  const guardInput = normalizeGuardInput(toolName, rawInput);
  const baseCwd = ctx?.cwd || ROOT;
  let cwd = baseCwd;
  const declaredCwd = guardInput && typeof guardInput.cwd === "string" ? guardInput.cwd : "";
  if (declaredCwd) {
    cwd = path.isAbsolute(declaredCwd) ? declaredCwd : path.resolve(baseCwd, declaredCwd);
  }
  return {
    host: "omp",
    session_id: String(ctx?.sessionManager?.getSessionId?.() || ctx?.sessionId || ""),
    agent_id: String(ctx?.agentId || ""),
    tool_name: toolName,
    tool: toolName,
    hook_event_name: String(event?.type || ""),
    tool_input: guardInput,
    input: guardInput,
    parameters: guardInput,
    cwd,
  };
}

const XD_FILE_MUTATORS = {
  ast_edit(args) {
    const items = args && Array.isArray(args.paths) ? args.paths : [];
    return items.filter((item) => typeof item === "string" && item);
  },
};

function xdDeviceName(value) {
  const match = /^xd:\/\/([A-Za-z0-9_.-]+)/.exec(String(value || ""));
  return match ? match[1] : "";
}

function normalizeGuardInput(toolName, input) {
  if (!input || typeof input !== "object") {
    return input || {};
  }
  const device = xdDeviceName(typeof input.path === "string" ? input.path : "");
  if (!device) {
    return input;
  }
  const extractor = XD_FILE_MUTATORS[device];
  if (!extractor) {
    return input;
  }
  let args = {};
  try {
    args = typeof input.content === "string" ? JSON.parse(input.content) : (input.content || {});
  } catch {
    return input;
  }
  const paths = extractor(args);
  if (!Array.isArray(paths) || paths.length === 0) {
    return input;
  }
  return { paths };
}

function forwardOmpExecutionIdentity(event, ctx) {
  const input = event?.input;
  if (!input || typeof input.command !== "string" || !input.command.trim()) {
    return;
  }
  const sessionId = String(ctx?.sessionManager?.getSessionId?.() || ctx?.sessionId || "").trim();
  if (!sessionId) {
    return;
  }
  const agentId = String(ctx?.agentId || "").trim();
  const assignments = [
    "AGENT_FLOW_ACTIVE_HOST=" + shellQuote("omp"),
    "AGENT_FLOW_EXECUTION_ID=" + shellQuote(sessionId),
    "AGENT_FLOW_AGENT_ID=" + shellQuote(agentId),
  ].join(" ");
  input.command = "export " + assignments + "; " + input.command;
}

function shellQuote(value) {
  return "'" + String(value).replaceAll("'", "'\\''") + "'";
}

function messageText(message) {
  const content = message?.content;
  if (typeof content === "string") {
    return content;
  }
  if (!Array.isArray(content)) {
    return "";
  }
  return content.map((part) => typeof part?.text === "string" ? part.text : "").join("\n");
}


function pathExists(filePath) {
  try {
    fs.statSync(filePath);
    return true;
  } catch {
    return false;
  }
}


function syncRootContextFiles(event, ctx) {
  const direction = rootContextSyncDirection(event, ctx);
  if (!direction) {
    return "";
  }
  try {
    const content = fs.readFileSync(direction.sourcePath, "utf8");
    const current = pathExists(direction.destPath) ? fs.readFileSync(direction.destPath, "utf8") : "";
    if (current !== content) {
      fs.writeFileSync(direction.destPath, content, "utf8");
    }
    return "";
  } catch (error) {
    return "agent-flow hook failed to sync " + direction.sourceName + " to " + direction.destName + ": " + String(error?.message || error);
  }
}

function rootContextSyncDirection(event, ctx) {
  const changed = modifiedRootContextFiles(event?.input, ctx?.cwd || ROOT);
  if (changed.has("CLAUDE.md")) {
    return {
      sourceName: "CLAUDE.md",
      destName: "AGENTS.md",
      sourcePath: path.join(ROOT, "CLAUDE.md"),
      destPath: path.join(ROOT, "AGENTS.md"),
    };
  }
  if (changed.has("AGENTS.md")) {
    return {
      sourceName: "AGENTS.md",
      destName: "CLAUDE.md",
      sourcePath: path.join(ROOT, "AGENTS.md"),
      destPath: path.join(ROOT, "CLAUDE.md"),
    };
  }
  return null;
}

function modifiedRootContextFiles(input, cwd) {
  const changed = new Set();
  for (const filePath of collectModifiedPaths(input)) {
    const fileName = rootContextFileName(filePath, cwd);
    if (fileName) {
      changed.add(fileName);
    }
  }
  return changed;
}

function collectModifiedPaths(input) {
  const paths = [];
  const visit = (value) => {
    if (typeof value === "string") {
      paths.push(...pathsFromPatch(value));
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        visit(item);
      }
      return;
    }
    if (!value || typeof value !== "object") {
      return;
    }
    for (const key of ["file_path", "filePath", "path", "filename"]) {
      if (typeof value[key] === "string") {
        paths.push(value[key]);
      }
    }
    for (const key of ["patch", "command"]) {
      if (typeof value[key] === "string") {
        paths.push(...pathsFromPatch(value[key]));
      }
    }
    if (Array.isArray(value.edits)) {
      visit(value.edits);
    }
  };
  visit(input);
  return paths;
}

function pathsFromPatch(text) {
  if (!text.includes("CLAUDE.md") && !text.includes("AGENTS.md")) {
    return [];
  }
  const paths = [];
  for (const line of text.split(/\r?\n/)) {
    const tagged = line.match(/^\[([^#\]\r\n]+)#[0-9A-Fa-f]+\]$/);
    if (tagged) {
      paths.push(tagged[1]);
      continue;
    }
    const unified = line.match(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/);
    if (unified) {
      paths.push(unified[1].trim());
    }
  }
  return paths;
}

function rootContextFileName(filePath, cwd) {
  const resolved = path.resolve(cwd || ROOT, filePath);
  for (const fileName of ["CLAUDE.md", "AGENTS.md"]) {
    if (samePath(resolved, path.join(ROOT, fileName))) {
      return fileName;
    }
  }
  return "";
}

function samePath(left, right) {
  return path.resolve(left) === path.resolve(right);
}

function isBashTool(toolName) {
  return /^(Bash|bash)$/.test(String(toolName || ""));
}

async function runHook(scriptName, payload, ctx) {
  const scriptPath = path.join(HOOK_DIR, scriptName);
  const result = await spawnHook(scriptPath, JSON.stringify(payload), ctx?.cwd || ROOT);
  const reason = (result.stderr || result.stdout || "").trim();
  if (result.status === 0) {
    return { block: false, reason };
  }
  return { block: true, reason: reason || "agent-flow hook blocked: " + scriptName };
}

function spawnHook(scriptPath, input, cwd) {
  return new Promise((resolve) => {
    const proc = spawn(
      scriptPath,
      [],
      { cwd, stdio: ["pipe", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      try {
        proc.kill("SIGTERM");
      } catch {
      }
      finish({ status: 124, stdout, stderr: stderr || "agent-flow hook timed out" });
    }, 8000);
    proc.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    proc.on("error", (error) => {
      finish({ status: 2, stdout: "", stderr: "agent-flow hook failed to start: " + String(error?.message || error) });
    });
    proc.on("close", (status) => {
      finish({ status: status ?? 2, stdout, stderr });
    });
    proc.stdin.end(input);
  });
}

function parseSystemMessage(text) {
  if (!text) {
    return "";
  }
  try {
    const parsed = JSON.parse(text);
    return String(parsed.systemMessage || "");
  } catch {
    return text;
  }
}
`;
}

function installOmpHooks(root) {
  return writeManagedFileIfMissingOrSame(
    path.join(root, ".omp", "extensions", "agent-flow-hooks.ts"),
    ompHooksExtensionSource(root),
    forceManaged,
  );
}

function makeHooksExecutable(root) {
  const hooksDir = path.join(root, ".agent-flow", "scripts", "hooks");
  if (!fs.existsSync(hooksDir)) {
    return;
  }
  withManagedInstallMutation(hooksDir, (managedHooksDir) => {
    for (const entry of fs.readdirSync(managedHooksDir)) {
      if (entry.endsWith(".sh") || entry === "comment-checker.py" || entry === "guard-worktree-write.py") {
        fs.chmodSync(path.join(managedHooksDir, entry), 0o755);
      }
    }
  });
}

function workflowContract() {
  return `# Workflow Contract

The workflow runner is the source of truth for phase order. Agents may read skills and prompts, but must follow the runner's printed \`next_command\` exactly to move through the workflow.

Phases with completion markers are not complete just because the artifact file exists. The artifact must include every required marker printed by the current phase or status output.

Implementation rules:

- Run every phase through the runner. Do not skip review, QA, PR watch, or fix-loop phases.
- Apply \`code-generation-discipline\` during red, green, refactor, fix-loop, and review phases. Resolve required skills from active profile metadata, installed skill index, changed files, and task scope before writing or judging code.
- If review or QA fails, return to the fix phase before continuing.
- Required review happens before completion QA. After reviewer approve, gates run the configured profile checks. CI/CD owns lint/static/check commands; managed local profile gates keep agent-flow architecture checks and build/test checks only. If review or QA fails, fix-loop routes back through comment-authoring and review before gates run again.
- Reviewer sub-agents inspect existing gate-result and fingerprint evidence instead of rerunning test suites. A new finding may request only its targeted regression test.
- Android gates separate changed production modules, unit tests, and instrumented tests. Targeted success is non-terminal; only full Android success on frozen final code may advance the workflow.
- Reuse successful run-scoped gates only when command, Git scope, code/test/dependency/config hashes, toolchain, profile, host, and relevant environment fingerprints match exactly.
- Code review requires at least two active-host sub-agents (Codex sub-agent in Codex, Claude sub-agent in Claude, OMP sub-agent in OMP). If the changed scope spans multiple areas, run one additional active-host sub-agent in parallel. Additional non-host providers are optional, and every multi-review verdict requires 2+ independent sub-agent reviewer verdicts with reviewer-source: sub-agent. After recording each sub-agent result, close that sub-agent session. End multi-review artifacts with ## Overall followed by exactly one verdict line: verdict: approve or verdict: request-changes.
- In the default workflow, gates run as their own phase after final-review approve.

Document size rules:

- \`CONTEXT.md\`, domain-grill outputs, compact domain maps, and long planning docs must stay under 200 lines each.
- If a source doc grows past 200 lines, create or refresh a matching \`*-summary.md\` under \`.Codex/rules/\` and use that summary as agent context.
- Preserve the original long doc only as reference; do not load it as hot context unless the current phase needs a specific section.
- Artifacts must link to long docs by repo-relative path and summarize only the needed decision, not paste the full content.

Context rules:

- Artifacts and manifests must use repo-relative paths; local absolute paths are forbidden.
- Do not paste full docs or raw logs into artifacts. Summarize and link by relative path.
- \`CONTEXT.md\` is hot context only and must stay under 200 lines.
- Current and future vocabulary must stay separated.
- Follow the phase context map in \`.Codex/rules/context/\` for phase-specific context loading.
- User-facing agent-flow replies must be short Korean by default. Keep code, commands, paths, and identifiers in English.
- Summarize only current phase, action, \`next_command\`, and blocker when useful.
`;
}

function runArchitectureLint(args) {
  runSandboxedPythonCliCommand("architecture-lint", args);
}

function runGates(args) {
  runSandboxedPythonCliCommand("gates", args);
}

function trustedLinuxBubblewrap() {
  const candidate = "/usr/bin/bwrap";
  try {
    const stat = fs.lstatSync(candidate);
    if (
      !stat.isFile()
      || stat.isSymbolicLink()
      || stat.uid !== 0
      || stat.nlink !== 1
      || (stat.mode & 0o022) !== 0
    ) return null;
    fs.accessSync(candidate, fs.constants.X_OK);
    return candidate;
  } catch {
    return null;
  }
}

function sandboxProfilePath(pathName) {
  return String(pathName).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function readPythonGateRun(root) {
  assertProjectRuntimeContract(root);
  const invocationRoot = gitOutput(process.cwd(), ["rev-parse", "--show-toplevel"])
    ?? fs.realpathSync(process.cwd());
  const commonDir = gitOutput(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  if (!commonDir) throw new Error("blocked: Python run context requires a git common directory");
  const runtimeRoot = path.join(fs.realpathSync(commonDir), "agent-flow", "worktrees");
  if (!fs.existsSync(runtimeRoot)) throw new Error("no active run. start one with the project launcher");
  const active = [];
  for (const worktreeEntry of fs.readdirSync(runtimeRoot, { withFileTypes: true })) {
    if (!worktreeEntry.isDirectory() || worktreeEntry.isSymbolicLink()) continue;
    const runsRoot = path.join(runtimeRoot, worktreeEntry.name, ".agent-flow", "runs");
    const runsStat = lstatIfExists(runsRoot);
    if (!runsStat?.isDirectory() || runsStat.isSymbolicLink()) continue;
    for (const runEntry of fs.readdirSync(runsRoot, { withFileTypes: true })) {
      if (!runEntry.isDirectory() || runEntry.isSymbolicLink()) continue;
      const runDir = path.join(runsRoot, runEntry.name);
      const marker = lstatIfExists(path.join(runDir, "active"));
      const metaPath = path.join(runDir, "meta.json");
      const metaStat = lstatIfExists(metaPath);
      if (!marker?.isFile() || marker.isSymbolicLink() || marker.nlink !== 1) continue;
      if (!metaStat?.isFile() || metaStat.isSymbolicLink() || metaStat.nlink !== 1) {
        throw new Error(`blocked: Python run metadata is unsafe: ${metaPath}`);
      }
      const state = JSON.parse(fs.readFileSync(metaPath, "utf8"));
      if (state.run_id !== runEntry.name) {
        throw new Error(`blocked: Python run metadata identity mismatch: ${metaPath}`);
      }
      const workspaceRoot = state.workspace?.workspace_root;
      if (typeof workspaceRoot !== "string") {
        throw new Error(`blocked: Python run workspace is missing: ${metaPath}`);
      }
      let resolvedWorkspace;
      try {
        resolvedWorkspace = fs.realpathSync(workspaceRoot);
      } catch {
        continue;
      }
      if (samePath(resolvedWorkspace, invocationRoot)) {
        active.push({ ...state, run_dir: runDir, workspace_root: workspaceRoot });
      }
    }
  }
  if (active.length !== 1) {
    throw new Error(active.length === 0
      ? "no active run. start one with the project launcher"
      : "blocked: multiple active Python runs detected");
  }
  const [state] = active;
  const current = currentNodeSkillPlan(root);
  if (
    state.skill_plan_hash_version !== current.skill_plan_hash_version
    || state.skill_plan_hash !== current.skill_plan_hash
  ) {
    throw new Error("blocked: Python run skill plan differs from installed commitment");
  }
  validateNodeWorkspaceIdentity(state.workspace, root);
  return state;
}

function activeGateRun(root) {
  try {
    const current = readCurrentRun(root);
    if (current.status !== "complete" && current.phase !== "complete") {
      const state = assertNodeRunBoundary(current, root);
      assertNodeSkillPlanPinned(state, root);
      return state;
    }
  } catch (error) {
    const message = String(error?.message || error);
    if (
      !message.startsWith("no active run. start one with:")
      && message !== "no active run is bound to this execution"
    ) {
      throw error;
    }
  }
  return readPythonGateRun(root);
}

function currentExportWorkspace(root) {
  const topLevel = gitOutput(process.cwd(), ["rev-parse", "--show-toplevel"]);
  if (!topLevel) {
    throw new Error("blocked: APK export requires a git workspace");
  }
  const workspace = fs.realpathSync(topLevel);
  if (samePath(workspace, root)) {
    throw new Error("blocked: APK export is not allowed from the leader checkout");
  }
  const execution = currentExecutionIdentity();
  if (!execution) {
    throw new Error("blocked: APK export requires a bound execution identity");
  }
  const bindingPath = executionBindingPath(root, execution);
  if (!fs.existsSync(bindingPath)) {
    throw new Error("blocked: APK export requires an active execution binding");
  }
  const binding = readOwnedJson(bindingPath);
  if (!nodeBindingIsActive(binding, root)) {
    throw new Error("blocked: APK export execution binding is not active");
  }
  // 실행 소유 worktree만 export 소스로 허용한다: 바인딩이 고정한 workspace_root가
  // 현재 CWD toplevel과 일치해야 하며, 리더 체크아웃이나 형제 worktree는 거부한다.
  const boundWorkspace = binding.workspace?.workspace_root;
  if (!boundWorkspace || !samePath(boundWorkspace, workspace)) {
    throw new Error(
      `blocked: APK export must run from the execution's own pinned worktree: ${workspace}`,
    );
  }
  const identity = registeredNodeWorkspaceIdentity(root, workspace);
  if (!identity) {
    throw new Error(`blocked: APK export workspace is not registered: ${workspace}`);
  }
  return validateNodeWorkspaceIdentity(identity, root);
}

function parseExportApkArgs(args) {
  let source = null;
  let name = null;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "--name") {
      if (index + 1 >= args.length || !args[index + 1]) {
        throw new Error("--name requires an APK filename");
      }
      name = args[++index];
    } else if (argument.startsWith("--name=")) {
      name = argument.slice("--name=".length);
      if (!name) throw new Error("--name requires an APK filename");
    } else if (argument.startsWith("-") || source !== null) {
      throw new Error("usage: agent-flow export-apk <workspace-apk> [--name <filename.apk>]");
    } else {
      source = argument;
    }
  }
  if (!source) {
    throw new Error("usage: agent-flow export-apk <workspace-apk> [--name <filename.apk>]");
  }
  const filename = name ?? path.basename(source);
  if (
    filename !== path.basename(filename)
    || filename === "."
    || filename === ".."
    || !filename.toLowerCase().endsWith(".apk")
  ) {
    throw new Error("APK export name must be a filename ending in .apk");
  }
  return { source, filename };
}

function openAvailableApkDestination(downloads, filename) {
  const stem = filename.slice(0, -4);
  const noFollow = fs.constants.O_NOFOLLOW || 0;
  for (let suffix = 0; suffix < 1000; suffix += 1) {
    const candidateName = suffix === 0 ? filename : `${stem}-${suffix}.apk`;
    const candidate = path.join(downloads, candidateName);
    try {
      const descriptor = fs.openSync(
        candidate,
        fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | noFollow,
        0o600,
      );
      return { descriptor, path: candidate };
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
    }
  }
  throw new Error(`cannot allocate APK export name for ${filename}`);
}

function captureApkDirectoryChain(base, target) {
  const baseResolved = path.resolve(base);
  const targetResolved = path.resolve(target);
  const relative = path.relative(baseResolved, targetResolved);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`blocked: APK export path escapes its root: ${targetResolved}`);
  }
  const chain = [];
  let cursor = baseResolved;
  const record = (directory) => {
    const metadata = fs.lstatSync(directory);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      throw new Error(`blocked: APK export path component is not a real directory: ${directory}`);
    }
    chain.push({ path: directory, dev: metadata.dev, ino: metadata.ino });
  };
  record(cursor);
  for (const part of relative.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    record(cursor);
  }
  return chain;
}

function assertApkDirectoryChainUnchanged(chain, label) {
  for (const entry of chain) {
    const metadata = fs.lstatSync(entry.path);
    if (
      !metadata.isDirectory()
      || metadata.isSymbolicLink()
      || metadata.dev !== entry.dev
      || metadata.ino !== entry.ino
    ) {
      throw new Error(`blocked: APK export ${label} path changed during copy: ${entry.path}`);
    }
  }
}

function runExportApk(args) {
  const root = resolveAgentFlowRoot(process.cwd());
  assertProjectRuntimeContract(root);
  const workspace = currentExportWorkspace(root);
  const request = parseExportApkArgs(args);
  const source = path.resolve(workspace, request.source);
  ensureChildPath(workspace, source);
  if (pathHasSymlink(workspace, source)) {
    throw new Error(`blocked: APK export source contains a symlink: ${source}`);
  }
  // 소스 검사 직전 창을 노려 중간 디렉터리를 외부 심링크로 바꿔치기하는 TOCTOU를
  // 테스트에서 결정적으로 재현하기 위한 seam.
  holdInstallForTest("AGENT_FLOW_TEST_HOLD_BEFORE_APK_SOURCE_CHECK_MS", "apk-source-check-ready");
  const sourceMetadata = fs.lstatSync(source);
  if (
    !sourceMetadata.isFile()
    || sourceMetadata.isSymbolicLink()
    || (typeof process.getuid === "function" && sourceMetadata.uid !== process.getuid())
    || !source.toLowerCase().endsWith(".apk")
  ) {
    throw new Error(`blocked: APK export source is not an owned regular .apk file: ${source}`);
  }
  const sourceDirectoryChain = captureApkDirectoryChain(workspace, path.dirname(source));
  const workspaceReal = fs.realpathSync.native(workspace);
  const sourceParentReal = fs.realpathSync.native(path.dirname(source));

  const downloads = path.join(nodeOs.homedir(), "Downloads");
  const downloadsMetadata = fs.lstatSync(downloads);
  if (
    !downloadsMetadata.isDirectory()
    || downloadsMetadata.isSymbolicLink()
    || (typeof process.getuid === "function" && downloadsMetadata.uid !== process.getuid())
  ) {
    throw new Error(`blocked: Downloads directory is unsafe: ${downloads}`);
  }
  const downloadsDirectoryChain = captureApkDirectoryChain(nodeOs.homedir(), downloads);
  const downloadsReal = fs.realpathSync.native(downloads);

  const noFollow = fs.constants.O_NOFOLLOW || 0;
  const sourceDescriptor = fs.openSync(source, fs.constants.O_RDONLY | noFollow);
  let destinationDescriptor = null;
  let destination = null;
  let destinationIdentity = null;
  let completed = false;
  try {
    const openedSource = fs.fstatSync(sourceDescriptor);
    if (
      !openedSource.isFile()
      || openedSource.dev !== sourceMetadata.dev
      || openedSource.ino !== sourceMetadata.ino
    ) {
      throw new Error("blocked: APK export source changed before copy");
    }
    // O_NOFOLLOW는 마지막 컴포넌트만 보호하므로, 동일 uid 프로세스가 중간 디렉터리를
    // 외부 심링크로 바꿔치기해 외부 .apk를 유출하는 것을 조상 체인 재검증으로 차단한다.
    assertApkDirectoryChainUnchanged(sourceDirectoryChain, "source");
    if (
      fs.realpathSync.native(path.dirname(source)) !== sourceParentReal
      || fs.realpathSync.native(workspace) !== workspaceReal
    ) {
      throw new Error("blocked: APK export source path changed before copy");
    }
    const sourceParentRelative = path.relative(workspaceReal, sourceParentReal);
    if (sourceParentRelative.startsWith("..") || path.isAbsolute(sourceParentRelative)) {
      throw new Error("blocked: APK export source escaped its workspace before copy");
    }

    holdInstallForTest("AGENT_FLOW_TEST_HOLD_BEFORE_APK_DEST_OPEN_MS", "apk-dest-open-ready");
    assertApkDirectoryChainUnchanged(downloadsDirectoryChain, "Downloads");
    if (fs.realpathSync.native(downloads) !== downloadsReal) {
      throw new Error("blocked: Downloads directory changed before copy");
    }
    const openedDestination = openAvailableApkDestination(downloads, request.filename);
    destinationDescriptor = openedDestination.descriptor;
    destination = openedDestination.path;
    destinationIdentity = fs.fstatSync(destinationDescriptor);
    // 생성된 대상의 부모 식별자가 캡처한 Downloads와 여전히 동일한지 재확인한다.
    assertApkDirectoryChainUnchanged(downloadsDirectoryChain, "Downloads");
    if (fs.realpathSync.native(path.dirname(destination)) !== downloadsReal) {
      throw new Error("blocked: Downloads directory changed during copy");
    }

    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let offset = 0;
    while (offset < openedSource.size) {
      const read = fs.readSync(
        sourceDescriptor,
        buffer,
        0,
        Math.min(buffer.length, openedSource.size - offset),
        offset,
      );
      if (read === 0) throw new Error("blocked: APK export source ended during copy");
      let written = 0;
      while (written < read) {
        written += fs.writeSync(destinationDescriptor, buffer, written, read - written);
      }
      offset += read;
    }
    const finalSource = fs.fstatSync(sourceDescriptor);
    if (
      finalSource.dev !== openedSource.dev
      || finalSource.ino !== openedSource.ino
      || finalSource.size !== openedSource.size
      || finalSource.mtimeMs !== openedSource.mtimeMs
    ) {
      throw new Error("blocked: APK export source changed during copy");
    }
    fs.fchmodSync(destinationDescriptor, 0o644);
    fs.fsyncSync(destinationDescriptor);
    completed = true;
  } finally {
    if (destinationDescriptor !== null) fs.closeSync(destinationDescriptor);
    fs.closeSync(sourceDescriptor);
    if (!completed && destination && destinationIdentity) {
      const current = lstatIfExists(destination);
      if (
        current?.isFile()
        && !current.isSymbolicLink()
        && current.dev === destinationIdentity.dev
        && current.ino === destinationIdentity.ino
      ) {
        fs.unlinkSync(destination);
      }
    }
  }
  console.log(`exported apk: ${destination}`);
}

function scriptInvokesGradle(script) {
  if (typeof script !== "string" || !script) return false;
  // 쉘 스크립트 안에서 gradle/gradlew를 독립 명령 토큰으로 호출하는 경우만 매칭한다
  // (경로 접두사 허용, 뒤에 인접 문자가 붙는 오탐은 배제).
  return /(?:^|[\s;&|(])(?:[^\s;&|(]*\/)?gradlew?(?:\.bat)?(?=$|[\s;&|)=])/i.test(script);
}

function isGradleGateCommand(args) {
  if (!args.length) return false;
  const command = path.basename(args[0]).toLowerCase();
  if (command === "gradle" || command === "gradlew" || command === "gradlew.bat") return true;
  if (command === "sh" || command === "bash" || command === "dash" || command === "zsh") {
    const flagIndex = args.indexOf("-c");
    if (flagIndex !== -1 && flagIndex + 1 < args.length) {
      return scriptInvokesGradle(args[flagIndex + 1]);
    }
  }
  return false;
}

function runSandboxedGate(args, extraEnv = {}) {
  const separator = args.indexOf("--");
  const requestedGateArgs = separator === -1 ? args : args.slice(separator + 1);
  if (requestedGateArgs.length === 0) throw new Error("gate requires a command after --");
  const gradleGate = isGradleGateCommand(requestedGateArgs);
  if (gradleGate && requestedGateArgs.includes("--daemon")) {
    throw new Error("blocked: sandboxed Gradle gates do not allow --daemon");
  }
  const gateArgs = gradleGate && !requestedGateArgs.includes("--no-daemon")
    ? [...requestedGateArgs, "--no-daemon"]
    : requestedGateArgs;
  const root = resolveAgentFlowRoot(process.cwd());
  assertProjectRuntimeContract(root);
  const state = activeGateRun(root);
  const pinned = fs.realpathSync(state.workspace_root ?? root);
  const invocation = gitOutput(process.cwd(), ["rev-parse", "--show-toplevel"])
    ?? fs.realpathSync(process.cwd());
  if (!samePath(invocation, pinned)) {
    throw new Error(`blocked: sandboxed gate must run from pinned workspace ${pinned}`);
  }
  const gateRuntime = path.join(pinned, ".agent-flow", "gate-runtime");
  const gateHome = path.join(gateRuntime, "home");
  const gateTemp = path.join(gateRuntime, "tmp");
  ensureManagedDirectory(gateHome, pinned);
  ensureManagedDirectory(gateTemp, pinned);
  const gradleHome = path.join(gateRuntime, "gradle-home");
  const kotlinDaemon = path.join(gateRuntime, "kotlin-daemon");
  if (gradleGate) {
    ensureManagedDirectory(gradleHome, pinned);
    ensureManagedDirectory(kotlinDaemon, pinned);
  }
  const env = {
    ...process.env,
    ...extraEnv,
    HOME: gateHome,
    TMPDIR: gateTemp,
    TEMP: gateTemp,
    TMP: gateTemp,
  };
  if (gradleGate) {
    delete env.GRADLE_OPTS;
    env.GRADLE_USER_HOME = gradleHome;
    env.KOTLIN_DAEMON_RUNFILES_PATH = kotlinDaemon;
  }
  let executable;
  let sandboxArgs;
  if (process.platform === "darwin" && fs.existsSync("/usr/bin/sandbox-exec")) {
    const profile = [
      "(version 1)",
      "(allow default)",
      "(deny file-write*)",
      `(allow file-write* (subpath "${sandboxProfilePath(pinned)}"))`,
      "(allow file-write* (literal \"/dev/null\") (literal \"/dev/tty\"))",
    ].join(" ");
    executable = "/usr/bin/sandbox-exec";
    sandboxArgs = ["-p", profile, ...gateArgs];
  } else if (process.platform === "linux") {
    executable = trustedLinuxBubblewrap();
    if (!executable) throw new Error("blocked: sandboxed gate requires bwrap on Linux");
    sandboxArgs = [
      "--die-with-parent",
      "--ro-bind", "/", "/",
      "--bind", pinned, pinned,
      "--dev-bind", "/dev", "/dev",
      "--proc", "/proc",
      "--chdir", pinned,
      "--",
      ...gateArgs,
    ];
  } else {
    throw new Error(`blocked: sandboxed gate is unsupported on ${process.platform}`);
  }
  const result = safeSpawnSync(executable, sandboxArgs, {
    cwd: pinned,
    env,
    stdio: "inherit",
    timeout: 30 * 60 * 1000,
  });
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
}

function runSandboxedPythonCliCommand(subcommand, args) {
  const normalizedArgs = [];
  let requestedRoot = null;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    let rootValue = null;
    if (argument === "--root") {
      if (index + 1 >= args.length || !args[index + 1]) {
        throw new Error("--root requires a path");
      }
      rootValue = args[++index];
    } else if (argument.startsWith("--root=")) {
      rootValue = argument.slice("--root=".length);
      if (!rootValue) throw new Error("--root requires a path");
    } else {
      normalizedArgs.push(argument);
      continue;
    }
    const canonicalRoot = path.resolve(rootValue);
    if (requestedRoot && requestedRoot !== canonicalRoot) {
      throw new Error("conflicting --root arguments");
    }
    requestedRoot = canonicalRoot;
  }
  const root = requestedRoot || resolveAgentFlowRoot(process.cwd());
  const rootArgument = ["--root", root];
  const pythonArgs = [...rootArgument, ...normalizedArgs];
  const kitPath = path.join(root, ".agent-flow", "kit.json");
  if (!lstatIfExists(kitPath)) {
    const python = projectPythonPath();
    const pythonPathEntries = [
      path.join(KIT_ROOT, "src"),
      root ? installedPythonRuntimePath(root) : "",
      process.env.PYTHONPATH,
    ].filter(Boolean);
    const result = safeSpawnSync(python, ["-m", "agent_flow.cli", subcommand, ...pythonArgs], {
      cwd: process.cwd(),
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONNOUSERSITE: "1",
        PYTHONSAFEPATH: "1",
        PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
      },
      stdio: "inherit",
      timeout: 30 * 60 * 1000,
    });
    if (result.error) throw result.error;
    process.exit(result.status ?? 1);
  }
  const contract = assertProjectRuntimeContract(root);
  const pythonPathEntries = [
    path.join(KIT_ROOT, "src"),
    root ? installedPythonRuntimePath(root) : "",
    process.env.PYTHONPATH,
  ].filter(Boolean);
  if (subcommand === "gates") {
    const result = safeSpawnAuthenticatedSync(
      contract.python,
      ["-m", "agent_flow.cli", subcommand, ...pythonArgs],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          PYTHONDONTWRITEBYTECODE: "1",
          PYTHONNOUSERSITE: "1",
          PYTHONSAFEPATH: "1",
          PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
        },
        stdio: "inherit",
        timeout: 30 * 60 * 1000,
      },
    );
    if (result.error) throw result.error;
    process.exit(result.status ?? 1);
  }
  runSandboxedGate(
    ["--", "/usr/bin/python3", ...authenticatedExecutableArgs(
      contract.python,
      ["-m", "agent_flow.cli", subcommand, ...pythonArgs],
    )],
    {
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONNOUSERSITE: "1",
      PYTHONSAFEPATH: "1",
      PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
    },
  );
}

function portableRecoveryAuthorityIsValid(root) {
  try {
    if (process.env.AGENT_FLOW_PORTABLE_BOOTSTRAP !== "1") return false;
    const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
    if (
      process.env.AGENT_FLOW_PROJECT_LAUNCHER !== launcher
      || !samePath(path.resolve(process.env.AGENT_FLOW_PROJECT_LAUNCHER), launcher)
    ) return false;
    const descriptor = Number.parseInt(
      process.env.AGENT_FLOW_PORTABLE_BOOTSTRAP_FD || "",
      10,
    );
    if (!Number.isInteger(descriptor)) return false;
    const held = fs.fstatSync(descriptor);
    const current = fs.lstatSync(launcher);
    if (
      !held.isFile()
      || current.isSymbolicLink()
      || !current.isFile()
      || held.dev !== current.dev
      || held.ino !== current.ino
    ) return false;
    const bytes = Buffer.alloc(held.size);
    let offset = 0;
    while (offset < bytes.length) {
      const count = fs.readSync(descriptor, bytes, offset, bytes.length - offset, offset);
      if (count <= 0) return false;
      offset += count;
    }
    const kit = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"));
    const contract = kit?.project_runtime_contract;
    if (
      !contract
      || contract.version !== 3
      || kit.project_runtime_contract_commitment_version !== 1
      || kit.project_runtime_contract_commitment !== projectRuntimeContractCommitment(contract)
      || sha256Bytes(bytes) !== contract.launcher.sha256
    ) return false;
    const encodedAuthority = process.env.AGENT_FLOW_PORTABLE_AUTHORITY || "";
    if (
      !encodedAuthority
      || !bytes.toString("utf8").includes(` ${shellSingleQuote(encodedAuthority)} "$@"`)
    ) return false;
    const authority = JSON.parse(
      Buffer.from(encodedAuthority, "base64").toString("utf8"),
    );
    return authority?.version === 1
      && Object.keys(authority).sort(compareCodePoints).join(",")
        === "python_runtime_integrity,runtime_integrity,version"
      && authority.runtime_integrity === contract.runtime.integrity
      && authority.python_runtime_integrity === contract.python_runtime.integrity;
  } catch {
    return false;
  }
}


function authenticatedRelocatedProjectLauncher(root) {
  if (!projectRuntimeContentMatchesContract(root)) {
    throw new Error("portable project launcher authentication failed");
  }
  const launcher = path.join(root, PROJECT_LAUNCHER_RELATIVE);
  const contract = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"))
    ?.project_runtime_contract;
  const descriptor = fs.openSync(
    launcher,
    fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0),
  );
  try {
    const before = fs.fstatSync(descriptor);
    const named = fs.lstatSync(launcher);
    if (
      !before.isFile()
      || named.isSymbolicLink()
      || !named.isFile()
      || before.dev !== named.dev
      || before.ino !== named.ino
    ) throw new Error("portable project launcher authentication failed");
    const source = fs.readFileSync(descriptor);
    const after = fs.fstatSync(descriptor);
    if (
      before.dev !== after.dev
      || before.ino !== after.ino
      || before.size !== after.size
      || sha256Bytes(source) !== contract.launcher.sha256
    ) throw new Error("portable project launcher authentication failed");
    return { launcher, source };
  } finally {
    fs.closeSync(descriptor);
  }
}


function runProjectPythonCli(args) {
  const root = resolveAgentFlowRoot(process.cwd());
  const portableRecoveryRequired = isRuntimeRecoveryCommand()
    && runtimeContractRequiresPortableRepin(root);
  const portableRecovery = isRuntimeRecoveryCommand()
    && portableRecoveryAuthorityIsValid(root);
  if (
    !portableRecovery
    && process.env.AGENT_FLOW_PORTABLE_BOOTSTRAP !== "1"
    && isRuntimeRecoveryCommand()
    && projectRuntimeContentMatchesContract(root)
  ) {
    const { launcher, source } = authenticatedRelocatedProjectLauncher(root);
    const contract = readJsonIfExists(path.join(root, ".agent-flow", "kit.json"))
      .project_runtime_contract;
    const invocation = originalNodeAuthority();
    const launcherRelocated = !source.toString("utf8").includes(
      `AGENT_FLOW_PROJECT_LAUNCHER=${shellSingleQuote(launcher)}`,
    );
    const invocationChanged = invocation.path !== contract.node.path
      || invocation.sha256 !== contract.node.sha256
      || invocation.device !== contract.node.device
      || invocation.inode !== contract.node.inode
      || invocation.mode !== contract.node.mode;
    if (portableRecoveryRequired || launcherRelocated || invocationChanged) {
      assertRecoveryTargetsProject(args, root);
      const env = { ...process.env };
      delete env.AGENT_FLOW_PORTABLE_BOOTSTRAP;
      delete env.AGENT_FLOW_PORTABLE_BOOTSTRAP_FD;
      delete env.AGENT_FLOW_PORTABLE_AUTHORITY;
      const delegated = safeSpawnSync(
        "/bin/sh",
        ["-c", source.toString("utf8"), launcher, ...args],
        {
          cwd: process.cwd(),
          env,
          stdio: "inherit",
          timeout: 30 * 60 * 1000,
        },
      );
      if (delegated.error) throw delegated.error;
      process.exit(delegated.status ?? 1);
    }
  }
  let pythonContract;
  if (portableRecovery) {
    assertRecoveryTargetsProject(args, root);
    const pythonPath = projectPythonPath();
    pythonContract = {
      path: pythonPath,
      resolved_path: fs.realpathSync(pythonPath),
      ...executableIdentity(pythonPath),
      dependencies: executableDependencyContracts(pythonPath),
    };
  } else {
    assertProjectRuntimeReady(root);
    refreshSkillCatalogAtBoundary(root);
    pythonContract = assertProjectRuntimeContract(root).python;
  }
  const pythonPathEntries = [
    path.join(KIT_ROOT, "src"),
    installedPythonRuntimePath(root),
  ].filter(Boolean);
  const result = safeSpawnAuthenticatedSync(
    pythonContract,
    ["-m", "agent_flow.cli", ...args],
    {
      cwd: process.cwd(),
      env: {
        ...process.env,
        PYTHON: pythonContract.path,
        PYTHON_EXECUTABLE: pythonContract.path,
        AGENT_FLOW_PYTHON_EXECUTABLE: pythonContract.path,
        AGENT_FLOW_GIT_EXECUTABLE: portableRecovery
          ? projectGitPath()
          : process.env.AGENT_FLOW_GIT_EXECUTABLE,
        PYTHONDONTWRITEBYTECODE: "1",
        PYTHONNOUSERSITE: "1",
        PYTHONSAFEPATH: "1",
        PYTHONPATH: [...new Set(pythonPathEntries)].join(path.delimiter),
      },
      stdio: "inherit",
      timeout: 30 * 60 * 1000,
    },
  );
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
}

function installedPythonRuntimePath(root) {
  const runtimePath = path.join(root, RUNTIME_PYTHON_RELATIVE);
  return fs.existsSync(path.join(runtimePath, "agent_flow", "__init__.py")) ? runtimePath : "";
}

const MAC_SANDBOX_POLICY_PROBE = [
  "import ctypes,json,os,sys",
  "target=sys.argv[1]; metadata=os.stat(target,follow_symlinks=False)",
  "lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)",
  "check=lib.sandbox_check; check.restype=ctypes.c_int",
  "check.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_uint64]",
  "denied=check(os.getpid(),b'file-write-data',ctypes.c_uint64(1),ctypes.c_char_p(os.fsencode(target)))",
  "print(json.dumps({'denied':denied!=0,'flags':int(getattr(metadata,'st_flags',0))}))",
].join("\n");

const INSTALL_LOCK_EXEC_WRAPPER = [
  "import base64,fcntl,os,stat,subprocess,sys",
  "root,nonce,encoded,original_encoded,verifier_encoded=sys.argv[1:6]; command=sys.argv[6:]",
  "agent=os.path.join(root,'.agent-flow'); os.makedirs(agent,exist_ok=True)",
  "agent_stat=os.lstat(agent)",
  "if not stat.S_ISDIR(agent_stat.st_mode) or stat.S_ISLNK(agent_stat.st_mode): raise SystemExit('managed install root is unsafe')",
  "lock_path=os.path.join(agent,'install.flock')",
  "lock_fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)",
  "lock_stat=os.fstat(lock_fd)",
  "if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink!=1: raise SystemExit('project install lock is unsafe')",
  "try: fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
  "except BlockingIOError: raise SystemExit('project install lock is held: '+lock_path)",
  "os.ftruncate(lock_fd,0); os.write(lock_fd,(nonce+'\\n').encode()); os.fsync(lock_fd); os.set_inheritable(lock_fd,True)",
  "env=dict(os.environ); env['AGENT_FLOW_INSTALL_FLOCK_FD']=str(lock_fd); env['AGENT_FLOW_INSTALL_FLOCK_NONCE']=nonce; env['AGENT_FLOW_AUTH_EXEC_ROOT']=root; env['AGENT_FLOW_ORIGINAL_NODE_AUTHORITY']=original_encoded",
  "verifier=base64.b64decode(verifier_encoded,validate=True).decode()",
  "completed=subprocess.run(['/usr/bin/python3','-I','-B','-c',verifier,encoded,*command],env=env,pass_fds=(lock_fd,),check=False)",
  "raise SystemExit(completed.returncode)",
].join("\n");

function verifyInstallFlock(root, nonce) {
  const descriptor = Number.parseInt(process.env.AGENT_FLOW_INSTALL_FLOCK_FD || "", 10);
  const lockPath = path.join(root, ".agent-flow", "install.flock");
  if (!Number.isInteger(descriptor) || process.env.AGENT_FLOW_INSTALL_FLOCK_NONCE !== nonce) {
    throw new Error("blocked: project mutation lock proof is missing");
  }
  const held = fs.fstatSync(descriptor);
  const current = fs.lstatSync(lockPath);
  if (
    !held.isFile()
    || held.nlink !== 1
    || held.dev !== current.dev
    || held.ino !== current.ino
    || fs.readFileSync(lockPath, "utf8") !== `${nonce}\n`
  ) throw new Error("blocked: project mutation lock proof is invalid");
}

function verifyMacSandboxPolicy(canary) {
  if (process.platform !== "darwin") return;
  const result = safeSpawnSync("/usr/bin/python3", ["-I", "-B", "-c", MAC_SANDBOX_POLICY_PROBE, canary], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  let proof = null;
  try {
    proof = JSON.parse(result.stdout || "null");
  } catch {
    // 아래 공통 오류로 닫는다.
  }
  if (result.error || result.status !== 0 || proof?.denied !== true || proof?.flags !== 0) {
    throw new Error("blocked: macOS install sandbox policy proof is invalid");
  }
}

function verifyMutationSandboxAndRun(args) {
  const [action, rootArgument, canaryPath, nonce, ...requestedInstallArgs] = args;
  if (!new Set(["install", "sync"]).has(action)) throw new Error("blocked: invalid project mutation action");
  if (!rootArgument || !path.isAbsolute(rootArgument) || !canaryPath || !path.isAbsolute(canaryPath)) {
    throw new Error("blocked: invalid install sandbox proof");
  }
  const root = resolveInstallRoot(rootArgument);
  assertLeaderMutationSource(root, action);
  verifyInstallFlock(root, nonce);
  const canary = path.resolve(canaryPath);
  if (samePath(canary, root) || canary.startsWith(`${root}${path.sep}`) || !/^[0-9a-f]{48}$/.test(nonce || "")) {
    throw new Error("blocked: invalid install sandbox proof");
  }
  const stat = fs.lstatSync(canary);
  if (
    !stat.isFile()
    || stat.isSymbolicLink()
    || stat.nlink !== 1
    || (typeof process.getuid === "function" && stat.uid !== process.getuid())
    || (stat.mode & 0o200) === 0
    || fs.readFileSync(canary, "utf8") !== `${nonce}\n`
  ) {
    throw new Error("blocked: install sandbox proof is unsafe");
  }
  verifyMacSandboxPolicy(canary);
  let descriptor = null;
  try {
    descriptor = fs.openSync(canary, fs.constants.O_WRONLY | fs.constants.O_APPEND | (fs.constants.O_NOFOLLOW || 0));
  } catch (error) {
    if (error?.code !== "EACCES" && error?.code !== "EPERM" && error?.code !== "EROFS") throw error;
  }
  if (descriptor !== null) {
    fs.closeSync(descriptor);
    throw new Error("blocked: install sandbox write boundary is not active");
  }
  if (action === "install") {
    installArgs = requestedInstallArgs;
    installProject(root);
  } else {
    syncProject(root);
  }
}

function runMutationSandbox(action, rootOverride = null, requestedInstallArgs = []) {
  const requestedRoot = path.resolve(rootOverride || process.cwd());
  const managedWorktreeRoot = resolveManagedWorktreeRoot(requestedRoot);
  if (managedWorktreeRoot) {
    if (action === "install" && fs.existsSync(path.join(managedWorktreeRoot, ".agent-flow", "kit.json"))) {
      console.log(`agent-flow already installed root=${managedWorktreeRoot}`);
      console.log("worktree install skipped; reinstall from the leader checkout if needed");
      return 0;
    }
    throw new Error(`managed worktree ${action} blocked; run ${action} from the leader checkout`);
  }
  const root = resolveInstallRoot(requestedRoot);
  assertExistingRuntimeExecutablesUntampered(root);
  assertLeaderMutationSource(root, action);
  let executable = null;
  let args = [];
  const nonce = crypto.randomBytes(24).toString("hex");
  const canaryRoot = fs.mkdtempSync(path.join(nodeOs.tmpdir(), "agent-flow-install-proof-"));
  const canaryPath = path.join(canaryRoot, "outside-project");
  fs.writeFileSync(canaryPath, `${nonce}\n`, { flag: "wx", mode: 0o600 });
  const originalNode = originalNodeAuthority();
  const nodeAuthority = {
    ...originalNode,
    staging_root: path.join(root, ".agent-flow", "exec-staging"),
    project_root: fs.realpathSync(root),
  };
  const lockedCommand = [
    "/usr/bin/python3", "-I", "-B", "-c", INSTALL_LOCK_EXEC_WRAPPER,
    root,
    nonce,
    Buffer.from(JSON.stringify(nodeAuthority), "utf8").toString("base64"),
    Buffer.from(JSON.stringify(originalNode), "utf8").toString("base64"),
    Buffer.from(AUTHENTICATED_EXEC_VERIFIER, "utf8").toString("base64"),
    fileURLToPath(import.meta.url),
    "__sandboxed-mutation",
    action,
    root,
    canaryPath,
    nonce,
    ...requestedInstallArgs,
  ];
  if (process.platform === "darwin" && fs.existsSync("/usr/bin/sandbox-exec")) {
    executable = "/usr/bin/sandbox-exec";
    const profile = [
      "(version 1)",
      "(allow default)",
      "(deny file-write*)",
      `(allow file-write* (subpath "${sandboxProfilePath(root)}"))`,
      "(allow file-write* (literal \"/dev/null\") (literal \"/dev/tty\"))",
    ].join(" ");
    args = [
      "-p", profile,
      ...lockedCommand,
    ];
  } else if (process.platform === "linux") {
    executable = trustedLinuxBubblewrap();
    if (!executable) {
      fs.rmSync(canaryRoot, { recursive: true, force: true });
      throw new Error("blocked: agent-flow install requires trusted bwrap on Linux");
    }
    args = [
      "--die-with-parent",
      "--ro-bind", "/", "/",
      "--bind", root, root,
      "--dev-bind", "/dev", "/dev",
      "--proc", "/proc",
      "--chdir", root,
      "--",
      ...lockedCommand,
    ];
  } else {
    fs.rmSync(canaryRoot, { recursive: true, force: true });
    throw new Error(`blocked: agent-flow install sandbox is unsupported on ${process.platform}`);
  }
  let result;
  try {
    result = safeSpawnSync(executable, args, {
      cwd: root,
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: "1",
      },
      stdio: "inherit",
      timeout: 30 * 60 * 1000,
    });
  } finally {
    fs.rmSync(canaryRoot, { recursive: true, force: true });
  }
  if (result.error) throw result.error;
  const status = result.status ?? 1;
  if (status === 0 && action === "install") {
    try {
      installCodexTrustState(root);
    } catch (error) {
      console.error(`warning: Codex trust registration failed after install commit: ${error.message}`);
    }
  }
  return status;
}

function runInstallSandbox(rootOverride = null, requestedInstallArgs = []) {
  return runMutationSandbox("install", rootOverride, requestedInstallArgs);
}

function runSyncSandbox(rootOverride = null) {
  return runMutationSandbox("sync", rootOverride, []);
}

try {
  if (command === "__sandboxed-mutation") {
    verifyMutationSandboxAndRun(process.argv.slice(3));
    process.exit(0);
  }

  if (command === "install") {
    process.exit(runInstallSandbox(null, process.argv.slice(3)));
  }

  if (command === "sync") {
    process.exit(runSyncSandbox());
  }

  if (command === "run" && process.argv[3] === "install") {
    process.exit(runInstallSandbox(null, process.argv.slice(4)));
  }

  if (command === "run") {
    const runArgs = process.argv.slice(3);
    if (!NODE_RUN_SUBCOMMANDS.has(runArgs[0])) {
      runProjectPythonCli(["run", ...runArgs]);
    }
    runWorkflowCommand(runArgs);
    process.exit(0);
  }

  if (command === "status") {
    runProjectPythonCli(["status", ...process.argv.slice(3)]);
  }

  if (command === "continue") {
    runProjectPythonCli(["continue"]);
  }

  if (command === "abort") {
    runProjectPythonCli(["abort", ...process.argv.slice(3)]);
  }

  if (command === "worktree") {
    runProjectPythonCli(["worktree", ...process.argv.slice(3)]);
  }

  if (command === "export-apk") {
    runExportApk(process.argv.slice(3));
    process.exit(0);
  }

  if (command === "architecture-lint") {
    runArchitectureLint(process.argv.slice(3));
  }

  if (command === "gates") {
    runGates(process.argv.slice(3));
  }

  if (command === "gate") {
    runSandboxedGate(process.argv.slice(3));
  }

  console.error("usage: agent-flow-kit install [--force-managed] | sync | status | continue | abort [--worktree <name>] --yes | worktree <create|status|list|repin|remove> | export-apk <workspace-apk> [--name <filename.apk>] | gate -- <command ...> | gates [--profile <id>] [--worktree <name>] | architecture-lint [--profile <id>] [--files ...] | run <task|install|start|status|next|advance|push-watch|push-watch-tick>");
  process.exit(1);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
