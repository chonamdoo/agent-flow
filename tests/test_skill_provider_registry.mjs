import assert from "node:assert/strict";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const registryApi = await import("../lib/skill-provider-registry.mjs").catch(() => ({}));
const selectionApi = await import("../lib/skill-selection.mjs").catch(() => ({}));
const loaderApi = await import("../lib/skill-provider-registry-loader.mjs").catch(() => ({}));

const ANDROID_ARCHIVE_HASH = "b7315d6ee5010d17e9ba169eb025fca2725682289e5dde3544669e408940d3c9";
const ANDROID_CATALOG_HASH = "54e5a4242c2fcc9cf8ff36312e87d8c344fe47886b5b065b76f7c76050f77694";
const CHRIS_ARCHIVE_HASH = "662b75d20d32d2ec20cc483e0f5b22e1a1ac37e3c52dbf82cf1526388ad1a6c7";
const CHRIS_CATALOG_HASH = "79628d78922f87eac27649ec3e50f236857c21cd801d5a65d624e62dd1ec4ebd";
const ANDROID_EDGE_HASH = "4e2fbec8006a807e59f76114177aaae97ef6b1b9b83c6bb119c66a9e31aaa3c2";
const CHRIS_STATE_HOISTING_HASH = "49da3ec77d9107a41eaec384ec08b226cf9bd762fbedfb51a44410240596cf2e";

function requireApi(name) {
  assert.equal(typeof registryApi[name], "function", `${name} must be exported`);
  return registryApi[name];
}

function requireSelectionApi(name) {
  assert.equal(typeof selectionApi[name], "function", `${name} must be exported`);
  return selectionApi[name];
}
function requireLoaderApi(name) {
  assert.equal(typeof loaderApi[name], "function", `${name} must be exported by loader`);
  return loaderApi[name];
}


function catalogManifestDigest(sourceHashes) {
  const canonical = Object.fromEntries(
    Object.entries(sourceHashes).sort(([left], [right]) => (
      left < right ? -1 : left > right ? 1 : 0
    )),
  );
  return crypto.createHash("sha256").update(JSON.stringify(canonical)).digest("hex");
}

function provider(id, overrides = {}) {
  return {
    id,
    adapter: "source-kind",
    version: "1.0.0",
    trust_tier: "organization",
    ownership: "organization",
    provenance: {
      source: `https://example.test/${id}`,
    },
    compatibility: {
      registry: 1,
      profiles: ["*"],
      hosts: ["*"],
      source_kinds: ["bundled"],
    },
    config: {
      source_kinds: ["bundled"],
    },
    ...overrides,
  };
}

function profileProvider(id, concreteId, sourceHash) {
  return provider(id, {
    adapter: "profile-catalog",
    provenance: {
      source: "https://github.com/android/skills",
    },
    compatibility: {
      registry: 1,
      profiles: ["android"],
      hosts: ["*"],
      source_kinds: ["host-bootstrap"],
    },
    config: {
      profile: "android",
      catalog: "android_skills",
      membership: ["implementation", "review"],
      source_binding: {
        mode: "active-host",
        require_install_policy: "never",
        require_active_host_only: true,
      },
      content_hash: {
        mode: "pinned",
        hashes: {
          [concreteId]: sourceHash,
        },
      },
    },
  });
}

function registry(providers) {
  return {
    version: 1,
    policy: {
      allowlist: providers.map((entry) => entry.id).filter((id) => /^[a-z0-9][a-z0-9_-]*$/.test(id || "")),
      minimum_trust: "user",
      preferred_providers: {},
    },
    providers,
  };
}

function writeRegistry(value) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-provider-registry-"));
  const registryPath = path.join(root, "registry.json");
  fs.writeFileSync(registryPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  return registryPath;
}

function indexedCandidateEvidence(skill) {
  const concreteId = skill.name.toLowerCase();
  const sourceHost = skill.source_host ?? null;
  return {
    concrete_id: concreteId,
    source_kind: skill.source,
    source_hash: skill.tree_hash,
    source_host: sourceHost,
    source_locator: sourceHost === null
      ? `project://${skill.path ?? `skills/${concreteId}/SKILL.md`}`
      : `host://${sourceHost}/skills/${concreteId}`,
  };
}

test("registry authority IO is exposed only by the outer loader", () => {
  assert.equal(registryApi.loadSkillProviderRegistry, undefined);
  assert.equal(registryApi.readAuthorityFileSnapshot, undefined);
  assert.equal(typeof requireLoaderApi("loadSkillProviderRegistry"), "function");
  assert.equal(typeof requireLoaderApi("readAuthorityFileSnapshot"), "function");
});

test("provider registry loads versioned provider-neutral metadata", () => {
  const loadSkillProviderRegistry = requireLoaderApi("loadSkillProviderRegistry");
  const loaded = loadSkillProviderRegistry(writeRegistry(registry([
    provider("organization"),
  ])));

  assert.equal(loaded.version, 1);
  assert.deepEqual(loaded.providers.map((entry) => entry.id), ["organization"]);
  assert.deepEqual(loaded.quarantined, []);
  assert.match(loaded.fingerprint, /^[0-9a-f]{64}$/);
  assert.deepEqual(loaded.providers[0].provenance, {
    source: "https://example.test/organization",
  });
});

test("repository upstream profile catalogs pin revisions and content hashes", () => {
  const loadSkillProviderRegistry = requireLoaderApi("loadSkillProviderRegistry");
  const root = process.cwd();
  const loaded = loadSkillProviderRegistry(
    path.join(root, "skills", "provider-registry.json"),
    { authorityRoot: root },
  );
  const android = loaded.providers.find((entry) => entry.id === "android-official");
  const chris = loaded.providers.find((entry) => entry.id === "chris-banes");

  assert.deepEqual(loaded.quarantined, []);
  assert.equal(android.provenance.source, "https://github.com/android/skills");
  assert.equal(android.provenance.revision, "57ff3c7d02a53781954f9ce2df92f14b7fbb2ded");
  assert.equal(android.config.content_hash.mode, "pinned");
  assert.equal(android.config.content_hash.hashes["edge-to-edge"], ANDROID_EDGE_HASH);
  assert.equal(android.config.content_hash.hashes["camera1-to-camerax"], undefined);
  assert.equal(Object.keys(android.config.content_hash.hashes).length, 13);
  assert.deepEqual(android.compatibility.source_kinds, ["host-bootstrap"]);
  assert.equal(chris.provenance.source, "https://github.com/chrisbanes/skills/tree/main/skills");
  assert.equal(chris.provenance.revision, "1290b051d29f81929fec3a09ec5fc8caf96b4555");
  assert.equal(chris.config.content_hash.mode, "pinned");
  assert.equal(
    chris.config.content_hash.hashes["compose-state-hoisting"],
    CHRIS_STATE_HOISTING_HASH,
  );
  assert.equal(Object.keys(chris.config.content_hash.hashes).length, 16);
  assert.deepEqual(chris.compatibility.source_kinds, ["host-bootstrap"]);
});

