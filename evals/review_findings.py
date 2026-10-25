#!/usr/bin/env python3
"""Split code-review findings into bugs and unfinished work, and hidden-check failures into missing and wrong (#409 v3.10).

A reviewer flags both "this behaves wrong" and "this is not built yet". The hidden evaluator likewise fails a check
because the element, route or API is not there, or because it is there and behaves wrong. This tool separates both
sides and crosses them, after the runs have ended. It only reads run directories; it never writes into `project/`.

1. Findings. Each reviewer artifact (`<phase>-<angle>-<provider>.md`, listed in `<phase>-review-results.json`) is
   parsed into finding items: bullets with a `<severity:..>` tag or a `[file:line]` location, or any bullet under a
   Must-fix/Should-fix heading. Each item keeps the reviewer job id, provider, verdict, phase and round. Rounds come
   from `<run>/review-archive/` (every round, see `index.jsonl`) and from the final files left in the project.
2. Labels. A fixed prompt sent to `claude -p` (no tools) labels each finding `bug` (implemented, behaves wrong),
   `incomplete` (a requirement is not implemented), `design`, `procedure` (kit process) or `other`, with a one-line
   rationale. Labels are cached by finding sha, so re-runs cost nothing.
3. Hidden failures. Fixed rules on the failure message give `missing` (not found, 404/501, locator timeout, count 0)
   or `wrong` (value, state or text differs). What the rules cannot decide is `unclear` and goes to the classifier.
   A run whose build failed to install has every check `missing` (`no_build`).
4. Matching. Per run, one classifier call gets the PRD/contract text (paths given on the command line), the failed
   checks (id, spec text read from the check definitions given on the command line, message) and the findings. It
   says which findings point at each failed check, decides the `unclear` failures, and says whether the PRD supports
   each bug/incomplete finding. Recall is the share of failed checks some finding pointed at, split by missing and
   wrong, for the final round and for any round. Precision is the share of final-round bug/incomplete findings that
   point at a hidden failure (the reviewed code is the final build), or, looser, that the PRD supports.
5. Completeness (v3.11): `complete` when no check is missing, the missing-check ratio, the wrong ratio among the
   checks that are not missing, and a false-completion grade for declared-complete runs.

    python3 evals/review_findings.py extract <run>...
    python3 evals/review_findings.py analyze --checks <api_checks.py> --checks <browser.json> --prd <prd.md> \\
        --cache <cache.json> --out <dir> <run>...
    python3 evals/review_findings.py report --markdown <table.md> <dir>...

Check definitions: a `.json` file with `checks: [{id, spec, title, ...}]`, or a `.py` file with a module-level
`CHECKS` dict of id to function (its docstring, or its source, is the spec text). Labels run from an empty temporary
directory with `CLAUDE_CODE_EFFORT_LEVEL=medium`; `--budget` caps the spend.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable

LABELS = ("bug", "incomplete", "design", "procedure", "other")
KINDS = ("missing", "wrong")
MODEL = "claude-opus-5-5"
EFFORT = "medium"
LABEL_BATCH = 40
MATCH_BATCH = 120

LABEL_PROMPT = """You label code-review findings. Each finding is one item a reviewer wrote about a change that builds a \
product from a PRD (requirements) and a contract (interface: routes, APIs, fields, test ids). Give each finding exactly \
one label, judged by the defect the finding claims, not by its severity or tone.

- bug: the finding claims something that is implemented behaves wrong: a wrong value, condition, order, timing, \
status code, state transition or message; a crash, race, lost or corrupted data, a leak, a security hole, an unhandled \
error in code that exists. A missing check or edge case inside an existing feature is a bug, unless the next rule \
applies.
- incomplete: the finding claims that something the PRD or contract requires is not implemented: an absent screen, \
route, API, field, element or test id; a required state, message, rule or flow that the code never produces; a stub \
or placeholder; a feature that exists but is not wired up. A missing error display or edge-case handling is \
incomplete when the finding says the PRD or contract requires it.
- design: structure, layering, module size, responsibility split, coupling, duplication or maintainability, with no \
claim that the product behaves wrong or lacks a requirement.
- procedure: the agent-flow process rather than the product: architecture mode or selection, gates or checks not run, \
missing test evidence or missing tests, artifact or report format, branch or workflow rules, review markers.
- other: style, naming, comments, docs; an observation that something is fine or matches the contract; a note with no \
defect claim; anything else.

Input: a JSON list of {"id", "section", "severity", "text"}. Output only JSON, no prose and no code fence:
{"labels": [{"id": "<id>", "label": "<bug|incomplete|design|procedure|other>", "why": "<one short line>"}]}
Return one entry for every input id."""

MATCH_PROMPT = """You cross hidden acceptance checks with code-review findings for one build. You get the product's PRD \
and contract text, the hidden checks that failed on the final build, and review findings. Output only JSON, no prose \
and no code fence.

Task A. Every failed check has a "rule" of missing, wrong or unclear. For each check with rule "unclear", decide its \
kind: "missing" when the element, route, API, field or behaviour the check needs is not there at all (not found, never \
produced, not implemented); "wrong" when it is there but its value, state, timing or text differs from what is \
required. For the other checks repeat the rule as the kind.

