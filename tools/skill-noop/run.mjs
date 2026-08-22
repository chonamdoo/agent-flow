#!/usr/bin/env node
// Measures whether one line in a skill document changes review outcomes.
//
// A case renders two prompts that differ by exactly one edit to a skill file (`baseline` arm vs
// `variant` arm), feeds each the same fixture diff to every configured reviewer CLI N times, and
// compares verdicts and `blocking:` findings with a two-tailed Fisher exact test.
//
// This tree sits outside `scripts/` on purpose. `scripts/` is a shipped asset tree (npm `files`,
// RECORDED_KIT_ASSET_TREES, KIT_SOURCE_DIGEST_ROOTS), so a harness that writes its own output
// there would install measurement corpora into user projects and stale every install on each run.
import { appendFileSync, existsSync, readFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const CASES = path.join(HERE, 'cases');
const PROMPT = readFileSync(path.join(HERE, 'prompt.md'), 'utf8');
const TIMEOUT_MS = 300_000;

const ARMS = ['baseline', 'variant'];
const KINDS = ['deletion', 'addition'];
const EXPECTS = ['approve', 'request-changes'];
const INVOKE = {
  claude: ['claude', ['-p', '--allowed-tools', 'none']],
  codex: ['codex', ['exec', '--sandbox', 'read-only', '-']],
};

// Reviewers bold, backtick and number their output. A formatting variant that fails to parse
// becomes a dead run, and a dead run scored as a disagreement flips a decision, so these stay
// deliberately permissive.
const VERDICT_RE = /verdict\**\s*:\s*[*_`]*\s*(approve|request-changes)/gi;
const FINDING_RE = /^\s*(?:[-*\u2022]|\d+[.)])?\s*[`*_]*\s*(blocking|suggestion)\b[`*_]*\s*:/i;
const BLOCKING_RE = /^\s*(?:[-*\u2022]|\d+[.)])?\s*[`*_]*\s*blocking\b/i;

const fail = (msg) => {
  throw new Error(msg);
};
const isPosInt = (v) => Number.isInteger(v) && v > 0;
const isBlocking = (line) => BLOCKING_RE.test(line);

function variantText(kase) {
  const lines = kase.baselineText.split('\n');
  const v = kase.variant ?? fail(`${kase.id}: no variant`);
  if (v.deleteLines) {
    const [from, to] = v.deleteLines;
    if (!isPosInt(from) || !isPosInt(to) || to < from) fail(`${kase.id}: deleteLines must be positive with from <= to`);
    if (to > lines.length) fail(`${kase.id}: deleteLines runs past the end of the file`);
    const removed = lines.slice(from - 1, to);
    // One needle per removed line. A shorter list would leave the rest of the range unverified,
    // so a line-number shift could silently measure a different edit than the case describes.
    if (v.expectRemoved?.length !== removed.length)
      fail(`${kase.id}: expectRemoved needs one needle per deleted line (${removed.length})`);
    v.expectRemoved.forEach((needle, i) => {
      if (!removed[i].includes(needle)) fail(`${kase.id}: line ${from + i} is "${removed[i]}", expected "${needle}"`);
    });
    return [...lines.slice(0, from - 1), ...lines.slice(to)].join('\n');
  }
  if (v.insertAfter != null) {
    if (!isPosInt(v.insertAfter) || v.insertAfter > lines.length) fail(`${kase.id}: insertAfter out of range`);
    if (!v.expectAnchor) fail(`${kase.id}: insertAfter needs expectAnchor`);
    if (!lines[v.insertAfter - 1].includes(v.expectAnchor))
      fail(`${kase.id}: line ${v.insertAfter} is "${lines[v.insertAfter - 1]}", expected "${v.expectAnchor}"`);
    if (!v.text?.length) fail(`${kase.id}: insertAfter needs text`);
    return [...lines.slice(0, v.insertAfter), ...v.text, ...lines.slice(v.insertAfter)].join('\n');
  }
  return fail(`${kase.id}: variant needs deleteLines or insertAfter+text`);
}

function loadCase(id) {
  const dir = path.join(CASES, id);
  const specPath = path.join(dir, 'case.json');
  if (!existsSync(specPath)) fail(`${id}: no case.json`);
  const spec = JSON.parse(readFileSync(specPath, 'utf8'));
  if (!KINDS.includes(spec.kind)) fail(`${id}: kind must be one of ${KINDS.join(' | ')}`);

  const baselinePath = path.isAbsolute(spec.baseline) ? spec.baseline : path.join(REPO, spec.baseline);
  if (!existsSync(baselinePath)) fail(`${id}: missing skill ${baselinePath}`);
  // One read per case. Rendering each arm from its own read would let an edit made while the
  // batch is in flight change the baseline text under later jobs, so the two arms would differ
  // by more than the single edit the case declares.
  const baselineText = readFileSync(baselinePath, 'utf8');
  const baselineHash = createHash('sha256').update(baselineText).digest('hex').slice(0, 12);

  if (!spec.fixtures?.length) fail(`${id}: no fixtures`);
  const fixtureText = new Map();
  for (const f of spec.fixtures) {
    if (!f.name || fixtureText.has(f.name)) fail(`${id}: fixture names must be present and unique`);
    if (!EXPECTS.includes(f.expect)) fail(`${id}: fixture ${f.name} expect must be ${EXPECTS.join(' | ')}`);
    const file = path.join(dir, f.file ?? '');
    if (!existsSync(file)) fail(`${id}: missing fixture file ${f.file}`);
    fixtureText.set(f.name, readFileSync(file, 'utf8'));
  }

  // Patterns compile here rather than in the report: an uncompilable regex must fail before the
  // run spends real CLI calls, not after all of them.
  if (!spec.patterns || !Object.keys(spec.patterns).length) fail(`${id}: no patterns`);
  const patterns = new Map();
  for (const [name, source] of Object.entries(spec.patterns)) {
    try {
      patterns.set(name, new RegExp(source, 'i'));
    } catch (e) {
      fail(`${id}: pattern ${name} does not compile: ${e.message}`);
    }
  }
  // An unknown pattern name would reach `new RegExp(undefined)`, which compiles to /(?:)/ and
  // matches every finding. That prints a confident wrong decision in both directions: a typo in
  // `target` reads as NO-OP, a typo in `forbidden` reads as LOAD-BEARING.
  if (spec.kind === 'addition') {
    if (!patterns.has(spec.target)) fail(`${id}: target "${spec.target}" is not a pattern name`);
    if (spec.lineIn != null && !ARMS.includes(spec.lineIn)) fail(`${id}: lineIn must be one of ${ARMS.join(' | ')}`);
  } else {
    if (!spec.forbidden?.length) fail(`${id}: a deletion case needs forbidden topics`);
    for (const name of spec.forbidden) if (!patterns.has(name)) fail(`${id}: forbidden "${name}" is not a pattern name`);
  }

  const kase = {
    ...spec,
    id,
    dir,
    baselinePath,
    baselineText,
    baselineHash,
    patterns,
    fixtureText,
    lineIn: spec.lineIn ?? 'variant',
  };
  kase.arms = { baseline: baselineText, variant: variantText(kase) };
  return kase;
}

const buildPrompt = (kase, arm, fixture) =>
  PROMPT.replace('{{SKILL}}', kase.arms[arm]).replace('{{DIFF}}', kase.fixtureText.get(fixture));

function runOne(kase, job) {
  const [bin, args] = INVOKE[job.provider];
  const prompt = buildPrompt(kase, job.arm, job.fixture);
  return new Promise((resolve) => {
    const t0 = Date.now();
    let settled = false;
    let child;
    const finish = ({ stderrRaw, ...extra }) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const row = { ...job, seconds: Math.round((Date.now() - t0) / 1000), ...extra };
      row.ok = row.code === 0 && row.verdict != null;
      // stderr carries no signal on a good run — codex echoes the entire prompt there, which was
      // 43% of the committed evidence. On a failed run it is the only clue, so keep it there,
      // head and tail, because the echo pushes a startup or auth error out of a tail-only window.
      if (!row.ok && stderrRaw)
        row.stderr = stderrRaw.length > 1000 ? `${stderrRaw.slice(0, 500)}\n...\n${stderrRaw.slice(-500)}` : stderrRaw;
      resolve(row);
    };
    const timer = setTimeout(() => child?.kill('SIGKILL'), TIMEOUT_MS);
    try {
      child = spawn(bin, args, { cwd: kase.dir, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch (e) {
      return finish({ code: null, verdict: null, findings: [], error: `spawn: ${e.message}` });
    }
    let out = '';
    let err = '';
    // Without an encoding the chunks arrive as Buffers and each is coerced independently, so a
    // multi-byte character split across a chunk boundary becomes replacement characters. Most
    // topic patterns here are Korean, so that would turn real hits into misses.
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('error', (e) => finish({ code: null, verdict: null, findings: [], error: `child: ${e.message}` }));
    child.stdin.on('error', (e) => finish({ code: null, verdict: null, findings: [], error: `stdin: ${e.message}` }));
    child.on('close', (code) => {
      const verdicts = [...out.matchAll(VERDICT_RE)];
      finish({
        code,
        verdict: verdicts.length ? verdicts.at(-1)[1].toLowerCase() : null,
        // Only finding lines are kept. Patterns are only ever matched against them, and keeping
        // full transcripts put hundreds of KB of model prose under version control.
        findings: out
          .split('\n')
          .map((l) => l.trim())
          .filter((l) => FINDING_RE.test(l)),
        stdoutBytes: Buffer.byteLength(out),
        stderrRaw: err,
      });
    });
    child.stdin.end(prompt);
  });
}

async function pool(jobs, limit, work, sink) {
  const results = [];
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(limit, jobs.length) }, async () => {
      while (cursor < jobs.length) {
        const job = jobs[cursor++];
        let row;
        try {
          row = await work(job);
        } catch (e) {
          // One bad job must not discard the runs already paid for.
          row = { ...job, code: null, verdict: null, findings: [], ok: false, error: `harness: ${e.message}` };
        }
        results.push(row);
        sink(row);
        process.stderr.write(
          `[${results.length}/${jobs.length}] ${row.provider}/${row.arm}/${row.fixture}#${row.rep} -> ${row.ok ? row.verdict : `DEAD(${row.error ?? `exit ${row.code}`})`} (${row.seconds ?? 0}s)\n`,
        );
      }
    }),
  );
  return results;
}

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
  if (!n) return 1;
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

