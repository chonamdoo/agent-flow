import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const VALIDATOR = path.join(ROOT, "scripts", "validate-skills.mjs");

function treeHash(root) {
  const digest = crypto.createHash("sha256");
  for (const file of fs.readdirSync(root).map((name) => path.join(root, name)).sort()) {
    digest.update(path.relative(root, file).split(path.sep).join("/"));
    digest.update("\0");
    digest.update(fs.readFileSync(file));
    digest.update("\0");
  }
  return digest.digest("hex");
}

test("skill validator rejects SKILL.md files over 200 lines", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-skill-limit-"));
  try {
    const skillDir = path.join(root, "skills", "oversized");
    fs.mkdirSync(skillDir, { recursive: true });
    const body = [
      "---",
      "name: oversized",
      "description: Oversized fixture. Use when validating the hard limit.",
      "---",
      "",
      "## Quick start",
      ...Array.from({ length: 195 }, (_, index) => `line ${index + 1}`),
    ].join("\n");
    fs.writeFileSync(path.join(skillDir, "SKILL.md"), `${body}\n`, "utf8");
    const result = spawnSync(process.execPath, [VALIDATOR], {
      cwd: root,
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /max is 200/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("skill validator rejects adapted Matt snapshot drift", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-skill-lock-"));
  try {
    const skillDir = path.join(root, "skills", "adapted");
    fs.mkdirSync(skillDir, { recursive: true });
    const skillPath = path.join(skillDir, "SKILL.md");
    fs.writeFileSync(skillPath, [
      "---",
      "name: adapted",
      "description: Adapted fixture. Use when validating the source lock.",
      "---",
      "",
      "## Quick start",
      "",
      "Use the fixture.",
      "",
    ].join("\n"), "utf8");
    fs.writeFileSync(path.join(root, "skills", "upstream-lock.json"), JSON.stringify({
      whole_tree_required: true,
      exact_copies: {},
      local_adaptations: { adapted: "fixture-adapter" },
      project_tree_hashes: { adapted: treeHash(skillDir) },
    }), "utf8");
    fs.appendFileSync(skillPath, "changed\n", "utf8");

    const result = spawnSync(process.execPath, [VALIDATOR], {
      cwd: root,
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /tracked project snapshot changed: adapted/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("skill validator rejects Android official catalog and lock drift offline", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-android-lock-"));
  try {
    fs.mkdirSync(path.join(root, "skills"), { recursive: true });
    fs.mkdirSync(path.join(root, "profiles"), { recursive: true });
    const licensePath = path.join(root, "skills", "LICENSE.txt");
    fs.writeFileSync(licensePath, "license\n", "utf8");
    fs.writeFileSync(path.join(root, "profiles", "android.yaml"), [
      "id: android",
      "android_skills:",
      "  implementation:",
      "    - skill: profile-only",
      "      when: fixture",
      "  review: []",
      "",
    ].join("\n"), "utf8");
    fs.writeFileSync(path.join(root, "skills", "upstream-lock.json"), JSON.stringify({
      whole_tree_required: true,
      exact_copies: {},
      local_adaptations: {},
      project_tree_hashes: {},
      android_official: {
        source: "https://github.com/android/skills",
        commit: "a".repeat(40),
        runtime_fetch: false,
        catalog: "profiles/android.yaml#android_skills.implementation",
        runtime_tree_verification: "installed-index",
        license_reference: "LICENSE.txt",
        license_sha256: crypto.createHash("sha256").update(fs.readFileSync(licensePath)).digest("hex"),
        snapshots: {
          "lock-only": {
            upstream_path: "fixture/lock-only",
            upstream_tree_hash: "b".repeat(64),
            upstream_skill_sha256: "c".repeat(64),
            project_tree_hash: null,
            snapshot_mode: "install-time-indexed",
          },
        },
      },
    }), "utf8");

    const result = spawnSync(process.execPath, [VALIDATOR], {
      cwd: root,
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Android official skill catalog does not match lock coverage/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
