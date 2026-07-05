---
name: grilling
description: Stress-test a plan or design through a one-question-at-a-time interview that explores decisions, dependencies, and trade-offs. Use when the user wants to grill, stress-test, pressure-test, or sharpen a plan before implementation; pair with `domain-modeling` only when domain terms or ADR-worthy decisions need to be captured.
---


# Grilling

This is a standalone interviewing skill. It sharpens a plan through questions only; it does not create issues, triage existing tickets, file QA reports, or implement the plan.

## Quick start

1. Restate the plan or design in one sentence.
2. Ask the single most load-bearing unresolved question and include your recommended answer.
3. Wait for the user's answer before asking the next question.
4. Explore the codebase instead of asking when the repo can answer the question directly.

## Interview rules

Ask the questions one at a time, waiting for feedback on each question before continuing. Asking multiple questions at once is bewildering.

If a question can be answered by exploring the codebase, explore the codebase instead.

Do not enact the plan until I confirm we have reached a shared understanding.