test("upstream trust providers reject observed content hashes", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const value = registry([
    profileProvider("official-provider", "demo", "1".repeat(64)),
  ]);
  value.providers[0].trust_tier = "official";
  value.providers[0].config.content_hash = { mode: "observed" };

  const loaded = normalizeSkillProviderRegistry(value);

  assert.deepEqual(loaded.providers, []);
  assert.equal(loaded.quarantined[0].reason, "provider_metadata_invalid");
  assert.equal(loaded.blocking_scopes[0].id, "official-provider");
});


test("provider registry rejects symlinks and hardlinks", () => {
  const loadSkillProviderRegistry = requireLoaderApi("loadSkillProviderRegistry");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-provider-auth-"));
  const source = path.join(root, "source.json");
  const symlink = path.join(root, "symlink.json");
  const hardlink = path.join(root, "hardlink.json");
  fs.writeFileSync(source, JSON.stringify(registry([provider("organization")])));
  fs.symlinkSync(source, symlink);
  fs.linkSync(source, hardlink);

  assert.throws(
    () => loadSkillProviderRegistry(symlink, { authorityRoot: root }),
    /unsafe skill provider registry file/,
  );
  assert.throws(
    () => loadSkillProviderRegistry(hardlink, { authorityRoot: root }),
    /unsafe skill provider registry file/,
  );
});

test("provider registry rejects replacement after descriptor open", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-flow-provider-race-"));
  const registryPath = path.join(root, "registry.json");
  const originalPath = path.join(root, "registry.original.json");
  fs.writeFileSync(registryPath, JSON.stringify(registry([provider("organization")])));
  const moduleUrl = new URL("../lib/skill-provider-registry-loader.mjs", import.meta.url).href;
  const script = `
    import { loadSkillProviderRegistry } from ${JSON.stringify(moduleUrl)};
    loadSkillProviderRegistry(process.argv[1], { authorityRoot: process.argv[2] });
  `;
  const child = spawn(process.execPath, ["--input-type=module", "-e", script, registryPath, root], {
    env: {
      ...process.env,
      AGENT_FLOW_TEST_HOLD_JSON_AUTH_PATH: registryPath,
      AGENT_FLOW_TEST_HOLD_AFTER_JSON_AUTH_OPEN_MS: "1500",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`provider race marker timeout: ${stderr}`)), 5000);
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
      if (!stderr.includes("agent-flow:test-json-authority-opened")) return;
      clearTimeout(timeout);
      resolve();
    });
    child.once("error", reject);
  });
  fs.renameSync(registryPath, originalPath);
  fs.writeFileSync(registryPath, JSON.stringify(registry([provider("replacement")])));
  const exitCode = await new Promise((resolve, reject) => {
    child.once("exit", resolve);
    child.once("error", reject);
  });

  assert.notEqual(exitCode, 0);
  assert.match(stderr, /skill provider registry (?:file|directory) changed while reading/);
});

test("provider registry envelope rejects unknown fields", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  assert.throws(
    () => normalizeSkillProviderRegistry({
      ...registry([provider("organization")]),
      ignored: true,
    }),
    /invalid skill provider registry fields/,
  );
});

test("new adapter contract resolves providers without resolver changes", () => {
  const createSkillProviderAdapterRegistry = requireApi("createSkillProviderAdapterRegistry");
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const adapterRegistry = createSkillProviderAdapterRegistry([{
    id: "concrete-id",
    version: "1.0.0",
    priority: 300,
    content_hash_mode: () => "verified",
    evidence_source: (_providerMetadata, providerCandidate) => (
      providerCandidate.source_locator
    ),
    normalize_config(raw) {
      assert.deepEqual(Object.keys(raw), ["concrete_id"]);
      return { concrete_id: raw.concrete_id };
    },
    match(providerMetadata, providerCandidate) {
      const matched = providerMetadata.config.concrete_id === providerCandidate.concrete_id;
      return matched
        ? { matched: true, evidence: testEvidence(providerCandidate) }
        : { matched: false };
    },
  }]);
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("new-provider", {
      adapter: "concrete-id",
      config: { concrete_id: "new-skill" },
      compatibility: {
        registry: 1,
        profiles: ["node"],
        hosts: ["codex"],
        source_kinds: ["project"],
      },
    }),
  ]), { adapterRegistry });
  const resolved = resolveSkillProviderClaims(loaded, {
    profile: "node",
    host: "codex",
    catalogs: {},
    candidates: [candidate("new-skill", "project", "b".repeat(64))],
  }, { adapterRegistry });

  assert.equal(resolved.claims[0].provider_id, "new-provider");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-adapter-composition-"));
  const registryPath = path.join(root, "provider-registry.json");
  fs.writeFileSync(registryPath, JSON.stringify(registry([
    provider("new-provider", {
      adapter: "concrete-id",
      config: { concrete_id: "new-skill" },
      compatibility: {
        registry: 1,
        profiles: ["node"],
        hosts: ["codex"],
        source_kinds: ["project"],
      },
    }),
  ])));
  const composed = resolveSkillProviderIndex({
    skills: [{
      name: "new-skill",
      path: "skills/new-skill/SKILL.md",
      source: "project",
      tree_hash: "b".repeat(64),
    }],
    activeProfiles: ["node"],
    activeHost: "codex",
    profilesRoot: root,
    registryPath,
    compatibility: { version: 1, skills: [] },
    adapterRegistry,
    authenticateCandidate: indexedCandidateEvidence,
  });
  assert.equal(composed.claims[0].provider_id, "new-provider");
  assert.equal(composed.claims[0].adapter, "concrete-id");
  assert.equal(resolved.claims[0].adapter, "concrete-id");
  const mismatches = [
    { profile: "python", host: "codex", source_kind: "project" },
    { profile: "node", host: "claude", source_kind: "project" },
    { profile: "node", host: "codex", source_kind: "bundled" },
  ];
  for (const mismatch of mismatches) {
    const outcome = resolveSkillProviderClaims(loaded, {
      profile: mismatch.profile,
      host: mismatch.host,
      catalogs: {},
      candidates: [candidate("new-skill", mismatch.source_kind, "b".repeat(64))],
    }, { adapterRegistry });
    assert.deepEqual(outcome.claims, []);
  }
});

