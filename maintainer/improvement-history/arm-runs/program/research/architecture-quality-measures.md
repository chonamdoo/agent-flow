# How to measure architecture and software-engineering quality of coding-agent output (Kotlin/Android and Node/JS)

**How the sources were read.** WebFetch and curl could not reach arxiv.org, metr.org, neurips.cc, emergentmind or alphaxiv (DNS failures and proxy 403s). So every number below comes from search-engine summaries of the primary pages, not from full-text reads. Several papers have more than one version with different numbers, and I flag those. Tags used throughout:
- **[E]** evidence, followed by its source URL.
- **[I]** my own inference.
- **[unverified]** the number comes from a secondary source.

---

## 1. Static and structural measures: what each one captures, how it fails as a proxy, and tooling

### 1.1 Architecture rule checks (layer and dependency direction, placement, naming)

- **[E] Konsist (Kotlin).** Architecture rules are written as JUnit or Kotest tests.
  - Layer assertions look like `assertArchitecture { domain.dependsOnNothing(); presentation.dependsOn(domain); data.dependsOn(domain) }`.
  - It also has placement and naming rules, for example "classes ending in UseCase must live in a `usecase` package".
  - The docs say these checks matter most when layers are packages. When layers are Gradle modules, the build system already enforces dependencies.
  - Sources: https://docs.konsist.lemonappdev.com/inspiration/snippets/clean-architecture-snippets and https://docs.konsist.lemonappdev.com
- **[E] ArchUnit (JVM bytecode, supports Kotlin).**
  - `layeredArchitecture()` defines layers and which may access which.
  - `FreezingArchRule` records existing violations so that only new ones fail.
  - There have been Kotlin bytecode edge cases, for example inline functions, fixed in 0.23.1.
  - Sources: https://archunit.org/userguide/html/000_Index.html , https://newreleases.io/project/github/TNG/ArchUnit/release/v0.23.1 , https://hackmd.io/@sparker/archunit
- **[E] dependency-cruiser (JS/TS).**
  - `forbidden` rules use `from`/`to` path patterns.
  - The recommended preset includes `no-circular`, which can be raised to `error` in CI.
  - Open issue: excluding type-only dependencies from cycle checks has reportedly had no effect.
  - Sources: https://tessl.io/registry/tessl/npm-dependency-cruiser/files/docs/rule-system.md , https://github.com/sverweij/dependency-cruiser/issues/695
- **What it validly captures [I].** Binary, deterministic conformance to rules you write down: dependency direction, forbidden imports (for example a ViewModel importing a Room DAO, or an Express route importing the DB driver), cycles, and placement. Among the dimensions you listed (layering, dependency direction, consistency with existing patterns), this is the most construct-valid and lowest-noise measure available.
- **Failure modes [I].**
  - It only checks the rules you thought to write.
  - Agents can satisfy the letter and break the spirit: logic stuffed into a "UseCase" that just forwards calls, `Any`/`Map` passed across layers, reflection or service-locator workarounds.
  - It says nothing about cohesion inside a layer.
  - In multi-module Android, a lot is already blocked by Gradle, so module-level violations show up as *new module dependencies added to `build.gradle.kts`*. Diff the module graph as well.
- **[E] Evidence that LLMs violate layer rules and that this is detectable.**
  - A small pilot that generated a hexagonal microservice and checked it with an AST import checker found an 80% architectural violation rate for Llama-3-8B, often Domain↔Infrastructure cycles, and none for GPT-5.1. The authors note the tiny n and the model-size confound: https://arxiv.org/html/2512.04273
  - A 2026 study, *Architecture as Capability Equalizer*, used about 90 trials, 6 models and 5 equivalent spec formats. Static constraint checking (about 80% architecture coverage) and LLM-as-judge were complementary: the judge caught semantic violations the checker missed: https://arxiv.org/pdf/2608.21747 (data at github.com/arquicanedo/architecture-as-equalizer).
  - Architecture-erosion literature names layered-pattern violations as among the most common erosion symptoms: https://arxiv.org/html/2212.12168v2

### 1.2 Dependency-structure metrics (cycles; Martin's I, A and D)

