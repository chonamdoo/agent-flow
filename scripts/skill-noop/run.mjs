#!/usr/bin/env node
// Measures whether a skill line changes review outcomes.
// deletion case: baseline has the line, variant removes it. The line earns deletion when outcomes match.
// addition case: baseline lacks the line, variant adds it. The line earns addition when it surfaces a miss.
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const CASES = path.join(HERE, 'cases');
const PROMPT = readFileSync(path.join(HERE, 'prompt.md'), 'utf8');

const INVOKE = {
  claude: ['claude', ['-p', '--allowed-tools', 'none']],
  codex: ['codex', ['exec', '--sandbox', 'read-only', '-']],
};

function loadCase(id) {
  const dir = path.join(CASES, id);
  const spec = JSON.parse(readFileSync(path.join(dir, 'case.json'), 'utf8'));
  const baselinePath = path.isAbsolute(spec.baseline) ? spec.baseline : path.join(REPO, spec.baseline);
  if (!existsSync(baselinePath)) throw new Error(`missing skill: ${baselinePath}`);
  for (const f of spec.fixtures) if (!existsSync(path.join(dir, f.file))) throw new Error(`missing fixture: ${f.file}`);
  const kase = { ...spec, id, dir, baselinePath };
  skillText(kase, 'variant');
  return kase;
}

// The variant is derived from the live skill file, never stored as a copy, so a skill edit
// cannot leave a stale duplicate behind. expectRemoved/expectAnchor make a line-number shift
// fail loudly instead of silently measuring the wrong lines.
function skillText(kase, arm) {
  const base = readFileSync(kase.baselinePath, 'utf8');
  if (arm === 'baseline') return base;
  const lines = base.split('\n');
  const v = kase.variant;
  if (v.deleteLines) {
    const [from, to] = v.deleteLines;
    const removed = lines.slice(from - 1, to);
    if (removed.length !== to - from + 1) throw new Error(`${kase.id}: deleteLines out of range`);
    (v.expectRemoved ?? []).forEach((needle, i) => {
      if (!removed[i]?.includes(needle))
        throw new Error(`${kase.id}: line ${from + i} is "${removed[i]}", expected to contain "${needle}"`);
    });
    return [...lines.slice(0, from - 1), ...lines.slice(to)].join('\n');
  }
  if (v.insertAfter != null) {
    if (v.expectAnchor && !lines[v.insertAfter - 1]?.includes(v.expectAnchor))
      throw new Error(`${kase.id}: line ${v.insertAfter} is "${lines[v.insertAfter - 1]}", expected to contain "${v.expectAnchor}"`);
    return [...lines.slice(0, v.insertAfter), ...v.text, ...lines.slice(v.insertAfter)].join('\n');
  }
  throw new Error(`${kase.id}: variant needs deleteLines or insertAfter+text`);
}

const buildPrompt = (kase, arm, fixture) =>
  PROMPT.replace('{{SKILL}}', skillText(kase, arm)).replace(
    '{{DIFF}}',
    readFileSync(path.join(kase.dir, fixture.file), 'utf8'),
  );

function runOne(kase, job) {
  const [bin, args] = INVOKE[job.provider];
  const prompt = buildPrompt(kase, job.arm, kase.fixtures.find((f) => f.name === job.fixture));
  return new Promise((resolve) => {
    const t0 = Date.now();
    const child = spawn(bin, args, { cwd: kase.dir, stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '';
    let err = '';
    const timer = setTimeout(() => child.kill('SIGKILL'), 300_000);
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('close', (code) => {
      clearTimeout(timer);
      const v = [...out.matchAll(/verdict:\s*(approve|request-changes)/gi)];
      resolve({
        ...job,
        code,
        seconds: Math.round((Date.now() - t0) / 1000),
        verdict: v.length ? v.at(-1)[1].toLowerCase() : null,
        stdout: out,
        stderr: err.slice(-1500),
      });
    });
    child.stdin.end(prompt);
  });
}

async function pool(jobs, limit, work) {
  const results = [];
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(limit, jobs.length) }, async () => {
      while (cursor < jobs.length) {
        const r = await work(jobs[cursor++]);
        results.push(r);
        process.stderr.write(
          `[${results.length}/${jobs.length}] ${r.provider}/${r.arm}/${r.fixture}#${r.rep} -> ${r.verdict ?? 'PARSE_FAIL'} (${r.seconds}s)\n`,
        );
      }
    }),
  );
  return results;
}

const blockingLines = (out) =>
  out
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => /^[-*]?\s*\**blocking\**\s*:/i.test(l));

const lfact = (n) => {
  let s = 0;
  for (let i = 2; i <= n; i++) s += Math.log(i);
  return s;
};
const hyp = (a, b, c, d) =>
  Math.exp(
    lfact(a + b) + lfact(c + d) + lfact(a + c) + lfact(b + d) - lfact(a + b + c + d) - lfact(a) - lfact(b) - lfact(c) - lfact(d),
  );
function fisher(a, b, c, d) {
  const obs = hyp(a, b, c, d);
  const n = a + b + c + d;
  let p = 0;
  for (let i = 0; i <= Math.min(a + b, a + c); i++) {
    const j = a + b - i;
    const k = a + c - i;
    const l = n - i - j - k;
    if (j < 0 || k < 0 || l < 0) continue;
    const q = hyp(i, j, k, l);
    if (q <= obs * 1.0000001) p += q;
  }
  return Math.min(1, p);
}

