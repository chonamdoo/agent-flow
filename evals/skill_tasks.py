"""Manual, tool-enabled skill review tasks; model scores are not a required CI gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import yaml

from configs import KIT_ROOT, _install_skills
from run import _invoke_host

CASE_ROOT = Path(__file__).parent / "skill-task-cases"
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request-changes"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["file", "line", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}


def fixture_path(project: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError(f"fixture path must stay inside the project: {name!r}")
    return project.joinpath(*relative.parts)


def prepare_skill_index(project: Path) -> None:
    names = _install_skills(project)
    entries = []
    for name in names:
        path = project / ".agent-flow" / "skills" / name / "SKILL.md"
        metadata = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
        entries.append({"name": name, "description": metadata.get("description", ""),
                        "path": f".agent-flow/skills/{name}/SKILL.md"})
    (project / ".agent-flow" / "skills" / "index.json").write_text(
        json.dumps({"skills": entries}, ensure_ascii=False), encoding="utf-8"
    )


def _read_command_arguments(command: str) -> list[str]:
    try:
        arguments = shlex.split(command)
        if (len(arguments) == 3 and Path(arguments[0]).name in {"sh", "bash", "zsh"}
                and arguments[1] in {"-c", "-lc"}):
            command = arguments[2]
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    paths, segment = [], []
    for token in [*tokens, ";"]:
        if token and all(character in ";|&\n" for character in token):
            if segment and Path(segment[0]).name in {"cat", "sed", "head", "tail", "rg", "grep"}:
                paths.extend(segment[1:])
            segment = []
        else:
            segment.append(token)
    return paths


def observed_skill_reads(stdout: str) -> set[str]:
    reads = set()
    skill_root = KIT_ROOT / "skills"
    documents = {
        source.relative_to(skill_root).as_posix(): [
            line.strip() for line in source.read_text(encoding="utf-8").splitlines()
            if len(line.strip()) >= 20
        ]
        for source in skill_root.rglob("*.md")
    }
    for row in stdout.splitlines():
        try:
            event = json.loads(row)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if (not isinstance(item, dict) or item.get("type") != "command_execution"
                or item.get("exit_code") != 0 or not item.get("aggregated_output")):
            continue
        command = item.get("command", "")
        if not isinstance(command, str):
            continue
        for argument in _read_command_arguments(command):
            prefix = ".agent-flow/skills/"
            if prefix not in argument:
                continue
            relative = argument.split(prefix, 1)[1]
            if relative in documents and any(line in item["aggregated_output"] for line in documents[relative]):
                reads.add(relative)
    return reads


def score_review(case: dict, response: object, *, host_ok: bool, reads: set[str], config: str) -> dict:
    valid = (isinstance(response, dict) and isinstance(response.get("verdict"), str)
             and response["verdict"] in {"approve", "request-changes"})
    findings = response.get("findings") if valid else None
    valid = valid and isinstance(findings, list) and all(
        isinstance(finding, dict) and isinstance(finding.get("file"), str)
        and type(finding.get("line")) is int and isinstance(finding.get("reason"), str)
        and bool(finding["reason"].strip()) for finding in findings
    )
    expected = case["expected_findings"]

    def matches_location(finding: dict, region: dict) -> bool:
        return finding["file"] == region["file"] and region["start_line"] <= finding["line"] <= region["end_line"]

    def matches(finding: dict, defect: dict) -> bool:
        return matches_location(finding, defect) or any(
            matches_location(finding, region) for region in defect.get("alternate_locations", ())
        )

    verdict_ok = bool(valid and response["verdict"] == case["expected_verdict"])
    defects_found = bool(valid and all(any(matches(f, region) for f in findings) for region in expected))
    no_false_positives = bool(valid and all(any(matches(f, region) for region in expected) for f in findings))
    references_opened = None if config == "baseline" else all(ref in reads for ref in case["required_references"])
    non_target_respected = not any(
        path.startswith(skill + "/") for path in reads for skill in case["forbidden_skills"]
    )
    return {
        "response_valid": bool(valid), "verdict_ok": verdict_ok,
        "defects_found": defects_found, "no_false_positives": no_false_positives,
        "references_opened": references_opened, "non_target_respected": non_target_respected,
        "passed": bool(host_ok and verdict_ok and defects_found and no_false_positives
                       and references_opened is not False and non_target_respected),
    }


def run_case(case: dict, config: str, trial: int, *, model: str, executable: str,
             timeout: int, evidence_root: Path) -> dict:
    destination = evidence_root / f"{case['id']}-{config}-{trial}"
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="agent-flow-skill-task-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        for name, content in case["files"].items():
            target = fixture_path(project, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if config == "skill-index":
            prepare_skill_index(project)
        schema = root / "response-schema.json"
        schema.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        final = root / "response.json"
        prompt = (
            "Review the fixture project for this task. Do not edit files, run builds/tests, "
            "install packages, start a workflow, or use network services. You may use read-only "
            "tools to inspect local code and documents. If .agent-flow/skills/index.json exists, "
            "read its descriptions and load only skills and conditional references relevant to "
            "the task. For observable read traces, read each document in a separate cat or sed "
            "command with its literal path; avoid loops and commands with combined outputs. "
            "Report actual correctness issues, not preferred naming or file counts. "
            "Use project-relative finding paths. Return the required JSON.\n\n" + case["task"]
        )
        command = (
            executable, "exec", "--ignore-user-config", "--sandbox", "read-only",
            "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0",
            "--skip-git-repo-check", "--ephemeral", "--json", "--model", model,
            "--output-schema", str(schema), "--output-last-message", str(final), prompt,
        )
        code, timed_out, stdout, stderr, truncated = _invoke_host(
            command, project, timeout, output_limit=512 * 1024
        )
        try:
            response = json.loads(final.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            response = None
        try:
            fixture_unchanged = all(
                fixture_path(project, name).read_text(encoding="utf-8") == content
                for name, content in case["files"].items()
            )
        except OSError:
            fixture_unchanged = False
        reads = observed_skill_reads(stdout)
        host_ok = code == 0 and not timed_out and not truncated and fixture_unchanged
        row = {
            "case": case["id"], "skill": case["skill"], "kind": case["kind"],
            "config": config, "trial": trial, "requested_model": model,
            "command": list(command[:-1]),
            "host_ok": host_ok, "returncode": code, "timed_out": timed_out,
            "output_truncated": truncated, "fixture_unchanged": fixture_unchanged,
            "response": response, "observed_skill_reads": sorted(reads),
            "case_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
            "score": score_review(case, response, host_ok=host_ok, reads=reads, config=config),
        }
        (destination / "stdout.jsonl").write_text(stdout, encoding="utf-8")
        (destination / "stderr.log").write_text(stderr, encoding="utf-8")
        (destination / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append")
    parser.add_argument("--model", required=True)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.trials, args.concurrency, args.timeout) < 1:
        parser.error("trials, concurrency and timeout must be positive")
    cases = [case for path in sorted(CASE_ROOT.glob("*.json"))
             for case in json.loads(path.read_text(encoding="utf-8"))["cases"]]
    if args.case:
        missing = set(args.case) - {case["id"] for case in cases}
        if missing:
            parser.error(f"unknown cases: {sorted(missing)}")
        cases = [case for case in cases if case["id"] in args.case]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        parser.error("case IDs must be nonempty and unique")
    executable = shutil.which("codex")
    if executable is None:
        parser.error("codex is not installed")
    if args.output.exists() or args.output.is_symlink():
        parser.error(f"output already exists: {args.output}")
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    args.output.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__), Path(__file__).with_name("configs.py"), Path(__file__).with_name("run.py")]
    sources.extend(path for path in (KIT_ROOT / "skills").rglob("*") if path.is_file())
    metadata = {"cli_version": version, "executable": executable,
                "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
                "requested_model": args.model, "model_identity_verified": False,
                "trials": args.trials, "timeout": args.timeout, "concurrency": args.concurrency,
                "source_sha256": {
                    str(path.relative_to(KIT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(sources)
                },
                "limitation": "Line-region review scoring and observed read traces, not semantic proof, "
                              "UI/DB execution, model internals, or isolation from host-global skills."}
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    jobs = [(case, config, trial) for case in cases
            for config in ("baseline", "skill-index") for trial in range(args.trials)]

    def execute(job: tuple) -> dict:
        return run_case(*job, model=args.model, executable=executable,
                        timeout=args.timeout, evidence_root=args.output)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(execute, jobs))
    report = {"metadata": metadata, "results": rows}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "trials": len(rows), "host_ok": sum(row["host_ok"] for row in rows),
                      "passed": sum(row["score"]["passed"] for row in rows), "report": str(args.output / "report.json")}))
    return 0 if all(row["host_ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