test("invalid provider metadata is quarantined without poisoning valid providers", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("valid-provider"),
    provider("Bad Provider"),
  ]));

  assert.deepEqual(loaded.providers.map((entry) => entry.id), ["valid-provider"]);
  assert.equal(loaded.quarantined.length, 1);
  assert.equal(loaded.quarantined[0].reason, "provider_metadata_invalid");
  assert.equal(loaded.quarantined[0].provider_id, null);
  assert.equal(loaded.quarantined[0].repairable, false);
  assert.equal(
    loaded.quarantined[0].metadata_path,
    "skill-provider-registry.json#provider:unknown",
  );
});

test("valid and invalid entries sharing a provider id are quarantined together", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("duplicate-provider"),
    provider("duplicate-provider", { version: "invalid" }),
  ]));

  assert.deepEqual(loaded.providers, []);
  assert.equal(loaded.quarantined.length, 2);
  assert.ok(loaded.quarantined.every(
    (entry) => entry.provider_id === "duplicate-provider"
      && entry.reason === "provider_metadata_invalid",
  ));
});

test("registry fingerprint includes valid providers and quarantine diagnostics deterministically", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const left = normalizeSkillProviderRegistry(registry([
    provider("z-provider"),
    provider("Bad Provider"),
    provider("a-provider"),
  ]));
  const right = normalizeSkillProviderRegistry(registry([
    provider("a-provider"),
    provider("z-provider"),
    provider("Bad Provider"),
  ]));

  assert.equal(left.fingerprint, right.fingerprint);
  assert.deepEqual(left.providers.map((entry) => entry.id), ["a-provider", "z-provider"]);
});

test("unknown adapter type and unsafe provider id are quarantined", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("unknown-adapter", { adapter: "provider-specific-code" }),
    provider("../unsafe"),
    provider("valid-provider"),
  ]));

  assert.deepEqual(loaded.providers.map((entry) => entry.id), ["valid-provider"]);
  assert.equal(loaded.quarantined.length, 2);
  assert.ok(loaded.quarantined.some(
    (entry) => entry.provider_id === null
      && entry.metadata_path === "skill-provider-registry.json#provider:unknown",
  ));
});

test("unsafe adapter identifiers do not escape into quarantine diagnostics", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("unsafe-adapter", { adapter: "missing\n\ud800" }),
  ]));

  assert.equal(loaded.quarantined.length, 1);
  assert.equal(loaded.quarantined[0].detail, "unknown provider adapter");
  assert.doesNotMatch(JSON.stringify(loaded.quarantined[0]), /\\ud800|missing/);
});

test("provider provenance rejects Unicode control and format characters", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("source-control", {
      provenance: { source: "project://skills\u001c", source_hash: "candidate-tree" },
    }),
    provider("source-next-line", {
      provenance: { source: "project://skills\u0085", source_hash: "candidate-tree" },
    }),
    provider("source-bom", {
      provenance: { source: "project://skills\ufeff", source_hash: "candidate-tree" },
    }),
  ]));

  assert.deepEqual(loaded.providers, []);
  assert.equal(loaded.quarantined.length, 3);
  assert.ok(loaded.quarantined.every(
    (entry) => entry.reason === "provider_metadata_invalid",
  ));
});

test("provider provenance rejects malformed immutable revisions", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("bad-revision", {
      provenance: { source: "https://example.test/bad", revision: "main" },
    }),
  ]));

  assert.deepEqual(loaded.providers, []);
  assert.equal(loaded.quarantined[0].reason, "provider_metadata_invalid");
});


test("provider provenance rejects lone surrogate characters", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("source-surrogate", {
      provenance: { source: "project://skills\ud800", source_hash: "candidate-tree" },
    }),
  ]));

  assert.deepEqual(loaded.providers, []);
  assert.equal(loaded.quarantined[0].provider_id, "source-surrogate");
});

test("provider identifiers, versions, adapters, and hashes require full-string matches", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const createSkillProviderAdapterRegistry = requireApi("createSkillProviderAdapterRegistry");
  const badHash = profileProvider("bad-hash", "demo", `${"a".repeat(64)}\n`);
  const badCatalog = profileProvider("bad-catalog", "demo", "b".repeat(64));
  badCatalog.config.catalog = "android_skills\n";
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("bad-version", { version: "1.0.0\n" }),
    provider("bad-adapter", { adapter: "source-kind\n" }),
    badHash,
    provider("bad-revision-newline", {
      provenance: {
        source: "https://example.test/bad-revision-newline",
        revision: `${"a".repeat(40)}\n`,
      },
    }),
    badCatalog,
    provider("\ud800"),
  ]));

  assert.deepEqual(loaded.providers, []);
  assert.equal(loaded.quarantined.length, 6);
  assert.ok(loaded.quarantined.every(
    (entry) => entry.reason === "provider_metadata_invalid",
  ));
  assert.ok(loaded.quarantined.some((entry) => entry.provider_id === null));
  assert.throws(
    () => createSkillProviderAdapterRegistry([{
      id: "custom-adapter",
      version: "1.0.0\n",
      priority: 1,
      content_hash_mode: () => "verified",
      evidence_source: (_providerMetadata, providerCandidate) => (
        providerCandidate.source_locator
      ),
      normalize_config: () => ({}),
      match: () => ({ matched: true, evidence: null }),
    }]),
    /invalid skill provider adapter version/,
  );
});

function candidate(concreteId, sourceKind, sourceHash, overrides = {}) {
  const sourceHost = overrides.source_host
    ?? (sourceKind === "host-bootstrap" ? "codex" : null);
  return {
    concrete_id: concreteId,
    aliases: [],
    source_kind: sourceKind,
    source_hash: sourceHash,
    source_host: sourceHost,
    source_locator: sourceHost === null
      ? `project://skills/${concreteId}`
      : `host://${sourceHost}/skills/${concreteId}`,
    source_authenticated: true,
    declared_provider: null,
    ...overrides,
  };
}

function testEvidence(providerCandidate, overrides = {}) {
  return {
    source: overrides.source ?? providerCandidate.source_locator,
    catalog_ref: overrides.catalog_ref ?? null,
    catalog_hash: overrides.catalog_hash ?? null,
    content_hash_mode: overrides.content_hash_mode ?? "verified",
    source_host: overrides.source_host ?? providerCandidate.source_host,
    source_kind: overrides.source_kind ?? providerCandidate.source_kind,
    source_locator: overrides.source_locator ?? providerCandidate.source_locator,
  };
}

