import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  discoverAutomaticExternalSkillNames,
  hashSkillTree,
  mergeResolvedSkillClosure,
  mergeInstallSelectionWithPrevious,
  parseSkillFrontmatter,
  resolveProfileSkillSources,
  resolveInstallSelection,
  resolveRuntimeSkillPlan,
} from "../lib/skill-selection.mjs";

const PROFILE_ROUTING = JSON.parse(
  fs.readFileSync(new URL("../skills/profile-routing.json", import.meta.url), "utf8"),
);
const KIT_ROOT = fileURLToPath(new URL("..", import.meta.url));
const ADAPTED_ANDROID_SKILLS = [
  "adaptive",
  "android-cli",
  "camera1-to-camerax",
  "compose-modifier-and-layout-style",
  "display-glasses-with-jetpack-compose-glimmer",
  "edge-to-edge",
  "jetpack-compose-m3",
  "kotlin-coroutines-structured-concurrency",
  "styles",
  "verified-email",
];
const OFFICIAL_ANDROID_SKILLS = [
  "adaptive",
  "agp-9-upgrade",
  "android-cli",
  "android-intent-security",
  "appfunctions",
  "camera1-to-camerax",
  "display-glasses-with-jetpack-compose-glimmer",
  "edge-to-edge",
  "engage-sdk-integration",
  "jetpack-compose-m3",
  "migrate-xml-views-to-jetpack-compose",
  "navigation-3",
  "perfetto-sql",
  "perfetto-trace-analysis",
  "play-billing-library-version-upgrade",
  "play-policy-insights",
  "r8-analyzer",
  "styles",
  "testing-setup",
  "verified-email",
];
const BUNDLED_ANDROID_TEST_SKILLS = [
  ...ADAPTED_ANDROID_SKILLS,
  "agp-9-upgrade",
  "android-intent-security",
  "play-policy-insights",
].sort();

function writeSkill(root, name, body) {
  const dir = path.join(root, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "SKILL.md"), `---\nname: ${name}\n---\n${body}\n`, "utf8");
}

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-skill-source-"));
  const kitRoot = path.join(root, "kit");
  const projectRoot = path.join(root, "project");
  const projectSkillsRoot = path.join(projectRoot, ".agent-flow", "skills");
  const home = path.join(root, "home");
  fs.mkdirSync(path.join(kitRoot, "skills"), { recursive: true });
  fs.mkdirSync(projectSkillsRoot, { recursive: true });
  fs.writeFileSync(path.join(kitRoot, "skills", "source-policy.yaml"), [
    "version: 1",
    "authority_order:",
    "  - project-local",
    "  - project",
    "  - project-snapshot",
    "  - active-host",
    "  - shared",
    "  - bundled",
    "  - deterministic-host-bootstrap",
    "rules:",
    "  missing_required_skill: fail",
    "  source_symlink: fail",
    "  host_hash_conflict: fail",
    "  snapshot_replace_from_other_host: deny",
    "  runtime_global_lookup: deny",
    "  automatic_skill_mutation: deny",
    "  hash_scope: whole-tree",
    "",
  ].join("\n"), "utf8");
  fs.writeFileSync(path.join(kitRoot, "skills", "profile-routing.json"), JSON.stringify({
    version: 1,
    profiles: {},
    escalations: {},
  }), "utf8");
  return { root, kitRoot, projectRoot, projectSkillsRoot, home };
}

test("LF and CRLF skill frontmatter preserve routing host and dependency metadata", () => {
  const lines = [
    "---",
    "name: crlf-policy",
    "activation: conditional",
    "workflowPhases: [implement, review]",
    "taskTerms: [intent security]",
    "pathGlobs: [app/src/**/AndroidManifest.xml]",
    "hosts: [claude, codex, omp]",
    "dependencies: [dependency-skill]",
    "---",
    "Use when testing line endings.",
    "",
  ];
  const lf = lines.join("\n");
  const crlf = lines.join("\r\n");
  const expected = {
    name: "crlf-policy",
    activation: "conditional",
    workflowPhases: ["implement", "review"],
    taskTerms: ["intent security"],
    pathGlobs: ["app/src/**/AndroidManifest.xml"],
    hosts: ["claude", "codex", "omp"],
    dependencies: ["dependency-skill"],
  };

  assert.deepEqual(parseSkillFrontmatter(lf), expected);
  assert.deepEqual(parseSkillFrontmatter(crlf), expected);

  const f = fixture();
  const skill = path.join(f.projectRoot, "skills", "crlf-policy");
  fs.mkdirSync(skill, { recursive: true });
  fs.writeFileSync(path.join(skill, "SKILL.md"), crlf, "utf8");
  const selection = resolveInstallSelection({
    args: ["--skill", "crlf-policy"],
    detectedProfile: "generic",
    kitRoot: f.kitRoot,
    projectRoot: f.projectRoot,
  });
  assert(selection.skillNames.has("crlf-policy"));
  assert(selection.skillNames.has("dependency-skill"));
});

test("active host skill precedes bundled fallback", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled");
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", "codex variant");
  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "codex" });
  assert.equal(plan.entries[0].source_kind, "host-bootstrap");
  assert.equal(plan.entries[0].source_host, "codex");
});

test("automatic external catalog adopts only validated active and shared skills as on demand", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".codex", "skills"), "automatic-parent", "parent");
  fs.writeFileSync(
    path.join(f.home, ".codex", "skills", "automatic-parent", "SKILL.md"),
    "---\nname: automatic-parent\ndependencies: [automatic-dependency]\n---\nparent\n",
    "utf8",
  );
  writeSkill(path.join(f.home, ".codex", "skills"), "automatic-dependency", "dependency");
  writeSkill(path.join(f.home, ".agents", "skills"), "shared-automatic", "shared");
  writeSkill(path.join(f.home, ".codex", "skills"), "authority-winner", "active bytes");
  writeSkill(path.join(f.home, ".agents", "skills"), "authority-winner", "shared bytes");
  writeSkill(path.join(f.home, ".claude", "skills"), "inactive-only", "inactive");
  writeSkill(path.join(f.home, ".omp", "agent", "skills"), "omp-only", "omp");
  writeSkill(
    path.join(f.home, ".codex", "skills"),
    "ignored-overlong",
    Array.from({ length: 201 }, (_, index) => `line ${index}`).join("\n"),
  );
  const outside = path.join(f.root, "outside-skill");
  writeSkill(f.root, "outside-skill", "outside");
  fs.symlinkSync(outside, path.join(f.home, ".codex", "skills", "ignored-symlink"));

  const automatic = discoverAutomaticExternalSkillNames({
    home: f.home,
    activeHost: "codex",
    previousIndex: {
      selection: { external_exposure_skills: ["removed-automatic"] },
    },
  });
  assert.deepEqual(
    [...automatic].sort(),
    [
      "authority-winner",
      "automatic-dependency",
      "automatic-parent",
      "removed-automatic",
      "shared-automatic",
    ],
  );
  assert.equal(automatic.has("ignored-overlong"), false);
  assert.equal(automatic.has("ignored-symlink"), false);
  assert.deepEqual(
    [...discoverAutomaticExternalSkillNames({ home: f.home, activeHost: "claude" })].sort(),
    ["authority-winner", "inactive-only", "shared-automatic"],
  );
  assert.deepEqual(
    [...discoverAutomaticExternalSkillNames({ home: f.home, activeHost: "omp" })].sort(),
    ["authority-winner", "omp-only", "shared-automatic"],
  );

  const selected = new Set([...automatic].filter((name) => name !== "removed-automatic"));
  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: selected,
    automaticSkillNames: selected,
    activeHost: "codex",
  });
  assert.deepEqual(
    plan.entries.map((entry) => [entry.name, entry.automatic_on_demand]),
    [
      ["authority-winner", true],
      ["automatic-dependency", true],
      ["automatic-parent", true],
      ["shared-automatic", true],
    ],
  );
  const authorityWinner = plan.entries.find((entry) => entry.name === "authority-winner");
  assert.equal(authorityWinner.source_kind, "host-bootstrap");
  assert.equal(authorityWinner.source_host, "codex");
});

