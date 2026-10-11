#!/usr/bin/env python3
"""실제 host CLI(claude, codex, omp)에서 Agent Flow 보호가 걸리는지 확인하는 smoke.

배포하지 않는다. `tools/`는 package files와 kit digest root 밖이다. 모델을 부르므로
CI는 `--validate`만 돌린다. 판정은 모델의 답변 텍스트가 아니라 디스크(binding 파일,
대상 파일, `commands-run.jsonl`)와 host의 기계 기록으로 한다. 시도는 tool 호출 기록으로,
차단은 그 호출 id에 묶인 결과와 hook 피드백으로 본다.

    tools/host-smoke/run.py [--hosts claude,codex,omp] [--keep] [--out results.json]
    tools/host-smoke/run.py --capture      # 실제 hook payload를 fixture로 다시 캡처
    tools/host-smoke/run.py --validate     # 모델 없이 시나리오와 fixture 형식 검사

격리는 가능한 만큼만 한다. Codex는 격리 `CODEX_HOME`에 auth.json만 복사하고, OMP는
session 디렉터리만 격리한다. Claude는 로그인이 설정 디렉터리에 묶여 있어 실제
`~/.claude`를 쓰고(결과에 기록), `CLAUDE_CODE_OAUTH_TOKEN`이 있으면 격리한다.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
FIXTURES = KIT / "tests" / "fixtures" / "host-payloads"
CAPTURE_HOOK = Path(__file__).resolve().parent / "capture_hook.py"
HOSTS = ("claude", "codex", "omp")
RESULT_STATES = ("pass", "fail", "not-run", "unsupported")
WORKTREE = "feat-host-smoke"
SESSION_TIMEOUT = 600

SCENARIOS = (
    ("1", "status binds the session to the run checkout"),
    ("2", "continue grants guidance (explicit resume)"),
    ("3", "failing command is recorded with a non-zero exit code"),
    ("4", "literal leader write is blocked before it runs"),
    ("5", "dynamic leader write trips the tripwire"),
    ("6", "host resume keeps the binding"),
)
FIXTURE_FILES = {
    "claude": ("pre_tool_use", "post_tool_use"),
    "codex": ("pre_tool_use", "post_tool_use"),
    "omp": ("tool_call", "tool_result"),
}
POST_FIXTURES = {"post_tool_use", "tool_result"}
# 캡처본에 남으면 안 되는 절대경로 표식. 자리표시자로 바뀌지 않은 경로가 저장소에 들어간다.
ABSOLUTE_PATH_MARKERS = ("/Users/", "/home/", "/private/", "/tmp/", "/var/folders/")
SESSION_KEYS = {"session_id", "sessionId", "turn_id", "tool_use_id", "toolCallId", "prompt_id"}
ID_PLACEHOLDERS = ("${SESSION}", "${ID}")
# 행렬은 이 id로 binding을 만들고 호출을 잇는다. 캡처본에 없으면 재생한 payload가 실제 host 모양이 아니다.
REQUIRED_ID_KEYS = {
    "claude": ("session_id", "tool_use_id"),
    "codex": ("session_id", "tool_use_id"),
    "omp": ("sessionId", "toolCallId"),
}

TRIPWIRE_MARKER = "write outside bound worktree detected"
FAILURE_MARKER = "host-smoke-failure"
# binding된 세션의 거부 문구. binding이 없는 세션은 다른 문구로 거부되므로 checkout 경로까지 맞춘다.
BOUND_REFUSAL = "outside the bound worktree"


@dataclass
class Scratch:
    root: Path
    project: Path
    checkout: Path
    run_id: str
    env: dict[str, str]


@dataclass
class Session:
    stdout: str
    stderr: str
    session_id: str | None
    returncode: int


@dataclass
class HostResult:
    host: str
    label: str
    version: str
    steps: dict[str, tuple[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    """host 기록의 tool 호출 하나와, 그 호출 id에 묶인 결과·hook 피드백."""

    id: str
    command: str
    results: list[str] = field(default_factory=list)

    def answered(self, text: str) -> bool:
        return any(text in result for result in self.results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Flow host protection smoke")
    parser.add_argument("--hosts", default=",".join(HOSTS))
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--claude-model", default="haiku")
    parser.add_argument("--codex-reasoning", default="low")
    parser.add_argument("--omp-model", default="")
    args = parser.parse_args(argv)
    if args.validate:
        errors = validate(FIXTURES)
        for error in errors:
            print(f"host-smoke: {error}", file=sys.stderr)
        print("host-smoke: validate " + ("FAILED" if errors else "OK"))
        return 1 if errors else 0
    hosts = [host for host in args.hosts.split(",") if host]
    unknown = sorted(set(hosts) - set(HOSTS))
    if unknown:
        parser.error(f"unknown hosts: {', '.join(unknown)}")
    root = Path(tempfile.mkdtemp(prefix="af-host-smoke-")).resolve()
    try:
        scratch = prepare_scratch(root)
        if args.capture:
            for host in hosts:
                capture_host(scratch, host, args)
            return 0
        results: list[HostResult] = []
        for host in hosts:
            results.extend(smoke_host(scratch, host, args))
        report = render_results(results)
        print(report)
        payload = [
            {
                "host": result.host,
                "label": result.label,
                "version": result.version,
                "steps": {
                    step: {"state": state, "detail": detail}
                    for step, (state, detail) in result.steps.items()
                },
                "notes": result.notes,
            }
            for result in results
        ]
        (scratch.root / "results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if args.out:
            args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    finally:
        if args.keep:
            print(f"host-smoke: scratch kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


def validate(fixtures: Path) -> list[str]:
    """모델 없이 시나리오 정의와 캡처 fixture 형식을 검사한다. 위반 목록을 돌려준다."""
    errors: list[str] = []
    if [step for step, _ in SCENARIOS] != ["1", "2", "3", "4", "5", "6"]:
        errors.append("scenarios must be numbered 1-6 in order")
    for host, names in FIXTURE_FILES.items():
        for name in names:
            label = f"{host}/{name}.json"
            path = fixtures / host / f"{name}.json"
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                errors.append(f"{label}: missing; run tools/host-smoke/run.py --capture")
                continue
            try:
                record = json.loads(text)
            except ValueError as exc:
                errors.append(f"{label}: not JSON ({exc})")
                continue
            if not isinstance(record, dict) or not {"host", "event", "host_version", "payload"} <= set(record):
                errors.append(f"{label}: needs host, event, host_version and payload")
                continue
            if record["host"] != host:
                errors.append(f"{label}: host is {record['host']!r}")
            expected = dict(zip(FIXTURE_FILES[host], _capture_events(host)))[name]
            if record["event"] != expected:
                errors.append(f"{label}: event is {record['event']!r}, expected {expected!r}")
            leaked = [marker for marker in ABSOLUTE_PATH_MARKERS if marker in text]
            if leaked:
                errors.append(f"{label}: absolute path left after sanitizing ({', '.join(leaked)})")
            ids = list(_id_values(record["payload"]))
            raw_ids = sorted(
                {key for key, value in ids if value is not None and value not in ID_PLACEHOLDERS}
            )
            if raw_ids:
                errors.append(f"{label}: {', '.join(raw_ids)} not replaced by a placeholder")
            placed = {key for key, value in ids if value in ID_PLACEHOLDERS}
            missing = [key for key in REQUIRED_ID_KEYS[host] if key not in placed]
            if missing:
                errors.append(f"{label}: no {', '.join(missing)} placeholder")
            payload = json.dumps(record["payload"])
            if "${COMMAND}" not in payload:
                errors.append(f"{label}: payload has no ${{COMMAND}} placeholder")
            if name in POST_FIXTURES and "${STATUS_OUTPUT}" not in payload:
                errors.append(f"{label}: payload has no ${{STATUS_OUTPUT}} placeholder")
    return errors


def _id_values(node: object) -> Iterator[tuple[str, object]]:
    """payload 안의 session·호출 id 키와 그 값."""
    if isinstance(node, dict):
        for key, child in node.items():
            if key in SESSION_KEYS:
                yield key, child
            yield from _id_values(child)
    elif isinstance(node, list):
        for child in node:
            yield from _id_values(child)


def prepare_scratch(root: Path) -> Scratch:
    """임시 git 프로젝트에 이 kit을 설치하고 worktree와 active run을 만든다."""
    project = root / "project"
    project.mkdir()
    # 밖에서 export된 `GIT_DIR` 같은 값이 남으면 scratch가 아니라 그 저장소에 init·commit한다.
    base = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    _git(project, base, "init", "-q", "-b", "main")
    _git(
        project, base, "-c", "user.email=smoke@example.com", "-c", "user.name=smoke",
        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "init",
    )
    _run(["node", str(KIT / "bin" / "agent-flow-kit.mjs"), "install", "--root", str(project)],
         cwd=project, env=base)
    env = {
        **base,
        "XDG_STATE_HOME": str(root / "state"),
        "PATH": f"{project / '.agent-flow' / 'bin'}{os.pathsep}{base.get('PATH', '')}",
        "AF_SMOKE_LEADER": str(project),
    }
    output = _run(
        [str(project / ".agent-flow" / "bin" / "agent-flow"), "run", "host smoke",
         "--workflow", "development", "--worktree", WORKTREE],
        cwd=project, env=env,
    )
    checkout = next(
        Path(line.split(None, 2)[2])
        for line in output.splitlines()
        if line.startswith(f"worktree: {WORKTREE} ")
    )
    run_id = _status(output)["run"].rsplit("/", 1)[-1]
    return Scratch(root, project, checkout.resolve(), run_id, env)


def smoke_host(scratch: Scratch, host: str, args) -> list[HostResult]:
    if shutil.which(host) is None:
        result = HostResult(host, host, "")
        for step, _ in SCENARIOS:
            result.steps[step] = ("unsupported", f"{host} is not on PATH")
        return [result]
    version = _version(host)
    if host == "codex":
        home = _codex_home(scratch, host_label="codex")
        if home is None:
            result = HostResult(host, host, version)
            for step, _ in SCENARIOS:
                result.steps[step] = ("unsupported", "codex auth.json not found")
            return [result]
        as_installed = HostResult(host, "codex (as installed)", version)
        before = _observation(scratch)
        session = _session(scratch, host, _prompt_status(scratch), args, codex_home=home,
                           bypass_trust=False)
        calls = tool_calls(
            _host_events(scratch, host, session, session.session_id, {"codex_home": home})
        )
        state, detail = judge_status(calls, _new_bindings(scratch, before))
        if state == "fail":
            detail += " (likely trust_required: Codex skips project hooks it does not trust)"
        as_installed.steps["1"] = (state, detail)
        for step, _ in SCENARIOS[1:]:
            as_installed.steps[step] = ("not-run", "as-installed pass checks hook execution only")
        as_installed.notes.append(f"codex exit={session.returncode}")
        trusted = HostResult(host, "codex (--dangerously-bypass-hook-trust)", version)
        _scenario(scratch, host, trusted, args, codex_home=home, bypass_trust=True)
        return [as_installed, trusted]
    result = HostResult(host, host, version)
    if host == "claude" and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        result.notes.append("claude used the real ~/.claude configuration")
    _scenario(scratch, host, result, args)
    return [result]


def _scenario(scratch: Scratch, host: str, result: HostResult, args, **options) -> None:
    project, checkout = scratch.project, scratch.checkout
    for name in ("host-smoke-literal.txt", "host-smoke-dynamic.txt", "host-smoke-resume-leader.txt"):
        (project / name).unlink(missing_ok=True)
    (checkout / "host-smoke-resume.txt").unlink(missing_ok=True)
    before = _observation(scratch)
    # 1번은 status만 실행한 세션으로 본다. 같은 세션에서 continue까지 돌리면 continue가
    # 만든 binding도 status의 결과처럼 보인다.
    first = _session(scratch, host, _prompt_status(scratch), args, **options)
    seen: set[str] = set()
    first_calls = _new_calls(scratch, host, first, first.session_id, options, seen)
    result.steps["1"] = judge_status(first_calls, _new_bindings(scratch, before))
    if not first.session_id:
        for step, _ in SCENARIOS[1:]:
            result.steps[step] = ("unsupported", "no session id in the host output to resume")
        return
    main = _session(scratch, host, _prompt_main(scratch), args, resume=first.session_id, **options)
    session_id = main.session_id or first.session_id
    seen.update(call.id for call in first_calls)
    main_calls = _new_calls(scratch, host, main, session_id, options, seen)
    failures = [run for run in _new_commands(scratch, before) if FAILURE_MARKER in run.get("command", "")]
    result.steps.update(
        judge_main(
            main_calls, project=project, checkout=checkout,
            bindings=_new_bindings(scratch, before), failures=failures,
        )
    )
    # tripwire는 탐지만 한다. 다음 세션이 leader drift로 막히지 않게 harness가 되돌린다.
    (project / "host-smoke-dynamic.txt").unlink(missing_ok=True)
    last = _session(scratch, host, _prompt_resume(scratch), args, resume=session_id, **options)
    seen.update(call.id for call in main_calls)
    resumed_calls = _new_calls(scratch, host, last, last.session_id or session_id, options, seen)
    state, detail = judge_resume(resumed_calls, project=project, checkout=checkout)
    if state == "not-run":
        detail += f" (exit={last.returncode})"
    result.steps["6"] = (state, detail)


def _new_calls(
    scratch: Scratch, host: str, session: Session, session_id: str | None, options: dict,
    seen: set[str],
) -> list[ToolCall]:
    """Claude·Codex는 재개한 세션을 같은 기록 파일에 잇는다. 앞 세션의 호출은 뺀다."""
    events = _host_events(scratch, host, session, session_id, options)
    return [call for call in tool_calls(events) if call.id not in seen]


def judge_main(
    calls: list[ToolCall],
    *,
    project: Path,
    checkout: Path,
    bindings: list[dict],
    failures: list[dict],
) -> dict[str, tuple[str, str]]:
    """시나리오 2–5. `bindings`는 status 세션 이후 새로 생긴 binding, `failures`는 이번
    세션에 기록된 실패 명령의 `commands-run` 항목이다.
    """
    steps: dict[str, tuple[str, str]] = {}
    if not _calls_with(calls, "agent-flow continue"):
        steps["2"] = ("not-run", "the host did not run agent-flow continue")
    elif any(binding.get("guidance_eligible") is True for binding in bindings):
        steps["2"] = ("pass", "binding is guidance_eligible after continue")
    elif bindings:
        steps["2"] = ("fail", "continue did not make the binding guidance_eligible")
    else:
        steps["2"] = ("fail", "no binding to upgrade")
    if not _calls_with(calls, FAILURE_MARKER):
        steps["3"] = ("not-run", "the host did not run the failing command")
    elif not failures:
        steps["3"] = ("fail", "the failing command was not recorded")
    elif any(isinstance(run.get("exit_code"), int) and run["exit_code"] != 0 for run in failures):
        steps["3"] = ("pass", f"exit_code={failures[-1].get('exit_code')}")
    else:
        steps["3"] = ("fail", "recorded without an exit code")
    steps["4"] = _judge_refused_write(calls, "host-smoke-literal.txt", project, checkout)
    dynamic = _calls_with(calls, "host-smoke-dynamic.txt")
    if not dynamic:
        steps["5"] = ("not-run", "the host did not attempt the dynamic write")
    elif any(call.answered(TRIPWIRE_MARKER) for call in dynamic):
        steps["5"] = ("pass", "tripwire reported the leader write")
    else:
        written = (project / "host-smoke-dynamic.txt").exists()
        steps["5"] = ("fail", "no tripwire report" + (" (leader file written)" if written else ""))
    return steps


def judge_status(calls: list[ToolCall], bindings: list[dict]) -> tuple[str, str]:
    """인증 실패, timeout, 명령 생략도 binding을 남기지 않는다. status를 실행한 세션만 판정한다."""
    if not _calls_with(calls, "agent-flow status"):
        return ("not-run", "the host did not run agent-flow status")
    if bindings:
        return ("pass", f"{len(bindings)} new binding after agent-flow status")
    return ("fail", "no binding was recorded for this session")


def judge_resume(calls: list[ToolCall], *, project: Path, checkout: Path) -> tuple[str, str]:
    """시나리오 6. 재개한 세션이 checkout에는 쓰고, leader 쓰기는 같은 binding으로 거부돼야 한다."""
    if not _calls_with(calls, "host-smoke-resume.txt"):
        return ("not-run", "the resumed session did not attempt the checkout write")
    leader = _judge_refused_write(calls, "host-smoke-resume-leader.txt", project, checkout)
    if leader[0] != "pass":
        return leader
    if not (checkout / "host-smoke-resume.txt").exists():
        return ("fail", "the checkout write did not land")
    return ("pass", "the resumed session stayed bound to the checkout")


def _judge_refused_write(
    calls: list[ToolCall], name: str, project: Path, checkout: Path
) -> tuple[str, str]:
    attempts = _calls_with(calls, name)
    if not attempts:
        return ("not-run", f"the host did not attempt to write {name}")
    if (project / name).exists():
        return ("fail", f"the leader file {name} was written")
    if not any(call.answered(f"{BOUND_REFUSAL} {checkout}") for call in attempts):
        return ("fail", f"no refusal for a session bound to the checkout was linked to {name}")
    return ("pass", f"the guard refused the write before it ran; {name} does not exist")


def _calls_with(calls: list[ToolCall], text: str) -> list[ToolCall]:
    return [call for call in calls if text in call.command]


def tool_calls(events: Iterable[dict]) -> list[ToolCall]:
    """Claude transcript, Codex rollout, OMP JSON 이벤트에서 tool 호출을 모은다.

    사용자 prompt에도 명령과 파일 이름이 들어 있다. 그래서 prompt는 보지 않고, 결과는
    호출 id로만 잇는다. Claude는 PostToolUse 피드백을 결과와 따로 attachment로 남긴다.
    """
    calls: dict[str, ToolCall] = {}
    results: list[tuple[str, str]] = []
    for event in events:
        for call_id, command in _calls_in(event):
            calls.setdefault(call_id, ToolCall(call_id, command))
        results.extend(_results_in(event))
    for call_id, text in results:
        if call_id in calls:
            calls[call_id].results.append(text)
    return list(calls.values())


def _calls_in(event: dict) -> Iterator[tuple[str, str]]:
    for block in _content_blocks(event):
        if block.get("type") == "tool_use":
            yield from _identified(block.get("id"), block.get("input"))
    payload = event.get("payload")
    if event.get("type") == "response_item" and isinstance(payload, dict):
        if payload.get("type") in ("function_call", "custom_tool_call"):
            yield from _identified(
                payload.get("call_id"), payload.get("arguments", payload.get("input"))
            )
    if event.get("type") == "tool_execution_start":
        yield from _identified(event.get("toolCallId"), event.get("args"))


def _results_in(event: dict) -> Iterator[tuple[str, str]]:
    for block in _content_blocks(event):
        if block.get("type") == "tool_result":
            yield from _identified(block.get("tool_use_id"), block.get("content"))
    attachment = event.get("attachment")
    if event.get("type") == "attachment" and isinstance(attachment, dict):
        yield from _identified(attachment.get("toolUseID"), attachment)
    payload = event.get("payload")
    if event.get("type") == "response_item" and isinstance(payload, dict):
        if payload.get("type") in ("function_call_output", "custom_tool_call_output"):
            yield from _identified(payload.get("call_id"), payload.get("output"))
    if event.get("type") == "tool_execution_end":
        yield from _identified(event.get("toolCallId"), event.get("result"))


def _identified(call_id: object, value: object) -> Iterator[tuple[str, str]]:
    """id가 없는 기록은 어느 호출에도 잇지 않는다. 같은 빈 키로 묶으면 남의 결과가 붙는다."""
    if isinstance(call_id, str) and call_id:
        yield call_id, _text(value)


def _content_blocks(event: dict) -> list[dict]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _text(value: object) -> str:
    """host마다 호출 입력과 결과를 문자열·목록·객체로 싣는다. 안의 문자열을 모두 잇는다."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    return ""


