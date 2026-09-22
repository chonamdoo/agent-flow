import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

function setup(t) {
  const root = mkdtempSync(path.join(tmpdir(), 'skill-noop-validate-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const harness = path.join(root, 'tools', 'skill-noop');
  const cases = path.join(harness, 'cases');
  const emptyPath = path.join(root, 'empty-bin');
  mkdirSync(cases, { recursive: true });
  mkdirSync(emptyPath);
  for (const file of ['run.mjs', 'prompt.md']) copyFileSync(path.join(HERE, file), path.join(harness, file));
  writeFileSync(path.join(root, 'baseline.txt'), 'Review policy\nStable anchor\nReject unsafe changes\n');
  return {
    cases,
    validate(...selection) {
      return spawnSync(process.execPath, [path.join(harness, 'run.mjs'), ...selection, '--validate'], {
        cwd: root,
        env: { PATH: emptyPath, HOME: root },
        encoding: 'utf8',
        timeout: 10_000,
      });
    },
  };
}

function addCase(cases, id, overrides = {}) {
  const dir = path.join(cases, id);
  mkdirSync(dir);
  writeFileSync(
    path.join(dir, 'case.json'),
    JSON.stringify({
      kind: 'addition',
      baseline: 'baseline.txt',
      variant: { insertAfter: 2, expectAnchor: 'Stable anchor', text: ['Check unsafe changes'] },
      fixtures: [{ name: 'unsafe', file: 'fixture.diff', expect: 'request-changes' }],
      patterns: { unsafe: 'unsafe' },
      target: 'unsafe',
      ...overrides,
    }),
  );
  writeFileSync(path.join(dir, 'fixture.diff'), '--- a/input.js\n+++ b/input.js\n@@ -1 +1 @@\n-safe();\n+unsafe();\n');
}

function assertNoResults(cases) {
  for (const id of readdirSync(cases)) {
    assert.deepEqual(
      readdirSync(path.join(cases, id)).filter((file) => file.startsWith('results-')),
      [],
      `${id} must not create measurement results during validation`,
    );
  }
}

test('--all --validate accepts addition and deletion cases without provider CLIs or result files', (t) => {
  const repo = setup(t);
  addCase(repo.cases, 'addition');
  addCase(repo.cases, 'deletion', {
    kind: 'deletion',
    variant: { deleteLines: [3, 3], expectRemoved: ['Reject unsafe changes'] },
    forbidden: ['unsafe'],
  });

  const result = repo.validate('--all');

  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assertNoResults(repo.cases);
});

test('--case --validate validates only the selected case', (t) => {
  const repo = setup(t);
  addCase(repo.cases, 'selected');
  addCase(repo.cases, 'unselected-invalid', { patterns: { unsafe: '[' } });

  const result = repo.validate('--case', 'selected');

  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assertNoResults(repo.cases);
});

for (const scenario of [
  {
    name: 'stale line numbers even when the anchor exists elsewhere',
    overrides: { variant: { insertAfter: 1, expectAnchor: 'Stable anchor', text: ['Check unsafe changes'] } },
    diagnostic: /line 1 .*expected "Stable anchor"/,
  },
  {
    name: 'expectRemoved that does not match the selected line',
    overrides: {
      kind: 'deletion',
      variant: { deleteLines: [3, 3], expectRemoved: ['Different policy'] },
      forbidden: ['unsafe'],
    },
    diagnostic: /line 3 .*expected "Different policy"/,
  },
  {
    name: 'an invalid topic regex',
    overrides: { patterns: { unsafe: '[' } },
    diagnostic: /pattern unsafe does not compile/,
  },
  {
    name: 'a missing fixture',
    overrides: { fixtures: [{ name: 'missing', file: 'missing.diff', expect: 'approve' }] },
    diagnostic: /missing fixture file missing\.diff/,
  },
]) {
  test(`--all --validate rejects ${scenario.name} before any case executes`, (t) => {
    const repo = setup(t);
    addCase(repo.cases, 'a-valid');
    addCase(repo.cases, 'z-invalid', scenario.overrides);

    const result = repo.validate('--all');

    assert.ifError(result.error);
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, scenario.diagnostic);
    assertNoResults(repo.cases);
  });
}
