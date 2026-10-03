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


def review_fingerprint(case: dict, response: object) -> str:
    payload = json.dumps({"case": case, "response": response}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reviewed_pairs(case: dict, response: dict, adjudication: object) -> tuple[bool, set[tuple[int, int]]]:
    findings, expected = response["findings"], case["expected_findings"]
    if not findings:
        return True, set()
    if (not isinstance(adjudication, dict)
            or adjudication.get("review_sha256") != review_fingerprint(case, response)
            or not isinstance(adjudication.get("findings"), list)
            or any(not isinstance(defect.get("invariant"), str) or not defect["invariant"].strip()
                   for defect in expected)):
        return False, set()
    reviewed, accepted = set(), set()
    for item in adjudication["findings"]:
        if not isinstance(item, dict):
            return False, set()
        finding, defect = item.get("finding_index"), item.get("expected_finding_index")
        if (type(finding) is not int or not 0 <= finding < len(findings)
                or type(defect) is not int or not 0 <= defect < len(expected)
                or type(item.get("cause_correct")) is not bool
                or type(item.get("contract_correct")) is not bool
                or (finding, defect) in reviewed):
            return False, set()
        reviewed.add((finding, defect))
        if item["cause_correct"] and item["contract_correct"]:
            accepted.add((finding, defect))
    complete = {finding for finding, _ in reviewed} == set(range(len(findings)))
    return complete, accepted


def score_review(case: dict, response: object, *, host_ok: bool, reads: set[str], config: str,
                 adjudication: object = None) -> dict:
    if not isinstance(response, dict):
        response = {}
    valid = (isinstance(response.get("verdict"), str)
             and response["verdict"] in {"approve", "request-changes"})
    findings = response.get("findings") if valid else None
    valid = valid and isinstance(findings, list) and all(
        isinstance(finding, dict) and isinstance(finding.get("file"), str)
        and type(finding.get("line")) is int and isinstance(finding.get("reason"), str)
        and bool(finding["reason"].strip()) for finding in findings
    )
    findings = findings if valid else ()
    expected = case["expected_findings"]

    def matches_location(finding: dict, region: dict) -> bool:
        return finding["file"] == region["file"] and region["start_line"] <= finding["line"] <= region["end_line"]

    def matches(finding: dict, defect: dict) -> bool:
        return matches_location(finding, defect) or any(
            matches_location(finding, region) for region in defect.get("alternate_locations", ())
        )

    reviewed, accepted = _reviewed_pairs(case, response, adjudication) if valid else (False, set())
    matched = {(i, j) for i, finding in enumerate(findings or ()) for j, defect in enumerate(expected)
               if valid and (i, j) in accepted and matches(finding, defect)}
    verdict_ok = bool(valid and response["verdict"] == case["expected_verdict"])
    defects_found = bool(valid and all(any(j == index for _, j in matched)
                                      for index in range(len(expected))))
    no_false_positives = bool(valid and all(any(i == index for i, _ in matched)
                                          for index in range(len(findings or ()))))
    references_opened = None if config == "baseline" else all(ref in reads for ref in case["required_references"])
    non_target_respected = not any(
        path.startswith(skill + "/") for path in reads for skill in case["forbidden_skills"]
    )
    return {
        "response_valid": bool(valid), "verdict_ok": verdict_ok,
        "defects_found": defects_found, "no_false_positives": no_false_positives,
        "reasons_reviewed": reviewed,
        "references_opened": references_opened, "non_target_respected": non_target_respected,
        "passed": bool(host_ok and verdict_ok and defects_found and no_false_positives and reviewed
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
            "review_sha256": review_fingerprint(case, response),
            "score": score_review(case, response, host_ok=host_ok, reads=reads, config=config),
        }
        (destination / "stdout.jsonl").write_text(stdout, encoding="utf-8")
        (destination / "stderr.log").write_text(stderr, encoding="utf-8")
        (destination / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        return row


def parse_adjudications(content: bytes) -> dict[str, dict]:
    adjudications = json.loads(content)["reviews"]
    if not isinstance(adjudications, list):
        raise ValueError("adjudications must contain a reviews list")
    by_digest = {}
    for adjudication in adjudications:
        if not isinstance(adjudication, dict) or not isinstance(adjudication.get("review_sha256"), str):
            raise ValueError("each adjudication must identify its review_sha256")
        digest = adjudication["review_sha256"]
        if digest in by_digest:
            raise ValueError("duplicate review adjudication")
        by_digest[digest] = adjudication
    return by_digest


def rescore_report(report_path: Path, adjudications_path: Path, output: Path) -> dict:
    report_bytes, adjudication_bytes = report_path.read_bytes(), adjudications_path.read_bytes()
    report = json.loads(report_bytes)
    if not isinstance(report, dict) or not isinstance(report.get("results"), list) or not report["results"]:
        raise ValueError("report must contain recorded review results")
    by_digest = parse_adjudications(adjudication_bytes)
    rows = []
    cases = {}
    for original in report["results"]:
        case_path = fixture_path(
            report_path.parent, f"{original['case']}-{original['config']}-{original['trial']}/case.json"
        )
        case_bytes = case_path.read_bytes()
        case = json.loads(case_bytes)
        cases[case_path.relative_to(report_path.parent)] = case_bytes
        case_digest = hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()
        if case_digest != original["case_sha256"]:
            raise ValueError("recorded case changed after the model review")
        response = original["response"]
        digest = review_fingerprint(case, response)
        if "review_sha256" in original and original["review_sha256"] != digest:
            raise ValueError("recorded response changed after the model review")
        row = dict(original)
        row["review_sha256"] = digest
        row["score"] = score_review(
            case, response, host_ok=original["host_ok"], reads=set(original["observed_skill_reads"]),
            config=original["config"], adjudication=by_digest.get(digest),
        )
        rows.append(row)
    result = {
        "metadata": {
            "original": report["metadata"],
            "source_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "adjudications_sha256": hashlib.sha256(adjudication_bytes).hexdigest(),
            "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limitation": "External cause/contract adjudications, not automated semantic or runtime proof.",
        },
        "results": rows,
    }
    output.mkdir(parents=True, exist_ok=False)
    for relative, content in cases.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (output / "source-report.json").write_bytes(report_bytes)
    (output / "adjudications.json").write_bytes(adjudication_bytes)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append")
    parser.add_argument("--model")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rescore", type=Path, help="recorded report.json; does not invoke a model")
    parser.add_argument("--adjudications", type=Path, help="independent cause/contract review decisions")
    args = parser.parse_args()
    if args.rescore:
        if args.adjudications is None or args.model or args.case:
            parser.error("--rescore requires --adjudications and cannot use --model or --case")
        report = rescore_report(args.rescore, args.adjudications, args.output)
        rows = report["results"]
        print(json.dumps({"trials": len(rows), "passed": sum(row["score"]["passed"] for row in rows),
                          "report": str(args.output / "report.json")}))
        return 0 if all(row["score"]["passed"] for row in rows) else 1
    if not args.model or args.adjudications:
        parser.error("model runs require --model; --adjudications is only valid with --rescore")
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
                "limitation": "Locations and observed reads are deterministic; finding reasons require "
                              "independent cause/contract adjudication. No UI/DB execution, model internals, "
                              "or isolation from host-global skills is established."}
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
