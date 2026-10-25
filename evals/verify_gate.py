"""Arm V's completion gate (#409 adversarial review: fix the adjudication, leave the procedure to the model).

`arm_compare.prepare --arm V` copies this file into `<run>/gate/` and wires it into the project as a Claude Code Stop
hook (`.claude/settings.json`, committed with the task), so the hook and its record stay out of the agent's diff:

    python3 -I <run>/gate/verify_gate.py <run>/gate/config.json      # stdin: the Stop hook's JSON input

On each Stop attempt it

1. runs the project's verification command (the config's, or `npm test --silent` when there is none and
   `package.json` has a test script) and blocks the stop with the tail of its output while it fails, at most
   `MAX_VERIFY_BLOCKS` times;
2. once that passes, runs one independent review (`claude -p`, read-only tools, on a throwaway copy of the project)
   and blocks once with its numbered findings;
3. after either cap it lets the stop through and records `gate_cap_reached`.

The review→fix rule is the kit arms' (#409 v3.18): the reviewer's findings are labelled by the frozen classifier
(`review_findings.LABEL_PROMPT`), which the harness runs on the host, unseen by the agent: during `drive` a
`LabelBroker` listens on an abstract unix socket (the address and a per-drive token in `<run>/gate/labels.json`) and
the hook sends it only the finding texts. The stop is blocked only for `incomplete`, `bug` and `architecture`
findings (`config["blocking_labels"]`), and the block lists only those, each tagged with its label; findings that are
all `style`, `design`, `procedure` or `other` let the stop through (`non_blocking`). When the labels cannot be had
(no broker, an error, a missing label) the hook blocks on every finding as before and records `label_error`. The
verification command logic is unchanged.

A review that fails (the call errors, times out, or its answer is neither a numbered list nor `NO_FINDINGS`) does not
pass silently: its row is recorded with `decision: gate-error` and `gate_error` (the stop goes through, the gate could
not judge), and `summary` counts it, so the run is reported as a gate error, not as a clean pass. The reviewer runs on
the config's model and effort (`--model`, `--effort`), and each review row records both.

It never edits the project: the reviewer works in a copy. It reads only project files, so it cannot reveal hidden
checks. Every attempt is one row in `<run>/verify-gate.jsonl`; the counters are read back from those rows. Only the
standard library is used, because the hook runs outside this repository.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "verify-gate-v3"
MAX_VERIFY_BLOCKS = 3
MAX_REVIEW_ROUNDS = 1
VERIFY_TIMEOUT = 900
REVIEW_TIMEOUT = 900
REVIEW_BUDGET_USD = 3.0
LABEL_TIMEOUT = 660
LABELS_CLIENT = "labels.json"
BLOCKING_LABELS = ("incomplete", "bug", "architecture")
# hook 하나가 검증과 리뷰와 분류를 모두 상한까지 쓸 수 있게 둔다. CLI 기본 hook 시한(60초)으로는 리뷰가 끝나지 않는다.
HOOK_TIMEOUT = VERIFY_TIMEOUT + REVIEW_TIMEOUT + LABEL_TIMEOUT + 300
TAIL_CHARS = 3000
REVIEW_TOOLS = ("Read", "Glob", "Grep", "Bash")
DENIED_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
NO_FINDINGS = "NO_FINDINGS"
LOG = "verify-gate.jsonl"
# `npm init`이 만드는 자리표시 test script는 테스트가 아니다.
NPM_PLACEHOLDER = "no test specified"

REVIEW_PROMPT = """You are an independent reviewer. The repository in the current directory holds a change that had to \
implement this task:

{task}

Task documents in the repository: {documents}

Verify the implementation against the task and those documents. Read the code, and run the app or the tests as \
needed. Do not modify any file.

Output only the concrete requirements the implementation does not meet, as a numbered list (one line each: the \
requirement and what is missing), or exactly `NO_FINDINGS` when there are none.
"""


def review_prompt(task: str, documents: list[str]) -> str:
    listed = ", ".join(f"`{name}`" for name in documents) if documents else "none (the task above is the whole task)"
    return REVIEW_PROMPT.format(task=task.strip(), documents=listed)


def allowlisted_env(parent: dict[str, str], env: dict) -> dict[str, str]:
    """The controller's environment (`arm_compare.controller_env`): the allowlist and the fixed extras, nothing else.

    The hook runs as a child of the controller, which adds its own session variables; none of them reach the
    verification command or the reviewer.
    """
    return {**{key: parent[key] for key in env["keys"] if key in parent}, **env["set"]}


def verify_command(config: dict, project: Path) -> list[str] | None:
    if config.get("verify_command"):
        return list(config["verify_command"])
    try:
        scripts = json.loads((project / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    return ["npm", "test", "--silent"] if isinstance(test, str) and test and NPM_PLACEHOLDER not in test else None


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int | None, str, bool]:
    """(exit code, stdout+stderr, timed out). The whole process group is stopped at the timeout."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return None, str(exc), False
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace"), True


