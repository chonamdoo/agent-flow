import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  discoverAutomaticExternalSkillNames,
  mergeInstallSelectionWithPrevious,
  resolveInstallSelection,
  resolveProfileSkillSources,
  resolveRuntimeSkillPlan,
} from "../lib/skill-selection.mjs";
import { detectActiveHost } from "../lib/host-detection.mjs";
import { evaluateDeclaredArtifacts } from "../lib/phase-contract.mjs";

const KIT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-skill-source-"));
}

function writeSkill(root, name, metadata = "", body = "body") {
  const skillRoot = path.join(root, name);
  fs.mkdirSync(skillRoot, { recursive: true });
  fs.writeFileSync(
    path.join(skillRoot, "SKILL.md"),
    `---\nname: ${name}\ndescription: Use when testing ${name}.\n${metadata}---\n${body}\n`,
    "utf8",
  );
  return skillRoot;
}

function hostSkills(home, host) {
  if (host === "omp") return path.join(home, ".omp", "agent", "skills");
  return path.join(home, `.${host}`, "skills");
}

test("generic installs stay filtered and automatic external skills are opt in", () => {
  const root = tempRoot();
  const home = path.join(root, "home");
  const project = path.join(root, "project");
  fs.mkdirSync(project, { recursive: true });
  writeSkill(hostSkills(home, "codex"), "auto-demo");

  const selection = resolveInstallSelection({
    args: [],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: project,
  });

  assert.equal(selection.filtered, true);
  assert.deepEqual(selection.profiles, ["generic"]);
  assert.deepEqual(
    [...discoverAutomaticExternalSkillNames({ home, activeHost: "codex" })],
    [],
  );
  assert.deepEqual(
    [...discoverAutomaticExternalSkillNames({
      home,
      activeHost: "codex",
      env: { AGENT_FLOW_AUTO_EXTERNAL_SKILLS: "1" },
    })],
    ["auto-demo"],
  );
});

