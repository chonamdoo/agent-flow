// OMP host에 심는 관리형 확장의 소스. 두 진입점(`agent-flow-kit.mjs`,
// `agent-flow-install.mjs`)이 같은 바이트를 심어야 한다 — 예전에 한쪽만
// `tool_result` 핸들러 등록 순서가 달라서, 이벤트당 핸들러 하나만 남기는 host에서
// 루트 컨텍스트 동기화가 통째로 죽었다. 사본을 두고 대조하는 대신 한 벌만 둔다.
export const OMP_EXTENSION_MARKER = "agent-flow: managed omp extension";

export function ompHooksExtensionSource() {
  return String.raw`// ${OMP_EXTENSION_MARKER}
import fs from "node:fs";
import os from "node:os";
import { execFileSync, spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

function hasInstall(dir) {
  // 판정 기준은 .agent-flow/kit.json 하나뿐이다 — Python 쪽 형제 함수
  // hook_integrity.find_install_root()와 같은 조건이어야 두 구현이 같은 설치본을
  // 고른다. 여기에 scripts/hooks 존재를 더하면, hooks가 부분 삭제된 설치본을
  // 건너뛰고 조상의 다른 프로젝트 설치본을 집는다.
  return fs.existsSync(path.join(dir, ".agent-flow", "kit.json"));
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

function gitCommonRoot(start) {
  // toplevel이 아니라 common root를 쓴다. managed checkout은
  // <leader>/.agent-flow/worktrees/<name>인 별개 worktree라 자기 toplevel에서
  // 멈추면 정작 찾아야 할 leader 설치본에 닿지 못한다.
  try {
    const commonDir = execFileSync("git", ["rev-parse", "--git-common-dir"], {
      cwd: start,
      env: gitEnv(),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    }).trim();
    return commonDir ? path.dirname(path.resolve(start, commonDir)) : "";
  } catch {
    return "";
  }
}

// worktree checkout에서 시작된 OMP 세션은 ROOT가 worktree라 hook 스크립트가 거기 없다.
// 조상을 훑되 경계를 둔다: 경계가 없으면 설치본이 없는 checkout이 조상에 있는
// 남의 프로젝트 설치본을 집어, 그쪽 hook 스크립트를 이 cwd로 실행한다. 여기서
// 고른 값은 보고용이 아니라 실제로 실행할 hook 디렉터리다.
function resolveInstallRoot(start) {
  const home = os.homedir();
  const gitRoot = gitCommonRoot(start);
  const boundary = isAncestorOrSame(gitRoot, start)
    ? gitRoot
    : (isAncestorOrSame(home, start) ? home : start);
  let current = start;
  for (;;) {
    // HOME 자신은 후보가 아니다. ~/.agent-flow/kit.json 하나가 그 아래 모든
    // 프로젝트를 삼킨다. cli.py의 _managed_worktree_context는 .codex/.omp 마커에만
    // HOME을 건너뛰므로 이건 그보다 좁은 경계다.
    if (current !== home && hasInstall(current)) {
      return current;
    }
    const parent = path.dirname(current);
    if (current === boundary || parent === current) {
      break;
    }
    current = parent;
  }
  // leader가 조상이 아닌 수동 worktree라도 같은 저장소의 설치본은 남의 것이 아니다.
  if (gitRoot && gitRoot !== home && hasInstall(gitRoot)) {
    return gitRoot;
  }
  return start;
}

const INSTALL_ROOT = resolveInstallRoot(ROOT);
const HOOK_DIR = path.join(INSTALL_ROOT, ".agent-flow", "scripts", "hooks");
const INSTALL_ROOT_REASON = hasInstall(INSTALL_ROOT)
  ? ""
  : "no .agent-flow/kit.json at or above " + ROOT + " inside its git repository";
const WRITE_TOOL_RE = /^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$/i;
const READ_TOOL_RE = /^(Read|read|read_file|view|cat)$/i;
// Skill tool과 셸 읽기도 사용 증거다. Read만 보면 두 축이 관측되지 않는다.
const SKILL_TOOL_RE = /^(Skill|skill)$/i;
const COMMAND_TOOL_RE = /^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$/i;


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
    const commandTool = COMMAND_TOOL_RE.test(toolName);
    if (!commandTool && !WRITE_TOOL_RE.test(toolName)) {
      return;
    }
    const payload = hookPayload(event, ctx);
    const scripts = commandTool
      ? ["guard-protected-branch.sh", "guard-host-worktree.sh"]
      : ["guard-host-worktree.sh"];
    for (const scriptName of scripts) {
      const result = await runHook(scriptName, payload, ctx);
      if (result.block) {
        return { block: true, reason: result.reason };
      }
    }
  });

  // tool_result 핸들러는 **하나만** 둔다. 이벤트당 하나만 유지하는 host가 있어
  // 두 번 등록하면 뒤엣것이 앞엣것을 조용히 덮는다.
  pi.on("tool_result", async (event, ctx) => {
    const toolName = String(event?.toolName || "");
    if (READ_TOOL_RE.test(toolName) || SKILL_TOOL_RE.test(toolName)) {
      // 관측 전용이다. 결과를 보지 않고, 어떤 경우에도 read를 막지 않는다.
      await runHook("record-skill-read.py", hookPayload(event, ctx), ctx);
      return;
    }
    if (COMMAND_TOOL_RE.test(toolName)) {
      // 셸로 SKILL.md를 여는 경로(Codex에는 Read tool이 없다)도 같은 관측자에게 보낸다.
      await runHook("record-skill-read.py", hookPayload(event, ctx), ctx);
      // 관측 전용. tool_call(PreToolUse에 해당)이 아니라 여기 붙는다 — 관측자가
      // 판정자로 승격되면 실패한 관측이 곧 사용자 도구 차단이 된다.
      const payload = commandRunPayload(event, ctx);
      await runHook("record-command-run.py", payload, ctx);
      const binding = await runHook("bind-host-worktree.py", payload, ctx);
      if (binding.block) {
        return {
          content: [{ type: "text", text: binding.reason }],
          details: { agentFlowHook: "bind-host-worktree.py" },
          isError: true,
        };
      }
      const boundary = await runHook("guard-host-worktree.sh", payload, ctx);
      if (boundary.block) {
        return {
          content: [{ type: "text", text: boundary.reason }],
          details: { agentFlowHook: "guard-host-worktree.sh" },
          isError: true,
        };
      }
      const tripwire = await runHook("worktree-tripwire.py", payload, ctx);
      if (tripwire.block) {
        return {
          content: [{ type: "text", text: tripwire.reason }],
          details: { agentFlowHook: "worktree-tripwire.py" },
          isError: true,
        };
      }
      return;
    }
    if (!WRITE_TOOL_RE.test(toolName)) {
      return;
    }
    const boundary = await runHook("guard-host-worktree.sh", hookPayload(event, ctx), ctx);
    if (boundary.block) {
      return {
        content: [{ type: "text", text: boundary.reason }],
        details: { agentFlowHook: "guard-host-worktree.sh" },
        isError: true,
      };
    }
    const syncError = syncRootContextFiles(event, ctx);
    if (syncError) {
      return {
        content: [{ type: "text", text: syncError }],
        details: { agentFlowHook: "sync-root-context" },
        isError: true,
      };
    }
    const result = await runHook("comment-checker.py", hookPayload(event, ctx), ctx);
    if (result.block) {
      return {
        content: [{ type: "text", text: result.reason }],
        details: { agentFlowHook: "comment-checker.py" },
        isError: true,
      };
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const result = await runHook("show-phase-status.sh", { hook_event_name: "session_shutdown" }, ctx);
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
    cwd: ctx?.cwd || ROOT,
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


let hookLogger = null;
let hooksNotRegisteredReported = false;

// 등록 실패를 사용자에게 보이는 유일한 경로다. 세션당 한 번만 낸다 — 도구 호출마다
// 같은 줄을 쌓으면 사용자가 읽지 않는다.
function reportHooksNotRegistered(reason) {
  if (hooksNotRegisteredReported) {
    return;
  }
  hooksNotRegisteredReported = true;
  const message = "agent-flow hooks are not registered: " + reason;
  for (const level of ["warn", "info", "log"]) {
    if (typeof hookLogger?.[level] === "function") {
      try {
        hookLogger[level](message);
        return;
      } catch {
      }
    }
  }
  process.stderr.write(message + "\n");
}


function hookScriptIsInstalled(scriptPath) {
  try {
    return fs.statSync(scriptPath).isFile();
  } catch {
    return false;
  }
}

async function runHook(scriptName, payload, ctx) {
  const scriptPath = path.join(HOOK_DIR, scriptName);
  if (!hookScriptIsInstalled(scriptPath)) {
    // 설치본 자체를 못 찾은 것과, 설치본은 있는데 관리 hook만 사라진 것은 다른
    // 사건이다. 앞은 이 프로젝트가 agent-flow를 안 쓰는 상태라 도구를 막으면
    // 세션 전체가 죽는다. 뒤는 가드 제거이므로 fail-closed로 남긴다 — 삭제 한 번으로
    // 승인 가드가 사라지면 hook_integrity(런 시작 1회)도 그 사이를 못 본다.
    reportHooksNotRegistered(INSTALL_ROOT_REASON || "no hook script at " + scriptPath);
    if (INSTALL_ROOT_REASON) {
      return { block: false, reason: "" };
    }
    return { block: true, reason: "agent-flow managed hook is missing: " + scriptPath };
  }
  const result = await spawnHook(scriptName, scriptPath, JSON.stringify(payload), ctx?.cwd || ROOT);
  const reason = (result.stderr || result.stdout || "").trim();
  if (result.status === 0) {
    return { block: false, reason };
  }
  // 스크립트가 있는데 0이 아닌 종료로 끝난 것은 판정이다. 그대로 막는다.
  return { block: true, reason: reason || "agent-flow hook blocked: " + scriptName };
}

function spawnHook(scriptName, scriptPath, input, cwd) {
  return new Promise((resolve) => {
    const command = scriptName.endsWith(".py") ? "/usr/bin/python3" : "/bin/bash";
    const args = scriptName.endsWith(".py") ? ["-I", scriptPath] : [scriptPath];
    const proc = spawn(command, args, { cwd, stdio: ["pipe", "pipe", "pipe"] });
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
    }, 15000);
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
        status: status ?? 1,
        stdout,
        stderr: stderr || (signal ? "agent-flow hook terminated by " + signal : ""),
      });
    });
    // hook이 stdin을 읽기 전에 끝나면 이 쓰기가 EPIPE를 던진다. 핸들러가 없으면
    // 그 error 이벤트가 세션 프로세스를 통째로 죽인다 — 빠르게 차단하는 가드
    // 하나가 host를 내리는 것이므로 무시하고 close 결과만 기다린다.
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
