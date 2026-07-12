import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import {
  ensureCodexWorktreeHookTrust,
  loadManagedHookCommandPolicy,
  trustedManagedHookCommand,
} from "./codex-hook-trust.mjs";

const MANIFEST_VERSION = 3;
const LEGACY_MANIFEST_VERSION = 2;
const FLOW_START = "<!-- agent-flow:start -->";
const FLOW_END = "<!-- agent-flow:end -->";
const EXCLUDE_START = "# agent-flow worktree host bridge:start";
const EXCLUDE_END = "# agent-flow worktree host bridge:end";
const MANAGED_HOOK_SCRIPTS = new Set([
  "guard-worktree.sh",
  "guard-worktree-write.py",
  "guard-protected-branch.sh",
  "show-phase-status.sh",
  "comment-checker.py",
]);

const BRIDGE_SPECS = Object.freeze([
  {
    source: "AGENTS.md",
    destination: "AGENTS.md",
    kind: "file",
    sourceType: "file",
    policy: "context",
  },
  {
    source: "CLAUDE.md",
    destination: "CLAUDE.md",
    kind: "file",
    sourceType: "file",
    policy: "context",
  },
  {
    source: ".Codex/agents/code-reviewer.md",
    destination: ".Codex/agents/code-reviewer.md",
    kind: "file",
    sourceType: "file",
    policy: "reviewer",
  },
  {
    source: ".claude/agents/code-reviewer.md",
    destination: ".claude/agents/code-reviewer.md",
    kind: "file",
    sourceType: "file",
    policy: "reviewer",
  },
  {
    source: ".claude/agents/code-reviewer.md",
    destination: ".omp/agents/code-reviewer.md",
    kind: "file",
    sourceType: "file",
    policy: "reviewer",
  },
  {
    source: ".Codex/hooks.json",
    destination: ".Codex/hooks.json",
    kind: "file",
    sourceType: "file",
    policy: "hook-json",
  },
  {
    source: ".codex/hooks.json",
    destination: ".codex/hooks.json",
    kind: "file",
    sourceType: "file",
    policy: "hook-json",
  },
  {
    source: ".claude/settings.json",
    destination: ".claude/settings.json",
    kind: "file",
    sourceType: "file",
    policy: "hook-json",
  },
  {
    source: ".agent-flow/bin",
    destination: ".agent-flow/bin",
    kind: "symlink",
    sourceType: "directory",
    policy: "runtime",
  },
  {
    source: ".agent-flow/skills",
    destination: ".agent-flow/skills",
    kind: "symlink",
    sourceType: "directory",
    policy: "runtime",
  },
  {
    source: ".agents/skills",
    destination: ".agents/skills",
    kind: "symlink",
    sourceType: "directory",
    policy: "host",
  },
  {
    source: ".claude/skills",
    destination: ".claude/skills",
    kind: "symlink",
    sourceType: "directory",
    policy: "host",
  },
  {
    source: ".omp/extensions/agent-flow-hooks.ts",
    destination: ".omp/extensions/agent-flow-hooks.ts",
    kind: "symlink",
    sourceType: "file",
    policy: "host",
  },
  {
    source: ".omp/skills",
    destination: ".omp/skills",
    kind: "symlink",
    sourceType: "directory",
    policy: "host",
  },
]);

const EXCLUDE_PATHS = Object.freeze(BRIDGE_SPECS.map(({ destination }) => `/${destination}`));

