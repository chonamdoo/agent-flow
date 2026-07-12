#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const root = process.cwd();
const skillsDir = path.join(root, 'skills');
const installedDir = path.join(root, '.agent-flow', 'skills');
const errors = [];
const warnings = [];

function rel(file) {
  return path.relative(root, file).split(path.sep).join('/');
}

function readUtf8(file) {
  return fs.readFileSync(file, 'utf8');
}

function parseFrontmatter(file, content) {
  const match = content.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!match) {
    errors.push(`${rel(file)}: missing YAML frontmatter`);
    return {};
  }

  const lines = match[1].split('\n');
  const data = {};
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const keyValue = line.match(/^([A-Za-z0-9_-]+):(?:\s*(.*))?$/);
    if (!keyValue) continue;

    const [, key, rawValue = ''] = keyValue;
    if (rawValue === '|' || rawValue === '>') {
      const block = [];
      while (i + 1 < lines.length && /^\s+/.test(lines[i + 1])) {
        i += 1;
        block.push(lines[i].replace(/^\s{2}/, ''));
      }
      data[key] = rawValue === '>' ? block.join(' ').replace(/\s+/g, ' ').trim() : block.join('\n').trim();
      continue;
    }

    data[key] = rawValue.trim().replace(/^['"]|['"]$/g, '');
  }
  return data;
}

function firstSentence(description) {
  const match = description.trim().match(/^[^.!?]+[.!?]?/);
  return match ? match[0].trim() : description.trim();
}

function validateLinks(file, content) {
  const dir = path.dirname(file);
  const markdownLinks = content.matchAll(/\[[^\]]+\]\(([^)]+)\)/g);
  for (const [, targetRaw] of markdownLinks) {
    if (/^(?:https?:|mailto:|#)/.test(targetRaw)) continue;
    const target = targetRaw.split('#')[0];
    if (!target) continue;
    const resolved = path.resolve(dir, target);
    if (!fs.existsSync(resolved)) {
      errors.push(`${rel(file)}: broken Markdown link ${targetRaw}`);
    }
  }
}

function collectSkillFiles() {
  if (!fs.existsSync(skillsDir)) {
    errors.push('skills/: directory not found');
    return [];
  }
  return fs.readdirSync(skillsDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(skillsDir, entry.name, 'SKILL.md'))
    .filter((file) => fs.existsSync(file))
    .sort();
}

function skillTreeHash(directory) {
  const files = [];
  const pending = [directory];
  while (pending.length > 0) {
    const current = pending.pop();
    const currentStat = fs.lstatSync(current);
    if (currentStat.isSymbolicLink() || !currentStat.isDirectory()) {
      throw new Error(`skill source must be a regular directory: ${rel(current)}`);
    }
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const candidate = path.join(current, entry.name);
      if (entry.isSymbolicLink()) {
        throw new Error(`skill source may not contain symlinks: ${rel(candidate)}`);
      }
      if (entry.isDirectory()) pending.push(candidate);
      else if (entry.isFile()) files.push(candidate);
      else throw new Error(`skill source must contain only regular files: ${rel(candidate)}`);
    }
  }
  const digest = crypto.createHash('sha256');
  for (const file of files.sort()) {
    digest.update(path.relative(directory, file).split(path.sep).join('/'));
    digest.update('\0');
    digest.update(fs.readFileSync(file));
    digest.update('\0');
  }
  return digest.digest('hex');
}

function validateUpstreamLock() {
  const lockPath = path.join(skillsDir, 'upstream-lock.json');
  if (!fs.existsSync(lockPath)) return;
  let lock;
  try {
    lock = JSON.parse(readUtf8(lockPath));
  } catch (error) {
    errors.push(`${rel(lockPath)}: invalid JSON: ${error.message}`);
    return;
  }
  const exactCopies = lock?.exact_copies;
  const localAdaptations = lock?.local_adaptations;
  const projectTreeHashes = lock?.project_tree_hashes;
  if (
    lock?.whole_tree_required !== true
    || !exactCopies
    || typeof exactCopies !== 'object'
    || Array.isArray(exactCopies)
    || !localAdaptations
    || typeof localAdaptations !== 'object'
    || Array.isArray(localAdaptations)
    || !projectTreeHashes
    || typeof projectTreeHashes !== 'object'
    || Array.isArray(projectTreeHashes)
  ) {
    errors.push(`${rel(lockPath)}: invalid whole-tree project snapshot lock`);
    return;
  }
  const exactNames = Object.keys(exactCopies);
  const adaptedNames = Object.keys(localAdaptations);
  const lockedNames = [...exactNames, ...adaptedNames].sort();
  const hashNames = Object.keys(projectTreeHashes).sort();
  if (exactNames.some((name) => adaptedNames.includes(name))) {
    errors.push(`${rel(lockPath)}: exact copies and local adaptations overlap`);
  }
  if (JSON.stringify(lockedNames) !== JSON.stringify(hashNames)) {
    errors.push(`${rel(lockPath)}: project_tree_hashes must cover every tracked Matt skill exactly`);
    return;
  }
  for (const name of lockedNames) {
    const expected = projectTreeHashes[name];
    const directory = path.join(skillsDir, name);
    if (!/^[A-Za-z0-9._-]+$/.test(name) || !/^[0-9a-f]{64}$/.test(expected)) {
      errors.push(`${rel(lockPath)}: invalid project tree hash for ${name}`);
      continue;
    }
    if (!fs.existsSync(path.join(directory, 'SKILL.md'))) {
      errors.push(`${rel(lockPath)}: missing tracked project snapshot ${name}`);
      continue;
    }
    try {
      if (skillTreeHash(directory) !== expected) {
        errors.push(`${rel(lockPath)}: tracked project snapshot changed: ${name}`);
      }
    } catch (error) {
      errors.push(`${rel(lockPath)}: ${error.message}`);
    }
  }
  validateAndroidOfficialLock(lock, lockPath);
}

