import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import { lintProfiles } from "./architecture-lint.mjs";
import yamlRuntime from "./yaml-runtime-bundled.cjs";

const { parseInstalledProfileYaml } = yamlRuntime;

const PROFILE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const ARCHITECTURE_OPTIONS = new Set(["--root", "--profile", "--files", "--worktree"]);
const GATE_OPTIONS = new Set(["--root", "--profile", "--run-dir", "--timeout", "--worktree"]);
const HELP_FLAGS = new Set(["-h", "--help"]);
const ATTACHED_OPTION_VALUE = Symbol("attached-option-value");

export function nativeGateHelpFromArgs(command, argv) {
  if (!Array.isArray(argv) || !["architecture-lint", "gates"].includes(command)) return null;
  const options = command === "architecture-lint" ? ARCHITECTURE_OPTIONS : GATE_OPTIONS;
  const normalized = expandOptionAssignments(argv, options);
  if (!nativeGateHelpIsEffective(normalized, options, command === "architecture-lint")) return null;
  return command === "architecture-lint"
    ? (
        "usage: agent-flow architecture-lint [-h] [--root ROOT] [--profile PROFILE]\n" +
        "                                    [--files [FILES ...]] [--worktree WORKTREE]\n"
      )
    : (
        "usage: agent-flow gates [-h] [--root ROOT] [--profile PROFILE]\n" +
        "                        [--run-dir RUN_DIR] [--timeout TIMEOUT] [--worktree WORKTREE]\n"
      );
}

export function parseArchitectureLintArgs(argv) {
  argv = expandOptionAssignments(argv, ARCHITECTURE_OPTIONS);
  const options = { root: ".", profile: "auto", files: null, worktree: null };
  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (!ARCHITECTURE_OPTIONS.has(option)) throw new Error(`unrecognized arguments: ${option}`);
    if (option === "--files") {
      const files = [];
      while (index + 1 < argv.length && !looksLikeOption(argv[index + 1])) {
        files.push(argumentText(argv[index + 1]));
        index += 1;
      }
      options.files = files;
      continue;
    }
    const value = requiredOptionValue(argv, index, option, ARCHITECTURE_OPTIONS);
    if (option === "--root") options.root = value;
    else if (option === "--profile") options.profile = value;
    else options.worktree = value;
    index += 1;
  }
  return options;
}

export function parseGatesArgs(argv) {
  argv = expandOptionAssignments(argv, GATE_OPTIONS);
  const options = { root: ".", profile: "auto", runDir: null, timeoutSeconds: 600, worktree: null };
  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (!GATE_OPTIONS.has(option)) throw new Error(`unrecognized arguments: ${option}`);
    const value = requiredOptionValue(argv, index, option, GATE_OPTIONS);
    if (option === "--root") options.root = value;
    else if (option === "--profile") options.profile = value;
    else if (option === "--run-dir") options.runDir = value;
    else if (option === "--worktree") options.worktree = value;
    else options.timeoutSeconds = parseIntegerOption(option, value);
    index += 1;
  }
  return options;
}

export function requestedProfileIds(requested, {
  autoProfileIds,
  loadInstalledProfile = null,
  loadCanonicalProfile,
} = {}) {
  const ids = requested === "auto"
    ? dedupeProfileIds(autoProfileIds)
    : dedupeProfileIds(String(requested).split(",").filter((profileId) => profileId.trim()));
  const validator = typeof loadInstalledProfile === "function" ? loadInstalledProfile : loadCanonicalProfile;
  if (typeof validator !== "function") throw new Error("profile selection requires a profile loader");
  for (const profileId of ids) validator(profileId);
  return ids;
}

export function createCanonicalProfileLoader(profileRoot) {
  const lexicalRoot = path.resolve(profileRoot);
  return (profileId) => {
    validateProfileId(profileId);
    const profilePath = path.join(lexicalRoot, `${profileId}.yaml`);
    if (!fs.existsSync(profilePath)) throw new Error(`unknown profile: ${profileId}`);
    requirePlainChildFile(lexicalRoot, profilePath, `canonical profile ${profileId}`);
    const payload = parseInstalledProfileYaml(
      fs.readFileSync(profilePath, "utf8"),
      profileId,
      profilePath,
    );
    return normalizeProfile(payload, profileId);
  };
}

