import importlib.util
import json
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "evals/phase_eval_context_reads.py"
spec = importlib.util.spec_from_file_location("context_reads", PATH)
context_reads = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_reads)

CATALOG = {"selection": {"selected_skills": ["tdd", "other"]},
           "skills": [{"name": "tdd"}, {"name": "other"}], "links": []}


def evidence(tmp_path, *items):
    path = tmp_path / "stdout.raw"
    events = [{"type": "item.completed", "item": item} for item in items]
    events.append({"type": "turn.completed"})
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    return path


def installed(tmp_path, catalog=CATALOG):
    path = tmp_path / "index.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return path


def command(text, output, exit_code=0):
    return {"type": "command_execution", "command": text,
            "aggregated_output": output, "exit_code": exit_code}


def test_catalog_output_is_detected_inside_mixed_command_output(tmp_path):
    output = "profile loaded\n" + json.dumps(CATALOG) + "\nstatus clean\n"
    result = context_reads.context_outputs(evidence(
        tmp_path, command("cat profile.yaml .agent-flow/skills/index.json; git status", output)),
        catalog=installed(tmp_path))
    assert result["skill_catalog_outputs"] == 1
    assert result["command_output_bytes"] == len(output.encode())


def test_unfiltered_install_catalog_is_detected(tmp_path):
    catalog = {"selection": {"mode": "all", "profiles": [], "explicit_skills": [],
                             "selected_skills": "all"},
               "skills": [{"name": "tdd"}, {"name": "other"}], "links": []}
    result = context_reads.context_outputs(evidence(
        tmp_path, command("cat .agent-flow/skills/index.json", json.dumps(catalog, indent=2))),
        catalog=installed(tmp_path, catalog))
    assert result["skill_catalog_outputs"] == 1


def test_path_listing_and_selected_entry_are_not_full_catalog_reads(tmp_path):
    result = context_reads.context_outputs(evidence(tmp_path,
        command("rg --files .agent-flow", ".agent-flow/skills/index.json\n"),
        command("jq '.skills[] | select(.name == \"tdd\")' .agent-flow/skills/index.json",
                json.dumps({"name": "tdd", "path": "skills/tdd/SKILL.md"})),
        command("/bin/cat .agent-flow/skills/tdd/SKILL.md", "Use red/green tests.")),
        catalog=installed(tmp_path))
    assert result["broad_context_outputs"] == []
    assert result["command_count"] == 3


def test_whole_workflow_emitted_by_failed_grep_is_still_observed(tmp_path):
    output = 'meta.json:16: "source_text": "id: full-feature\\nphases:\\n  - id: green"\n'
    result = context_reads.context_outputs(evidence(tmp_path,
        command("rg -n 'green' .agent-flow/runs/run/*.json missing-file", output, 2)),
        catalog=installed(tmp_path))
    assert result["workflow_source_outputs"] == 1
    assert result["broad_context_outputs"][0]["exit_code"] == 2


def test_selected_phase_metadata_is_not_a_whole_workflow_dump(tmp_path):
    result = context_reads.context_outputs(evidence(tmp_path,
        command("jq '.phase' meta.json", '{"id":"green","required":["tdd"]}'),
        {"type": "agent_message", "text": 'I read skills/index.json and "source_text": phases:'}),
        catalog=installed(tmp_path))
    assert result["broad_context_outputs"] == []
    assert result["command_count"] == 1


def test_filtered_catalog_shape_does_not_count_as_the_full_installed_index(tmp_path):
    filtered = {"selection": {"selected_skills": ["tdd"]},
                "skills": [{"name": "tdd"}], "links": []}
    result = context_reads.context_outputs(evidence(tmp_path,
        command("jq filter .agent-flow/skills/index.json", json.dumps(filtered))),
        catalog=installed(tmp_path))
    assert result["broad_context_outputs"] == []


def test_catalog_emitted_through_a_wildcard_is_still_detected(tmp_path):
    result = context_reads.context_outputs(evidence(tmp_path,
        command("cat .agent-flow/skills/*.json", json.dumps(CATALOG))),
        catalog=installed(tmp_path))
    assert result["skill_catalog_outputs"] == 1


