"""The graded tree, frozen once after drive (#409 v3.14 C).

Every arm is graded on its working tree as the run left it, committed or not, by the same rule: every file under the
checkout except `.git`, dependency and cache folders (`SKIP_NAMES`) and the kit's runner state (`SKIP_PREFIXES`).
`freeze` copies that tree to `<run>/graded/tree`, read-only, writes `manifest.json` (path, type, mode, symlink target,
content sha256 of each entry; the HEAD it sat on, the start commit and the uncommitted paths `git status` showed) and
keeps the commit history as `repo.bundle`. `score` and `chain-next` only ever read this snapshot: `verify` recomputes
the manifest and refuses a tree that changed, `materialize` makes a throwaway git checkout whose HEAD is the recorded
commit and whose working tree is the snapshot, so git-based checks (integrity, commits, uncommitted) see what the
agent left.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

VERSION = "graded-snapshot-v1"
SKIP_NAMES = (".git", "node_modules", "__pycache__", ".gradle")
SKIP_PREFIXES = (".agent-flow/runs",)
DIR = "graded"
WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class SnapshotError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, check: bool = False) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and done.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)}: {done.stderr.strip()[-300:]}")
    return done.stdout.strip() if done.returncode == 0 else ""


def _skipped(relative: str) -> bool:
    parts = relative.split("/")
    return bool(set(parts) & set(SKIP_NAMES)) or any(relative == prefix or relative.startswith(prefix + "/")
                                                       for prefix in SKIP_PREFIXES)


GRADLE_SCRIPTS = ("build.gradle.kts", "build.gradle")


def _gradle_output(base: Path, name: str) -> bool:
    """A Gradle module's `build/` output folder: named `build`, next to that module's build script. The evaluator
    rebuilds from source, so freezing hundreds of MB of intermediates per Android run only fills the disk."""
    return name == "build" and any((base / script).is_file() for script in GRADLE_SCRIPTS)


def entries(root: Path, skip_gradle_build: bool = True) -> list[dict]:
    """The manifest rows of a tree (files, symlinks; directories are implied), sorted by path. Modes are kept without
    the write bits (the frozen copy has none). Gradle `build/` outputs are left out unless `skip_gradle_build` is false
    (snapshots frozen before that rule)."""
    rows = []
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        kept_dirs = []
        for name in sorted(dirs):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if _skipped(relative) or (skip_gradle_build and _gradle_output(base, name)):
                continue
            if path.is_symlink():
                files.append(name)
                continue
            kept_dirs.append(name)  # directories are implied by their entries, as in git
        dirs[:] = kept_dirs
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if _skipped(relative):
                continue
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode) & ~WRITE_BITS
            if stat.S_ISLNK(info.st_mode):
                rows.append({"path": relative, "type": "symlink", "mode": mode, "target": os.readlink(path)})
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                rows.append({"path": relative, "type": "file", "mode": mode, "size": info.st_size, "sha256": digest})
            else:
                rows.append({"path": relative, "type": "other", "mode": mode})
    return sorted(rows, key=lambda row: row["path"])


def entries_sha256(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def freeze(checkout: Path, run: Path, start_commit: str | None, source_label: str | None = None) -> dict:
    """Copies the checkout into `<run>/graded/` once. A second freeze of the same run is refused."""
    target = run / DIR
    if target.exists():
        raise SnapshotError(f"{target} already exists: a run is frozen once")
    tree = target / "tree"
    tree.mkdir(parents=True)
    rows = entries(checkout)
    for row in rows:
        source, copy = checkout / row["path"], tree / row["path"]
        if row["type"] == "dir":
            copy.mkdir(parents=True, exist_ok=True)
        elif row["type"] == "symlink":
            copy.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(row["target"], copy)
        elif row["type"] == "file":
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copy, follow_symlinks=False)
    head = _git(["rev-parse", "HEAD"], checkout) or None
    branch = _git(["branch", "--show-current"], checkout) or None
    status = _git(["status", "--porcelain", "--untracked-files=all", "-z"], checkout)
    uncommitted = sorted({item[3:] for item in status.split("\0") if len(item) > 3 and not _skipped(item[3:])})
    bundle = None
    if head:
        _git(["bundle", "create", "-q", str(target / "repo.bundle"), "HEAD"], checkout, check=True)
        bundle = hashlib.sha256((target / "repo.bundle").read_bytes()).hexdigest()
    manifest = {"version": VERSION, "source": source_label or str(checkout), "head": head, "branch": branch,
                "start_commit": start_commit, "uncommitted": uncommitted, "has_uncommitted": bool(uncommitted),
                "entries": rows, "files": sum(row["type"] == "file" for row in rows),
                "tree_sha256": entries_sha256(rows), "bundle_sha256": bundle,
                "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "skip_names": list(SKIP_NAMES), "skip_prefixes": list(SKIP_PREFIXES), "skip_gradle_build": True}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    _read_only(target)
    return record(manifest)


def record(manifest: dict) -> dict:
    """What a run record keeps of a snapshot (everything but the entry list)."""
    return {key: value for key, value in manifest.items() if key != "entries"}


def _read_only(root: Path) -> None:
    for current, dirs, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~WRITE_BITS)
        for name in dirs:
            path = Path(current) / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~WRITE_BITS)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~WRITE_BITS)


def load(run: Path) -> dict | None:
    path = run / DIR / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def verify(run: Path) -> dict:
    """The snapshot's manifest, after checking that its tree and bundle are what was frozen."""
    manifest = load(run)
    if manifest is None:
        raise SnapshotError(f"{run} has no graded snapshot")
    rows = entries(run / DIR / "tree", skip_gradle_build=bool(manifest.get("skip_gradle_build")))
    if entries_sha256(rows) != manifest["tree_sha256"]:
        raise SnapshotError("the graded snapshot changed after it was frozen")
    bundle = run / DIR / "repo.bundle"
    if manifest.get("bundle_sha256") and hashlib.sha256(bundle.read_bytes()).hexdigest() != manifest["bundle_sha256"]:
        raise SnapshotError("the graded snapshot's history changed after it was frozen")
    return manifest