function report(kase, results, providers, expectedPerCell) {
  const usable = results.filter((r) => r.ok);
  const dead = results.filter((r) => !r.ok);
  console.log(`\n=== ${kase.id} (${kase.kind}) skill@${kase.baselineHash} ===`);
  if (dead.length) {
    console.log(`   dropped ${dead.length} unusable run(s) from every count below:`);
    for (const r of dead)
      console.log(`     ${r.provider}/${r.arm}/${r.fixture}#${r.rep} exit=${r.code} verdict=${r.verdict} ${r.error ?? ''}`);
  }

  const bucket = (fixture, arm, provider) =>
    usable.filter((r) => r.fixture === fixture && r.arm === arm && (provider ? r.provider === provider : true));
  const hits = (rows, re) => rows.filter((r) => r.findings.some((l) => isBlocking(l) && re.test(l))).length;

  const short = [];
  for (const f of kase.fixtures)
    for (const arm of ARMS)
      for (const provider of providers) {
        const n = bucket(f.name, arm, provider).length;
        if (n !== expectedPerCell) short.push(`${provider}/${arm}/${f.name} has ${n} of ${expectedPerCell}`);
      }

  for (const f of kase.fixtures) {
    console.log(`\n-- fixture ${f.name} (expect ${f.expect})`);
    for (const arm of ARMS) {
      const rows = bucket(f.name, arm);
      const off = rows.filter((r) => r.verdict !== f.expect).length;
      const nBlock = rows.reduce((a, r) => a + r.findings.filter(isBlocking).length, 0);
      console.log(`   ${arm.padEnd(9)} n=${rows.length}  off-expected=${off}  blocking-findings=${nBlock}`);
    }
    for (const [name, re] of kase.patterns) {
      const hb = hits(bucket(f.name, 'baseline'), re);
      const hv = hits(bucket(f.name, 'variant'), re);
      const nb = bucket(f.name, 'baseline').length;
      const nv = bucket(f.name, 'variant').length;
      if (!hb && !hv) {
        console.log(`   topic ${name.padEnd(20)} 0/${nb} vs 0/${nv}  (never blocked)`);
        continue;
      }
      const perProvider = providers
        .map((p) => `${p} ${hits(bucket(f.name, 'baseline', p), re)}/${bucket(f.name, 'baseline', p).length}:${hits(bucket(f.name, 'variant', p), re)}/${bucket(f.name, 'variant', p).length}`)
        .join('  ');
      console.log(
        `   topic ${name.padEnd(20)} baseline ${hb}/${nb} vs variant ${hv}/${nv}  p=${fisher(hb, nb - hb, hv, nv - hv).toFixed(4)}  [${perProvider}]`,
      );
    }
  }

  console.log('\n-- decision');
  if (short.length) {
    console.log('   REFUSED: cells are short, so no decision is printed.');
    for (const s of short) console.log(`     ${s}`);
    return;
  }
  if (kase.kind === 'deletion') {
    const offAny = kase.fixtures.some((f) => ARMS.some((arm) => bucket(f.name, arm).some((r) => r.verdict !== f.expect)));
    const forbidden = kase.forbidden.filter((name) =>
      usable.some((r) => r.findings.some((l) => isBlocking(l) && kase.patterns.get(name).test(l))),
    );
    console.log(`   verdict off-expected anywhere: ${offAny}`);
    console.log(`   forbidden topics ever blocked: ${forbidden.length ? forbidden.join(', ') : 'none'}`);
    console.log(
      `   => ${!offAny && forbidden.length === 0 ? 'NO-OP (the lines change no outcome; they belong outside the skill)' : 'LOAD-BEARING (keep the lines)'}`,
    );
    return;
  }
  const re = kase.patterns.get(kase.target);
  // lineIn names the arm carrying the candidate line, so a case stays valid after the line is
  // applied: flip lineIn to "baseline" and have the variant remove it again.
  const other = ARMS.find((a) => a !== kase.lineIn);
  for (const f of kase.fixtures) {
    const withLine = bucket(f.name, kase.lineIn);
    const without = bucket(f.name, other);
    const hw = hits(withLine, re);
    const ho = hits(without, re);
    const p = fisher(hw, withLine.length - hw, ho, without.length - ho);
    console.log(
      `   ${f.name}: target "${kase.target}" with-line ${hw}/${withLine.length} vs without-line ${ho}/${without.length} p=${p.toFixed(4)} => ${hw > ho && p < 0.05 ? 'LOAD-BEARING (the line surfaces a miss the guide otherwise loses)' : 'NO-OP (the line changes nothing; keep it out)'}`,
    );
  }
}

