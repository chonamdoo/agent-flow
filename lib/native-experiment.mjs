import fs from "node:fs";
import path from "node:path";

import { recordExecutionStateUsage } from "./execution-state-ledger.mjs";

const VALUE_OPTIONS = new Set([
  "--run-dir",
  "--event-id",
  "--generated-at",
  "--scope",
  "--phase-id",
  "--round",
  "--model-id",
  "--input-tokens",
  "--output-tokens",
  "--additional-tokens",
  "--latency-ms",
  "--estimated-cost-usd",
  "--receipt",
  "--receipt-sha256",
]);
const INTEGER_OPTIONS = new Set([
  "--round",
  "--input-tokens",
  "--output-tokens",
  "--additional-tokens",
  "--latency-ms",
]);
const HELP_FLAGS = new Set(["-h", "--help"]);

export function recordUsageFromArgs(argv, { root = process.cwd() } = {}) {
  try {
    const options = parseRecordUsageArgs(argv);
    const projectRoot = path.resolve(root);
    const runDir = resolveProjectPath(projectRoot, options["--run-dir"]);
    const config = readExecutionLedgerConfig(runDir);
    const result = recordExecutionStateUsage({
      runDir,
      runId: config.run_id,
      mode: config.mode,
      experimentEnabled: true,
      eventId: options["--event-id"],
      generatedAt: options["--generated-at"],
      scope: options["--scope"],
      phaseId: options["--phase-id"],
      round: options["--round"],
      modelId: options["--model-id"],
      inputTokens: options["--input-tokens"],
      outputTokens: options["--output-tokens"],
      additionalTokens: options["--additional-tokens"],
      latencyMs: options["--latency-ms"],
      estimatedCostUsd: options["--estimated-cost-usd"],
      receiptPath: options["--receipt"] === undefined
        ? null
        : resolveProjectPath(projectRoot, options["--receipt"]),
      receiptSha256: options["--receipt-sha256"] ?? null,
    });
    if (result?.ok !== true) {
      return {
        exitCode: 2,
        stdout: "",
        stderr: `${String(result?.error || "execution usage recording failed")}\n`,
      };
    }
    return { exitCode: 0, stdout: `${pythonJson(result)}\n`, stderr: "" };
  } catch (error) {
    return {
      exitCode: 2,
      stdout: "",
      stderr: `${error instanceof Error ? error.message : String(error)}\n`,
    };
  }
}

export function experimentHelpFromArgs(argv) {
  if (!Array.isArray(argv)) return null;
  if (argv[0] === "record-usage" && recordUsageHelpIsEffective(argv)) {
      return (
        "usage: agent-flow experiment record-usage --run-dir RUN_DIR [options]\n\n" +
        "Record verified provider usage, including condition-total additional input tokens.\n"
      );
  }
  if (HELP_FLAGS.has(argv[0])) {
    return "usage: agent-flow experiment {record-usage}\n\npositional arguments:\n  record-usage\n";
  }
  return null;
}

export function parseRecordUsageArgs(argv) {
  if (!Array.isArray(argv) || argv[0] !== "record-usage") {
    throw new Error("usage: agent-flow experiment record-usage --run-dir RUN_DIR [options]");
  }
  const options = {};
  for (let index = 1; index < argv.length; index += 1) {
    const parsed = recordUsageOptionToken(argv[index]);
    const option = parsed.option;
    if (!VALUE_OPTIONS.has(option)) throw new Error(`unrecognized arguments: ${option}`);
    const rawValue = parsed.attached ? parsed.value : argv[index + 1];
    if (!parsed.attached && optionValueIsMissing(rawValue)) {
      throw new Error(`argument ${option}: expected one argument`);
    }
    const parsedValue = INTEGER_OPTIONS.has(option)
      ? parseIntegerArgument(option, rawValue)
      : rawValue;
    if (option === "--scope" && !["phase", "run-total"].includes(parsedValue)) {
      throw new Error(`argument --scope: invalid choice: ${JSON.stringify(parsedValue)}`);
    }
    options[option] = parsedValue;
    if (!parsed.attached) index += 1;
  }
  if (typeof options["--run-dir"] !== "string") {
    throw new Error("the following arguments are required: --run-dir");
  }
  return options;
}

