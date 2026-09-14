from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
UNAVAILABLE = "unavailable"
SOURCE_PATHS = ("src", "skills", "templates", "evals", "bin", "lib", "pyproject.toml", "package.json")


def digest(data: bytes) -> str:
    """Return the SHA-256 identity of immutable measurement bytes."""
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: object) -> None:
    """Write deterministic UTF-8 JSON evidence with a trailing newline."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_identity(root: Path) -> dict[str, str]:
    """Capture content identities for every source file in a snapshot."""
    return {path.relative_to(root).as_posix(): digest(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}


def load_cases(directory: Path, *, source: Path | None = None) -> list[dict]:
    """Load and validate the immutable fixture corpus and its references."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    content = (directory / "cases.json").read_bytes()
    if digest(content) != manifest["cases_sha256"]:
        raise ValueError("immutable fixture digest mismatch; create a separately versioned fixture set")
    cases = json.loads(content)
    for case in cases:
        identifier = case.get("id")
        if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identifier) is None:
            raise ValueError(f"unsafe fixture id: {identifier!r}")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("duplicate fixture id")
    spec = importlib.util.spec_from_file_location("t6_context_render", HERE / "render.py")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    for case in cases:
        renderer.validate_case_paths(case)
        if source is not None:
            renderer.required_reference_paths(source, case)
    return cases


def extract_baseline(archive: Path, destination: Path) -> None:
    """Extract only repository source members from a safe baseline archive."""
    destination.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe baseline member: {member.name}")
            if not relative.parts or relative.parts[0] not in SOURCE_PATHS:
                continue
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"non-regular baseline source: {member.name}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable baseline member: {member.name}")
                with stream, target.open("wb") as handle:
                    shutil.copyfileobj(stream, handle)
    if not (destination / "src/agent_flow/core/skill_resolver.py").is_file():
        raise ValueError("archive must be a repository-root git archive")


def snapshot_after(source: Path, destination: Path) -> None:
    """Copy the selected post-change source set into measurement evidence."""
    destination.mkdir(parents=True)
    for name in SOURCE_PATHS:
        path = source / name
        if path.is_dir():
            shutil.copytree(path, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "results"))
        elif path.is_file():
            shutil.copy2(path, destination / name)


def invoke(command: list[str], cwd: Path, evidence: Path, *, timeout: float,
           prompt: bytes | None = None, env: dict | None = None) -> dict:
    """Run one measured subprocess and persist its raw execution evidence."""
    started = time.perf_counter()
    state = {"call_count": 0, "call_count_scope": "actual-subprocess-launches",
             "return_code": None, "timed_out": False, "launch_error": None}
    save(evidence / "command.json", {"argv": command, "cwd": str(cwd),
                                    "stdin_sha256": digest(prompt) if prompt is not None else None})
    with (evidence / "stdout.raw").open("wb") as stdout, (evidence / "stderr.raw").open("wb") as stderr:
        try:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True, env=env)
        except OSError as exc:
            state["launch_error"] = str(exc)
        else:
            state["call_count"] = 1
            try:
                process.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                state["timed_out"] = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            state["return_code"] = process.returncode
    state["wall_seconds"] = time.perf_counter() - started
    return state


def events_from(raw: str) -> list[dict]:
    """Parse dictionary events from a provider's JSON-lines output."""
    events = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def usage_from(provider: str, events: list[dict]) -> dict:
    """Normalize provider usage without inventing unavailable token counts."""
    result = {"input_tokens": UNAVAILABLE, "cached_tokens": UNAVAILABLE,
              "uncached_tokens": UNAVAILABLE, "cache_creation_tokens": UNAVAILABLE,
              "provider_call_count": UNAVAILABLE, "usage_source": UNAVAILABLE}
    if provider == "codex":
        reports = [e["usage"] for e in events if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)]
        if reports:
            result["usage_source"] = "codex turn.completed aggregate usage"
            for target, key in (("input_tokens", "input_tokens"), ("cached_tokens", "cached_input_tokens")):
                if all(type(r.get(key)) is int and r[key] >= 0 for r in reports):
                    result[target] = sum(r[key] for r in reports)
            if type(result["input_tokens"]) is int and type(result["cached_tokens"]) is int:
                if result["cached_tokens"] <= result["input_tokens"]:
                    result["uncached_tokens"] = result["input_tokens"] - result["cached_tokens"]
    else:
        reports = [e for e in events if e.get("type") == "result"]
        ids = {e.get("message", {}).get("id") for e in events
               if e.get("type") == "assistant" and isinstance(e.get("message"), dict) and e["message"].get("id")}
        if ids:
            result["provider_call_count"] = len(ids)
        if len(reports) == 1 and isinstance(reports[0].get("usage"), dict):
            report = reports[0]["usage"]
            result["usage_source"] = "claude result.usage (input excludes cache reads and writes)"
            for target, key in (("uncached_tokens", "input_tokens"), ("cached_tokens", "cache_read_input_tokens"),
                                ("cache_creation_tokens", "cache_creation_input_tokens")):
                if type(report.get(key)) is int and report[key] >= 0:
                    result[target] = report[key]
            parts = [result[key] for key in ("uncached_tokens", "cached_tokens", "cache_creation_tokens")]
            if all(type(value) is int for value in parts):
                result["input_tokens"] = sum(parts)
            if type(parts[0]) is int and type(parts[2]) is int:
                result["uncached_tokens"] = parts[0] + parts[2]
            else:
                result["uncached_tokens"] = UNAVAILABLE
    return result