function catalogContext(candidates) {
  return {
    profile: "android",
    host: "codex",
    candidates,
    catalogs: {
      android: {
        android_skills: {
          source: "https://github.com/android/skills",
          install_policy: "never",
          active_host_only: true,
          hosts: {
            codex: "~/.codex/skills/<skill>/SKILL.md",
            claude: "~/.claude/skills/<skill>/SKILL.md",
            omp: "~/.omp/agent/skills/<skill>/SKILL.md",
          },
          implementation: ["edge-to-edge"],
          review: [],
        },
        chrisbanes_skills: {
          source: "https://github.com/chrisbanes/skills/tree/main/skills",
          install_policy: "never",
          active_host_only: true,
          hosts: {
            codex: "~/.codex/skills/<skill>/SKILL.md",
            claude: "~/.claude/skills/<skill>/SKILL.md",
            omp: "~/.omp/agent/skills/<skill>/SKILL.md",
          },
          implementation: ["compose-state-hoisting"],
          review: [],
        },
      },
    },
  };
}

test("profile and source kind adapters produce provider-neutral claims", () => {
  const loadSkillProviderRegistry = requireLoaderApi("loadSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = loadSkillProviderRegistry(path.join(process.cwd(), "skills", "provider-registry.json"));
  const hash = "a".repeat(64);
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate("edge-to-edge", "host-bootstrap", ANDROID_EDGE_HASH),
    candidate("compose-state-hoisting", "host-bootstrap", CHRIS_STATE_HOISTING_HASH),
    candidate("code-generation-discipline", "bundled", hash),
    candidate("local-demo", "local", hash, {
      source_locator: "project://.agent-flow/local-skills/local-demo",
    }),
    candidate("project-demo", "project", hash),
    candidate("snapshot-demo", "project-snapshot", hash, {
      source_locator: "project://.agent-flow/skills/snapshot-demo",
    }),
    candidate("user-demo", "shared", hash),
  ]));

  assert.deepEqual(
    resolved.claims.map((claim) => [claim.concrete_id, claim.provider_id]),
    [
      ["code-generation-discipline", "organization"],
      ["compose-state-hoisting", "chris-banes"],
      ["edge-to-edge", "android-official"],
      ["local-demo", "project-local"],
      ["project-demo", "project-local"],
      ["snapshot-demo", "project-local"],
      ["user-demo", "user-local"],
    ],
  );
  const claimsByName = new Map(
    resolved.claims.map((claim) => [claim.concrete_id, claim]),
  );
  assert.equal(
    claimsByName.get("local-demo").source,
    "project://.agent-flow/local-skills/local-demo",
  );
  assert.equal(
    claimsByName.get("project-demo").source,
    "project://skills/project-demo",
  );
  assert.equal(
    claimsByName.get("snapshot-demo").source,
    "project://.agent-flow/skills/snapshot-demo",
  );
  assert.equal(
    claimsByName.get("user-demo").source,
    "user://skills",
  );
  const androidClaim = claimsByName.get("edge-to-edge");
  assert.equal(androidClaim.content_hash_mode, "pinned");
  assert.equal(androidClaim.status, "verified");
  assert.equal(
    androidClaim.provenance_revision,
    "57ff3c7d02a53781954f9ce2df92f14b7fbb2ded",
  );
  assert.deepEqual(androidClaim.compatibility, {
    registry: 1,
    profiles: ["android"],
    hosts: ["*"],
    source_kinds: ["host-bootstrap"],
  });
  assert.deepEqual(resolved.quarantined, []);
});

test("catalog provider rejects a candidate from another host", () => {
  const loadSkillProviderRegistry = requireLoaderApi("loadSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = loadSkillProviderRegistry(path.join(process.cwd(), "skills", "provider-registry.json"));
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate("edge-to-edge", "host-bootstrap", "0".repeat(64), {
      source_host: "claude",
      source_locator: "host://claude/skills/edge-to-edge",
    }),
  ]));

  assert.deepEqual(resolved.claims, []);
  assert.equal(resolved.quarantined[0].reason, "provider_source_not_active_host");
  assert.equal(resolved.quarantined[0].provider_id, "android-official");
});

test("catalog membership without a pinned provider hash fails closed", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const fallback = provider("organization", {
    compatibility: {
      registry: 1,
      profiles: ["*"],
      hosts: ["*"],
      source_kinds: ["host-bootstrap"],
    },
    config: {
      source_kinds: ["host-bootstrap"],
    },
  });
  const loaded = normalizeSkillProviderRegistry(registry([
    profileProvider("android-official", "edge-to-edge", "1".repeat(64)),
    fallback,
  ]));
  const context = catalogContext([
    candidate("camera1-to-camerax", "host-bootstrap", "2".repeat(64)),
  ]);
  context.catalogs.android.android_skills.source = "https://github.com/android/skills";
  context.catalogs.android.android_skills.implementation = ["camera1-to-camerax"];
  const resolved = resolveSkillProviderClaims(loaded, context);

  assert.deepEqual(resolved.claims, []);
  assert.equal(resolved.quarantined[0].reason, "provider_source_hash_missing");
});

test("registered adapter resolves a new provider without provider-specific code", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("new-provider", {
      compatibility: {
        registry: 1,
        profiles: ["node"],
        hosts: ["codex"],
        source_kinds: ["project"],
      },
      config: { source_kinds: ["project"] },
    }),
  ]));
  const resolved = resolveSkillProviderClaims(loaded, {
    profile: "node",
    host: "codex",
    catalogs: {},
    candidates: [candidate("new-skill", "project", "b".repeat(64))],
  });

  assert.equal(resolved.claims[0].provider_id, "new-provider");
});

test("provider allowlist and minimum trust fail closed with structured diagnostics", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const denied = registry([provider("organization")]);
  denied.policy.allowlist = [];
  const untrusted = registry([provider("organization")]);
  untrusted.policy.minimum_trust = "official";
  const context = {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates: [candidate("demo", "bundled", "c".repeat(64))],
  };

  assert.equal(
    resolveSkillProviderClaims(normalizeSkillProviderRegistry(denied), context).quarantined[0].reason,
    "provider_not_allowed",
  );
  assert.equal(
    resolveSkillProviderClaims(normalizeSkillProviderRegistry(untrusted), context).quarantined[0].reason,
    "provider_trust_failure",
  );
});
test("configured preferred provider fails closed when it is ineligible", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = registry([
    provider("provider-a"),
    provider("provider-b", { trust_tier: "user", ownership: "user" }),
  ]);
  value.policy.minimum_trust = "organization";
  value.policy.preferred_providers.demo = "provider-b";
  const resolved = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(value),
    {
      profile: "android",
      host: "codex",
      catalogs: {},
      candidates: [candidate("demo", "bundled", "c".repeat(64))],
    },
  );

  assert.deepEqual(resolved.claims, []);
  assert.ok(resolved.quarantined.some(
    (entry) => entry.reason === "provider_preferred_unavailable",
  ));
});