def run_verify(argv: list[str], project: Path, env: dict[str, str]) -> dict:
    started = time.monotonic()
    code, output, timed_out = _run(argv, project, env, VERIFY_TIMEOUT)
    return {"exit": code, "timed_out": timed_out, "passed": code == 0 and not timed_out,
            "seconds": round(time.monotonic() - started, 1), "tail": output[-TAIL_CHARS:]}


def reviewer_argv(prompt: str, model: str, effort: str | None = None) -> list[str]:
    # 리뷰어는 어떤 설정 파일도 읽지 않는다. 사본에도 이 Stop hook이 있으므로 읽으면 리뷰어 안에서 gate가 다시 돈다.
    return ["claude", "-p", prompt, "--model", model, *(["--effort", effort] if effort else []), "--output-format", "json",
            "--tools", ",".join(REVIEW_TOOLS), "--allowedTools", ",".join(REVIEW_TOOLS),
            "--disallowedTools", ",".join(DENIED_TOOLS), "--permission-mode", "auto",
            "--setting-sources", "", "--settings", json.dumps({"disableAllHooks": True}),
            "--no-session-persistence", "--max-budget-usd", f"{REVIEW_BUDGET_USD:g}"]


def run_review(prompt: str, model: str, project: Path, env: dict[str, str], effort: str | None = None) -> dict:
    """One `claude -p` review on a throwaway copy of the project, so nothing it runs can change the agent's tree."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="verify-gate-review-") as scratch:
        copy = Path(scratch) / "project"
        shutil.copytree(project, copy, symlinks=True)
        code, output, timed_out = _run(reviewer_argv(prompt, model, effort), copy, env, REVIEW_TIMEOUT)
    seconds = round(time.monotonic() - started, 1)
    try:
        payload = json.loads(output)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return {"exit": code, "timed_out": timed_out, "seconds": seconds, "is_error": True, "text": None,
                "cost_usd": None, "usage": None, "num_turns": None, "error": output[-TAIL_CHARS:]}
    return {"exit": code, "timed_out": timed_out, "seconds": seconds, "is_error": bool(payload.get("is_error")),
            "text": payload.get("result") if isinstance(payload.get("result"), str) else None,
            "cost_usd": payload.get("total_cost_usd"), "usage": payload.get("usage"),
            "num_turns": payload.get("num_turns"), "error": None}


def parse_findings(text: str | None) -> list[str] | None:
    """Numbered findings, [] for `NO_FINDINGS`, or None when the answer is neither (it then blocks nothing)."""
    if not isinstance(text, str):
        return None
    findings = [line.strip() for line in text.splitlines() if re.match(r"^\s*\d+[.)]\s+\S", line)]
    if findings:
        return findings
    return [] if text.strip().strip("`").strip() == NO_FINDINGS else None


def read_events(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def verify_block_reason(argv: list[str], result: dict) -> str:
    status = "timed out" if result["timed_out"] else f"exited {result['exit']}"
    return (f"The project's verification command `{' '.join(argv)}` {status}. Make it pass before you finish.\n\n"
            f"Last output:\n```\n{result['tail']}\n```")


def review_block_reason(findings: list[str]) -> str:
    return ("An independent review of the project against the task found these unmet requirements:\n\n"
            + "\n".join(findings)
            + "\n\nAddress them before you finish, or say in your final message why one does not apply.")


# ─── labels: the harness's classifier on the host, reached through a socket ───

def _send_line(conn: socket.socket, data: dict) -> None:
    conn.sendall(json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n")


def _read_line(conn: socket.socket, limit: int = 4_000_000) -> bytes:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("message too long")
    return data.split(b"\n", 1)[0]


def label_findings(config: dict, findings: list[str], timeout: float = LABEL_TIMEOUT) -> dict:
    """{"labels": [{"label", "why"}, ...] in the findings' order, "cost_usd"} from the drive's `LabelBroker`, or
    {"error": ...}. Never raises."""
    path = Path(config.get("labels_client") or Path(config["run_dir"]) / "gate" / LABELS_CLIENT)
    try:
        client = json.loads(path.read_text(encoding="utf-8"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(b"\0" + str(client["socket"]).encode("utf-8"))
            _send_line(conn, {"token": client["token"], "findings": findings})
            reply = json.loads(_read_line(conn).decode("utf-8"))
    except (OSError, ValueError, KeyError) as failure:
        return {"error": f"{type(failure).__name__}: {failure}"[:200], "cost_usd": 0.0}  # nothing was asked
    labels = reply.get("labels") if isinstance(reply, dict) else None
    if not reply.get("ok") or not isinstance(labels, list) or len(labels) != len(findings) \
            or not all(isinstance(item, dict) and item.get("label") for item in labels):
        return {"error": str(reply.get("error") or "unlabelled findings")[:200], "cost_usd": reply.get("cost_usd")}
    return {"labels": labels, "cost_usd": reply.get("cost_usd")}


class LabelBroker:
    """Host side, held open by `drive` for a gate arm: `labeler(texts) -> {"labels": [...], "cost_usd"}` (the frozen
    classifier) answers the hook's requests; the address and a per-drive token go to `<gate_dir>/labels.json`."""

    def __init__(self, gate_dir: Path, labeler):
        self.gate_dir, self.labeler = gate_dir, labeler
        self.socket_name = f"arm-compare-gate-labels-{secrets.token_hex(8)}"
        self.token = secrets.token_hex(16)
        self.cost_usd = 0.0
        self.calls = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LabelBroker":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(b"\0" + self.socket_name.encode("utf-8"))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        (self.gate_dir / LABELS_CLIENT).write_text(json.dumps({"socket": self.socket_name, "token": self.token}) + "\n",
                                                  encoding="utf-8")
        self._thread = threading.Thread(target=self._loop, name="gate-label-broker", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(30)
                request = json.loads(_read_line(conn).decode("utf-8"))
                if not hmac.compare_digest(str(request.get("token") or ""), self.token):
                    _send_line(conn, {"ok": False, "error": "unknown token"})
                    return
                texts = [str(item) for item in request.get("findings") or []]
                with self._lock:
                    try:
                        answer = self.labeler(texts)
                    except Exception as failure:  # noqa: BLE001 - the hook falls back to blocking on every finding
                        answer = {"error": f"{type(failure).__name__}: {failure}"[:200], "cost_usd": 0.0}
                    self.calls += 1
                    self.cost_usd += float(answer.get("cost_usd") or 0.0)
                conn.settimeout(None)
                _send_line(conn, {"ok": "error" not in answer, **answer})
            except (OSError, ValueError):
                pass

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.close()
        (self.gate_dir / LABELS_CLIENT).unlink(missing_ok=True)


def decide(config: dict, hook_input: dict, *, verify=run_verify, review=run_review, label=label_findings,
           environ: dict[str, str] | None = None) -> tuple[dict, dict]:
    """(hook output, the event row) for one Stop attempt. `verify` and `review` are injectable for tests."""
    project = Path(config["project"])
    log = Path(config["run_dir"]) / LOG
    events = read_events(log)
    verify_blocks = sum(row.get("step") == "verify" and row.get("decision") == "block" for row in events)
    review_rounds = sum(row.get("step") == "review" for row in events)
    active = bool(hook_input.get("stop_hook_active"))
    env = allowlisted_env(dict(os.environ if environ is None else environ), config["env"])
    event = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "attempt": len(events) + 1,
             "stop_hook_active": active, "session_id": hook_input.get("session_id"), "verify_command": None,
             "verify": None, "review": None, "findings": None, "gate_cap_reached": None}
    # 다른 Stop hook이 막아 다시 멈추는 중인데 이 gate는 막은 적이 없다. 그 차단은 이 gate가 셀 수 없으므로 끼지 않는다.
    if active and not any(row.get("decision") == "block" for row in events):
        return {}, {**event, "step": "foreign-block", "decision": "allow"}
    argv = verify_command(config, project)
    event["verify_command"] = argv
    if argv is not None:
        result = verify(argv, project, env)
        event["verify"] = result
        if not result["passed"]:
            if verify_blocks >= MAX_VERIFY_BLOCKS:
                return {}, {**event, "step": "verify", "decision": "allow", "gate_cap_reached": "verify"}
            reason = verify_block_reason(argv, result)
            return {"decision": "block", "reason": reason}, {**event, "step": "verify", "decision": "block"}
    if review_rounds >= MAX_REVIEW_ROUNDS:
        # 리뷰는 한 번뿐이다. 그 리뷰가 지적을 냈다면 지적이 해소됐는지 다시 보지 않고 멈춤을 허락한다.
        flagged = any(row.get("step") == "review" and row.get("findings") for row in events)
        return {}, {**event, "step": "done", "decision": "allow", "gate_cap_reached": "review" if flagged else None}
    prompt = review_prompt(config["task"], config["documents"])
    effort = config.get("effort")
    outcome = review(prompt, config["model"], project, env, effort) if effort else review(prompt, config["model"],
                                                                                          project, env)
    findings = parse_findings(outcome.get("text")) if not outcome.get("is_error") else None
    event.update({"step": "review", "review": {key: value for key, value in outcome.items() if key != "text"},
                  "review_text": outcome.get("text"), "findings": None if findings is None else len(findings),
                  "model": config["model"], "effort": effort})
    if findings:
        labelled = label(config, findings)
        event["label_cost_usd"] = labelled.get("cost_usd")
        if "labels" not in labelled:
            # 분류를 얻지 못했으면 예전처럼 모든 지적으로 막는다. 기록에 남긴다.
            event["label_error"] = labelled.get("error") or "no labels"
            return {"decision": "block", "reason": review_block_reason(findings)}, {**event, "decision": "block"}
        blocking_labels = tuple(config.get("blocking_labels") or BLOCKING_LABELS)
        tagged = [{"text": text, "label": str(item["label"]), "why": item.get("why")}
                  for text, item in zip(findings, labelled["labels"])]
        counts: dict[str, int] = {}
        for item in tagged:
            counts[item["label"]] = counts.get(item["label"], 0) + 1
        blocking = [item for item in tagged if item["label"] in blocking_labels]
        event.update({"labels": tagged, "label_counts": counts, "blocking_findings": len(blocking)})
        if blocking:
            shown = [f"[{item['label']}] {item['text']}" for item in blocking]
            return {"decision": "block", "reason": review_block_reason(shown)}, {**event, "decision": "block"}
        return {}, {**event, "decision": "allow", "non_blocking": True}
    if findings is None:
        # 판정하지 못한 리뷰는 통과가 아니다. 멈춤은 허락하되 gate 오류로 남긴다.
        why = ("review-timed-out" if outcome.get("timed_out") else
               "review-error" if outcome.get("is_error") or outcome.get("text") is None else "review-unparsed")
        return {}, {**event, "decision": "gate-error", "gate_error": why}
    return {}, {**event, "decision": "allow"}