test("project roots keep declared routing for transitive external dependencies", () => {
  const f = fixture();
  const consumerRoot = path.join(f.projectRoot, "skills", "project-consumer");
  writeSkill(path.join(f.projectRoot, "skills"), "project-consumer", "consumer");
  fs.writeFileSync(
    path.join(consumerRoot, "SKILL.md"),
    "---\nname: project-consumer\nactivation: always\ndependencies: [external-dependency]\n---\nconsumer\n",
    "utf8",
  );
  const externalRoot = path.join(f.home, ".codex", "skills", "external-dependency");
  writeSkill(path.join(f.home, ".codex", "skills"), "external-dependency", "dependency");
  fs.writeFileSync(
    path.join(externalRoot, "SKILL.md"),
    "---\nname: external-dependency\nactivation: always\n---\ndependency\n",
    "utf8",
  );
  const automatic = discoverAutomaticExternalSkillNames({ home: f.home, activeHost: "codex" });
  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: new Set(["project-consumer", ...automatic]),
    automaticSkillNames: automatic,
    activeHost: "codex",
  });

  const dependency = plan.entries.find((entry) => entry.name === "external-dependency");
  assert.equal(dependency.source_kind, "host-bootstrap");
  assert.equal(dependency.automatic_on_demand, undefined);
});

test("external source resolution stays scoped to its supplied deterministic catalog", () => {
  for (const host of [
    { name: "claude", parts: [".claude", "skills"] },
    { name: "codex", parts: [".codex", "skills"] },
    { name: "omp", parts: [".omp", "agent", "skills"] },
  ]) {
    const f = fixture();
    const root = path.join(f.home, ...host.parts);
    writeSkill(root, "guide", `${host.name} selected`);
    writeSkill(root, "new-upstream-skill", `${host.name} new`);

    const current = resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      activeHost: host.name,
    });
    assert.deepEqual(current.entries.map((entry) => entry.name), ["guide"], host.name);

    const catalogUpdated = resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide", "new-upstream-skill"]),
      activeHost: host.name,
    });
    assert.deepEqual(
      catalogUpdated.entries.map((entry) => entry.name),
      ["guide", "new-upstream-skill"],
      host.name,
    );
    assert.equal(catalogUpdated.entries[1].source_host, host.name, host.name);
  }
});

test("selected external skill changes and removals fail closed for every host", () => {
  for (const host of [
    { name: "claude", parts: [".claude", "skills"] },
    { name: "codex", parts: [".codex", "skills"] },
    { name: "omp", parts: [".omp", "agent", "skills"] },
  ]) {
    const changed = fixture();
    const changedRoot = path.join(changed.home, ...host.parts);
    writeSkill(changedRoot, "guide", "original");
    const originalHash = hashSkillTree(path.join(changedRoot, "guide"));
    writeSkill(changedRoot, "guide", "changed");
    assert.throws(
      () => resolveProfileSkillSources({
        ...changed,
        skillNames: new Set(["guide"]),
        activeHost: host.name,
        previousIndex: {
          skills: [{
            name: "guide",
            source: "host-bootstrap",
            source_host: host.name,
            tree_hash: originalHash,
          }],
        },
      }),
      /pinned host skill snapshot changed: guide/,
      host.name,
    );

    const removed = fixture();
    const removedRoot = path.join(removed.home, ...host.parts);
    writeSkill(removedRoot, "guide", "original");
    const removedHash = hashSkillTree(path.join(removedRoot, "guide"));
    fs.rmSync(path.join(removedRoot, "guide"), { recursive: true });
    writeSkill(path.join(removed.home, ".agents", "skills"), "guide", "replacement");
    assert.throws(
      () => resolveProfileSkillSources({
        ...removed,
        skillNames: new Set(["guide"]),
        activeHost: host.name,
        previousIndex: {
          skills: [{
            name: "guide",
            source: "host-bootstrap",
            source_host: host.name,
            tree_hash: removedHash,
          }],
        },
      }),
      /pinned host skill snapshot recovery requires the original source: guide/,
      host.name,
    );
  }
});

test("selected external dependency changes and removals fail closed for every host", () => {
  for (const host of [
    { name: "claude", parts: [".claude", "skills"] },
    { name: "codex", parts: [".codex", "skills"] },
    { name: "omp", parts: [".omp", "agent", "skills"] },
  ]) {
    const changed = fixture();
    const changedRoot = path.join(changed.home, ...host.parts);
    writeSkill(changedRoot, "guide-helper", "original helper");
    const helperHash = hashSkillTree(path.join(changedRoot, "guide-helper"));
    fs.writeFileSync(
      path.join(changedRoot, "guide-helper", "SKILL.md"),
      "---\nname: guide-helper\n---\nchanged helper\n",
      "utf8",
    );
    writeSkill(changedRoot, "guide", "host root");
    fs.writeFileSync(
      path.join(changedRoot, "guide", "SKILL.md"),
      "---\nname: guide\ndependencies: [guide-helper]\n---\nhost root\n",
      "utf8",
    );
    const rootHash = hashSkillTree(path.join(changedRoot, "guide"));
    assert.throws(
      () => resolveProfileSkillSources({
        ...changed,
        skillNames: new Set(["guide"]),
        activeHost: host.name,
        previousIndex: {
          skills: [
            {
              name: "guide",
              source: "host-bootstrap",
              source_host: host.name,
              tree_hash: rootHash,
            },
            {
              name: "guide-helper",
              source: "host-bootstrap",
              source_host: host.name,
              tree_hash: helperHash,
            },
          ],
        },
      }),
      /pinned host skill snapshot changed: guide-helper/,
      host.name,
    );

    const removed = fixture();
    const removedRoot = path.join(removed.home, ...host.parts);
    writeSkill(removedRoot, "guide", "host root");
    fs.writeFileSync(
      path.join(removedRoot, "guide", "SKILL.md"),
      "---\nname: guide\nrequires: [guide-helper]\n---\nhost root\n",
      "utf8",
    );
    const removedRootHash = hashSkillTree(path.join(removedRoot, "guide"));
    writeSkill(removedRoot, "guide-helper", "original helper");
    const removedHelperHash = hashSkillTree(path.join(removedRoot, "guide-helper"));
    fs.rmSync(path.join(removedRoot, "guide-helper"), { recursive: true });
    writeSkill(path.join(removed.home, ".agents", "skills"), "guide-helper", "replacement");
    assert.throws(
      () => resolveProfileSkillSources({
        ...removed,
        skillNames: new Set(["guide"]),
        activeHost: host.name,
        previousIndex: {
          skills: [
            {
              name: "guide",
              source: "host-bootstrap",
              source_host: host.name,
              tree_hash: removedRootHash,
            },
            {
              name: "guide-helper",
              source: "host-bootstrap",
              source_host: host.name,
              tree_hash: removedHelperHash,
            },
          ],
        },
      }),
      /pinned host skill snapshot recovery requires the original source: guide-helper/,
      host.name,
    );
  }
});

test("existing external snapshots do not hide source changes or removals", () => {
  for (const source of ["host-bootstrap", "shared"]) {
    const changed = fixture();
    const changedSource = source === "shared"
      ? path.join(changed.home, ".agents", "skills")
      : path.join(changed.home, ".codex", "skills");
    writeSkill(changedSource, "guide", "original");
    writeSkill(changed.projectSkillsRoot, "guide", "original");
    const originalHash = hashSkillTree(path.join(changedSource, "guide"));
    writeSkill(changedSource, "guide", "changed");

    assert.throws(
      () => resolveProfileSkillSources({
        ...changed,
        skillNames: new Set(["guide"]),
        activeHost: "codex",
        previousIndex: {
          skills: [{
            name: "guide",
            source,
            source_host: source === "host-bootstrap" ? "codex" : null,
            tree_hash: originalHash,
          }],
        },
      }),
      /pinned external skill source changed: guide/,
      source,
    );

    const removed = fixture();
    const removedSource = source === "shared"
      ? path.join(removed.home, ".agents", "skills")
      : path.join(removed.home, ".codex", "skills");
    writeSkill(removedSource, "guide", "original");
    writeSkill(removed.projectSkillsRoot, "guide", "original");
    const removedHash = hashSkillTree(path.join(removedSource, "guide"));
    fs.rmSync(path.join(removedSource, "guide"), { recursive: true });

    assert.throws(
      () => resolveProfileSkillSources({
        ...removed,
        skillNames: new Set(["guide"]),
        activeHost: "codex",
        previousIndex: {
          skills: [{
            name: "guide",
            source,
            source_host: source === "host-bootstrap" ? "codex" : null,
            tree_hash: removedHash,
          }],
        },
      }),
      /pinned external skill source is unavailable: guide/,
      source,
    );
  }
});

