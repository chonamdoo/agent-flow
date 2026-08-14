---
name: push-watch
description: Use this skill after local verification is complete and the branch is ready to publish.
---

# Push Watch

Use this skill after local verification is complete and the branch is ready to publish.

This skill runs inside the active workflow's `pr-watch` phase. Do not start a
new run. Read `agent-flow status` and execute its exact `next_command`.

Flow:

1. Sanity check the branch and working tree.
2. Commit and push the current branch.
3. Open or record the pull request.
4. Watch PR checks and review threads.
5. Route failures through `pr-comment-fix` or `pr-ci-fix`; comment fixes must also resolve the corresponding GitHub review threads.
6. Push again and return to `pr-watch`.
7. When checks and comments are green, route to `merge`.

Rules:

- Protected branches are blocked: main, master, develop.
- Record PR watch state with `status: green`, `status: comments`, `status: ci-failed`, or `status: pending`.
- merge requires explicit approval. Do not merge unattended.
