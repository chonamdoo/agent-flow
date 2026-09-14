# Review Angle - Architecture Contract

Use the selected architecture contract supplied in this reviewer's own prompt.
Output strictly markdown findings. Do not propose code unless asked.

## Local mode

Read the complete normative root and every `requires_docs` document included in
the prompt. Assess every applicable requirement, permitted alternative, and
exception against actual changed code and the approved design. Cite both the
normative rule and implementation evidence for a violation; missing evidence is
not a pass.

Check the contract's dependency and ownership rules, action composition,
persistence and cache policy, representation and error boundaries, dependency
wiring, and testability wherever they apply. A feature-colocated or
framework-aware design is valid when the selected contract permits it. Preserve
shared security, data-integrity, cancellation, and interface-contract checks;
architecture selection is not an exemption from them.

An applicable required-rule violation produces `verdict: request-changes`.
Record `must-avoid-check: pass|fail` for the selected contract's prohibitions.
Retain the phase's completion markers and review-category coverage, explaining
the selected-contract evidence or a permitted `n/a` for each category. Legacy
Clean-named review categories record this assessment, not adoption of Clean
layers or permission to skip the review. A fixed legacy
`clean-architecture: applied` marker certifies selected-contract assessment
where the workflow requires it; document the actual mode and contract rather
than claiming an unselected Clean skill was read.

## Clean mode

Apply every applicable requirement, alternative, and exception in the required
`clean-architecture-core` and matching platform skills resolved for this
reviewer's own input. The core owns the full semantic rubric, Error Boundary,
Must Avoid sweep, and Review Checklist. Its required
`code-generation-discipline` owns the full SOLID Boundaries.
Assess actual changed code and the approved design; a read/applied marker alone
does not establish compliance.

## Completion assessment

The active phase's `required_markers` owns its legacy completion categories and
allowed values; the core's Review Checklist supplies applicable semantic checks
in Clean mode. Assess all of them without copying a second compatibility list.
Retain `clean-architecture` and `clean-architecture-review` markers wherever the
phase requires them: they are evidence contracts, not required skill names.
Use the core's applicability rule for use-case `n/a` values and keep every other
phase enum and conditional guard unchanged.

Return assessment and findings on stdout. Completion criteria are review
subjects, not permission to write artifacts, run workflow commands, or advance
the phase. The author/controller owns aggregation and artifact writing.

## Output format

```markdown
## Architecture Contract review findings

### Must-fix
- <severity:high> [path:line] <boundary violation>. Why: <one sentence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...

### Overall
verdict: approve | request-changes
```

Cite paths as `path/to/file:line`. If a category is empty, write `none`.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