function validateAndroidOfficialLock(lock, lockPath) {
  const official = lock?.android_official;
  if (official === undefined) return;
  const snapshots = official?.snapshots;
  if (
    official?.source !== 'https://github.com/android/skills'
    || !/^[0-9a-f]{40}$/.test(official?.commit || '')
    || official?.runtime_fetch !== false
    || official?.catalog !== 'profiles/android.yaml#android_skills.implementation'
    || official?.runtime_tree_verification !== 'installed-index'
    || !snapshots
    || typeof snapshots !== 'object'
    || Array.isArray(snapshots)
  ) {
    errors.push(`${rel(lockPath)}: invalid Android official skill lock`);
    return;
  }
  const sourcePolicyPath = path.join(skillsDir, 'source-policy.yaml');
  const officialPolicy = fs.existsSync(sourcePolicyPath)
    ? yamlMapSection(readUtf8(sourcePolicyPath), 'official_project_snapshots')
    : {};
  if (
    officialPolicy.source !== official.source
    || officialPolicy.commit !== official.commit
    || officialPolicy.catalog !== official.catalog
    || officialPolicy.install_policy !== official.policy
    || officialPolicy.runtime_fetch !== 'false'
    || officialPolicy.offline_validation !== 'required'
    || officialPolicy.runtime_tree_verification !== official.runtime_tree_verification
  ) {
    errors.push(`${rel(lockPath)}: Android official source policy does not match lock provenance`);
  }
  const profilePath = path.join(root, 'profiles', 'android.yaml');
  if (!fs.existsSync(profilePath)) {
    errors.push(`${rel(lockPath)}: missing Android profile for official skill lock`);
    return;
  }
  const catalogNames = androidOfficialCatalog(readUtf8(profilePath));
  const snapshotNames = Object.keys(snapshots).map(portableCasefold).sort();
  if (JSON.stringify(catalogNames) !== JSON.stringify(snapshotNames)) {
    errors.push(`${rel(lockPath)}: Android official skill catalog does not match lock coverage`);
    return;
  }
  const licenseReference = official.license_reference;
  const licensePath = typeof licenseReference === 'string'
    ? path.join(skillsDir, licenseReference)
    : '';
  if (
    !safeRelativePath(licenseReference)
    || !/^[0-9a-f]{64}$/.test(official.license_sha256 || '')
    || !fs.existsSync(licensePath)
    || crypto.createHash('sha256').update(fs.readFileSync(licensePath)).digest('hex') !== official.license_sha256
  ) {
    errors.push(`${rel(lockPath)}: Android official license provenance changed`);
  }
  for (const name of snapshotNames) {
    const provenance = snapshots[name];
    const upstreamName = provenance?.upstream_name ?? name;
    if (
      !/^[A-Za-z0-9._-]+$/.test(name)
      || !/^[A-Za-z0-9._-]+$/.test(upstreamName)
      || !safeRelativePath(provenance?.upstream_path)
      || !/^[0-9a-f]{64}$/.test(provenance?.upstream_tree_hash || '')
      || !/^[0-9a-f]{64}$/.test(provenance?.upstream_skill_sha256 || '')
      || !['bundled-adapter', 'install-time-indexed'].includes(provenance?.snapshot_mode)
    ) {
      errors.push(`${rel(lockPath)}: invalid Android official skill provenance: ${name}`);
      continue;
    }
    if (provenance.snapshot_mode === 'install-time-indexed') {
      if (provenance.project_tree_hash !== null) {
        errors.push(`${rel(lockPath)}: invalid Android indexed skill project hash: ${name}`);
      }
      continue;
    }
    const directory = path.join(skillsDir, name);
    if (!/^[0-9a-f]{64}$/.test(provenance.project_tree_hash || '')) {
      errors.push(`${rel(lockPath)}: invalid Android bundled skill project hash: ${name}`);
      continue;
    }
    if (!fs.existsSync(path.join(directory, 'SKILL.md'))) {
      errors.push(`${rel(lockPath)}: missing Android bundled skill snapshot: ${name}`);
      continue;
    }
    try {
      if (skillTreeHash(directory) !== provenance.project_tree_hash) {
        errors.push(`${rel(lockPath)}: Android bundled skill snapshot changed: ${name}`);
      }
    } catch (error) {
      errors.push(`${rel(lockPath)}: ${error.message}`);
    }
  }
}