export function profileGateCommands(profileIds, { loadProfile, pythonExecutable = "python3" } = {}) {
  if (typeof loadProfile !== "function") throw new Error("gates require a profile loader");
  const ids = dedupeProfileIds(profileIds);
  const commands = [];
  const seen = new Set();
  const multiProfile = ids.length > 1;
  let architectureLintAdded = false;
  let order = 0;
  for (const profileId of ids) {
    const profile = normalizeProfile(loadProfile(profileId), profileId);
    for (const gate of profile.gates) {
      let command = normalizeProfileGateCommand(
        profileId,
        gate.id,
        gate.command,
        pythonExecutable,
      );
      let gateId = multiProfile ? `${profileId}:${gate.id}` : gate.id;
      let required = gate.required;
      if (multiProfile && isArchitectureLintGate(gate.id, gate.command)) {
        if (architectureLintAdded) continue;
        command = architectureLintCommand(ids);
        gateId = "architecture-lint";
        required = true;
        architectureLintAdded = true;
      }
      const key = JSON.stringify(command);
      if (seen.has(key)) continue;
      seen.add(key);
      commands.push({ gate_id: gateId, command, required, order });
      order += 1;
    }
  }
  commands.sort((left, right) => {
    const leftKey = gateOrderKey(left);
    const rightKey = gateOrderKey(right);
    for (let index = 0; index < leftKey.length; index += 1) {
      const compared = compareValues(leftKey[index], rightKey[index]);
      if (compared !== 0) return compared;
    }
    return left.order - right.order;
  });
  return commands.map(({ order: _order, ...command }) => command);
}

export function runGate(command, {
  cwd,
  timeoutSeconds = 600,
  profileIds = [],
  loadProfile,
  env = process.env,
} = {}) {
  const workingDirectory = fs.realpathSync(path.resolve(cwd));
  const recordedCommand = command.command.map((part) => recordCommandPart(part, workingDirectory));
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
    return failedGateResult(command, recordedCommand, "", "");
  }
  if (isArchitectureLintGate(command.gate_id, command.command)) {
    const selectedProfiles = architectureProfiles(command.command, profileIds);
    return architectureGateResult({
      command,
      recordedCommand,
      cwd: workingDirectory,
      profileIds: selectedProfiles,
      loadProfile,
    });
  }
  let completed;
  try {
    completed = spawnSync(command.command[0], command.command.slice(1), {
      cwd: workingDirectory,
      env: { ...env },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: timeoutSeconds * 1000,
      maxBuffer: 32 * 1024 * 1024,
    });
  } catch (error) {
    return failedGateResult(command, recordedCommand, "", errorMessage(error));
  }
  const stdout = outputText(completed.stdout);
  const stderr = outputText(completed.stderr);
  if (completed.error) {
    if (completed.error.code === "ETIMEDOUT") {
      return failedGateResult(command, recordedCommand, stdout, stderr);
    }
    return failedGateResult(
      command,
      recordedCommand,
      stdout,
      stderr || osErrorMessage(completed.error, command.command[0]),
    );
  }
  return {
    gate_id: command.gate_id,
    command: recordedCommand,
    passed: completed.status === 0,
    exit_code: Number.isInteger(completed.status) ? completed.status : null,
    stdout,
    stderr,
    required: command.required !== false,
  };
}

export function runGates(commands, options = {}) {
  return commands.map((command) => runGate(command, options));
}

export function runArchitectureLint(root, profileIds, { files = null, loadProfile } = {}) {
  const ids = dedupeProfileIds(profileIds);
  const findingsByProfile = lintProfiles(path.resolve(root), ids, { files, loadProfile });
  const failed = Object.values(findingsByProfile).some((findings) => findings.length > 0);
  if (!failed) {
    return {
      passed: true,
      stdout: `${ids.join(",")}: architecture lint passed\n`,
      stderr: "",
      findings_by_profile: findingsByProfile,
    };
  }
  const lines = [`${ids.join(",")}: architecture lint failed`];
  for (const [profileId, findings] of Object.entries(findingsByProfile)) {
    for (const finding of findings) {
      lines.push(`- [${profileId}] ${finding.path}: ${finding.message}`);
    }
  }
  return {
    passed: false,
    stdout: "",
    stderr: `${lines.join("\n")}\n`,
    findings_by_profile: findingsByProfile,
  };
}