function recordUsageHelpIsEffective(argv) {
  for (let index = 1; index < argv.length; index += 1) {
    const parsed = recordUsageOptionToken(argv[index]);
    const option = parsed.option;
    if (HELP_FLAGS.has(option)) return true;
    if (!VALUE_OPTIONS.has(option)) continue;
    const value = parsed.attached ? parsed.value : argv[index + 1];
    if (!parsed.attached && optionValueIsMissing(value)) return false;
    if (INTEGER_OPTIONS.has(option)) {
      try {
        parseIntegerArgument(option, value);
      } catch {
        return false;
      }
    }
    if (option === "--scope" && !["phase", "run-total"].includes(value)) return false;
    if (!parsed.attached) index += 1;
  }
  return false;
}

function recordUsageOptionToken(value) {
  const text = String(value);
  const match = /^(--[^=]+)=(.*)$/s.exec(text);
  if (match && VALUE_OPTIONS.has(match[1])) {
    return { option: match[1], value: match[2], attached: true };
  }
  return { option: text, value: undefined, attached: false };
}

function optionValueIsMissing(value) {
  if (value === undefined || HELP_FLAGS.has(value) || VALUE_OPTIONS.has(value)) return true;
  return value.startsWith("-") && !/^-(?:\d+|\d*\.\d+)$/.test(value);
}

export function readExecutionLedgerConfig(runDir) {
  const configPath = path.join(path.resolve(runDir), "artifacts", "execution-ledger", "config.json");
  const stat = fs.lstatSync(configPath);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`unsafe execution ledger config: ${configPath}`);
  }
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch (error) {
    throw new Error(`invalid execution ledger config: ${configPath}: ${error.message}`);
  }
  if (!isMapping(payload) || payload.schema_version !== 1) {
    throw new Error(`invalid execution ledger config: ${configPath}`);
  }
  if (typeof payload.run_id !== "string" || !payload.run_id || typeof payload.mode !== "string" || !payload.mode) {
    throw new Error(`invalid execution ledger config: ${configPath}`);
  }
  return { run_id: payload.run_id, mode: payload.mode };
}

export function canonicalJson(value) {
  return JSON.stringify(canonicalValue(value));
}

export function pythonJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(pythonJson).join(", ")}]`;
  }
  if (isMapping(value)) {
    return `{${Object.keys(value)
      .sort(compareCodePoints)
      .map((key) => `${JSON.stringify(key)}: ${pythonJson(value[key])}`)
      .join(", ")}}`;
  }
  return JSON.stringify(value);
}

function resolveProjectPath(root, value) {
  return path.isAbsolute(value) ? path.resolve(value) : path.resolve(root, value);
}

function parseIntegerArgument(option, value) {
  if (!/^[+-]?\d+$/.test(value)) {
    throw new Error(`argument ${option}: invalid int value: ${JSON.stringify(value)}`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error(`argument ${option}: invalid int value: ${JSON.stringify(value)}`);
  }
  return parsed;
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (isMapping(value)) {
    return Object.fromEntries(
      Object.keys(value).sort(compareCodePoints).map((key) => [key, canonicalValue(value[key])]),
    );
  }
  return value;
}

function compareCodePoints(left, right) {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0));
  const rightPoints = Array.from(right, (value) => value.codePointAt(0));
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] < rightPoints[index] ? -1 : 1;
    }
  }
  return leftPoints.length < rightPoints.length ? -1 : leftPoints.length > rightPoints.length ? 1 : 0;
}

function isMapping(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
