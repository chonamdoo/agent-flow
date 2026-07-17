#!/usr/bin/env node

import path from "node:path";
import { fileURLToPath } from "node:url";
import { runtimeParityFailures } from "../lib/runtime-parity.mjs";


const sourceRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const installRoot = path.resolve(process.argv[2] || process.cwd());
const failures = runtimeParityFailures(sourceRoot, installRoot);

if (failures.length > 0) {
  console.error(`agent-flow-installed-runtime-parity: FAIL (${failures.length})`);
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log("agent-flow-installed-runtime-parity: OK");