Task B. For each failed check, list the ids of findings that point at the defect this check detects: the finding is \
about the same requirement, behaviour, element or field that the check exercises, and claims it is absent or wrong in \
a way that would make this check fail. Same feature area but a different defect does not match. A generic finding \
("tests are missing", "architecture pending") does not match. A finding that only says the area is fine does not \
match. Use an empty list when nothing matches.

Task C. For each finding id in "judge_prd", say whether the PRD or contract text states the requirement that the \
finding says is missing or violated: "supported" (the text requires what the finding says is absent or wrong), \
"contradicted" (the text allows the current behaviour or requires something else), "not-covered" (the text is \
silent; general robustness or taste).

Output:
{"checks": [{"id": "<check id>", "kind": "<missing|wrong>", "findings": ["<finding id>", ...]}],
 "prd": [{"id": "<finding id>", "verdict": "<supported|contradicted|not-covered>"}]}
Return one entry per failed check and one per judge_prd id."""


# Frozen after validation against 20 hand-labelled findings (19/20 agree, #409 v3.10). Changing LABEL_PROMPT changes
# its cache key and needs a new validation; the test pins this value.
LABEL_PROMPT_SHA256 = "fc6e89b3cffd0792cfcb8b1eedf9236a1fd969d70b510399cec580d684111d2d"


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- review artifacts

SEVERITY = re.compile(r"<severity:\s*([A-Za-z_-]+)\s*>")
LOCATION = re.compile(r"\[([^\]\s]+?:\d+(?:\s*[-–,]\s*\d+)*)\]")
BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
BOLD_HEADING = re.compile(r"^\s*\*\*([^*]+?)\*\*:?\s*$")
EMPTY_ITEM = re.compile(r"^((none|nothing|n/?a|no (blocking |further |other )?(findings?|issues?))\b|(해당\s*)?없음)",
                        re.I)
BARE_LOCATION = re.compile(r"^`?([\w./-]+\.\w+:\d+(?:[-–]\d+)?)`?:?\s")
MARKER_ITEM = re.compile(r"^(verdict|must-avoid-check|reviewer-source|status)\s*:", re.I)


def section_of(heading: str) -> str:
    text = heading.lower().replace("_", "-")
    if re.search(r"must[- ]?fix|blocking|critical|blocker", text):
        return "must-fix"
    if re.search(r"should[- ]?fix|recommend|important", text):
        return "should-fix"
    if re.search(r"note|nit|minor|observation|optional|consider", text):
        return "notes"
    if re.search(r"right|calibration|strength|positive|good", text):
        return "praise"
    return "other"


def review_body(text: str) -> str:
    """The reviewer's own output: the fenced block after `## review output`, else the whole text."""
    match = re.search(r"^##\s+review output\s*$", text, re.M | re.I)
    if not match:
        return text
    rest = text[match.end():]
    fence = re.search(r"^(`{3,}|~{3,})[^\n]*\n", rest, re.M)
    if not fence:
        end = re.search(r"^##\s", rest, re.M)
        return rest[:end.start()] if end else rest
    mark = fence.group(1)
    body = rest[fence.end():]
    close = re.search(rf"^{re.escape(mark)}\s*$", body, re.M)
    return body[:close.start()] if close else body


def artifact_meta(text: str) -> dict:
    title = re.search(r"^#\s+(\S+)", text, re.M)
    provider = re.search(r"^-\s*provider:\s*(\S+)", text, re.M)
    verdicts = re.findall(r"^verdict:\s*(approve|request-changes)\s*$", text, re.M)
    return {"job_id": title.group(1) if title else None, "provider": provider.group(1) if provider else None,
            "verdict": verdicts[-1] if verdicts else None}


def parse_findings(text: str) -> list[dict]:
    """Finding items of one reviewer artifact, in order."""
    items: list[dict] = []
    section, heading = "other", ""
    current: dict | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            body = " ".join(part.strip() for part in current.pop("parts") if part.strip())
            current["text"] = re.sub(r"\s+", " ", body).strip()
            if keep(current):
                severity = SEVERITY.search(current["text"])
                location = LOCATION.search(current["text"]) or BARE_LOCATION.match(current["text"])
                current["severity"] = severity.group(1).lower() if severity else None
                current["location"] = location.group(1) if location else None
                items.append(current)
        current = None

    def keep(item: dict) -> bool:
        text = SEVERITY.sub("", item["text"]).strip()
        if not text or EMPTY_ITEM.match(text) or MARKER_ITEM.match(text):
            return False
        return bool(SEVERITY.search(item["text"]) or LOCATION.search(item["text"]) or BARE_LOCATION.match(text)
                    or item["section"] in ("must-fix", "should-fix"))

    in_fence = False
    for line in review_body(text).splitlines():
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_fence = not in_fence
            if current is not None:
                current["parts"].append(line)
            continue
        if not in_fence:
            heading_match = HEADING.match(line) or BOLD_HEADING.match(line)
            if heading_match:
                close()
                heading = heading_match.group(heading_match.lastindex).strip()
                section = section_of(heading)
                continue
            bullet = BULLET.match(line)
            if bullet and len(bullet.group(1).expandtabs()) <= 1:
                close()
                current = {"section": section, "heading": heading, "parts": [bullet.group(2)]}
                continue
            if not line.strip():
                if current is not None and current["parts"] and current["parts"][-1] == "":
                    close()
                elif current is not None:
                    current["parts"].append("")
                continue
            if current is not None and not line.startswith((" ", "\t")) and not bullet:
                close()
                continue
        if current is not None:
            current["parts"].append(line)
    close()
    return items


