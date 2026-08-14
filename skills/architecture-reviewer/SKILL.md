---
name: architecture-reviewer
description: Use during the full-feature architecture-review phase.
---

# Architecture Reviewer

Use during the full-feature architecture-review phase.

Review implemented code against domain decisions and DDD/Clean Architecture. Aggregate only the per-angle artifacts produced by the runner's confined Claude/Codex reviewer subprocesses; do not launch reviewer CLIs yourself. Each reviewer section must include `reviewer-source: sub-agent`. OMP and controller-session work are never reviewer providers.

Artifact template:

# Architecture Review

## Reviewer 1
reviewer-source: sub-agent
verdict: approve | request-changes

## Findings

## Domain Alignment

## Layer Violations

## Repository Boundary Issues

## Dependency Direction Issues

## Required Refactors

## Approved Exceptions

## Reviewer 2
reviewer-source: sub-agent
verdict: approve | request-changes

## Findings

## Overall
verdict: approve | request-changes

## Completion Gate

Use only the markers supplied by the active phase. Do not copy a marker list into this skill.
