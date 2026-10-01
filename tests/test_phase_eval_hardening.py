from __future__ import annotations

import importlib.util
import hashlib
import json
import shlex
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
REVIEW = "## Reviewer\nreviewer-source: sub-agent\nverdict: approve"


@pytest.fixture
def evaluator(monkeypatch):
    spec = importlib.util.spec_from_file_location("phase_eval_hardening", ROOT / "evals/phase_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    return module


def review_case(evaluator, monkeypatch, tmp_path, provider, stdout, evidence=None, documents=None):
    case = tmp_path / "case"
    case.mkdir()
    (case / "case.json").write_text(json.dumps({
        "review": {"local": {"clean": {"expect": "approve", "defects": []}}},
    }), encoding="utf-8")
    monkeypatch.setattr(evaluator, "_prepare_subprocess", lambda *args: {
        "jobs": [{"angle": "types", "provider": provider, "prompt": "Review this change.",
                  "documents": documents or []}],
    })
    commands = []

    def invoke(args, prompt, cwd, timeout):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(evaluator, "_run_cli", invoke)
    result = evaluator.run_review("after", ROOT, case, "local", "clean", 1, tmp_path, 10, evidence=evidence)
    return result, commands


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_review_without_usage_does_not_report_zero_tokens(evaluator, monkeypatch, tmp_path, provider):
    if provider == "claude":
        stdout = json.dumps({"type": "result", "result": REVIEW})
    else:
        stdout = "\n".join(json.dumps(event) for event in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
            {"type": "turn.completed"},
        ])
    result, _ = review_case(evaluator, monkeypatch, tmp_path, provider, stdout)
    assert result["overall"] == "approve"
    assert result["usage"]["input"] is None
    assert result["usage"]["cached_input"] is None
    assert result["usage"]["output"] is None
    assert result["usage_missing_reviewers"]["input"] == 1


def test_claude_reviewer_has_only_read_tools_and_records_successful_reads(evaluator, monkeypatch, tmp_path):
    stdout = "\n".join(json.dumps(event) for event in [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "read-ok", "name": "Read", "input": {"file_path": "/fixture/rule.md"}},
            {"type": "tool_use", "id": "read-failed", "name": "Read", "input": {"file_path": "/fixture/missing.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-ok", "content": "A rule."},
            {"type": "tool_result", "tool_use_id": "read-failed", "is_error": True, "content": "Missing."},
        ]}},
        {"type": "result", "result": REVIEW, "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 100, "output_tokens": 7,
        }},
    ])
    result, commands = review_case(evaluator, monkeypatch, tmp_path, "claude", stdout)
    args = commands[0]
    assert args[args.index("--permission-mode") + 1] == "default"
    assert set(args[args.index("--tools") + 1].split(",")) == {"Read", "Glob", "Grep"}
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert result["overall"] == "approve"
    assert result["angles"][0]["read_paths_observed"] == ["/fixture/rule.md"]
    assert result["angles"][0]["failed_reads"] == ["/fixture/missing.md"]
    assert result["usage"] == {"input": 130, "uncached_input": 30, "cached_input": 100, "output": 7}


@pytest.mark.parametrize("failure", ["provider", "write", "format"])
def test_review_failure_stays_invalid_even_with_approve_text(evaluator, monkeypatch, tmp_path, failure):
    events = []
    if failure == "write":
        events.extend([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "write", "name": "Write", "input": {"file_path": "/outside/plan.md"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "write", "content": "Written."},
            ]}},
        ])
    events.append({"type": "result", "is_error": failure == "provider",
                   "result": "verdict: approve" if failure == "format" else REVIEW})
    result, _ = review_case(evaluator, monkeypatch, tmp_path, "claude", "\n".join(map(json.dumps, events)))
    assert result["overall"] == "incomplete"
    assert result["correct"] is None
    angle = result["angles"][0]
    if failure == "format":
        assert angle["error"] is None
        assert angle["output_contract_error"]
    else:
        assert angle["error"]