def test_cli_reports_broad_context_as_a_failure(tmp_path, capsys):
    output = '{"source_text":"id: full-feature\\nphases:\\n  - id: green"}'
    path = evidence(tmp_path, command("rg green meta.json", output, 2))
    assert context_reads.main([str(path), "--catalog", str(installed(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out)["workflow_source_outputs"] == 1


def test_cli_accepts_required_skill_output(tmp_path, capsys):
    path = evidence(tmp_path, command("/bin/cat skills/tdd/SKILL.md", "Use red/green tests."))
    assert context_reads.main([str(path), "--catalog", str(installed(tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out)["broad_context_outputs"] == []


@pytest.mark.parametrize("content", [
    "truncated native event\n",
    '[]\n{"type":"turn.completed"}\n',
    '{"type":"item.completed","item":null}\n{"type":"turn.completed"}\n',
    '{"type":"item.completed","item":{"type":"command_execution","command":"cat x",'
    '"aggregated_output":null}}\n{"type":"turn.completed"}\n',
    # An untyped item carrying the full catalog must not pass as a non-command item.
    '{"type":"item.completed","item":{"command":"cat index.json","aggregated_output":'
    + json.dumps(json.dumps(CATALOG)) + '}}\n{"type":"turn.completed"}\n',
    '{"item":{}}\n{"type":"turn.completed"}\n',
    '{"type":"item.completed","item":{"type":"command_execution","command":"cat x",'
    '"aggregated_output":"\\ud800"}}\n{"type":"turn.completed"}\n',
    '{"type":"item.completed","item":{"type":"command_execution","command":"cat \\ud800",'
    '"aggregated_output":"ok"}}\n{"type":"turn.completed"}\n',
    "[" * 100000 + "]" * 100000 + '\n{"type":"turn.completed"}\n',
])
def test_malformed_native_evidence_is_not_reported_as_zero_reads(tmp_path, content):
    path = tmp_path / "stdout.raw"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        context_reads.main([str(path), "--catalog", str(installed(tmp_path))])
    assert error.value.code == 2


def test_deeply_nested_command_output_is_scanned_without_crashing(tmp_path, capsys):
    # Deeper than the json decoder's recursion limit on supported Pythons.
    nested = '{"a":' * 12000 + "0" + "}" * 12000
    path = evidence(tmp_path, command("cat deep.json", nested + "\n" + json.dumps(CATALOG)))
    assert context_reads.main([str(path), "--catalog", str(installed(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out)["skill_catalog_outputs"] == 1


def test_unicode_line_separators_inside_strings_do_not_split_events(tmp_path, capsys):
    event = {"type": "item.completed",
             "item": command("cat notes.txt", "a\u2028b\u2029c\x85d")}
    path = tmp_path / "stdout.raw"
    path.write_text(json.dumps(event, ensure_ascii=False) + '\n{"type":"turn.completed"}\n',
                    encoding="utf-8")
    assert context_reads.main([str(path), "--catalog", str(installed(tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out)["command_count"] == 1


@pytest.mark.skipif(not hasattr(sys, "set_int_max_str_digits"),
                    reason="integer digit limit exists from Python 3.11")
def test_integer_digit_limit_is_malformed_evidence_but_not_a_scan_crash(tmp_path, capsys):
    huge = "1" * 5000
    path = tmp_path / "stdout.raw"
    path.write_text('{"type":"x","n":' + huge + '}\n{"type":"turn.completed"}\n', encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        context_reads.main([str(path), "--catalog", str(installed(tmp_path))])
    assert error.value.code == 2
    path = evidence(tmp_path, command("cat n.json", '{"n":' + huge + "}\n" + json.dumps(CATALOG)))
    assert context_reads.main([str(path), "--catalog", str(installed(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out)["skill_catalog_outputs"] == 1


@pytest.mark.parametrize("content", ["", '{"type":"thread.started"}\n'])
def test_cli_rejects_empty_or_incomplete_native_stream(tmp_path, content):
    path = tmp_path / "stdout.raw"
    path.write_text(content)
    with pytest.raises(SystemExit) as error:
        context_reads.main([str(path), "--catalog", str(installed(tmp_path))])
    assert error.value.code == 2


def test_cli_rejects_missing_evidence_and_non_object_catalog(tmp_path):
    path = evidence(tmp_path, command("/bin/cat skills/tdd/SKILL.md", "Use red/green tests."))
    with pytest.raises(SystemExit) as missing:
        context_reads.main([str(tmp_path / "absent.raw"), "--catalog", str(installed(tmp_path))])
    with pytest.raises(SystemExit) as listed:
        context_reads.main([str(path), "--catalog", str(installed(tmp_path, []))])
    assert (missing.value.code, listed.value.code) == (2, 2)


def test_cli_requires_reference_catalog_for_filtered_json(tmp_path):
    filtered = {"selection": {"selected_skills": ["tdd"]},
                "skills": [{"name": "tdd"}], "links": []}
    path = evidence(tmp_path, command("jq filter .agent-flow/skills/index.json", json.dumps(filtered)))
    with pytest.raises(SystemExit) as error:
        context_reads.main([str(path)])
    assert error.value.code == 2
    assert context_reads.main([str(path), "--catalog", str(installed(tmp_path))]) == 0
