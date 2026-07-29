// 관리형 hook 목록. 두 진입점이 심고 Python `hook_integrity`가 검증한다.
// 세 곳이 같아야 하는데 Node 쪽이 두 벌이면 한쪽만 고쳐도 절반만 반영된다.
// 언어 경계는 합칠 수 없으므로 Node 1벌 + Python 1벌로 줄이고, 둘이 같은지는
// pytest가 확인한다.
export const MANAGED_HOOK_SCRIPTS = [
  "bind-host-worktree.py",
  "confirm-spec-user-prompt.py",
  "guard-protected-branch.sh",
  "guard-host-worktree.sh",
  "guard-spec-approval.sh",
  "prepare-spec-user-prompt.py",
  "show-phase-status.sh",
  "comment-checker.py",
  "record-skill-read.py",
  "record-command-run.py",
  "worktree-tripwire.py",
];
export const RETIRED_MANAGED_HOOK_SCRIPTS = ["guard-worktree.sh", "guard-worktree-write.py"];