test("platform install selection covers node android react native python generic and explicit skills", () => {
  const cases = [
    ["node", ["typescript-development-guide"], ["android-code-review"]],
    ["android", ["android-code-review", "android-clean-architecture"], ["react-native-development-guide"]],
    ["react-native", ["react-native-development-guide", "typescript-development-guide"], ["android-code-review"]],
    ["python", ["python-development-guide", "python-api-clean-architecture"], ["android-code-review"]],
    ["generic", ["ddd-architecture", "clean-architecture-core"], ["android-code-review", "react-native-development-guide"]],
  ];
  for (const [profile, included, excluded] of cases) {
    const project = tempRoot();
    const selection = resolveInstallSelection({
      args: [],
      detectedProfile: profile,
      kitRoot: KIT_ROOT,
      projectRoot: project,
    });
    assert.deepEqual(selection.profiles, [profile]);
    for (const name of included) assert.equal(selection.skillNames.has(name), true, `${profile}:${name}`);
    for (const name of excluded) assert.equal(selection.skillNames.has(name), false, `${profile}:${name}`);
  }

  const nativeProject = tempRoot();
  fs.mkdirSync(path.join(nativeProject, "android"));
  fs.mkdirSync(path.join(nativeProject, "ios"));
  const composite = resolveInstallSelection({
    args: [],
    detectedProfile: "react-native",
    kitRoot: KIT_ROOT,
    projectRoot: nativeProject,
  });
  assert.deepEqual(composite.profiles, ["react-native"]);
  assert.deepEqual(composite.skillProfiles, ["android", "ios", "react-native"]);
  assert.equal(composite.skillNames.has("android-code-review"), true);
  assert.equal(composite.skillNames.has("ios-clean-architecture"), true);

  const explicit = resolveInstallSelection({
    args: ["--skills", "react-development-guide"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: tempRoot(),
  });
  assert.deepEqual(explicit.profiles, []);
  assert.equal(explicit.skillNames.has("react-development-guide"), true);
});

test("active host wins deterministically for Claude Codex and OMP", () => {
  for (const host of ["claude", "codex", "omp"]) {
    const root = tempRoot();
    const home = path.join(root, "home");
    const project = path.join(root, "project");
    const snapshots = path.join(project, ".agent-flow", "skills");
    writeSkill(hostSkills(home, host), "external-demo", "", `${host} bytes`);
    writeSkill(path.join(home, ".agents", "skills"), "external-demo", "", "shared bytes");
    fs.mkdirSync(snapshots, { recursive: true });

    const plan = resolveProfileSkillSources({
      skillNames: new Set(["external-demo"]),
      kitRoot: KIT_ROOT,
      projectRoot: project,
      projectSkillsRoot: snapshots,
      home,
      activeHost: host,
      automaticSkillNames: ["external-demo"],
    });

    assert.equal(plan.entries[0].source_kind, "host-bootstrap");
    assert.equal(plan.entries[0].source_host, host);
    assert.equal(plan.entries[0].automatic_on_demand, true);
  }
});

test("explicit active host overrides ambient host markers", () => {
  assert.equal(
    detectActiveHost({
      AGENT_FLOW_ACTIVE_HOST: "omp",
      CLAUDECODE: "1",
      CODEX_THREAD_ID: "thread-1",
    }),
    "omp",
  );
});

test("explicit invalid active source must fail before fallback", () => {
  const root = tempRoot();
  const home = path.join(root, "home");
  const project = path.join(root, "project");
  const active = writeSkill(hostSkills(home, "codex"), "bad-name", "", "invalid");
  fs.writeFileSync(
    path.join(active, "SKILL.md"),
    "---\nname: different-name\ndescription: Invalid active source.\n---\ninvalid\n",
    "utf8",
  );
  writeSkill(path.join(home, ".agents", "skills"), "bad-name", "", "fallback must not run");

  assert.throws(
    () => resolveProfileSkillSources({
      skillNames: new Set(["bad-name"]),
      explicitSkillNames: ["bad-name"],
      kitRoot: KIT_ROOT,
      projectRoot: project,
      projectSkillsRoot: path.join(project, ".agent-flow", "skills"),
      home,
      activeHost: "codex",
    }),
    (error) => /explicit skill bad-name rejected source/.test(error.message)
      && error.message.includes(active)
      && error.message.includes("logical name mismatch"),
  );
});

test("explicit invalid shared source must fail before bundled fallback without an active host", () => {
  const root = tempRoot();
  const home = path.join(root, "home");
  const project = path.join(root, "project");
  const shared = writeSkill(path.join(home, ".agents", "skills"), "agent-flow", "", "invalid shared");
  fs.writeFileSync(
    path.join(shared, "SKILL.md"),
    "---\nname: different-name\ndescription: Invalid shared source.\n---\ninvalid\n",
    "utf8",
  );

  assert.throws(
    () => resolveProfileSkillSources({
      skillNames: new Set(["agent-flow"]),
      explicitSkillNames: ["agent-flow"],
      kitRoot: KIT_ROOT,
      projectRoot: project,
      projectSkillsRoot: path.join(project, ".agent-flow", "skills"),
      home,
      activeHost: null,
    }),
    (error) => /explicit skill agent-flow rejected source/.test(error.message)
      && error.message.includes(shared)
      && error.message.includes("logical name mismatch"),
  );
});

test("automatic invalid active source falls back with authenticated shared bytes", () => {
  const root = tempRoot();
  const home = path.join(root, "home");
  const project = path.join(root, "project");
  const active = writeSkill(hostSkills(home, "claude"), "auto-demo");
  fs.writeFileSync(
    path.join(active, "SKILL.md"),
    "---\nname: wrong\ndescription: Invalid automatic source.\n---\ninvalid\n",
    "utf8",
  );
  const shared = writeSkill(path.join(home, ".agents", "skills"), "auto-demo", "", "shared bytes");

  const plan = resolveProfileSkillSources({
    skillNames: new Set(["auto-demo"]),
    automaticSkillNames: ["auto-demo"],
    kitRoot: KIT_ROOT,
    projectRoot: project,
    projectSkillsRoot: path.join(project, ".agent-flow", "skills"),
    home,
    activeHost: "claude",
  });

  assert.equal(plan.entries[0].source_kind, "shared");
  assert.equal(plan.entries[0].source_path, shared);
});

test("testing localization remains explicit only after merge discovery dependency and runtime closure", () => {
  const root = tempRoot();
  const project = path.join(root, "project");
  fs.mkdirSync(project, { recursive: true });
  writeSkill(path.join(project, "skills"), "testing-localization");
  writeSkill(
    path.join(project, "skills"),
    "consumer",
    "dependencies: [testing-localization]\n",
  );

  const consumer = resolveInstallSelection({
    args: ["--skills", "consumer"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: project,
  });
  assert.equal(consumer.skillNames.has("testing-localization"), false);

  const direct = resolveInstallSelection({
    args: ["--skills", "testing-localization"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: project,
  });
  assert.equal(direct.skillNames.has("testing-localization"), true);

  const merged = mergeInstallSelectionWithPrevious(
    resolveInstallSelection({ args: [], detectedProfile: "generic", kitRoot: KIT_ROOT, projectRoot: project }),
    { selection: { mode: "filtered", profiles: [], explicit_skills: ["testing-localization"] } },
    KIT_ROOT,
    project,
  );
  assert.equal(merged.explicitSkills.includes("testing-localization"), false);

  const home = path.join(root, "home");
  writeSkill(hostSkills(home, "codex"), "testing-localization");
  assert.deepEqual(
    [...discoverAutomaticExternalSkillNames({ home, activeHost: "codex" })],
    [],
  );

  const index = {
    selection: {
      profiles: [],
      explicit_skills: [],
      profile_routing: { profiles: {}, escalations: {} },
    },
    skills: [
      { name: "code-generation-discipline", path: "code", tree_hash: "a".repeat(64) },
      { name: "consumer", path: "consumer", tree_hash: "b".repeat(64), activation: "always", requires: ["testing-localization"] },
      { name: "testing-localization", path: "testing", tree_hash: "c".repeat(64), activation: "always" },
    ],
  };
  assert.deepEqual(
    resolveRuntimeSkillPlan(index, { phaseId: "implement" }).skills.map((skill) => skill.name),
    ["code-generation-discipline", "consumer"],
  );
  index.selection.explicit_skills = ["testing-localization"];
  assert.deepEqual(
    resolveRuntimeSkillPlan(index, { phaseId: "implement" }).skills.map((skill) => skill.name),
    ["code-generation-discipline", "consumer", "testing-localization"],
  );
});

test("runtime activation honors phase and both task and path selectors", () => {
  const index = {
    selection: { profiles: [], explicit_skills: [], profile_routing: { profiles: {}, escalations: {} } },
    skills: [
      { name: "code-generation-discipline", path: "code", tree_hash: "a".repeat(64) },
      { name: "always-code", path: "always", tree_hash: "b".repeat(64), activation: "always" },
      { name: "conditional", path: "conditional", tree_hash: "c".repeat(64), activation: "conditional", workflowPhases: ["implement"], taskTerms: ["payment"], pathGlobs: ["src/pay/**"] },
      { name: "on-demand", path: "demand", tree_hash: "d".repeat(64), activation: "on-demand" },
    ],
  };
  assert.deepEqual(
    resolveRuntimeSkillPlan(index, { phaseId: "implement", taskScope: "payment retry" }).skills.map((skill) => skill.name),
    ["always-code", "code-generation-discipline", "conditional"],
  );
  assert.deepEqual(
    resolveRuntimeSkillPlan(index, { phaseId: "implement", changedFiles: ["src/pay/api.ts"] }).skills.map((skill) => skill.name),
    ["always-code", "code-generation-discipline", "conditional"],
  );
  assert.deepEqual(resolveRuntimeSkillPlan(index, { phaseId: "design" }).skills, []);
});

test("secondary declared artifacts must exist and be fresh", () => {
  const phase = {
    artifacts: ["artifacts/result.md", "artifacts/evidence.json", "artifacts/log.txt"],
  };
  const issues = evaluateDeclaredArtifacts(
    phase,
    [
      {
        path: "artifacts/evidence.json",
        exists: true,
        is_file: true,
        mtime_ms: Date.parse("2026-07-14T23:59:00Z"),
      },
      {
        path: "artifacts/log.txt",
        exists: false,
        is_file: false,
      },
    ],
    "2026-07-15T00:00:00Z",
  );

  assert.deepEqual(issues, [
    "stale declared artifact artifacts/evidence.json",
    "missing declared artifact artifacts/log.txt",
  ]);
});