function report(kase, results) {
  const arms = ['baseline', 'variant'];
  const patterns = Object.entries(kase.patterns ?? {}).map(([n, r]) => [n, new RegExp(r, 'i')]);
  const bucket = (fixture, arm) => results.filter((r) => r.fixture === fixture && r.arm === arm);
  const hits = (rows, re) => rows.filter((r) => blockingLines(r.stdout).some((l) => re.test(l))).length;

  console.log(`\n=== ${kase.id} (${kase.kind}) ===`);
  for (const f of kase.fixtures) {
    console.log(`\n-- fixture ${f.name} (expect ${f.expect})`);
    for (const arm of arms) {
      const rows = bucket(f.name, arm);
      const off = rows.filter((r) => r.verdict !== f.expect).length;
      const nBlock = rows.reduce((a, r) => a + blockingLines(r.stdout).length, 0);
      console.log(
        `   ${arm.padEnd(9)} n=${rows.length}  off-expected=${off}  blocking-findings=${nBlock}  parse-fail=${rows.filter((r) => !r.verdict).length}`,
      );
    }
    for (const [name, re] of patterns) {
      const b = bucket(f.name, 'baseline');
      const v = bucket(f.name, 'variant');
      const hb = hits(b, re);
      const hv = hits(v, re);
      if (hb === 0 && hv === 0) {
        console.log(`   topic ${name.padEnd(18)} 0/${b.length} vs 0/${v.length}  (never blocked)`);
        continue;
      }
      console.log(
        `   topic ${name.padEnd(18)} baseline ${hb}/${b.length} vs variant ${hv}/${v.length}  p=${fisher(hb, b.length - hb, hv, v.length - hv).toFixed(4)}`,
      );
    }
  }

  console.log('\n-- decision inputs');
  if (kase.kind === 'deletion') {
    const offAny = kase.fixtures.some((f) =>
      arms.some((arm) => bucket(f.name, arm).some((r) => r.verdict !== f.expect)),
    );
    const forbidden = (kase.forbidden ?? []).filter((name) => {
      const re = new RegExp(kase.patterns[name], 'i');
      return results.some((r) => blockingLines(r.stdout).some((l) => re.test(l)));
    });
    console.log(`   verdict off-expected anywhere: ${offAny}`);
    console.log(`   forbidden topics ever blocked: ${forbidden.length ? forbidden.join(', ') : 'none'}`);
    console.log(
      `   => ${!offAny && forbidden.length === 0 ? 'NO-OP (the lines change no outcome; they belong outside the skill)' : 'LOAD-BEARING (keep the lines)'}`,
    );
  } else {
    const re = new RegExp(kase.patterns[kase.target], 'i');
    // lineIn names the arm that carries the candidate line, so a case stays valid after the
    // line is applied: flip lineIn to "baseline" and make the variant remove it again.
    const lineIn = kase.lineIn ?? 'variant';
    const other = lineIn === 'variant' ? 'baseline' : 'variant';
    for (const f of kase.fixtures) {
      const withLine = bucket(f.name, lineIn);
      const without = bucket(f.name, other);
      const hw = hits(withLine, re);
      const ho = hits(without, re);
      const p = fisher(hw, withLine.length - hw, ho, without.length - ho);
      console.log(
        `   ${f.name}: target "${kase.target}" with-line ${hw}/${withLine.length} vs without-line ${ho}/${without.length} p=${p.toFixed(4)} => ${hw > ho && p < 0.05 ? 'LOAD-BEARING (the line surfaces a miss the guide otherwise loses)' : 'NO-OP (the line changes nothing; keep it out)'}`,
      );
    }
  }
}

const argv = process.argv.slice(2);
const argOf = (k, d) => {
  const i = argv.indexOf(k);
  return i === -1 ? d : argv[i + 1];
};
const ids = argv.includes('--all')
  ? readdirSync(CASES).filter((d) => existsSync(path.join(CASES, d, 'case.json')))
  : [argOf('--case', null)].filter(Boolean);
if (!ids.length) throw new Error('usage: run.mjs --case <id> | --all [--reps N] [--providers claude,codex]');

const reps = Number(argOf('--reps', 12));
const providers = argOf('--providers', 'claude,codex').split(',');
const limit = Number(argOf('--concurrency', 8));

for (const id of ids) {
  const kase = loadCase(id);
  const jobs = [];
  for (const provider of providers)
    for (const arm of ['baseline', 'variant'])
      for (const f of kase.fixtures) for (let rep = 1; rep <= reps; rep++) jobs.push({ provider, arm, fixture: f.name, rep });
  process.stderr.write(`\n${id}: ${jobs.length} jobs\n`);
  const results = await pool(jobs, limit, (job) => runOne(kase, job));
  // The filename carries the repeat count so a low-power smoke run cannot overwrite the
  // high-power evidence a decision was made on.
  writeFileSync(path.join(kase.dir, `results-n${reps}.json`), JSON.stringify(results, null, 2));
  report(kase, results);
}
