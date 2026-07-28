// OMP host에 심는 관리형 확장의 소스. 두 진입점(`agent-flow-kit.mjs`,
// `agent-flow-install.mjs`)이 같은 바이트를 심어야 한다 — 예전에 한쪽만
// `tool_result` 핸들러 등록 순서가 달라서, 이벤트당 핸들러 하나만 남기는 host에서
// 루트 컨텍스트 동기화가 통째로 죽었다. 사본을 두고 대조하는 대신 한 벌만 둔다.
export const OMP_EXTENSION_MARKER = "agent-flow: managed omp extension";

export function ompHooksExtensionSource() {
  return String.raw`// ${OMP_EXTENSION_MARKER}
import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const HOOK_DIR = path.join(ROOT, ".agent-flow", "scripts", "hooks");
const WRITE_TOOL_RE = /^(apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit)$/i;
const READ_TOOL_RE = /^(Read|read|read_file|view|cat)$/i;
const COMMAND_TOOL_RE = /^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal)$/i;

export default function agentFlowHooks(pi) {
  if (typeof pi.setLabel === "function") {
    pi.setLabel("agent-flow hooks");
  }


  pi.on("input", async (event, ctx) => {
    if (event?.source !== "interactive") {
      return;
    }
    const prompt = inputPrompt(event);
    if (!prompt) {
      return;
    }
    const payload = {
      hook_event_name: "UserPromptSubmit",
      cwd: ctx?.cwd || ROOT,
      prompt,
      session_id: sessionIdentity(event, ctx),
    };
    await runHook("prepare-spec-user-prompt.py", payload, ctx);
    await runHook("confirm-spec-user-prompt.py", payload, ctx);
  });
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
      ? ["guard-protected-branch.sh", "guard-host-worktree.sh", "guard-spec-approval.sh"]
      : ["guard-host-worktree.sh", "guard-spec-approval.sh"];
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
    if (READ_TOOL_RE.test(toolName)) {
      // 관측 전용이다. 결과를 보지 않고, 어떤 경우에도 read를 막지 않는다.
      await runHook("record-skill-read.py", hookPayload(event, ctx), ctx);
      return;
    }
    if (COMMAND_TOOL_RE.test(toolName)) {
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
      await runHook("prepare-spec-user-prompt.py", payload, ctx);
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
    await runHook("prepare-spec-user-prompt.py", hookPayload(event, ctx), ctx);
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
  const code = result && typeof result === "object"
    ? result.exit_code ?? result.exitCode ?? null
    : null;
  if (typeof code === "number") {
    payload.exit_code = code;
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
  return String(
    event?.session_id
      ?? event?.sessionId
      ?? ctx?.session_id
      ?? ctx?.sessionId
      ?? ctx?.session?.id
      ?? process.env.OMP_SESSION_ID
      ?? "",
  ).trim();
}

function inputPrompt(event) {
  if (typeof event === "string") {
    return event;
  }
  for (const key of ["prompt", "text", "message"]) {
    if (typeof event?.[key] === "string") {
      return event[key];
    }
  }
  if (event?.message && typeof event.message === "object") {
    return messageText(event.message);
  }
  return "";
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


async function runHook(scriptName, payload, ctx) {
  const scriptPath = path.join(HOOK_DIR, scriptName);
  const result = await spawnHook(scriptName, scriptPath, JSON.stringify(payload), ctx?.cwd || ROOT);
  const reason = (result.stderr || result.stdout || "").trim();
  if (result.status === 0) {
    return { block: false, reason };
  }
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
    }, 8000);
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
