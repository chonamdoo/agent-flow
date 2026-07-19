import crypto from "node:crypto";
import {
  compareCodePoints,
  isPortableSkillName,
  portableSkillCasefold,
} from "./portable-skill-name.mjs";

const TRUST_TIERS = new Map([
  ["user", 1],
  ["project", 2],
  ["organization", 3],
  ["official", 4],
]);
const OWNERSHIP_TYPES = new Set(["user", "project", "organization", "upstream"]);
const SOURCE_KINDS = new Set([
  "bundled",
  "host-bootstrap",
  "local",
  "project",
  "project-snapshot",
  "shared",
]);
const SEMANTIC_VERSION = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const SHA256_DIGEST = /^[0-9a-f]{64}$/;
const CANDIDATE_PROVENANCE_ROOTS = new Map([
  ["bundled", "project://skills"],
  ["host-bootstrap", "host://bootstrap"],
  ["local", "project://.agent-flow/local-skills"],
  ["project", "project://skills"],
  ["project-snapshot", "project://.agent-flow/skills"],
  ["shared", "user://.agents/skills"],
]);
const BUILT_IN_ADAPTERS = [
  {
    id: "profile-catalog",
    version: "2.0.0",
    priority: 200,
    normalize_config: normalizeProfileCatalogConfig,
    content_hash_mode: profileCatalogContentHashMode,
    evidence_source: profileCatalogEvidenceSource,
    match: matchProfileCatalog,
  },
  {
    id: "source-kind",
    version: "2.0.0",
    priority: 100,
    normalize_config: normalizeSourceKindConfig,
    content_hash_mode: verifiedContentHashMode,
    evidence_source: sourceKindEvidenceSource,
    match: matchSourceKind,
  },
];

export class SkillProviderResolutionError extends Error {
  constructor(diagnostics) {
    super(`skill_provider_resolution_error: ${JSON.stringify(diagnostics)}`);
    this.name = "SkillProviderResolutionError";
    this.code = "skill_provider_resolution_error";
    this.diagnostics = diagnostics;
  }
}

export function createSkillProviderAdapterRegistry(additionalAdapters = []) {
  if (!Array.isArray(additionalAdapters)) {
    throw new Error("skill provider adapters must be a list");
  }
  const definitions = new Map();
  for (const raw of [...BUILT_IN_ADAPTERS, ...additionalAdapters]) {
    const definition = normalizeAdapterDefinition(raw);
    if (definitions.has(definition.id)) {
      throw new Error(`duplicate skill provider adapter: ${definition.id}`);
    }
    definitions.set(definition.id, definition);
  }
  const contracts = [...definitions.values()]
    .map(({ id, version, priority }) => ({
      id,
      version,
      priority,
    }))
    .sort((left, right) => compareCodePoints(left.id, right.id));
  const fingerprint = crypto.createHash("sha256")
    .update(canonicalJson(contracts))
    .digest("hex");
  return Object.freeze({
    fingerprint,
    ids: Object.freeze(contracts.map(({ id }) => id)),
    get(id) {
      return definitions.get(id) ?? null;
    },
  });
}

const DEFAULT_ADAPTER_REGISTRY = createSkillProviderAdapterRegistry();


export function normalizeSkillProviderRegistry(value, options = {}) {
  if (!isRecord(value)) throw new Error("invalid skill provider registry envelope");
  assertExactKeys(value, ["policy", "providers", "version"], "skill provider registry");
  if (value.version !== 1 || !Array.isArray(value.providers)) {
    throw new Error("invalid skill provider registry envelope");
  }
  const adapterRegistry = requireAdapterRegistry(options.adapterRegistry);
  const metadataPath = options.metadataPath ?? "provider-registry.json";
  const policy = normalizeProviderPolicy(value.policy);
  const providers = [];
  const quarantined = [];
  const blockingScopes = [];
  const duplicateRawIds = duplicateProviderIds(value.providers
    .map((raw) => isRecord(raw) && isPortableSkillName(raw.id)
      ? { id: portableSkillCasefold(raw.id) }
      : null)
    .filter((provider) => provider !== null));
  for (const [index, raw] of value.providers.entries()) {
    const providerMetadataPath = isRecord(raw) && typeof raw.id === "string"
      ? `${metadataPath}#provider:${raw.id}`
      : `${metadataPath}#providers[${index}]`;
    try {
      providers.push(normalizeProvider(raw, adapterRegistry, providerMetadataPath));
    } catch (error) {
      quarantined.push(providerDiagnostic(raw, error, providerMetadataPath));
      const scope = recoverProviderBlockingScope(raw, adapterRegistry, providerMetadataPath);
      if (scope !== null) blockingScopes.push(scope);
    }
  }
  const duplicates = new Set([
    ...duplicateRawIds,
    ...duplicateProviderIds(providers),
  ]);
  const activeProviders = providers
    .filter((provider) => !duplicates.has(provider.id))
    .sort((left, right) => compareCodePoints(left.id, right.id));
  for (const provider of providers) {
    if (!duplicates.has(provider.id)) continue;
    quarantined.push({
      reason: "provider_metadata_invalid",
      provider_id: provider.id,
      detail: `duplicate provider id: ${provider.id}`,
      metadata_path: provider.metadata_path,
      repairable: false,
    });
    blockingScopes.push(providerBlockingScope(provider));
  }
  quarantined.sort(compareDiagnostics);
  const normalized = {
    version: 1,
    adapter_fingerprint: adapterRegistry.fingerprint,
    policy,
    providers: activeProviders,
    quarantined,
    blocking_scopes: blockingScopes.sort(compareBlockingScopes),
  };
  return {
    ...normalized,
    fingerprint: crypto.createHash("sha256")
      .update(canonicalJson(normalized))
      .digest("hex"),
  };
}

