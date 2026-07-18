import {
  canonicalCompatibilityReferences,
  SkillResolutionError,
} from "./skill-compatibility.mjs";

export function evaluatePhaseContract(phase, artifact) {
  const requiredSkills = Array.isArray(phase?.required_skills) ? phase.required_skills : [];
  const requiredRequirements = Array.isArray(phase?.requirements) ? phase.requirements : [];
  if (requiredSkills.length === 0 && requiredRequirements.length === 0) {
    return { valid: true, issues: [], route: null };
  }
  const payload = phaseContractPayload(artifact);
  if (!payload) {
    return { valid: false, issues: ["phase-contract payload is invalid"], route: null };
  }
  const applied = payload.applied_skills;
  const requirements = payload.requirements;
  if (
    !Array.isArray(applied)
    || applied.some((name) => typeof name !== "string" || !name)
    || !requirements
    || typeof requirements !== "object"
    || Array.isArray(requirements)
  ) {
    return { valid: false, issues: ["phase-contract payload is invalid"], route: null };
  }
  let requiredSet;
  let appliedSet;
  try {
    requiredSet = canonicalCompatibilityReferences(phase?.skill_compatibility, requiredSkills);
    appliedSet = canonicalCompatibilityReferences(phase?.skill_compatibility, applied);
  } catch (error) {
    const issue = error instanceof SkillResolutionError
      ? error.message
      : "phase-contract skill compatibility is invalid";
    return { valid: false, issues: [issue], route: null };
  }
  const missingSkills = [...requiredSet]
    .filter((name) => !appliedSet.has(name))
    .sort(compareCodePoints);
  const missingRequirements = requiredRequirements.filter((name) => !(name in requirements));
  const invalidRequirements = requiredRequirements.filter(
    (name) => name in requirements && !["pass", "fail"].includes(requirements[name]),
  );
  const issues = [];
  if (missingSkills.length > 0) {
    issues.push(`phase-contract missing required skills: ${missingSkills.join(", ")}`);
  }
  if (missingRequirements.length > 0) {
    issues.push(`phase-contract missing requirements: ${missingRequirements.join(", ")}`);
  }
  if (invalidRequirements.length > 0) {
    issues.push(`phase-contract invalid requirement status: ${invalidRequirements.join(", ")}`);
  }
  if (issues.length > 0) {
    return { valid: false, issues, route: null };
  }
  const route = requiredRequirements.some((name) => requirements[name] === "fail")
    ? "failure"
    : "success";
  return { valid: true, issues: [], route };
}

export function evaluateDeclaredArtifacts(phase, records, phaseEnteredAt) {
  const declared = Array.isArray(phase?.artifacts) ? phase.artifacts.slice(1) : [];
  if (declared.length === 0) return [];
  const byPath = new Map(
    (Array.isArray(records) ? records : [])
      .filter((record) => record && typeof record.path === "string")
      .map((record) => [record.path, record]),
  );
  const enteredAt = Date.parse(String(phaseEnteredAt ?? ""));
  const issues = [];
  for (const relative of declared) {
    const record = byPath.get(relative);
    if (!record?.exists || !record?.is_file) {
      issues.push(`missing declared artifact ${relative}`);
      continue;
    }
    if (
      Number.isFinite(enteredAt)
      && Number.isFinite(record.mtime_ms)
      && record.mtime_ms < enteredAt
    ) {
      issues.push(`stale declared artifact ${relative}`);
    }
  }
  return issues;
}

function phaseContractPayload(artifact) {
  const lines = String(artifact)
    .split(/\r?\n/)
    .filter((line) => line.startsWith("phase-contract:"))
    .map((line) => line.slice("phase-contract:".length).trim());
  if (lines.length !== 1 || !lines[0]) return null;
  try {
    const payload = JSON.parse(lines[0]);
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : null;
  } catch {
    return null;
  }
}

function compareCodePoints(left, right) {
  const a = Array.from(String(left), (character) => character.codePointAt(0));
  const b = Array.from(String(right), (character) => character.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}