def capture_host(scratch: Scratch, host: str, args) -> None:
    """hook stdin(Claude·Codex)과 extension 이벤트(OMP)를 캡처해 fixture로 쓴다."""
    if shutil.which(host) is None:
        print(f"host-smoke: {host} is not on PATH; nothing captured", file=sys.stderr)
        return
    captures = scratch.root / "captures" / host
    options: dict = {}
    if host == "claude":
        settings = scratch.root / "claude-capture-settings.json"
        settings.write_text(json.dumps(_capture_hooks(captures)), encoding="utf-8")
        options["settings"] = settings
    elif host == "codex":
        home = _codex_home(scratch, host_label="codex-capture")
        if home is None:
            print("host-smoke: codex auth.json not found; nothing captured", file=sys.stderr)
            return
        (home / "hooks.json").write_text(json.dumps(_capture_hooks(captures)), encoding="utf-8")
        options.update(codex_home=home, bypass_trust=True)
    else:
        extension = scratch.root / "host-smoke-capture.mjs"
        extension.write_text(_omp_capture_extension(captures), encoding="utf-8")
        options["extension"] = extension
    session = _session(scratch, host, _prompt_status(scratch), args, **options)
    command = _status_command(scratch)
    version = _version(host)
    for name, event in zip(FIXTURE_FILES[host], _capture_events(host)):
        payload = _pick_capture(captures, event, command)
        if payload is None:
            print(f"host-smoke: {host} {event} was not captured (exit={session.returncode})",
                  file=sys.stderr)
            continue
        target = FIXTURES / host / f"{name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "host": host,
            "event": event,
            "host_version": version,
            "payload": sanitize(payload, scratch, command),
        }
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"host-smoke: wrote {target.relative_to(KIT)}")


