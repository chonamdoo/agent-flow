#!/usr/bin/env node

import path from "node:path";
import { fileURLToPath } from "node:url";
import { packPlugin } from "../lib/plugin-bundle.mjs";

const usage = "agent-flow-plugin pack --output <new-directory>";
const args = process.argv.slice(2);
if (args.length === 1 && ["--help", "-h"].includes(args[0])) {
  console.log(`Usage: ${usage}\nCreates a self-contained native plugin directory. Does not install or enable it.`);
} else {
  try {
    if (args.length !== 3 || args[0] !== "pack" || args[1] !== "--output" || !args[2] || args[2].startsWith("-")) {
      throw new Error(`Usage: ${usage}`);
    }
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    console.log(`agent-flow plugin packed: ${packPlugin(root, args[2])}`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
