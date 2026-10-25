"""Prices the kit's reviewers without changing the kit (#409 v3.14 B): a transparent `claude` first on PATH.

The kit (the treatment) starts each reviewer as

    claude -p [--model M] [--effort E] --safe-mode --permission-mode plan --no-session-persistence <prompt>

in text mode, so the CLI reports no usage. `install(bin_dir, socket)` writes this file as `<bin_dir>/claude` and puts
it first on the PATH of the run sandbox (`arm_compare.run_box`). The wrapper:

- for every call that is not a kit reviewer (the controller, `--version`, the gate, the router, the simulator, the
  agent's own calls) execs the real CLI with the same arguments: nothing changes;
- for a kit reviewer (`is_kit_reviewer`: print mode with `--safe-mode`, `--permission-mode plan` and
  `--no-session-persistence`, no output format of its own; the kit sets no environment marker for its reviewers)
  runs the real CLI with `--output-format json` added, prints exactly what text mode prints (`result` and a newline)
  with the same exit code, and sends one usage row to the run's collector.

The kit runs reviewers under Landlock (`landlock_launch.py`): writes outside a scratch directory that is deleted after
the call are denied, so the wrapper cannot append to a file. It sends the row as one datagram to an abstract unix
socket instead (Landlock as the kit configures it handles only filesystem rights). `Collector`, in `drive` on the host
(the sandbox shares the host's network namespace, which abstract sockets live in), appends each row to the run's
ledger (`<run>/kit-reviewer-usage.jsonl`), outside the project and not writable from the sandbox. A row that never
arrives (a reviewer killed at its timeout, a lost datagram) shows as fewer rows than reviewer jobs: the cost is then
incomplete, never guessed.

When the CLI's JSON does not parse, the raw stdout is printed and the row is `unpriced`. When the CLI ends in an error
without a `result` (text mode prints its own error line), the wrapper prints the errors and marks `text_exact: false`.

Standard library only: the file runs inside the sandbox, under Landlock, with `python3 -I`.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "claude-usage-v1"
CONFIG = "claude-usage.json"
LOG = "kit-reviewer-usage.jsonl"
MAX_DATAGRAM = 60_000
REVIEWER_FLAGS = ("--safe-mode", "--no-session-persistence")
OWN_FORMAT = ("--output-format", "--input-format")


# ---------------------------------------------------------------- the wrapper (runs as `claude`)

def _option(args: list[str], name: str) -> str | None:
    for index, item in enumerate(args):
        if item == name and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def is_kit_reviewer(args: list[str]) -> bool:
    """The kit's reviewer launch (`multi_review._reviewer_cli_args`, claude): print mode, safe mode, plan permission
    mode, no session persistence, and no output format of its own."""
    if not ("-p" in args or "--print" in args):
        return False
    if not all(flag in args for flag in REVIEWER_FLAGS) or _option(args, "--permission-mode") != "plan":
        return False
    return not any(item == name or item.startswith(name + "=") for item in args for name in OWN_FORMAT)


def argv_digest(args: list[str]) -> str:
    return hashlib.sha256("\0".join(args).encode("utf-8", "surrogateescape")).hexdigest()


def real_cli(config: dict, own: Path, path: str | None) -> str | None:
    """The CLI this wrapper stands in for: the configured path, or the next `claude` on PATH that is not this file."""
    configured = config.get("real")
    if configured:
        return configured
    mine = own.resolve()
    for folder in (path or "").split(os.pathsep):
        if not folder:
            continue
        candidate = Path(folder) / "claude"
        try:
            if candidate.resolve() == mine or Path(folder).resolve() == mine.parent:
                continue
        except OSError:
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _load_config(own: Path) -> dict:
    try:
        return json.loads((own.parent / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def send(socket_name: str | None, row: dict) -> bool:
    if not socket_name:
        return False
    data = json.dumps(row, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_DATAGRAM:
        row = {key: row[key] for key in row if key != "usage"} | {"usage_truncated": True}
        data = json.dumps(row, separators=(",", ":")).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as out:
            out.sendto(data, b"\0" + socket_name.encode("utf-8"))
        return True
    except OSError:
        return False


def text_of(payload: dict) -> tuple[bytes, bool]:
    """What text mode prints for this result: `result` and a newline (exact), or, for an error without a result,
    the errors (text mode words its own error line, so not exact)."""
    result = payload.get("result")
    if isinstance(result, str):
        return (result + "\n").encode("utf-8"), True
    errors = payload.get("errors") or [payload.get("subtype") or "error"]
    return ("Error: " + "; ".join(str(item) for item in errors)).encode("utf-8"), False


def reviewer(real: str, args: list[str], config: dict) -> int:
    started = time.monotonic()
    done = subprocess.run([real, "--output-format", "json", *args], stdout=subprocess.PIPE)
    seconds = round(time.monotonic() - started, 3)
    raw = done.stdout or b""
    row = {"v": VERSION, "kind": "kit-reviewer", "argv_digest": argv_digest(args), "exit": done.returncode,
           "duration_s": seconds, "requested_model": _option(args, "--model"),
           "effort": _option(args, "--effort") or os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"),
           "prompt_bytes": len(args[-1].encode("utf-8", "surrogateescape")) if args else 0,
           "cwd": os.getcwd(), "pid": os.getpid(), "at": time.time()}
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except ValueError as failure:
        sys.stdout.buffer.write(raw)
        sys.stdout.flush()
        send(config.get("socket"), {**row, "status": "unpriced", "error": f"json: {failure}"[:200],
                                    "stdout_bytes": len(raw), "total_cost_usd": None, "usage": None, "model": None,
                                    "models": []})
        return done.returncode
    text, exact = text_of(payload)
    sys.stdout.buffer.write(text)
    sys.stdout.flush()
    cost = payload.get("total_cost_usd")
    models = sorted((payload.get("modelUsage") or {}).keys()) if isinstance(payload.get("modelUsage"), dict) else []
    priced = isinstance(cost, (int, float)) and not isinstance(cost, bool)
    send(config.get("socket"), {**row, "status": "priced" if priced else "unpriced", "total_cost_usd": cost,
                                "usage": payload.get("usage"), "model": models[0] if len(models) == 1 else None,
                                "models": models, "model_usage": payload.get("modelUsage"),
                                "subtype": payload.get("subtype"), "is_error": payload.get("is_error"),
                                "cli_duration_ms": payload.get("duration_ms"), "text_exact": exact,
                                "stdout_bytes": len(text)})
    return done.returncode


def wrapper_main(argv: list[str]) -> int:
    own = Path(argv[0])
    args = list(argv[1:])
    config = _load_config(own)
    real = real_cli(config, own, os.environ.get("PATH"))
    if real is None:
        sys.stderr.write("claude: the real CLI is not on PATH behind the usage wrapper\n")
        return 127
    if not is_kit_reviewer(args):
        os.execv(real, [real, *args])
    return reviewer(real, args, config)


# ---------------------------------------------------------------- the harness side

def install(bin_dir: Path, socket_name: str, real: str | None = None, interpreter: str | None = None) -> Path:
    """Writes the wrapper as `<bin_dir>/claude` (a regular, non-group-writable file, as the kit's provider check wants)
    and its config. Returns the wrapper's path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = interpreter or ("/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable)
    target = bin_dir / "claude"
    source = Path(__file__).read_text(encoding="utf-8")
    target.write_text(f"#!{python} -I\n{source}", encoding="utf-8")
    target.chmod(0o755)
    config = {"version": VERSION, "socket": socket_name, **({"real": real} if real else {})}
    (bin_dir / CONFIG).write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    (bin_dir / CONFIG).chmod(0o644)
    return target


def with_wrapper(env: dict[str, str], bin_dir: Path | None) -> dict[str, str]:
    """`env` with the wrapper's directory first on PATH (unchanged when there is no wrapper)."""
    if bin_dir is None or not (bin_dir / "claude").is_file():
        return env
    rest = [item for item in (env.get("PATH") or "").split(os.pathsep) if item and item != str(bin_dir)]
    return {**env, "PATH": os.pathsep.join([str(bin_dir), *rest])}


class Collector:
    """Receives the wrapper's rows on an abstract unix datagram socket and appends each to `log`."""

    def __init__(self, socket_name: str, log: Path):
        self.socket_name, self.log = socket_name, log
        self.received = 0
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Collector":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(b"\0" + self.socket_name.encode("utf-8"))
        self._sock.settimeout(0.2)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.touch()
        self._thread = threading.Thread(target=self._loop, name="claude-usage-collector", daemon=True)
        self._thread.start()
        return self

    def _write(self, data: bytes) -> None:
        try:
            row = json.loads(data.decode("utf-8"))
        except ValueError:
            row = {"kind": "malformed", "bytes": len(data)}
        if not isinstance(row, dict):
            row = {"kind": "malformed", "bytes": len(data)}
        with self.log.open("a", encoding="utf-8") as out:
            out.write(json.dumps({**row, "received_at": time.time()}) + "\n")
        self.received += 1

    def _loop(self) -> None:
        assert self._sock is not None
        while True:
            try:
                data = self._sock.recv(MAX_DATAGRAM + 4096)
            except socket.timeout:
                if self._stop.is_set():
                    return
                continue
            except OSError:
                return
            self._write(data)

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.close()


def read_rows(log: Path) -> list[dict]:
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summary(rows: list[dict]) -> dict:
    """Kit reviewer rows: how many, how many unpriced, their summed cost, and the models and efforts seen."""
    reviewers = [row for row in rows if row.get("kind") == "kit-reviewer"]
    unpriced = [row for row in reviewers if row.get("status") != "priced"]
    cost = sum(float(row["total_cost_usd"]) for row in reviewers if row.get("status") == "priced")
    return {"rows": len(reviewers), "unpriced": len(unpriced), "malformed": sum(row.get("kind") == "malformed"
                                                                              for row in rows),
            "cost_usd": round(cost, 6),
            "models": sorted({model for row in reviewers for model in row.get("models") or []}),
            "requested_models": sorted({str(row.get("requested_model")) for row in reviewers}),
            "efforts": sorted({str(row.get("effort")) for row in reviewers}),
            "text_inexact": sum(row.get("text_exact") is False for row in reviewers)}


if __name__ == "__main__":
    sys.exit(wrapper_main(sys.argv))
