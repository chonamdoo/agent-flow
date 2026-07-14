#!/usr/bin/env node
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

function validateSkill(file) {
  const content = readUtf8(file);
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