const argv = process.argv.slice(2);
const argOf = (k, d) => {
  const i = argv.indexOf(k);
  return i === -1 ? d : argv[i + 1];
};
const posInt = (raw, label) => {
  const n = Number(raw);
  if (!isPosInt(n)) fail(`${label} must be a positive integer, got "${raw}"`);
  return n;
};

const ids = argv.includes('--all')
  ? readdirSync(CASES)
      .filter((d) => existsSync(path.join(CASES, d, 'case.json')))
      .sort()
  : [argOf('--case', null)].filter(Boolean);
if (!ids.length) fail('usage: run.mjs --case <id> | --all [--reps N] [--providers claude,codex] [--concurrency N]');

const reps = posInt(argOf('--reps', '12'), '--reps');
const limit = posInt(argOf('--concurrency', '8'), '--concurrency');
const providers = argOf('--providers', 'claude,codex')
  .split(',')
  .map((p) => p.trim())
  .filter(Boolean);
if (!providers.length) fail('--providers is empty');
for (const p of providers) if (!INVOKE[p]) fail(`unknown provider "${p}" (known: ${Object.keys(INVOKE).join(', ')})`);

// Load every case before spending a single CLI call, so a malformed spec fails immediately
// instead of after the first case has already run.
const cases = ids.map(loadCase);

for (const kase of cases) {
  const jobs = [];
  for (const provider of providers)
    for (const arm of ARMS)
      for (const f of kase.fixtures) for (let rep = 1; rep <= reps; rep++) jobs.push({ provider, arm, fixture: f.name, rep });
  // The filename carries the repeat count and the provider set, because a cell is identified by
  // provider as well: a rerun with fewer providers would otherwise overwrite the evidence a
  // decision was justified on while the name still claimed the same power.
  const out = path.join(kase.dir, `results-n${reps}-${[...providers].sort().join('+')}.jsonl`);
  appendFileSync(out, '');
  process.stderr.write(`\n${kase.id}: ${jobs.length} jobs -> ${path.basename(out)}\n`);
  // Rows land on disk as they finish, so a crash keeps the runs already paid for.
  const results = await pool(jobs, limit, (job) => runOne(kase, job), (row) => appendFileSync(out, `${JSON.stringify(row)}\n`));
  report(kase, results, providers, reps);
}