def test_evaluation_retains_raw_call_evidence(evaluator, monkeypatch, tmp_path):
    stdout = json.dumps({"type": "result", "result": REVIEW})
    monkeypatch.setattr(evaluator, "_run_cli", lambda args, *rest: subprocess.CompletedProcess(args, 0, stdout, "warning"))
    evidence = tmp_path / "evidence"
    result = evaluator._claude("A fixed prompt.", tmp_path, author=False, timeout=10, evidence=evidence)
    assert (evidence / "stdout.raw").read_text() == stdout
    assert (evidence / "stderr.raw").read_text() == "warning"
    assert (evidence / "prompt.txt").read_text() == "A fixed prompt."
    command = json.loads((evidence / "command.json").read_text())
    assert command["prompt_sha256"] == hashlib.sha256(b"A fixed prompt.").hexdigest()
    assert result["execution"]["stdout_sha256"] == hashlib.sha256(stdout.encode()).hexdigest()


def test_document_manifest_checks_inline_bytes_and_keeps_reference_documents(evaluator):
    def item(path, body):
        return SimpleNamespace(document=SimpleNamespace(path=path, sha256=hashlib.sha256(body).hexdigest(), bytes=len(body)))
    root = item("/fixture/SKILL.md", b"A rule.")
    reference = item("/fixture/reference.md", b"An exception.")
    resolution = SimpleNamespace(delivery=[
        SimpleNamespace(content=b"A rule.", inline=True, documents=[root]),
        SimpleNamespace(content=b"An exception.", inline=False, documents=[reference]),
    ])
    manifest = evaluator._document_manifest(resolution, "A rule.")
    assert len(manifest) == 2
    assert manifest[0]["inline_in_prompt"] is True
    assert manifest[1]["inline_in_prompt"] is False
    assert evaluator._document_manifest(resolution, "Only a path.")[0]["inline_in_prompt"] is False


@pytest.mark.parametrize("usage, expected", [
    ({"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
     {"input": 0, "uncached_input": 0, "cached_input": 0, "output": 0}),
    ({"input_tokens": 4, "cache_creation_input_tokens": 6, "output_tokens": 2},
     {"input": None, "uncached_input": 10, "cached_input": None, "output": 2}),
    ({"input_tokens": True, "cache_creation_input_tokens": 6, "cache_read_input_tokens": 8, "output_tokens": -1},
     {"input": None, "uncached_input": None, "cached_input": 8, "output": None}),
])
def test_claude_preserves_reported_zero_and_partial_usage(evaluator, monkeypatch, tmp_path, usage, expected):
    stdout = json.dumps({"type": "result", "result": REVIEW, "usage": usage})
    result, _ = review_case(evaluator, monkeypatch, tmp_path, "claude", stdout)
    assert result["usage"] == expected


@pytest.mark.parametrize("second, expected", [
    ({"input_tokens": 20, "cached_input_tokens": 12, "output_tokens": 3},
     {"input": 30, "uncached_input": 12, "cached_input": 18, "output": 5}),
    ({"input_tokens": 20, "output_tokens": 3},
     {"input": 30, "uncached_input": None, "cached_input": None, "output": 5}),
    ({"input_tokens": 20, "cached_input_tokens": 21, "output_tokens": 3},
     {"input": 30, "uncached_input": None, "cached_input": 27, "output": 5}),
])
def test_codex_counts_all_turns_without_inventing_uncached_usage(evaluator, monkeypatch, tmp_path, second, expected):
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 6, "output_tokens": 2}},
        {"type": "turn.completed", "usage": second},
    ]
    result, _ = review_case(evaluator, monkeypatch, tmp_path, "codex", "\n".join(map(json.dumps, events)))
    assert result["usage"] == expected