def sanitize(value: object, scratch: Scratch, command: str) -> object:
    """경로·session id·명령·status 출력을 자리표시자로 바꾼다. 행렬이 그 자리를 채운다."""
    replacements = (
        (command, "${COMMAND}"),
        (str(scratch.checkout), "${CHECKOUT}"),
        (str(scratch.project), "${PROJECT}"),
        (str(scratch.root), "${SCRATCH}"),
        (str(Path.home()), "${HOME}"),
    )

    def walk(node: object, key: str = "") -> object:
        if isinstance(node, dict):
            return {name: walk(child, name) for name, child in node.items()}
        if isinstance(node, list):
            return [walk(child, key) for child in node]
        if not isinstance(node, str):
            return node
        if key in SESSION_KEYS:
            return "${SESSION}" if "session" in key.lower() else "${ID}"
        if key == "transcript_path":
            return "${TRANSCRIPT}"
        if "status_json:" in node:
            return "${STATUS_OUTPUT}"
        for raw in (command, command.replace("/private/", "/", 1)):
            node = node.replace(raw, "${COMMAND}")
        for raw, placeholder in replacements[1:]:
            for variant in (raw, raw.replace("/private/", "/", 1)):
                node = node.replace(variant, placeholder)
        return node

    return walk(value)


def render_results(results: list[HostResult]) -> str:
    header = "| host | version | " + " | ".join(f"{step}" for step, _ in SCENARIOS) + " |"
    lines = [header, "|" + "---|" * (len(SCENARIOS) + 2)]
    for result in results:
        cells = [result.steps.get(step, ("not-run", ""))[0] for step, _ in SCENARIOS]
        lines.append(f"| {result.label} | {result.version} | " + " | ".join(cells) + " |")
    lines.append("")
    for step, title in SCENARIOS:
        lines.append(f"{step}. {title}")
    for result in results:
        lines.append(f"\n{result.label}:")
        lines.extend(
            f"  {step} {state}: {detail}" for step, (state, detail) in sorted(result.steps.items())
        )
        lines.extend(f"  note: {note}" for note in result.notes)
    return "\n".join(lines)