export function providerTrustRank(value) {
  return TRUST_TIERS.get(value) ?? 0;
}

export function resolveSkillProviderClaims(registry, context, options = {}) {
  const adapterRegistry = requireAdapterRegistry(options.adapterRegistry);
  if (
    !isRecord(registry)
    || registry.version !== 1
    || registry.adapter_fingerprint !== adapterRegistry.fingerprint
    || !Array.isArray(registry.providers)
    || !Array.isArray(registry.blocking_scopes)
  ) {
    throw new Error("invalid normalized skill provider registry");
  }
  if (!isRecord(context) || !Array.isArray(context.candidates)) {
    throw new Error("invalid skill provider context");
  }
  const rawProfiles = Array.isArray(context.profiles) ? context.profiles : [context.profile];
  const profiles = uniqueSorted(
    rawProfiles.map((profile) => providerName(profile, "active profile")),
  );
  if (profiles.length === 0) throw new Error("active profiles must not be empty");
  const host = providerName(context.host ?? "unknown", "active host");
  const catalogs = isRecord(context.catalogs) ? context.catalogs : {};
  const claims = [];
  const blockingPriorities = new Map();
  const quarantined = [...(Array.isArray(registry.quarantined) ? registry.quarantined : [])];
  const logicalCandidateCounts = new Map();
  for (const rawCandidate of context.candidates) {
    const logicalNames = new Set([
      safePortableName(rawCandidate?.concrete_id),
      ...(Array.isArray(rawCandidate?.aliases)
        ? rawCandidate.aliases.map(safePortableName)
        : []),
    ]);
    logicalNames.delete(null);
    for (const name of logicalNames) {
      logicalCandidateCounts.set(name, (logicalCandidateCounts.get(name) ?? 0) + 1);
    }
  }

  for (const rawCandidate of context.candidates) {
    let candidate;
    try {
      candidate = normalizeProviderCandidate(rawCandidate);
    } catch (error) {
      quarantined.push(candidateDiagnostic(rawCandidate, "provider_candidate_invalid", error));
      continue;
    }
    if (candidate.declared_provider !== null) {
      quarantined.push(candidateDiagnostic(
        candidate,
        "provider_spoofing",
        new Error(`skill metadata cannot declare provider ownership: ${candidate.declared_provider}`),
      ));
      continue;
    }

    const compatible = [];
    const evidenceByProvider = new Map();
    let diagnosed = false;
    for (const scope of registry.blocking_scopes) {
      if (!providerMatchesContext(scope, candidate, profiles, host)) continue;
      const adapter = adapterRegistry.get(scope.adapter);
      if (adapter === null) continue;
      if (scope.block_all) {
        quarantined.push(providerClaimDiagnostic(candidate, scope, "provider_metadata_invalid"));
        blockingPriorities.set(
          candidate,
          Math.max(blockingPriorities.get(candidate) ?? -1, adapter.priority),
        );
        diagnosed = true;
        continue;
      }
      let result;
      try {
        result = normalizeAdapterMatchResult(
          adapter.match(scope, candidate, { catalogs, host, profiles }),
        );
        if (result.matched && !providerEvidenceMatchesCandidate(
          result.evidence,
          candidate,
          adapter.content_hash_mode(scope.config),
          adapter.evidence_source(scope, candidate),
        )) {
          throw new Error("provider adapter evidence does not match candidate");
        }
      } catch {
        result = { matched: true, reason: null };
      }
      if (!result.matched && result.reason === null) continue;
      quarantined.push(providerClaimDiagnostic(candidate, scope, "provider_metadata_invalid"));
      blockingPriorities.set(
        candidate,
        Math.max(blockingPriorities.get(candidate) ?? -1, adapter.priority),
      );
      diagnosed = true;
    }
    for (const provider of registry.providers) {
      if (!providerMatchesContext(provider, candidate, profiles, host)) continue;
      const adapter = adapterRegistry.get(provider.adapter);
      if (adapter === null) {
        quarantined.push(providerClaimDiagnostic(candidate, provider, "provider_adapter_unavailable"));
        diagnosed = true;
        continue;
      }
      let result;
      try {
        result = normalizeAdapterMatchResult(
          adapter.match(provider, candidate, { catalogs, host, profiles }),
        );
        if (result.matched && !providerEvidenceMatchesCandidate(
          result.evidence,
          candidate,
          adapter.content_hash_mode(provider.config),
          adapter.evidence_source(provider, candidate),
        )) {
          throw new Error("provider adapter evidence does not match candidate");
        }
      } catch {
        quarantined.push(providerClaimDiagnostic(candidate, provider, "provider_adapter_failure"));
        blockingPriorities.set(
          candidate,
          Math.max(blockingPriorities.get(candidate) ?? -1, adapter.priority),
        );
        diagnosed = true;
        continue;
      }
      if (!result.matched) {
        if (result.reason !== null) {
          quarantined.push(providerClaimDiagnostic(candidate, provider, result.reason));
          blockingPriorities.set(
            candidate,
            Math.max(blockingPriorities.get(candidate) ?? -1, adapter.priority),
          );
          diagnosed = true;
        }
        continue;
      }
      compatible.push({ provider, priority: adapter.priority });
      evidenceByProvider.set(provider, result.evidence);
    }
    if (compatible.length === 0) {
      if (!diagnosed) {
        quarantined.push(candidateDiagnostic(
          candidate,
          "provider_unresolved",
          new Error(`no provider adapter claimed skill: ${candidate.concrete_id}`),
        ));
      }
      continue;
    }
    const blockingPriority = blockingPriorities.get(candidate) ?? -1;
    const matchedPriority = Math.max(...compatible.map((entry) => entry.priority));
    if (blockingPriority >= matchedPriority) continue;
    const priority = Math.max(...compatible.map((entry) => entry.priority));
    const specific = compatible
      .filter((entry) => entry.priority === priority)
      .map((entry) => entry.provider);
    const eligible = [];
    for (const provider of specific) {
      if (!registry.policy.allowlist.includes(provider.id)) {
        quarantined.push(providerClaimDiagnostic(candidate, provider, "provider_not_allowed"));
        continue;
      }
      if (providerTrustRank(provider.trust_tier) < providerTrustRank(registry.policy.minimum_trust)) {
        quarantined.push(providerClaimDiagnostic(candidate, provider, "provider_trust_failure"));
        continue;
      }
      eligible.push(provider);
    }
    if (eligible.length === 0) continue;

    const preferredIds = uniqueSorted([
      ownValue(registry.policy.preferred_providers, candidate.concrete_id),
      ...candidate.aliases
        .filter((alias) => logicalCandidateCounts.get(alias) === 1)
        .map((alias) => ownValue(registry.policy.preferred_providers, alias)),
    ].filter((providerId) => providerId !== undefined));
    if (preferredIds.length > 1) {
      quarantined.push(candidateDiagnostic(
        candidate,
        "provider_preferred_conflict",
        new Error(`candidate selects conflicting preferred providers: ${preferredIds.join(", ")}`),
      ));
      continue;
    }
    const preferredId = preferredIds[0];
    if (preferredId !== undefined) {
      const preferred = eligible.find((provider) => provider.id === preferredId);
      if (!preferred) {
        quarantined.push(candidateDiagnostic(
          candidate,
          "provider_preferred_unavailable",
          new Error(`preferred provider is not eligible: ${preferredId}`),
        ));
        continue;
      }
      claims.push(providerClaim(
        candidate,
        preferred,
        registry.fingerprint,
        evidenceByProvider.get(preferred),
      ));
      continue;
    }
    if (eligible.length > 1) {
      quarantined.push(candidateDiagnostic(
        candidate,
        "provider_claim_ambiguous",
        new Error(`multiple providers claimed skill: ${eligible.map((provider) => provider.id).join(", ")}`),
      ));
      continue;
    }
    claims.push(providerClaim(
      candidate,
      eligible[0],
      registry.fingerprint,
      evidenceByProvider.get(eligible[0]),
    ));
  }

  quarantineLogicalNameCollisions(claims, quarantined, registry.policy.preferred_providers);
  claims.sort(compareClaims);
  quarantined.sort(compareDiagnostics);
  const catalogFingerprint = crypto.createHash("sha256")
    .update(canonicalJson({
      claims: claims.map(catalogFingerprintClaim),
      quarantined,
      registry_fingerprint: registry.fingerprint,
    }))
    .digest("hex");
  for (const claim of claims) claim.registry_fingerprint = catalogFingerprint;
  return {
    registry_fingerprint: catalogFingerprint,
    claims,
    quarantined,
  };
}

