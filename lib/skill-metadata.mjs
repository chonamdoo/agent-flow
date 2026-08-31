import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { parseSimpleYaml, splitFrontmatter } from "./frontmatter.mjs";
import { arrayValue, safeSkillName, uniqueStrings } from "./installer-shared.mjs";

const CONTENT_EXCLUDED_NAMES = new Set([
  ".agent-flow",
  ".git",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  ".venv",
  "__pycache__",
  "node_modules",
]);

const INVALID_GOVERNANCE_SCALAR = "<invalid-structured-value>";

export function parseSkillMetadata(text, fallbackName, projectSkillHosts, source = "") {
  const frontmatter = splitFrontmatter(text);
  const metadata = frontmatter ? parseSimpleYaml(frontmatter) : {};
  const warnings = [];
  const parsedName = String(metadata.name || fallbackName);
  const name = safeSkillName(parsedName);
  if (name !== parsedName) {
    warnings.push(`unsafe skill name ignored: ${parsedName}`);
  }
  const hostValues = Array.isArray(metadata.hosts) ? metadata.hosts : [];
  const knownHosts = new Set(projectSkillHosts);
  const hosts = [];
  for (const host of hostValues) {
    const normalized = String(host).trim().toLowerCase();
    if (knownHosts.has(normalized)) {
      hosts.push(normalized);
    } else if (normalized) {
      warnings.push(`unknown host ignored: ${normalized}`);
    }
  }
  const body = text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
  const useWhen = body.split(/\r?\n/).find((line) => /^\s*use when\b/i.test(line));
  return {
    id: String(metadata.id || name),
    name,
    title: String(metadata.title || ""),
    description: String(metadata.description || useWhen || ""),
    hosts: hostValues.length > 0 ? [...new Set(hosts)] : [...projectSkillHosts],
    tags: Array.isArray(metadata.tags) ? metadata.tags.map(String) : [],
    trigger: String(metadata.trigger || metadata.description || useWhen || ""),
    triggers: arrayValue(metadata.triggers),
    platforms: arrayValue(metadata.platforms),
    stacks: arrayValue(metadata.stacks),
    dependencies: uniqueStrings([
      ...arrayValue(metadata.dependencies),
      ...arrayValue(metadata.requires),
    ]),
    requires: uniqueStrings([
      ...arrayValue(metadata.dependencies),
      ...arrayValue(metadata.requires),
    ]),
    optionalDependencies: arrayValue(metadata.optionalDependencies),
    references: arrayValue(metadata.references),
    hostSupport: arrayValue(metadata.hostSupport),
    workflowPhases: arrayValue(metadata.workflowPhases),
    reviewAngles: arrayValue(metadata.reviewAngles),
    installGroup: String(metadata.installGroup || ""),
    delivery: String(metadata.delivery || "on-demand"),
    excludes: arrayValue(metadata.excludes || metadata.conflicts),
    governance: {
      version: governanceScalar(metadata.version, "", frontmatter, "version"),
      owner: governanceScalar(metadata.owner, "", frontmatter, "owner"),
      lifecycle: governanceScalar(metadata.lifecycle, "active", frontmatter, "lifecycle").toLowerCase(),
      approval: governanceScalar(metadata.approval, "unattested", frontmatter, "approval").toLowerCase(),
      provenance: governanceScalar(
        metadata.provenance,
        canonicalProvenance(source),
        frontmatter,
        "provenance",
      ),
    },
    warnings,
  };
}

export function skillObservedContentDigest(skillDirectory) {
  const root = fs.realpathSync(skillDirectory);
  const files = walkRegularFiles(root)
    .map((filePath) => ({
      filePath,
      relativePath: path.relative(root, filePath).split(path.sep).join("/"),
    }))
    .sort((left, right) =>
      Buffer.compare(Buffer.from(left.relativePath), Buffer.from(right.relativePath)),
    );
  const hash = crypto.createHash("sha256");
  const readBuffer = Buffer.allocUnsafe(1024 * 1024);
  for (const { filePath, relativePath } of files) {
    hash.update(relativePath);
    hash.update("\0");
    hash.update(fileDigest(filePath, readBuffer));
    hash.update("\0");
  }
  return hash.digest("hex");
}

export function observeSkillContent(skillDirectory) {
  try {
    return { digest: skillObservedContentDigest(skillDirectory), warning: "" };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { digest: "", warning: `content digest unavailable: ${message}` };
  }
}

function walkRegularFiles(target) {
  const info = fs.lstatSync(target);
  if (info.isSymbolicLink() || info.isFile()) {
    return [target];
  }
  if (!info.isDirectory()) {
    return [];
  }
  const files = [];
  const entries = fs.readdirSync(target, { withFileTypes: true });
  for (const entry of entries) {
    if (CONTENT_EXCLUDED_NAMES.has(entry.name) || entry.name.endsWith(".pyc")) {
      continue;
    }
    const child = path.join(target, entry.name);
    if (entry.isSymbolicLink() || entry.isFile()) {
      files.push(child);
    } else if (entry.isDirectory()) {
      files.push(...walkRegularFiles(child));
    }
  }
  return files;
}

function fileDigest(filePath, readBuffer) {
  if (fs.lstatSync(filePath).isSymbolicLink()) {
    return crypto
      .createHash("sha256")
      .update("symlink\0")
      .update(fs.readlinkSync(filePath, { encoding: "buffer" }))
      .digest("hex");
  }
  const hash = crypto.createHash("sha256");
  const descriptor = fs.openSync(filePath, "r");
  try {
    let size;
    while ((size = fs.readSync(descriptor, readBuffer, 0, readBuffer.length, null)) > 0) {
      hash.update(readBuffer.subarray(0, size));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest("hex");
}

function governanceScalar(value, fallback, frontmatter, key) {
  const kind = governanceValueKind(frontmatter, key);
  if (kind === "structured") {
    return INVALID_GOVERNANCE_SCALAR;
  }
  if (kind === "blank" || value === null || value === undefined || value === "") {
    return fallback;
  }
  if (!["string", "number", "boolean"].includes(typeof value)) {
    return INVALID_GOVERNANCE_SCALAR;
  }
  return String(value).trim() || fallback;
}

function governanceValueKind(frontmatter, key) {
  if (!frontmatter) {
    return "absent";
  }
  const lines = frontmatter.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match || match[1] !== key) {
      continue;
    }
    const rawText = match[2].trim();
    const raw = rawText.startsWith("#") ? "" : rawText;
    if (raw.startsWith("[") || raw.startsWith("{")) {
      return "structured";
    }
    if (raw !== "") {
      return "scalar";
    }
    for (let nested = index + 1; nested < lines.length; nested += 1) {
      if (lines[nested].trim() === "" || /^\s*#/.test(lines[nested])) {
        continue;
      }
      return /^\s/.test(lines[nested]) ? "structured" : "blank";
    }
    return "blank";
  }
  return "absent";
}

function canonicalProvenance(source) {
  return source === "project-local" ? "local" : source;
}
