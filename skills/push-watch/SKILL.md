---
name: push-watch
description: Use inside an active Agent Flow pr-watch phase after local verification, when publishing the branch is already authorized by the user or active workflow.
---

# Push Watch

Use this skill after local verification inside an active `pr-watch` phase with publication authority from the current user request or active workflow. A ready branch alone is not permission to publish. Reuse existing authorization within its scope rather than asking for it again.

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
