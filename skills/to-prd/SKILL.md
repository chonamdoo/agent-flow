---
name: to-prd
description: Synthesizes the current conversation and codebase context into a PRD, then writes or publishes it according to the caller's requested output target. Use when the user asks to turn an already-discussed idea, plan, or conversation into a PRD; use `grilling` first if key decisions are still unresolved.
disable-model-invocation: true
---

# To PRD

This skill is for synthesis, not discovery interviewing. Do not ask a fresh question set; use only the current conversation, codebase context, and any already-settled decisions.

## Quick start

1. Confirm the issue tracker and triage-label conventions exist; run `/setup-matt-pocock-skills` if they do not.
2. Explore the repo only enough to describe current state, domain language, ADR constraints, and testing seams.
3. Confirm the intended test seams with the user.
4. Write the PRD from the template below. Save it to the caller's requested artifact path, or publish and label it `ready-for-agent` only when the workflow or user explicitly asks for tracker publication.

Use `grilling` before this when decisions are unresolved. Use `to-issues` after this when the PRD needs implementation slices. Use `qa` for bug-report intake and `triage` for existing tracker items.

This skill takes the current conversation context and codebase understanding and produces a PRD. Do NOT interview the user — just synthesize what you already know.

The issue tracker and triage label vocabulary should have been provided to you — run `/setup-matt-pocock-skills` if not.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary throughout the PRD, and respect any ADRs in the area you're touching.

2. Sketch out the seams at which you're going to test the feature. Existing seams should be preferred to new ones. Use the highest seam possible. If new seams are needed, propose them at the highest point you can. The fewer seams across the codebase, the better - the ideal number is one.

Check with the user that these seams match their expectations.

3. Write the PRD using the template below. Save it to the caller's requested artifact path; publish it to the project issue tracker and apply the `ready-for-agent` triage label only when the workflow or user explicitly asks for tracker publication.

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

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
