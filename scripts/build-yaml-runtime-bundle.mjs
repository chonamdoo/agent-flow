#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const entry = path.join(root, "lib", "yaml-runtime.mjs");
const outfile = path.join(root, "lib", "yaml-runtime-bundled.cjs");
const packageJson = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
if (packageJson.dependencies?.yaml !== "2.9.0" || packageJson.devDependencies?.esbuild !== "0.25.6") {
  throw new Error("yaml runtime bundle inputs must remain exact-pinned");
}

const result = await build({
  absWorkingDir: root,
  bundle: true,
  entryPoints: [entry],
  format: "cjs",
  legalComments: "inline",
  outfile,
  platform: "node",
  target: "node18",
  write: false,
});
const generated = result.outputFiles.find((file) => path.resolve(file.path) === outfile)?.contents;
if (!generated) throw new Error("esbuild did not produce the pinned YAML runtime bundle");
if (process.argv.includes("--write")) {
  fs.writeFileSync(outfile, generated);
} else if (!fs.existsSync(outfile) || !fs.readFileSync(outfile).equals(generated)) {
  throw new Error("yaml runtime bundle is stale; run npm run runtime:bundle");
}

if (!fs.existsSync(path.join(root, "lib", "yaml-runtime-LICENSE"))) {
  throw new Error("yaml runtime license is missing");
}
