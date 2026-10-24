"""Linux write confinement for read-only provider runs (#422): Landlock, then exec.

    python -I landlock_launch.py --write <scratch> -- <provider argv...>

Needs Landlock ABI 3 (Linux 6.2): ABI 1 and 2 cannot handle truncation. Reads stay allowed.
Every write-class filesystem access is handled by the ruleset, and only
`--write` directories (all write rights) and `/dev/null` (file writes) are allowed. The rules hold
for the provider and every process it starts; `PR_SET_NO_NEW_PRIVS` keeps a setuid child from
shedding them. Any failure exits 126 before the provider runs: no confinement, no launch.

This is the macOS reviewer profile (`(deny file-write*)`, scratch and `/dev/null` allowed) in
Landlock terms. Landlock cannot re-deny a path under an allowed directory, so the runner uses this
backend only for runs without workspace writes. Run as a script with `-I`: it imports nothing from
the provider's working directory.
"""
from __future__ import annotations

import ctypes
import os
import sys

# x86_64와 aarch64 모두 generic syscall 번호를 쓴다.
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
O_PATH = 0o10000000
O_CLOEXEC = 0o2000000

WRITE_FILE = 1 << 1
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13  # ABI 2
TRUNCATE = 1 << 14  # ABI 3
# ABI 1·2는 truncate를 막지 못한다. 그 커널에서는 scratch 밖 파일을 비울 수 있으므로 쓰지 않는다.
MIN_ABI = 3


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def write_access(abi: int) -> int:
    """Every write-class right this ABI can handle. REFER and TRUNCATE exist only from ABI 2 and 3."""
    access = (WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK
              | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM)
    if abi >= 2:
        access |= REFER
    if abi >= 3:
        access |= TRUNCATE
    return access


def abi_version(libc: ctypes.CDLL | None = None) -> int:
    """The kernel's Landlock ABI, or 0 when Landlock is missing or disabled."""
    if not sys.platform.startswith("linux"):
        return 0
    libc = libc or ctypes.CDLL(None, use_errno=True)
    version = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                           ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    return version if version > 0 else 0


def _fail(message: str) -> None:
    sys.stderr.write(f"agent-flow landlock: {message}; provider not started\n")
    sys.exit(126)


def confine(writable: list[str]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = abi_version(libc)
    if abi < MIN_ABI:
        _fail(f"Landlock ABI {abi} is below {MIN_ABI}" if abi else "Landlock is unavailable")
    handled = write_access(abi)
    attr = _RulesetAttr(handled)
    ruleset = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                           ctypes.c_uint32(0))
    if ruleset < 0:
        _fail(f"create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    file_rights = WRITE_FILE | (TRUNCATE if abi >= 3 else 0)
    for path, rights in [*((p, handled) for p in writable), ("/dev/null", file_rights)]:
        try:
            fd = os.open(path, O_PATH | O_CLOEXEC)
        except OSError as exc:
            _fail(f"cannot open {path}: {exc.strerror}")
        rule = _PathBeneathAttr(rights, fd)
        added = libc.syscall(SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                             ctypes.byref(rule), ctypes.c_uint32(0))
        os.close(fd)
        if added < 0:
            _fail(f"add_rule {path} failed: {os.strerror(ctypes.get_errno())}")
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _fail(f"PR_SET_NO_NEW_PRIVS failed: {os.strerror(ctypes.get_errno())}")
    if libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
        _fail(f"restrict_self failed: {os.strerror(ctypes.get_errno())}")
    os.close(ruleset)


def main(argv: list[str]) -> None:
    writable: list[str] = []
    rest = list(argv)
    while rest and rest[0] == "--write":
        if len(rest) < 2:
            _fail("--write needs a directory")
        writable.append(rest[1])
        rest = rest[2:]
    if not rest or rest[0] != "--" or len(rest) < 2:
        _fail("usage: --write <dir> ... -- <argv>")
    if not writable or not all(os.path.isabs(path) and os.path.isdir(path) for path in writable):
        _fail("--write must name existing absolute directories")
    confine(writable)
    try:
        os.execv(rest[1], rest[1:])
    except OSError as exc:
        _fail(f"exec {rest[1]} failed: {exc.strerror}")


if __name__ == "__main__":
    main(sys.argv[1:])