export function ensureWorktreeHostBridge(leaderRoot, worktreeRoot) {
  const leader = fs.realpathSync.native(leaderRoot);
  const worktree = fs.realpathSync.native(worktreeRoot);
  if (leader === worktree) return;
  assertRegisteredWorktree(leader, worktree);

  const commonDir = resolveGitPath(leader, gitOutput(leader, ["rev-parse", "--git-common-dir"]));
  const gitDir = resolveGitPath(worktree, gitOutput(worktree, ["rev-parse", "--git-dir"]));
  const identity = normalizedRelative(commonDir, gitDir);
  if (!identity || identity.startsWith("../") || path.isAbsolute(identity)) {
    throw new Error(`blocked: worktree git dir is outside the leader git common dir: ${gitDir}`);
  }
  const manifestPath = path.join(
    commonDir,
    "agent-flow",
    "worktree-host-bridges",
    `${hashBuffer(Buffer.from(identity, "utf8"))}.json`,
  );
  assertSafeParents(commonDir, manifestPath);
  const manifestSnapshot = snapshotPlainFile(manifestPath, "host bridge manifest");
  const previous = manifestSnapshot.exists
    ? parseManifest(manifestSnapshot.content, manifestPath, leader, worktree)
    : null;
  const sources = resolveSources(leader);
  validatePreviousLinks(previous, sources);
  const previousLinks = new Map((previous?.links ?? []).map((entry) => [entry.destination, entry]));
  const actions = [];
  const links = [];

  for (const source of sources) {
    const destination = path.join(worktree, source.destination);
    assertSafeParents(worktree, destination);
    const destinationStat = lstatIfExists(destination);
    const tracked = trackedPaths(worktree, source.destination).length > 0;
    const old = previousLinks.get(source.destination) ?? null;

    if (source.policy === "context") {
      const context = planContextFile({ source, destination, destinationStat, old });
      if (context.action) actions.push(context.action);
      links.push(context.link);
      continue;
    }

    if (source.policy === "hook-json") {
      const hookJson = planHookJson({ source, destination, destinationStat, tracked, old });
      if (hookJson.action) actions.push(hookJson.action);
      links.push(hookJson.link);
      continue;
    }

    const planned = planHostPath({ source, destination, destinationStat, tracked, old });
    if (planned.action) actions.push(planned.action);
    links.push(planned.link);
  }

  const excludePath = path.join(commonDir, "info", "exclude");
  assertSafeParents(commonDir, excludePath);
  const excludeSnapshot = snapshotPlainFile(excludePath, "git info exclude");
  const excludeContent = mergedExclude(excludeSnapshot.exists ? excludeSnapshot.content : Buffer.alloc(0), excludePath);
  const unsignedManifest = {
    version: MANIFEST_VERSION,
    leader_root: leader,
    workspace_root: worktree,
    links,
  };
  const manifest = {
    ...unsignedManifest,
    manifest_hash: hashBuffer(Buffer.from(canonicalJson(unsignedManifest), "utf8")),
  };
  const manifestContent = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  const createdDirectories = [];
  const appliedActions = [];
  let excludeAppliedSnapshot = null;
  let manifestAppliedSnapshot = null;

  try {
    for (const action of actions) {
      ensureSafeParentDirectories(worktree, action.destination, createdDirectories);
      applyAction(action);
      action.applied = appliedActionSnapshot(action);
      appliedActions.push(action);
    }
    ensureSafeParentDirectories(commonDir, path.join(commonDir, "info", "exclude"), createdDirectories);
    if (!bufferEqualsSnapshot(excludeContent, excludeSnapshot)) {
      excludeAppliedSnapshot = publishSnapshotUpdate(
        excludePath,
        excludeContent,
        excludeSnapshot,
        "git info exclude",
      );
    }
    ensureSafeParentDirectories(commonDir, manifestPath, createdDirectories);
    if (!bufferEqualsSnapshot(manifestContent, manifestSnapshot)) {
      manifestAppliedSnapshot = publishSnapshotUpdate(
        manifestPath,
        manifestContent,
        manifestSnapshot,
        "host bridge manifest",
      );
    }
    ensureCodexWorktreeHookTrust({ leaderRoot: leader, worktreeRoot: worktree });
  } catch (error) {
    const rollbackErrors = [];
    if (manifestAppliedSnapshot) {
      restoreSnapshot(manifestPath, manifestSnapshot, manifestAppliedSnapshot, rollbackErrors);
    }
    if (excludeAppliedSnapshot) {
      restoreSnapshot(excludePath, excludeSnapshot, excludeAppliedSnapshot, rollbackErrors);
    }
    for (const action of [...appliedActions].reverse()) rollbackAction(action, rollbackErrors);
    for (const directory of [...createdDirectories].reverse()) removeEmptyDirectory(directory, rollbackErrors);
    if (rollbackErrors.length > 0) {
      throw new Error(`${error.message}; rollback failed: ${rollbackErrors.join("; ")}`, { cause: error });
    }
    throw error;
  }
}

function resolveSources(leader) {
  const sources = [];
  const caseDestinations = new Map();
  const hookPolicy = loadManagedHookCommandPolicy(leader);
  for (const spec of BRIDGE_SPECS) {
    const source = path.join(leader, spec.source);
    const sourceStat = assertSafeSource(leader, source, spec.sourceType);
    const destinationKey = spec.destination.toLowerCase();
    const existing = caseDestinations.get(destinationKey);
    if (existing && sameExistingPath(existing, source)) continue;
    if (!existing) caseDestinations.set(destinationKey, source);
    const content = spec.kind === "file" ? fs.readFileSync(source) : null;
    const flowBlock = spec.policy === "context" ? extractFlowBlock(content, source) : null;
    const hookConfig = spec.policy === "hook-json" ? parseHookConfig(content, source) : null;
    const managedHooks = hookConfig
      ? managedHookEntries(hookConfig, source, hookPolicy)
      : null;
    const unmanagedHooks = hookConfig ? withoutManagedHooks(hookConfig, hookPolicy) : null;
    sources.push({
      ...spec,
      sourceRelative: spec.source,
      source,
      sourceStat,
      content,
      sourceHash: content ? hashBuffer(content) : null,
      flowBlock,
      flowHash: flowBlock ? hashBuffer(flowBlock) : null,
      hookConfig,
      hookPolicy,
      managedHooks,
      managedHash: managedHooks
        ? hashBuffer(Buffer.from(canonicalJson(managedHooks), "utf8"))
        : null,
      unmanagedHooks,
    });
  }
  return sources;
}

function planContextFile({ source, destination, destinationStat, old }) {
  if (destinationStat?.isSymbolicLink() || (destinationStat && !destinationStat.isFile())) {
    throw new Error(`blocked: unsafe worktree context path: ${destination}`);
  }
  const current = destinationStat ? fs.readFileSync(destination) : Buffer.alloc(0);
  if (old) {
    assertOldLink(old, source);
    if (old.ownership !== "managed-block") {
      throw new Error(`blocked: invalid host bridge ownership for ${source.destination}`);
    }
    if (!destinationStat) {
      throw new Error(`blocked: managed worktree context file is missing: ${destination}`);
    }
    const currentBlock = extractFlowBlock(current, destination);
    if (hashBuffer(currentBlock) !== old.hash) {
      throw new Error(`blocked: managed worktree context block was modified: ${destination}`);
    }
  }
  const desired = mergeFlowBlock(current, source.flowBlock, destination, path.basename(source.destination));
  const action = desired.equals(current)
    ? null
    : fileAction(destination, desired, destinationStat?.mode ?? source.sourceStat.mode, destinationStat);
  return {
    action,
    link: manifestLink(source, "managed-block", source.flowHash),
  };
}

