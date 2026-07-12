#!/usr/bin/env node

import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const kitCli = path.join(path.dirname(fileURLToPath(import.meta.url)), "agent-flow-kit.mjs");
const args = process.argv.slice(2);

if (args.length === 0 || args[0] === "--help" || args[0] === "-h") {
  console.log("Usage: agent-flow-install install [--force-managed] [--profile <id>] [--skill <name>]");
  process.exit(0);
}

const result = spawnSync(process.execPath, [kitCli, ...args], {
  cwd: process.cwd(),
  env: process.env,
  stdio: "inherit",
});

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