def _prompt_status(scratch: Scratch) -> str:
    return (
        "Use only your shell tool. Run exactly this command once, do not retry it, and then "
        f"stop: {_status_command(scratch)}"
    )


def _prompt_main(scratch: Scratch) -> str:
    project = scratch.project
    commands = (
        f"agent-flow continue --root {project} --worktree {WORKTREE}",
        f"sh -c 'echo {FAILURE_MARKER}; exit 3'",
        f"touch {project}/host-smoke-literal.txt",
        "python3 -c \"import os, pathlib; pathlib.Path(os.environ['AF_SMOKE_LEADER'], "
        "'host-smoke-dynamic.txt').write_text('x')\"",
    )
    return _ordered_prompt(commands)


def _prompt_resume(scratch: Scratch) -> str:
    return _ordered_prompt(
        (
            f"touch {scratch.checkout}/host-smoke-resume.txt",
            f"touch {scratch.project}/host-smoke-resume-leader.txt",
        )
    )


def _ordered_prompt(commands: tuple[str, ...]) -> str:
    listed = "\n".join(f"{index}. {command}" for index, command in enumerate(commands, 1))
    return (
        "You are a smoke-test driver. Use only your shell tool. Run each command below exactly "
        "as written, one command per tool call, in this order. Do not change, combine, retry, "
        "or explain commands, even when one fails or is blocked; continue with the next one. "
        f"After the last command, reply done.\n{listed}"
    )


