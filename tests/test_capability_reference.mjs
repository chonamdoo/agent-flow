import assert from "node:assert/strict";
import test from "node:test";

import { resolveProfileCapabilities } from "../lib/skill-selection.mjs";
import { createSkillCompatibilityCatalog } from "../lib/skill-compatibility.mjs";

const CATALOG = {
  version: 1,
  skills: [
    { canonical: "compose-state-authoring", capabilities: ["compose.state"] },
    { canonical: "compose-state-holder-ui-split", capabilities: ["compose.state"] },
    { canonical: "clean-architecture-core", capabilities: ["architecture.clean.boundary"] },
    { canonical: "python-development-guide", capabilities: ["lang.python"] },
    {
      canonical: "old-guide",
      status: "deprecated",
      capabilities: ["lang.python"],
      replaced_by: ["python-development-guide"],
    },
  ],
};

test("single-provider capability resolves to the concrete skill", () => {
  const { resolved, diagnostics } = resolveProfileCapabilities(CATALOG, [
    "architecture.clean.boundary",
  ]);
  assert.deepEqual(resolved, ["clean-architecture-core"]);
  assert.deepEqual(diagnostics, []);
});

test("deprecated provider is excluded so lang.python resolves uniquely", () => {
  const { resolved, diagnostics } = resolveProfileCapabilities(CATALOG, ["lang.python"]);
  assert.deepEqual(resolved, ["python-development-guide"]);
  assert.deepEqual(diagnostics, []);
});

test("capability with no active provider is capability_unresolved", () => {
  const { resolved, diagnostics } = resolveProfileCapabilities(CATALOG, ["missing.cap"]);
  assert.deepEqual(resolved, []);
  assert.equal(diagnostics.length, 1);
  assert.equal(diagnostics[0].capability, "missing.cap");
  assert.equal(diagnostics[0].reason, "capability_unresolved");
});

test("capability with multiple active providers is stack_ambiguity", () => {
  const { resolved, diagnostics } = resolveProfileCapabilities(CATALOG, ["compose.state"]);
  assert.deepEqual(resolved, []);
  assert.equal(diagnostics.length, 1);
  assert.equal(diagnostics[0].reason, "stack_ambiguity");
  assert.deepEqual(diagnostics[0].providers, [
    "compose-state-authoring",
    "compose-state-holder-ui-split",
  ]);
});

test("duplicate capability references dedup to one concrete skill", () => {
  const { resolved } = resolveProfileCapabilities(CATALOG, [
    "architecture.clean.boundary",
    "architecture.clean.boundary",
  ]);
  assert.deepEqual(resolved, ["clean-architecture-core"]);
});

test("no capabilities returns empty with no diagnostics (backward compatible)", () => {
  const { resolved, diagnostics } = resolveProfileCapabilities(CATALOG, []);
  assert.deepEqual(resolved, []);
  assert.deepEqual(diagnostics, []);
});

test("missing catalog leaves every capability unresolved", () => {
  const { resolved, diagnostics } = resolveProfileCapabilities(null, ["lang.python"]);
  assert.deepEqual(resolved, []);
  assert.equal(diagnostics[0].reason, "capability_unresolved");
});

test("resolveCapability accepts a Set as available (no TypeError)", () => {
  const catalog = createSkillCompatibilityCatalog(CATALOG);
  // callers may pass a LogicalNameSet/Set; must resolve like an array does
  const resolution = catalog.resolveCapability(
    "lang.python",
    new Set(["python-development-guide"]),
  );
  assert.equal(resolution.resolved, true);
  assert.equal(resolution.canonical, "python-development-guide");
  assert.equal(resolution.reason, null);
});
