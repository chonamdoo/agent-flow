from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    """Load a measurement helper directly from its repository path."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(monkeypatch):
    """Load the T6 measurement harness with its evaluation imports available."""
    monkeypatch.syspath_prepend(str(ROOT / "evals"))
    return load_module("t6_measurement", ROOT / "tools/t6-context/run.py")


def test_accounting_counts_utf8_deliveries_not_selected_paths():
    """Count delivered UTF-8 bodies rather than merely selected document paths."""
    renderer = load_module("t6_render", ROOT / "tools/t6-context/render.py")
    body = "규칙: 경계를 지킨다.\n".encode()
    prompt = b"header\n" + body + b"footer\n"
    result = renderer.delivered_bytes(prompt, [body, body])
    assert result["raw_input_bytes"] == len(prompt)
    assert result["delivered_normative_bytes"] == len(body)
    assert result["duplicated_bytes"] == 0
    duplicate = renderer.delivered_bytes(prompt + body, [body, body])
    assert duplicate["delivered_normative_bytes"] == 2 * len(body)
    assert duplicate["duplicated_bytes"] == len(body)
    missing = renderer.delivered_bytes(prompt, [body + b"changed"])
    assert missing["delivered_normative_bytes"] == 0


def test_provider_usage_missing_is_not_zero(harness):
    """Represent missing provider usage as unavailable instead of measured zero."""
    missing = harness.usage_from("codex", [{"type": "turn.completed"}])
    assert missing["input_tokens"] == "unavailable"
    measured = harness.usage_from("codex", [{"type": "turn.completed", "usage": {
        "input_tokens": 0, "cached_input_tokens": 0,
    }}])
    assert measured["input_tokens"] == 0
    assert measured["cached_tokens"] == 0
    assert measured["uncached_tokens"] == 0
    assert measured["provider_call_count"] == "unavailable"


def test_claude_aggregate_avoids_assistant_double_counting(harness):
    """Avoid double-counting repeated Claude assistant usage events."""
    events = [
        {"type": "assistant", "message": {"id": "one", "usage": {"input_tokens": 4}}},
        {"type": "assistant", "message": {"id": "one", "usage": {"input_tokens": 4}}},
        {"type": "result", "usage": {"input_tokens": 4, "cache_read_input_tokens": 10,
                                     "cache_creation_input_tokens": 3}},
    ]
    result = harness.usage_from("claude", events)
    assert result["input_tokens"] == 17
    assert result["cached_tokens"] == 10
    assert result["uncached_tokens"] == 7
    assert result["cache_creation_tokens"] == 3
    assert result["provider_call_count"] == 1


def test_partial_or_inconsistent_usage_cannot_invent_uncached_tokens(harness):
    """Refuse to derive uncached tokens from partial or inconsistent usage."""
    partial = harness.usage_from("claude", [{"type": "result", "usage": {"input_tokens": 4}}])
    assert partial["input_tokens"] == "unavailable"
    assert partial["uncached_tokens"] == "unavailable"
    bad = harness.usage_from("codex", [{"type": "turn.completed", "usage": {
        "input_tokens": 2, "cached_input_tokens": 4,
    }}])
    assert bad["uncached_tokens"] == "unavailable"


def test_scoring_keeps_failure_categories_distinct(harness):
    """Keep execution, fixture, response, and oracle failures distinguishable."""
    from skill_tasks import score_review

    case = {"expected_verdict": "request-changes", "expected_findings": [
        {"file": "change.py", "start_line": 3, "end_line": 4}],
        "required_references": [], "forbidden_skills": []}
    response = {"verdict": "request-changes", "findings": [
        {"file": "change.py", "line": 3, "reason": "The domain policy imports the runtime container."}]}
    execution = {"call_count": 1, "return_code": 0, "timed_out": False}
    def score(answer=response, run=execution, unchanged=True):
        """Score one synthetic response under explicit execution and fixture state."""
        return harness.score(case, answer, run, fixture_unchanged=unchanged, scorer=score_review)

    assert score()["status"] == "matched-oracle"
    assert score(run={**execution, "return_code": 1})["status"] == "provider-failure"
    assert score(run={**execution, "provider_reported_error": True})["status"] == "provider-failure"
    assert score(run={**execution, "timed_out": True})["status"] == "timeout"
    assert score(run={**execution, "call_count": 0})["status"] == "not-executed"
    assert score(unchanged=False)["status"] == "fixture-mutated"
    assert score(answer={"verdict": []})["status"] == "malformed-response"
    mismatch = score(answer={"verdict": "approve", "findings": []})
    assert mismatch["status"] == "oracle-mismatch"
    assert mismatch["passed"] is False
    wrong_location = score(answer={"verdict": "request-changes", "findings": [
        {"file": "change.py", "line": 1, "reason": "Prefer another name."}]})
    assert wrong_location["defects_found"] is False
    assert wrong_location["no_false_positives"] is False


def test_equal_failed_verdicts_are_not_parity_or_savings(harness):
    """Do not claim parity or savings when both equal verdicts are failed measurements."""
    failed = {"score": {"status": "provider-failure"}, "response": {"verdict": "approve"}}
    result = harness.compare(failed, failed)
    assert result["semantic_parity"] == "unavailable"
    assert result["byte_delta_after_minus_before"] == "unavailable"
    before = {"score": {"status": "oracle-mismatch", "passed": False},
              "response": {"verdict": "approve"}, "input": {"raw_input_bytes": 100}}
    after = {**before, "input": {"raw_input_bytes": 90}}
    result = harness.compare(before, after)
    assert result["semantic_parity"] is True
    assert result["both_match_oracle"] is False
    assert result["byte_delta_after_minus_before"] == -10


def test_modified_fixture_cannot_be_silently_rescored(harness, tmp_path):
    """Reject silent rescoring after an immutable fixture changes."""
    source = ROOT / "tools/t6-context/fixtures"
    (tmp_path / "manifest.json").write_bytes((source / "manifest.json").read_bytes())
    content = json.loads((source / "cases.json").read_text(encoding="utf-8"))
    content[0]["expected_verdict"] = "request-changes"
    (tmp_path / "cases.json").write_text(json.dumps(content))
    with pytest.raises(ValueError, match="immutable fixture digest mismatch"):
        harness.load_cases(tmp_path)


def test_unsafe_archive_never_writes_outside_snapshot(harness, tmp_path):
    """Prevent unsafe baseline members from escaping the snapshot directory."""
    import io
    import tarfile

    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as bundle:
        item = tarfile.TarInfo("../escaped")
        item.size = 1
        bundle.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe baseline member"):
        harness.extract_baseline(archive, tmp_path / "snapshot")
    assert not (tmp_path / "escaped").exists()


def test_standalone_renderer_rejects_escape_before_materialization(tmp_path):
    """Reject fixture path escape before the standalone renderer creates files."""
    renderer = load_module("t6_render", ROOT / "tools/t6-context/render.py")
    project = tmp_path / "project"
    case = {"files": {"safe.py": "pass\n", "../escaped.py": "pass\n"}}
    with pytest.raises(ValueError):
        renderer.render(ROOT, project, case, "codex", tmp_path / "output")
    assert not project.exists()
    assert not (tmp_path / "escaped.py").exists()


@pytest.mark.parametrize("reference", [
    "../outside.md", "/tmp/t6-outside.md", "guide\\..\\outside.md", "C:/outside.md",
])
def test_standalone_renderer_rejects_reference_escape_before_imports(tmp_path, reference):
    """Reject reference escape before the renderer imports project code."""
    renderer = load_module("t6_render", ROOT / "tools/t6-context/render.py")
    project = tmp_path / "project"
    case = {"files": {"safe.py": "pass\n"}, "required_references": [reference]}
    with pytest.raises(ValueError, match="unsafe fixture reference"):
        renderer.render(tmp_path / "nonexistent-source", project, case, "codex", tmp_path / "output")
    assert not project.exists()


@pytest.mark.parametrize("fields", [
    {"references": ["../outside.md"]},
    {"required_references": "guide/references/rules.md"},
    {"required_references": [None]},
])
def test_fixture_reference_schema_is_explicit(harness, tmp_path, fields):
    """Require fixture references to use the explicit supported schema."""
    case = {"id": "invalid-reference-schema", "files": {"safe.py": "pass\n"}, **fields}
    cases = tmp_path / "cases.json"
    harness.save(cases, [case])
    harness.save(tmp_path / "manifest.json", {"cases_sha256": harness.digest(cases.read_bytes())})
    with pytest.raises(ValueError, match="reference"):
        harness.load_cases(tmp_path)
    renderer = load_module("t6_render", ROOT / "tools/t6-context/render.py")
    with pytest.raises(ValueError, match="reference"):
        renderer.render(ROOT, tmp_path / "project", case, "codex", tmp_path / "output")
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("escape_skills_only", [False, True])
def test_reference_symlink_escape_is_rejected_before_materialization(tmp_path, escape_skills_only):
    """Reject reference symlinks that escape before materialization starts."""
    renderer = load_module("t6_render", ROOT / "tools/t6-context/render.py")
    source = tmp_path / "source"
    skills = source / "skills"
    skills.mkdir(parents=True)
    target = (source if escape_skills_only else tmp_path) / "outside.md"
    target.write_text("This body must not enter the normative corpus.\n", encoding="utf-8")
    (skills / "linked.md").symlink_to(target)
    case = {"files": {"safe.py": "pass\n"}, "required_references": ["linked.md"]}
    with pytest.raises(ValueError, match="escapes"):
        renderer.render(source, tmp_path / "project", case, "codex", tmp_path / "output")
    assert not (tmp_path / "project").exists()


@pytest.fixture
def local_reference_case(tmp_path):
    """Create a local-contract fixture with one declared normative reference."""
    cases = json.loads((ROOT / "tools/t6-context/fixtures/cases.json").read_text(encoding="utf-8"))
    case = next(case for case in cases if case["id"] == "local-framework-allowed")
    reference = "architecture/references/ownership.md"
    case["required_references"] = [reference]
    output = tmp_path / "output"
    output.mkdir()
    return case, reference, output


def render_reference_case(tmp_path, case, output):
    """Render a reference fixture into an isolated output directory."""
    case_file = tmp_path / "case.json"
    case_file.write_text(json.dumps(case), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/t6-context/render.py"), str(ROOT),
         str(tmp_path / "project"), str(case_file), "codex", str(output)],
        cwd=tmp_path, capture_output=True, timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_declared_reference_contributes_selected_normative_body(tmp_path, local_reference_case):
    """Include a declared reference body in selected normative input accounting."""
    case, reference, output = local_reference_case
    result = render_reference_case(tmp_path, case, output)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    capture = json.loads((output / "render.json").read_text(encoding="utf-8"))
    document = next(item for item in capture["documents"] if item["path"] == "skills/" + reference)
    body = case["files"]["skills/" + reference].encode("utf-8")
    assert (output / document["captured_body"]).read_bytes() == body
    assert body in (output / "semantic.prompt.txt").read_bytes()


def test_reference_assertion_cannot_select_an_unresolved_document(tmp_path, local_reference_case):
    """Prevent fixture assertions from selecting unresolved documents."""
    case, reference, output = local_reference_case
    unselected = "architecture/references/unselected.md"
    case["files"]["skills/" + unselected] = "An unselected rule must not become normative.\n"
    case["required_references"] = [reference, unselected]
    result = render_reference_case(tmp_path, case, output)
    assert result.returncode != 0
    assert b"required reference missing from selected normative capture" in result.stderr
    assert not (output / "semantic.prompt.txt").exists()


def test_fixture_and_response_unicode_survive_ascii_locale(harness, tmp_path):
    """Preserve fixture and response Unicode under an ASCII process locale."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    cases = fixtures / "cases.json"
    harness.save(cases, [{"id": "unicode", "task": "규칙", "files": {"source.py": "pass\n"}}])
    harness.save(fixtures / "manifest.json", {
        "cases_sha256": harness.digest(cases.read_bytes()), "description": "규칙",
    })
    response = tmp_path / "response.json"
    harness.save(response, {"verdict": "request-changes", "findings": [
        {"file": "source.py", "line": 1, "reason": "규칙"},
    ]})
    script = """
import importlib.util
import json
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("measurement_utf8", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cases = module.load_cases(Path(sys.argv[2]))
response = module.response_from("codex", [], Path(sys.argv[3]))
print(json.dumps([cases[0]["task"], response["findings"][0]["reason"]]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "tools/t6-context/run.py"),
         str(fixtures), str(response)],
        cwd=tmp_path, capture_output=True, timeout=30,
        env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0",
             "PYTHONCOERCECLOCALE": "0", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout) == ["규칙", "규칙"]


@pytest.mark.parametrize("identifier", ["../outside", "/tmp/t6-outside"])
def test_unsafe_fixture_id_is_rejected_before_output_creation(harness, tmp_path, identifier):
    """Reject unsafe fixture identifiers before creating measurement output."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    cases = fixtures / "cases.json"
    harness.save(cases, [{"id": identifier, "files": {"source.py": "pass\n"}}])
    harness.save(fixtures / "manifest.json", {"cases_sha256": harness.digest(cases.read_bytes())})
    output = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/t6-context/run.py"),
         "--fixtures", str(fixtures), "--baseline-archive", str(tmp_path / "unused.tar"),
         "--output", str(output), "--provider", "codex", "--render-only"],
        cwd=tmp_path, capture_output=True, timeout=30,
    )
    assert result.returncode != 0
    assert b"unsafe fixture id" in result.stderr
    assert not output.exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("symlink_escape", [False, True])
def test_runner_rejects_reference_escape_before_snapshot_creation(harness, tmp_path, symlink_escape):
    """Reject reference escape before the measurement runner snapshots sources."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    source = tmp_path / "source"
    skills = source / "skills"
    skills.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("A fixture cannot capture this external body.\n", encoding="utf-8")
    (skills / "linked.md").symlink_to(outside)
    cases = fixtures / "cases.json"
    harness.save(cases, [{
        "id": "reference-escape", "files": {"source.py": "pass\n"},
        "required_references": ["linked.md" if symlink_escape else "../outside.md"],
    }])
    harness.save(fixtures / "manifest.json", {"cases_sha256": harness.digest(cases.read_bytes())})
    output = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/t6-context/run.py"),
         "--fixtures", str(fixtures), "--baseline-archive", str(tmp_path / "unused.tar"),
         "--after", str(source), "--output", str(output), "--provider", "codex", "--render-only"],
        cwd=tmp_path, capture_output=True, timeout=30,
    )
    assert result.returncode != 0
    error = b"normative document escapes project" if symlink_escape else b"unsafe fixture reference"
    assert error in result.stderr
    assert not output.exists()