test("alias preferred provider fails closed when it is ineligible", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = registry([
    provider("provider-a"),
    provider("provider-b", { trust_tier: "user", ownership: "user" }),
  ]);
  value.policy.minimum_trust = "organization";
  value.policy.preferred_providers.alias = "provider-b";
  const resolved = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(value),
    {
      profile: "android",
      host: "codex",
      catalogs: {},
      candidates: [candidate("demo", "bundled", "c".repeat(64), { aliases: ["alias"] })],
    },
  );

  assert.deepEqual(resolved.claims, []);
  assert.ok(resolved.quarantined.some(
    (entry) => entry.reason === "provider_preferred_unavailable",
  ));
});
test("concrete and alias preferred providers must agree", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = registry([provider("provider-a"), provider("provider-b")]);
  value.policy.preferred_providers.demo = "provider-a";
  value.policy.preferred_providers.alias = "provider-b";

  const resolved = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(value),
    {
      profile: "android",
      host: "codex",
      catalogs: {},
      candidates: [candidate("demo", "bundled", "c".repeat(64), { aliases: ["alias"] })],
    },
  );

  assert.deepEqual(resolved.claims, []);
  assert.ok(resolved.quarantined.some(
    (entry) => entry.reason === "provider_preferred_conflict",
  ));
});


test("preferred provider policy treats constructor and __proto__ as ordinary keys", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = registry([provider("provider-a"), provider("provider-b")]);
  value.policy.preferred_providers = JSON.parse(
    '{"constructor":"provider-a","__proto__":"provider-b"}',
  );
  const loaded = normalizeSkillProviderRegistry(value);
  const context = {
    profile: "android",
    host: "codex",
    catalogs: {},
  };

  const constructorResult = resolveSkillProviderClaims(loaded, {
    ...context,
    candidates: [candidate("constructor", "bundled", "c".repeat(64))],
  });
  const protoResult = resolveSkillProviderClaims(loaded, {
    ...context,
    candidates: [candidate("__proto__", "bundled", "d".repeat(64))],
  });

  assert.equal(constructorResult.claims[0].provider_id, "provider-a");
  assert.equal(protoResult.claims[0].provider_id, "provider-b");
});

test("unsafe candidate identifiers are sanitized in diagnostics", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([provider("provider-a")]));
  const resolved = resolveSkillProviderClaims(loaded, {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates: [{
      concrete_id: "unsafe\n\ud800",
      aliases: [],
      source_kind: "bundled",
      source_hash: "c".repeat(64),
    }],
  });

  assert.deepEqual(resolved.claims, []);
  assert.equal(resolved.quarantined[0].concrete_id, null);
  assert.equal(resolved.quarantined[0].metadata_path, "skill-index#skill:unknown");
  assert.doesNotMatch(JSON.stringify(resolved.quarantined[0]), /\\ud800/);
});

test("duplicate concrete id is ambiguous unless policy selects an eligible preferred provider", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const providers = [
    provider("provider-a"),
    provider("provider-b"),
  ];
  const context = {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates: [candidate("demo", "bundled", "d".repeat(64))],
  };
  const ambiguous = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(registry(providers)),
    context,
  );
  const preferredValue = registry(providers);
  preferredValue.policy.preferred_providers.demo = "provider-b";
  const preferred = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(preferredValue),
    context,
  );

  assert.deepEqual(ambiguous.claims, []);
  assert.equal(ambiguous.quarantined[0].reason, "provider_claim_ambiguous");
  assert.equal(preferred.claims[0].provider_id, "provider-b");
});

test("real profile catalog provider conflict fails closed without preferred policy", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = JSON.parse(fs.readFileSync(
    path.join(process.cwd(), "skills", "provider-registry.json"),
    "utf8",
  ));
  const mirror = structuredClone(
    value.providers.find((entry) => entry.id === "android-official"),
  );
  mirror.id = "android-official-mirror";
  value.providers.push(mirror);
  value.policy.allowlist.push(mirror.id);
  const context = catalogContext([
    candidate("edge-to-edge", "host-bootstrap", ANDROID_EDGE_HASH),
  ]);
  const ambiguous = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(value),
    context,
  );
  value.policy.preferred_providers["edge-to-edge"] = mirror.id;
  const preferred = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(value),
    context,
  );

  assert.deepEqual(ambiguous.claims, []);
  assert.ok(ambiguous.quarantined.some(
    (entry) => entry.reason === "provider_claim_ambiguous"
      && entry.concrete_id === "edge-to-edge",
  ));
  assert.equal(preferred.claims[0].provider_id, mirror.id);
});
test("duplicate concrete id candidates quarantine every claim", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([provider("organization")]));
  const resolved = resolveSkillProviderClaims(loaded, {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates: [
      candidate("demo", "bundled", "d".repeat(64)),
      candidate("demo", "bundled", "e".repeat(64)),
    ],
  });

  assert.deepEqual(resolved.claims, []);
  const collisions = resolved.quarantined.filter(
    (entry) => entry.reason === "provider_concrete_id_ambiguous",
  );
  assert.equal(collisions.length, 2);
});

test("duplicate aliases quarantine every colliding claim", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([provider("organization")]));
  const resolved = resolveSkillProviderClaims(loaded, {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates: [
      candidate("first", "bundled", "e".repeat(64), { aliases: ["shared-alias"] }),
      candidate("second", "bundled", "f".repeat(64), { aliases: ["shared-alias"] }),
    ],
  });

  assert.deepEqual(resolved.claims, []);
  assert.equal(resolved.quarantined.length, 2);
  assert.ok(resolved.quarantined.every((entry) => entry.reason === "provider_alias_ambiguous"));
});