def test_codex_failed_turn_is_not_an_approval(evaluator, monkeypatch, tmp_path):
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
        {"type": "turn.failed", "error": {"message": "Provider unavailable."}},
    ]
    result, _ = review_case(evaluator, monkeypatch, tmp_path, "codex", "\n".join(map(json.dumps, events)))
    assert result["overall"] == "incomplete"
    assert result["angles"][0]["error"]


def test_codex_malformed_item_preserves_raw_and_remaining_usage(evaluator, monkeypatch, tmp_path):
    stdout = "\n".join(map(json.dumps, [
        {"type": "item.completed", "item": "malformed"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
        {"type": "turn.completed", "usage": {"input_tokens": 8, "cached_input_tokens": 2, "output_tokens": 3}},
    ]))
    monkeypatch.setattr(evaluator, "_run_cli", lambda args, *rest: subprocess.CompletedProcess(args, 0, stdout, ""))
    evidence = tmp_path / "codex-evidence"
    result = evaluator._codex("Review.", tmp_path, timeout=10, evidence=evidence)
    assert (evidence / "stdout.raw").read_text() == stdout
    assert result["usage"]["input"] == 8
    assert result["text"] == REVIEW


def test_codex_unobserved_tools_are_not_an_empty_list(evaluator, monkeypatch, tmp_path):
    stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}})
    result, _ = review_case(evaluator, monkeypatch, tmp_path, "codex", stdout)
    assert result["angles"][0]["tool_calls"] is None
    assert result["angles"][0]["read_paths_observed"] is None


def test_one_reviewer_exception_keeps_other_reviewers_usage(evaluator, monkeypatch, tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    (case / "case.json").write_text(json.dumps({
        "review": {"local": {"clean": {"expect": "approve", "defects": []}}},
    }), encoding="utf-8")
    monkeypatch.setattr(evaluator, "_prepare_subprocess", lambda *args: {"jobs": [
        {"angle": "types", "provider": provider, "prompt": "Review."}
        for provider in ("claude", "codex")
    ]})
    monkeypatch.setattr(evaluator, "_claude", lambda *args, **kwargs: {
        "rc": 0, "text": REVIEW, "seconds": 1,
        "usage": {"input": 8, "uncached_input": 6, "cached_input": 2, "output": 3},
    })

    def fail(*args, **kwargs):
        raise ValueError("Malformed provider event.")

    monkeypatch.setattr(evaluator, "_codex", fail)
    result = evaluator.run_review("after", ROOT, case, "local", "clean", 1, tmp_path, 10)
    assert result["overall"] == "incomplete"
    assert len(result["angles"]) == 2
    assert result["angles"][1]["error"] == "Malformed provider event."
    assert result["usage"]["input"] is None
    assert result["usage_observed"]["input"] == 8
    assert result["usage_missing_reviewers"]["input"] == 1


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_evidence_write_failure_is_invalid_with_unknown_usage(evaluator, monkeypatch, tmp_path, provider):
    if provider == "claude":
        stdout = json.dumps({"type": "result", "result": REVIEW, "usage": {
            "input_tokens": 5, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 2,
        }})
    else:
        stdout = "\n".join(map(json.dumps, [
            {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 2}},
        ]))
    evidence = tmp_path / "evidence"
    (evidence / f"types-{provider}").mkdir(parents=True)
    result, _ = review_case(evaluator, monkeypatch, tmp_path, provider, stdout, evidence=evidence)
    assert result["overall"] == "incomplete"
    assert result["angles"][0]["error"]
    assert result["usage"] == {key: None for key in ("input", "uncached_input", "cached_input", "output")}
    assert result["usage_missing_reviewers"]["input"] == 1


SKILL_RULE = "Never approve a release without successful behavioral evidence."


def codex_command_event(project, command):
    process = subprocess.run(["sh", "-c", command], cwd=project, capture_output=True, text=True)
    return {
        "type": "item.completed",
        "item": {"id": "read", "type": "command_execution", "command": command,
                 "exit_code": process.returncode, "status": "completed" if process.returncode == 0 else "failed",
                 "aggregated_output": process.stdout + process.stderr},
    }