test("whole-tree lock rejects changed adapted Matt snapshots", () => {
  const f = fixture();
  const skillRoot = path.join(f.kitRoot, "skills", "guide");
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "locked adaptation");
  fs.writeFileSync(path.join(f.kitRoot, "skills", "upstream-lock.json"), JSON.stringify({
    whole_tree_required: true,
    exact_copies: {},
    local_adaptations: { guide: "fixture-adapter" },
    project_tree_hashes: { guide: hashSkillTree(skillRoot) },
  }), "utf8");
  assert.equal(
    resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }).entries.length,
    1,
  );

  fs.appendFileSync(path.join(skillRoot, "SKILL.md"), "changed\n", "utf8");
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }),
    /locked project skill snapshot changed: guide/,
  );
});

test("Android official catalog lock verifies bundled trees offline at runtime", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "official adapter");
  fs.mkdirSync(path.join(f.kitRoot, "profiles"), { recursive: true });
  fs.writeFileSync(path.join(f.kitRoot, "profiles", "android.yaml"), [
    "id: android",
    "android_skills:",
    "  implementation:",
    "    - skill: guide",
    "      when: fixture",
    "  review: []",
    "",
  ].join("\n"), "utf8");
  const license = path.join(f.kitRoot, "skills", "LICENSE.txt");
  fs.writeFileSync(license, "license\n", "utf8");
  const licenseHash = crypto.createHash("sha256").update(fs.readFileSync(license)).digest("hex");
  const skillRoot = path.join(f.kitRoot, "skills", "guide");
  fs.writeFileSync(path.join(f.kitRoot, "skills", "upstream-lock.json"), JSON.stringify({
    whole_tree_required: true,
    exact_copies: {},
    local_adaptations: {},
    project_tree_hashes: {},
    android_official: {
      source: "https://github.com/android/skills",
      commit: "a".repeat(40),
      policy: "offline-catalog-lock-and-indexed-project-snapshot",
      runtime_fetch: false,
      catalog: "profiles/android.yaml#android_skills.implementation",
      runtime_tree_verification: "installed-index",
      license_reference: "LICENSE.txt",
      license_sha256: licenseHash,
      snapshots: {
        guide: {
          upstream_path: "fixture/guide",
          upstream_tree_hash: "b".repeat(64),
          upstream_skill_sha256: "c".repeat(64),
          project_tree_hash: hashSkillTree(skillRoot),
          snapshot_mode: "bundled-adapter",
        },
      },
    },
  }), "utf8");
  fs.appendFileSync(path.join(f.kitRoot, "skills", "source-policy.yaml"), [
    "official_project_snapshots:",
    "  source: https://github.com/android/skills",
    `  commit: ${"a".repeat(40)}`,
    "  catalog: profiles/android.yaml#android_skills.implementation",
    "  install_policy: offline-catalog-lock-and-indexed-project-snapshot",
    "  runtime_fetch: false",
    "  offline_validation: required",
    "  runtime_tree_verification: installed-index",
    "",
  ].join("\n"), "utf8");

  assert.equal(resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }).entries.length, 1);
  const profilePath = path.join(f.kitRoot, "profiles", "android.yaml");
  const profileBefore = fs.readFileSync(profilePath, "utf8");
  fs.writeFileSync(
    profilePath,
    profileBefore.replace(
      "  review: []",
      "    - skill: newly-added-official\n      when: fixture\n  review: []",
    ),
    "utf8",
  );
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }),
    /Android official skill catalog does not match lock coverage/,
  );
  fs.writeFileSync(profilePath, profileBefore, "utf8");
  fs.appendFileSync(path.join(skillRoot, "SKILL.md"), "changed\n", "utf8");
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }),
    /Android bundled skill snapshot changed: guide/,
  );
});

test("explicit active-host config directories replace default skill roots", () => {
  for (const fixtureCase of [
    { host: "codex", envKey: "CODEX_HOME", defaultRoot: [".codex"] },
    { host: "claude", envKey: "CLAUDE_CONFIG_DIR", defaultRoot: [".claude"] },
    { host: "omp", envKey: "PI_CODING_AGENT_DIR", defaultRoot: [".omp", "agent"] },
  ]) {
    const f = fixture();
    const configuredRoot = path.join(f.root, `${fixtureCase.host}-config`);
    writeSkill(path.join(f.home, ...fixtureCase.defaultRoot, "skills"), "guide", "default variant");
    writeSkill(path.join(configuredRoot, "skills"), "guide", "configured variant");

    const plan = resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      activeHost: fixtureCase.host,
      env: { [fixtureCase.envKey]: configuredRoot },
    });

    assert.equal(plan.entries[0].source_kind, "host-bootstrap", fixtureCase.host);
    assert.equal(plan.entries[0].source_host, fixtureCase.host, fixtureCase.host);
    assert.equal(plan.entries[0].source_path, path.join(configuredRoot, "skills", "guide"));
  }
});

test("Claude Codex and OMP use the same active-host shared bundled authority order", () => {
  const hosts = [
    { name: "claude", root: (f) => path.join(f.home, ".claude", "skills") },
    { name: "codex", root: (f) => path.join(f.home, ".codex", "skills") },
    { name: "omp", root: (f) => path.join(f.home, ".omp", "agent", "skills") },
  ];
  for (const host of hosts) {
    const activeFixture = fixture();
    writeSkill(path.join(activeFixture.kitRoot, "skills"), "guide", "bundled");
    writeSkill(path.join(activeFixture.home, ".agents", "skills"), "guide", "shared");
    writeSkill(host.root(activeFixture), "guide", `${host.name} active`);
    const active = resolveProfileSkillSources({
      ...activeFixture,
      skillNames: new Set(["guide"]),
      activeHost: host.name,
    });
    assert.equal(active.entries[0].source_kind, "host-bootstrap", host.name);
    assert.equal(active.entries[0].source_host, host.name, host.name);

    const sharedFixture = fixture();
    writeSkill(path.join(sharedFixture.kitRoot, "skills"), "guide", "bundled");
    writeSkill(path.join(sharedFixture.home, ".agents", "skills"), "guide", "shared");
    const shared = resolveProfileSkillSources({
      ...sharedFixture,
      skillNames: new Set(["guide"]),
      activeHost: host.name,
    });
    assert.equal(shared.entries[0].source_kind, "shared", host.name);
  }
});

test("self install treats kit skills as bundled instead of uncopied project sources", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled");
  const plan = resolveProfileSkillSources({
    skillNames: new Set(["guide"]),
    kitRoot: f.kitRoot,
    projectRoot: f.kitRoot,
    projectSkillsRoot: path.join(f.kitRoot, ".agent-flow", "skills"),
    home: f.home,
  });

  assert.equal(plan.entries[0].source_kind, "bundled");
});

test("untrusted project snapshot cannot override the current source chain", () => {
  const f = fixture();
  writeSkill(f.projectSkillsRoot, "guide", "snapshot");
  writeSkill(path.join(f.home, ".claude", "skills"), "guide", "claude variant");
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "claude" }),
    /untrusted existing skill snapshot differs/,
  );
});

test("matching legacy snapshot migrates from the current bundled source", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled");
  writeSkill(f.projectSkillsRoot, "guide", "bundled");

  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) });

  assert.equal(plan.entries[0].source_kind, "bundled");
  assert.equal(plan.entries[0].source_host, null);
});

test("unchanged managed bundled snapshot upgrades atomically on reinstall", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "new bundled");
  writeSkill(f.projectSkillsRoot, "guide", "old bundled");
  const oldHash = hashSkillTree(path.join(f.projectSkillsRoot, "guide"));
  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: new Set(["guide"]),
    previousIndex: {
      skills: [{ name: "guide", source: "bundled", tree_hash: oldHash }],
    },
  });
  assert.equal(plan.entries[0].source_kind, "bundled");
  assert.equal(plan.entries[0].replace_existing, true);
  assert.notEqual(plan.entries[0].tree_hash, oldHash);
});