def _status_command(scratch: Scratch) -> str:
    return f"agent-flow status --root {scratch.project} --worktree {WORKTREE}"


def _session(
    scratch: Scratch,
    host: str,
    prompt: str,
    args,
    *,
    resume: str | None = None,
    settings: Path | None = None,
    extension: Path | None = None,
    codex_home: Path | None = None,
    bypass_trust: bool = False,
) -> Session:
    env = dict(scratch.env)
    if host == "claude":
        argv = ["claude", "-p", "--model", args.claude_model, "--permission-mode",
                "bypassPermissions", "--output-format", "stream-json", "--verbose"]
        if settings is not None:
            argv += ["--settings", str(settings)]
        if resume:
            argv += ["--resume", resume]
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CONFIG_DIR"] = str(scratch.root / "claude-config")
        argv.append(prompt)
    elif host == "codex":
        assert codex_home is not None
        env["CODEX_HOME"] = str(codex_home)
        argv = ["codex", "exec"] + (["resume"] if resume else []) + [
            "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "-c", f"model_reasoning_effort={args.codex_reasoning}",
        ]
        if bypass_trust:
            argv.append("--dangerously-bypass-hook-trust")
        if resume:
            argv.append(resume)
        argv.append(prompt)
    else:
        argv = ["omp", "-p", "--mode", "json", "--session-dir", str(scratch.root / "omp-sessions")]
        if args.omp_model:
            argv += ["--model", args.omp_model]
        if extension is not None:
            argv += ["--extension", str(extension)]
        if resume:
            argv += ["--resume", resume]
        argv.append(prompt)
    stdout, stderr, returncode = run_host(
        argv, cwd=scratch.checkout, env=env, timeout=SESSION_TIMEOUT
    )
    return _keep_session(
        scratch, host, Session(stdout, stderr, _session_id(host, stdout), returncode)
    )


