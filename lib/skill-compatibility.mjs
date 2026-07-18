import {
  compareCodePoints,
  portableSkillCasefold,
} from "./portable-skill-name.mjs";

const SKILL_COMPATIBILITY_STATUSES = new Set(["active", "renamed", "deprecated", "removed"]);

export class SkillResolutionError extends Error {
  constructor(diagnostics) {
    super(`skill_resolution_error: ${JSON.stringify(diagnostics)}`);
    this.name = "SkillResolutionError";
    this.code = "skill_resolution_error";
    this.diagnostics = diagnostics;
  }
}

export function normalizeSkillCompatibility(value) {
  if (value === undefined || value === null) return { version: 1, skills: [] };
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error("compatibility projection must be an object");
  }
  const version = Object.hasOwn(value, "version") ? value.version : 1;
  if (!Number.isInteger(version) || version !== 1) {
    throw new Error("unsupported compatibility version");
  }
  const rawSkills = Object.hasOwn(value, "skills") ? value.skills : [];
  if (!Array.isArray(rawSkills)) throw new Error("compatibility skills must be a list");

  const byCanonical = new Map();
  const references = new Map();
  const skills = rawSkills.map((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new Error("compatibility skill entry must be an object");
    }
    const canonical = compatibilityName(raw.canonical, "canonical skill");
    if (byCanonical.has(canonical)) {
      throw new Error(`duplicate compatibility canonical: ${canonical}`);
    }
    const rawStatus = Object.hasOwn(raw, "status") ? raw.status : "active";
    if (typeof rawStatus !== "string") {
      throw new Error(`invalid compatibility status: ${canonical}`);
    }
    const status = rawStatus.trim().toLowerCase();
    if (!SKILL_COMPATIBILITY_STATUSES.has(status)) {
      throw new Error(`invalid compatibility status: ${canonical}`);
    }
    const capabilities = compatibilityNameList(raw.capabilities, "capability");
    const aliases = compatibilityNameList(raw.aliases, "alias");
    const renamedFrom = compatibilityNameList(raw.renamed_from, "renamed reference");
    const replacements = raw.replaced_by === undefined || raw.replaced_by === null || raw.replaced_by === ""
      ? []
      : compatibilityNameList(
        Array.isArray(raw.replaced_by) ? raw.replaced_by : [raw.replaced_by],
        "replacement",
      );
    if (status === "active" && replacements.length > 0) {
      throw new Error(`active compatibility skill has replacement: ${canonical}`);
    }
    const record = {
      canonical,
      capabilities,
      aliases,
      renamed_from: renamedFrom,
      status,
      replaced_by: replacements,
    };
    byCanonical.set(canonical, record);
    for (const reference of [canonical, ...aliases, ...renamedFrom]) {
      const owner = references.get(reference);
      if (owner !== undefined) {
        throw new Error(`duplicate compatibility reference: ${reference} (${owner}, ${canonical})`);
      }
      references.set(reference, canonical);
    }
    return record;
  });
  canonicalizeCompatibilityReplacements(byCanonical, references);
  validateCompatibilityReplacementCycles(byCanonical);
  skills.sort((left, right) => compareCodePoints(left.canonical, right.canonical));
  return { version: 1, skills };
}