function catalogFingerprintClaim({
  registry_fingerprint: _registryFingerprint,
  ...claim
}) {
  return canonicalizeHostNeutralSkillProviderClaim(claim);
}

export function canonicalizeHostNeutralSkillProviderClaim(claim) {
  if (!isRecord(claim)) throw new Error("invalid skill provider claim");
  if (claim.source_host === null) return { ...claim };
  const sourceLocator = `host://active-host/skills/${claim.concrete_id}`;
  return {
    ...claim,
    source: claim.source === candidateSourceRoot(claim.source_locator)
      ? candidateSourceRoot(sourceLocator)
      : claim.source,
    source_host: "active-host",
    source_locator: sourceLocator,
  };
}

function normalizeProviderCandidate(raw) {
  if (!isRecord(raw)) throw new Error("provider candidate must be an object");
  assertExactKeys(raw, [
    "aliases",
    "concrete_id",
    "declared_provider",
    "source_authenticated",
    "source_hash",
    "source_host",
    "source_kind",
    "source_locator",
  ], "provider candidate");
  const concreteId = providerName(raw.concrete_id, "candidate concrete id");
  if (!Array.isArray(raw.aliases)) throw new Error(`candidate aliases must be a list: ${concreteId}`);
  const aliases = uniqueSorted(raw.aliases.map((alias) => providerName(alias, "candidate alias")));
  if (aliases.includes(concreteId)) throw new Error(`candidate alias duplicates concrete id: ${concreteId}`);
  if (typeof raw.source_kind !== "string" || !SOURCE_KINDS.has(raw.source_kind)) {
    throw new Error(`invalid candidate source kind: ${concreteId}`);
  }
  if (!hasFullMatch(raw.source_hash, SHA256_DIGEST)) {
    throw new Error(`invalid candidate source hash: ${concreteId}`);
  }
  const sourceHost = raw.source_host === null
    ? null
    : providerName(raw.source_host, "candidate source host");
  if (
    typeof raw.source_locator !== "string"
    || raw.source_locator.length === 0
    || !hasWellFormedUnicode(raw.source_locator)
    || /[\p{White_Space}\p{Cc}\p{Cf}]/u.test(raw.source_locator)
  ) {
    throw new Error(`invalid candidate source locator: ${concreteId}`);
  }
  if (raw.source_authenticated !== true) {
    throw new Error(`unauthenticated candidate source: ${concreteId}`);
  }
  let declaredProvider = null;
  if (raw.declared_provider !== null) {
    declaredProvider = providerName(raw.declared_provider, "declared provider");
  }
  return {
    concrete_id: concreteId,
    aliases,
    source_kind: raw.source_kind,
    source_hash: raw.source_hash,
    source_host: sourceHost,
    source_locator: raw.source_locator,
    source_authenticated: true,
    declared_provider: declaredProvider,
  };
}