def run_host(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> tuple[str, str, int]:
    """host를 새 프로세스 그룹으로 띄우고 stdout, stderr, exit code를 돌려준다.

    host는 sandbox와 승인을 끈 채 돈다. 직접 자식만 끝내면 그 shell과 hook이 남아
    scratch를 지우는 동안에도 쓴다. 그래서 정상 종료가 아니면 그룹째 끝내고 회수한다.
    timeout은 exit 124로 돌려주고, Ctrl-C 같은 그 밖의 중단은 정리한 뒤 다시 올린다.
    """
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        try:
            out, err = process.communicate(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            _end_group(process)
            out, err = process.communicate()
            returncode = 124
    except BaseException:
        # timeout을 정리하던 중에 온 Ctrl-C도 여기로 온다. 회수하지 않으면 host가 좀비로 남는다.
        _end_group(process)
        process.wait()
        raise
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), returncode


def _end_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # 그룹이 이미 끝났다. macOS는 좀비만 남은 그룹에 EPERM을 돌려준다.
        pass


def _keep_session(scratch: Scratch, host: str, session: Session) -> Session:
    """host 출력을 scratch에 남긴다. `--keep`으로 판정 근거와 시작 실패의 stderr를 다시 볼 수 있다."""
    directory = scratch.root / "sessions"
    directory.mkdir(exist_ok=True)
    index = len(list(directory.glob(f"{host}-*.jsonl"))) + 1
    (directory / f"{host}-{index}.jsonl").write_text(session.stdout, encoding="utf-8")
    (directory / f"{host}-{index}.stderr.txt").write_text(session.stderr, encoding="utf-8")
    return session


def _host_events(
    scratch: Scratch, host: str, session: Session, session_id: str | None, options: dict
) -> list[dict]:
    return [
        *_json_lines(session.stdout),
        *_json_lines(_transcript(scratch, host, session_id, options)),
    ]


def _transcript(scratch: Scratch, host: str, session_id: str | None, options: dict) -> str:
    """Claude·Codex는 hook 피드백을 stdout JSON에 싣지 않고 세션 기록에만 남긴다."""
    if not session_id:
        return ""
    if host == "claude":
        root = (
            scratch.root / "claude-config"
            if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            else Path.home() / ".claude"
        )
        paths = list((root / "projects").glob(f"*/{session_id}.jsonl"))
    elif host == "codex" and options.get("codex_home") is not None:
        paths = list((options["codex_home"] / "sessions").rglob(f"*{session_id}*.jsonl"))
    else:
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths)


