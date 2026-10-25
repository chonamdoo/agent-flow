"""Run sandbox (#409 v3.14 A): one PID and mount namespace per controller, harness runner call and graded submission.

    python3 -I evals/sandbox.py exec --spec <spec.json> -- <argv>...     # what eval.json / gradlew wrappers call

`run(spec, argv, ...)` starts

    unshare --pid --fork --mount-proc --mount --propagation private --kill-child -- python3 -I sandbox.py inner ...

so the command and everything it starts (hooks, the kit's reviewers, arm V's gate, the simulated user, servers and test
runners) live in a namespace whose PID 1 is this file's `inner`. Inside:

- every mount of the host is read-only (`/`, `/opt/...`), so nothing can install a shim in `/usr/local/bin`;
- `/tmp`, `/var/tmp` and `/dev/shm` are fresh, private tmpfs;
- `/home`, `/mnt` and the other `hide` paths are empty read-only tmpfs: the private evaluator repository, the harness
  checkout, the scratch area with other runs, the operator's sessions are not there;
- `/root` is the run's own HOME (`<run>/sandbox/home`), with the tools on PATH (`HOME_RO`) and the proxy CA read-only,
  and the dependency caches (`HOME_OVERLAY`) as copy-on-write overlays whose writes stay in the run. A Gradle daemon
  registry of the host is masked, so a build cannot hand itself to a daemon outside;
- only the paths in `expose` come back, at the same path, read-only or writable (the run directory read-only, its
  project and the logs the gate and the simulator append to writable).

The network namespace is shared: the proxy listens on the host's loopback. A server another run starts is reachable by
port, but not visible (`ps`, `pkill`) or killable.

When the command exits or the deadline passes, PID 1 kills every process left in the namespace and reports them
(`killed_at_exit`); when PID 1 exits the kernel ends the namespace. `unshare` gets `PR_SET_PDEATHSIG` (SIGKILL, set by `setpriv`), so a
caller killed from outside (an evaluator stopping a started server by killing its `sandbox.py exec` wrapper) takes
`unshare`, and with `--kill-child` the whole namespace, with it. `exec` also records each `unshare` pid in the spec's
`pids` directory, so the scorer can check after grading that nothing it started survives (`reap`). `run` then looks for any host process still in that
PID namespace: one found is `survivors`, a harness error. `tripwire()` hashes listings of the system directories before
and after a run; a change marks the run contaminated.

Standard library only: the submission wrapper runs from a copy outside this repository.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

VERSION = "sandbox-v1"
UNSHARE = ("unshare", "--pid", "--fork", "--mount-proc", "--mount", "--propagation", "private", "--kill-child")
STAGE = "/mnt"
HOME_TARGET = "/root"
# The host HOME's tool trees on PATH (`~/.local/bin`, cargo, bun) and the agent proxy's CA bundle, read-only.
HOME_RO = (".local", ".cargo", ".rustup", ".bun", ".ccr")
HOME_LINKS = (".nix-profile",)
# Dependency caches: the host's as the lower layer, the run's writes in `<run>/sandbox/overlay/<name>`.
HOME_OVERLAY = (".gradle", ".npm", ".m2")
HOME_MASK = (".gradle/daemon",)
SCRATCH = ("/tmp", "/var/tmp", "/dev/shm")
HIDE = ("/home", "/opt/env-runner")
TRIPWIRE = ("/usr/local/bin", "/usr/bin", "/etc", "/opt")
TRIPWIRE_DEPTH = {"/opt": 2}
GRACE_SECONDS = 60
SCRATCH_LAYER = "tmpfs"  # `overlay_root` for caches whose writes are thrown away (a graded submission)


class SandboxError(RuntimeError):
    pass


def available() -> bool:
    """unshare exists, the harness is root, and a PID+mount namespace with a tmpfs can be made here."""
    if shutil.which("unshare") is None or os.geteuid() != 0:
        return False
    try:
        done = subprocess.run([*UNSHARE, "--", "sh", "-c", "mount -t tmpfs tmpfs /mnt && test -d /proc/1"],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# ---------------------------------------------------------------- specs

def spec(*, expose: list[dict], cwd: str, env: dict[str, str], timeout: float, home: str | None,
         overlay_root: str | None = None, hide: tuple[str, ...] = HIDE, label: str = "") -> dict:
    """A sandbox description. `expose`: [{"path": <host path>, "mode": "ro" | "rw"}], shown at the same path.

    `home` is the host directory mounted at `/root` (None: an empty tmpfs). `overlay_root` holds the caches' upper
    layers (`<overlay_root>/<name>/upper`); `SCRATCH_LAYER` keeps them on a tmpfs; None leaves the caches out.
    """
    for item in expose:
        if item["mode"] not in ("ro", "rw"):
            raise ValueError(f"expose mode must be ro or rw: {item}")
        if not os.path.isabs(item["path"]) or item["path"].startswith(STAGE + "/") or item["path"] == STAGE:
            raise ValueError(f"cannot expose {item['path']}")
    overlays = []
    if overlay_root is not None:
        for name in HOME_OVERLAY:
            base = None if overlay_root == SCRATCH_LAYER else os.path.join(overlay_root, name.strip("."))
            overlays.append({"name": name, "base": base})
    return {"version": VERSION, "label": label, "expose": expose, "cwd": cwd, "env": env, "timeout": timeout,
            "home": home, "home_target": HOME_TARGET, "home_ro": list(HOME_RO), "home_links": list(HOME_LINKS),
            "home_overlay": overlays, "home_mask": list(HOME_MASK), "scratch": list(SCRATCH), "hide": list(hide)}


def run_layout(run_dir: Path) -> dict[str, Path]:
    """Where a run's sandbox keeps its state on the host, all inside the run directory."""
    root = run_dir / "sandbox"
    return {"root": root, "home": root / "home", "overlay": root / "overlay",
            "worktrees": root / "home" / ".agent-flow" / "worktrees", "projects": root / "home" / ".claude" / "projects"}