def finding_sha(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()


def _phase_of(results_name: str) -> str:
    return results_name[: -len("-review-results.json")] if results_name.endswith("-review-results.json") \
        else results_name.rsplit(".", 1)[0]


def _review_round(results: dict, artifacts: Callable[[dict], str | None], **meta) -> dict:
    reviewers = []
    for outcome in results.get("outcomes") or []:
        text = artifacts(outcome)
        if text is None:
            reviewers.append({"job_id": outcome.get("job_id"), "provider": outcome.get("provider"),
                              "verdict": outcome.get("verdict"), "artifact": outcome.get("artifact"),
                              "missing_artifact": True, "findings": []})
            continue
        head = artifact_meta(text)
        reviewers.append({"job_id": outcome.get("job_id") or head["job_id"],
                          "provider": outcome.get("provider") or head["provider"],
                          "verdict": outcome.get("verdict") or head["verdict"], "artifact": outcome.get("artifact"),
                          "findings": parse_findings(text)})
    verdicts = [reviewer["verdict"] for reviewer in reviewers]
    verdict = "request-changes" if "request-changes" in verdicts else "approve" if verdicts else None
    return {**meta, "verdict": verdict, "reviewers": reviewers}


def _worktree_runs(run: Path) -> list[Path]:
    return sorted((run / "project" / ".git" / "agent-flow" / "worktrees").glob("*/.agent-flow/runs/*"))


def load_rounds(run: Path, skip_phase: re.Pattern | None = None) -> list[dict]:
    """Every review round of a run, oldest first, each with its reviewers and their findings."""
    found: list[tuple[tuple, dict]] = []
    seen_results: set[str] = set()
    archive = run / "review-archive"
    index_path = archive / "index.jsonl"
    if index_path.exists():
        index = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        files = {entry["seq"]: next(iter(sorted(archive.glob(f"{entry['seq']:04d}-*"))), None) for entry in index}
        by_sha = {entry["sha"]: files[entry["seq"]] for entry in index if files.get(entry["seq"])}
        for entry in index:
            if not entry["file"].endswith("review-results.json") or not files.get(entry["seq"]):
                continue
            results = json.loads(files[entry["seq"]].read_text(encoding="utf-8"))
            seen_results.add(entry["sha"])

            def from_archive(outcome: dict, entry=entry) -> str | None:
                path = by_sha.get(outcome.get("artifact_sha256") or "")
                if path is None:
                    earlier = [e for e in index if e["file"] == outcome.get("artifact") and e["seq"] < entry["seq"]
                               and e.get("run_id") == entry.get("run_id") and files.get(e["seq"])]
                    path = files[earlier[-1]["seq"]] if earlier else None
                return path.read_text(encoding="utf-8") if path else None

            found.append(((entry.get("run_id") or "", 0, entry["seq"]),
                          _review_round(results, from_archive, run_id=entry.get("run_id"),
                                        phase=_phase_of(entry["file"]), source="archive")))
    for directory in _worktree_runs(run):
        for path in sorted(directory.glob("*review-results.json")):
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() in seen_results:
                continue
            results = json.loads(data)

            def from_project(outcome: dict, directory=directory) -> str | None:
                artifact = directory / (outcome.get("artifact") or "")
                return artifact.read_text(encoding="utf-8") if outcome.get("artifact") and artifact.is_file() else None

            found.append(((directory.name, 1, 0), _review_round(results, from_project, run_id=directory.name,
                                                                 phase=_phase_of(path.name), source="final")))
    found.sort(key=lambda pair: pair[0])
    rounds = [entry for _, entry in found if not (skip_phase and skip_phase.search(entry["phase"] or ""))]
    entered = _phases_entered(run)
    per_phase: Counter = Counter()
    for number, entry in enumerate(rounds, start=1):
        per_phase[entry["phase"]] += 1
        entry["order"] = number
        entry["seq"] = per_phase[entry["phase"]]
    if not index_path.exists() and entered:
        for entry in rounds:  # only the last round of each phase survived; number it as the runner did
            entry["seq"] = max(entry["seq"], entered.count(entry["phase"]))
            entry["earlier_rounds_lost"] = entry["seq"] > 1
    last_run_id = rounds[-1]["run_id"] if rounds else None
    last_of_phase = {entry["phase"]: entry["order"] for entry in rounds if entry["run_id"] == last_run_id}
    for entry in rounds:
        entry["final"] = last_of_phase.get(entry["phase"]) == entry["order"]
        for reviewer in entry["reviewers"]:
            for item in reviewer["findings"]:
                item["sha"] = finding_sha(item["text"])
    return rounds


def _phases_entered(run: Path) -> list[str]:
    result = _read_json(run / "result.json") or {}
    return list(result.get("phases_entered") or [])


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def flat_findings(rounds: list[dict]) -> list[dict]:
    rows = []
    for entry in rounds:
        for reviewer in entry["reviewers"]:
            for item in reviewer["findings"]:
                rows.append({**item, "phase": entry["phase"], "round": entry["seq"], "order": entry["order"],
                             "final": entry["final"], "run_id": entry["run_id"], "job_id": reviewer["job_id"],
                             "provider": reviewer["provider"], "verdict": reviewer["verdict"]})
    return rows


# ---------------------------------------------------------------- hidden failures

MISSING_RULES = (
    re.compile(r"\b(404|405|501)\b|not found|not implemented|cannot (get|post|put|delete|patch)\b", re.I),
    re.compile(r"^locator\.\w+: Timeout \d+ms exceeded", re.I),
    re.compile(r"\((locator\.\w+: )?Timeout \d+ms exceeded\.?\)\s*$", re.I),
    re.compile(r"\bvisible \(not visible\)\s*$"),
    re.compile(r"\bcount \(count 0\)\s*$"),
    re.compile(r"\bno such (element|file|route)\b", re.I),
)
WRONG_RULES = (
    # The API answered, but the object lacks a field the contract names: the endpoint exists with another shape.
    re.compile(r"^KeyError: '|: \w+ = undefined, want\b|^AssertionError: no \w+ for \S+: [{\[]"),
    re.compile(r"\((got|count [1-9]\d*|focus on|enabled|disabled|still visible|covered|not above|not below)\b", re.I),
    re.compile(r"\b(disabled|enabled|hidden|focused|selected label|text|url|uncovered) \(", re.I),
    re.compile(r"\bwant\b.*\bgot\b|\bgot\b.*\bwant\b|expected .* (got|received)", re.I),
    re.compile(r"^AssertionError: .*(status \d{3}, want|\{|\[|\()", re.I),
)
UNCLEAR_RULES = (
    re.compile(r"ECONNREFUSED|ECONNRESET|socket hang up|connection (refused|reset)", re.I),
)


def rule_kind(message: str | None) -> str:
    """missing / wrong / unclear from one failure message, by fixed rules (unclear first, then missing, then wrong)."""
    text = (message or "").strip()
    if not text:
        return "unclear"
    for pattern in UNCLEAR_RULES:
        if pattern.search(text):
            return "unclear"
    if WRONG_RULES[0].search(text):
        return "wrong"
    for pattern in MISSING_RULES:
        if pattern.search(text):
            return "missing"
    for pattern in WRONG_RULES:
        if pattern.search(text):
            return "wrong"
    return "unclear"


def base_id(check_id: str) -> str:
    return check_id.split("@", 1)[0]


def hidden_failures(run: Path) -> dict:
    """Failed checks of a run, merged across viewports, each with its messages and rule kind."""
    result = _read_json(run / "result.json") or {}
    score = _read_json(run / "ui-score.json") or {}
    ui = result.get("ui") or {}
    failed = score.get("failed_checks") or ui.get("failed_checks")
    if failed is None:
        failed = [*(score.get("failed_browser") or ui.get("failed_browser") or []),
                  *(score.get("failed_api") or ui.get("failed_api") or [])]
    messages: dict[str, str] = {}
    for key in ("api_failures", "ui_failures", "first_failures", "failures"):
        for check, message in (score.get(key) or {}).items():
            messages.setdefault(check, str(message))
    install = score.get("install") or {}
    no_build = bool(failed) and not messages and (install.get("exit") not in (0, None) or not ui.get("G"))
    merged: dict[str, dict] = {}
    for check in failed:
        row = merged.setdefault(base_id(check), {"id": base_id(check), "variants": [], "messages": []})
        row["variants"].append(check)
        if check in messages:
            row["messages"].append(messages[check])
    rank = {"missing": 0, "wrong": 1, "unclear": 2}
    for row in merged.values():
        if no_build:
            row["rule"], row["reason"] = "missing", "no-build"
        else:
            kinds = [rule_kind(message) for message in row["messages"]] or ["unclear"]
            row["rule"] = min(kinds, key=rank.__getitem__)
    return {"declaration": result.get("declaration"), "S": ui.get("S", score.get("S")), "G": ui.get("G", score.get("G")),
            "no_build": no_build, "failed": sorted(merged.values(), key=lambda row: row["id"])}


# ---------------------------------------------------------------- check specs and PRD

def load_check_specs(paths: Iterable[str]) -> dict[str, str]:
    """id -> spec text, from browser-style JSON or a Python module with a `CHECKS` dict."""
    specs: dict[str, str] = {}
    for raw in paths:
        path = Path(raw)
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            for check in (data.get("checks") if isinstance(data, dict) else data) or []:
                steps = json.dumps(check.get("steps") or [], ensure_ascii=False)
                spec = " ".join(str(check.get(key) or "") for key in ("spec", "title", "description")).strip()
                specs[str(check["id"])] = f"{spec} | steps: {steps[:700]}"
        elif path.suffix == ".py":
            specs.update(_python_specs(path))
    return specs


def _python_specs(path: Path) -> dict[str, str]:
    try:
        spec = importlib.util.spec_from_file_location(f"_checks_{hashlib.sha1(str(path).encode()).hexdigest()[:8]}",
                                                      path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        checks = getattr(module, "CHECKS", None)
    except Exception:  # noqa: BLE001 - fall back to reading the source
        checks = None
    if isinstance(checks, dict):
        out = {}
        for check_id, function in checks.items():
            doc = inspect.getdoc(function)
            if not doc:
                try:
                    doc = inspect.getsource(function)
                except (OSError, TypeError):
                    doc = ""
            out[str(check_id)] = doc[:1500]
        return out
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name: (ast.get_docstring(node) or "")[:1500] for node in tree.body
            if isinstance(node, ast.FunctionDef) and re.fullmatch(r"[a-z]+\d+", node.name)}


def spec_for(check_id: str, specs: dict[str, str]) -> str:
    if check_id in specs:
        return specs[check_id]
    lowered = {key.lower(): value for key, value in specs.items()}
    return lowered.get(check_id.lower(), "")


# ---------------------------------------------------------------- classifier

def run_claude(prompt: str, system: str, model: str = MODEL, effort: str = EFFORT, timeout: int = 900) -> dict:
    """One `claude -p` call with no tools, from an empty directory. Returns {"text", "cost"}."""
    env = {**os.environ, "CLAUDE_CODE_EFFORT_LEVEL": effort}
    with tempfile.TemporaryDirectory(prefix="review-findings-") as cwd:
        done = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "", "--strict-mcp-config",
             "--no-session-persistence", "--setting-sources", "", "--system-prompt", system],
            input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout, check=False)
    try:
        payload = json.loads(done.stdout)
    except ValueError as error:
        raise RuntimeError(f"claude -p gave no JSON (exit {done.returncode}): {done.stderr[-400:]}") from error
    if payload.get("is_error"):
        raise RuntimeError(f"claude -p failed: {str(payload.get('result'))[:400]}")
    return {"text": payload.get("result") or "", "cost": float(payload.get("total_cost_usd") or 0.0)}