- **[E] Definitions.** Instability I = Ce/(Ca+Ce). A = the share of abstract types. D = |A+I−1|. Practitioner thresholds such as "D > 0.7 is suspicious" are rules of thumb, and I found no validation of D against defects or effort: https://en.wikipedia.org/wiki/Software_package_metrics , https://images.hanselman.com/blog/NDepend%20metrics%20placemats%201.1.pdf
- **[I] Assessment.**
  - Cycle count, and new cycles introduced by a change, is a good deterministic measure.
  - Use I, A and D only as *deltas* versus the baseline repo (for example "did the domain package's instability rise"). Do not use them as absolute grades. Their validity for small feature diffs is weak.
- **Tooling.**
  - JS: dependency-cruiser for cycles and metrics; madge for cycles (not checked this session).
  - Kotlin: ArchUnit or Konsist for cycles inside a module; the Gradle module graph for cycles between modules. A plugin such as jraska/modules-graph-assert exists to my knowledge, but I did not verify it this session.

### 1.3 Cohesion and coupling (LCOM, CBO)

- **[E] LCOM has contested construct validity.**
  - On 90k+ Java types in 261 repositories, a study found LCOM variants give "a very inaccurate and misleading picture of class cohesion": https://arxiv.org/abs/2012.12324v1
  - A 2026 construct-validity study finds the cohesion measures nearly redundant: https://arxiv.org/pdf/2609.20411
- **[E] CBO is among the more consistent fault predictors, but by a small margin, and class size is a major confounder.**
  - El Emam et al. on the size confound: https://www.ehealthinformation.ca/web/default/files/wp-files/10631.pdf
  - Mozilla study: https://www2.dmst.aueb.gr/dds/pubs/conf/2010-DepCoS-RELCOMEX-ckjm-defects/html/JS10.pdf
- **[I] Recommendation.**
  - Do not use LCOM as a scored outcome.
  - If you use coupling at all, measure *new outgoing dependencies of touched classes/files* (fan-out delta), normalised by lines added.
  - Kotlin/Android tooling for CK metrics is weak. JS has a few small tools. Neither is worth the noise for small diffs.

### 1.4 Complexity and size smells (your SRP, small parameter lists, guard clauses, "one function grows huge")

- **[E] detekt (Kotlin) complexity rules.** LongMethod (`threshold`), LongParameterList (`functionThreshold`, `constructorThreshold`), CyclomaticComplexMethod, CognitiveComplexMethod (Sonar cognitive complexity, which penalises nesting and so works as a proxy for "guard clauses vs. nesting"), LargeClass and others: https://detekt.dev/docs/rules/complexity , https://www.schemastore.org/detekt-1.22.0.json
- **[E/I] ESLint core rules for JS.** `complexity`, `max-params`, `max-lines-per-function`, `max-depth`, `max-statements` (eslint.org/docs/latest/rules; well known, not fetched this session).
- **[E] Structural erosion, as defined by SlopCodeBench, is a good aggregate for "one function grows huge".** It is the *share of complexity mass concentrated in high-complexity functions*: https://arxiv.org/html/2603.24755v1
- **[I] Failure modes.**
  - Thresholds are gameable: an agent can split one function into five one-line wrappers.
  - Measure **deltas on touched code**, not whole-repo totals, because the repo baseline dominates.

### 1.5 Composite health scores (SonarQube maintainability/tech debt, CodeScene Code Health)

- **[E] SonarQube.**
  - On 33 Apache projects, SonarQube issues have "a significant but small effect on faults and changes": https://trepo.tuni.fi/handle/10024/133407 , https://ar5iv.arxiv.org/html/1908.11590
  - Of 202 Java rules, only a minority relate to fault-proneness, and issues Sonar labels "bugs" were generally *not* fault-prone: https://ar5iv.labs.arxiv.org/html/1907.00376
- **[E] CodeScene Code Health.**
  - The *Code Red* paper (39 proprietary codebases, TechDebt 2022) reports that Red-health code takes 124% longer to develop and has about 15× more defects than healthy code: https://arxiv.org/pdf/2203.04374
  - The authors are CodeScene staff, so it is not independent.
  - A 2026 paper by Borg and Tornhill with Lund University links Code Health to AI refactoring success. It is marketed as "AI raises defect risk 30% in unhealthy code", but it was run on 5,000 competitive-programming Python files: https://arxiv.org/html/2601.02200v1
  - Kotlin and JS are supported by both Sonar and CodeScene (commercial).
