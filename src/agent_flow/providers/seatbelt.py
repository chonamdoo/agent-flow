"""macOS Seatbelt backend built on the OS-provided `sandbox-exec`.

Chosen over a packaged sandbox runtime because it needs no dependency and the
enforcement is identical: the profile is applied by the kernel at exec time,
inherited by every descendant, and cannot be widened from inside — a nested
`sandbox-exec` with a permissive profile exits 71 instead of escaping.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from agent_flow.core.provider_sandbox import SandboxCapability, SandboxPolicy

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_PROBE_TIMEOUT_S = 30


class SeatbeltBackend:
    name = "seatbelt"

    def probe(self) -> SandboxCapability:
        if sys.platform != "darwin":
            return SandboxCapability(self.name, False, f"unsupported platform {sys.platform!r}")
        if not Path(SANDBOX_EXEC).exists() and shutil.which("sandbox-exec") is None:
            return SandboxCapability(self.name, False, "sandbox-exec not found")
        return self._self_test()

    def wrap(self, argv: Sequence[str], *, policy: SandboxPolicy) -> tuple[str, ...]:
        if not argv:
            raise ValueError("argv must not be empty")
        # `-p` keeps the profile in argv. A profile file would be a second
        # thing to keep in sync with the spawn and a window for someone to
        # swap its contents between render and exec.
        return (self._executable(), "-p", render_profile(policy), *argv)

    def _self_test(self) -> SandboxCapability:
        """Prove the rules this backend actually emits, not a simpler subset.

        `render_profile` depends on later rules overriding earlier ones twice.
        A single deny probe would pass on a `sandbox-exec` that stopped
        honouring override order, while the real profile collapsed to
        `(allow default)` beating every deny — reported healthy, enforcing
        nothing. The accepted write is checked too, so "sandbox-exec rejects
        every profile" cannot read as "the sandbox denied it".
        """
        with tempfile.TemporaryDirectory(prefix="agent-flow-sbprobe-") as raw:
            root = Path(raw).resolve()
            reopened = root / "reopened"
            reopened.mkdir()
            # A second grant, because a subtree unlink deny also covers the
            # entry: pointing both at one directory would let either rule alone
            # satisfy the probe and neither would be proven.
            shared = root / "shared"
            shared.mkdir()
            staged, kept = shared / "tmp_x", shared / "keep.txt"
            staged.write_text("x")
            kept.write_text("x")
            policy = SandboxPolicy(
                protected_roots=(root,),
                writable_subpaths=(reopened, shared),
                writable_literals=(root / "granted.txt",),
                protected_literals=(reopened / "closed.txt",),
                undeletable=(reopened,),
                undeletable_subtrees=(shared,),
                transient_globs=(f"{shared}/tmp_*",),
            )
            denied, allowed, reclosed = root / "denied.txt", reopened / "ok.txt", reopened / "closed.txt"
            granted = root / "granted.txt"
            script = "; ".join(
                (
                    # First, while `reopened` is still empty. After the writes
                    # below it is not, and rmdir would fail with ENOTEMPTY on
                    # every host — a probe that cannot fail.
                    f"rmdir {_shell_quote(reopened)} 2>/dev/null",
                    f"rm -f {_shell_quote(kept)} 2>/dev/null",
                    f"rm -f {_shell_quote(staged)} 2>/dev/null",
                    f"echo x > {_shell_quote(denied)} 2>/dev/null",
                    f"echo x > {_shell_quote(allowed)} 2>/dev/null",
                    f"echo x > {_shell_quote(granted)} 2>/dev/null",
                    f"echo x > {_shell_quote(reclosed)} 2>/dev/null",
                    "true",
                )
            )
            try:
                completed = subprocess.run(
                    (self._executable(), "-p", render_profile(policy), "/bin/sh", "-c", script),
                    capture_output=True,
                    text=True,
                    timeout=_PROBE_TIMEOUT_S,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return SandboxCapability(self.name, False, f"sandbox-exec probe failed: {exc}")
            if denied.exists():
                return SandboxCapability(self.name, False, "protected root was writable")
            if reclosed.exists():
                return SandboxCapability(self.name, False, "rule order not honoured: protected literal was writable")
            if not reopened.is_dir():
                return SandboxCapability(self.name, False, "granted directory entry was removable")
            if not kept.exists():
                return SandboxCapability(
                    self.name, False, "deletion inside a granted subtree was allowed"
                )
            if not allowed.exists():
                # No sentinel at all usually means the script never ran — the
                # common cause is an outer sandbox this one cannot nest inside,
                # which exits 71 without exec'ing. Reporting "rule order" there
                # sends the operator to read SBPL semantics instead of looking
                # one level up, so carry what sandbox-exec actually said.
                detail = (completed.stderr or completed.stdout or "").strip().splitlines()
                reason = "rule order not honoured: reopened subpath was not writable"
                if completed.returncode != 0:
                    reason = f"sandbox-exec exited {completed.returncode}"
                    if detail:
                        reason += f": {detail[-1]}"
                return SandboxCapability(self.name, False, reason)
            if not granted.exists():
                # After the exit-code branch on purpose: a script that never
                # ran leaves this sentinel missing too, and the answer there is
                # the exit status, not a rule-order verdict.
                return SandboxCapability(
                    self.name, False, "rule order not honoured: granted literal was not writable"
                )
            if staged.exists():
                # Also after the exit-code branch: an unparseable regex group
                # makes sandbox-exec reject the whole profile, and that shows
                # up as the exit status rather than as an unhonoured allow.
                return SandboxCapability(
                    self.name, False, "allow-back for staging names was not honoured"
                )
        return SandboxCapability(self.name, True)

    def _executable(self) -> str:
        if Path(SANDBOX_EXEC).exists():
            return SANDBOX_EXEC
        resolved = shutil.which("sandbox-exec")
        if resolved is None:
            raise FileNotFoundError("sandbox-exec not found")
        return resolved


def render_profile(policy: SandboxPolicy) -> str:
    """Render the policy as SBPL.

    Rule order is the contract: later rules win, so the protected subtree is
    closed first, the worker's own paths are reopened inside it, and single
    files that must stay closed are denied last.

    The trailing `file-write-unlink` groups are a different verb, so they do
    not participate in that ordering. They keep the granted entries from being
    removed or renamed, and the object database from being emptied, while
    every write inside them stays allowed. Within the verb the same
    later-wins rule applies, so the staging names are allowed back last.
    """
    lines = ["(version 1)", "(allow default)"]
    if policy.protected_roots:
        lines.append(_rule("deny", (f'(subpath "{_sbpl_string(p)}")' for p in policy.protected_roots)))
    writable = [f'(subpath "{_sbpl_string(p)}")' for p in policy.writable_subpaths]
    writable += [f'(literal "{_sbpl_string(p)}")' for p in policy.writable_literals]
    # No device allowlist: the profile opens with `(allow default)` and no
    # protected root covers `/dev`, so terminal and null writes are already
    # permitted. Listing them would read as load-bearing and outlive a future
    # change that made it matter.
    if writable:
        lines.append(_rule("allow", writable))
    if policy.protected_literals:
        lines.append(_rule("deny", (f'(literal "{_sbpl_string(p)}")' for p in policy.protected_literals)))
    undeletable = [f'(literal "{_sbpl_string(p)}")' for p in policy.undeletable]
    undeletable += [f'(subpath "{_sbpl_string(p)}")' for p in policy.undeletable_subtrees]
    if undeletable:
        lines.append(_rule("deny", undeletable, verb_target="file-write-unlink"))
    if policy.undeletable_subtrees:
        # Truncating an object in place loses exactly what removing it loses,
        # so the subtree closes both verbs. New objects are unaffected: git
        # writes their bytes into a staging name and hard-links that into
        # place, so the final path never takes a data write.
        lines.append(
            _rule(
                "deny",
                [f'(subpath "{_sbpl_string(p)}")' for p in policy.undeletable_subtrees],
                verb_target="file-write-data file-write-mode",
            )
        )
    if policy.transient_globs:
        staging = [f'(regex #"{_sbpl_regex(g)}")' for g in policy.transient_globs]
        lines.append(_rule("allow", staging, verb_target="file-write-unlink"))
        lines.append(_rule("allow", staging, verb_target="file-write-data file-write-mode"))
    return "\n".join(lines) + "\n"


def _rule(verb: str, clauses, *, verb_target: str = "file-write*") -> str:
    body = "\n  ".join(clauses)
    return f"({verb} {verb_target}\n  {body})"


_REGEX_META = frozenset('\\.^$*+?()[]{}|')


def _sbpl_regex(glob: str) -> str:
    """An absolute glob as an anchored SBPL regex.

    Only `*` is a wildcard and it never crosses a `/`, so a pattern cannot
    widen past the directory it names. Everything else is escaped with a
    single backslash — including the `.` in a name like `maintenance.lock`,
    which unescaped would match any character and turn one allow-back into a
    wider hole than the deny it punches through.

    Single, not doubled: `#"..."` hands its bytes to the regex engine without
    unescaping them, so the doubling `_sbpl_string` needs would arrive as an
    escaped backslash and the pattern would match nothing. Measured: `\\\\.`
    stops matching the path it names, `\\.` matches it.

    A `"` or a newline in the path has no representation here — sandbox-exec's
    reader ends the literal at the quote. Refuse rather than emit a rule that
    parses as something else; the caller turns this into a spawn refusal.
    """
    if '"' in glob or "\n" in glob:
        raise ValueError(f"cannot express {glob!r} as an SBPL regex: quote or newline in path")
    return "^" + "[^/]*".join(
        "".join("\\" + ch if ch in _REGEX_META else ch for ch in part)
        for part in glob.split("*")
    ) + "$"


def _sbpl_string(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _shell_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"
