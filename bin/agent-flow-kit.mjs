#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const command = process.argv[2];

if (command !== "install") {
  console.error("usage: agent-flow-kit install");
  process.exit(1);
}

const root = process.cwd();
const agentFlowDir = path.join(root, ".agent-flow");
const profile = detectProfile(root);

for (const name of ["runs", "state", "handoffs", "team"]) {
  fs.mkdirSync(path.join(agentFlowDir, name), { recursive: true });
}

const payload = {
  install_scope: "project",
  profile,
  root,
  installed_at: new Date().toISOString(),
};

fs.writeFileSync(path.join(agentFlowDir, "kit.json"), `${JSON.stringify(payload, null, 2)}\n`, "utf8");
console.log(`agent-flow installed profile=${profile}`);

function detectProfile(rootDir) {
  const packagePath = path.join(rootDir, "package.json");
  if (fs.existsSync(packagePath)) {
    const packageText = fs.readFileSync(packagePath, "utf8");
    if (packageText.includes("react-native")) {
      return "react-native";
    }
    if (packageText.includes("next")) {
      return "nextjs";
    }
    return "node";
  }
  if (fs.existsSync(path.join(rootDir, "pyproject.toml"))) {
    return "python";
  }
  return "generic";
}