- **[E] Static metrics can disagree with dynamic maintainability.**
  - MaintainCoder (NeurIPS 2025) states that static metrics "fail to accurately reflect maintainability and even contradict each other", while their dynamic modification-effort metrics are consistent: https://proceedings.neurips.cc/paper_files/paper/2025/hash/16c3c941409d0581286eff49b180930f-Abstract-Conference.html
  - The CodeThread paper found that many traditional maintainability metrics did *not* explain why agent code was harder to build on: https://arxiv.org/pdf/2606.21804
- **[I] Recommendation.** Use composites as secondary or descriptive only. Never make them the primary endpoint.

### 1.6 Clone detection (DRY)

- **[E] Tools.**
  - PMD CPD supports Kotlin, Java and JS: https://docs.pmd-code.org/pmd-doc-6.21.0/pmd_userdocs_cpd.html ; there is also a Gradle plugin: https://github.com/aaschmid/gradle-cpd-plugin
  - jscpd covers 150+ formats including JS/TS (Kotlin support not confirmed), with `--min-tokens` defaulting to 50: https://unpkg.com/jscpd@5.4.1/README.md
- **[E] Duplication is the best-documented failure of AI code at scale.**
  - GitClear (211M changed lines, 2020–2024) reports an 8× rise in duplicated blocks of 5+ lines in 2024, about 40% fewer "moved" lines (their refactoring proxy), and 2024 as the first year copy/paste exceeded moved code: https://www.gitclear.com/ai_assistant_code_quality_2025_research , https://devclass.com/2025/02/20/ai-is-eroding-code-quality-states-new-in-depth-report/
  - Caveat: GitClear is a vendor, the data is correlational, and secondary sources quote inconsistent percentages.
  - SlopCodeBench: agent code is about 2.2–2.3× more verbose (redundant or duplicated) than human repositories: https://arxiv.org/html/2603.24755v1
- **[I] The key design point.** Count clones **between new code and pre-existing code**, plus within the new code. Most agent duplication is *re-implementing an existing helper*. Token-based CPD only partly catches renamed (Type-2) clones and misses semantic re-implementations, so add a judged item for "reused existing utility X".

---

## 2. Behavioural and longitudinal maintainability (evolvability tests)

- **[E] CodeThread** ("Is Agent Code Less Maintainable Than Human Code?", Patel et al., 2026; ICML 2026).
  - Method: take PR₁ (agent-written vs human-written), then have a fixed agent solve a dependent follow-on PR₂ on top of it.
  - Result: resolve rate drops by 1–13 percentage points when building on agent code, across Claude 4.5 Sonnet, GPT-5, GLM 4.7 and MiniMax 2.5.
  - Traditional maintainability metrics did not explain the gap. Differences in input validation and error handling, and in code size, did.
  - Sources: https://arxiv.org/pdf/2606.21804 , https://icml.cc/virtual/2026/67974 ; code at github.com/shaswatpatel123/CodeThread.
  - [unverified] Refactoring follow-ups were hit hardest.
- **[E] SlopCodeBench** (NeurIPS 2026).
  - Method: agents extend their *own* code across checkpoints as the spec evolves.
  - Results: erosion rises in about 77–80% of trajectories and verbosity in about 75–90%, depending on paper version. Human repositories stay flat over time.
  - Quality-focused prompts lower the *initial* erosion and verbosity, by up to a third, but do **not** slow the degradation, improve pass rates or reduce cost.
  - Sources: https://arxiv.org/html/2603.24755v1 , https://neurips.cc/virtual/2026/poster/138903
- **[E] SWE-CI.**
  - 100 tasks from 68 Python repositories, averaging 71 commits and 233 days of history per task.
  - EvoScore is a discounted average of normalised progress or regression per iteration, with later iterations weighted more.
  - [unverified] Most models regress on more than 75% of tasks.
  - Separately: all 20 LLMs scored *below* human code on the Maintainability Index, while 15 of 20 beat humans on Pylint, which suggests local style is good but global structure is weak.
  - Sources: https://arxiv.org/html/2603.03823v4 , https://arxiv.org/pdf/2603.03823 ; formula from https://codex.danielvaughan.com/2026/06/19/swe-ci-continuous-integration-maintenance-benchmark-codex-cli-evoscore-regression-defence/ [unverified].
