"""Kernel-enforced write boundary for processes agent-flow spawns.

Worktree separation is not a security boundary. A spawned CLI that writes an
absolute path reaches the leader checkout no matter what its cwd is, so
`assert_cwd_bound` cannot see it and `assert_leader_unchanged` only reports it
after the bytes have already landed.

The policy allows by default and denies a closed set. The inverse — deny by
default plus a write allowlist — requires enumerating every path a real agent
legitimately writes (build output, package caches, scratch dirs, provider
config). That list has no upper bound, and every gap in it is a false
rejection. The set that must *not* change is bounded and known: the leader
checkout, sibling worktrees, git common metadata, other workers' run state,
and the worktree's own gitdir pointer.

Expected noise: git's auto-maintenance tries to take `<common>/packed-refs.lock`
on commit and is refused, so a sandboxed worker sees
`error: Unable to create '.../packed-refs.lock': Operation not permitted` on
stderr. The commit still succeeds. Re-allowing that file is not the fix — it
would let one worker rewrite every ref in the repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, Union

from agent_flow.core.worktree_isolation import RUNTIME_WRITE_PATHS, real_path


class SandboxUnavailableError(RuntimeError):
    """The host cannot enforce the boundary; the caller must fail closed.

    Never downgrade to an unsandboxed spawn on this error. An unenforced run
    that looks enforced is worse than a refusal, because the caller stops
    watching.
    """


@dataclass(frozen=True)
class SandboxCapability:
    backend: str
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class SandboxPolicy:
    """Write rules for one spawn, in the order a backend must apply them.

    Later groups override earlier ones: `protected_roots` closes a subtree,
    `writable_*` reopens the worker's own paths inside it, and
    `protected_literals` re-closes individual files that sit inside a reopened
    subtree.

    `undeletable` and `undeletable_subtrees` are a separate verb, not an
    ordering step. An SBPL subpath grant covers the granted *entry* as well as
    its contents, so a granted directory can be removed and replaced with a
    symlink pointing anywhere — and the next trusted writer follows it.
    `undeletable` denies unlink on the entry itself, which is enough wherever
    the contents belong to the worker.

    `undeletable_subtrees` covers the one grant where they do not: the shared
    object database. It must be writable — the worker's commits land there —
    but its contents are every other checkout's history, so deletion inside it
    is denied too. `transient_globs` punches back the staging names git creates
    and immediately renames, which a rename cannot do without unlink.
    """

    protected_roots: tuple[Path, ...]
    writable_subpaths: tuple[Path, ...]
    writable_literals: tuple[Path, ...]
    protected_literals: tuple[Path, ...]
    undeletable: tuple[Path, ...]
    undeletable_subtrees: tuple[Path, ...] = ()
    transient_globs: tuple[str, ...] = ()


class SandboxBackend(Protocol):
    """A backend enforces the whole policy or refuses.

    There is no partial implementation: a backend that can only enforce some
    of the rules is not substitutable for one that enforces all of them, and
    the caller has no way to tell the difference at runtime.
    """

    name: str

    def probe(self) -> SandboxCapability: ...

    def wrap(self, argv: Sequence[str], *, policy: SandboxPolicy) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class SandboxedSpawn:
    backend: SandboxBackend
    policy: SandboxPolicy
    capability: SandboxCapability

    def __post_init__(self) -> None:
        # Proven at construction, not at spawn. The pool folds a spawn-time
        # exception into one job's result, which turns a host-wide capability
        # failure into N flaky-looking children while the phase carries on.
        require_capability(self.capability)


@dataclass(frozen=True)
class UnboundedSpawn:
    """Chosen absence of a boundary, for the case where none can apply.

    A spawn running in the leader checkout has no outer checkout to protect,
    so a policy would deny nothing. That is a legitimate state, but it must be
    stated at the call site rather than inferred from a missing field —
    otherwise the one caller that forgets to pass a boundary looks identical
    to the one that decided it needs none.
    """

    reason: str


# Union rather than `X | Y`: this is a runtime expression and the repo's test
# command runs on the system interpreter, which is still 3.9 here.
SpawnBoundary = Union[SandboxedSpawn, UnboundedSpawn]


def prove_sandbox(backend: SandboxBackend, policy: SandboxPolicy) -> SandboxedSpawn:
    """Probe the backend and bind the proof to the spawn, or raise."""
    return SandboxedSpawn(backend=backend, policy=policy, capability=backend.probe())


def resolve_spawn_argv(boundary: SpawnBoundary, argv: Sequence[str]) -> tuple[str, ...]:
    """Wrap ``argv`` for the boundary. Capability was proven at construction."""
    if isinstance(boundary, UnboundedSpawn):
        return tuple(argv)
    return boundary.backend.wrap(argv, policy=boundary.policy)


# Sourced from the tripwire's own whitelist so the two cannot drift: a write
# the tripwire calls legitimate must not be one the kernel refuses.
#
# `.agent-flow/worktrees` is the one entry that inverts. The tripwire skips it
# because the checkouts under it are the workers' own output; re-allowing it in
# a write policy would reopen every sibling checkout that `protected_roots`
# just closed. The worker's own checkout is granted explicitly instead.
#
# Note what this opens: `team` and `state` hold cross-worker claim records, so
# a worker can overwrite another's claim, and the tripwire excludes the same
# paths so it will not report that either. Coordination state is not protected
# by this boundary — only checkouts and git metadata are.
_LEADER_RUNTIME_WRITES = tuple(
    relative for relative in RUNTIME_WRITE_PATHS if relative != ".agent-flow/worktrees"
)


def derive_sandbox_policy(
    *,
    worktree: Path,
    leader_root: Path,
    git_common_dir: Path,
    worktree_git_dir: Path,
    branch: str | None,
    run_state_dir: Path | None = None,
    sibling_roots: Sequence[Path] = (),
) -> SandboxPolicy:
    """Build the policy for one worker from live paths.

    Nothing here is cached or persisted. A stored policy would let a moved or
    replaced worktree inherit grants that no longer describe it, which is the
    class of failure this module exists to prevent.
    """
    resolved_worktree = _absolute(worktree, "worktree")
    resolved_common = _absolute(git_common_dir, "git_common_dir")
    resolved_admin = _absolute(worktree_git_dir, "worktree_git_dir")

    protected_roots = _unique(
        [_absolute(leader_root, "leader_root"), resolved_common]
        + [_absolute(path, "sibling_roots") for path in sibling_roots]
    )

    resolved_leader = _absolute(leader_root, "leader_root")
    # `objects` can be a symlink to shared storage. An unresolved subpath
    # matches nothing, and the write then lands outside the denied tree
    # where allow-default covers it — a dead rule, not a closed one.
    objects = real_path(resolved_common / "objects")
    writable = [resolved_worktree, objects, resolved_admin]
    if run_state_dir is not None:
        writable.append(_absolute(run_state_dir, "run_state_dir"))
    # The leader-side state a worker legitimately appends to from inside its
    # worktree: the runner writes `runs/`, the read hook appends
    # `skills-read.jsonl`. Denying these produces a PermissionError the hooks
    # swallow, so the evidence file stops growing and the gates that consume it
    # degrade to "unavailable" and pass — a guard switching off with no signal.
    writable += [resolved_leader / relative for relative in _LEADER_RUNTIME_WRITES]
    writable_dirs = _unique(writable)
    ref_parents, ref_leaves = branch_write_targets(resolved_common, branch)

    return SandboxPolicy(
        protected_roots=protected_roots,
        writable_subpaths=writable_dirs,
        writable_literals=ref_parents + ref_leaves,
        # Pointer files on both sides of the worktree link. `<worktree>/.git`
        # names the admin dir; `gitdir`/`commondir` name the checkout and the
        # repository. They sit inside subtrees we just reopened, and the paths
        # they record are what `git worktree list` reports — which is how the
        # next policy enumerates the siblings it must protect.
        #
        # `objects/info/alternates` is the same kind of file for the object
        # store: it names another directory to read objects from, so a worker
        # that writes it decides what history the leader sees. Set at clone
        # time and never by a worker.
        protected_literals=(
            resolved_worktree / ".git",
            resolved_admin / "gitdir",
            resolved_admin / "commondir",
            objects / "info" / "alternates",
        ),
        # Every granted entry, files included: the two leader jsonl logs are
        # append-only, so denying rename-over costs nothing. Not the ref
        # leaves — git replaces those by renaming `<ref>.lock` over the ref.
        undeletable=_unique(list(writable_dirs) + list(ref_parents)),
        # The object database is the one grant whose contents are not the
        # worker's own. Losing them destroys every checkout's history and
        # nothing in the repository can restore it, so deletion is denied
        # inside the subtree, not just at its entry.
        undeletable_subtrees=(objects,),
        transient_globs=_object_staging_globs(objects),
    )


def require_capability(capability: SandboxCapability) -> None:
    if capability.available:
        return
    raise SandboxUnavailableError(
        f"sandbox backend {capability.backend!r} cannot enforce the write boundary: "
        f"{capability.reason or 'no reason reported'}"
    )


def _object_staging_globs(objects: Path) -> tuple[str, ...]:
    """Names git creates inside the object database and then removes itself.

    Loose objects are staged as `tmp_obj_*` and moved into place. Measured on
    git 2.50.1 over APFS the move is `link` + `unlink`, so denying unlink
    across the subtree does not fail the commit — the object lands and git
    prints `warning: unable to unlink`. What it leaves behind is one staging
    file per object in a shared database whose only reaper, `gc`, this policy
    denies. Allowing the staging names back keeps that from accumulating, and
    costs nothing an attacker can spend: real objects are hex, `tmp_` is the
    prefix `write_loose_object` and `index-pack` stage under, and the pattern
    cannot leave the object directory.
    """
    return (
        f"{objects}/tmp_*",
        f"{objects}/*/tmp_*",
        f"{objects}/maintenance.lock",
    )


def branch_write_targets(
    common: Path, branch: str | None
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """The directories and the files git touches to move one branch.

    Returned separately because they need different treatment: the leaves must
    stay unlinkable (git replaces a ref by renaming its `.lock` over it) while
    the directories must not be, or the shared `refs/heads/feat` prefix can be
    swapped for a symlink.

    Only this worker's branch is listed. Opening `refs/heads/` as a subtree
    would let concurrent workers overwrite each other's refs, which is the
    cross-contamination the worktree split is supposed to prevent.

    Measured cost of that closed set, from a real rendered policy: `commit`,
    `reset`, `checkout`, `merge` and `push` work; `fetch`, `pull`, `tag`,
    `stash`, `branch <new>` and `gc` are refused, because each needs a lock on
    a ref this worker does not own. `fetch`/`pull` are the ones a worker loop
    hits first — a "rebase onto latest main" instruction fails with a
    permission error the agent cannot act on. Fetching is the trusted parent's
    job.
    """
    if not branch:
        return (), ()
    segments = branch.split("/")
    parents: list[Path] = []
    leaves: list[Path] = []
    for base in (common / "refs" / "heads", common / "logs" / "refs" / "heads"):
        leaf = base.joinpath(*segments)
        # Every directory git must create on the way to a hierarchical ref.
        # `git pack-refs --prune` — which `git gc` runs — deletes the empty
        # ones, so a policy that grants only the leaf works until the first gc
        # and then refuses every commit with "unable to create directory".
        parent = leaf.parent
        while parent != base:
            parents.append(parent)
            parent = parent.parent
        # git writes `<ref>.lock` first and renames; denying the lock denies
        # the commit with a confusing "cannot lock ref" error.
        leaves.extend((leaf, Path(f"{leaf}.lock")))
    return tuple(parents), tuple(leaves)


def _absolute(value: Path, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path: {path}")
    # Sandbox profiles match the resolved path. On macOS `/tmp` and `/var` are
    # symlinks into `/private`, so an unresolved path silently matches nothing
    # and every rule built from it is dead.
    return real_path(path)


def _unique(paths: Sequence[Path]) -> tuple[Path, ...]:
    seen: dict[str, Path] = {}
    for path in paths:
        seen.setdefault(str(path), path)
    return tuple(seen.values())