export function writeGateResults(runDir, results) {
  const resolvedRunDir = path.resolve(runDir);
  const serializedResults = results.map(gateResultPayload);
  const passed = results.every((result) => result.passed || result.required === false);
  const payload = {
    passed,
    status: passed ? "green" : "request-changes",
    results: serializedResults,
  };
  const artifactPath = path.join(resolvedRunDir, "artifacts", "gate-results.json");
  const legacyPath = path.join(resolvedRunDir, "gate-results.json");
  fs.mkdirSync(path.dirname(artifactPath), { recursive: true });
  fs.writeFileSync(artifactPath, pythonJson(payload), "utf8");
  fs.writeFileSync(legacyPath, pythonJson(serializedResults), "utf8");
  writeRunReport(resolvedRunDir);
  return artifactPath;
}

export function gateResultPayload(result) {
  const argv = [...result.command];
  return {
    gate_id: result.gate_id,
    command: argv.join(" "),
    argv,
    passed: result.passed === true,
    required: result.required !== false,
    exit_code: Number.isInteger(result.exit_code) ? result.exit_code : null,
    stdout: outputText(result.stdout),
    stderr: outputText(result.stderr),
  };
}

export function writeRunReport(runDir) {
  const resolvedRunDir = path.resolve(runDir);
  const artifacts = collectArtifacts(resolvedRunDir);
  const manifest = readJsonIfValid(path.join(resolvedRunDir, "manifest.json"))
    ?? readJsonIfValid(path.join(resolvedRunDir, "meta.json"));
  const gateResults = readJsonIfValid(path.join(resolvedRunDir, "gate-results.json"));
  const reviewSummary = readJsonIfValid(path.join(resolvedRunDir, "review-summary.json"));
  const artifactBlockedCount = artifacts.filter(isBlockedArtifact).length;
  const reviewBlocked = isMapping(reviewSummary)
    && String(reviewSummary.verdict || "").toUpperCase() === "NEEDS_CHANGES"
    && artifactBlockedCount === 0;
  const lines = [
    "# Run Report",
    "",
    "## Summary",
    "",
    `- Run: ${manifestValue(manifest, ["run_id"], path.basename(resolvedRunDir))}`,
    `- Workflow: ${manifestValue(manifest, ["workflow_id", "workflow"], "unknown")}`,
    `- Task: ${manifestValue(manifest, ["task"], "")}`,
    `- Artifacts: ${artifacts.length}`,
    `- Blocked: ${artifactBlockedCount + Number(reviewBlocked)}`,
    `- Stale: ${artifacts.filter((artifact) => artifact.stale).length}`,
    "",
    "## Artifacts",
    "",
  ];
  if (artifacts.length === 0) {
    lines.push("- None");
  } else {
    for (const artifact of artifacts) {
      const relative = path.relative(resolvedRunDir, artifact.path).split(path.sep).join("/");
      const details = [
        `- \`${artifact.stage_id}\``,
        `path=\`${relative}\``,
        `status=${artifact.status}`,
      ];
      if (artifact.verdict) details.push(`verdict=${artifact.verdict}`);
      details.push(`evidence=${artifact.evidence_type}`);
      details.push(`confidence=${artifact.confidence}`);
      if (artifact.stale) details.push("stale=true");
      lines.push(details.join(" "));
    }
  }
  lines.push("", "## Gates", "");
  if (Array.isArray(gateResults)) {
    for (const gate of gateResults) {
      if (isMapping(gate)) {
        lines.push(`- \`${gate.gate_id ?? "unknown"}\` ${gate.passed ? "passed" : "failed"}`);
      }
    }
  } else {
    lines.push("- Not recorded");
  }
  lines.push("", "## Review Summary", "");
  if (isMapping(reviewSummary)) {
    lines.push(`- Verdict: ${String(reviewSummary.verdict || "unknown")}`);
    lines.push(`- Findings: ${Array.isArray(reviewSummary.findings) ? reviewSummary.findings.length : 0}`);
  } else {
    lines.push("- Not recorded");
  }
  lines.push("");
  const reportPath = path.join(resolvedRunDir, "RUN_REPORT.md");
  fs.writeFileSync(reportPath, lines.join("\n"), "utf8");
  return reportPath;
}