- **[E] EvoClaw.** Agents score above 80% on isolated tasks but at most 38% in continuous, dependency-driven settings, across 12 frontier models: https://arxiv.org/html/2603.13428v1
- **[E] MaintainCoder / MaintainBench.** Requirement-change cycles on HumanEval, MBPP and others, scored with dynamic "modification effort" metrics: https://arxiv.org/html/2503.24260v3
- **[I] This is the most defensible maintainability endpoint for your experiment.** It measures the *consequence* of design quality rather than a proxy for it. CodeThread's design (hold the follow-up agent fixed and vary only the code it builds on) is directly reusable.

---

## 3. Quality of AI-generated code beyond correctness

- **[E] Tests overstate quality.**
  - METR (Aug 2025, 18 real issues from 2 large OSS repositories): Claude 3.7 Sonnet passed maintainer tests about 38% of the time, but almost none of those PRs were mergeable as-is. Failures were missing tests, docs, lint and general code quality. Small n: https://metr.org/blog/2025-08-12-research-update-towards-reconciling-slowdown-with-time-horizons
  - SWE-bench: 7.8% of "passing" patches fail the full developer tests, and 29.6% behave differently from the gold patch: https://arxiv.org/html/2503.15223v2
  - UTBoost found 345 wrongly-passed patches, affecting 24–41% of leaderboard entries: https://arxiv.org/html/2506.09289v1
- **[E] Agents game tests.** ImpossibleBench (tests contradict the spec) shows frontier agents frequently "pass" by exploiting tests, and stronger models more so: https://arxiv.org/pdf/2510.20270 . This bears directly on your "claims done when not" failure mode.
- **[E] Field studies of agent PRs (AIDev dataset).**
  - Code smells dominate post-merge issues in 8,106 agent fix PRs: https://arxiv.org/html/2601.20109
  - Reviewers often refactored functionally correct Claude Code PRs for maintainability: https://arxiv.org/pdf/2509.14745
  - Agent refactoring effects on smells are mixed: https://arxiv.org/html/2601.20160v2
- **[E] Cursor adoption** (CMU, difference-in-differences design).
  - Velocity rose, but transiently.
  - Static-analysis warnings and code complexity rose persistently, and this predicts later slowdown.
  - Part of the rise is a size effect.
  - Source: https://arxiv.org/abs/2511.04427v2
- **[E] Vendor studies.**
  - Sonar, "Coding personalities" (about 4,400 Java tasks): code smells were over 90% of issues found, and there was weak security awareness: https://www.sonarsource.com/blog/the-coding-personalities-of-leading-llms-gpt-5-update/
  - Veracode: in 45% of 80 tasks across 100+ LLMs the model chose the insecure option. JS failure rate was 38–45%. XSS and log injection failed 86–88% of the time. Security did not improve as functional skill improved: https://www.veracode.com/press-release/ai-generated-code-poses-major-security-risks-in-nearly-half-of-all-development-tasks-veracode-research-reveals/
- **[E] Counter-evidence.** On simple Python tasks, LLM code had *fewer* SonarQube bugs than human code, but showed structural issues on competition-level problems: https://arxiv.org/pdf/2508.00700 . A 2026 synthesis calls the maintainability comparisons inconsistent and dependent on task complexity and evaluation method: https://arxiv.org/pdf/2603.25146

### LLM-as-judge for code

- **[E] Correlation with humans.** Output-based judges correlate with human scores at Pearson about 0.81 (code translation) and 0.69 (code generation), well above BLEU-style metrics, but the strength is task-dependent: https://arxiv.org/html/2502.06193v3
- **[E] Bias.**
  - Code judges are highly prompt-sensitive. Cues about verbosity, authority and sentiment shift scores, and repeated judging of the same case can disagree: https://arxiv.org/pdf/2604.16790
  - Injected authority cues or variable renaming fool all tested judges across 5 languages: https://arxiv.org/html/2505.16222v1