def _session_id(host: str, stdout: str) -> str | None:
    for event in _json_lines(stdout):
        if host == "claude" and isinstance(event.get("session_id"), str):
            return event["session_id"]
        if host == "codex" and event.get("type") == "thread.started":
            return event.get("thread_id")
        if host == "omp" and event.get("type") == "session":
            return event.get("id")
    return None


def _observation(scratch: Scratch) -> tuple[set[str], int]:
    return set(_binding_files(scratch)), len(_commands(scratch))


def _new_bindings(scratch: Scratch, before: tuple[set[str], int]) -> list[dict]:
    found = []
    for name, payload in _binding_files(scratch).items():
        if name not in before[0] and payload.get("run_id") == scratch.run_id:
            found.append(payload)
    return found


def _new_commands(scratch: Scratch, before: tuple[set[str], int]) -> list[dict]:
    return _commands(scratch)[before[1]:]


def _binding_files(scratch: Scratch) -> dict[str, dict]:
    directory = scratch.project / ".git" / "agent-flow" / "host-sessions"
    found: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            found[path.name] = payload
    return found


def _commands(scratch: Scratch) -> list[dict]:
    path = scratch.project / ".agent-flow" / "commands-run.jsonl"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(_json_lines(text))


def _codex_home(scratch: Scratch, *, host_label: str) -> Path | None:
    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if not source.is_file():
        return None
    home = scratch.root / host_label
    home.mkdir(exist_ok=True)
    shutil.copy2(source, home / "auth.json")
    return home