test("preferred provider resolves an alias collision without dropping concrete claims", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const providerB = provider("provider-b", {
    compatibility: {
      registry: 1,
      profiles: ["*"],
      hosts: ["*"],
      source_kinds: ["project"],
    },
    config: {
      source_kinds: ["project"],
    },
  });
  const value = registry([provider("provider-a"), providerB]);
  value.policy.preferred_providers["shared-alias"] = "provider-b";
  const resolved = resolveSkillProviderClaims(
    normalizeSkillProviderRegistry(value),
    {
      profile: "android",
      host: "codex",
      catalogs: {},
      candidates: [
        candidate("first", "bundled", "e".repeat(64), { aliases: ["shared-alias"] }),
        candidate("second", "project", "f".repeat(64), { aliases: ["shared-alias"] }),
      ],
    },
  );

  assert.deepEqual(
    resolved.claims.map((claim) => [claim.concrete_id, claim.aliases]),
    [["first", []], ["second", ["shared-alias"]]],
  );
  assert.ok(resolved.quarantined.some(
    (entry) => entry.reason === "provider_alias_not_preferred"
      && entry.concrete_id === "first",
  ));
});

test("skill metadata cannot spoof provider ownership", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([provider("organization")]));
  const resolved = resolveSkillProviderClaims(loaded, {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates: [
      candidate("demo", "bundled", "1".repeat(64), { declared_provider: "android-official" }),
    ],
  });

  assert.deepEqual(resolved.claims, []);
  assert.equal(resolved.quarantined[0].reason, "provider_spoofing");
});

test("source hash mismatch rejects the claim", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([
    profileProvider("pinned-provider", "edge-to-edge", "2".repeat(64)),
  ]));
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate("edge-to-edge", "host-bootstrap", "3".repeat(64)),
  ]));

  assert.deepEqual(resolved.claims, []);
  assert.equal(resolved.quarantined[0].reason, "provider_source_hash_mismatch");
});

test("provider index rejects a symlinked profile catalog", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-profile-auth-"));
  const profilesRoot = path.join(root, "profiles");
  const registryRoot = path.join(root, "skills");
  fs.mkdirSync(profilesRoot);
  fs.mkdirSync(registryRoot);
  const outside = path.join(root, "outside.yaml");
  fs.writeFileSync(outside, "android_skills:\n  implementation:\n    - skill: demo\n");
  fs.symlinkSync(outside, path.join(profilesRoot, "android.yaml"));
  const registryPath = path.join(registryRoot, "provider-registry.json");
  fs.writeFileSync(registryPath, JSON.stringify(registry([provider("organization")])));

  assert.throws(
    () => resolveSkillProviderIndex({
      skills: [{ name: "demo", source: "bundled", tree_hash: "3".repeat(64) }],
      activeProfiles: ["android"],
      activeHost: "codex",
      profilesRoot,
      registryPath,
      authorityRoot: root,
      compatibility: { version: 1, skills: [] },
      authenticateCandidate: indexedCandidateEvidence,
    }),
    /unsafe skill profile file/,
  );
});

test("provider index requires trusted candidate source evidence", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-candidate-auth-"));
  const registryPath = path.join(root, "provider-registry.json");
  fs.writeFileSync(registryPath, JSON.stringify(registry([provider("organization")])));
  const input = {
    skills: [{
      name: "demo",
      source: "bundled",
      source_authenticated: true,
      tree_hash: "3".repeat(64),
    }],
    activeProfiles: ["node"],
    activeHost: "codex",
    profilesRoot: root,
    registryPath,
    compatibility: { version: 1, skills: [] },
  };
  for (const authenticateCandidate of [
    undefined,
    () => ({
      ...indexedCandidateEvidence(input.skills[0]),
      source_hash: "4".repeat(64),
    }),
  ]) {
    assert.throws(
      () => resolveSkillProviderIndex({
        ...input,
        ...(authenticateCandidate ? { authenticateCandidate } : {}),
      }),
      (error) => {
        assert.equal(error.code, "skill_provider_resolution_error");
        assert.equal(error.diagnostics[0].reason, "provider_source_unauthenticated");
        return true;
      },
    );
  }
});

test("provider index resolves profile catalogs, compatibility aliases, and source ownership", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const hash = "4".repeat(64);
  const resolved = resolveSkillProviderIndex({
    skills: [
      {
        name: "edge-to-edge",
        source: "host-bootstrap",
        source_host: "codex",
        tree_hash: ANDROID_EDGE_HASH,
      },
      {
        name: "compose-state-hoisting",
        source: "host-bootstrap",
        source_host: "codex",
        tree_hash: CHRIS_STATE_HOISTING_HASH,
      },
      { name: "code-generation-discipline", source: "bundled", tree_hash: hash },
      { name: "project-demo", source: "project", tree_hash: hash },
    ],
    activeProfiles: ["android"],
    activeHost: "codex",
    profilesRoot: path.join(process.cwd(), "profiles"),
    registryPath: path.join(process.cwd(), "skills", "provider-registry.json"),
    compatibility: JSON.parse(fs.readFileSync(
      path.join(process.cwd(), "skills", "compatibility.json"),
      "utf8",
    )),
    authenticateCandidate: indexedCandidateEvidence,
  });

  assert.deepEqual(
    resolved.claims.map((claim) => [claim.concrete_id, claim.provider_id]),
    [
      ["code-generation-discipline", "organization"],
      ["compose-state-hoisting", "chris-banes"],
      ["edge-to-edge", "android-official"],
      ["project-demo", "project-local"],
    ],
  );
  assert.equal(
    resolved.claims.find((claim) => claim.concrete_id === "edge-to-edge")
      .provenance_revision,
    "57ff3c7d02a53781954f9ce2df92f14b7fbb2ded",
  );
  assert.equal(resolved.claims.find(
    (claim) => claim.concrete_id === "code-generation-discipline",
  ).aliases.includes("code-generation"), true);
  assert.match(resolved.fingerprint, /^[0-9a-f]{64}$/);
  assert.deepEqual(resolved.quarantined, []);
  assert.equal(
    resolved.claims.find((claim) => claim.concrete_id === "project-demo").source_locator,
    "project://skills/project-demo/SKILL.md",
  );
});
test("resolved catalog fingerprint includes claims and quarantine diagnostics", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([provider("organization")]));
  const resolve = (candidates) => resolveSkillProviderClaims(loaded, {
    profile: "android",
    host: "codex",
    catalogs: {},
    candidates,
  });
  const first = resolve([candidate("demo", "bundled", "a".repeat(64))]);
  const changedClaim = resolve([candidate("demo", "bundled", "b".repeat(64))]);
  const changedDiagnostic = resolve([
    candidate("demo", "bundled", "a".repeat(64)),
    { concrete_id: "broken" },
  ]);

  assert.equal(first.claims[0].registry_fingerprint, first.registry_fingerprint);
  assert.notEqual(first.registry_fingerprint, changedClaim.registry_fingerprint);
  assert.notEqual(first.registry_fingerprint, changedDiagnostic.registry_fingerprint);
});