function providerMatchesContext(provider, candidate, profiles, host) {
  return profiles.some((profile) => selectorMatches(provider.compatibility.profiles, profile))
    && selectorMatches(provider.compatibility.hosts, host)
    && provider.compatibility.source_kinds.includes(candidate.source_kind);
}

function selectorMatches(selectors, value) {
  return selectors.includes("*") || selectors.includes(value);
}

function matchProfileCatalog(provider, candidate, { catalogs, host }) {
  const profileCatalogs = ownValue(catalogs, provider.config.profile);
  const catalog = isRecord(profileCatalogs)
    ? ownValue(profileCatalogs, provider.config.catalog)
    : null;
  const names = profileCatalogMembers(catalog, provider);
  if (names === null) throw new Error("invalid provider profile catalog");
  const inCatalog = names.some(
    (name) => portableSkillCasefold(name) === candidate.concrete_id,
  );
  if (!inCatalog) return { matched: false };
  if (!profileCatalogMetadataMatches(catalog, provider, host)) {
    throw new Error("invalid provider profile catalog");
  }
  if (
    candidate.source_kind !== "host-bootstrap"
    || candidate.source_host !== host
    || candidate.source_authenticated !== true
    || candidate.source_locator !== `host://${host}/skills/${candidate.concrete_id}`
  ) {
    return { matched: false, reason: "provider_source_not_active_host" };
  }
  const expectedHash = provider.config.content_hash.mode === "pinned"
    ? ownValue(provider.config.content_hash.hashes, candidate.concrete_id)
    : null;
  if (provider.config.content_hash.mode === "pinned" && expectedHash === undefined) {
    return { matched: false, reason: "provider_source_hash_missing" };
  }
  if (expectedHash !== null && expectedHash !== candidate.source_hash) {
    return { matched: false, reason: "provider_source_hash_mismatch" };
  }
  return {
    matched: true,
    evidence: providerEvidence(provider, candidate, {
      catalog_ref: `profile://${provider.config.profile}/${provider.config.catalog}`,
      catalog_hash: crypto.createHash("sha256").update(canonicalJson(catalog)).digest("hex"),
      content_hash_mode: provider.config.content_hash.mode,
    }),
  };
}

function matchSourceKind(provider, candidate) {
  if (!provider.config.source_kinds.includes(candidate.source_kind)) {
    return { matched: false };
  }
  if (candidate.source_authenticated !== true) {
    return { matched: false, reason: "provider_source_unauthenticated" };
  }
  return {
    matched: true,
    evidence: providerEvidence(provider, candidate, {
      source: sourceKindEvidenceSource(provider, candidate),
    }),
  };
}

function providerEvidence(provider, candidate, overrides = {}) {
  return {
    source: overrides.source ?? provider.provenance.source,
    catalog_ref: overrides.catalog_ref ?? null,
    catalog_hash: overrides.catalog_hash ?? null,
    source_locator: candidate.source_locator,
    source_kind: candidate.source_kind,
    source_host: candidate.source_host,
    content_hash_mode: overrides.content_hash_mode ?? "verified",
  };
}

function profileCatalogEvidenceSource(provider) {
  return provider.provenance?.source ?? null;
}

function sourceKindEvidenceSource(provider, candidate) {
  return provider.config.source_mode === "candidate-root"
    ? candidateSourceRoot(candidate.source_locator)
    : provider.provenance?.source ?? null;
}

function profileCatalogContentHashMode(config) {
  return config.content_hash.mode;
}

function verifiedContentHashMode() {
  return "verified";
}

function providerEvidenceMatchesCandidate(
  evidence,
  candidate,
  contentHashMode,
  evidenceSource,
) {
  return ["observed", "pinned", "verified"].includes(contentHashMode)
    && typeof evidenceSource === "string"
    && evidence.source === evidenceSource
    && evidence.content_hash_mode === contentHashMode
    && evidence.source_kind === candidate.source_kind
    && evidence.source_host === candidate.source_host
    && evidence.source_locator === candidate.source_locator;
}

function profileCatalogMembers(catalog, provider) {
  if (!isRecord(catalog)) return null;
  if (!isExactRecord(catalog, [
    "active_host_only",
    "hosts",
    "implementation",
    "install_policy",
    "review",
    "source",
  ])) {
    return null;
  }
  const names = provider.config.membership.flatMap((section) => catalog[section]);
  if (
    names.length === 0
    || names.some((name) => typeof name !== "string" || !isPortableSkillName(name))
  ) {
    return null;
  }
  return names;
}

