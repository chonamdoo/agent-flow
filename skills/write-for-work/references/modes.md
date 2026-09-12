# Document modes

Use one primary mode. Apply another mode only for a specific section that genuinely needs it.

## Documentation

Optimize for finding and completing a task.

- State what the document enables before explaining background.
- Name the intended user or system when ambiguity is possible.
- Put prerequisites before procedures.
- Use exact screen names, file paths, commands, inputs, outputs, and observable behavior.
- Explain failure cases and recovery when they are likely or costly.
- Replace abstract labels such as "responsibility boundary" or "processing criteria" with the location or behavior the reader can inspect.
- Keep architecture terminology when the reader needs it for precision.
- Explain current behavior in durable reference material. Preserve before/after and version history when the document is a migration guide, release note, or change record.
- Keep prerequisites, warnings, defaults, and requirement strength exact. A clearer sentence must not invent supported versions, recovery behavior, or successful verification.
- Remove introductions that merely announce what the title already says.
- End with the resulting state or the next linked task, not a generic summary.

Preferred sequence when applicable:

1. Purpose
2. Prerequisites
3. Procedure or behavior
4. Verification
5. Failure and recovery
6. Related references

## PR descriptions

Help a reviewer understand why the change exists and what evidence supports it.

- Start from the supplied issue, diff, notes, and execution results. Lead with the problem and changed behavior, not a file inventory or "안정성을 강화했다".
- Separate implemented behavior from intended benefits. Say which condition or operation changed; do not claim faster, safer, or regression-free behavior without evidence.
- Report verification exactly as observed: what ran, what it checked, its result, and what did not run. A test added to the diff is not evidence it passed.
- Preserve the repository's template, required headings, issue links, and machine-readable markers. Keep checkbox state unless source evidence and the task authorize a status update; prose editing alone does not.
- Include compatibility effects, remaining risks, or rollout/rollback requirements only when supported. Do not invent an issue number, reviewer approval, test result, or release plan to fill a section.
- Keep change history here when it explains the patch. Do not apply the documentation rule about current behavior by deleting the reason for the change.
- Return the requested PR text; publishing or updating a remote PR requires separate authorization.

Synthetic example with all facts in the draft:

> 캐시 무효화 스코프의 강화를 진행했습니다. 이전에는 모든 캐시를 지웠지만 이제 변경된 키만 지웁니다. 테스트는 실행하지 못했습니다.

Possible revision:

> 전체 캐시 대신 변경된 키만 지우도록 수정했습니다. 테스트는 실행하지 못했습니다.

Do not replace the last sentence with "검증 완료".

## Code comments and docstrings

Improve maintenance information at its point of use, not the amount of commentary.

- Follow the project's comment policy to decide whether a comment belongs. A prose-editing request does not authorize adding comments throughout the code or changing its behavior.
- For a warranted comment, state the reason or constraint that a maintainer cannot recover from the nearby code alone. Preserve public API contracts, exceptions, and non-obvious security, concurrency, compatibility, or data-loss warnings.
- Remove syntax narration only when it carries no additional contract or rationale. A public return-value description is not redundant merely because it starts with "Returns".
- Keep identifiers, surrounding executable code, indentation and delimiters required by syntax, documentation tags, links, and tool directives intact. Rewrite only explanatory text; do not translate `@param`, `:raises:`, `noqa`, or an issue identifier.
- Use the file's established comment language and register unless the task requests a change. In Korean comments, prefer a direct condition and reason over ceremonial wording or duplicate English/Korean labels.
- Derive guarantees and reasons only from supplied context or inspected implementation. Missing rationale stays unknown; do not manufacture an issue, owner, TODO, future optimization, or safety guarantee.

Synthetic example with its rationale already present:

> 응답이 유실되면 서버 반영 여부를 알 수 없으므로 중복 반영 방지를 위해 자동 재시도의 수행을 금지한다.

Possible revision:

> 응답이 유실되면 서버 반영 여부를 알 수 없어 자동 재시도하면 안 된다. 중복 반영을 막기 위해서다.

Preserve the prohibition, uncertain outcome, and duplicate-write risk; "자동 재시도하면 안 된다" alone loses the reason.

## Business or strategy report

Optimize for a decision under uncertainty.

- Reframe the title around what the reader must decide.
- Put the recommendation or current conclusion on the first screen.
- Distinguish confirmed fact, measured result, estimate, assumption, and proposal.
- State which evidence supports each important conclusion.
- Tie every major unknown to business impact.
- Separate technical feasibility from customer demand, unit economics, legal readiness, and operational readiness.
- Replace absolute competitive claims with dated, sourced comparisons or verification tasks.
- Show one usable planning number when the evidence supports it. Put sensitivity ranges in a table.
- Convert the plan into decision stages with clear success criteria instead of using a generic phase plan.
- End with the decision requested and the smallest tests that reduce uncertainty.

Useful structure:

1. Decision or recommendation
2. Confirmed evidence and its limits
3. Customer or market hypothesis
4. Business model and unit economics
5. Risks and assumptions
6. Validation plan and success criteria
7. Decision inputs still required

Do not imply that technical evaluation scores prove retention, willingness to pay, market size, or profitability.

## Decision memo or project update

Optimize for shared state and ownership.

- Lead with the current state or decision.
- Include only background needed to understand the change.
- Separate completed work, evidence, decisions, risks, and open items.
- Name owners and dates only when the source provides them.
- State what changed since the last update.
- Tie blockers to the decision or dependency they affect.
- End with the next action and decision owner when known.

Preferred structure:

1. Current state
2. What changed
3. Evidence or result
4. Decision and rationale
5. Risks or blockers
6. Next actions

## Public or social thread post

Optimize for comprehension and credibility in a short reading session.

- Open with the actual observation, result, or tension. Do not use clickbait.
- Give one main idea to each paragraph or post.
- Supply enough context for a reader who did not see the underlying work.
- Prefer concrete examples, numbers, and firsthand observations from the source.
- Keep uncertainty where it matters.
- Remove report metadata, internal path names, and implementation detail that do not help the public reader.
- Avoid manufactured controversy, engagement bait, rhetorical questions, and "agree?" endings.
- End with the implication, lesson, or concrete next step.

For a multi-post thread:

1. Claim or observation
2. Context
3. Evidence or example
4. Interpretation
5. Limit or tradeoff
6. Takeaway

Use only as many posts as the content needs. Do not force a numbered sequence or a fixed count.