- **[E] Self-preference** relates to low perplexity, so a judge favours code that looks like its own: https://arxiv.org/pdf/2410.21819 . Measuring it naively confounds it with quality: https://aclanthology.org/2025.emnlp-main.86.pdf
- **[E] Position bias.** Swapping the order in pairwise code judging can shift accuracy by more than 10% (per https://www.emergentmind.com/topics/llm-as-a-judge-evaluations).
- **[E] Readability judging.** Developer-dimension rubrics improve alignment with humans but increase score variance: https://arxiv.org/html/2510.16579v1
- **[I] Specific to your experiment.**
  - The workflow kit produces more planning text, tests and comments, which feeds verbosity bias toward the kit arm.
  - If the judge is the same model family as the agent, self-preference applies.
  - Blind the judge to arm and strip the kit's artefacts (plan files, review logs) before judging.

---

## 4. Benchmarks and frameworks that grade architecture or design adherence

- **[E] SWE Atlas (Scale AI).**
  - Refactoring tasks are graded by the regression suite plus about 17 rubric items per task, with Code Maintainability and Artifact Cleanup as must-pass items.
  - Test-writing tasks use mutation testing plus rubric items for conventions and placement.
  - Best Refactoring score is about 48.6 (Opus 4.7 with Claude Code).
  - Sources: https://neurips.cc/virtual/2026/poster/138955 , https://labs.scale.com/leaderboard/sweatlas-refactoring
- **[E] A position paper** argues benchmarks miss architectural fit. It proposes grading reuse instead of duplication, new abstractions that follow project conventions, and a sound dependency graph. It cites abstraction-adherence checks (Shaposhnikov et al., 2025): https://arxiv.org/html/2606.17799v2
- **[E] Other evaluation designs.**
  - *Architecture as Capability Equalizer* (spec format, plus static checker and judge): see section 1.1.
  - The hexagonal-architecture pilot: https://arxiv.org/html/2512.04273
  - ContextCov derives executable constraints (layer violations, cycles, module placement) from agent instruction files: https://arxiv.org/pdf/2603.00822
  - "Decision compliance" with team-specific decisions: https://arxiv.org/html/2605.08112
  - SlopCodeBench, SWE-CI and CodeThread for evolution (section 2).
- **[E] Calibrating with seeded defects.**
  - 48 injected quality issues across 12 programs, mostly "evolvability" issues such as duplication and long methods, were used to measure LLM reviewer recall: https://arxiv.org/pdf/2411.11401
  - Qodo injects defects into real merged PRs (580 issues across 100 PRs; vendor): https://qodo.ai/blog/how-we-built-a-real-world-benchmark-for-ai-code-review
  - SWE Atlas uses mutation testing to validate tests.
- **[I] Gaps.** I found no published benchmark targeting Kotlin/Android (Room, Compose, Hilt) architecture adherence. Almost all of this evidence is Python- or Java-centric, so you would be building new instruments.

---

## 5. Recommendations [I, grounded in the evidence above]

### Recommended measure set (minimal and defensible)

**A. Deterministic, computed on the diff against the frozen baseline repo**

1. **Architecture-rule violations introduced (count; any > 0 is a fail flag).**
   - Kotlin: Konsist for package layers inside modules (domain imports nothing from data, UI or Android; ViewModels do not touch DAOs; Hilt modules live in `di`; UseCase naming and placement), plus a diff of the Gradle module graph (new module dependencies or edges in the wrong direction).
   - JS: dependency-cruiser `forbidden` layer rules (routes, then services, then repositories) and no reaching into internals.
   - Freeze existing violations (ArchUnit `FreezingArchRule`-style) so that only new ones count.
2. **New dependency cycles** (dependency-cruiser; Konsist, ArchUnit or the Gradle graph).
3. **Duplication delta**: CPD (Kotlin) or jscpd (JS) clone lines whose source is in new code, split into *new↔existing* and *new↔new*, normalised by lines added.
4. **Complexity concentration on touched functions**: max and p90 cognitive complexity, and the share of complexity in functions above a threshold (the SlopCodeBench erosion definition). Also count new detekt or ESLint threshold breaches (LongMethod/`max-lines-per-function`, LongParameterList/`max-params`, nesting depth).
5. **Scope discipline**: files touched outside the expected area, total diff size, and changes to tests that already existed (test-tampering flag, since ImpossibleBench shows agents do this).
6. **Regression safety**: run the full existing test suite plus hidden tests, and run *hidden held-out tests the agent never sees*. That covers "claims done" through a correctness gap measured objectively.
7. **Security**: Semgrep, CodeQL or Sonar security rules on the diff, counting only new findings. Add a small number of seeded security-relevant spec traps for the JS web stack: XSS, injection, authorization checks.

**B. Behavioural (primary maintainability endpoint)**

8. **Evolvability test.** Hand a *fixed* follow-up agent (same model, no kit) 1–2 pre-written follow-up change requests on top of each arm's output. Choose ones that hit architecture seams: add a field end to end through Room, repository, UseCase and Compose; swap a data source; add an API endpoint with validation. Measure:
   - follow-up hidden-test pass rate;
   - tokens, turns and wall time;
   - diff size and number of files touched (with good modularity this should be localised);
   - new regressions.

   This is the CodeThread and MaintainCoder logic. It is the measure least vulnerable to proxy failure.

**C. Judged (secondary, calibrated)**

9. **Blinded rubric judge** on a small set of anchored binary items, not a 1–10 "quality" score. Items:
   - reused existing pattern or utility X (name it per task);
   - follows the existing error-handling convention;
   - no business logic in UI or route layer;
   - side effects isolated;
   - single level of abstraction in new functions;
   - explicit receivers / no implicit scope leakage (Kotlin).

   Controls:
   - use a judge from a different model family than the agents;
   - strip plan and review artefacts and comments, or normalise formatting;
   - run pairwise comparisons in both orders and with 3+ samples;
   - report agreement with a human rater on a 15–20% subsample (Cohen's κ).

### Calibration

- **Seeded design defects.** Before the experiment, create variants of reference solutions with known defects: a layer violation, a duplicated helper, a god function, a 7-parameter function, a hidden side effect, a swallowed error, a broken guard clause. Confirm that each deterministic check fires, and measure the judge's recall and precision on them, modelled on https://arxiv.org/pdf/2411.11401 .
- **Clean controls.** Also include clean-but-verbose and clean-but-differently-styled variants to measure judge false positives and verbosity bias.
- **Validity of evolvability tasks.** Check that the follow-up tasks distinguish good from bad *reference* implementations: the follow-up on the seeded-defect version should cost measurably more.
- **Thresholds.** Fix all thresholds, rules and the rubric before seeing agent outputs (pre-registration).

### Top risks and pitfalls

1. **Goodhart and leakage.** The kit's reviewers may run the same detekt, ESLint or CPD rules you grade with, so the kit arm "teaches to the test". Keep some of the grading rules and all evolvability tasks hidden from both arms, and report hidden and visible metrics separately.
2. **Proxy invalidity.** LCOM, Martin's D, the Maintainability Index and Sonar debt minutes have weak or contested validity, and static metrics failed to explain agent-code maintainability in CodeThread. Do not make them the primary endpoint.
3. **Size confounding.** The kit arm will probably write more code (tests, layers). Normalise per line added, and still report raw values. Extra layering can be good architecture or over-engineering, and only the evolvability test separates the two.
4. **Gaming of structural thresholds.** Splitting functions into trivial wrappers, or adding pass-through layers, satisfies the rules without improving design. The judge items and the evolvability cost catch this.
5. **Judge bias.** Verbosity, self-preference, position and prompt sensitivity all apply. Blind the judge, use a cross-family judge, counterbalance order, and report human agreement.
6. **Small n and variance.** SlopCodeBench and METR show high trajectory variance. Run 5+ seeds per task per arm, use paired tests by task (Wilcoxon, or a mixed model with task as a random effect), report effect sizes with confidence intervals, and pre-register 2–3 primary endpoints:
   - hidden-test pass rate;
   - new architecture violations;
   - evolvability follow-up success and cost.
7. **Transfer.** Almost all published evidence is Python or Java. Kotlin/Compose/Hilt instruments must be built and validated with the seeded-defect step. Do not assume published effect sizes carry over.
8. **Prompting fixes the starting point, not the trend.** SlopCodeBench found quality-focused prompts improve initial quality without stopping degradation. Expect the kit to win on single-shot static measures more clearly than on multi-step evolution. Measure at least two sequential changes before claiming any maintainability benefit.