function profileCatalogMetadataMatches(catalog, provider, host) {
  return catalog.source === provider.provenance?.source
    && catalog.install_policy === provider.config.source_binding.require_install_policy
    && catalog.active_host_only === provider.config.source_binding.require_active_host_only
    && isRecord(catalog.hosts)
    && typeof ownValue(catalog.hosts, host) === "string";
}

function providerClaim(candidate, provider, registryFingerprint, evidence) {
  return {
    concrete_id: candidate.concrete_id,
    aliases: candidate.aliases,
    provider_id: provider.id,
    provider_version: provider.version,
    trust_tier: provider.trust_tier,
    ownership: provider.ownership,
    provenance_revision: provider.provenance.revision ?? null,
    source: evidence.source,
    source_hash: candidate.source_hash,
    source_host: evidence.source_host,
    source_kind: evidence.source_kind,
    source_locator: evidence.source_locator,
    content_hash_mode: evidence.content_hash_mode,
    catalog_ref: evidence.catalog_ref,
    catalog_hash: evidence.catalog_hash,
    adapter: provider.adapter,
    registry_fingerprint: registryFingerprint,
    compatibility: provider.compatibility,
    status: evidence.content_hash_mode === "observed" ? "observed" : "verified",
  };
}

function candidateDiagnostic(candidate, reason, error) {
  return {
    reason,
    provider_id: null,
    concrete_id: safePortableName(candidate?.concrete_id),
    detail: error instanceof Error ? error.message : String(error),
    metadata_path: `skill-index#skill:${safePortableName(candidate?.concrete_id) ?? "unknown"}`,
    repairable: false,
  };
}

function providerClaimDiagnostic(candidate, provider, reason) {
  return {
    reason,
    provider_id: provider.id,
    concrete_id: candidate.concrete_id,
    detail: `${reason}: ${candidate.concrete_id} from ${provider.id}`,
    metadata_path: provider.metadata_path,
    repairable: false,
  };
}

function quarantineLogicalNameCollisions(claims, quarantined, preferredProviders) {
  const byName = new Map();
  for (const claim of claims) {
    for (const name of [claim.concrete_id, ...claim.aliases]) {
      if (!byName.has(name)) byName.set(name, []);
      byName.get(name).push(claim);
    }
  }
  const rejected = new Set();
  for (const [name, matching] of byName) {
    const uniqueClaims = [...new Set(matching)];
    if (uniqueClaims.length < 2) continue;
    const concreteCollision = uniqueClaims.every((claim) => claim.concrete_id === name);
    const preferredId = ownValue(preferredProviders, name);
    const preferred = preferredId === undefined
      ? []
      : uniqueClaims.filter((claim) => claim.provider_id === preferredId);
    if (preferred.length === 1) {
      for (const claim of uniqueClaims) {
        if (claim === preferred[0]) continue;
        if (concreteCollision) {
          rejected.add(claim);
        } else {
          claim.aliases = claim.aliases.filter((alias) => alias !== name);
        }
        quarantined.push({
          reason: concreteCollision
            ? "provider_concrete_id_not_preferred"
            : "provider_alias_not_preferred",
          provider_id: claim.provider_id,
          concrete_id: claim.concrete_id,
          detail: `preferred provider selected for logical name: ${name}`,
          metadata_path: `skill-index#skill:${claim.concrete_id}`,
          repairable: false,
        });
      }
      continue;
    }
    for (const claim of uniqueClaims) {
      rejected.add(claim);
      quarantined.push({
        reason: preferredId === undefined
          ? (concreteCollision ? "provider_concrete_id_ambiguous" : "provider_alias_ambiguous")
          : "provider_preferred_unavailable",
        provider_id: claim.provider_id,
        concrete_id: claim.concrete_id,
        detail: preferredId === undefined
          ? `provider logical name collision: ${name}`
          : `preferred provider cannot resolve logical name: ${name}`,
        metadata_path: `skill-index#skill:${claim.concrete_id}`,
        repairable: false,
      });
    }
  }
  for (let index = claims.length - 1; index >= 0; index -= 1) {
    if (rejected.has(claims[index])) claims.splice(index, 1);
  }
}

function compareClaims(left, right) {
  return compareCodePoints(
    `${left.concrete_id}\u0000${left.provider_id}`,
    `${right.concrete_id}\u0000${right.provider_id}`,
  );
}

