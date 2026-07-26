#!/usr/bin/env python3
"""eval 러너.

`(case × config × trial)`마다 격리된 임시 프로젝트를 만들고, host CLI에 과제를
주고, 기계 oracle로 채점한다. agent가 남긴 어떤 자기신고도 점수에 들어가지 않는다.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import signal
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"
RESULTS = ROOT / "results"
MAX_HOST_OUTPUT_BYTES = 64 * 1024

sys.path.insert(0, str(ROOT))
from configs import CONFIGS  # noqa: E402


HOSTS = {
    # host마다 "비대화형으로 과제 하나를 끝까지 수행" 형태가 다르다.
    "claude": lambda task: ("claude", "-p", task, "--permission-mode", "acceptEdits", "--output-format", "text"),
    "codex": lambda task: ("codex", "exec", "--full-auto", task),
}


def _short_diagnostic(output: str | None, fallback: str) -> str:
    compact = " ".join((output or "").split())
    return compact[-240:] if compact else fallback


def _host_succeeded(return_code: int | None, timed_out: bool) -> bool:
    return return_code == 0 and not timed_out


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill()


class _BoundedCapture:
    __slots__ = ("data", "total")

    def __init__(self) -> None:
        self.data = bytearray()
        self.total = 0

    def consume(self, stream) -> None:
        try:
            while chunk := stream.read(8192):
                self.total += len(chunk)
                self.data.extend(chunk)
                overflow = len(self.data) - MAX_HOST_OUTPUT_BYTES
                if overflow > 0:
                    del self.data[:overflow]
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def output(self) -> tuple[str, bool]:
        return (
            self.data.decode("utf-8", errors="replace"),
            self.total > len(self.data),
        )


def _invoke_host(
    command: tuple[str, ...],
    project: Path,
    timeout_s: float,
) -> tuple[int | None, bool, str, str, bool]:
    process = subprocess.Popen(
        command,
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _BoundedCapture()
    stderr_capture = _BoundedCapture()
    readers = [
        threading.Thread(
            target=stdout_capture.consume,
            args=(process.stdout,),
            daemon=True,
        ),
        threading.Thread(
            target=stderr_capture.consume,
            args=(process.stderr,),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    return_code: int | None
    try:
        return_code = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        return_code = None
    finally:
        _kill_process_group(process)
        if process.poll() is None:
            process.wait()
        for reader in readers:
            reader.join(timeout=1)
        for reader, stream in zip(
            readers,
            (process.stdout, process.stderr),
        ):
            if reader.is_alive():
                stream.close()
                reader.join(timeout=1)

    stdout, stdout_truncated = stdout_capture.output()
    stderr, stderr_truncated = stderr_capture.output()
    return return_code, timed_out, stdout, stderr, stdout_truncated or stderr_truncated


def _python_sources(project: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in sorted(project.rglob("*.py")):
        relative = path.relative_to(project)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        sources[str(relative)] = path.read_text(encoding="utf-8")
    return sources


def _project_files(project: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(project.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project)
        if (
            path.suffix == ".pyc"
            or "__pycache__" in relative.parts
            or ".pytest_cache" in relative.parts
        ):
            continue
        files[str(relative)] = path.read_bytes()
    return files


def _project_hash(project: Path) -> str:
    digest = hashlib.sha256()
    for path, content in _project_files(project).items():
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _path_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_host_executable(host: str) -> str:
    requested = HOSTS[host]("")[0]
    resolved = shutil.which(requested)
    return str(Path(resolved).resolve()) if resolved else requested


def _host_executable_hash(executable: str) -> str | None:
    path = Path(executable)
    return _path_hash(path.resolve()) if path.is_file() else None


def _is_guidance_path(path: str) -> bool:
    parts = Path(path).parts
    return (
        path == "AGENTS.md"
        or parts == (".agent-flow", "skills", "index.json")
        or (
            len(parts) == 4
            and parts[:2] == (".agent-flow", "skills")
            and parts[3] == "SKILL.md"
        )
    )


def _validate_config_changes(
    seed_files: dict[str, bytes],
    prepared_files: dict[str, bytes],
    config: str,
    case: str,
) -> None:
    changed_seed = [
        path
        for path, content in seed_files.items()
        if prepared_files.get(path) != content
    ]
    unexpected_additions = [
        path
        for path in prepared_files.keys() - seed_files.keys()
        if not _is_guidance_path(path)
    ]
    if changed_seed or unexpected_additions:
        details = ", ".join(sorted([*changed_seed, *unexpected_additions]))
        raise RuntimeError(
            f"config {config!r} changed seed or non-guidance files "
            f"for {case!r}: {details}"
        )


def _source_hash(source: str | None) -> str:
    if source is None:
        return "<absent>"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _source_provenance(
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[list[str], str]:
    changed = sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )
    diffs: list[str] = []
    for path in changed:
        old = before.get(path)
        new = after.get(path)
        status = "added" if old is None else "deleted" if new is None else "modified"
        header = (
            f"diff --eval {path}\n"
            f"status: {status}\n"
            f"old-sha256: {_source_hash(old)}\n"
            f"new-sha256: {_source_hash(new)}\n"
        )
        patch = "".join(
            difflib.unified_diff(
                [] if old is None else old.splitlines(keepends=True),
                [] if new is None else new.splitlines(keepends=True),
                fromfile="/dev/null" if old is None else f"seed/{path}",
                tofile="/dev/null" if new is None else f"candidate/{path}",
            )
        )
        if old is not None and old and not old.endswith(("\n", "\r")):
            patch += "\n\\ No newline at end of seed file\n"
        if new is not None and new and not new.endswith(("\n", "\r")):
            patch += "\n\\ No newline at end of candidate file\n"
        diffs.append(header + patch)
    return changed, "\n".join(diffs)


def load_oracle(case: Path):
    spec = importlib.util.spec_from_file_location(f"eval_{case.name}", case / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score


def run_once(
    case: Path,
    config: str,
    host: str,
    timeout_s: int,
    *,
    host_executable: str | None = None,
    host_executable_sha256: str | None = None,
) -> dict:
    task = (case / "task.md").read_text(encoding="utf-8").strip()
    requested_command = HOSTS[host](task)
    executable = host_executable or requested_command[0]
    command = (executable, *requested_command[1:])
    executable_sha256_before = _host_executable_hash(executable)
    expected_executable_sha256 = (
        host_executable_sha256
        if host_executable is not None
        else executable_sha256_before
    )
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw) / "project"
        shutil.copytree(case / "seed", project)
        seed_sha256 = _project_hash(project)
        seed_files = _project_files(project)
        CONFIGS[config](project, case.name)
        _validate_config_changes(
            seed_files,
            _project_files(project),
            config,
            case.name,
        )
        prepared_project_sha256 = _project_hash(project)
        source_before = _python_sources(project)
        started = time.time()
        return_code: int | None = None
        timed_out = False
        output_truncated = False
        diagnostic = "ok"
        oracle_input_hash: str | None = None
        oracle_output_hash: str | None = None
        identity_ok = (
            expected_executable_sha256 is None
            or executable_sha256_before == expected_executable_sha256
        )
        if not identity_ok:
            diagnostic = "host executable identity changed before trial"
        else:
            try:
                (
                    return_code,
                    timed_out,
                    stdout,
                    stderr,
                    output_truncated,
                ) = _invoke_host(command, project, timeout_s)
                if timed_out:
                    diagnostic = f"host timed out after {timeout_s}s"
                elif return_code != 0:
                    diagnostic = _short_diagnostic(
                        stderr or stdout,
                        f"host exited with code {return_code}",
                    )
            except FileNotFoundError as error:
                diagnostic = (
                    f"host executable not found: {error.filename or command[0]}"
                )
            except OSError as error:
                diagnostic = _short_diagnostic(
                    str(error),
                    "host process could not start",
                )
        executable_sha256_after = _host_executable_hash(executable)
        if (
            identity_ok
            and expected_executable_sha256 is not None
            and executable_sha256_after != expected_executable_sha256
        ):
            identity_ok = False
            diagnostic = "host executable identity changed during trial"
        elapsed = round(time.time() - started, 1)
        host_ok = _host_succeeded(return_code, timed_out) and identity_ok
        source_after = _python_sources(project)
        changed_files, source_diff = _source_provenance(source_before, source_after)
        if host_ok:
            scored_project = Path(raw) / "scored-project"
            shutil.copytree(project, scored_project)
            oracle_input_hash = _project_hash(scored_project)
            scores = load_oracle(case)(scored_project)
            oracle_output_hash = _project_hash(scored_project)
            if oracle_output_hash != oracle_input_hash:
                raise RuntimeError(f"oracle modified candidate project for {case.name!r}")
        else:
            scores = {"behavior": None, "norm": None}
    return {
        "case": case.name,
        "config": config,
        "host": host,
        "host_ok": host_ok,
        "return_code": return_code,
        "timed_out": timed_out,
        "diagnostic": diagnostic,
        "output_truncated": output_truncated,
        "seconds": elapsed,
        "host_executable": executable,
        "host_executable_sha256": executable_sha256_before,
        "host_executable_sha256_after": executable_sha256_after,
        **scores,
        "changed_files": changed_files,
        "source_diff": source_diff,
        "oracle_input_hash": oracle_input_hash,
        "oracle_output_hash": oracle_output_hash,
        "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "seed_sha256": seed_sha256,
        "prepared_project_sha256": prepared_project_sha256,
        "oracle_sha256": _path_hash(case / "check.py"),
        "runner_sha256": _path_hash(Path(__file__)),
        "configs_sha256": _path_hash(ROOT / "configs.py"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="claude", choices=sorted(HOSTS))
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--config", action="append", default=None)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    cases = [CASES / name for name in (args.case or sorted(p.name for p in CASES.iterdir() if p.is_dir()))]
    configs = args.config or list(CONFIGS)
    host_executable = _resolve_host_executable(args.host)
    host_executable_sha256 = _host_executable_hash(host_executable)
    rows: list[dict] = []
    for case in cases:
        for config in configs:
            for trial in range(args.trials):
                row = run_once(
                    case,
                    config,
                    args.host,
                    args.timeout,
                    host_executable=host_executable,
                    host_executable_sha256=host_executable_sha256,
                )
                row["trial"] = trial
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    report = {
        "host": args.host,
        "host_executable": host_executable,
        "host_executable_sha256": host_executable_sha256,
        "trials": args.trials,
        "rows": rows,
        "summary": summarize(rows),
    }
    (RESULTS / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report["summary"]))
    return 0 if any(row["host_ok"] for row in rows) else 1


def _resolution(rows: list[dict], case: str) -> tuple[bool, str]:
    if case != "layer_boundary":
        return True, "not-required"
    case_rows = [row for row in rows if row["case"] == case]
    present_configs = {row["config"] for row in case_rows}
    expected_configs = set(CONFIGS)
    if present_configs != expected_configs:
        return False, "incomplete-config-matrix"
    attempts = {
        config: sum(row["config"] == config for row in case_rows)
        for config in expected_configs
    }
    if not attempts or len(set(attempts.values())) != 1:
        return False, "uneven-trial-matrix"
    if not case_rows or not all(row["host_ok"] for row in case_rows):
        return False, "invalid-host-trials"
    trial_sets = {
        config: [row.get("trial") for row in case_rows if row["config"] == config]
        for config in expected_configs
    }
    if any(len(trials) != len(set(trials)) for trials in trial_sets.values()):
        return False, "uneven-trial-matrix"
    shared_fields = (
        "host",
        "host_executable",
        "host_executable_sha256",
        "host_executable_sha256_after",
        "task_sha256",
        "seed_sha256",
        "oracle_sha256",
        "runner_sha256",
        "configs_sha256",
    )
    for field in shared_fields:
        observed = {row.get(field) for row in case_rows}
        if None in observed or len(observed) != 1:
            return False, f"contract-drift:{field}"
    for config in expected_configs:
        prepared = {
            row.get("prepared_project_sha256")
            for row in case_rows
            if row["config"] == config
        }
        if None in prepared or len(prepared) != 1:
            return False, f"prepared-project-drift:{config}"
    baseline_has_signal = any(
        row["config"] == "baseline"
        and row["behavior"] is True
        and row["norm"] is False
        for row in case_rows
    )
    if not baseline_has_signal:
        return False, "baseline-no-boundary-signal"
    return True, "resolved"


def summarize(rows: list[dict]) -> list[dict]:
    axes = ("behavior", "norm")
    summary = []
    for case, config in dict.fromkeys(
        (row["case"], row["config"]) for row in rows
    ):
        group = [
            row
            for row in rows
            if row["case"] == case and row["config"] == config
        ]
        valid = [row for row in group if row["host_ok"]]
        resolved, resolution_reason = _resolution(rows, case)
        entry = {
            "case": case,
            "config": config,
            "attempted": len(group),
            "n": len(valid),
            "invalid": len(group) - len(valid),
        }
        if valid:
            observed = {
                axis: round(
                    statistics.mean(1.0 if row[axis] else 0.0 for row in valid),
                    3,
                )
                for axis in axes
            }
            observed["both"] = round(
                statistics.mean(
                    1.0 if all(row[axis] for axis in axes) else 0.0
                    for row in valid
                ),
                3,
            )
        else:
            observed = {"behavior": None, "norm": None, "both": None}
        if case == "layer_boundary":
            entry.update(
                {
                    "resolution": resolved,
                    "resolution_reason": resolution_reason,
                    **{
                        f"observed_{axis}": observed[axis]
                        for axis in (*axes, "both")
                    },
                }
            )
            entry.update(
                observed
                if resolved
                else {"behavior": None, "norm": None, "both": None}
            )
        else:
            entry.update(observed)
        summary.append(entry)
    return summary


def render(summary: list[dict]) -> str:
    header = (
        f"{'case':18s} {'config':22s} {'resolution':>10s} {'tries':>5s} "
        f"{'valid':>5s} {'invalid':>7s} {'behavior':>9s} {'norm':>6s} "
        f"{'both':>6s}"
    )
    lines = [header, "-" * len(header)]
    for entry in summary:
        rates = {
            axis: "n/a" if entry[axis] is None else f"{entry[axis]:.0%}"
            for axis in ("behavior", "norm", "both")
        }
        resolution = entry.get("resolution")
        resolution_label = (
            "n/a"
            if resolution is None
            else "yes"
            if resolution
            else "null"
        )
        lines.append(
            f"{entry['case']:18s} {entry['config']:22s} "
            f"{resolution_label:>10s} "
            f"{entry['attempted']:5d} {entry['n']:5d} {entry['invalid']:7d} "
            f"{rates['behavior']:>9s} {rates['norm']:>6s} {rates['both']:>6s}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