test("resolved catalog fingerprint is independent of candidate ordering", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([provider("organization")]));
  const context = {
    profile: "android",
    host: "codex",
    catalogs: {},
  };
  const candidates = [
    candidate("alpha", "bundled", "a".repeat(64)),
    candidate("beta", "bundled", "b".repeat(64)),
  ];
  const forward = resolveSkillProviderClaims(loaded, { ...context, candidates });
  const reverse = resolveSkillProviderClaims(loaded, {
    ...context,
    candidates: [...candidates].reverse(),
  });

  assert.equal(forward.registry_fingerprint, reverse.registry_fingerprint);
  assert.deepEqual(forward.claims, reverse.claims);
  assert.deepEqual(forward.quarantined, reverse.quarantined);
});

test("invalid unrelated provider metadata is quarantined while valid claims remain usable", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-index-"));
  const value = registry([
    provider("organization"),
    provider("invalid-provider", { adapter: "missing-adapter" }),
  ]);
  const registryPath = path.join(root, "provider-registry.json");
  fs.writeFileSync(registryPath, JSON.stringify(value));

  const resolved = resolveSkillProviderIndex({
    skills: [{ name: "demo", source: "bundled", tree_hash: "5".repeat(64) }],
    activeProfiles: ["android"],
    activeHost: "codex",
    profilesRoot: root,
    registryPath,
    compatibility: { version: 1, skills: [] },
    authenticateCandidate: indexedCandidateEvidence,
  });

  assert.equal(resolved.claims[0].provider_id, "organization");
  assert.equal(resolved.quarantined[0].provider_id, "invalid-provider");
  assert.equal(resolved.quarantined[0].reason, "provider_metadata_invalid");
});

test("malformed catalog provider blocks lower-priority fallback for its candidate", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = JSON.parse(fs.readFileSync(
    path.join(process.cwd(), "skills", "provider-registry.json"),
    "utf8",
  ));
  value.providers.find((entry) => entry.id === "android-official").ignored = true;
  const loaded = normalizeSkillProviderRegistry(value);
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate("edge-to-edge", "host-bootstrap", ANDROID_EDGE_HASH),
  ]));

  assert.deepEqual(resolved.claims, []);
  assert.ok(resolved.quarantined.some(
    (entry) => entry.reason === "provider_metadata_invalid"
      && entry.concrete_id === "edge-to-edge",
  ));
});

test("blocking provider diagnostics surface as a structured resolution error", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-index-"));
  const value = registry([
    profileProvider("pinned-provider", "demo", "6".repeat(64)),
  ]);
  const registryPath = path.join(root, "provider-registry.json");
  const profilePath = path.join(root, "android.yaml");
  fs.writeFileSync(registryPath, JSON.stringify(value));
  fs.writeFileSync(
    profilePath,
    [
      "android_skills:",
      "  source: https://github.com/android/skills",
      "  install_policy: never",
      "  active_host_only: true",
      "  hosts:",
      "    codex: ~/.codex/skills/<skill>/SKILL.md",
      "  implementation:",
      "    - skill: demo",
      "  review:",
      "",
    ].join("\n"),
  );
  assert.throws(
    () => resolveSkillProviderIndex({
      skills: [{
        name: "demo",
        source: "host-bootstrap",
        source_host: "codex",
        tree_hash: "7".repeat(64),
      }],
      activeProfiles: ["android"],
      activeHost: "codex",
      profilesRoot: root,
      registryPath,
      compatibility: { version: 1, skills: [] },
      authenticateCandidate: indexedCandidateEvidence,
    }),
    (error) => {
      assert.equal(error.code, "skill_provider_resolution_error");
      assert.equal(error.diagnostics[0].reason, "provider_source_hash_mismatch");
      return true;
    },
  );
});

test("provider index is host-neutral for Codex Claude and OMP", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const input = {
    activeProfiles: ["android"],
    profilesRoot: path.join(process.cwd(), "profiles"),
    registryPath: path.join(process.cwd(), "skills", "provider-registry.json"),
    compatibility: { version: 1, skills: [] },
    authenticateCandidate: indexedCandidateEvidence,
  };
  const results = ["codex", "claude", "omp"].map((activeHost) => (
    resolveSkillProviderIndex({
      ...input,
      activeHost,
      skills: [
        { name: "demo", source: "project", tree_hash: "8".repeat(64) },
        {
          name: "edge-to-edge",
          source: "host-bootstrap",
          source_host: activeHost,
          tree_hash: ANDROID_EDGE_HASH,
        },
      ],
    })
  ));
  const normalized = results.map((result) => ({
    ...result,
    claims: result.claims.map((claim) => (
      claim.source_host === null
        ? claim
        : {
          ...claim,
          source_host: "active-host",
          source_locator: `host://active-host/skills/${claim.concrete_id}`,
        }
    )),
  }));

  assert.deepEqual(normalized[0], normalized[1]);
  assert.deepEqual(normalized[1], normalized[2]);
});

test("profile catalog maps treat prototype names as ordinary keys", () => {
  const resolveSkillProviderIndex = requireSelectionApi("resolveSkillProviderIndex");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-profile-prototype-"));
  const profilesRoot = path.join(root, "profiles");
  const registryPath = path.join(root, "provider-registry.json");
  const hash = "9".repeat(64);
  fs.mkdirSync(profilesRoot);
  fs.writeFileSync(path.join(profilesRoot, "__proto__.yaml"), [
    "__proto__:",
    "  source: https://example.test/catalog",
    "  install_policy: never",
    "  active_host_only: true",
    "  hosts:",
    "    __proto__: ~/.codex/skills/<skill>/SKILL.md",
    "  implementation:",
    "    - skill: demo",
    "  review:",
    "",
  ].join("\n"));
  fs.writeFileSync(registryPath, JSON.stringify(registry([
    provider("profile-provider", {
      adapter: "profile-catalog",
      provenance: { source: "https://example.test/catalog" },
      compatibility: {
        registry: 1,
        profiles: ["__proto__"],
        hosts: ["__proto__"],
        source_kinds: ["host-bootstrap"],
      },
      config: {
        profile: "__proto__",
        catalog: "__proto__",
        membership: ["implementation", "review"],
        source_binding: {
          mode: "active-host",
          require_install_policy: "never",
          require_active_host_only: true,
        },
        content_hash: {
          mode: "pinned",
          hashes: { demo: hash },
        },
      },
    }),
  ])));

  try {
    const resolved = resolveSkillProviderIndex({
      skills: [{
        name: "demo",
        source: "host-bootstrap",
        source_host: "__proto__",
        tree_hash: hash,
      }],
      activeProfiles: ["__proto__"],
      activeHost: "__proto__",
      profilesRoot,
      registryPath,
      compatibility: { version: 1, skills: [] },
      authenticateCandidate: indexedCandidateEvidence,
    });
    assert.equal(resolved.claims[0].provider_id, "profile-provider");
    assert.equal(Object.hasOwn(Object.prototype, "source"), false);
  } finally {
    delete Object.prototype.source;
    delete Object.prototype.install_policy;
    delete Object.prototype.active_host_only;
  }
});