def parse_json_reply(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


class Classifier:
    """Cached, budgeted classifier calls. `runner(prompt, system) -> {"text", "cost"}` is replaceable in tests."""

    def __init__(self, cache_path: Path | None, runner: Callable[[str, str], dict] = run_claude,
                 budget: float = 10.0, label_batch: int = LABEL_BATCH, match_batch: int = MATCH_BATCH):
        self.cache_path, self.runner, self.budget = cache_path, runner, budget
        self.label_batch, self.match_batch = label_batch, match_batch
        self.cache = (_read_json(cache_path) or {}) if cache_path else {}
        self.cache.setdefault("labels", {})
        self.cache.setdefault("match", {})
        self.spent, self.calls = 0.0, 0

    def save(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")

    def _ask(self, prompt: str, system: str) -> dict:
        if self.spent >= self.budget:
            raise RuntimeError(f"classifier budget ${self.budget:.2f} reached")
        last: Exception | None = None
        for _ in range(2):
            reply = self.runner(prompt, system)
            self.spent += reply.get("cost", 0.0)
            self.calls += 1
            try:
                return parse_json_reply(reply["text"])
            except ValueError as error:
                last = error
        raise RuntimeError(f"classifier reply was not JSON twice: {last}")

    def label(self, findings: list[dict]) -> dict[str, dict]:
        """sha -> {"label", "why"} for every finding."""
        key = prompt_sha(LABEL_PROMPT)[:16]
        todo: dict[str, dict] = {}
        for item in findings:
            if f"{key}:{item['sha']}" not in self.cache["labels"]:
                todo.setdefault(item["sha"], item)
        pending = list(todo.values())
        for start in range(0, len(pending), self.label_batch):
            batch = pending[start:start + self.label_batch]
            ids = {f"f{n}": item["sha"] for n, item in enumerate(batch, start=1)}
            payload = [{"id": short, "section": todo[sha].get("section"), "severity": todo[sha].get("severity"),
                        "text": todo[sha]["text"][:1800]} for short, sha in ids.items()]
            reply = self._ask(json.dumps(payload, ensure_ascii=False), LABEL_PROMPT)
            for row in reply.get("labels") or []:
                sha = ids.get(str(row.get("id")))
                label = str(row.get("label") or "").strip().lower()
                if sha and label in LABELS:
                    self.cache["labels"][f"{key}:{sha}"] = {"label": label, "why": str(row.get("why") or "")[:300]}
            self.save()
        return {item["sha"]: self.cache["labels"].get(f"{key}:{item['sha']}", {"label": None, "why": "unlabelled"})
                for item in findings}

    def match(self, prd: str, checks: list[dict], findings: list[dict], judge: set[str]) -> dict:
        """{"kinds": {check: kind}, "matches": {check: [sha]}, "prd": {sha: verdict}} for one run."""
        unique = list({item["sha"]: item for item in findings}.values())
        out = {"kinds": {}, "matches": {check["id"]: [] for check in checks}, "prd": {}}
        if not checks and not judge:
            return out
        chunks = [unique[start:start + self.match_batch] for start in range(0, len(unique), self.match_batch)] or [[]]
        for number, chunk in enumerate(chunks):
            ids = {f"r{n}": item["sha"] for n, item in enumerate(chunk, start=1)}
            body = {
                "failed_checks": [{"id": check["id"], "rule": check["rule"] if number == 0 else "known",
                                   "spec": check.get("spec", "")[:1500],
                                   "message": " | ".join(check.get("messages") or [])[:600]} for check in checks],
                "findings": [{"id": short, "text": next(i for i in chunk if i["sha"] == sha)["text"][:1500]}
                             for short, sha in ids.items()],
                "judge_prd": [short for short, sha in ids.items() if sha in judge],
            }
            prompt = f"<prd_and_contract>\n{prd}\n</prd_and_contract>\n\n{json.dumps(body, ensure_ascii=False)}"
            key = f"{prompt_sha(MATCH_PROMPT)[:16]}:{prompt_sha(prompt)}"
            reply = self.cache["match"].get(key)
            if reply is None:
                reply = self._ask(prompt, MATCH_PROMPT)
                self.cache["match"][key] = reply
                self.save()
            for row in reply.get("checks") or []:
                check_id = str(row.get("id"))
                if check_id not in out["matches"]:
                    continue
                kind = str(row.get("kind") or "").lower()
                if number == 0 and kind in KINDS:
                    out["kinds"][check_id] = kind
                out["matches"][check_id] += [ids[f] for f in row.get("findings") or [] if f in ids]
            for row in reply.get("prd") or []:
                if row.get("id") in ids:
                    out["prd"][ids[row["id"]]] = str(row.get("verdict") or "").lower()
        return out


# ---------------------------------------------------------------- per run

def _run_meta(run: Path) -> dict:
    result = _read_json(run / "result.json") or {}
    arm = _read_json(run / "arm.json") or {}
    return {"run": run.name, "path": str(run), "arm": result.get("arm") or arm.get("arm"),
            "case": result.get("case") or arm.get("case"), "layer": result.get("layer") or arm.get("layer"),
            "declaration": result.get("declaration"), "has_result": bool(result)}


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def analyze_run(run: Path, specs: dict[str, str], prd: str, classifier: Classifier | None,
                total_checks: int | None = None, skip_phase: re.Pattern | None = None) -> dict:
    meta = _run_meta(run)
    rounds = load_rounds(run, skip_phase)
    findings = flat_findings(rounds)
    hidden = hidden_failures(run)
    for check in hidden["failed"]:
        check["spec"] = spec_for(check["id"], specs)
    labels = classifier.label(findings) if classifier and findings else {}
    for item in findings:
        item.update(labels.get(item["sha"]) or {"label": None, "why": None})
    judge = {item["sha"] for item in findings if item.get("label") in ("bug", "incomplete")}
    unclear = any(check["rule"] == "unclear" for check in hidden["failed"])
    needs_call = classifier is not None and bool(judge or unclear or (findings and hidden["failed"]))
    crossed = classifier.match(prd, hidden["failed"], findings, judge) if needs_call else \
        {"kinds": {}, "matches": {}, "prd": {}}
    for check in hidden["failed"]:
        check["kind"] = check["rule"] if check["rule"] != "unclear" else crossed["kinds"].get(check["id"], "unclear")
        matched = set(crossed["matches"].get(check["id"]) or [])
        check["matched_any"] = sorted(matched)
        check["matched_final"] = sorted({i["sha"] for i in findings if i["final"] and i["sha"] in matched})
    pointed = {sha for check in hidden["failed"] for sha in check["matched_any"]}
    for item in findings:
        item["hidden_match"] = item["sha"] in pointed
        item["prd"] = crossed["prd"].get(item["sha"])
    return {**meta, **_summarize_run(rounds, findings, hidden, total_checks), "rounds": [
        {key: entry[key] for key in ("run_id", "phase", "seq", "order", "final", "verdict", "source")}
        | {"earlier_rounds_lost": entry.get("earlier_rounds_lost", False)} for entry in rounds],
        "findings": findings, "hidden": hidden}


def _distribution(items: Iterable[dict]) -> dict:
    counts = Counter(item.get("label") or "unlabelled" for item in items)
    return {label: counts.get(label, 0) for label in (*LABELS, "unlabelled") if counts.get(label) or label in LABELS}


def _summarize_run(rounds: list[dict], findings: list[dict], hidden: dict, total_checks: int | None) -> dict:
    final = [item for item in findings if item["final"]]
    failed = hidden["failed"]
    kinds = Counter(check.get("kind", check["rule"]) for check in failed)
    recall = {}
    for scope in ("final", "any"):
        for kind in KINDS:
            pool = [check for check in failed if check.get("kind") == kind]
            hit = [check for check in pool if check[f"matched_{scope}"]]
            recall[f"{scope}_{kind}"] = {"hit": len(hit), "of": len(pool), "rate": _ratio(len(hit), len(pool))}
    claimed_final = [item for item in final if item.get("label") in ("bug", "incomplete")]
    claimed_all = [item for item in findings if item.get("label") in ("bug", "incomplete")]
    precision = {
        "final_claims": len(claimed_final),
        "final_hidden": _ratio(sum(item["hidden_match"] for item in claimed_final), len(claimed_final)),
        "final_hidden_or_prd": _ratio(sum(item["hidden_match"] or item.get("prd") == "supported"
                                          for item in claimed_final), len(claimed_final)),
        "all_claims": len(claimed_all),
        "all_prd": _ratio(sum(item.get("prd") == "supported" for item in claimed_all), len(claimed_all)),
    }
    for label in ("bug", "incomplete"):
        subset = [item for item in claimed_final if item["label"] == label]
        precision[f"final_{label}_hidden"] = _ratio(sum(item["hidden_match"] for item in subset), len(subset))
    per_round = defaultdict(list)
    for item in findings:
        per_round[(item["order"], item["phase"], item["round"])].append(item)
    missing, wrong, unclear = kinds.get("missing", 0), kinds.get("wrong", 0), kinds.get("unclear", 0)
    total = total_checks or None
    declared_complete = hidden.get("declaration") == "declared-complete"
    grade = None
    if declared_complete:
        grade = "false-complete-missing" if missing else "false-complete-bug" if failed else "complete-true"
    return {
        "S": hidden["S"], "G": hidden["G"], "no_build": hidden["no_build"],
        "review_rounds": len(rounds), "findings_total": len(findings),
        "findings_final": _distribution(final), "findings_all": _distribution(findings),
        "findings_by_round": [{"order": order, "phase": phase, "round": number, **_distribution(items)}
                              for (order, phase, number), items in sorted(per_round.items())],
        "failed_checks": len(failed), "missing": missing, "wrong": wrong, "unclear": unclear,
        "rule_unclear": sum(check["rule"] == "unclear" for check in failed),
        "total_checks": total, "complete": missing == 0,
        "missing_ratio": _ratio(missing, total) if total else None,
        "wrong_ratio_of_present": _ratio(wrong, total - missing) if total else None,
        "false_completion": grade, "recall": recall, "precision": precision,
    }


# ---------------------------------------------------------------- per arm and markdown

SUM_KEYS = ("findings_total", "failed_checks", "missing", "wrong", "unclear", "review_rounds")


def summarize_arms(rows: list[dict]) -> dict:
    arms: dict[str, dict] = {}
    for row in rows:
        arm = arms.setdefault(str(row.get("arm")), {"runs": 0, "no_build": 0, **{key: 0 for key in SUM_KEYS},
                                                    "findings_final": Counter(), "findings_all": Counter(),
                                                    "recall": defaultdict(lambda: [0, 0]),
                                                    "precision": defaultdict(lambda: [0, 0]),
                                                    "false_completion": Counter(), "complete": 0,
                                                    "total_checks": 0})
        arm["runs"] += 1
        arm["no_build"] += bool(row.get("no_build"))
        for key in SUM_KEYS:  # a run with no build fails every check; it is counted apart, not as missing checks
            if not (row.get("no_build") and key in ("failed_checks", "missing", "wrong", "unclear")):
                arm[key] += row.get(key) or 0
        arm["findings_final"].update(row.get("findings_final") or {})
        arm["findings_all"].update(row.get("findings_all") or {})
        reviewed = row.get("findings_total") and not row.get("no_build")
        for key, value in (row.get("recall") or {}).items() if reviewed else ():
            arm["recall"][key][0] += value["hit"]
            arm["recall"][key][1] += value["of"]
        final = [item for item in row.get("findings", []) if item["final"] and item.get("label") in ("bug", "incomplete")]
        arm["precision"]["final_hidden"][0] += sum(item["hidden_match"] for item in final)
        arm["precision"]["final_hidden_or_prd"][0] += sum(item["hidden_match"] or item.get("prd") == "supported"
                                                          for item in final)
        arm["precision"]["final_hidden"][1] += len(final)
        arm["precision"]["final_hidden_or_prd"][1] += len(final)
        if row.get("false_completion"):
            arm["false_completion"][row["false_completion"]] += 1
        arm["complete"] += bool(row.get("complete"))
        arm["total_checks"] += 0 if row.get("no_build") else row.get("total_checks") or 0
    out = {}
    for name, arm in sorted(arms.items()):
        out[name] = {
            **{key: arm[key] for key in ("runs", "no_build", "complete", *SUM_KEYS)},
            "findings_final": dict(arm["findings_final"]), "findings_all": dict(arm["findings_all"]),
            "recall": {key: {"hit": hit, "of": of, "rate": _ratio(hit, of)} for key, (hit, of) in arm["recall"].items()},
            "precision": {key: {"hit": hit, "of": of, "rate": _ratio(hit, of)}
                          for key, (hit, of) in arm["precision"].items()},
            "false_completion": dict(arm["false_completion"]),
            "missing_ratio": _ratio(arm["missing"], arm["total_checks"]),
            "wrong_ratio_of_present": _ratio(arm["wrong"], arm["total_checks"] - arm["missing"]),
        }
    return out


def _dist(values: dict) -> str:
    return "/".join(str(values.get(label, 0)) for label in LABELS)


def _frac(value: dict) -> str:
    return f"{value['hit']}/{value['of']}" if value and value.get("of") else "-"


def markdown(rows: list[dict], arms: dict) -> str:
    lines = ["| run | arm | decl | S | G | rounds | final b/i/d/p/o | all b/i/d/p/o | failed | missing | wrong | "
             "grade | recall final m | recall final w | recall any m | recall any w | prec final hidden | "
             "prec final +PRD |", "|" + "---|" * 18]
    for row in rows:
        recall, precision = row["recall"], row["precision"]
        p_hidden = precision.get("final_hidden")
        p_prd = precision.get("final_hidden_or_prd")
        lines.append("| " + " | ".join(str(cell) for cell in (
            row["run"], row["arm"], (row.get("declaration") or "-").replace("declared-", ""), row.get("S"),
            row.get("G"), row["review_rounds"], _dist(row["findings_final"]) if row["findings_total"] else "-",
            _dist(row["findings_all"]) if row["findings_total"] else "-", row["failed_checks"],
            row["missing"] if not row["no_build"] else f"{row['missing']} (no build)", row["wrong"],
            row.get("false_completion") or "-",
            *(_frac(recall[key]) if row["findings_total"] else "-"
              for key in ("final_missing", "final_wrong", "any_missing", "any_wrong")),
            "-" if p_hidden is None else f"{p_hidden:.2f} (n={precision['final_claims']})",
            "-" if p_prd is None else f"{p_prd:.2f}")) + " |")
    lines += ["", "| arm | runs | no build | failed | missing | wrong | missing ratio | wrong ratio of present | "
                  "false completion | final b/i/d/p/o | recall final m | recall final w | prec final hidden |",
              "|" + "---|" * 13]
    for name, arm in arms.items():
        lines.append("| " + " | ".join(str(cell) for cell in (
            name, arm["runs"], arm["no_build"], arm["failed_checks"], arm["missing"], arm["wrong"],
            arm["missing_ratio"], arm["wrong_ratio_of_present"],
            ", ".join(f"{k} {v}" for k, v in sorted(arm["false_completion"].items())) or "-",
            _dist(arm["findings_final"]) if arm["findings_total"] else "-",
            _frac(arm["recall"].get("final_missing")), _frac(arm["recall"].get("final_wrong")),
            _frac(arm["precision"].get("final_hidden")))) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def _read_texts(paths: Iterable[str]) -> str:
    return "\n\n".join(f"<file name=\"{Path(path).name}\">\n{Path(path).read_text(encoding='utf-8')}\n</file>"
                       for path in paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract", help="print parsed findings and hidden failures, no classifier")
    extract.add_argument("runs", nargs="+")
    analyze = sub.add_parser("analyze", help="label, match and write one JSON per run")
    analyze.add_argument("runs", nargs="+")
    analyze.add_argument("--checks", action="append", default=[], help="check definitions (.json or .py)")
    analyze.add_argument("--prd", action="append", default=[], help="PRD/contract text for matching")
    analyze.add_argument("--cache", type=Path, required=True)
    analyze.add_argument("--out", type=Path, required=True)
    analyze.add_argument("--budget", type=float, default=10.0)
    analyze.add_argument("--skip-phase", default="plan", help="regex of review phases to leave out")
    report = sub.add_parser("report", help="merge per-run JSON into a per-arm summary and a markdown table")
    report.add_argument("dirs", nargs="+", type=Path)
    report.add_argument("--markdown", type=Path)
    report.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    skip = re.compile(getattr(args, "skip_phase", "plan")) if getattr(args, "skip_phase", "") else None

    if args.command == "extract":
        for raw in args.runs:
            run = Path(raw)
            rounds = load_rounds(run, skip)
            print(json.dumps({"run": run.name, "rounds": len(rounds), "findings": flat_findings(rounds),
                              "hidden": hidden_failures(run)}, ensure_ascii=False))
        return 0
    if args.command == "analyze":
        specs = load_check_specs(args.checks)
        total = len({base_id(key) for key in specs}) or None
        classifier = Classifier(args.cache, budget=args.budget)
        prd = _read_texts(args.prd)
        args.out.mkdir(parents=True, exist_ok=True)
        for raw in args.runs:
            row = analyze_run(Path(raw), specs, prd, classifier, total, skip)
            (args.out / f"{row['run']}.json").write_text(json.dumps(row, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
            print(f"{row['run']}: findings {row['findings_total']} failed {row['failed_checks']} "
                  f"spent ${classifier.spent:.2f}", file=sys.stderr)
        meta = {"label_prompt_sha256": prompt_sha(LABEL_PROMPT), "match_prompt_sha256": prompt_sha(MATCH_PROMPT),
                "model": MODEL, "effort": EFFORT, "calls": classifier.calls, "spent_usd": round(classifier.spent, 4)}
        (args.out / "_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        print(json.dumps(meta))
        return 0
    rows = [json.loads(path.read_text(encoding="utf-8")) for directory in args.dirs
            for path in sorted(directory.glob("*.json")) if not path.name.startswith("_")]
    arms = summarize_arms(rows)
    summary = {"arms": arms, "runs": [{key: value for key, value in row.items() if key not in ("findings", "hidden",
                                                                                              "rounds")}
                                      for row in rows]}
    if args.json:
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    table = markdown(rows, arms)
    if args.markdown:
        args.markdown.write_text(table, encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