export function pythonJson(value) {
  return `${JSON.stringify(sortJsonKeys(value), null, 2).replace(/[\u007f-\uffff]/g, (char) => (
    `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`
  ))}\n`;
}

export function gateSummary(profileIds, results) {
  const failed = results.filter((result) => !result.passed);
  const required = results.filter((result) => result.required !== false);
  const failedRequired = required.filter((result) => !result.passed);
  if (failed.some((result) => result.required === false)) {
    return {
      message: `${profileIds.join(",")}: ${required.length - failedRequired.length}/${required.length} required gates passed (${results.length - failed.length}/${results.length} total gates passed)`,
      exitCode: failedRequired.length > 0 ? 1 : 0,
    };
  }
  return {
    message: `${profileIds.join(",")}: ${results.length - failed.length}/${results.length} gates passed`,
    exitCode: failedRequired.length > 0 ? 1 : 0,
  };
}

function architectureGateResult({ command, recordedCommand, cwd, profileIds, loadProfile }) {
  try {
    const output = runArchitectureLint(cwd, profileIds, { loadProfile });
    return {
      gate_id: command.gate_id,
      command: recordedCommand,
      passed: output.passed,
      exit_code: output.passed ? 0 : 1,
      stdout: output.stdout,
      stderr: output.stderr,
      required: command.required !== false,
    };
  } catch (error) {
    return failedGateResult(command, recordedCommand, "", errorMessage(error));
  }
}

function failedGateResult(command, recordedCommand, stdout, stderr) {
  return {
    gate_id: command.gate_id,
    command: recordedCommand,
    passed: false,
    exit_code: null,
    stdout,
    stderr,
    required: command.required !== false,
  };
}

function architectureProfiles(command, fallback) {
  const index = command.indexOf("--profile");
  if (index >= 0 && typeof command[index + 1] === "string") {
    return dedupeProfileIds(command[index + 1].split(","));
  }
  return dedupeProfileIds(fallback);
}

function architectureLintCommand(profileIds) {
  return ["./.agent-flow/bin/agent-flow", "architecture-lint", "--profile", profileIds.join(",")];
}

function normalizeProfileGateCommand(profileId, gateId, command, pythonExecutable) {
  if (isArchitectureLintGate(gateId, command)) {
    const profileIndex = command.indexOf("--profile");
    if (profileIndex >= 0 && typeof command[profileIndex + 1] === "string") {
      return architectureLintCommand([command[profileIndex + 1]]);
    }
  }
  if (profileId === "python" && ["mypy", "pytest", "ruff"].includes(command[0])) {
    return [pythonExecutable, "-m", command[0], ...command.slice(1)];
  }
  return [...command];
}

function isArchitectureLintGate(gateId, command) {
  return gateId === "architecture-lint" || command.includes("architecture-lint");
}

function gateOrderKey(gate) {
  const lowered = `${gate.gate_id} ${gate.command.join(" ")}`.toLowerCase();
  let kind = 4;
  if (["build", "assemble", "xcodebuild"].some((token) => lowered.includes(token))) kind = 0;
  else if (["typecheck", "tsc", "mypy", "pyright", "type "].some((token) => lowered.includes(token))) kind = 1;
  else if (["lint", "ruff", "detekt", "ktlint", "architecture-lint"].some((token) => lowered.includes(token))) kind = 2;
  else if (lowered.includes("test") || lowered.includes("pytest")) kind = 3;
  return [kind, gateKindTiebreaker(lowered), gate.gate_id];
}

function gateKindTiebreaker(text) {
  if (text.includes("architecture-lint")) return 0;
  if (text.includes("context")) return 1;
  return 2;
}