function normalizeProvider(raw, adapterRegistry, metadataPath) {
  if (!isRecord(raw)) throw new Error("provider entry must be an object");
  assertExactKeys(raw, [
    "adapter",
    "compatibility",
    "config",
    "id",
    "ownership",
    "provenance",
    "trust_tier",
    "version",
  ], "provider");
  const id = providerName(raw.id, "provider id");
  const adapter = typeof raw.adapter === "string"
    ? adapterRegistry.get(raw.adapter)
    : null;
  if (adapter === null) {
    throw new Error("unknown provider adapter");
  }
  if (!hasFullMatch(raw.version, SEMANTIC_VERSION)) {
    throw new Error(`invalid provider version: ${id}`);
  }
  if (!TRUST_TIERS.has(raw.trust_tier)) {
    throw new Error(`invalid provider trust tier: ${id}`);
  }
  if (!OWNERSHIP_TYPES.has(raw.ownership)) {
    throw new Error(`invalid provider ownership: ${id}`);
  }
  const compatibility = normalizeCompatibility(raw.compatibility, id);
  const config = adapter.normalize_config(raw.config, compatibility, id);
  if (!isRecord(config)) throw new Error(`invalid provider adapter config: ${id}`);
  const contentHashMode = adapter.content_hash_mode(config);
  if (!["observed", "pinned", "verified"].includes(contentHashMode)) {
    throw new Error(`invalid provider content hash mode: ${id}`);
  }
  if (
    contentHashMode === "observed"
    && (raw.trust_tier !== "user" || raw.ownership !== "user")
  ) {
    throw new Error(`untrusted observed provider content: ${id}`);
  }
  const provider = {
    id,
    adapter: adapter.id,
    version: raw.version,
    trust_tier: raw.trust_tier,
    ownership: raw.ownership,
    provenance: normalizeProvenance(raw.provenance, id),
    compatibility,
    config,
  };
  Object.defineProperty(provider, "metadata_path", { value: metadataPath });
  return provider;
}
function providerBlockingScope(provider) {
  return {
    id: provider.id,
    adapter: provider.adapter,
    provenance: provider.provenance,
    compatibility: provider.compatibility,
    config: provider.config,
    metadata_path: provider.metadata_path,
    block_all: false,
  };
}

function recoverProviderBlockingScope(raw, adapterRegistry, metadataPath) {
  if (!isRecord(raw)) return null;
  let id;
  let adapter;
  let compatibility;
  try {
    id = providerName(raw.id, "provider id");
    adapter = typeof raw.adapter === "string"
      ? adapterRegistry.get(raw.adapter)
      : null;
    if (adapter === null) return null;
    compatibility = normalizeCompatibility(raw.compatibility, id);
  } catch {
    return null;
  }
  let config;
  let blockAll = false;
  try {
    config = adapter.normalize_config(raw.config, compatibility, id);
    if (!isRecord(config)) throw new Error(`invalid provider adapter config: ${id}`);
  } catch {
    config = null;
    blockAll = true;
  }
  let provenance = null;
  try {
    provenance = normalizeProvenance(raw.provenance, id);
  } catch {
    provenance = null;
  }
  return {
    id,
    adapter: adapter.id,
    provenance,
    compatibility,
    config,
    block_all: blockAll,
    metadata_path: metadataPath,
  };
}

function compareBlockingScopes(left, right) {
  return compareCodePoints(
    `${left.id}\u0000${left.metadata_path}`,
    `${right.id}\u0000${right.metadata_path}`,
  );
}

function normalizeProviderPolicy(raw) {
  if (!isRecord(raw)) throw new Error("invalid provider policy");
  assertExactKeys(raw, ["allowlist", "minimum_trust", "preferred_providers"], "provider policy");
  if (!Array.isArray(raw.allowlist)) throw new Error("provider allowlist must be a list");
  if (!TRUST_TIERS.has(raw.minimum_trust)) throw new Error("invalid provider minimum trust");
  if (!isRecord(raw.preferred_providers)) throw new Error("invalid preferred provider policy");
  const allowlist = uniqueSorted(
    raw.allowlist.map((value) => providerName(value, "allowlisted provider")),
  );
  const preferredProviders = Object.create(null);
  for (const [name, providerId] of Object.entries(raw.preferred_providers).sort(([left], [right]) => compareCodePoints(left, right))) {
    const normalizedName = providerName(name, "preferred skill");
    if (Object.hasOwn(preferredProviders, normalizedName)) {
      throw new Error(`duplicate preferred skill: ${normalizedName}`);
    }
    preferredProviders[normalizedName] = providerName(providerId, "preferred provider");
  }
  return {
    allowlist,
    minimum_trust: raw.minimum_trust,
    preferred_providers: preferredProviders,
  };
}

function normalizeProvenance(raw, id) {
  if (!isRecord(raw)) throw new Error(`invalid provider provenance: ${id}`);
  const expectedKeys = Object.hasOwn(raw, "revision")
    ? ["revision", "source"]
    : ["source"];
  assertExactKeys(raw, expectedKeys, "provider provenance");
  if (
    typeof raw.source !== "string"
    || raw.source.length === 0
    || !hasWellFormedUnicode(raw.source)
    || /[\p{White_Space}\p{Cc}\p{Cf}]/u.test(raw.source)
  ) {
    throw new Error(`invalid provider provenance source: ${id}`);
  }
  if (
    Object.hasOwn(raw, "revision")
    && (
      typeof raw.revision !== "string"
      || !hasFullMatch(raw.revision, /^[0-9a-f]{40}$/)
    )
  ) {
    throw new Error(`invalid provider provenance revision: ${id}`);
  }
  return Object.hasOwn(raw, "revision")
    ? { source: raw.source, revision: raw.revision }
    : { source: raw.source };
}

function normalizeCompatibility(raw, id) {
  if (!isRecord(raw)) throw new Error(`invalid provider compatibility: ${id}`);
  assertExactKeys(raw, ["hosts", "profiles", "registry", "source_kinds"], "provider compatibility");
  if (raw.registry !== 1) throw new Error(`unsupported provider registry compatibility: ${id}`);
  return {
    registry: 1,
    profiles: normalizeSelectorList(raw.profiles, "profile", id),
    hosts: normalizeSelectorList(raw.hosts, "host", id),
    source_kinds: normalizeSourceKinds(raw.source_kinds, id),
  };
}