test("active host precedes shared source for a fresh external skill", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".agents", "skills"), "guide", "shared");
  writeSkill(path.join(f.home, ".omp", "agent", "skills"), "guide", "omp variant");
  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "omp" });
  assert.equal(plan.entries[0].source_kind, "host-bootstrap");
  assert.equal(plan.entries[0].source_host, "omp");
  assert.match(plan.entries[0].tree_hash, /^[a-f0-9]{64}$/);
});

test("shared source precedes bundled when the active host has no candidate", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".agents", "skills"), "guide", "shared");
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled");
  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "codex" });
  assert.equal(plan.entries[0].source_kind, "shared");
});

test("missing pinned snapshot never switches to another host", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".claude", "skills"), "guide", "claude replacement");
  assert.throws(
    () => resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      activeHost: "claude",
      previousIndex: {
        skills: [{
          name: "guide",
          source: "host-bootstrap",
          source_host: "codex",
          tree_hash: "a".repeat(64),
        }],
      },
    }),
    /skill snapshot replacement from another host is denied: guide/,
  );
});

test("missing pinned snapshot may recover from its original host", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", "codex original");
  writeSkill(path.join(f.home, ".claude", "skills"), "guide", "claude replacement");
  const originalHash = hashSkillTree(path.join(f.home, ".codex", "skills", "guide"));
  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: new Set(["guide"]),
    activeHost: "claude",
    previousIndex: {
      skills: [{
        name: "guide",
        source: "host-bootstrap",
        source_host: "codex",
        tree_hash: originalHash,
      }],
    },
  });

  assert.equal(plan.entries[0].source_kind, "host-bootstrap");
  assert.equal(plan.entries[0].source_host, "codex");
  assert.equal(plan.entries[0].tree_hash, originalHash);
});

test("missing pinned snapshot rejects changed bytes from its original host", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", "changed codex source");

  assert.throws(
    () => resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      activeHost: "codex",
      previousIndex: {
        skills: [{
          name: "guide",
          source: "host-bootstrap",
          source_host: "codex",
          tree_hash: "a".repeat(64),
        }],
      },
    }),
    /pinned host skill snapshot changed: guide/,
  );
});

test("missing pinned snapshot never falls back to shared or bundled sources", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".agents", "skills"), "guide", "shared replacement");
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled replacement");

  assert.throws(
    () => resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      activeHost: "claude",
      previousIndex: {
        skills: [{
          name: "guide",
          source: "host-bootstrap",
          source_host: "codex",
          tree_hash: "a".repeat(64),
        }],
      },
    }),
    /pinned host skill snapshot recovery requires the original source: guide; shared or bundled replacement is denied/,
  );
});

test("overlong active host candidate falls back to a valid bundled adapter", () => {
  const f = fixture();
  writeSkill(
    path.join(f.home, ".codex", "skills"),
    "guide",
    Array.from({ length: 201 }, (_, index) => `host line ${index}`).join("\n"),
  );
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled adapter");
  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "codex" });
  assert.equal(plan.entries[0].source_kind, "bundled");
  assert.equal(plan.entries[0].source_host, null);
});

test("unchanged overlong managed snapshot migrates to a valid bundled adapter", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled adapter");
  writeSkill(
    f.projectSkillsRoot,
    "guide",
    Array.from({ length: 201 }, (_, index) => `host line ${index}`).join("\n"),
  );
  const snapshotRoot = path.join(f.projectSkillsRoot, "guide");
  const snapshotHash = hashSkillTree(snapshotRoot);
  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: new Set(["guide"]),
    previousIndex: {
      skills: [{
        name: "guide",
        source: "host-bootstrap",
        source_host: "codex",
        tree_hash: snapshotHash,
      }],
    },
  });

  assert.equal(plan.entries[0].source_kind, "bundled");
  assert.equal(plan.entries[0].source_host, null);
  assert.equal(plan.entries[0].replace_existing, true);
  assert.notEqual(plan.entries[0].tree_hash, snapshotHash);
});

test("modified overlong managed snapshot is never replaced by a bundled adapter", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled adapter");
  writeSkill(f.projectSkillsRoot, "guide", "previous managed snapshot");
  const previousHash = hashSkillTree(path.join(f.projectSkillsRoot, "guide"));
  writeSkill(
    f.projectSkillsRoot,
    "guide",
    Array.from({ length: 201 }, (_, index) => `user line ${index}`).join("\n"),
  );

  assert.throws(
    () => resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      previousIndex: {
        skills: [{ name: "guide", source: "host-bootstrap", tree_hash: previousHash }],
      },
    }),
    /existing skill snapshot changed: guide/,
  );
});

test("unsafe active-host symlink falls back to a valid bundled adapter", () => {
  const f = fixture();
  writeSkill(path.join(f.home, "source"), "guide", "unsafe host source");
  const hostRoot = path.join(f.home, ".codex", "skills");
  fs.mkdirSync(hostRoot, { recursive: true });
  fs.symlinkSync(path.join(f.home, "source", "guide"), path.join(hostRoot, "guide"));
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled adapter");
  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "codex" });
  assert.equal(plan.entries[0].source_kind, "bundled");
});

test("all invalid external candidates fail with preserved diagnostics", () => {
  const f = fixture();
  const overlong = Array.from({ length: 201 }, (_, index) => `line ${index}`).join("\n");
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", overlong);
  writeSkill(path.join(f.home, ".agents", "skills"), "guide", overlong);
  writeSkill(path.join(f.kitRoot, "skills"), "guide", overlong);
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "codex" }),
    (error) => /no valid skill source for guide/.test(error.message)
      && /host-bootstrap:codex/.test(error.message)
      && /shared/.test(error.message)
      && /bundled/.test(error.message)
      && /max is 200/.test(error.message),
  );
});

