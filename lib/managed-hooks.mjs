// 관리 hook 정책의 정본. Host adapter는 event를 여기로 dispatch하고,
// 정책 순서는 immutable shared runtime에 묶여 manifest digest로 검증한다.
export const MANAGED_HOOK_SCRIPTS = [
  "bind-host-worktree.py",
  "guard-protected-branch.sh",
  "guard-host-worktree.sh",
  "show-phase-status.sh",
  "comment-checker.py",
  "record-skill-read.py",
  "record-command-run.py",
  "worktree-tripwire.py",
];
export const MANAGED_HOOK_POLICY_SEQUENCES = {
  PreToolUse: {
    matcher: "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal|apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|write_file|edit_file)$",
    command: ["guard-protected-branch.sh", "guard-host-worktree.sh"],
    write: ["guard-host-worktree.sh"],
  },
  PostToolUse: {
    matcher: "^(Bash|bash|shell|run_terminal_cmd|execute_command|local_shell|terminal|apply_patch|Write|Edit|MultiEdit|write|edit|multi_edit|multiedit|write_file|edit_file|Read|read|read_file|view|cat|Skill|skill)$",
    command: [
      "record-skill-read.py",
      "record-command-run.py",
      "bind-host-worktree.py",
      "guard-host-worktree.sh",
      "worktree-tripwire.py",
    ],
    write: ["comment-checker.py", "guard-host-worktree.sh"],
    read: ["record-skill-read.py"],
  },
  Stop: {
    matcher: "",
    stop: ["show-phase-status.sh"],
  },
};
export const RETIRED_MANAGED_HOOK_SCRIPTS = [
  "guard-worktree.sh",
  "guard-worktree-write.py",
  "prepare-spec-user-prompt.py",
  "confirm-spec-user-prompt.py",
  "guard-spec-approval.sh",
];
