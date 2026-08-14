---
name: resolving-merge-conflicts
description: Use when you need to resolve an in-progress git merge/rebase conflict.
---

1. **See the current state** of the merge/rebase. Check git history and the conflicting files.

2. **Find the primary sources** for each conflict. Understand why each change was made and what the original intent was. Read commit messages, PRs, and original issues when available.

3. **Resolve each hunk.** Preserve both intents where compatible. Where they conflict, choose only when the merge goal or an accepted spec decides the trade-off. Otherwise stop and ask the user whether to abort or which intent wins. Do **not** invent new behavior.

4. Run the project checks required by the active workflow/profile and fix breakage caused by the resolution.

5. **Finish the merge/rebase.** Stage only the paths resolved for this operation, inspect the staged diff, and continue the merge/rebase. Do not sweep unrelated working-tree changes into the commit. Repeat until Git reports the operation complete.