test("overlong Android host skills with bundled adapters resolve self-contained snapshots", () => {
  const f = fixture();
  const overlong = Array.from({ length: 201 }, (_, index) => `host line ${index}`).join("\n");
  for (const name of BUNDLED_ANDROID_TEST_SKILLS) {
    fs.cpSync(
      path.join(KIT_ROOT, "skills", name),
      path.join(f.kitRoot, "skills", name),
      { recursive: true },
    );
    writeSkill(path.join(f.home, ".codex", "skills"), name, overlong);
  }
  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: new Set(BUNDLED_ANDROID_TEST_SKILLS),
    activeHost: "codex",
  });
  assert.deepEqual(plan.entries.map((entry) => entry.name), BUNDLED_ANDROID_TEST_SKILLS);
  assert.equal(plan.entries.every((entry) => entry.source_kind === "bundled"), true);

  for (const name of ADAPTED_ANDROID_SKILLS) {
    const root = path.join(f.kitRoot, "skills", name);
    const main = fs.readFileSync(path.join(root, "SKILL.md"), "utf8");
    assert.equal(main.split(/\r?\n/).length <= 200, true, `${name} line limit`);
    const markdownFiles = [
      path.join(root, "SKILL.md"),
      ...fs.readdirSync(path.join(root, "references"), { recursive: true })
        .map((file) => path.join(root, "references", file))
        .filter((file) => fs.statSync(file).isFile() && file.endsWith(".md")),
    ];
    for (const target of markdownFiles) {
      const file = path.relative(root, target);
      if (!fs.statSync(target).isFile() || !target.endsWith(".md")) continue;
      const text = fs.readFileSync(target, "utf8");
      assert.doesNotMatch(text, /\/Users\//, `${name}/${file} absolute path`);
      assert.doesNotMatch(text, /\]\(https?:\/\//, `${name}/${file} external Markdown link`);
      const protocolLiterals = [...text.matchAll(/https?:\/\/[^\s`)\]]+/g)].map((match) => match[0]);
      const allowedProtocolLiterals = name === "verified-email" && file === "references/server-security.md"
        ? new Set([
          "https://verifiablecredentials-pa.googleapis.com",
          "https://verifiablecredentials-pa.googleapis.com/.well-known/vc-public-jwks",
        ])
        : new Set();
      assert.deepEqual(
        new Set(protocolLiterals),
        allowedProtocolLiterals,
        `${name}/${file} unexpected protocol literal`,
      );
      if (file !== "SKILL.md") {
        assert.doesNotMatch(text, /\]\([^)]+\)/, `${name}/${file} reference chain`);
      }
      for (const match of text.matchAll(/\]\(([^)]+)\)/g)) {
        const link = match[1].split("#", 1)[0];
        if (!link) continue;
        const resolved = path.resolve(path.dirname(target), link);
        assert.equal(resolved.startsWith(`${root}${path.sep}`), true, `${name}/${file} escaped link`);
        assert.equal(fs.existsSync(resolved), true, `${name}/${file} missing ${link}`);
      }
    }
  }
});

test("missing required skill is explicit", () => {
  const f = fixture();
  const plan = resolveProfileSkillSources({ ...f, skillNames: new Set(["missing"]), activeHost: "codex" });
  assert.deepEqual(plan.entries, []);
  assert.deepEqual(plan.missing, ["missing"]);
});

test("host bootstrap is independent of active host", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", "bootstrap");
  const codex = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "codex" });
  const claude = resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: "claude" });
  assert.equal(codex.entries[0].source_kind, "host-bootstrap");
  assert.equal(claude.entries[0].source_host, "codex");
  assert.equal(codex.entries[0].tree_hash, claude.entries[0].tree_hash);
});

test("different host hashes fail closed", () => {
  const f = fixture();
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", "codex");
  writeSkill(path.join(f.home, ".claude", "skills"), "guide", "claude");
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]), activeHost: null }),
    /conflicting host skill snapshots/,
  );
});

test("every external skill source rejects SKILL.md files over 200 lines", () => {
  const locations = [
    (f) => path.join(f.projectRoot, ".agent-flow", "local-skills"),
    (f) => path.join(f.projectRoot, "skills"),
    (f) => path.join(f.home, ".agents", "skills"),
    (f) => path.join(f.home, ".codex", "skills"),
  ];
  for (const locate of locations) {
    const f = fixture();
    writeSkill(locate(f), "guide", Array.from({ length: 201 }, (_, index) => `line ${index}`).join("\n"));
    assert.throws(
      () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }),
      /max is 200/,
    );
  }
});

test("symlinked skill roots fail closed", () => {
  const f = fixture();
  writeSkill(path.join(f.home, "source"), "guide", "source");
  const hostRoot = path.join(f.home, ".codex", "skills");
  fs.mkdirSync(hostRoot, { recursive: true });
  fs.symlinkSync(path.join(f.home, "source", "guide"), path.join(hostRoot, "guide"));
  assert.throws(
    () => resolveProfileSkillSources({ ...f, skillNames: new Set(["guide"]) }),
    /symlink/,
  );
});

test("Node skill tree hashes reject special files", (t) => {
  const f = fixture();
  const skill = path.join(f.projectRoot, "skills", "guide");
  writeSkill(path.join(f.projectRoot, "skills"), "guide", "project guide");
  const fifo = path.join(skill, "runtime.pipe");
  const created = spawnSync("mkfifo", [fifo], { encoding: "utf8" });
  if (created.error?.code === "ENOENT") {
    t.skip("mkfifo is unavailable");
    return;
  }
  assert.equal(created.status, 0, created.stderr || created.stdout);

  assert.throws(
    () => hashSkillTree(skill),
    /only regular files and directories/,
  );
});

test("host and shared source ancestor symlinks never become snapshot authority", () => {
  for (const source of ["codex-root", "codex-skills", "shared-root", "shared-skills"]) {
    const f = fixture();
    writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled fallback");
    const outside = path.join(f.home, `outside-${source}`);
    if (source.endsWith("root")) {
      writeSkill(path.join(outside, "skills"), "guide", "unsafe external");
      const link = source.startsWith("codex")
        ? path.join(f.home, ".codex")
        : path.join(f.home, ".agents");
      fs.mkdirSync(path.dirname(link), { recursive: true });
      fs.symlinkSync(outside, link, "dir");
    } else {
      writeSkill(outside, "guide", "unsafe external");
      const parent = source.startsWith("codex")
        ? path.join(f.home, ".codex")
        : path.join(f.home, ".agents");
      fs.mkdirSync(parent, { recursive: true });
      fs.symlinkSync(outside, path.join(parent, "skills"), "dir");
    }

    const plan = resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
      activeHost: "codex",
    });
    assert.equal(plan.entries[0].source_kind, "bundled", source);
    assert.match(plan.entries[0].tree_hash, /^[a-f0-9]{64}$/, source);
  }

  const invalidOnly = fixture();
  const outside = path.join(invalidOnly.home, "outside-codex");
  writeSkill(path.join(outside, "skills"), "guide", "unsafe external");
  fs.symlinkSync(outside, path.join(invalidOnly.home, ".codex"), "dir");
  assert.throws(
    () => resolveProfileSkillSources({
      ...invalidOnly,
      skillNames: new Set(["guide"]),
      activeHost: "codex",
    }),
    /symlink ancestors/,
  );
});

test("unsafe explicit skill names fail before path resolution", () => {
  const f = fixture();
  for (const name of ["../../escape", "skill name", ".hidden", "a..b", "ᾲ", "ὰι"]) {
    assert.throws(
      () => resolveInstallSelection({
        args: ["--skill", name],
        detectedProfile: "generic",
        kitRoot: f.kitRoot,
        projectRoot: f.projectRoot,
      }),
      /unsafe skill name/,
      name,
    );
  }
});

test("macOS-equivalent Unicode skill names fail closed for every external host", () => {
  const f = fixture();
  for (const activeHost of ["claude", "codex", "omp"]) {
    for (const name of ["ᾲ", "ὰι"]) {
      assert.throws(
        () => resolveProfileSkillSources({
          ...f,
          skillNames: new Set([name]),
          activeHost,
        }),
        /unsafe skill name/,
        `${activeHost}:${name}`,
      );
    }
  }
});

test("project-local catalog rejects nonportable frontmatter names instead of dropping them", () => {
  const f = fixture();
  const skillRoot = path.join(f.projectRoot, "skills", "unicode-policy");
  fs.mkdirSync(skillRoot, { recursive: true });
  fs.writeFileSync(path.join(skillRoot, "SKILL.md"), "---\nname: ᾲ\n---\npolicy\n", "utf8");

  assert.throws(
    () => resolveInstallSelection({
      args: ["--skill", "guide"],
      detectedProfile: "generic",
      kitRoot: f.kitRoot,
      projectRoot: f.projectRoot,
    }),
    /unsafe project-local skill name/,
  );
});

test("project-local fallback paths cannot satisfy a different selected logical name", () => {
  const f = fixture();
  const skillRoot = path.join(f.projectRoot, "skills", "guide");
  fs.mkdirSync(skillRoot, { recursive: true });
  fs.writeFileSync(path.join(skillRoot, "SKILL.md"), "---\nname: other-guide\n---\npolicy\n", "utf8");

  assert.throws(
    () => resolveProfileSkillSources({
      ...f,
      skillNames: new Set(["guide"]),
    }),
    /skill source logical name mismatch: expected guide, got other-guide/,
  );
});

test("explicit profile names are validated before logical-name folding", () => {
  const f = fixture();
  for (const name of ["K", ".hidden", "node.v2", "-node"]) {
    assert.throws(
      () => resolveInstallSelection({
        args: ["--profile", name],
        detectedProfile: "generic",
        kitRoot: f.kitRoot,
        projectRoot: f.projectRoot,
      }),
      /unsafe profile name/,
      name,
    );
  }
});

test("react native android changes add the installed android review profile", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["react-native"],
      skill_profiles: ["android", "react-native"],
      required_review: {
        android: ["android-code-review"],
        "react-native": ["react-native-development-guide"],
      },
      conditional_skills: { android: { implementation: [], review: [] } },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: ".agent-flow/skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: ".agent-flow/skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "react-native-development-guide", path: ".agent-flow/skills/react-native-development-guide/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "review", changedFiles: ["android/app/src/main/MainActivity.kt"] });
  assert.deepEqual(plan.touched_profiles, ["android", "react-native"]);
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "android-code-review",
    "code-generation-discipline",
    "react-native-development-guide",
  ]);
});

test("empty installed skill profiles fail closed while an absent field falls back", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["python"],
      skill_profiles: [],
      required_review: { python: ["python-development-guide"] },
      conditional_skills: {},
      profile_routing: {},
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
    ],
  }, { phaseId: "implement" });

  assert.deepEqual(plan.active_profiles, ["python"]);
  assert.deepEqual(plan.touched_profiles, ["python"]);
  assert.deepEqual(plan.missing_profiles, ["python"]);
  assert.deepEqual(plan.missing, ["python-development-guide"]);

  const absent = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["python"],
      required_review: { python: ["python-development-guide"] },
      conditional_skills: {},
      profile_routing: {},
    },
    skills: [],
  }, { phaseId: "implement" });
  assert.deepEqual(absent.missing_profiles, []);
});

test("changed files narrow a multi-profile plan", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android", "python"],
      skill_profiles: ["android", "python"],
      required_review: {
        android: ["android-code-review"],
        python: ["python-development-guide"],
      },
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "python-development-guide", path: "skills/python-development-guide/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "review", changedFiles: ["src/service.py"] });
  assert.deepEqual(plan.touched_profiles, ["python"]);
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "code-generation-discipline",
    "python-development-guide",
  ]);
});

test("react native native change fails closed when android profile was not installed", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["react-native"],
      skill_profiles: ["react-native"],
      required_review: {
        android: ["android-clean-architecture", "android-code-review"],
        "react-native": ["react-native-development-guide"],
      },
      conditional_skills: { android: { implementation: [], review: [] } },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "react-native-development-guide", path: "skills/react-native-development-guide/SKILL.md", tree_hash: "b" },
    ],
  }, { phaseId: "review", changedFiles: ["android/app/src/main/MainActivity.kt"] });
  assert.deepEqual(plan.touched_profiles, ["android", "react-native"]);
  assert.deepEqual(plan.missing_profiles, ["android"]);
  assert.deepEqual(plan.missing, ["android-clean-architecture", "android-code-review"]);
});

test("android compose paths select only declared matching specialist skills", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android"],
      skill_profiles: ["android"],
      required_review: { android: ["android-code-review"] },
      conditional_skills: {
        android: {
          implementation: [],
          review: ["compose-side-effects", "compose-state-authoring"],
        },
      },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "compose-side-effects", path: "skills/compose-side-effects/SKILL.md", tree_hash: "c" },
      { name: "compose-state-authoring", path: "skills/compose-state-authoring/SKILL.md", tree_hash: "d" },
    ],
  }, { phaseId: "review", changedFiles: ["app/src/main/ui/FeatureScreen.kt"] });
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "android-code-review",
    "code-generation-discipline",
    "compose-state-authoring",
  ]);
});

test("wear os paths select the newly adopted compose material skill", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android"],
      skill_profiles: ["android"],
      required_review: { android: ["android-code-review"] },
      conditional_skills: {
        android: { implementation: [], review: ["jetpack-compose-m3"] },
      },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "jetpack-compose-m3", path: "skills/jetpack-compose-m3/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "review", changedFiles: ["wear/src/main/WatchFace.kt"] });
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "android-code-review",
    "code-generation-discipline",
    "jetpack-compose-m3",
  ]);
});

test("task scope and changed files contribute to the same profile union", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android", "python"],
      skill_profiles: ["android", "python"],
      required_review: {
        android: ["android-code-review"],
        python: ["python-development-guide"],
      },
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "python-development-guide", path: "skills/python-development-guide/SKILL.md", tree_hash: "c" },
    ],
  }, {
    phaseId: "review",
    changedFiles: ["src/service.py"],
    taskScope: "안드로이드 컴포즈 화면과 파이썬 API를 함께 수정",
  });
  assert.deepEqual(plan.touched_profiles, ["android", "python"]);
  assert.equal(plan.task_scope, "안드로이드 컴포즈 화면과 파이썬 API를 함께 수정");
});

test("task scope keeps separately named profiles in the same family", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["node", "react"],
      skill_profiles: ["node", "react"],
      required_review: {
        node: ["node-development-guide"],
        react: ["react-development-guide"],
      },
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "node-development-guide", path: "skills/node-development-guide/SKILL.md", tree_hash: "b" },
      { name: "react-development-guide", path: "skills/react-development-guide/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "implement", taskScope: "Build a React frontend and Node.js API" });

  assert.deepEqual(plan.touched_profiles, ["node", "react"]);
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "code-generation-discipline",
    "node-development-guide",
    "react-development-guide",
  ]);
});

test("task scope uses the longer overlapping profile term", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["react", "react-native"],
      skill_profiles: ["react", "react-native"],
      required_review: {},
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
    ],
  }, { phaseId: "implement", taskScope: "Build a React Native screen" });

  assert.deepEqual(plan.touched_profiles, ["react-native"]);
});

test("react-native-web task scope selects React Web without native evidence", () => {
  const index = {
    selection: {
      profiles: ["react", "react-native"],
      skill_profiles: ["react", "react-native"],
      required_review: {
        react: ["react-development-guide"],
        "react-native": ["react-native-development-guide"],
      },
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "react-development-guide", path: "skills/react-development-guide/SKILL.md", tree_hash: "b" },
      { name: "react-native-development-guide", path: "skills/react-native-development-guide/SKILL.md", tree_hash: "c" },
    ],
  };
  for (const taskScope of ["Build a react-native-web component", "Build a React Native Web component"]) {
    const plan = resolveRuntimeSkillPlan(index, { phaseId: "implement", taskScope });
    assert.deepEqual(plan.touched_profiles, ["react"], taskScope);
    assert.deepEqual(plan.skills.map((skill) => skill.name), [
      "code-generation-discipline",
      "react-development-guide",
    ], taskScope);
  }

  const genericWebPlan = resolveRuntimeSkillPlan(index, {
    phaseId: "implement",
    taskScope: "Build a react-native-web component",
    changedFiles: ["src/App.tsx"],
  });
  assert.deepEqual(genericWebPlan.touched_profiles, ["react"]);

  const nativePlan = resolveRuntimeSkillPlan(index, {
    phaseId: "implement",
    taskScope: "Build a react-native-web component",
    changedFiles: ["src/screens/NativeHome.tsx"],
  });
  assert.deepEqual(nativePlan.touched_profiles, ["react", "react-native"]);
});

test("react native task scope escalates native Android before files exist", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["react-native"],
      skill_profiles: ["android", "react-native"],
      required_review: {
        android: ["android-code-review"],
        "react-native": ["react-native-development-guide"],
      },
      conditional_skills: { android: { implementation: [], review: [] } },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "react-native-development-guide", path: "skills/react-native-development-guide/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "implement", taskScope: "리액트 네이티브 안드로이드 네이티브 브리지 구현" });
  assert.deepEqual(plan.touched_profiles, ["android", "react-native"]);
});

test("task scope selects a routed Android specialist before files exist", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android"],
      skill_profiles: ["android"],
      required_review: { android: ["android-code-review"] },
      conditional_skills: { android: { implementation: ["jetpack-compose-m3"], review: [] } },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "jetpack-compose-m3", path: "skills/jetpack-compose-m3/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "implement", taskScope: "Build a Wear OS watch face with Compose Material 3 for Wear" });
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "android-code-review",
    "code-generation-discipline",
    "jetpack-compose-m3",
  ]);
});

test("adapter trigger terms select Android specialists before files exist", () => {
  const conditional = [...BUNDLED_ANDROID_TEST_SKILLS];
  const skills = ["code-generation-discipline", "android-code-review", ...conditional]
    .map((name) => ({ name, path: `skills/${name}/SKILL.md`, tree_hash: name }));
  const index = {
    selection: {
      profiles: ["android"],
      skill_profiles: ["android"],
      required_review: { android: ["android-code-review"] },
      conditional_skills: { android: { implementation: conditional, review: conditional } },
      profile_routing: PROFILE_ROUTING,
    },
    skills,
  };
  const cases = [
    ["Use Compose MediaQuery for pointer precision", "adaptive"],
    ["Migrate this app to AGP 9 and built-in Kotlin", "agp-9-upgrade"],
    ["Set up an Android SDK and AVD with the android CLI", "android-cli"],
    ["Audit an exported PendingIntent for intent redirection", "android-intent-security"],
    ["Migrate a SurfaceHolder Camera2 screen", "camera1-to-camerax"],
    ["Review the Compose Modifier parameter and root layout", "compose-modifier-and-layout-style"],
    ["Build a projected Activity with GlimmerTheme", "display-glasses-with-jetpack-compose-glimmer"],
    ["Fix an IME inset overlap in Compose", "edge-to-edge"],
    ["Migrate a Wear screen to TransformingLazyColumn", "jetpack-compose-m3"],
    ["Remove runBlocking and preserve CancellationException", "kotlin-coroutines-structured-concurrency"],
    ["Run a Google Play Data Safety compliance audit", "play-policy-insights"],
    ["Migrate custom components to the Compose Styles API", "styles"],
    ["Implement OTP-less email verification with GetDigitalCredentialOption", "verified-email"],
  ];

  for (const [taskScope, expected] of cases) {
    const plan = resolveRuntimeSkillPlan(index, { phaseId: "implement", taskScope });
    assert.equal(
      plan.skills.some((skill) => skill.name === expected),
      true,
      `${expected} not selected for ${taskScope}`,
    );
  }
});

test("adapter trigger terms preserve alphanumeric word boundaries", () => {
  const conditional = [...BUNDLED_ANDROID_TEST_SKILLS];
  const skills = ["code-generation-discipline", "android-code-review", ...conditional]
    .map((name) => ({ name, path: `skills/${name}/SKILL.md`, tree_hash: name }));
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android"],
      skill_profiles: ["android"],
      required_review: { android: ["android-code-review"] },
      conditional_skills: { android: { implementation: conditional, review: conditional } },
      profile_routing: PROFILE_ROUTING,
    },
    skills,
  }, {
    phaseId: "implement",
    taskScope: "Refactor Android RunBlockingAdapterFactory, MediaQueryable, and RestyleApiResult",
  });

  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "android-code-review",
    "code-generation-discipline",
  ]);
});

test("family specificity separates web and backend TypeScript files deterministically", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["nextjs", "node", "react", "typescript"],
      skill_profiles: ["nextjs", "node", "react", "typescript"],
      required_review: {
        nextjs: ["react-development-guide"],
        node: ["node-development-guide"],
        react: ["react-development-guide"],
        typescript: ["typescript-development-guide"],
      },
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "node-development-guide", path: "skills/node-development-guide/SKILL.md", tree_hash: "b" },
      { name: "react-development-guide", path: "skills/react-development-guide/SKILL.md", tree_hash: "c" },
      { name: "typescript-development-guide", path: "skills/typescript-development-guide/SKILL.md", tree_hash: "d" },
    ],
  }, { phaseId: "review", changedFiles: ["server/routes.ts", "app/page.tsx"] });
  assert.deepEqual(plan.touched_profiles, ["nextjs", "node"]);
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "code-generation-discipline",
    "node-development-guide",
    "react-development-guide",
  ]);
});

test("generic profile stays a fallback when a specific file profile matches", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["generic", "python"],
      skill_profiles: ["generic", "python"],
      required_review: {},
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
    ],
  }, { phaseId: "review", changedFiles: ["src/service.py"] });
  assert.deepEqual(plan.touched_profiles, ["python"]);
});

test("Spring source paths outrank Android extension-only matches", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android", "spring"],
      skill_profiles: ["android", "spring"],
      required_review: {
        android: ["android-code-review"],
        spring: ["spring-development-guide"],
      },
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "android-code-review", path: "skills/android-code-review/SKILL.md", tree_hash: "b" },
      { name: "spring-development-guide", path: "skills/spring-development-guide/SKILL.md", tree_hash: "c" },
    ],
  }, { phaseId: "review", changedFiles: ["src/main/java/com/example/OrderService.java"] });
  assert.deepEqual(plan.touched_profiles, ["spring"]);
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "code-generation-discipline",
    "spring-development-guide",
  ]);
});

test("Android module paths outrank Spring source-pattern matches", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android", "spring"],
      skill_profiles: ["android", "spring"],
      required_review: {},
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
    ],
  }, { phaseId: "review", changedFiles: ["feature/orders/src/main/kotlin/OrdersScreen.kt"] });
  assert.deepEqual(plan.touched_profiles, ["android"]);
});

test("unmatched early task scope keeps every active profile fail closed", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["android", "python"],
      skill_profiles: ["android", "python"],
      required_review: {},
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
    ],
  }, { phaseId: "implement", taskScope: "Fix the login regression" });
  assert.deepEqual(plan.touched_profiles, ["android", "python"]);
});

test("task terms use word boundaries instead of matching reactive as React", () => {
  const plan = resolveRuntimeSkillPlan({
    selection: {
      profiles: ["react", "spring"],
      skill_profiles: ["react", "spring"],
      required_review: {},
      conditional_skills: {},
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
    ],
  }, { phaseId: "implement", taskScope: "Fix Spring reactive streams" });
  assert.deepEqual(plan.touched_profiles, ["spring"]);
});

test("Node and Spring profile installs include their runtime guides", () => {
  const f = fixture();
  const node = resolveInstallSelection({
    args: ["--profile", "node"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  const spring = resolveInstallSelection({
    args: ["--profile", "spring"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  assert(node.skillNames.has("node-development-guide"));
  assert(node.skillNames.has("typescript-development-guide"));
  assert.deepEqual(node.conditionalSkills.node, {
    implementation: ["typescript-development-guide"],
    review: ["typescript-development-guide"],
  });
  assert(spring.skillNames.has("spring-development-guide"));
});

test("Android install inventory keeps every locked official and ecosystem skill", () => {
  const f = fixture();
  const selection = resolveInstallSelection({
    args: ["--profile", "android"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  for (const name of OFFICIAL_ANDROID_SKILLS) {
    assert.equal(selection.skillNames.has(name), true, name);
  }
  assert.equal(selection.skillNames.has("testing-localization"), true);
});

test("Node TypeScript guide is conditional on task or touched TypeScript files", () => {
  const index = {
    selection: {
      profiles: ["node"],
      skill_profiles: ["node"],
      required_review: { node: ["node-development-guide"] },
      conditional_skills: {
        node: {
          implementation: ["typescript-development-guide"],
          review: ["typescript-development-guide"],
        },
      },
      profile_routing: PROFILE_ROUTING,
    },
    skills: [
      { name: "code-generation-discipline", path: "skills/code-generation-discipline/SKILL.md", tree_hash: "a" },
      { name: "node-development-guide", path: "skills/node-development-guide/SKILL.md", tree_hash: "b" },
      { name: "typescript-development-guide", path: "skills/typescript-development-guide/SKILL.md", tree_hash: "c" },
    ],
  };

  for (const input of [
    { phaseId: "implement", changedFiles: ["server/orders.ts"] },
    { phaseId: "review", changedFiles: ["tsconfig.json"] },
    { phaseId: "implement", taskScope: "Implement an Express TypeScript API" },
  ]) {
    const plan = resolveRuntimeSkillPlan(index, input);
    assert.deepEqual(plan.touched_profiles, ["node"]);
    assert.deepEqual(plan.skills.map((skill) => skill.name), [
      "code-generation-discipline",
      "node-development-guide",
      "typescript-development-guide",
    ]);
  }

  const javascriptOnly = resolveRuntimeSkillPlan(index, {
    phaseId: "review",
    changedFiles: ["server/orders.js"],
    taskScope: "Review the Express API",
  });
  assert.deepEqual(javascriptOnly.skills.map((skill) => skill.name), [
    "code-generation-discipline",
    "node-development-guide",
  ]);
});

test("explicit skills remain additive to auto-detected profile requirements", () => {
  const f = fixture();
  for (const [profile, required] of [
    ["android", "android-code-review"],
    ["react", "react-development-guide"],
    ["python", "python-development-guide"],
  ]) {
    const selection = resolveInstallSelection({
      args: ["--skill", "extra"],
      detectedProfile: profile,
      kitRoot: KIT_ROOT,
      projectRoot: f.projectRoot,
    });
    assert.deepEqual(selection.profiles, [profile]);
    assert.equal(selection.profileSelection, "auto");
    assert(selection.skillNames.has(required), profile);
    assert(selection.skillNames.has("extra"), profile);
  }
});

test("auto reinstall refreshes detection while explicit union reinstall preserves order", () => {
  const f = fixture();
  const auto = resolveInstallSelection({
    args: [],
    detectedProfile: "react",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  const refreshed = mergeInstallSelectionWithPrevious(
    auto,
    {
      selection: {
        mode: "filtered",
        profile_selection: "auto",
        profiles: ["node"],
        explicit_skills: ["old-extra"],
      },
    },
    KIT_ROOT,
    f.projectRoot,
  );
  assert.deepEqual(refreshed.profiles, ["react"]);
  assert.equal(refreshed.profileSelection, "auto");
  assert.deepEqual(refreshed.explicitSkills, ["old-extra"]);

  const additive = resolveInstallSelection({
    args: ["--skill", "new-extra"],
    detectedProfile: "react",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  const preserved = mergeInstallSelectionWithPrevious(
    additive,
    {
      selection: {
        mode: "filtered",
        profile_selection: "explicit",
        profiles: ["python", "android"],
        explicit_skills: ["old-extra"],
      },
    },
    KIT_ROOT,
    f.projectRoot,
  );
  assert.deepEqual(preserved.profiles, ["python", "android"]);
  assert.equal(preserved.profileSelection, "explicit");
  assert.deepEqual(new Set(preserved.explicitSkills), new Set(["old-extra", "new-extra"]));
});

test("installing the kit into itself does not defeat profile filtering", () => {
  const selection = resolveInstallSelection({
    args: ["--profile", "node"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: KIT_ROOT,
  });
  assert(selection.skillNames.has("node-development-guide"));
  assert(!selection.skillNames.has("android-code-review"));
});

test("a separate target project still installs all project-local skill sources", () => {
  const f = fixture();
  writeSkill(path.join(f.projectRoot, "skills"), "project-guide", "project guide");
  writeSkill(path.join(f.projectRoot, ".agent-flow", "local-skills"), "private-guide", "private guide");
  const selection = resolveInstallSelection({
    args: ["--profile", "node"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  assert(selection.skillNames.has("project-guide"));
  assert(selection.skillNames.has("private-guide"));
});

test("dot path segments are rejected as explicit skill names", () => {
  const f = fixture();
  for (const name of [".", ".."]) {
    assert.throws(
      () => resolveInstallSelection({
        args: ["--skill", name],
        detectedProfile: "generic",
        kitRoot: f.kitRoot,
        projectRoot: f.projectRoot,
      }),
      /unsafe skill name/,
    );
  }
});

test("dependency closure uses only the selected authority metadata", () => {
  const f = fixture();
  writeSkill(path.join(f.kitRoot, "skills"), "guide", "bundled");
  fs.writeFileSync(
    path.join(f.kitRoot, "skills", "guide", "SKILL.md"),
    "---\nname: guide\ndependencies: [shadowed-dependency]\n---\nbundled\n",
    "utf8",
  );
  writeSkill(path.join(f.kitRoot, "skills"), "shadowed-dependency", "shadowed");
  writeSkill(path.join(f.home, ".codex", "skills"), "guide", "host");
  fs.writeFileSync(
    path.join(f.home, ".codex", "skills", "guide", "SKILL.md"),
    "---\nname: guide\ndependencies: [selected-dependency]\n---\nhost\n",
    "utf8",
  );
  writeSkill(path.join(f.home, ".codex", "skills"), "selected-dependency", "selected");

  const plan = resolveProfileSkillSources({
    ...f,
    skillNames: new Set(["guide"]),
    activeHost: "codex",
  });

  assert.deepEqual(plan.entries.map((entry) => entry.name), ["guide", "selected-dependency"]);
  assert.equal(plan.entries[0].source_kind, "host-bootstrap");
  assert.equal(plan.skillNames.has("SELECTED-DEPENDENCY"), true);
  assert.equal(plan.skillNames.has("shadowed-dependency"), false);

  const finalized = mergeResolvedSkillClosure({
    skillNames: new Set(["guide"]),
    copyRootNames: new Set(["guide"]),
  }, plan);
  assert.equal(finalized.skillNames.has("SELECTED-DEPENDENCY"), true);
  assert.equal(finalized.copyRootNames.has("selected-dependency"), true);
});

test("logical skill and explicit profile names are casefolded and deduplicated", () => {
  const f = fixture();
  writeSkill(path.join(f.projectRoot, "skills"), "MixedDirectory", "project guide");
  fs.writeFileSync(
    path.join(f.projectRoot, "skills", "MixedDirectory", "SKILL.md"),
    "---\nname: Mixed-Guide\n---\nproject guide\n",
    "utf8",
  );
  const selection = resolveInstallSelection({
    args: ["--profile", "Node,node,NODE", "--skill", "MIXED-GUIDE,mixed-guide"],
    detectedProfile: "generic",
    kitRoot: KIT_ROOT,
    projectRoot: f.projectRoot,
  });
  assert.deepEqual(selection.profiles, ["node"]);
  assert.deepEqual(selection.explicitSkills, ["mixed-guide"]);
  assert.equal(selection.skillNames.has("MIXED-GUIDE"), true);

  const plan = resolveProfileSkillSources({
    ...f,
    kitRoot: KIT_ROOT,
    skillNames: new Set(["MIXED-GUIDE", "mixed-guide"]),
  });
  assert.deepEqual(plan.entries.map((entry) => entry.name), ["mixed-guide"]);
  assert.equal(plan.entries[0].source_path.endsWith("MixedDirectory"), true);

  writeSkill(path.join(f.home, ".codex", "skills"), "Host-Guide", "host guide");
  const hostPlan = resolveProfileSkillSources({
    ...f,
    kitRoot: KIT_ROOT,
    skillNames: new Set(["HOST-GUIDE"]),
    activeHost: "codex",
  });
  assert.deepEqual(hostPlan.entries.map((entry) => entry.name), ["host-guide"]);
  assert.equal(hostPlan.entries[0].source_kind, "host-bootstrap");
  assert.match(fs.readFileSync(path.join(hostPlan.entries[0].source_path, "SKILL.md"), "utf8"), /host guide/);
});

test("duplicate frontmatter logical names fail independent of directory order", () => {
  const f = fixture();
  for (const directory of ["z-last", "a-first"]) {
    const root = path.join(f.projectRoot, "skills", directory);
    fs.mkdirSync(root, { recursive: true });
    fs.writeFileSync(path.join(root, "SKILL.md"), "---\nname: Shared-Name\n---\nbody\n", "utf8");
  }
  assert.throws(
    () => resolveInstallSelection({
      args: ["--profile", "node"],
      detectedProfile: "generic",
      kitRoot: KIT_ROOT,
      projectRoot: f.projectRoot,
    }),
    (error) => /conflicting project-local skill paths: shared-name/.test(error.message)
      && error.message.indexOf("a-first") < error.message.indexOf("z-last"),
  );
});

test("runtime lookup is casefolded and rejects logical index conflicts", () => {
  const selection = {
    profiles: ["NODE", "node"],
    required_review: { node: ["NODE-DEVELOPMENT-GUIDE"] },
    conditional_skills: {},
    profile_routing: PROFILE_ROUTING,
  };
  const plan = resolveRuntimeSkillPlan({
    selection,
    skills: [
      { name: "Code-Generation-Discipline", path: "skills/code/SKILL.md", tree_hash: "a" },
      { name: "Node-Development-Guide", path: "skills/node/SKILL.md", tree_hash: "b" },
    ],
  }, { phaseId: "review" });
  assert.deepEqual(plan.active_profiles, ["node"]);
  assert.deepEqual(plan.skills.map((skill) => skill.name), [
    "code-generation-discipline",
    "node-development-guide",
  ]);

  assert.throws(
    () => resolveRuntimeSkillPlan({
      selection,
      skills: [
        { name: "guide", path: "skills/a/SKILL.md", tree_hash: "a" },
        { name: "GUIDE", path: "skills/b/SKILL.md", tree_hash: "b" },
      ],
    }, { phaseId: "review" }),
    /conflicting installed skill index logical skill name: guide/,
  );
});