function normalizeSourceKindConfig(raw, compatibility, id) {
  if (!isRecord(raw)) throw new Error(`invalid provider adapter config: ${id}`);
  const expectedKeys = Object.hasOwn(raw, "source_mode")
    ? ["source_kinds", "source_mode"]
    : ["source_kinds"];
  assertExactKeys(raw, expectedKeys, "source-kind adapter config");
  const sourceKinds = normalizeSourceKinds(raw.source_kinds, id);
  if (sourceKinds.some((sourceKind) => !compatibility.source_kinds.includes(sourceKind))) {
    throw new Error(`provider adapter source kind is incompatible: ${id}`);
  }
  const sourceMode = ownValue(raw, "source_mode") ?? "provider";
  if (!["candidate-root", "provider"].includes(sourceMode)) {
    throw new Error(`invalid provider adapter source mode: ${id}`);
  }
  return { source_kinds: sourceKinds, source_mode: sourceMode };
}

function normalizeProfileCatalogConfig(raw, compatibility, id) {
  if (!isRecord(raw)) throw new Error(`invalid provider adapter config: ${id}`);
  assertExactKeys(
    raw,
    ["catalog", "content_hash", "membership", "profile", "source_binding"],
    "profile-catalog adapter config",
  );
  const profile = providerName(raw.profile, "catalog profile");
  if (!hasFullMatch(raw.catalog, /^[A-Za-z0-9_]+$/)) {
    throw new Error(`invalid provider catalog: ${id}`);
  }
  if (
    compatibility.source_kinds.length !== 1
    || compatibility.source_kinds[0] !== "host-bootstrap"
  ) {
    throw new Error(`provider catalog source kind is incompatible: ${id}`);
  }
  const membership = uniqueSorted(
    Array.isArray(raw.membership) ? raw.membership : [],
  );
  if (
    membership.length === 0
    || membership.some((section) => !["implementation", "review"].includes(section))
  ) {
    throw new Error(`invalid provider catalog membership: ${id}`);
  }
  if (!isRecord(raw.source_binding)) {
    throw new Error(`invalid provider source binding: ${id}`);
  }
  assertExactKeys(
    raw.source_binding,
    ["mode", "require_active_host_only", "require_install_policy"],
    "provider source binding",
  );
  if (
    raw.source_binding.mode !== "active-host"
    || raw.source_binding.require_install_policy !== "never"
    || raw.source_binding.require_active_host_only !== true
  ) {
    throw new Error(`invalid provider source binding: ${id}`);
  }
  const contentHash = normalizeContentHash(raw.content_hash, id);
  return {
    profile,
    catalog: raw.catalog,
    membership,
    source_binding: {
      mode: "active-host",
      require_install_policy: "never",
      require_active_host_only: true,
    },
    content_hash: contentHash,
  };
}

function normalizeContentHash(raw, id) {
  if (!isRecord(raw) || !["observed", "pinned"].includes(raw.mode)) {
    throw new Error(`invalid provider content hash policy: ${id}`);
  }
  if (raw.mode === "observed") {
    assertExactKeys(raw, ["mode"], "observed provider content hash");
    return { mode: "observed" };
  }
  assertExactKeys(raw, ["hashes", "mode"], "pinned provider content hash");
  if (!isRecord(raw.hashes) || Object.keys(raw.hashes).length === 0) {
    throw new Error(`provider content hashes are empty: ${id}`);
  }
  const hashes = Object.create(null);
  for (const [rawName, sourceHash] of Object.entries(raw.hashes)) {
    const name = providerName(rawName, "provider content hash skill");
    if (Object.hasOwn(hashes, name) || !hasFullMatch(sourceHash, SHA256_DIGEST)) {
      throw new Error(`invalid provider content hash: ${id}`);
    }
    hashes[name] = sourceHash;
  }
  return {
    mode: "pinned",
    hashes: Object.fromEntries(
      Object.entries(hashes).sort(([left], [right]) => compareCodePoints(left, right)),
    ),
  };
}

function normalizeSelectorList(raw, label, id) {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new Error(`provider ${label} compatibility is empty: ${id}`);
  }
  const values = raw.map((value) => value === "*" ? "*" : providerName(value, `compatible ${label}`));
  return uniqueSorted(values);
}

function normalizeSourceKinds(raw, id) {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new Error(`provider source compatibility is empty: ${id}`);
  }
  const values = raw.map((value) => {
    if (typeof value !== "string" || !SOURCE_KINDS.has(value)) {
      throw new Error(`invalid provider source kind: ${id}`);
    }
    return value;
  });
  return uniqueSorted(values);
}

function normalizeAdapterDefinition(raw) {
  if (!isRecord(raw)) throw new Error("skill provider adapter must be an object");
  assertExactKeys(raw, [
    "content_hash_mode",
    "evidence_source",
    "id",
    "match",
    "normalize_config",
    "priority",
    "version",
  ], "skill provider adapter");
  const id = providerName(raw.id, "provider adapter id");
  if (!hasFullMatch(raw.version, SEMANTIC_VERSION)) {
    throw new Error(`invalid skill provider adapter version: ${id}`);
  }
  if (!Number.isSafeInteger(raw.priority) || raw.priority < 0) {
    throw new Error(`invalid skill provider adapter priority: ${id}`);
  }
  if (
    typeof raw.normalize_config !== "function"
    || typeof raw.match !== "function"
    || typeof raw.content_hash_mode !== "function"
    || typeof raw.evidence_source !== "function"
  ) {
    throw new Error(`invalid skill provider adapter callbacks: ${id}`);
  }
  return Object.freeze({
    id,
    version: raw.version,
    priority: raw.priority,
    content_hash_mode: raw.content_hash_mode,
    evidence_source: raw.evidence_source,
    normalize_config: raw.normalize_config,
    match: raw.match,
  });
}