function androidOfficialCatalog(text) {
  const names = new Set();
  let inCatalog = false;
  let inImplementation = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      inCatalog = line.trim() === 'android_skills:';
      inImplementation = false;
      continue;
    }
    if (!inCatalog) continue;
    if (/^  implementation:\s*$/.test(line)) {
      inImplementation = true;
      continue;
    }
    if (/^  \S/.test(line)) inImplementation = false;
    const match = inImplementation && line.match(/^    - skill:\s*([A-Za-z0-9._-]+)\s*$/);
    if (match) names.add(portableCasefold(match[1]));
  }
  return [...names].sort();
}

function portableCasefold(value) {
  return String(value).normalize('NFKC').toLowerCase().replaceAll('ß', 'ss').replaceAll('ς', 'σ');
}

function safeRelativePath(value) {
  return typeof value === 'string'
    && value.length > 0
    && !path.isAbsolute(value)
    && !value.includes('\\')
    && value.split('/').every((part) => part && part !== '.' && part !== '..');
}

function yamlMapSection(text, section) {
  const values = {};
  let active = false;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      active = line.trim() === `${section}:`;
      continue;
    }
    if (!active) continue;
    const match = line.match(/^\s+([A-Za-z0-9._-]+):\s*(.*?)\s*$/);
    if (!match) continue;
    const raw = match[2].trim();
    values[match[1]] = raw.length >= 2 && (
      (raw.startsWith('"') && raw.endsWith('"'))
      || (raw.startsWith("'") && raw.endsWith("'"))
    )
      ? raw.slice(1, -1)
      : raw.replace(/\s+#.*$/, '').trim();
  }
  return values;
}

function validateSkill(file) {
  const content = readUtf8(file);
  const lines = content.split(/\r?\n/);
  const dirName = path.basename(path.dirname(file));
  const frontmatter = parseFrontmatter(file, content);
  const name = frontmatter.name || '';
  const description = (frontmatter.description || '').replace(/\s+/g, ' ').trim();

  if (name !== dirName) {
    errors.push(`${rel(file)}: frontmatter name "${name}" does not match directory "${dirName}"`);
  }

  if (!description) {
    errors.push(`${rel(file)}: description is empty`);
  } else {
    if (description.length > 1024) {
      errors.push(`${rel(file)}: description is ${description.length} chars; max is 1024`);
    }
    if (!/\bUse when\b/.test(description)) {
      errors.push(`${rel(file)}: description must include "Use when" trigger text`);
    }
    if (/^Use when\b/i.test(firstSentence(description))) {
      errors.push(`${rel(file)}: description should start with capability, not "Use when"`);
    }
  }

  if (lines.length > 200) {
    errors.push(`${rel(file)}: ${lines.length} lines; max is 200, split progressive references`);
  } else if (lines.length > 100) {
    warnings.push(`${rel(file)}: ${lines.length} lines; consider progressive disclosure`);
  }

  if (!/^##\s+Quick start\b/im.test(content) && !/^##\s+When loaded\b/im.test(content)) {
    warnings.push(`${rel(file)}: missing Quick start or When loaded section`);
  }

  validateLinks(file, content);
}

function warnInstalledDrift() {
  if (!fs.existsSync(installedDir)) return;
  for (const file of collectSkillFiles()) {
    const name = path.basename(path.dirname(file));
    const installed = path.join(installedDir, name, 'SKILL.md');
    if (fs.existsSync(installed) && readUtf8(file) !== readUtf8(installed)) {
      warnings.push(`${rel(installed)}: installed copy differs from source ${rel(file)}`);
    }
  }
}

const files = collectSkillFiles();
for (const file of files) validateSkill(file);
validateUpstreamLock();
warnInstalledDrift();

console.log(`Validated ${files.length} source skill(s).`);
if (warnings.length) {
  console.log(`\nWarnings (${warnings.length}):`);
  for (const warning of warnings) console.log(`- ${warning}`);
}
if (errors.length) {
  console.error(`\nErrors (${errors.length}):`);
  for (const error of errors) console.error(`- ${error}`);
  process.exitCode = 1;
} else {
  console.log('\nSkill validation passed.');
}
