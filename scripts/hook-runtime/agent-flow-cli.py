#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.abc
import importlib.util
from importlib.machinery import ModuleSpec
import io
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import BinaryIO, Iterator, NoReturn

MANIFEST_NAME = "runtime-manifest.json"
RUNTIME_PREFIX = "runtime/python/"


def _fail(message: str) -> NoReturn:
    raise SystemExit("agent-flow: verified CLI runtime failed: " + message)


def _read_fd(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_owned(path: Path, *, mode: int, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            _fail(label + " is not a regular file")
        if identity.st_uid != os.getuid():
            _fail(label + " is not owned by the current user")
        if identity.st_nlink != 1:
            _fail(label + " has an unsafe link count")
        if stat.S_IMODE(identity.st_mode) != mode:
            _fail(label + " has an unsafe mode")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_owned_directory(path: Path, *, label: str) -> None:
    identity = path.lstat()
    if not stat.S_ISDIR(identity.st_mode) or path.is_symlink():
        _fail(label + " is not a regular directory")
    if identity.st_uid != os.getuid():
        _fail(label + " is not owned by the current user")
    if identity.st_nlink < 1:
        _fail(label + " has an unsafe link count")
    if stat.S_IMODE(identity.st_mode) != 0o555:
        _fail(label + " has an unsafe mode")


def _bundle_entries(root: Path) -> list[str]:
    entries: list[str] = []

    def visit(directory: Path, relative: str) -> None:
        _assert_owned_directory(directory, label="runtime bundle directory")
        for child in directory.iterdir():
            child_relative = f"{relative}/{child.name}" if relative else child.name
            identity = child.lstat()
            if stat.S_ISLNK(identity.st_mode):
                _fail("runtime bundle contains a symlink: " + child_relative)
            if stat.S_ISDIR(identity.st_mode):
                visit(child, child_relative)
            elif stat.S_ISREG(identity.st_mode):
                entries.append(child_relative)
            else:
                _fail("runtime bundle contains an unsupported entry: " + child_relative)

    visit(root, "")
    return sorted(entries)


def _verified_files() -> dict[str, bytes]:
    runtime_dir_value = os.environ.get("AGENT_FLOW_RUNTIME_DIR", "")
    expected_digest = os.environ.get("AGENT_FLOW_RUNTIME_DIGEST", "")
    if not runtime_dir_value or len(expected_digest) != 64:
        _fail("runtime selection is missing")
    runtime_dir = Path(runtime_dir_value)
    _assert_owned_directory(runtime_dir, label="runtime bundle")
    manifest_fd = _open_owned(
        runtime_dir / MANIFEST_NAME,
        mode=0o444,
        label="runtime manifest",
    )
    try:
        manifest_content = _read_fd(manifest_fd)
    finally:
        os.close(manifest_fd)
    try:
        manifest = json.loads(manifest_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("runtime manifest is invalid: " + str(exc))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        _fail("runtime manifest file list is invalid")
    identity = {
        "protocol_version": manifest.get("protocol_version"),
        "entrypoint": manifest.get("entrypoint"),
        "cli_entrypoint": manifest.get("cli_entrypoint"),
        "policy_sequence": manifest.get("policy_sequence"),
        "files": files,
    }
    actual_digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if manifest.get("runtime_digest") != expected_digest or actual_digest != expected_digest:
        _fail("runtime manifest digest mismatch")

    verified: dict[str, bytes] = {}
    recorded_paths: list[str] = []
    for record in files:
        if not isinstance(record, dict):
            _fail("runtime manifest file record is invalid")
        relative = record.get("path")
        digest = record.get("sha256")
        mode = record.get("mode")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(digest, str)
            or len(digest) != 64
            or mode not in (0o444, 0o555)
        ):
            _fail("runtime manifest file record is invalid")
        descriptor = _open_owned(
            runtime_dir / relative,
            mode=mode,
            label="runtime bundle file",
        )
        try:
            content = _read_fd(descriptor)
        finally:
            os.close(descriptor)
        if hashlib.sha256(content).hexdigest() != digest:
            _fail("runtime bundle file digest mismatch: " + relative)
        recorded_paths.append(relative)
        if relative.startswith(RUNTIME_PREFIX):
            verified[relative[len(RUNTIME_PREFIX) :]] = content
    if _bundle_entries(runtime_dir) != sorted([MANIFEST_NAME, *recorded_paths]):
        _fail("runtime bundle contains unrecorded files")
    if "agent_flow/cli.py" not in verified:
        _fail("verified CLI package is missing")
    return verified


class _MemoryNode:
    def __init__(self, resources: dict[str, bytes], parts: tuple[str, ...] = ()) -> None:
        self._resources = resources
        self._parts = parts

    @property
    def name(self) -> str:
        return self._parts[-1] if self._parts else ""

    def _key(self) -> str:
        return "/".join(self._parts)

    def is_file(self) -> bool:
        return self._key() in self._resources

    def is_dir(self) -> bool:
        prefix = self._key()
        prefix = f"{prefix}/" if prefix else ""
        return any(key.startswith(prefix) for key in self._resources)

    def iterdir(self) -> Iterator[_MemoryNode]:
        prefix = self._key()
        prefix = f"{prefix}/" if prefix else ""
        names = {
            key[len(prefix) :].split("/", 1)[0]
            for key in self._resources
            if key.startswith(prefix) and key != prefix
        }
        return iter(_MemoryNode(self._resources, (*self._parts, name)) for name in sorted(names))

    def joinpath(self, *descendants: str) -> _MemoryNode:
        parts = self._parts
        for descendant in descendants:
            parts = (*parts, *Path(descendant).parts)
        return _MemoryNode(self._resources, parts)

    def __truediv__(self, descendant: str) -> _MemoryNode:
        return self.joinpath(descendant)

    def open(
        self,
        mode: str = "r",
        *args: object,
        encoding: str | None = None,
        errors: str | None = None,
        **kwargs: object,
    ) -> BinaryIO | io.TextIOWrapper:
        del args, kwargs
        if not self.is_file():
            raise FileNotFoundError(self._key())
        raw = io.BytesIO(self._resources[self._key()])
        if "b" in mode:
            return raw
        return io.TextIOWrapper(raw, encoding=encoding or "utf-8", errors=errors)

    def read_bytes(self) -> bytes:
        if not self.is_file():
            raise FileNotFoundError(self._key())
        return self._resources[self._key()]

    def read_text(self, encoding: str | None = None, errors: str | None = None) -> str:
        return self.read_bytes().decode(encoding or "utf-8", errors or "strict")


class _ResourceReader(importlib.abc.TraversableResources):
    def __init__(self, resources: dict[str, bytes], package_prefix: str) -> None:
        self._resources = {
            key[len(package_prefix) :]: value
            for key, value in resources.items()
            if key.startswith(package_prefix)
        }

    def files(self) -> _MemoryNode:
        return _MemoryNode(self._resources)


class _VerifiedLoader(importlib.abc.Loader):
    def __init__(
        self,
        fullname: str,
        source: bytes,
        origin: str,
        is_package: bool,
        resources: dict[str, bytes],
        package_prefix: str,
    ) -> None:
        self._fullname = fullname
        self._source = source
        self._origin = origin
        self._is_package = is_package
        self._resources = resources
        self._package_prefix = package_prefix

    def create_module(self, spec: object) -> ModuleType | None:
        del spec
        return None

    def exec_module(self, module: ModuleType) -> None:
        module.__file__ = self._origin
        if self._is_package:
            module.__path__ = []
        exec(compile(self._source, self._origin, "exec"), module.__dict__)

    def get_resource_reader(self, fullname: str) -> _ResourceReader | None:
        if not self._is_package or fullname != self._fullname:
            return None
        return _ResourceReader(self._resources, self._package_prefix)


class _VerifiedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files: dict[str, bytes], runtime_dir: str) -> None:
        self._files = files
        self._runtime_dir = runtime_dir
        self._modules: dict[str, tuple[str, bool, str]] = {}
        for relative in files:
            if not relative.endswith(".py"):
                continue
            parts = relative[:-3].split("/")
            is_package = parts[-1] == "__init__"
            if is_package:
                parts = parts[:-1]
            if not parts:
                continue
            fullname = ".".join(parts)
            package_parts = parts if is_package else parts[:-1]
            package_prefix = "/".join(package_parts)
            if package_prefix:
                package_prefix += "/"
            self._modules[fullname] = (relative, is_package, package_prefix)

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        record = self._modules.get(fullname)
        if record is None:
            return None
        relative, is_package, package_prefix = record
        origin = str(Path(self._runtime_dir) / RUNTIME_PREFIX / relative)
        loader = _VerifiedLoader(
            fullname,
            self._files[relative],
            origin,
            is_package,
            self._files,
            package_prefix,
        )
        return importlib.util.spec_from_loader(fullname, loader, origin=origin, is_package=is_package)


def main() -> int:
    files = _verified_files()
    runtime_dir = os.environ["AGENT_FLOW_RUNTIME_DIR"]
    sys.meta_path.insert(0, _VerifiedFinder(files, runtime_dir))
    cwd = os.getcwd()
    sys.path = [entry for entry in sys.path if entry not in ("", ".", cwd)]
    sys.argv[0] = "agent-flow"
    from agent_flow.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