function planHookJson({ source, destination, destinationStat, tracked, old }) {
  if (old?.ownership === "bridge") {
    return planHostPath({ source, destination, destinationStat, tracked, old });
  }
  if (destinationStat?.isSymbolicLink() || (destinationStat && !destinationStat.isFile())) {
    throw new Error(`blocked: unsafe hook settings path: ${destination}`);
  }
  if (!destinationStat) {
    if (old) {
      throw new Error(`blocked: managed hook settings file is missing: ${destination}`);
    }
    if (tracked) {
      throw new Error(`blocked: tracked host bridge path is missing: ${destination}`);
    }
    return {
      action: fileAction(destination, source.content, source.sourceStat.mode, null),
      link: manifestLink(source, "bridge", source.sourceHash),
    };
  }

  const currentContent = fs.readFileSync(destination);
  const currentConfig = parseHookConfig(currentContent, destination);
  const currentManaged = managedHookEntries(
    currentConfig,
    destination,
    source.hookPolicy,
    false,
  );
  if (old) {
    assertOldLink(old, source);
    if (old.ownership !== "managed-json") {
      throw new Error(`blocked: invalid host bridge ownership for ${source.destination}`);
    }
    const currentManagedHash = hashBuffer(Buffer.from(canonicalJson(currentManaged), "utf8"));
    if (currentManagedHash !== old.hash) {
      throw new Error(`blocked: managed hook settings were modified: ${destination}`);
    }
  }
  const currentUnmanaged = withoutManagedHooks(currentConfig, source.hookPolicy);
  if (canonicalJson(currentUnmanaged) !== canonicalJson(source.unmanagedHooks)) {
    throw new Error(`blocked: user hook settings differ from the leader source: ${destination}`);
  }
  const desiredConfig = mergeManagedHooks(
    currentConfig,
    source.managedHooks,
    source.hookPolicy,
  );
  const desiredContent = Buffer.from(`${JSON.stringify(desiredConfig, null, 2)}\n`, "utf8");
  const action = canonicalJson(desiredConfig) === canonicalJson(currentConfig)
    ? null
    : fileAction(destination, desiredContent, destinationStat.mode, destinationStat);
  return {
    action,
    link: manifestLink(source, "managed-json", source.managedHash),
  };
}

function planHostPath({ source, destination, destinationStat, tracked, old }) {
  if (old) {
    assertOldLink(old, source);
    if (old.ownership === "tracked") {
      if (!tracked || !destinationStat?.isFile() || destinationStat.isSymbolicLink()) {
        throw new Error(`blocked: tracked host bridge path changed: ${destination}`);
      }
      const current = fs.readFileSync(destination);
      if (hashBuffer(current) !== old.hash) {
        throw new Error(`blocked: tracked host bridge path was modified: ${destination}`);
      }
      if (!current.equals(source.content)) {
        throw new Error(`blocked: tracked reviewer is stale and cannot be overwritten: ${destination}`);
      }
      return { action: null, link: manifestLink(source, "tracked", source.sourceHash) };
    }
    if (old.ownership !== "bridge" || tracked) {
      throw new Error(`blocked: invalid host bridge ownership for ${source.destination}`);
    }
    if (!destinationStat) {
      throw new Error(`blocked: managed host bridge path is missing: ${destination}`);
    }
    if (source.kind === "file") {
      if (destinationStat.isSymbolicLink() || !destinationStat.isFile()) {
        throw new Error(`blocked: managed host bridge file type changed: ${destination}`);
      }
      const current = fs.readFileSync(destination);
      if (hashBuffer(current) !== old.hash) {
        throw new Error(`blocked: managed host bridge file was modified: ${destination}`);
      }
      const action = current.equals(source.content)
        ? null
        : fileAction(destination, source.content, source.sourceStat.mode, destinationStat);
      return { action, link: manifestLink(source, "bridge", source.sourceHash) };
    }
    const desiredTarget = relativeLinkTarget(destination, source.source);
    if (!destinationStat.isSymbolicLink()) {
      throw new Error(`blocked: managed host bridge symlink type changed: ${destination}`);
    }
    const currentTarget = fs.readlinkSync(destination);
    if (hashBuffer(Buffer.from(currentTarget, "utf8")) !== old.hash) {
      throw new Error(`blocked: managed host bridge symlink was modified: ${destination}`);
    }
    if (!sameExistingPath(path.resolve(path.dirname(destination), currentTarget), source.source)) {
      throw new Error(`blocked: managed host bridge symlink escaped its source: ${destination}`);
    }
    if (currentTarget !== desiredTarget) {
      throw new Error(`blocked: managed host bridge symlink is non-canonical: ${destination}`);
    }
    return {
      action: null,
      link: manifestLink(source, "bridge", hashBuffer(Buffer.from(desiredTarget, "utf8"))),
    };
  }

  if (destinationStat) {
    if (
      tracked
      && source.policy === "reviewer"
      && destinationStat.isFile()
      && !destinationStat.isSymbolicLink()
      && fs.readFileSync(destination).equals(source.content)
    ) {
      return { action: null, link: manifestLink(source, "tracked", source.sourceHash) };
    }
    throw new Error(`blocked: unmanaged host bridge path already exists: ${destination}`);
  }
  if (tracked) {
    throw new Error(`blocked: tracked host bridge path is missing: ${destination}`);
  }
  if (source.kind === "file") {
    return {
      action: fileAction(destination, source.content, source.sourceStat.mode, null),
      link: manifestLink(source, "bridge", source.sourceHash),
    };
  }
  const desiredTarget = relativeLinkTarget(destination, source.source);
  return {
    action: {
      kind: "symlink",
      destination,
      target: desiredTarget,
      sourceIsDirectory: source.sourceStat.isDirectory(),
      before: { exists: false },
    },
    link: manifestLink(source, "bridge", hashBuffer(Buffer.from(desiredTarget, "utf8"))),
  };
}