def prepare_layout(run_dir: Path) -> dict[str, Path]:
    layout = run_layout(run_dir)
    for key in ("home", "worktrees", "projects"):
        layout[key].mkdir(parents=True, exist_ok=True)
    for name in HOME_OVERLAY:
        for part in ("upper", "work"):
            (layout["overlay"] / name.strip(".") / part).mkdir(parents=True, exist_ok=True)
    gitconfig = layout["home"] / ".gitconfig"
    if not gitconfig.exists():
        # The same git identity for every arm; no signing program (the host's lives in /tmp, which is private here).
        gitconfig.write_text("[user]\n\tname = eval\n\temail = eval@example.com\n[commit]\n\tgpgsign = false\n"
                             "[tag]\n\tgpgsign = false\n[safe]\n\tdirectory = *\n", encoding="utf-8")
    return layout


def to_host(path: str | Path, home: Path) -> Path:
    """A path as the sandbox sees it, on the host: `/root/...` is the run's HOME."""
    text = str(path)
    if text == HOME_TARGET or text.startswith(HOME_TARGET + "/"):
        return home / text[len(HOME_TARGET):].lstrip("/")
    return Path(text)


def to_inside(path: str | Path, home: Path) -> Path:
    text, prefix = str(path), str(home)
    if text == prefix or text.startswith(prefix + "/"):
        return Path(HOME_TARGET) / text[len(prefix):].lstrip("/")
    return Path(text)


# ---------------------------------------------------------------- outside

def _pid_ns_members(inode: str) -> list[dict]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{entry}/ns/pid") != inode:
                continue
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        found.append({"pid": int(entry), "cmdline": cmdline.strip()[:300]})
    return found


PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """In the forked `unshare`, before exec: SIGKILL when the process that started it exits."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _children(pid: int) -> list[int]:
    found = []
    for task in Path(f"/proc/{pid}/task").glob("*"):
        try:
            found += [int(item) for item in (task / "children").read_text().split()]
        except (OSError, ValueError):
            continue
    return found


def namespace_members(unshare_pid: int) -> list[dict]:
    """The processes in the PID namespace whose PID 1 `unshare_pid` forked (empty when it is gone)."""
    members = []
    for child in _children(unshare_pid):
        try:
            inode = os.readlink(f"/proc/{child}/ns/pid")
        except OSError:
            continue
        members += _pid_ns_members(inode)
    return members


def reap(pid_dir: Path) -> list[dict]:
    """Every `unshare` recorded in `pid_dir` (by `exec`) that is still alive: its namespace's processes, then SIGKILL
    (with `--kill-child` its PID 1 and so the namespace die with it). Returns what survived."""
    survivors = []
    for record in sorted(pid_dir.glob("*.json")) if pid_dir.is_dir() else []:
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, ValueError, KeyError):
            continue
        if "sandbox.py" not in cmdline or " inner " not in f" {cmdline} ":
            continue  # the pid was reused
        members = namespace_members(pid)
        survivors.append({"unshare_pid": pid, "label": data.get("label"), "argv": data.get("argv"),
                          "members": members})
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        for member in members:
            try:
                os.kill(member["pid"], signal.SIGKILL)
            except OSError:
                pass
    return survivors


def run(sandbox: dict, argv: list[str], *, stdout=None, stderr=None, stdin=None, capture: bool = False,
        on_start=None) -> dict:
    """Runs `argv` in a new sandbox and returns {returncode, timed_out, killed_at_exit, survivors, ..., stdout?}.

    `stdout`/`stderr` are files (or None to inherit); `capture` collects both as text instead. Raises SandboxError when
    the namespace could not be set up (the command never ran).
    """
    read_fd, write_fd = os.pipe()
    payload = json.dumps({**sandbox, "argv": list(argv), "report_fd": write_fd})
    # PDEATHSIG through `setpriv` (kept across its exec of unshare): no preexec_fn in a threaded caller
    setpriv = shutil.which("setpriv")
    command = [*([setpriv, "--pdeathsig", "KILL"] if setpriv else []), *UNSHARE, "--", sys.executable, "-I",
               str(Path(__file__).resolve()), "inner", payload]
    started = time.monotonic()
    pipes = subprocess.PIPE if capture else None
    proc = subprocess.Popen(command, stdout=pipes or stdout, stderr=pipes or stderr,
                            stdin=stdin if stdin is not None else subprocess.DEVNULL, pass_fds=(write_fd,),
                            preexec_fn=None if setpriv else _die_with_parent)
    os.close(write_fd)
    if on_start is not None:
        on_start(proc)
    limit = float(sandbox["timeout"]) + GRACE_SECONDS
    out = err = b""
    try:
        if capture:
            out, err = proc.communicate(timeout=limit)
        else:
            proc.wait(timeout=limit)
        outer_killed = False
    except subprocess.TimeoutExpired:
        proc.kill()  # --kill-child takes PID 1, and so the namespace, with it
        out, err = proc.communicate() if capture else (b"", b"")
        outer_killed = True
    with os.fdopen(read_fd, "rb") as report_pipe:
        raw = report_pipe.read()
    try:
        report = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        report = {}
    if not report.get("setup_ok"):
        raise SandboxError(f"sandbox setup failed: {report.get('error') or (err or b'').decode('utf-8', 'replace')[-400:]}")
    survivors = []
    if report.get("pid_ns"):
        deadline = time.monotonic() + 10
        while (survivors := _pid_ns_members(report["pid_ns"])) and time.monotonic() < deadline:
            time.sleep(0.2)
    result = {"version": VERSION, "label": sandbox.get("label"), "returncode": report.get("returncode"),
              "timed_out": bool(report.get("timed_out") or outer_killed), "outer_killed": outer_killed,
              "seconds": round(time.monotonic() - started, 1), "killed_at_exit": report.get("killed_at_exit") or [],
              "survivors": survivors, "checks": report.get("checks") or {}, "error": report.get("error")}
    if capture:
        result["stdout"] = (out or b"").decode("utf-8", "replace")
        result["stderr"] = (err or b"").decode("utf-8", "replace")
    return result


def tripwire() -> dict[str, str]:
    """sha256 of the listing (path, type, mode, size, mtime) of each system directory."""
    digests = {}
    for top in TRIPWIRE:
        depth = TRIPWIRE_DEPTH.get(top)
        lines = []
        for root, dirs, files in os.walk(top):
            level = Path(root).relative_to(top).parts
            if depth is not None and len(level) >= depth:
                dirs[:] = []
            dirs.sort()
            for name in sorted(dirs + files):
                path = os.path.join(root, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                lines.append(f"{path}\0{stat.S_IFMT(info.st_mode)}\0{info.st_mode & 0o7777}\0{info.st_size}\0"
                             f"{info.st_mtime_ns}\n")
        digests[top] = hashlib.sha256("".join(lines).encode("utf-8", "surrogateescape")).hexdigest()
    return digests


def tripwire_changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


# ---------------------------------------------------------------- inside (PID 1)

def _mount(*args: str) -> None:
    done = subprocess.run(["mount", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise SandboxError(f"mount {' '.join(args)}: {done.stderr.strip()}")


def _mount_points() -> list[str]:
    points = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        raw = line.split(" ")[4]
        points.append(raw.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace"))
    return points


def _under(path: str, roots) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _scratch_layer(staged: list[str]) -> str:
    """An overlay's upper layer on its own tmpfs: writes vanish with the namespace."""
    target = f"{STAGE}/s{len(staged)}"
    os.mkdir(target)
    _mount("-t", "tmpfs", "-o", "mode=755", "tmpfs", target)
    for part in ("upper", "work"):
        os.mkdir(os.path.join(target, part))
    staged.append(target)
    return target