function requireAdapterRegistry(value) {
  const adapterRegistry = value ?? DEFAULT_ADAPTER_REGISTRY;
  if (
    !isRecord(adapterRegistry)
    || !hasFullMatch(adapterRegistry.fingerprint, SHA256_DIGEST)
    || !Array.isArray(adapterRegistry.ids)
    || typeof adapterRegistry.get !== "function"
  ) {
    throw new Error("invalid skill provider adapter registry");
  }
  return adapterRegistry;
}

function normalizeAdapterMatchResult(raw) {
  if (!isRecord(raw)) throw new Error("invalid skill provider adapter result");
  const expectedKeys = raw.matched
    ? ["evidence", "matched"]
    : raw.reason === undefined
      ? ["matched"]
      : ["matched", "reason"];
  if (
    typeof raw.matched !== "boolean"
    || canonicalJson(Object.keys(raw).sort(compareCodePoints)) !== canonicalJson(expectedKeys)
  ) {
    throw new Error("invalid skill provider adapter result");
  }
  const reasons = new Set([
    "provider_source_hash_mismatch",
    "provider_source_hash_missing",
    "provider_source_not_active_host",
    "provider_source_unauthenticated",
  ]);
  if (raw.reason !== undefined && !reasons.has(raw.reason)) {
    throw new Error("invalid skill provider adapter diagnostic");
  }
  if (raw.matched && !isProviderEvidence(raw.evidence)) {
    throw new Error("invalid skill provider adapter evidence");
  }
  return {
    matched: raw.matched,
    reason: raw.reason ?? null,
    evidence: raw.evidence ?? null,
  };
}


function providerName(value, label) {
  if (typeof value !== "string" || !isPortableSkillName(value)) {
    throw new Error(`invalid ${label}`);
  }
  return portableSkillCasefold(value);
}

function safePortableName(value) {
  return typeof value === "string" && isPortableSkillName(value)
    ? portableSkillCasefold(value)
    : null;
}

function ownValue(record, key) {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

function isExactRecord(value, keys) {
  if (!isRecord(value)) return false;
  return canonicalJson(Object.keys(value).sort(compareCodePoints))
    === canonicalJson([...keys].sort(compareCodePoints));
}

function isProviderEvidence(value) {
  if (!isExactRecord(value, [
    "catalog_hash",
    "catalog_ref",
    "content_hash_mode",
    "source",
    "source_host",
    "source_kind",
    "source_locator",
  ])) {
    return false;
  }
  const catalogFieldsValid = value.catalog_ref === null
    ? value.catalog_hash === null
    : (
      typeof value.catalog_ref === "string"
      && value.catalog_ref.length > 0
      && hasFullMatch(value.catalog_hash, SHA256_DIGEST)
    );
  return catalogFieldsValid
    && ["observed", "pinned", "verified"].includes(value.content_hash_mode)
    && typeof value.source === "string"
    && value.source.length > 0

    && hasWellFormedUnicode(value.source)
    && !/[\p{White_Space}\p{Cc}\p{Cf}]/u.test(value.source)
    && typeof value.source_kind === "string"
    && SOURCE_KINDS.has(value.source_kind)
    && (value.source_host === null || isPortableSkillName(value.source_host))
    && typeof value.source_locator === "string"
    && value.source_locator.length > 0
    && hasWellFormedUnicode(value.source_locator)
    && !/[\p{White_Space}\p{Cc}\p{Cf}]/u.test(value.source_locator);
}
function candidateSourceRoot(sourceLocator) {
  return sourceLocator.endsWith("/SKILL.md")
    ? sourceLocator.slice(0, -"/SKILL.md".length)
    : sourceLocator;
}


function hasWellFormedUnicode(value) {
  return typeof value === "string" && !/[\uD800-\uDFFF]/u.test(value);
}

function providerDiagnostic(raw, error, metadataPath) {
  const providerId = isRecord(raw) && isPortableSkillName(raw.id)
    ? portableSkillCasefold(raw.id)
    : null;
  return {
    reason: "provider_metadata_invalid",
    provider_id: providerId,
    detail: error instanceof Error ? error.message : String(error),
    metadata_path: providerId === null
      ? "skill-provider-registry.json#provider:unknown"
      : metadataPath,
    repairable: false,
  };
}

function duplicateProviderIds(providers) {
  const counts = new Map();
  for (const provider of providers) counts.set(provider.id, (counts.get(provider.id) ?? 0) + 1);
  return new Set([...counts].filter(([, count]) => count > 1).map(([id]) => id));
}

function hasFullMatch(value, pattern) {
  if (typeof value !== "string") return false;
  const match = pattern.exec(value);
  return match !== null && match[0] === value;
}

function assertExactKeys(value, allowed, label) {
  const actual = Object.keys(value).sort(compareCodePoints);
  const expected = [...allowed].sort(compareCodePoints);
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`invalid ${label} fields`);
  }
}

function uniqueSorted(values) {
  return [...new Set(values)].sort(compareCodePoints);
}

function compareDiagnostics(left, right) {
  return compareCodePoints(canonicalJson(left), canonicalJson(right));
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort(compareCodePoints)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