function fileAction(destination, content, mode, destinationStat) {
  return {
    kind: "file",
    destination,
    content,
    mode: mode & 0o777,
    before: destinationStat
      ? {
        exists: true,
        content: fs.readFileSync(destination),
        mode: destinationStat.mode & 0o777,
        dev: destinationStat.dev,
        ino: destinationStat.ino,
        nlink: destinationStat.nlink,
      }
      : { exists: false },
  };
}

function manifestLink(source, ownership, hash) {
  return {
    destination: source.destination,
    kind: source.kind,
    ownership,
    policy: source.policy,
    source: source.sourceRelative,
    target: source.source,
    hash,
  };
}

function validatePreviousLinks(previous, sources) {
  if (!previous) return;
  const sourcesByDestination = new Map(sources.map((source) => [source.destination, source]));
  const expectedDestinations = sources
    .filter((source) => previous.version === MANIFEST_VERSION || source.policy !== "runtime")
    .map((source) => source.destination)
    .sort();
  const actualDestinations = previous.links
    .map((entry) => entry?.destination)
    .sort();
  if (
    actualDestinations.some((destination) => typeof destination !== "string")
    || JSON.stringify(actualDestinations) !== JSON.stringify(expectedDestinations)
  ) {
    const label = previous.version === LEGACY_MANIFEST_VERSION ? "legacy host bridge" : "host bridge";
    throw new Error(`blocked: invalid ${label} manifest: link set changed`);
  }
  for (const entry of previous.links) {
    const source = sourcesByDestination.get(entry.destination);
    if (!source || (previous.version === LEGACY_MANIFEST_VERSION && source.policy === "runtime")) {
      throw new Error("blocked: invalid host bridge manifest: unexpected link");
    }
    const keys = Object.keys(entry).sort().join(",");
    if (keys !== "destination,hash,kind,ownership,policy,source,target") {
      throw new Error("blocked: invalid host bridge manifest: invalid link schema");
    }
    assertOldLink(entry, source);
    if (!/^[0-9a-f]{64}$/.test(entry.hash)) {
      throw new Error("blocked: invalid host bridge manifest: invalid link hash");
    }
  }
}

function assertOldLink(entry, source) {
  if (
    entry.destination !== source.destination
    || entry.kind !== source.kind
    || entry.policy !== source.policy
    || entry.source !== source.sourceRelative
    || entry.target !== source.source
    || !["bridge", "tracked", "managed-block", "managed-json"].includes(entry.ownership)
  ) {
    throw new Error(`blocked: invalid host bridge manifest entry: ${source.destination}`);
  }
}

function parseManifest(content, manifestPath, leader, worktree) {
  let value;
  try {
    value = JSON.parse(content.toString("utf8"));
  } catch (error) {
    throw new Error(`blocked: invalid host bridge manifest ${manifestPath}: ${error.message}`);
  }
  const keys = value && typeof value === "object" && !Array.isArray(value)
    ? Object.keys(value).sort().join(",")
    : "";
  if (
    keys !== "leader_root,links,manifest_hash,version,workspace_root"
    || ![LEGACY_MANIFEST_VERSION, MANIFEST_VERSION].includes(value.version)
    || value.leader_root !== leader
    || value.workspace_root !== worktree
    || !Array.isArray(value.links)
    || typeof value.manifest_hash !== "string"
  ) {
    throw new Error(`blocked: invalid host bridge manifest ${manifestPath}: invalid schema`);
  }
  const unsigned = {
    version: value.version,
    leader_root: value.leader_root,
    workspace_root: value.workspace_root,
    links: value.links,
  };
  if (hashBuffer(Buffer.from(canonicalJson(unsigned), "utf8")) !== value.manifest_hash) {
    throw new Error(`blocked: invalid host bridge manifest ${manifestPath}: hash mismatch`);
  }
  return value;
}

function extractFlowBlock(content, file) {
  const text = content.toString("utf8");
  if (!Buffer.from(text, "utf8").equals(content)) {
    throw new Error(`blocked: worktree context is not valid UTF-8: ${file}`);
  }
  const start = text.indexOf(FLOW_START);
  const end = text.indexOf(FLOW_END);
  if (
    start < 0
    || end < start
    || text.indexOf(FLOW_START, start + FLOW_START.length) >= 0
    || text.indexOf(FLOW_END, end + FLOW_END.length) >= 0
  ) {
    throw new Error(`blocked: malformed agent-flow context block in ${file}`);
  }
  return Buffer.from(text.slice(start, end + FLOW_END.length), "utf8");
}

