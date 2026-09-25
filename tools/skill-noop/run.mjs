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
const digest = (text) => createHash('sha256').update(text).digest('hex').slice(0, 12);
// What the report reads besides the patterns: the decision kind, the arm carrying the line and each
// fixture's expected verdict. Target and forbidden only pick patterns, so they stay rescoreable.
const scoringDigest = (kase) => digest(JSON.stringify([kase.kind, kase.lineIn, kase.fixtures.map((f) => [f.name, f.expect])]));

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
  const baselineHash = digest(baselineText);

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
  if (spec.lineIn != null && !ARMS.includes(spec.lineIn)) fail(`${id}: lineIn must be one of ${ARMS.join(' | ')}`);
  if (spec.kind === 'addition') {
    if (!patterns.has(spec.target)) fail(`${id}: target "${spec.target}" is not a pattern name`);
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
    // Which arm carries the candidate lines follows from the operator: insertAfter puts them in the
    // variant, deleteLines takes them out of it. Defaulting to a fixed arm would silently reverse
    // the decision direction for one of the two operators.
    lineIn: spec.lineIn ?? (spec.variant?.insertAfter != null ? 'variant' : 'baseline'),
  };
  kase.arms = { baseline: baselineText, variant: variantText(kase) };
  return kase;
}

// A string replacement expands $&, $`, $' and $$ from the replacement text. Skill text and fixture
// diffs are arbitrary, so a diff carrying `$&` would silently render a prompt other than the one
// the run claims to measure. A function replacement disables that expansion.
const buildPrompt = (kase, arm, fixture) =>
  PROMPT.replace('{{SKILL}}', () => kase.arms[arm]).replace('{{DIFF}}', () => kase.fixtureText.get(fixture));

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
      // The digest of the exact prompt binds the row to the arm and fixture it measured. A rescore
      // after the case changes can then tell a pattern-only edit from rows whose meaning moved.
      const row = { ...job, promptHash: digest(prompt), seconds: Math.round((Date.now() - t0) / 1000), ...extra };
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
    // A blocking finding that no pattern names is invisible to every count above. When one arm
    // leaves significantly more rows with such findings, either the patterns miss the words that arm
    // uses for the measured defect, so the counts measure vocabulary instead of detection (#247), or
    // a topic the case never named concentrates in that arm. Only a reader can tell which, so this
    // prints the lines and warns; the decision below is computed exactly as before.
    const unmatched = (r) => r.findings.filter((l) => isBlocking(l) && ![...kase.patterns.values()].some((re) => re.test(l)));
    const [ub, uv] = ARMS.map((arm) => bucket(f.name, arm).filter((r) => unmatched(r).length).length);
    const [nb, nv] = ARMS.map((arm) => bucket(f.name, arm).length);
    const pu = fisher(ub, nb - ub, uv, nv - uv);
    if (pu < 0.05) {
      console.log(
        `   WARNING unmatched blocking findings: baseline ${ub}/${nb} vs variant ${uv}/${nv} rows  p=${pu.toFixed(4)} — one arm uses wording or a topic no pattern names; read these before trusting the decision:`,
      );
      for (const arm of ARMS)
        for (const r of bucket(f.name, arm)) for (const l of unmatched(r)) console.log(`     ${r.provider}/${arm}#${r.rep} ${l}`);
    }
  }

  console.log('\n-- decision');
  if (short.length) {
    console.log('   REFUSED: cells are short, so no decision is printed.');
    for (const s of short) console.log(`     ${s}`);
    return;
  }
  if (kase.kind === 'deletion') {
    // The decision rests on the difference between the arms, never on a deviation existing
    // somewhere. One reviewer disagreement in each arm proves the lines changed nothing, so
    // reporting that as LOAD-BEARING would tell maintainers to keep a line the run just failed to
    // justify. A deviation that both arms share refuses the run instead.
    const withLine = kase.lineIn;
    const without = ARMS.find((a) => a !== withLine);
    const cells = [];
    for (const f of kase.fixtures) {
      const w = bucket(f.name, withLine);
      const o = bucket(f.name, without);
      cells.push({
        label: `${f.name} verdict off-expected`,
        hw: w.filter((r) => r.verdict !== f.expect).length,
        ho: o.filter((r) => r.verdict !== f.expect).length,
        nw: w.length,
        no: o.length,
      });
      for (const name of kase.forbidden)
        cells.push({
          label: `${f.name} forbidden ${name}`,
          hw: hits(w, kase.patterns.get(name)),
          ho: hits(o, kase.patterns.get(name)),
          nw: w.length,
          no: o.length,
        });
    }
    const worse = [];
    const shared = [];
    for (const c of cells) {
      const p = fisher(c.hw, c.nw - c.hw, c.ho, c.no - c.ho);
      if (c.ho > c.hw && p < 0.05) worse.push(c.label);
      else if (c.hw || c.ho) shared.push(c.label);
      console.log(`   ${c.label.padEnd(34)} with-line ${c.hw}/${c.nw} vs without-line ${c.ho}/${c.no}  p=${p.toFixed(4)}`);
    }
    if (worse.length) console.log(`   => LOAD-BEARING (keep the lines): ${worse.join(', ')}`);
    else if (shared.length)
      console.log(`   => INCONCLUSIVE: ${shared.join(', ')} deviates in both arms, so the lines are not what moves it`);
    else console.log('   => NO-OP (the lines change no outcome; they belong outside the skill)');
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
if (!ids.length)
  fail('usage: run.mjs --case <id> | --all [--validate | --rescore] [--reps N] [--providers claude,codex] [--concurrency N]');

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

if (argv.includes('--validate')) {
  for (const kase of cases) console.log(`${kase.id}: valid`);
  process.exit(0);
}

// The filename carries the repeat count and the provider set, because a cell is identified by
// provider as well: a rerun with fewer providers would otherwise overwrite the evidence a decision
// was justified on while the name still claimed the same power.
const resultsPath = (kase) => path.join(kase.dir, `results-n${reps}-${[...providers].sort().join('+')}.jsonl`);

// Rescoring re-runs the report over rows already on disk, so a pattern fix can be checked against
// the evidence a decision was made on without paying for a single CLI call. Like --validate it
// never writes: appending here would mix a rescore into the evidence it reads.
if (argv.includes('--rescore')) {
  for (const kase of cases) {
    const file = resultsPath(kase);
    if (!existsSync(file)) fail(`${kase.id}: no ${path.basename(file)} to rescore (pick the run with --reps/--providers)`);
    const rows = readFileSync(file, 'utf8')
      .split('\n')
      .filter(Boolean)
      .map((line, i) => {
        try {
          return JSON.parse(line);
        } catch {
          return fail(`${kase.id}: ${path.basename(file)}:${i + 1} is not JSON`);
        }
      });
    // Measuring appends, so one file can hold several runs. Pooling them would fill every cell past
    // --reps and refuse them all, so each run is reported on its own.
    const runs = new Map();
    for (const r of rows) {
      const id = r.runId ?? 'unrecorded';
      if (!runs.has(id)) runs.set(id, []);
      runs.get(id).push(r);
    }
    const scoring = scoringDigest(kase);
    for (const [runId, runRows] of runs) {
      // A row rendered from another prompt measured another edit: after a variant flips between
      // insertion and deletion the same arm name means the opposite, and the report would print a
      // confident decision about lines the rows never compared. Patterns are not in the prompt, so
      // a pattern fix still rescores.
      const moved = runRows.filter(
        (r) =>
          r.promptHash != null &&
          !(ARMS.includes(r.arm) && kase.fixtureText.has(r.fixture) && r.promptHash === digest(buildPrompt(kase, r.arm, r.fixture))),
      );
      // The same prompts under another kind, lineIn or expected verdict are read by other rules:
      // flipping an `expect` inverts every off-expected count without a single new run.
      const rescored = runRows.filter((r) => r.scoringHash != null && r.scoringHash !== scoring);
      if (moved.length || rescored.length) {
        console.log(`\n=== ${kase.id} run ${runId} ===`);
        if (moved.length)
          console.log(
            `   REFUSED: ${moved.length} row(s) were rendered from a prompt the current case no longer produces (skill, variant or fixture changed); measure again instead of rescoring.`,
          );
        if (rescored.length)
          console.log(
            `   REFUSED: ${rescored.length} row(s) were scored under another kind, lineIn or expected verdict; measure again instead of rescoring.`,
          );
        continue;
      }
      report(kase, runRows, providers, reps);
      const unchecked = runRows.filter((r) => r.promptHash == null || r.scoringHash == null).length;
      console.log(
        `   (rescored ${path.basename(file)} run ${runId}: ${runRows.length} row(s); ${unchecked ? `${unchecked} predate provenance digests, so their arm meaning and scoring are assumed, not checked` : 'every prompt and scoring rule matches the current case'})`,
      );
    }
  }
  process.exit(0);
}

for (const kase of cases) {
  const jobs = [];
  for (const provider of providers)
    for (const arm of ARMS)
      for (const f of kase.fixtures) for (let rep = 1; rep <= reps; rep++) jobs.push({ provider, arm, fixture: f.name, rep });
  const out = resultsPath(kase);
  appendFileSync(out, '');
  process.stderr.write(`\n${kase.id}: ${jobs.length} jobs -> ${path.basename(out)}\n`);
  // Rows land on disk as they finish, so a crash keeps the runs already paid for. Appending means
  // one file can hold more than one run, so each row carries the run it came from and the skill
  // text it was rendered against: without those a reader cannot tell two n12 runs apart, nor which
  // baseline produced a row, while the filename still claims a single experiment.
  const runId = new Date().toISOString();
  const results = await pool(
    jobs,
    limit,
    (job) => runOne(kase, job),
    (row) => appendFileSync(out, `${JSON.stringify({ runId, skill: kase.baselineHash, scoringHash: scoringDigest(kase), ...row })}\n`),
  );
  report(kase, results, providers, reps);
}
