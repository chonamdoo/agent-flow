import importlib.util
import json
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "evals/phase_eval_context_reads.py"
spec = importlib.util.spec_from_file_location("context_reads", PATH)
context_reads = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_reads)


def evidence(tmp_path, *items):
    path = tmp_path / "stdout.raw"
    events = [{"type": "item.completed", "item": item} for item in items]
    events.append({"type": "turn.completed"})
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    return path


def command(text, output, exit_code=0):
    return {"type": "command_execution", "command": text,
            "aggregated_output": output, "exit_code": exit_code}


def index(tmp_path, value=None):
    path = tmp_path / "index.json"
    value = value if value is not None else {
        "selection": {"selected_skills": ["tdd", "other"]},
        "skills": [{"name": "tdd"}, {"name": "other"}], "links": []}
    path.write_text(json.dumps(value))
    return path


def test_catalog_output_is_detected_inside_mixed_command_output(tmp_path):
    catalog = {"selection": {"selected_skills": ["tdd", "other"]},
               "skills": [{"name": "tdd"}, {"name": "other"}], "links": []}
    output = "profile loaded\n" + json.dumps(catalog) + "\nstatus clean\n"
    result = context_reads.context_outputs(evidence(
        tmp_path, command("cat profile.yaml .agent-flow/skills/index.json; git status", output)),
        catalog=index(tmp_path, catalog))
    assert result["skill_catalog_outputs"] == 1
    assert result["command_output_bytes"] == len(output.encode())


def test_path_listing_and_selected_entry_are_not_full_catalog_reads(tmp_path):
    result = context_reads.context_outputs(evidence(tmp_path,
        command("rg --files .agent-flow", ".agent-flow/skills/index.json\n"),
        command("jq '.skills[] | select(.name == \"tdd\")' .agent-flow/skills/index.json",
                json.dumps({"name": "tdd", "path": "skills/tdd/SKILL.md"})),
        command("/bin/cat .agent-flow/skills/tdd/SKILL.md", "Use red/green tests.")),
        catalog=index(tmp_path))
    assert result["broad_context_outputs"] == []
    assert result["command_count"] == 3


def test_whole_workflow_emitted_by_failed_grep_is_still_observed(tmp_path):
    output = 'meta.json:16: "source_text": "id: full-feature\\nphases:\\n  - id: green"\n'
    result = context_reads.context_outputs(evidence(tmp_path,
        command("rg -n 'green' .agent-flow/runs/run/*.json missing-file", output, 2)),
        catalog=index(tmp_path))
    assert result["workflow_source_outputs"] == 1
    assert result["broad_context_outputs"][0]["exit_code"] == 2


def test_selected_phase_metadata_is_not_a_whole_workflow_dump(tmp_path):
    result = context_reads.context_outputs(evidence(tmp_path,
        command("jq '.phase' meta.json", '{"id":"green","required":["tdd"]}'),
        {"type": "agent_message", "text": 'I read skills/index.json and "source_text": phases:'}),
        catalog=index(tmp_path))
    assert result["broad_context_outputs"] == []
    assert result["command_count"] == 1


def test_filtered_catalog_shape_does_not_count_as_the_full_installed_index(tmp_path):
    whole = {"selection": {"selected_skills": ["tdd", "other"]},
             "skills": [{"name": "tdd"}, {"name": "other"}], "links": []}
    index = tmp_path / "index.json"
    index.write_text(json.dumps(whole))
    filtered = {"selection": {"selected_skills": ["tdd"]},
                "skills": [{"name": "tdd"}], "links": []}
    result = context_reads.context_outputs(evidence(tmp_path,
        command("jq filter .agent-flow/skills/index.json", json.dumps(filtered))), catalog=index)
    assert result["broad_context_outputs"] == []


def test_catalog_emitted_through_a_wildcard_is_still_detected(tmp_path):
    catalog = {"selection": {"selected_skills": ["tdd", "other"]},
               "skills": [{"name": "tdd"}, {"name": "other"}], "links": []}
    result = context_reads.context_outputs(evidence(tmp_path,
        command("cat .agent-flow/skills/*.json", json.dumps(catalog))),
        catalog=index(tmp_path, catalog))
    assert result["skill_catalog_outputs"] == 1


def test_cli_reports_broad_context_as_a_failure(tmp_path, capsys):
    output = '{"source_text":"id: full-feature\\nphases:\\n  - id: green"}'
    path = evidence(tmp_path, command("rg green meta.json", output, 2))
    assert context_reads.main([str(path), "--catalog", str(index(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out)["workflow_source_outputs"] == 1


def test_cli_accepts_required_skill_output(tmp_path, capsys):
    path = evidence(tmp_path, command("/bin/cat skills/tdd/SKILL.md", "Use red/green tests."))
    assert context_reads.main([str(path), "--catalog", str(index(tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out)["broad_context_outputs"] == []


def test_malformed_native_evidence_is_not_reported_as_zero_reads(tmp_path):
    path = tmp_path / "stdout.raw"
    path.write_text("truncated native event\n")
    with pytest.raises(json.JSONDecodeError):
        context_reads.main([str(path), "--catalog", str(index(tmp_path))])


@pytest.mark.parametrize("content", ["", '{"type":"thread.started"}\n'])
def test_cli_rejects_empty_or_incomplete_native_stream(tmp_path, content):
    path = tmp_path / "stdout.raw"
    path.write_text(content)
    with pytest.raises(SystemExit) as error:
        context_reads.main([str(path), "--catalog", str(index(tmp_path))])
    assert error.value.code == 2


def test_cli_requires_reference_catalog_for_filtered_json(tmp_path):
    filtered = {"selection": {"selected_skills": ["tdd"]},
                "skills": [{"name": "tdd"}], "links": []}
    path = evidence(tmp_path, command("jq filter .agent-flow/skills/index.json", json.dumps(filtered)))
    with pytest.raises(SystemExit) as error:
        context_reads.main([str(path)])
    assert error.value.code == 2
    assert context_reads.main([str(path), "--catalog", str(index(tmp_path))]) == 0