function mergeFlowBlock(current, desiredBlock, file, label) {
  const text = current.toString("utf8");
  if (!Buffer.from(text, "utf8").equals(current)) {
    throw new Error(`blocked: worktree context is not valid UTF-8: ${file}`);
  }
  const start = text.indexOf(FLOW_START);
  const end = text.indexOf(FLOW_END);
  if ((start < 0) !== (end < 0)) {
    throw new Error(`blocked: malformed agent-flow context block in ${file}`);
  }
  if (
    (start >= 0 && end < start)
    || (start >= 0 && text.indexOf(FLOW_START, start + FLOW_START.length) >= 0)
    || (end >= 0 && text.indexOf(FLOW_END, end + FLOW_END.length) >= 0)
  ) {
    throw new Error(`blocked: malformed agent-flow context block in ${file}`);
  }
  if (start >= 0) {
    const block = desiredBlock.toString("utf8");
    return Buffer.from(`${text.slice(0, start)}${block}${text.slice(end + FLOW_END.length)}`, "utf8");
  }
  if (current.length === 0) {
    return Buffer.concat([Buffer.from(`# ${label}\n\n`, "utf8"), desiredBlock, Buffer.from("\n")]);
  }
  let separator;
  if (current.subarray(-4).equals(Buffer.from("\r\n\r\n")) || current.subarray(-2).equals(Buffer.from("\n\n"))) {
    separator = Buffer.alloc(0);
  } else if (current.subarray(-2).equals(Buffer.from("\r\n"))) {
    separator = Buffer.from("\r\n");
  } else if (current.subarray(-1).equals(Buffer.from("\n"))) {
    separator = Buffer.from("\n");
  } else {
    const newline = current.includes(Buffer.from("\r\n")) ? Buffer.from("\r\n") : Buffer.from("\n");
    separator = Buffer.concat([newline, newline]);
  }
  return Buffer.concat([current, separator, desiredBlock, Buffer.from("\n")]);
}

function mergedExclude(current, excludePath) {
  const text = current.toString("utf8");
  if (!Buffer.from(text, "utf8").equals(current)) {
    throw new Error(`blocked: git info exclude is not valid UTF-8: ${excludePath}`);
  }
  const block = [EXCLUDE_START, ...EXCLUDE_PATHS, EXCLUDE_END].join("\n");
  const start = text.indexOf(EXCLUDE_START);
  const end = text.indexOf(EXCLUDE_END);
  if (
    (start < 0) !== (end < 0)
    || (start >= 0 && end < start)
    || (start >= 0 && text.indexOf(EXCLUDE_START, start + EXCLUDE_START.length) >= 0)
    || (end >= 0 && text.indexOf(EXCLUDE_END, end + EXCLUDE_END.length) >= 0)
  ) {
    throw new Error(`blocked: malformed agent-flow host bridge block in ${excludePath}`);
  }
  if (start < 0) {
    const prefix = text.replace(/\s*$/, "");
    return Buffer.from(`${prefix}${prefix ? "\n\n" : ""}${block}\n`, "utf8");
  }
  let next = `${text.slice(0, start)}${block}${text.slice(end + EXCLUDE_END.length)}`;
  if (!next.endsWith("\n")) next += "\n";
  return Buffer.from(next, "utf8");
}