def _setup(config: dict) -> dict:
    _mount("--make-rprivate", "/")
    _mount("-t", "tmpfs", "-o", "mode=755", "tmpfs", STAGE)
    staged: list[str] = []

    def stage(source: str) -> str | None:
        if not os.path.lexists(source):
            return None
        target = f"{STAGE}/s{len(staged)}"
        if os.path.isdir(source):
            os.mkdir(target)
        else:
            Path(target).touch()
        _mount("--bind", source, target)
        staged.append(target)
        return target

    host_home = os.environ.get("HOME") or "/root"
    home = stage(config["home"]) if config.get("home") else None
    ro = {name: stage(os.path.join(host_home, name)) for name in config["home_ro"]}
    links = {name: os.readlink(os.path.join(host_home, name)) for name in config["home_links"]
             if os.path.islink(os.path.join(host_home, name))}
    overlays = [{**item, "lower": stage(os.path.join(host_home, item["name"])),
                 "staged": stage(item["base"]) if item["base"] else _scratch_layer(staged)}
                for item in config["home_overlay"]]
    exposed = [{**item, "staged": stage(item["path"])} for item in sorted(config["expose"], key=lambda e: e["path"])]
    for item in exposed:
        if item["staged"] is None:
            raise SandboxError(f"exposed path does not exist: {item['path']}")
    # Read-only host: every mount but the kernel's own trees and the stage.
    for point in _mount_points():
        if _under(point, ("/proc", "/sys", "/dev", STAGE)):
            continue
        try:
            _mount("-o", "remount,bind,ro", point)
        except SandboxError:
            if point == "/":
                raise
    hidden = [path for path in config["hide"] if os.path.isdir(path)]
    for path in config["scratch"]:
        if os.path.isdir(path):
            _mount("-t", "tmpfs", "-o", "mode=1777", "tmpfs", path)
    for path in hidden:
        _mount("-t", "tmpfs", "-o", "mode=755", "tmpfs", path)
    target = config["home_target"]
    if home is not None:
        _mount("--bind", home, target)
    else:
        _mount("-t", "tmpfs", "-o", "mode=700", "tmpfs", target)
    for name, source in ro.items():
        if source is None:
            continue
        place = os.path.join(target, name)
        if os.path.isdir(source):
            os.makedirs(place, exist_ok=True)
        elif not os.path.exists(place):
            Path(place).touch()
        _mount("--bind", source, place)
        _mount("-o", "remount,bind,ro", place)
    for name, link in links.items():
        place = os.path.join(target, name)
        if not os.path.lexists(place):
            os.symlink(link, place)
    for item in overlays:
        place = os.path.join(target, item["name"])
        os.makedirs(place, exist_ok=True)
        if item["lower"] is not None and item["staged"] is not None:
            _mount("-t", "overlay", "overlay", "-o",
                   f"lowerdir={item['lower']},upperdir={item['staged']}/upper,workdir={item['staged']}/work", place)
    for name in config["home_mask"]:
        place = os.path.join(target, name)
        if os.path.isdir(place):
            _mount("-t", "tmpfs", "-o", "mode=755", "tmpfs", place)
    for item in exposed:
        path = item["path"]
        if os.path.isdir(item["staged"]):
            os.makedirs(path, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                Path(path).touch()
        _mount("--bind", item["staged"], path)
        if item["mode"] == "ro":
            _mount("-o", "remount,bind,ro", path)
    for point in reversed(staged):
        subprocess.run(["umount", "-l", point], capture_output=True)
        if os.path.isdir(point) and not os.path.islink(point):
            os.rmdir(point)
        else:
            os.unlink(point)
    _mount("-o", "remount,ro", STAGE)
    for path in hidden:
        _mount("-o", "remount,ro", path)
    return _checks(config, hidden)


def _checks(config: dict, hidden: list[str]) -> dict:
    """What the namespace looks like from inside, recorded with the run: the host is read-only, the hidden paths hold
    only the mount points of exposed paths."""
    probe = "/usr/.sandbox-write-probe"
    try:
        with open(probe, "w", encoding="utf-8"):
            pass
        os.unlink(probe)
        read_only = False
    except OSError:
        read_only = True
    allowed = {path for item in config["expose"] for path in _ancestors(item["path"])}
    leaks = []
    for root in [*hidden, STAGE]:
        for entry in os.listdir(root):
            full = os.path.join(root, entry)
            if full not in allowed:
                leaks.append(full)
    if not read_only:
        raise SandboxError("the host filesystem is writable inside the sandbox")
    if leaks:
        raise SandboxError(f"hidden paths are not empty: {leaks[:5]}")
    return {"host_read_only": read_only, "hidden": hidden, "hidden_leaks": leaks}


def _ancestors(path: str) -> list[str]:
    parts = Path(path).parts
    return [str(Path(*parts[:index])) for index in range(2, len(parts) + 1)]


def _processes() -> list[dict]:
    me = os.getpid()
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit() and int(entry) != me:
            try:
                cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            found.append({"pid": int(entry), "cmdline": cmdline.strip()[:300]})
    return found


def _reap() -> dict[int, int]:
    """Collects every exited process (orphans are re-parented to PID 1): {pid: exit code}."""
    reaped = {}
    while True:
        try:
            pid, raw = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped[pid] = os.waitstatus_to_exitcode(raw)


def inner(config: dict) -> int:
    report_fd = config.pop("report_fd")
    report: dict = {"setup_ok": False}
    code = 125
    try:
        report["checks"] = _setup(config)
        report["setup_ok"] = True
        report["pid_ns"] = os.readlink("/proc/self/ns/pid")
        os.chdir(config["cwd"])
        child = subprocess.Popen(config["argv"], env=config["env"], cwd=config["cwd"], start_new_session=True)
        deadline = time.monotonic() + float(config["timeout"])
        status, signalled = None, None
        while status is None:
            reaped = _reap()
            if child.pid in reaped:
                status = reaped[child.pid]
                break
            now = time.monotonic()
            if now >= deadline and signalled is None:
                report["timed_out"] = True
                os.killpg(child.pid, signal.SIGTERM)
                signalled = now
            elif signalled is not None and now - signalled > 15:
                os.killpg(child.pid, signal.SIGKILL)
                signalled = float("inf")
            time.sleep(0.2)
        report["returncode"] = status
        code = status if status >= 0 else 128 - status
    except Exception as failure:  # noqa: BLE001 - every failure is reported to the outside
        report["error"] = f"{type(failure).__name__}: {failure}"[:600]
    finally:
        left = _processes()
        report["killed_at_exit"] = left
        if left:
            try:
                os.kill(-1, signal.SIGKILL)
            except OSError:
                pass
            deadline = time.monotonic() + 10
            while _processes() and time.monotonic() < deadline:
                _reap()
                time.sleep(0.05)
        _reap()
        with os.fdopen(report_fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(report))
    return code


# ---------------------------------------------------------------- submission wrapper (scoring)

def submission_spec(cwd: Path, env: dict[str, str], timeout: float, writable: list[Path]) -> dict:
    """A graded build's own sandbox: its working copy and the evaluator's data directories writable, an empty HOME
    plus the dependency caches read-only, nothing of the evaluator, the private repository or other runs."""
    expose = [{"path": str(path), "mode": "rw"} for path in sorted({cwd.resolve(), *(p.resolve() for p in writable)})
              if path.exists()]
    return spec(expose=expose, cwd=str(cwd.resolve()), env={**env}, timeout=timeout, home=None,
                overlay_root=SCRATCH_LAYER, label="submission")


def exec_main(args: argparse.Namespace) -> int:
    """`exec`: run a submission command in its own sandbox, as `eval.json`/`gradlew` wrappers do. The working
    directory, `$HOME` and `$DATA_DIR` (when they exist) are writable; the rest is as in `submission_spec`."""
    config = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else {}
    cwd = Path.cwd()
    env = dict(os.environ)
    writable = [Path(env[key]) for key in ("HOME", "DATA_DIR", "TMPDIR") if env.get(key) and Path(env[key]).is_dir()]
    writable += [Path(path) for path in config.get("writable", [])]
    if env.get("HOME") and Path(env["HOME"]).is_dir():
        env["HOME"] = str(Path(env["HOME"]).resolve())
    box = submission_spec(cwd, env, float(config.get("timeout", 3600)), writable)
    command = list(args.argv)
    if command and command[0] == "--":
        command = command[1:]
    pid_dir = Path(config["pids"]) if config.get("pids") else None
    record: list[Path] = []

    def started(proc) -> None:
        if pid_dir is None:
            return
        try:
            pid_dir.mkdir(parents=True, exist_ok=True)
            path = pid_dir / f"{proc.pid}.json"
            path.write_text(json.dumps({"pid": proc.pid, "label": box.get("label"), "argv": command[:6]}),
                            encoding="utf-8")
            record.append(path)
        except OSError:
            pass

    # a SIGTERM/SIGHUP from the evaluator ends this process normally, so PDEATHSIG takes the namespace down too
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, lambda *_: sys.exit(128 + 15))
    result = run(box, command, on_start=started)
    for path in record:  # the namespace ended with the command: nothing left to reap
        path.unlink(missing_ok=True)
    log = config.get("log")
    if log:
        try:
            with open(log, "a", encoding="utf-8") as out:
                out.write(json.dumps({key: result[key] for key in ("returncode", "timed_out", "killed_at_exit",
                                                                    "survivors", "seconds")} | {"argv": command}) + "\n")
        except OSError:
            pass
    return result["returncode"] if isinstance(result["returncode"], int) else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "inner":
        return inner(json.loads(argv[1]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("exec")
    ex.add_argument("--spec")
    ex.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("check")
    args = parser.parse_args(argv)
    if args.command == "check":
        print(json.dumps({"available": available()}))
        return 0
    return exec_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