def _capture_events(host: str) -> tuple[str, str]:
    return ("tool_call", "tool_result") if host == "omp" else ("PreToolUse", "PostToolUse")


def _capture_hooks(captures: Path) -> dict:
    def entry(event: str) -> list[dict]:
        command = shlex.join([sys.executable, str(CAPTURE_HOOK), str(captures), event])
        return [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]

    return {"hooks": {"PreToolUse": entry("PreToolUse"), "PostToolUse": entry("PostToolUse")}}


def _omp_capture_extension(captures: Path) -> str:
    return (
        'import fs from "node:fs";\n'
        'import path from "node:path";\n'
        f"const DIR = {json.dumps(str(captures))};\n"
        "let count = 0;\n"
        "function save(kind, event, ctx) {\n"
        "  fs.mkdirSync(DIR, { recursive: true });\n"
        "  count += 1;\n"
        "  const name = kind + \"-\" + String(Date.now()) + \"-\" + String(count) + \".json\";\n"
        "  const sessionId = ctx?.sessionManager?.getSessionId?.() ?? null;\n"
        "  fs.writeFileSync(path.join(DIR, name), JSON.stringify(\n"
        "    { event, ctx: { cwd: ctx?.cwd ?? null, sessionId } }, null, 2));\n"
        "}\n"
        "export default function hostSmokeCapture(pi) {\n"
        '  pi.on("tool_call", async (event, ctx) => { save("tool_call", event, ctx); });\n'
        '  pi.on("tool_result", async (event, ctx) => { save("tool_result", event, ctx); });\n'
        "}\n"
    )


def _pick_capture(captures: Path, event: str, command: str) -> object | None:
    for path in sorted(captures.glob(f"{event}-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if command in json.dumps(payload) or command.replace("/private/", "/", 1) in json.dumps(payload):
            return payload
    return None


def _version(host: str) -> str:
    try:
        completed = subprocess.run([host, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or completed.stderr).strip().splitlines()[0] if (
        completed.stdout or completed.stderr
    ).strip() else ""


def _status(output: str) -> dict:
    line = [line for line in output.splitlines() if line.startswith("status_json:")][-1]
    return json.loads(line.removeprefix("status_json:"))


def _json_lines(text: str) -> Iterator[dict]:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def _git(cwd: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(("git", *args), cwd=cwd, env=env, check=True, capture_output=True, text=True)


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(
            f"host-smoke: {' '.join(argv[:3])} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout


if __name__ == "__main__":
    sys.exit(main())