@pytest.mark.parametrize("reader", [
    "cat {path}", "sed -n '1,2p' {path}", "head -n 1 {path}", "tail -n 1 {path}",
    "sh -lc {wrapped}",
])
def test_codex_observes_successful_reader_output_not_self_report(evaluator, monkeypatch, tmp_path, reader):
    skill = tmp_path / "skill with spaces" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(SKILL_RULE + "\n", encoding="utf-8")
    path = shlex.quote(str(skill.relative_to(tmp_path)))
    command = reader.format(path=path, wrapped=shlex.quote(f"cd {shlex.quote(str(tmp_path))} && cat {path}"))
    event = codex_command_event(tmp_path, command)
    stdout = "\n".join(map(json.dumps, [
        event, {"type": "item.completed", "item": {"type": "agent_message", "text": "I read another skill."}},
    ]))
    monkeypatch.setattr(evaluator, "_run_cli", lambda args, *rest: subprocess.CompletedProcess(args, 0, stdout, ""))
    result = evaluator._codex("Write.", tmp_path, author=True, timeout=10)
    assert result["reads"] == [str(skill.resolve())]
    assert result["failed_reads"] == []
    assert result["tool_calls"][0]["succeeded"] is True


def test_codex_failed_command_is_not_a_successful_read(evaluator, monkeypatch, tmp_path):
    skill = tmp_path / "missing.md"
    event = codex_command_event(tmp_path, "cat missing.md")
    stdout = "\n".join(map(json.dumps, [
        event, {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
    ]))
    monkeypatch.setattr(evaluator, "_run_cli", lambda args, *rest: subprocess.CompletedProcess(args, 0, stdout, ""))
    result = evaluator._codex("Review.", tmp_path, timeout=10)
    assert result["reads"] == []
    assert result["failed_reads"] == [str(skill.resolve())]
    assert result["tool_calls"][0]["succeeded"] is False


@pytest.mark.parametrize("command", [
    "printf '%s\\n' 'cat SKILL.md'",
    "printf '%s\\n' {rule}",
    "grep -n 'cat SKILL.md' SKILL.md",
    "FILE=SKILL.md; cat \"$FILE\"",
    "cat $(printf SKILL.md)",
    "cat SKILL.*",
    "cat SKILL.md | cat",
    "cat missing.md || printf '%s\\n' {rule}",
    "sed -n 's/.*/Never approve a release without successful behavioral evidence./p' another.md",
    "cd missing; cat SKILL.md",
])
def test_codex_deceptive_dynamic_or_ambiguous_reads_are_not_observed(evaluator, tmp_path, command):
    (tmp_path / "SKILL.md").write_text(SKILL_RULE + "\n", encoding="utf-8")
    (tmp_path / "another.md").write_text("Unrelated file content.\n", encoding="utf-8")
    event = codex_command_event(tmp_path, command.format(rule=shlex.quote(SKILL_RULE)))
    assert evaluator._codex_read_paths(event["item"], tmp_path)[0] == []


def test_codex_reader_without_source_content_does_not_count(evaluator, tmp_path):
    (tmp_path / "SKILL.md").write_text("# Skill\n" + SKILL_RULE + "\n", encoding="utf-8")
    event = codex_command_event(tmp_path, "head -n 1 SKILL.md")
    assert evaluator._codex_read_paths(event["item"], tmp_path) == ([], [])


def test_codex_local_reader_named_cat_cannot_fabricate_read_evidence(evaluator, tmp_path):
    (tmp_path / "SKILL.md").write_text(SKILL_RULE + "\n", encoding="utf-8")
    reader = tmp_path / "cat"
    reader.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(SKILL_RULE) + "\n", encoding="utf-8")
    reader.chmod(0o755)
    event = codex_command_event(tmp_path, "./cat SKILL.md")
    assert event["item"]["aggregated_output"].strip() == SKILL_RULE
    assert evaluator._codex_read_paths(event["item"], tmp_path) == ([], [])


