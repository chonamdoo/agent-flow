import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
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
  const run = (args) =>
    spawnSync(process.execPath, [path.join(harness, 'run.mjs'), ...args], {
      cwd: root,
      env: { PATH: emptyPath, HOME: root },
      encoding: 'utf8',
      timeout: 10_000,
    });
  return {
    cases,
    validate: (...selection) => run([...selection, '--validate']),
    rescore: (...selection) => run([...selection, '--rescore']),
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

// Every row blocks on the target, so both arms score 12/12 and the decision is NO-OP whatever the
// warning says. `unmatched` variant rows add a blocking finding no pattern names.
function writeResults(cases, id, unmatched) {
  const file = path.join(cases, id, 'results-n12-claude.jsonl');
  const rows = [];
  for (const arm of ['baseline', 'variant'])
    for (let rep = 1; rep <= 12; rep++) {
      const findings = ['- blocking: unsafe call'];
      if (arm === 'variant' && rep <= unmatched) findings.push('- blocking: renamed guard is skipped');
      rows.push({ provider: 'claude', arm, fixture: 'unsafe', rep, code: 0, verdict: 'request-changes', findings, ok: true });
    }
  writeFileSync(file, `${rows.map((r) => JSON.stringify(r)).join('\n')}\n`);
  return file;
}

// 0/12 vs 5/12 is p≈0.037 and 0/12 vs 4/12 is p≈0.093, so these two sit on either side of the
// p < 0.05 line the decision uses.
for (const { unmatched, warns } of [
  { unmatched: 5, warns: true },
  { unmatched: 4, warns: false },
]) {
  test(`--rescore ${warns ? 'warns' : 'stays quiet'} when one arm leaves ${unmatched}/12 rows with unmatched blocking findings against 0/12`, (t) => {
    const repo = setup(t);
    addCase(repo.cases, 'wording');
    const file = writeResults(repo.cases, 'wording', unmatched);
    const before = readFileSync(file, 'utf8');

    const result = repo.rescore('--case', 'wording', '--providers', 'claude');

    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(/WARNING unmatched blocking findings: baseline 0\/12 vs variant \d+\/12/.test(result.stdout), warns, result.stdout);
    assert.equal(/claude\/variant#1 - blocking: renamed guard is skipped/.test(result.stdout), warns, result.stdout);
    assert.match(result.stdout, /with-line 12\/12 vs without-line 12\/12 p=1\.0000 => NO-OP/);
    // Rescoring reads evidence; it must never append to it or leave a new results file behind.
    assert.equal(readFileSync(file, 'utf8'), before);
    assert.deepEqual(
      readdirSync(path.join(repo.cases, 'wording')).filter((name) => name.startsWith('results-')),
      ['results-n12-claude.jsonl'],
    );
  });
}