test("malformed profile provider blocks only catalog members", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = JSON.parse(fs.readFileSync(
    path.join(process.cwd(), "skills", "provider-registry.json"),
    "utf8",
  ));
  value.providers.find((entry) => entry.id === "android-official").ignored = true;
  const loaded = normalizeSkillProviderRegistry(value);
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate(
      "compose-state-hoisting",
      "host-bootstrap",
      CHRIS_STATE_HOISTING_HASH,
    ),
  ]));

  assert.equal(resolved.claims[0].provider_id, "chris-banes");
  assert.equal(
    resolved.quarantined.some(
      (entry) => entry.concrete_id === "compose-state-hoisting"
        && entry.provider_id === "android-official",
    ),
    false,
  );
});
test("profile provider provenance mismatch does not block unrelated catalog members", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = JSON.parse(fs.readFileSync(
    path.join(process.cwd(), "skills", "provider-registry.json"),
    "utf8",
  ));
  value.providers.find(
    (entry) => entry.id === "android-official",
  ).provenance.source = "https://evil.test";
  const loaded = normalizeSkillProviderRegistry(value);
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate(
      "compose-state-hoisting",
      "host-bootstrap",
      CHRIS_STATE_HOISTING_HASH,
    ),
  ]));

  assert.equal(resolved.claims[0].provider_id, "chris-banes");
  assert.equal(
    resolved.quarantined.some(
      (entry) => entry.concrete_id === "compose-state-hoisting"
        && entry.provider_id === "android-official",
    ),
    false,
  );
});

test("malformed provider provenance blocks catalog member fallback", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const value = JSON.parse(fs.readFileSync(
    path.join(process.cwd(), "skills", "provider-registry.json"),
    "utf8",
  ));
  value.providers.find(
    (entry) => entry.id === "android-official",
  ).provenance.source += "\n";
  const loaded = normalizeSkillProviderRegistry(value);
  const resolved = resolveSkillProviderClaims(loaded, catalogContext([
    candidate("edge-to-edge", "host-bootstrap", ANDROID_EDGE_HASH),
  ]));

  assert.deepEqual(resolved.claims, []);
  assert.equal(
    resolved.quarantined.some(
      (entry) => entry.concrete_id === "edge-to-edge"
        && entry.provider_id === "android-official"
        && entry.reason === "provider_metadata_invalid",
    ),
    true,
  );
});


test("preferred provider policy rejects casefold collisions", () => {
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const value = registry([provider("provider-a"), provider("provider-b")]);
  value.policy.preferred_providers = JSON.parse(
    '{"Demo":"provider-a","demo":"provider-b"}',
  );

  assert.throws(
    () => normalizeSkillProviderRegistry(value),
    /duplicate preferred skill: demo/,
  );
});

test("custom adapter evidence must match authenticated candidate identity and mode", () => {
  const createSkillProviderAdapterRegistry = requireApi("createSkillProviderAdapterRegistry");
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  for (const evidenceOverrides of [
    { source: "https://spoofed.test" },
    { source_locator: "project://spoofed" },
    { content_hash_mode: "observed" },
  ]) {
    const adapterRegistry = createSkillProviderAdapterRegistry([{
      id: "untrusted-evidence",
      version: "1.0.0",
      priority: 300,
      content_hash_mode: () => "verified",
      evidence_source: (_providerMetadata, providerCandidate) => (
        providerCandidate.source_locator
      ),
      normalize_config: () => ({}),
      match: (_providerMetadata, providerCandidate) => ({
        matched: true,
        evidence: testEvidence(providerCandidate, evidenceOverrides),
      }),
    }]);
    const loaded = normalizeSkillProviderRegistry(registry([
      provider("custom-provider", {
        adapter: "untrusted-evidence",
        config: {},
      }),
    ]), { adapterRegistry });
    const resolved = resolveSkillProviderClaims(loaded, {
      profile: "node",
      host: "codex",
      catalogs: {},
      candidates: [candidate("demo", "bundled", "a".repeat(64))],
    }, { adapterRegistry });

    assert.deepEqual(resolved.claims, []);
    assert.equal(resolved.quarantined[0].reason, "provider_adapter_failure");
  }
});

test("candidate-root provider fingerprint and revision projection are host-neutral", () => {
  const canonicalizeHostNeutralSkillProviderClaim = requireApi(
    "canonicalizeHostNeutralSkillProviderClaim",
  );
  const normalizeSkillProviderRegistry = requireApi("normalizeSkillProviderRegistry");
  const resolveSkillProviderClaims = requireApi("resolveSkillProviderClaims");
  const loaded = normalizeSkillProviderRegistry(registry([
    provider("candidate-root", {
      compatibility: {
        registry: 1,
        profiles: ["node"],
        hosts: ["*"],
        source_kinds: ["host-bootstrap"],
      },
      config: {
        source_kinds: ["host-bootstrap"],
        source_mode: "candidate-root",
      },
    }),
  ]));
  const claims = ["codex", "claude", "omp"].map((host) => (
    resolveSkillProviderClaims(loaded, {
      profile: "node",
      host,
      catalogs: {},
      candidates: [candidate(
        "demo",
        "host-bootstrap",
        "a".repeat(64),
        {
          source_host: host,
          source_locator: `host://${host}/skills/demo`,
        },
      )],
    }).claims[0]
  ));
  const fingerprints = new Set(claims.map((claim) => claim.registry_fingerprint));
  const canonicalClaims = claims.map(canonicalizeHostNeutralSkillProviderClaim);
  const revisions = new Set(canonicalClaims.map(({
    registry_fingerprint: _registryFingerprint,
    ...claim
  }) => (
    crypto.createHash("sha256").update(JSON.stringify({ claims: [claim] })).digest("hex")
  )));

  assert.equal(fingerprints.size, 1);
  assert.equal(canonicalClaims[0].source, "host://active-host/skills/demo");
  assert.deepEqual(canonicalClaims[0], canonicalClaims[1]);
  assert.deepEqual(canonicalClaims[1], canonicalClaims[2]);
  assert.equal(revisions.size, 1);
});
