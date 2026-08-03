import {
  agentFlowHome,
  sharedHookLauncherInvocation,
} from "./shared-hook-runtime.mjs";

// OMP host에 심는 관리형 확장의 소스. 두 진입점(`agent-flow-kit.mjs`,
// `agent-flow-install.mjs`)이 같은 바이트를 심어야 한다 — 예전에 한쪽만
// `tool_result` 핸들러 등록 순서가 달라서, 이벤트당 핸들러 하나만 남기는 host에서
// 루트 컨텍스트 동기화가 통째로 죽었다. 사본을 두고 대조하는 대신 한 벌만 둔다.
export const OMP_EXTENSION_MARKER = "agent-flow: managed omp extension";

export function ompHooksExtensionSource() {
  const invocation = sharedHookLauncherInvocation({ homeDir: agentFlowHome() });
  return String.raw`// ${OMP_EXTENSION_MARKER}
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import { execFileSync, spawn } from "node:child_process";
import path from "node:path";

const AGENT_FLOW_HOME = path.resolve(${JSON.stringify(agentFlowHome())});
const HOOK_BOOTSTRAP = ${JSON.stringify(invocation.bootstrap)};
const HOOK_PYTHON = ${JSON.stringify(invocation.python)};
const MANAGED_PROJECTS = path.join(AGENT_FLOW_HOME, "managed-projects.json");
const GIT = "/usr/bin/git";
const STABLE_CWD = AGENT_FLOW_HOME;
const SAFE_DIGEST = /^[0-9a-f]{64}$/;
const HOOK_TIMEOUT_MS = 15000;

function hasInstall(dir) {
  try {
    const identity = fs.lstatSync(path.join(dir, ".agent-flow", "kit.json"));
    return identity.isFile() && !identity.isSymbolicLink();
  } catch {
    return false;
  }
}

function canonicalOmpPath(value) {
  try {
    return fs.realpathSync.native(value);
  } catch {
    return path.resolve(value);
  }
}
function nearestGitMarker(start) {
  let current = canonicalOmpPath(start);
  for (;;) {
    try {
      fs.lstatSync(path.join(current, ".git"));
      return current;
    } catch {
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return "";
    }
    current = parent;
  }
}

function gitMarkerIsNonDirectory(root) {
  if (!root) {
    return false;
  }
  try {
    return !fs.lstatSync(path.join(root, ".git")).isDirectory();
  } catch {
    return false;
  }
}

function readOwnedFile(target, label) {
  let descriptor;
  try {
    descriptor = fs.openSync(
      target,
      fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0),
    );
    const identity = fs.fstatSync(descriptor);
    if (
      !identity.isFile() ||
      identity.uid !== process.getuid() ||
      identity.nlink !== 1 ||
      (identity.mode & 0o022) !== 0
    ) {
      throw new Error(label + " ownership or mode is unsafe");
    }
    return fs.readFileSync(descriptor);
  } finally {
    if (descriptor !== undefined) {
      fs.closeSync(descriptor);
    }
  }
}

function isAncestorOrSame(ancestor, descendant) {
  if (!ancestor) {
    return false;
  }
  const base = path.resolve(ancestor);
  const target = path.resolve(descendant);
  return target === base || target.startsWith(base + path.sep);
}

// Python LEAKY_GIT_ENV_VARS(src/agent_flow/core/worktree_isolation.py)와 같은 목록이다.
// ambient GIT_COMMON_DIR 하나만 남아도 rev-parse가 남의 repo를 가리키고, 그 값이 그대로
// 실행할 hook 디렉터리가 된다.
const GIT_DISCOVERY_ENV = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
  "GIT_CEILING_DIRECTORIES",
];

function gitEnv() {
  const env = { ...process.env };
  for (const name of GIT_DISCOVERY_ENV) {
    delete env[name];
  }
  return env;
}

function gitContext(start) {
  try {
    const [topLevel, commonDir] = [
      ["rev-parse", "--show-toplevel"],
      ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    ].map((args) => execFileSync(GIT, args, {
      cwd: start,
      env: gitEnv(),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    }).trim());
    const checkout = canonicalOmpPath(topLevel);
    const common = canonicalOmpPath(commonDir);
    const leader = path.basename(common) === ".git"
      ? canonicalOmpPath(path.dirname(common))
      : "";
    return {
      checkout,
      leader,
      linked: Boolean(leader) && checkout !== leader,
    };
  } catch {
    const marker = nearestGitMarker(start);
    return {
      checkout: "",
      leader: "",
      linked: false,
      failed: Boolean(marker),
      nonDirectoryMarker: gitMarkerIsNonDirectory(marker),
    };
  }
}


class UnregisteredProjectError extends Error {}


function managedProjects() {
  let registry;
  try {
    registry = JSON.parse(
      readOwnedFile(MANAGED_PROJECTS, "managed project registry").toString("utf8"),
    );
  } catch (error) {
    throw new Error(
      "managed project registry is missing or unreadable: "
      + String(error?.message || error),
    );
  }
  if (
    !registry
    || typeof registry !== "object"
    || Array.isArray(registry)
    || registry.protocol_version !== 1
    || !registry.projects
    || typeof registry.projects !== "object"
    || Array.isArray(registry.projects)
  ) {
    throw new Error("managed project registry is invalid");
  }
  return registry.projects;
}


function registeredRecord(root, projects) {
  const canonicalRoot = canonicalOmpPath(root);
  const record = Object.prototype.hasOwnProperty.call(projects, canonicalRoot)
    ? projects[canonicalRoot]
    : null;
  const accepted = Array.isArray(record?.accepted_kit_digests)
    ? record.accepted_kit_digests
    : [];
  if (!record) {
    throw new UnregisteredProjectError("project root is not registered: " + canonicalRoot);
  }
  if (
    record.root !== canonicalRoot
    || typeof record.kit_digest !== "string"
    || !SAFE_DIGEST.test(record.kit_digest)
    || accepted.some((digest) => typeof digest !== "string" || !SAFE_DIGEST.test(digest))
  ) {
    throw new Error("managed project registry entry is invalid: " + canonicalRoot);
  }
  return { canonicalRoot, record, accepted };
}


function trustedManifest(root) {
  const projects = managedProjects();
  const { canonicalRoot, record, accepted } = registeredRecord(root, projects);
  const manifestPath = path.join(canonicalRoot, ".agent-flow", "kit.json");
  let manifest;
  try {
    const content = readOwnedFile(manifestPath, "registered project manifest");
    const digest = crypto.createHash("sha256").update(content).digest("hex");
    if (![record.kit_digest, ...accepted].includes(digest)) {
      throw new Error("digest does not match the private registry");
    }
    manifest = JSON.parse(content.toString("utf8"));
  } catch (error) {
    throw new Error("registered project manifest is invalid: " + manifestPath + ": " + String(error?.message || error));
  }
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new Error("registered project manifest is invalid: " + manifestPath);
  }
  return { root: canonicalRoot, manifest };
}


function nearestRegistration(start, boundary, projects) {
  let current = canonicalOmpPath(start);
  for (;;) {
    if (Object.prototype.hasOwnProperty.call(projects, current)) {
      return current;
    }
    const parent = path.dirname(current);
    if (current === boundary || parent === current) {
      return "";
    }
    current = parent;
  }
}

function nearestManifest(start, boundary, home) {
  let current = canonicalOmpPath(start);
  for (;;) {
    if (current !== home && hasInstall(current)) {
      return current;
    }
    const parent = path.dirname(current);
    if (current === boundary || parent === current) {
      return "";
    }
    current = parent;
  }
}

function resolveTrustedInstall(start) {
  const home = canonicalOmpPath(os.homedir());
  const candidate = canonicalOmpPath(start);
  const searchBoundary = isAncestorOrSame(home, candidate)
    ? home
    : path.parse(candidate).root;
  let projects = null;
  let registryError = "";
  try {
    projects = managedProjects();
  } catch (error) {
    registryError = String(error?.message || error);
  }
  const directRoot = nearestManifest(candidate, searchBoundary, home);
  let registeredRoot = projects
    ? nearestRegistration(candidate, searchBoundary, projects)
    : "";
  const git = gitContext(candidate);
  if (
    git.leader
    && registeredRoot
    && git.leader !== registeredRoot
  ) {
    return {
      root: candidate,
      installed: true,
      error: "git context conflicts with the registered project root",
    };
  }
  if (
    !registeredRoot
    && git.leader
    && projects
    && Object.prototype.hasOwnProperty.call(projects, git.leader)
  ) {
    registeredRoot = git.leader;
  }
  if (git.failed) {
    return {
      root: registeredRoot || directRoot || candidate,
      installed: Boolean(registeredRoot || directRoot),
      error: registeredRoot || directRoot || git.nonDirectoryMarker
        ? "git repository context cannot be resolved safely: " + candidate
        : "",
    };
  }
  if (
    git.leader
    && !git.linked
    && directRoot
    && git.leader !== directRoot
    && !isAncestorOrSame(git.leader, directRoot)
  ) {
    return {
      root: candidate,
      installed: false,
      error: "git context conflicts with the registered project root",
    };
  }
  if (registeredRoot) {
    return { root: registeredRoot, installed: true, error: "" };
  }
  let root = "";
  if (git.linked) {
    root = git.leader;
    if (!root || !hasInstall(root)) {
      return {
        root: candidate,
        installed: hasInstall(candidate),
        error: hasInstall(candidate)
          ? "linked worktree manifests are not trusted; install from the leader checkout"
          : "",
      };
    }
  } else {
    root = directRoot;
  }
  if (!root) {
    return { root: candidate, installed: false, error: "" };
  }
  if (registryError) {
    return {
      root: canonicalOmpPath(root),
      installed: true,
      error: registryError,
    };
  }
  return { root: canonicalOmpPath(root), installed: false, error: "" };
}



const WRITE_TOOL_RE = /^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|write_file|edit_file)$/i;
const COMMAND_TOOL_RE = /^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$/i;
const READ_TOOL_RE = /^(Read|read|read_file|view|cat)$/i;
const SKILL_TOOL_RE = /^(Skill|skill)$/i;


export default function agentFlowHooks(pi) {
  if (typeof pi.setLabel === "function") {
    pi.setLabel("agent-flow hooks");
  }
  hookLogger = pi?.logger ?? null;

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
    const toolName = String(event?.toolName || "");
    if (!COMMAND_TOOL_RE.test(toolName) && !WRITE_TOOL_RE.test(toolName)) {
      return;
    }
    const result = await runHook("PreToolUse", hookPayload(event, ctx), ctx);
    if (result.block) {
      return { block: true, reason: result.reason };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    const toolName = String(event?.toolName || "");
    if (
      !COMMAND_TOOL_RE.test(toolName)
      && !WRITE_TOOL_RE.test(toolName)
      && !READ_TOOL_RE.test(toolName)
      && !SKILL_TOOL_RE.test(toolName)
    ) {
      return;
    }
    const payload = COMMAND_TOOL_RE.test(toolName)
      ? commandRunPayload(event, ctx)
      : hookPayload(event, ctx);
    const result = await runHook("PostToolUse", payload, ctx);
    if (result.block) {
      return {
        content: [{ type: "text", text: result.reason }],
        details: { agentFlowHook: "PostToolUse" },
        isError: true,
      };
    }
    if (WRITE_TOOL_RE.test(toolName)) {
      const syncError = syncActiveRootContextFiles(event, ctx);
      if (syncError) {
        reportHostSideEffectFailure(syncError);
      }
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const result = await runHook(
      "Stop",
      {
        hook_event_name: "session_shutdown",
        cwd: ctx?.cwd || process.cwd(),
      },
      ctx,
    );
    const message = parseSystemMessage(result.reason);
    if (message && ctx?.hasUI && typeof ctx.ui?.notify === "function") {
      await ctx.ui.notify(message, "info");
    }
  });
}


function commandRunPayload(event, ctx) {
  // exit code는 관측 hook이 보는 유일한 결과 신호다. host가 안 실어 보내면
  // 없는 채로 기록된다 — 없는 것과 0을 섞지 않는다.
  const payload = hookPayload(event, ctx);
  const result = event?.output ?? event?.result ?? event?.toolResult ?? event ?? null;
  const detail = event?.details ?? (
    result && typeof result === "object" ? result.details : null
  );
  const directCode = event && typeof event === "object"
    ? event.exit_code ?? event.exitCode ?? null
    : null;
  const detailCode = detail && typeof detail === "object"
    ? detail.exit_code ?? detail.exitCode ?? null
    : null;
  const resultCode = result && typeof result === "object"
    ? result.exit_code ?? result.exitCode ?? null
    : null;
  const code = directCode ?? detailCode ?? resultCode;
  const isError = event?.isError ?? (
    result && typeof result === "object" ? result.isError : null
  );
  const completedForeground = detail && typeof detail === "object"
    && typeof detail.wallTimeMs === "number"
    && detail.timedOut !== true
    && detail.timed_out !== true
    && detail.async == null;
  if (typeof code === "number") {
    payload.exit_code = code;
  } else if (isError === false && completedForeground) {
    // OMP v17.2.1은 완료된 foreground Bash 성공에서 exitCode를 생략하고
    // isError=false만 준다. running/timeout 결과를 성공으로 기록하지 않는다.
    payload.exit_code = 0;
  }
  const output = commandOutputText(result);
  if (output) {
    payload.output = output.slice(-65_536);
  }
  return payload;
}

function commandOutputText(value, depth = 0) {
  if (depth > 5 || value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => commandOutputText(item, depth + 1)).filter(Boolean).join("\n");
  }
  if (typeof value !== "object") {
    return "";
  }
  return ["stdout", "text", "content", "message", "output"]
    .map((key) => commandOutputText(value[key], depth + 1))
    .filter(Boolean)
    .join("\n");
}

function hookPayload(event, ctx) {
  const input = event?.input || {};
  const toolName = String(event?.toolName || "");
  return {
    tool_name: toolName,
    tool: toolName,
    hook_event_name: String(event?.type || ""),
    tool_input: input,
    input,
    parameters: input,
    cwd: ctx?.cwd || process.cwd(),
    session_id: sessionIdentity(event, ctx),
  };
}


function sessionIdentity(event, ctx) {
  const direct = String(
    event?.session_id
      ?? event?.sessionId
      ?? ctx?.session_id
      ?? ctx?.sessionId
      ?? ctx?.session?.id
      ?? "",
  ).trim();
  if (direct) {
    return direct;
  }
  // 실측(omp 17.1.8, probe 확장으로 input/tool_call/tool_result를 기록): 이벤트에는
  // 세션 식별자가 없어 direct는 항상 비고, 이 getter가 현재 이벤트 세션의 id를
  // 제공한다. 같은 실측에서 task subagent는 부모와 다른 id를 받았다.
  try {
    const managed = ctx?.sessionManager?.getSessionId?.();
    if (typeof managed === "string" && managed.trim()) {
      return managed.trim();
    }
  } catch {
    // session manager가 없는 이전 host에서는 process 환경값을 마지막으로 사용한다.
  }
  return String(process.env.OMP_SESSION_ID ?? "").trim();
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


function syncRootContextFiles(event, ctx, installRoot) {
  const direction = rootContextSyncDirection(event, ctx, installRoot);
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

function rootContextSyncDirection(event, ctx, installRoot) {
  const cwd = ctx?.cwd || process.cwd();
  const changed = modifiedRootContextFiles(event?.input, cwd, installRoot);
  if (changed.has("CLAUDE.md")) {
    return {
      sourceName: "CLAUDE.md",
      destName: "AGENTS.md",
      sourcePath: path.join(installRoot, "CLAUDE.md"),
      destPath: path.join(installRoot, "AGENTS.md"),
    };
  }
  if (changed.has("AGENTS.md")) {
    return {
      sourceName: "AGENTS.md",
      destName: "CLAUDE.md",
      sourcePath: path.join(installRoot, "AGENTS.md"),
      destPath: path.join(installRoot, "CLAUDE.md"),
    };
  }
  return null;
}

function modifiedRootContextFiles(input, cwd, installRoot) {
  const changed = new Set();
  for (const filePath of collectModifiedPaths(input)) {
    const fileName = rootContextFileName(filePath, cwd, installRoot);
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

function rootContextFileName(filePath, cwd, installRoot) {
  const resolved = path.resolve(cwd || installRoot, filePath);
  for (const fileName of ["CLAUDE.md", "AGENTS.md"]) {
    if (samePath(resolved, path.join(installRoot, fileName))) {
      return fileName;
    }
  }
  return "";
}

function samePath(left, right) {
  return path.resolve(left) === path.resolve(right);
}


let hookLogger = null;
let hostSideEffectFailureReported = false;

function reportHostSideEffectFailure(message) {
  if (hostSideEffectFailureReported) {
    return;
  }
  hostSideEffectFailureReported = true;
  for (const level of ["warn", "info", "log"]) {
    if (typeof hookLogger?.[level] === "function") {
      try {
        hookLogger[level](message);
        return;
      } catch {
      }
    }
  }
  try {
    process.stderr.write(message + "\n");
  } catch {
  }
}

function syncActiveRootContextFiles(event, ctx) {
  try {
    const selected = resolveTrustedInstall(ctx?.cwd || process.cwd());
    if (selected.error || !selected.installed) {
      return "";
    }
    if (trustedManifest(selected.root).manifest?.hooks !== true) {
      return "";
    }
    return syncRootContextFiles(event, ctx, selected.root);
  } catch {
    return "";
  }
}

function isolatedEnvironment() {
  return Object.fromEntries(Object.entries(process.env).filter(([name]) => (
    !name.toUpperCase().startsWith("PYTHON") &&
    name !== "__PYVENV_LAUNCHER__" &&
    name !== "_PYTHON_SYSCONFIGDATA_NAME"
  )));
}

async function runHook(eventName, payload, ctx) {
  const eventCwd = ctx?.cwd || payload?.cwd || process.cwd();
  const normalizedPayload = { ...payload, cwd: eventCwd };
  const result = await spawnHook(
    eventName,
    JSON.stringify(normalizedPayload),
  );
  const reason = (result.stderr || result.stdout || "").trim();
  if (result.status === 0) {
    return { block: false, reason };
  }
  return {
    block: true,
    reason: reason || "agent-flow hook blocked: " + eventName,
  };
}

function terminateHookProcessTree(proc) {
  if (process.platform === "win32") {
    try {
      execFileSync(
        "taskkill",
        ["/PID", String(proc.pid), "/T", "/F"],
        { stdio: "ignore", timeout: 5000, windowsHide: true },
      );
    } catch {
      try {
        proc.kill("SIGKILL");
      } catch {
      }
    }
    return;
  }
  try {
    process.kill(-proc.pid, "SIGKILL");
  } catch {
    try {
      proc.kill("SIGKILL");
    } catch {
    }
  }
}

function timeoutDiagnostic(stderr) {
  if (!stderr) {
    return "agent-flow hook timed out";
  }
  return stderr + (stderr.endsWith("\n") ? "" : "\n") + "agent-flow hook timed out";
}


function spawnHook(eventName, input) {
  return new Promise((resolve) => {
    const proc = spawn(
      HOOK_PYTHON,
      ["-I", "-c", HOOK_BOOTSTRAP, "--event", eventName],
      {
        cwd: STABLE_CWD,
        env: isolatedEnvironment(),
        stdio: ["pipe", "pipe", "pipe"],
        detached: process.platform !== "win32",
      },
    );
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timedOut = false;
    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      timedOut = true;
      terminateHookProcessTree(proc);
    }, HOOK_TIMEOUT_MS);
    proc.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    proc.on("error", (error) => {
      finish({
        status: 126,
        stdout,
        stderr: stderr || String(error?.message || "agent-flow hook failed to start"),
      });
    });
    proc.on("close", (status, signal) => {
      finish({
        status: timedOut ? 124 : (status ?? 1),
        stdout,
        stderr: timedOut
          ? timeoutDiagnostic(stderr)
          : (stderr || (signal ? "agent-flow hook terminated by " + signal : "")),
      });
    });
    proc.stdin.on("error", () => {});
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