def materialize(run: Path, dest: Path) -> Path:
    """A writable git checkout of the snapshot at `dest`: HEAD and history from the bundle, the tree as frozen."""
    manifest = verify(run)
    bundle = run / DIR / "repo.bundle"
    if manifest.get("head") and bundle.is_file():
        _git(["clone", "-q", "--no-checkout", str(bundle), str(dest)], dest.parent, check=True)
        for args in (["checkout", "-q", "-B", manifest.get("branch") or "main", manifest["head"]],
                     ["remote", "remove", "origin"], ["config", "user.name", "eval"],
                     ["config", "user.email", "eval@example.com"], ["config", "commit.gpgsign", "false"]):
            _git(args, dest)
        for item in dest.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
    else:
        dest.mkdir(parents=True)
    tree = run / DIR / "tree"
    for row in manifest["entries"]:
        source, copy = tree / row["path"], dest / row["path"]
        if row["type"] == "dir":
            copy.mkdir(parents=True, exist_ok=True)
        elif row["type"] == "symlink":
            copy.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(row["target"], copy)
        elif row["type"] == "file":
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copy)
            copy.chmod(row["mode"] | stat.S_IWUSR)
    for row in manifest["entries"]:
        if row["type"] == "dir":
            (dest / row["path"]).chmod(row["mode"] | stat.S_IWUSR)
    return dest


def unfreeze(run: Path) -> None:
    """Gives the snapshot its write bits back so the run directory can be deleted (tests, cleanup)."""
    root = run / DIR
    for current, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(current) / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
    if root.exists():
        root.chmod(stat.S_IMODE(root.stat().st_mode) | stat.S_IWUSR)