function normalizeProfile(payload, profileId) {
  validateProfileId(profileId);
  if (!isMapping(payload) || payload.id !== profileId) {
    throw new Error(`profile id mismatch: ${profileId}`);
  }
  if (payload.gates !== undefined && !Array.isArray(payload.gates)) {
    throw new Error(`profile gates must be a list: ${profileId}`);
  }
  const gates = (payload.gates ?? []).map((gate) => {
    if (!isMapping(gate) || typeof gate.id !== "string" || !gate.id) {
      throw new Error(`invalid profile gate: ${profileId}`);
    }
    if (!Array.isArray(gate.command) || gate.command.length === 0 || gate.command.some((part) => typeof part !== "string" || !part)) {
      throw new Error(`invalid profile gate command: ${profileId}:${gate.id}`);
    }
    return {
      id: gate.id,
      command: [...gate.command],
      required: typeof gate.required === "boolean" ? gate.required : true,
    };
  });
  return { ...payload, gates };
}

function requiredOptionValue(argv, index, option, knownOptions) {
  const value = argv[index + 1];
  if (isAttachedOptionValue(value)) return value.value;
  if (
    value === undefined
    || knownOptions.has(value)
    || HELP_FLAGS.has(value)
    || looksLikeOption(value)
  ) {
    throw new Error(`argument ${option}: expected one argument`);
  }
  return argumentText(value);
}

function expandOptionAssignments(argv, knownOptions) {
  if (!Array.isArray(argv)) return argv;
  return argv.flatMap((token) => {
    const match = /^(--[^=]+)=(.*)$/s.exec(String(token));
    return match && knownOptions.has(match[1])
      ? [match[1], { kind: ATTACHED_OPTION_VALUE, value: match[2] }]
      : [token];
  });
}

function nativeGateHelpIsEffective(argv, knownOptions, allowsFiles) {
  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (HELP_FLAGS.has(option)) return true;
    if (!knownOptions.has(option)) continue;
    if (allowsFiles && option === "--files") {
      while (index + 1 < argv.length && !looksLikeOption(argv[index + 1])) index += 1;
      continue;
    }
    const value = argv[index + 1];
    if (
      value === undefined
      || HELP_FLAGS.has(value)
      || knownOptions.has(value)
      || looksLikeOption(value)
    ) {
      return false;
    }
    if (option === "--timeout") {
      try {
        parseIntegerOption(option, argumentText(value));
      } catch {
        return false;
      }
    }
    index += 1;
  }
  return false;
}

function looksLikeOption(value) {
  if (isAttachedOptionValue(value)) return false;
  const text = argumentText(value);
  return text.startsWith("-") && !/^-(?:\d+|\d*\.\d+)$/.test(text);
}

function isAttachedOptionValue(value) {
  return Boolean(value && typeof value === "object" && value.kind === ATTACHED_OPTION_VALUE);
}

function argumentText(value) {
  return isAttachedOptionValue(value) ? value.value : String(value);
}

function parseIntegerOption(option, value) {
  if (!/^[+-]?\d+$/.test(value)) throw new Error(`argument ${option}: invalid int value: ${JSON.stringify(value)}`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) throw new Error(`argument ${option}: invalid int value: ${JSON.stringify(value)}`);
  return parsed;
}

function validateProfileId(profileId) {
  if (typeof profileId !== "string" || !PROFILE_NAME_RE.test(profileId)) {
    throw new Error(`invalid profile name: ${JSON.stringify(profileId)}`);
  }
  return profileId;
}

function dedupeProfileIds(profileIds) {
  if (!Array.isArray(profileIds)) throw new Error("profiles must be a list");
  return [...new Set(
    profileIds
      .map((profileId) => String(profileId).trim())
      .filter(Boolean)
      .map(validateProfileId),
  )];
}

function requirePlainChildFile(root, candidate, label) {
  const resolvedCandidate = path.resolve(candidate);
  const relative = path.relative(root, path.resolve(candidate));
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`${label} escapes profile runtime`);
  }
  let cursor = root;
  for (const part of relative.split(path.sep)) {
    cursor = path.join(cursor, part);
    const stat = fs.lstatSync(cursor);
    if (stat.isSymbolicLink()) throw new Error(`${label} may not use symlinks: ${cursor}`);
    if (cursor === resolvedCandidate ? !stat.isFile() : !stat.isDirectory()) {
      throw new Error(`${label} has an invalid path component: ${cursor}`);
    }
  }
  const realRelative = path.relative(fs.realpathSync(root), fs.realpathSync(candidate));
  if (realRelative.startsWith("..") || path.isAbsolute(realRelative)) {
    throw new Error(`${label} escapes profile runtime`);
  }
}