export function createSkillCompatibilityCatalog(value) {
  const projection = normalizeSkillCompatibility(value);
  const byCanonical = new Map(projection.skills.map((record) => [record.canonical, record]));
  const references = new Map(
    projection.skills.flatMap((record) => (
      [record.canonical, ...record.aliases, ...record.renamed_from]
        .map((reference) => [reference, record.canonical])
    )),
  );
  return {
    projection,
    validateConcreteIds(concreteNames) {
      for (const value of logicalNameValues(concreteNames)) {
        const concrete = compatibilityName(value, "concrete skill");
        const owner = references.get(concrete) ?? concrete;
        if (owner !== concrete) {
          throw new Error(`compatibility reference shadows concrete skill: ${concrete}`);
        }
      }
    },
    resolve(requested) {
      const requestedName = compatibilityName(requested, "requested skill");
      let canonical = references.get(requestedName) ?? requestedName;
      const visited = new Set();
      while (true) {
        if (visited.has(canonical)) throw new Error(`replacement cycle: ${canonical}`);
        visited.add(canonical);
        const record = byCanonical.get(canonical);
        if (!record) {
          return { resolved: true, requested: requestedName, canonical, capabilities: [], reason: null };
        }
        if (record.status === "active") {
          return {
            resolved: true,
            requested: requestedName,
            canonical,
            capabilities: [...record.capabilities],
            reason: null,
          };
        }
        if (record.replaced_by.length === 0) {
          return compatibilityFailure(
            requestedName,
            canonical,
            record.capabilities,
            `${record.status}_without_replacement`,
          );
        }
        if (record.replaced_by.length !== 1) {
          return compatibilityFailure(
            requestedName,
            canonical,
            record.capabilities,
            "multiple_replacements_unsupported",
          );
        }
        [canonical] = record.replaced_by;
      }
    },
  };
}

export function resolveSkillCompatibility(value, requested) {
  return createSkillCompatibilityCatalog(value).resolve(requested);
}

export function canonicalCompatibilityReferences(value, values) {
  const catalog = createSkillCompatibilityCatalog(value);
  const result = new Set();
  const diagnostics = [];
  for (const item of logicalNameValues(values)) {
    const resolved = catalog.resolve(item);
    if (resolved.resolved) {
      result.add(resolved.canonical);
    } else {
      diagnostics.push(compatibilityDiagnostic(resolved));
    }
  }
  if (diagnostics.length > 0) throw new SkillResolutionError(diagnostics);
  return result;
}

export function canonicalizeSkillCompatibilitySelection(value, values) {
  return canonicalCompatibilityReferences(value, values);
}

export function validateConcreteSkillCompatibility(value, concreteNames) {
  const catalog = createSkillCompatibilityCatalog(value);
  catalog.validateConcreteIds(concreteNames);
  return catalog.projection;
}

export function compatibilityDiagnostic(resolution) {
  return {
    reason: resolution.reason,
    requested: resolution.requested,
    canonical: resolution.canonical,
    capabilities: [...resolution.capabilities],
    repairable: false,
  };
}

function compatibilityFailure(requested, canonical, capabilities, reason) {
  return {
    resolved: false,
    requested,
    canonical,
    capabilities: [...capabilities],
    reason,
  };
}

function compatibilityName(value, label) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`invalid ${label}`);
  }
  try {
    return portableSkillCasefold(value);
  } catch (error) {
    throw new Error(`invalid ${label}`, { cause: error });
  }
}

function compatibilityNameList(value, label) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error(`${label} list is invalid`);
  const names = value.map((item) => compatibilityName(item, label));
  if (new Set(names).size !== names.length) {
    throw new Error(`duplicate compatibility ${label}`);
  }
  return names.sort(compareCodePoints);
}

function logicalNameValues(values) {
  if (Array.isArray(values) || values instanceof Set) return values;
  return [];
}

function canonicalizeCompatibilityReplacements(byCanonical, references) {
  for (const record of byCanonical.values()) {
    const replacements = record.replaced_by
      .map((replacement) => references.get(replacement) ?? replacement)
      .sort(compareCodePoints);
    if (new Set(replacements).size !== replacements.length) {
      throw new Error("duplicate compatibility replacement");
    }
    record.replaced_by = replacements;
  }
}

function validateCompatibilityReplacementCycles(byCanonical) {
  const visit = (name, pathNames) => {
    if (pathNames.includes(name)) {
      throw new Error(`replacement cycle: ${[...pathNames, name].join(" -> ")}`);
    }
    const record = byCanonical.get(name);
    if (!record) return;
    for (const replacement of record.replaced_by) {
      visit(replacement, [...pathNames, name]);
    }
  };
  for (const canonical of byCanonical.keys()) visit(canonical, []);
}