def append_event(config: dict, event: dict) -> None:
    with (Path(config["run_dir"]) / LOG).open("a", encoding="utf-8") as out:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")


def summary(log: Path) -> dict:
    """What `score` records for an arm V run."""
    events = read_events(log)
    reviews = [row for row in events if row.get("step") == "review"]
    costs = [(row.get("review") or {}).get("cost_usd") for row in reviews]
    # a row without the key had no label call (a gate before v3.18): 0, not unknown
    label_costs = [row.get("label_cost_usd", 0.0) for row in reviews if row.get("findings")]
    label_counts: dict[str, int] = {}
    for row in reviews:
        for name, count in (row.get("label_counts") or {}).items():
            label_counts[name] = label_counts.get(name, 0) + count
    usage: dict[str, int] = {}
    for row in reviews:
        for key, value in ((row.get("review") or {}).get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    return {"attempts": len(events),
            "blocks": sum(row.get("decision") == "block" for row in events),
            "verify_blocks": sum(row.get("step") == "verify" and row.get("decision") == "block" for row in events),
            "review_rounds": len(reviews),
            "review_findings": sum(row.get("findings") or 0 for row in reviews),
            "review_unparsed": sum(row.get("findings") is None for row in reviews),
            "gate_errors": sum(row.get("decision") == "gate-error" for row in events),
            "gate_error_reasons": [row.get("gate_error") for row in events if row.get("decision") == "gate-error"],
            "review_models": sorted({f"{row.get('model')}/{row.get('effort')}" for row in reviews}),
            # 리뷰가 없으면 비용은 0이다. 리뷰가 비용을 보고하지 않았으면 0이 아니라 null이다.
            "reviewer_cost_usd": (round(sum(costs), 4) if None not in costs else None) if reviews else 0.0,
            "reviewer_seconds": round(sum((row.get("review") or {}).get("seconds") or 0 for row in reviews), 1),
            "reviewer_usage": usage or None,
            "gate_cap_reached": next((row["gate_cap_reached"] for row in events if row.get("gate_cap_reached")), None),
            # 지적 분류(호스트의 분류기) 비용: 분류가 실패해 비용을 모르면 null이다.
            "label_cost_usd": round(sum(label_costs), 4) if None not in label_costs else None,
            "label_counts": label_counts, "label_errors": sum(bool(row.get("label_error")) for row in reviews),
            "non_blocking_stops": sum(bool(row.get("non_blocking")) for row in reviews)}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    config = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        hook_input = {}
    output, event = decide(config, hook_input if isinstance(hook_input, dict) else {})
    append_event(config, event)
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