function recordCommandPart(part, cwd) {
  if (!path.isAbsolute(part)) return part;
  return path.relative(cwd, path.resolve(part)) || ".";
}

function collectArtifacts(runDir) {
  const candidates = [];
  const artifactsDir = path.join(runDir, "artifacts");
  if (isPlainDirectory(artifactsDir)) {
    for (const name of fs.readdirSync(artifactsDir).sort(compareCodePoints)) {
      if (name.endsWith(".md")) candidates.push(path.join(artifactsDir, name));
    }
  }
  if (isPlainDirectory(runDir)) {
    for (const name of fs.readdirSync(runDir).sort(compareCodePoints)) {
      if (name.endsWith(".md") && !["RUN_REPORT.md", "recovery.md"].includes(name)) {
        candidates.push(path.join(runDir, name));
      }
    }
  }
  const byStage = new Map();
  for (const candidate of candidates) {
    const artifact = readPhaseArtifact(candidate);
    const previous = byStage.get(artifact.stage_id);
    const priority = path.dirname(candidate) === artifactsDir ? 1 : 0;
    if (!previous || priority > previous.priority) byStage.set(artifact.stage_id, { ...artifact, priority });
  }
  return [...byStage.values()].sort((left, right) => compareCodePoints(left.path, right.path));
}

function readPhaseArtifact(pathName) {
  const text = fs.existsSync(pathName) ? fs.readFileSync(pathName, "utf8") : "";
  const stageMatch = text.split(/\r?\n/).map((line) => line.trim()).find((line) => /^#\s+Stage Result:/i.test(line));
  const stageId = stageMatch ? stageMatch.replace(/^#\s+Stage Result:\s*/i, "").trim() : path.basename(pathName, ".md");
  const field = (name) => {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const matcher = new RegExp(`^\\s*(?:[-*]\\s*)?${escaped}\\s*:\\s*(.+?)\\s*$`, "i");
    for (const line of text.split(/\r?\n/)) {
      const match = line.match(matcher);
      if (match) return match[1].trim();
    }
    return "";
  };
  return {
    stage_id: stageId,
    path: pathName,
    status: field("status") || "unknown",
    verdict: field("verdict") || "",
    evidence_type: field("evidence type") || field("evidence_type") || "observed",
    confidence: field("confidence") || "unknown",
    stale: field("stale").toLowerCase() === "true",
  };
}

function isBlockedArtifact(artifact) {
  return [artifact.status, artifact.verdict]
    .map((value) => value.toLowerCase())
    .some((value) => ["blocked", "request-changes", "failed", "error"].includes(value));
}

function readJsonIfValid(pathName) {
  if (!fs.existsSync(pathName)) return null;
  try {
    return JSON.parse(fs.readFileSync(pathName, "utf8"));
  } catch {
    return null;
  }
}

function manifestValue(manifest, keys, fallback) {
  if (!isMapping(manifest)) return fallback;
  for (const key of keys) {
    if (manifest[key]) return String(manifest[key]);
  }
  return fallback;
}

function sortJsonKeys(value) {
  if (Array.isArray(value)) return value.map(sortJsonKeys);
  if (!isMapping(value)) return value;
  return Object.fromEntries(Object.keys(value).sort(compareCodePoints).map((key) => [key, sortJsonKeys(value[key])]));
}

function outputText(value) {
  if (value === null || value === undefined) return "";
  return Buffer.isBuffer(value) ? value.toString("utf8") : String(value);
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

function osErrorMessage(error, executable) {
  if (error?.code === "ENOENT") return `[Errno 2] No such file or directory: '${executable}'`;
  if (error?.code === "EACCES") return `[Errno 13] Permission denied: '${executable}'`;
  return errorMessage(error);
}

function compareValues(left, right) {
  if (typeof left === "number" && typeof right === "number") return left - right;
  return compareCodePoints(String(left), String(right));
}

function compareCodePoints(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function isPlainDirectory(pathName) {
  try {
    const stat = fs.lstatSync(pathName);
    return !stat.isSymbolicLink() && stat.isDirectory();
  } catch {
    return false;
  }
}

function isMapping(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