def test_codex_author_read_metric_distinguishes_success_missing_and_self_report(evaluator, monkeypatch, tmp_path):
    evaluator.REQUESTED_PROVIDERS.update(author="codex", review=["codex"])
    project = tmp_path / "author-case-local-after-1"
    project.mkdir()
    read = project / "read" / "SKILL.md"
    unread = project / "unread" / "SKILL.md"
    missing = project / "missing" / "SKILL.md"
    for skill in (read, unread):
        skill.parent.mkdir()
        skill.write_text(SKILL_RULE + "\n", encoding="utf-8")
    monkeypatch.setattr(evaluator, "_prepare_subprocess", lambda *args: {
        "prompt": "Implement.", "required": [
            {"name": name, "path": str(path)} for name, path in (("read", read), ("unread", unread), ("missing", missing))
        ],
    })
    monkeypatch.setattr(evaluator, "_oracle", lambda *args: {"behavior": True, "plan": True, "norm": True})
    events = [
        codex_command_event(project, "cat read/SKILL.md"),
        codex_command_event(project, "cat missing/SKILL.md"),
        {"type": "item.completed", "item": {"type": "agent_message", "text": "I read unread/SKILL.md too."}},
    ]
    stdout = "\n".join(map(json.dumps, events))
    monkeypatch.setattr(evaluator, "_run_cli", lambda args, *rest: subprocess.CompletedProcess(args, 0, stdout, ""))
    row = evaluator.run_author("after", ROOT, tmp_path / "case", "local", 1, tmp_path, 10)
    assert row["required_read"] == ["read"]
    assert row["required_unread"] == ["missing", "unread"]
    assert row["required_read_rate"] == 0.333
    assert row["failed_reads"] == [str(missing)]


@pytest.mark.parametrize("failure", ["turn", "exit", "exception"])
def test_codex_author_failure_remains_invalid_with_passing_oracle(evaluator, monkeypatch, tmp_path, failure):
    evaluator.REQUESTED_PROVIDERS.update(author="codex", review=["codex"])
    monkeypatch.setattr(evaluator, "_prepare_subprocess", lambda *args: {
        "prompt": "Implement.", "required": [],
    })
    monkeypatch.setattr(evaluator, "_oracle", lambda *args: {"behavior": True, "plan": True, "norm": True})

    def invoke(args, *rest):
        if failure == "exception":
            raise FileNotFoundError("Codex unavailable.")
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "Implemented."}}]
        if failure == "turn":
            events.append({"type": "turn.failed", "error": "Provider unavailable."})
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0, "\n".join(map(json.dumps, events)), "")

    monkeypatch.setattr(evaluator, "_run_cli", invoke)
    row = evaluator.run_author("after", ROOT, tmp_path / "case", "local", 1, tmp_path, 10)
    assert row["provider"] == "codex"
    assert row["error"]
    if failure != "exception":
        assert row["oracle"]["behavior"] is True


@pytest.mark.parametrize("selection", ["", ",", "codex,", "codex,codex", "unknown", "claude,unknown"])
def test_invalid_reviewer_selection_rejected_before_preparing_project(evaluator, monkeypatch, tmp_path, selection):
    output = tmp_path / "output"
    monkeypatch.setattr(evaluator.sys, "argv", [
        "phase_eval.py", "--review-providers", selection, "--kit", f"after={ROOT}", "--output", str(output),
        "--prepare", "[]",
    ])
    with pytest.raises(SystemExit) as error:
        evaluator.main()
    assert error.value.code == 2
    assert not output.exists()