def response_from(provider: str, events: list[dict], final: Path) -> object:
    """Extract one structured provider response when the evidence is valid."""
    if provider == "codex":
        try:
            return json.loads(final.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    results = [e for e in events if e.get("type") == "result"]
    if len(results) != 1:
        return None
    if isinstance(results[0].get("structured_output"), dict):
        return results[0]["structured_output"]
    try:
        return json.loads(results[0].get("result", ""))
    except (TypeError, ValueError):
        return None


def score(case: dict, response: object, execution: dict, *, fixture_unchanged: bool, scorer) -> dict:
    """Classify measurement and oracle failures without collapsing their causes."""
    host_ok = (execution["call_count"] == 1 and execution["return_code"] == 0
               and not execution["timed_out"] and not execution.get("provider_reported_error", False))
    result = scorer(case, response, host_ok=host_ok and fixture_unchanged, reads=set(), config="baseline")
    if execution["call_count"] == 0:
        status = "not-executed"
    elif execution["timed_out"]:
        status = "timeout"
    elif not host_ok:
        status = "provider-failure"
    elif not fixture_unchanged:
        status = "fixture-mutated"
    elif not result["response_valid"]:
        status = "malformed-response"
    elif not result["passed"]:
        status = "oracle-mismatch"
    else:
        status = "matched-oracle"
    return {**result, "status": status}


def compare(before: dict, after: dict) -> dict:
    """Compare paired measurements only when both sides produced usable evidence."""
    measured = all(row.get("score", {}).get("status") in {"matched-oracle", "oracle-mismatch"} for row in (before, after))
    result = {"semantic_parity": "unavailable", "both_match_oracle": False, "byte_delta_after_minus_before": UNAVAILABLE}
    if measured:
        result["semantic_parity"] = before["response"]["verdict"] == after["response"]["verdict"]
        result["both_match_oracle"] = before["score"]["passed"] and after["score"]["passed"]
        result["byte_delta_after_minus_before"] = after["input"]["raw_input_bytes"] - before["input"]["raw_input_bytes"]
    return result


def provider_command(provider: str, executable: str, model: str, schema: Path, final: Path) -> list[str]:
    """Build a non-interactive, tool-free command for the selected provider."""
    if provider == "codex":
        return [executable, "exec", "--ignore-user-config", "--sandbox", "read-only", "-c", 'approval_policy="never"',
                "-c", "project_doc_max_bytes=0", "--skip-git-repo-check", "--ephemeral", "--json", "--model", model,
                "--output-schema", str(schema), "--output-last-message", str(final), "-"]
    return [executable, "-p", "--model", model, "--output-format", "stream-json", "--verbose", "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--no-session-persistence", "--json-schema", schema.read_text(encoding="utf-8")]


def main() -> int:
    """Run paired T6 fixture measurements and persist their provenance."""
    parser = argparse.ArgumentParser(description="Manual T6 paired measurement; never runner review approval.",
                                     epilog="See tools/t6-context/README.txt for scope, usage semantics, and evidence files.")
    parser.add_argument("--baseline-archive", type=Path, required=True)
    parser.add_argument("--after", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=HERE / "fixtures")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--provider", choices=("claude", "codex"), action="append", required=True)
    parser.add_argument("--claude-model")
    parser.add_argument("--codex-model")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    cases = load_cases(args.fixtures, source=args.after.resolve())
    if args.case_ids:
        unknown = set(args.case_ids) - {case["id"] for case in cases}
        if unknown:
            parser.error("unknown fixture ids: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.case_ids]
    providers = list(dict.fromkeys(args.provider))
    if not args.render_only and any(not getattr(args, f"{provider}_model") for provider in providers):
        parser.error("each executed provider needs its explicit --<provider>-model")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = {side: output / "sources" / side for side in ("before", "after")}
    extract_baseline(args.baseline_archive, sources["before"])
    snapshot_after(args.after.resolve(), sources["after"])
    source_hashes = {side: tree_identity(path) for side, path in sources.items()}
    sys.path.insert(0, str(sources["after"] / "evals"))
    from skill_tasks import RESPONSE_SCHEMA, score_review

    schema = output / "response-schema.json"
    save(schema, RESPONSE_SCHEMA)
    manifest = {"baseline_archive_sha256": digest(args.baseline_archive.read_bytes()), "source_files": source_hashes,
                "harness_files": {p.name: digest(p.read_bytes()) for p in HERE.glob("*.py")},
                "fixture_file_sha256": digest((args.fixtures / "cases.json").read_bytes()),
                "scorer_sha256": digest((sources["after"] / "evals/skill_tasks.py").read_bytes()),
                "scope": "manual-nonblocking-fixture-evaluation-not-independent-runner-review", "providers": providers}
    save(output / "manifest.json", manifest)
    rows = []
    for case in cases:
        for provider in providers:
            pair = {}
            for side, source in sources.items():
                destination = (output / case["id"] / provider / side).resolve()
                if not destination.is_relative_to(output):
                    raise ValueError(f"fixture output escapes output root: {destination}")
                destination.mkdir(parents=True)
                case_file = destination / "case.json"
                save(case_file, case)
                render_dir = destination / "render"
                render_dir.mkdir()
                project = destination / "project"
                command = [sys.executable, str(HERE / "render.py"), str(source), str(project),
                           str(case_file), provider, str(render_dir)]
                render_execution = invoke(command, output, render_dir, timeout=args.timeout,
                                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                row = {"case": case["id"], "provider": provider, "side": side, "render_execution": render_execution}
                if render_execution["return_code"] != 0:
                    row["status"] = "render-failure"
                    row["execution"] = {"call_count": 0, "return_code": None, "timed_out": False, "wall_seconds": 0}
                    row["usage"] = usage_from(provider, [])
                    row["input"] = {"raw_input_bytes": UNAVAILABLE, "delivered_normative_bytes": UNAVAILABLE,
                                    "duplicated_bytes": UNAVAILABLE}
                    save(destination / "result.json", row)
                    pair[side] = row
                    rows.append(row)
                    continue
                render_data = json.loads((render_dir / "render.json").read_text(encoding="utf-8"))
                row["input"] = render_data["semantic"]
                row["render_seconds"] = render_data["render_seconds"]
                execution = {"call_count": 0, "return_code": None, "timed_out": False, "wall_seconds": 0}
                response = None
                events = []
                unchanged = True
                if not args.render_only:
                    call_dir = destination / "call"
                    call_dir.mkdir()
                    executable = shutil.which(provider) or provider
                    row["executable"] = {"path": executable, "sha256": digest(Path(executable).resolve().read_bytes())
                                         if Path(executable).is_file() else UNAVAILABLE}
                    row["model"] = getattr(args, f"{provider}_model")
                    final = call_dir / "response.raw"
                    command = provider_command(provider, executable, row["model"], schema, final)
                    prompt = (render_dir / "semantic.prompt.txt").read_bytes()
                    (call_dir / "stdin.raw").write_bytes(prompt)
                    seed = tree_identity(project)
                    execution = invoke(command, project, call_dir, timeout=args.timeout, prompt=prompt)
                    events = events_from((call_dir / "stdout.raw").read_text(encoding="utf-8", errors="replace"))
                    response = response_from(provider, events, final)
                    unchanged = seed == tree_identity(project)
                    if any(e.get("is_error") or e.get("type") in {"error", "turn.failed"} for e in events):
                        execution["provider_reported_error"] = True
                row.update({"execution": execution, "usage": usage_from(provider, events), "response": response,
                            "fixture_unchanged": unchanged,
                            "score": score(case, response, execution, fixture_unchanged=unchanged, scorer=score_review)})
                save(destination / "result.json", row)
                pair[side] = row
                rows.append(row)
            save(output / case["id"] / provider / "comparison.json", compare(pair["before"], pair["after"]))
    stable = source_hashes == {side: tree_identity(path) for side, path in sources.items()}
    save(output / "summary.json", {"rows": rows, "source_snapshots_unchanged": stable,
                                   "savings_claim": "none", "runner_review_approval": "not-applicable"})
    print(output / "summary.json")
    if args.render_only:
        return 0 if stable and all(row.get("status") != "render-failure" for row in rows) else 1
    return 0 if stable and all(row.get("score", {}).get("passed") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
