---
name: to-prd
description: Synthesizes the current conversation and codebase context into a PRD, then writes or publishes it according to the caller's requested output target. Use when the user asks to turn an already-discussed idea, plan, or conversation into a PRD; use `grilling` first if key decisions are still unresolved.
disable-model-invocation: true
---

# To PRD

This skill is for synthesis, not discovery interviewing. Do not ask a fresh question set; use only the current conversation, codebase context, and any already-settled decisions.

## Quick start

1. Explore the repo only enough to describe current state, domain language, ADR constraints, and testing seams.
2. Reuse the agreed test seams; resolve only missing or materially changed seam decisions through `grilling` before synthesis.
3. Write the PRD from the template below. Save it to the caller's requested artifact path, or publish and label it `ready-for-agent` only when the workflow or user explicitly asks for tracker publication.

Use `grilling` before this when decisions are unresolved.

The Process below expands this quick start. Synthesize the existing context rather than starting a fresh interview.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary throughout the PRD, and respect any ADRs in the area you're touching.

2. Describe the agreed seams at which the feature will be tested. Prefer existing seams and the highest public interface that exposes the required behavior. Cover the source-supported acceptance conditions without inventing seams to meet a count or hiding distinct observable contracts behind an arbitrary single-seam target.

Reuse settled decisions. If a required seam decision is missing or materially changed, use `grilling` to resolve that decision before completing synthesis.

3. Write the PRD using the template below. Save it to the caller's requested artifact path; publish it to the project issue tracker and apply the `ready-for-agent` triage label only when the workflow or user explicitly asks for tracker publication.

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A numbered list covering every source-supported user need and observable acceptance condition, without inventing scope to meet a length target. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

Cover the agreed feature scope completely; distinguish unresolved assumptions from settled stories.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it within the relevant decision and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this PRD.

## Further Notes

Any further notes about the feature.

</prd-template>