def test_unselected_reviewer_is_invalid_without_launching_provider(evaluator, monkeypatch, tmp_path):
    evaluator.REQUESTED_PROVIDERS.update(author="codex", review=["codex"])
    row, commands = review_case(evaluator, monkeypatch, tmp_path, "claude",
                                json.dumps({"type": "result", "result": REVIEW}))
    assert commands == []
    assert row["overall"] == "incomplete"
    assert row["angles"][0]["provider"] == "claude"
    assert row["angles"][0]["error"] == "unselected reviewer provider: claude"


def test_codex_batch_reads_require_distinct_printed_source_evidence(evaluator, tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text(SKILL_RULE + "\nFirst document has its own specific requirement.\n", encoding="utf-8")
    second.write_text(SKILL_RULE + "\nSecond document has a different specific requirement.\n", encoding="utf-8")
    event = codex_command_event(tmp_path, "cat first.md second.md")
    assert set(evaluator._codex_read_paths(event["item"], tmp_path)[0]) == {str(first), str(second)}
    event = codex_command_event(tmp_path, "sed -n '1p' first.md; cat second.md")
    assert evaluator._codex_read_paths(event["item"], tmp_path)[0] == [str(second)]


def test_codex_hash_suffix_is_a_real_filename_not_a_comment(evaluator, tmp_path):
    (tmp_path / "SKILL.md").write_text(SKILL_RULE + "\n", encoding="utf-8")
    copy = tmp_path / "SKILL.md#copy"
    copy.write_text(SKILL_RULE + "\n", encoding="utf-8")
    event = codex_command_event(tmp_path, "cat SKILL.md#copy")
    assert evaluator._codex_read_paths(event["item"], tmp_path)[0] == [str(copy)]


def test_codex_unattributed_command_reads_remain_unknown(evaluator, monkeypatch, tmp_path):
    (tmp_path / "SKILL.md").write_text(SKILL_RULE + "\n", encoding="utf-8")
    command = "python3 -c \"from pathlib import Path; print(Path('SKILL.md').read_text())\""
    event = codex_command_event(tmp_path, command)
    row, _ = review_case(evaluator, monkeypatch, tmp_path, "codex",
                         "\n".join(map(json.dumps, [
                             event, {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
                         ])), documents=[{"path": str(tmp_path / "SKILL.md"), "inline_in_prompt": False}])
    assert row["angles"][0]["documents"][0]["read_path_observed"] is None


def test_codex_author_missing_read_channel_is_invalid_not_unread(evaluator, monkeypatch, tmp_path):
    evaluator.REQUESTED_PROVIDERS.update(author="codex", review=["codex"])
    monkeypatch.setattr(evaluator, "_prepare_subprocess", lambda *args: {
        "prompt": "Implement.", "required": [{"name": "policy", "path": str(tmp_path / "SKILL.md")}],
    })
    monkeypatch.setattr(evaluator, "_oracle", lambda *args: {"behavior": True, "plan": True, "norm": True})
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "Implemented."}}
    monkeypatch.setattr(evaluator, "_run_cli", lambda args, *rest:
                        subprocess.CompletedProcess(args, 0, json.dumps(event), ""))
    row = evaluator.run_author("after", ROOT, tmp_path / "case", "local", 1, tmp_path, 10)
    assert row["required_unread"] is None
    assert row["required_unobserved"] == ["policy"]
    assert row["required_read_rate"] is None
    assert row["error"]
    assert row["oracle"] == {"behavior": True, "plan": True, "norm": True}


def test_codex_recovered_stream_error_preserves_warning_not_invalid_result(evaluator, monkeypatch, tmp_path):
    events = [
        {"type": "error", "message": "Reconnecting after transport failure"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": REVIEW}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}},
    ]
    row, _ = review_case(evaluator, monkeypatch, tmp_path, "codex", "\n".join(map(json.dumps, events)))
    assert row["valid"] is True
    assert row["angles"][0]["provider_warnings"] == ["Reconnecting after transport failure"]
    assert row["usage"]["uncached_input"] == 10