function parseHookConfig(content, file) {
  let value;
  try {
    value = JSON.parse(content.toString("utf8"));
  } catch (error) {
    throw new Error(`blocked: invalid hook settings JSON ${file}: ${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`blocked: invalid hook settings JSON ${file}: expected an object`);
  }
  return value;
}

function managedHookEntries(config, file, hookPolicy, requireManaged = true) {
  if (config.hooks === undefined) {
    if (requireManaged) throw new Error(`blocked: leader hook bridge source has no managed hooks: ${file}`);
    return [];
  }
  if (!config.hooks || typeof config.hooks !== "object" || Array.isArray(config.hooks)) {
    throw new Error(`blocked: invalid hook settings schema: ${file}`);
  }
  const result = [];
  const managedMatchers = new Set();
  for (const [event, entries] of Object.entries(config.hooks)) {
    if (!Array.isArray(entries)) throw new Error(`blocked: invalid hook settings schema: ${file}`);
    for (const entry of entries) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry) || !Array.isArray(entry.hooks)) {
        throw new Error(`blocked: invalid hook settings schema: ${file}`);
      }
      const managed = [];
      for (const hook of entry.hooks) {
        if (!hook || typeof hook !== "object" || Array.isArray(hook)) {
          throw new Error(`blocked: invalid hook settings schema: ${file}`);
        }
        const candidateName = managedHookScriptName(hook.command);
        const trusted = trustedManagedHookCommand(hookPolicy, hook.command);
        if (!trusted) {
          if (candidateName) {
            throw new Error(`blocked: managed hook command is not immutable in ${file}`);
          }
          continue;
        }
        if (hook.type !== "command") {
          throw new Error(`blocked: invalid managed hook type in ${file}`);
        }
        managed.push(JSON.parse(JSON.stringify(hook)));
      }
      if (managed.length === 0) continue;
      const matcher = typeof entry.matcher === "string" ? entry.matcher : "";
      const matcherKey = `${event}\u0000${matcher}`;
      if (managedMatchers.has(matcherKey)) {
        throw new Error(`blocked: duplicate managed hook matcher in ${file}`);
      }
      managedMatchers.add(matcherKey);
      result.push({ event, matcher, hooks: managed });
    }
  }
  if (requireManaged && result.length === 0) {
    throw new Error(`blocked: leader hook bridge source has no managed hooks: ${file}`);
  }
  return result;
}

function withoutManagedHooks(config, hookPolicy) {
  const clean = JSON.parse(JSON.stringify(config));
  if (!clean.hooks || typeof clean.hooks !== "object" || Array.isArray(clean.hooks)) return clean;
  for (const [event, entries] of Object.entries(clean.hooks)) {
    const remainingEntries = [];
    for (const entry of entries) {
      const hooks = entry.hooks.filter((hook) => {
        const candidateName = managedHookScriptName(hook.command);
        const trusted = trustedManagedHookCommand(hookPolicy, hook.command);
        if (candidateName && !trusted) {
          throw new Error("blocked: managed hook command is not immutable");
        }
        return !trusted;
      });
      const otherKeys = Object.keys(entry).filter((key) => !["matcher", "hooks"].includes(key));
      if (hooks.length === 0 && otherKeys.length === 0) continue;
      remainingEntries.push({ ...entry, hooks });
    }
    if (remainingEntries.length > 0) clean.hooks[event] = remainingEntries;
    else delete clean.hooks[event];
  }
  if (Object.keys(clean.hooks).length === 0) delete clean.hooks;
  return clean;
}

function mergeManagedHooks(current, managedEntries, hookPolicy) {
  const desired = withoutManagedHooks(current, hookPolicy);
  if (!desired.hooks) desired.hooks = {};
  for (const managed of managedEntries) {
    if (!desired.hooks[managed.event]) desired.hooks[managed.event] = [];
    let entry = desired.hooks[managed.event].find(
      (candidate) => (typeof candidate.matcher === "string" ? candidate.matcher : "") === managed.matcher,
    );
    if (!entry) {
      entry = { ...(managed.matcher ? { matcher: managed.matcher } : {}), hooks: [] };
      desired.hooks[managed.event].push(entry);
    }
    entry.hooks.push(...managed.hooks.map((hook) => JSON.parse(JSON.stringify(hook))));
  }
  return desired;
}

function managedHookScriptName(command) {
  if (typeof command !== "string") return "";
  for (const match of command.matchAll(/(?:^|[\s'"])([A-Za-z0-9+/]+={0,2})(?=$|[\s'"])/g)) {
    const encoded = match[1];
    const decoded = Buffer.from(encoded, "base64");
    if (decoded.toString("base64") === encoded) {
      const decodedPath = decoded.toString("utf8");
      if (Buffer.from(decodedPath, "utf8").equals(decoded)) {
        const scriptName = path.basename(decodedPath);
        if (MANAGED_HOOK_SCRIPTS.has(scriptName)) return scriptName;
      }
    }
  }
  if (
    command.includes("AGENT_FLOW_MANAGED_HOOK_SOURCE_PATH")
    && command.includes("agent-flow-managed-hook-")
    && command.includes("descriptor execution unavailable")
  ) return "__managed-verifier__";
  const normalized = normalizedHookCommand(command).replaceAll("'", "").replaceAll('"', "");
  for (const scriptName of MANAGED_HOOK_SCRIPTS) {
    if (
      normalized.includes(`/.agent-flow/scripts/hooks/${scriptName}`)
      || normalized.includes(`/scripts/hooks/${scriptName}`)
    ) return scriptName;
  }
  return "";
}

function normalizedHookCommand(command) {
  let normalized = String(command).trim();
  if (normalized.startsWith("'") && normalized.endsWith("'")) {
    normalized = normalized.slice(1, -1).replaceAll("'\\''", "'");
  } else if (normalized.startsWith('"') && normalized.endsWith('"')) {
    normalized = normalized.slice(1, -1);
  }
  return normalized.replaceAll("\\", "/");
}

function applyAction(action) {
  assertActionUnchanged(action);
  if (action.kind === "file") {
    if (action.before.exists) replaceFileNoClobber(action);
    else createFileExclusive(action.destination, action.content, action.mode);
    return;
  }
  fs.symlinkSync(action.target, action.destination, action.sourceIsDirectory ? "dir" : "file");
}

function appliedActionSnapshot(action) {
  const current = lstatIfExists(action.destination);
  if (action.kind === "symlink") {
    if (!current?.isSymbolicLink() || fs.readlinkSync(action.destination) !== action.target) {
      throw new Error(`blocked: host bridge symlink changed after publish: ${action.destination}`);
    }
    return {
      kind: "symlink",
      target: action.target,
      dev: current.dev,
      ino: current.ino,
      nlink: current.nlink,
    };
  }
  const snapshot = snapshotPlainFile(action.destination, "published host bridge file");
  if (!snapshot.content.equals(action.content) || snapshot.mode !== action.mode) {
    throw new Error(`blocked: host bridge file changed after publish: ${action.destination}`);
  }
  return snapshot;
}

function assertActionUnchanged(action) {
  const current = lstatIfExists(action.destination);
  if (!action.before.exists) {
    if (current) throw new Error(`blocked: host bridge path changed during creation: ${action.destination}`);
    return;
  }
  if (!current?.isFile() || current.isSymbolicLink()) {
    throw new Error(`blocked: host bridge path changed during refresh: ${action.destination}`);
  }
  if (
    !fs.readFileSync(action.destination).equals(action.before.content)
    || (current.mode & 0o777) !== action.before.mode
    || (Number.isInteger(action.before.dev) && current.dev !== action.before.dev)
    || (Number.isInteger(action.before.ino) && current.ino !== action.before.ino)
    || (Number.isInteger(action.before.nlink) && current.nlink !== action.before.nlink)
  ) {
    throw new Error(`blocked: host bridge path changed during refresh: ${action.destination}`);
  }
}

function replaceFileNoClobber(action) {
  const directory = path.dirname(action.destination);
  const token = crypto.randomBytes(8).toString("hex");
  const replacement = path.join(directory, `.${path.basename(action.destination)}.bridge-new-${token}`);
  const displaced = path.join(directory, `.${path.basename(action.destination)}.bridge-old-${token}`);
  fs.writeFileSync(replacement, action.content, { mode: action.mode & 0o777, flag: "wx" });
  fs.chmodSync(replacement, action.mode & 0o777);
  try {
    assertActionUnchanged(action);
    fs.renameSync(action.destination, displaced);
    const previous = snapshotPlainFile(displaced, "displaced host bridge file");
    const matches = previous.content.equals(action.before.content)
      && previous.mode === action.before.mode
      && (!Number.isInteger(action.before.dev) || previous.dev === action.before.dev)
      && (!Number.isInteger(action.before.ino) || previous.ino === action.before.ino)
      && (!Number.isInteger(action.before.nlink) || previous.nlink === action.before.nlink);
    if (!matches) {
      if (!lstatIfExists(action.destination)) fs.linkSync(displaced, action.destination);
      throw new Error(`blocked: host bridge path changed during refresh: ${action.destination}`);
    }
    try {
      fs.linkSync(replacement, action.destination);
    } catch (error) {
      if (error?.code === "EEXIST") {
        throw new Error(`blocked: host bridge path changed during publish: ${action.destination}`);
      }
      throw error;
    }
  } finally {
    fs.rmSync(replacement, { force: true });
    fs.rmSync(displaced, { force: true });
  }
}

function rollbackAction(action, errors) {
  try {
    const current = lstatIfExists(action.destination);
    const applied = action.applied;
    if (!applied) return;
    if (action.kind === "symlink") {
      if (
        current?.isSymbolicLink()
        && fs.readlinkSync(action.destination) === applied.target
        && current.dev === applied.dev
        && current.ino === applied.ino
        && current.nlink === applied.nlink
      ) {
        fs.unlinkSync(action.destination);
      }
      return;
    }
    if (!snapshotMatches(action.destination, applied)) return;
    if (action.before.exists) {
      replaceFileNoClobber({
        kind: "file",
        destination: action.destination,
        content: action.before.content,
        mode: action.before.mode,
        before: applied,
      });
    } else {
      removeFileNoClobber(action.destination, applied);
    }
  } catch (error) {
    errors.push(`${action.destination}: ${error.message}`);
  }
}

function snapshotPlainFile(file, label) {
  const stat = lstatIfExists(file);
  if (!stat) return { exists: false, mode: 0o644, content: null };
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`blocked: unsafe ${label}: ${file}`);
  }
  return {
    exists: true,
    mode: stat.mode & 0o777,
    content: fs.readFileSync(file),
    dev: stat.dev,
    ino: stat.ino,
    nlink: stat.nlink,
  };
}

function assertSnapshotUnchanged(file, snapshot, label) {
  if (!snapshotMatches(file, snapshot)) {
    throw new Error(`blocked: ${label} changed during host bridge update: ${file}`);
  }
}

function publishSnapshotUpdate(file, content, snapshot, label) {
  const action = {
    kind: "file",
    destination: file,
    content,
    mode: snapshot.mode ?? 0o644,
    before: snapshot,
  };
  assertSnapshotUnchanged(file, snapshot, label);
  if (snapshot.exists) replaceFileNoClobber(action);
  else createFileExclusive(file, content, action.mode);
  return snapshotPlainFile(file, label);
}

function restoreSnapshot(file, snapshot, appliedSnapshot, errors) {
  try {
    if (!snapshotMatches(file, appliedSnapshot)) return;
    if (snapshot.exists) {
      publishSnapshotUpdate(file, snapshot.content, appliedSnapshot, "host bridge rollback file");
    } else {
      removeFileNoClobber(file, appliedSnapshot);
    }
  } catch (error) {
    errors.push(`${file}: ${error.message}`);
  }
}

function snapshotMatches(file, snapshot) {
  const current = lstatIfExists(file);
  if (!snapshot.exists) return current === null;
  return Boolean(
    current?.isFile()
    && !current.isSymbolicLink()
    && fs.readFileSync(file).equals(snapshot.content)
    && (current.mode & 0o777) === snapshot.mode
    && (!Number.isInteger(snapshot.dev) || current.dev === snapshot.dev)
    && (!Number.isInteger(snapshot.ino) || current.ino === snapshot.ino)
    && (!Number.isInteger(snapshot.nlink) || current.nlink === snapshot.nlink)
  );
}

function removeFileNoClobber(file, snapshot) {
  const displaced = `${file}.agent-flow-remove-${process.pid}-${crypto.randomBytes(6).toString("hex")}.tmp`;
  assertSnapshotUnchanged(file, snapshot, "host bridge rollback file");
  fs.renameSync(file, displaced);
  try {
    if (!snapshotMatches(displaced, snapshot)) {
      if (!lstatIfExists(file)) fs.linkSync(displaced, file);
      throw new Error(`blocked: host bridge rollback file changed during removal: ${file}`);
    }
  } finally {
    fs.rmSync(displaced, { force: true });
  }
}

function bufferEqualsSnapshot(content, snapshot) {
  return snapshot.exists && content.equals(snapshot.content);
}

function assertRegisteredWorktree(leader, worktree) {
  const listed = gitOutput(leader, ["worktree", "list", "--porcelain"]);
  const registered = listed.split(/\n\n+/).some((block) => {
    const value = block.split(/\r?\n/).find((line) => line.startsWith("worktree "))?.slice(9);
    return value && sameExistingPath(value, worktree);
  });
  if (!registered) {
    throw new Error(`blocked: host bridge target is not a registered git worktree: ${worktree}`);
  }
}

function trackedPaths(worktree, relative) {
  const result = spawnSync("git", ["ls-files", "--", relative], {
    cwd: worktree,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error || result.status !== 0) {
    throw new Error((result.stderr || result.error?.message || `git ls-files failed for ${relative}`).trim());
  }
  return result.stdout.split(/\r?\n/).filter(Boolean);
}

function assertSafeSource(leader, source, expectedType) {
  const relative = path.relative(leader, source);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`blocked: leader host bridge source escapes the leader checkout: ${source}`);
  }
  let current = leader;
  const parts = relative.split(path.sep).filter(Boolean);
  for (let index = 0; index < parts.length; index += 1) {
    current = path.join(current, parts[index]);
    const stat = lstatIfExists(current);
    if (!stat) throw new Error(`blocked: leader host bridge source is missing: ${source}`);
    if (stat.isSymbolicLink()) throw new Error(`blocked: unsafe leader host bridge source symlink: ${current}`);
    if (index < parts.length - 1 && !stat.isDirectory()) {
      throw new Error(`blocked: unsafe leader host bridge source parent: ${current}`);
    }
  }
  const stat = fs.lstatSync(source);
  const valid = expectedType === "file"
    ? stat.isFile()
    : expectedType === "directory" && stat.isDirectory();
  if (!valid) {
    throw new Error(`blocked: invalid leader host bridge source type: ${source}`);
  }
  return stat;
}

function assertSafeParents(boundary, destination) {
  const relative = path.relative(boundary, path.dirname(destination));
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`blocked: host bridge destination escapes its boundary: ${destination}`);
  }
  let current = boundary;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    const stat = lstatIfExists(current);
    if (stat && (stat.isSymbolicLink() || !stat.isDirectory())) {
      throw new Error(`blocked: unsafe host bridge parent path: ${current}`);
    }
  }
}

function ensureSafeParentDirectories(boundary, destination, created) {
  assertSafeParents(boundary, destination);
  const relative = path.relative(boundary, path.dirname(destination));
  let current = boundary;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    const stat = lstatIfExists(current);
    if (stat) continue;
    try {
      fs.mkdirSync(current);
      created.push(current);
    } catch (error) {
      const raced = lstatIfExists(current);
      if (!raced?.isDirectory() || raced.isSymbolicLink()) throw error;
    }
  }
}

function removeEmptyDirectory(directory, errors) {
  try {
    const stat = lstatIfExists(directory);
    if (stat?.isDirectory() && !stat.isSymbolicLink() && fs.readdirSync(directory).length === 0) {
      fs.rmdirSync(directory);
    }
  } catch (error) {
    errors.push(`${directory}: ${error.message}`);
  }
}

function atomicWriteBuffer(file, content, mode) {
  const temporary = temporaryPath(file);
  fs.writeFileSync(temporary, content, { mode: mode & 0o777, flag: "wx" });
  try {
    fs.renameSync(temporary, file);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

function createFileExclusive(file, content, mode) {
  const temporary = temporaryPath(file);
  fs.writeFileSync(temporary, content, { mode: mode & 0o777, flag: "wx" });
  try {
    fs.linkSync(temporary, file);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

function temporaryPath(file) {
  return `${file}.agent-flow-${process.pid}-${crypto.randomBytes(6).toString("hex")}.tmp`;
}

function relativeLinkTarget(destination, source) {
  return path.relative(path.dirname(destination), source) || ".";
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function hashBuffer(content) {
  return crypto.createHash("sha256").update(content).digest("hex");
}

function resolveGitPath(cwd, value) {
  return fs.realpathSync.native(path.resolve(cwd, value));
}

function gitOutput(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error || result.status !== 0) {
    throw new Error((result.stderr || result.error?.message || `git ${args.join(" ")} failed`).trim());
  }
  return result.stdout.trim();
}

function normalizedRelative(parent, child) {
  return path.relative(parent, child).split(path.sep).join("/");
}

function sameExistingPath(left, right) {
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    return path.resolve(left) === path.resolve(right);
  }
}

function lstatIfExists(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}
