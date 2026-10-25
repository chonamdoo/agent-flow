"""A simulated product owner who answers the agent's requirement questions from a hidden list of atomic facts.

`arm_compare.prepare` installs it for a case or layer that sets `user_sim: {"oracle": "<path in the case dir>"}`:
`install` copies this file and the oracle into `<run>/user-sim/` (outside the project) and writes an executable
`<run>/user-sim/ask-user`. Every arm's prompt then says it may run that script; the agent's question goes to

    python3 -I <run>/user-sim/user_sim.py ask <run>/user-sim/config.json "<question>"

The oracle is a facts file (`{"facts": [{"id", "ko", "en"?, "topic"?, "keywords"?, "rule"?}]}`), or a Markdown list
that is split into one fact per top-level bullet (indented lines stay with their bullet). One question is answered in
two separate `claude -p` calls (the configured model, effort `low` through `CLAUDE_CODE_EFFORT_LEVEL`, no tools, no
settings, no session, an allowlisted environment, a temporary empty working directory, JSON output, `BUDGET_USD` and
`TIMEOUT` per call):

1. **select**: a fixed prompt with every fact (id and text) returns the ids of the facts that directly answer some
   part of the question, as JSON. Unknown ids are dropped.
2. **answer**: a fixed prompt with only the selected facts phrases the answer in the question's language (Hangul →
   Korean, kana → Japanese, otherwise English), answers each part the facts do not settle with the fixed undecided
   sentence in that language, and volunteers nothing.

Nothing selected → the undecided sentence in the question's language, with no second call. The answering call never
sees an unselected fact, so it cannot volunteer one; `disclosed_fact_ids` (the facts shown to it) is logged per call.
Selection is a model call rather than keyword retrieval because the questions come in either language and in free
phrasing (Korean particles, synonyms, compound questions) where keywords miss facts or drag in neighbours; the
selection prompt and its output are fixed and logged, so a disclosure can be audited and joined to the checks through
the fact ids (`<rule id>.<n>`, the rule ids of the case's map from oracle rules to hidden checks).

The answer is printed on stdout. Every question is one row in `<run>/user-sim.jsonl` (question, language, answer,
disclosed fact ids, both calls with their cost). `count_questions` counts question marks, at least one per non-empty
question. Only the standard library is used, because the script runs outside this repository.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "user-sim-v2"
MODEL = "claude-opus-5-5"
EFFORT = "low"
TIMEOUT = 300
BUDGET_USD = 1.0
LOG = "user-sim.jsonl"
DIR = "user-sim"
SCRIPT = "ask-user"
# The copy keeps the source bytes (facts JSON or Markdown); the name has no extension so path markers match either.
ORACLE = "oracle"
CONFIG = "config.json"
UNDECIDED_BY_LANGUAGE = {"ko": "정해진 바 없습니다. 합리적으로 정해 주세요.",
                         "en": "That has not been decided. Please decide it reasonably yourself.",
                         "ja": "決まっていません。妥当に決めてください。"}
UNDECIDED = UNDECIDED_BY_LANGUAGE["ko"]
LANGUAGE_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese"}
FAILED = "The product owner could not answer this time. Decide it reasonably yourself."

SELECT_PROMPT = """You match a developer's question to the decisions a product owner has already made. The decisions \
are listed below as <fact id="...">...</fact>; they are written in Korean, the question may be in any language.

Return the ids of the facts that directly answer some part of the question: the fact states the very behaviour, \
value, limit, order or rule that the question asks about. Leave out facts that are only on a related topic, that \
give background, or that answer something the question did not ask. When the question asks several things, include \
the facts for each part. When no fact answers it, return an empty list.

Output exactly one JSON object and nothing else: {"fact_ids": ["<id>", ...]}
"""

ANSWER_PROMPT = """You are the product owner of a software product. A developer building it asks you a question. \
What you have decided that bears on this question is written below between <decided> and </decided>.

Rules:
- Answer only what is asked, concisely, and consistently with what you have decided. Leave out any decided detail \
the question did not ask about.
- When what you have decided does not settle a question or a part of it, answer that part with exactly: {undecided}
- Answer in {language}, the language of the question, even when your decisions are written in another language.
- Never mention facts, ids, files, documents or that your decisions are written down anywhere; answer as a person \
who simply knows what they want.
- When several questions are asked, answer each in turn, in the same order and numbering.
"""


# ─── oracle ───

def facts_from_markdown(text: str) -> list[dict]:
    """One fact per top-level bullet (or paragraph line); indented lines join their bullet; `##` sets the topic."""
    facts: list[dict] = []
    topic = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.startswith("#"):
            topic = stripped.lstrip("#").strip() if stripped.startswith("##") else topic
            continue
        if line.startswith((" ", "\t")) and facts:
            facts[-1]["ko"] += "\n" + stripped
            continue
        facts.append({"id": f"F{len(facts) + 1:02d}", "ko": re.sub(r"^[-*]\s+", "", stripped),
                      **({"topic": topic} if topic else {})})
    return facts


def load_facts(text: str) -> list[dict]:
    """Facts from the oracle copy: a facts JSON object or list, else Markdown."""
    try:
        data = json.loads(text)
    except ValueError:
        return facts_from_markdown(text)
    items = data.get("facts") if isinstance(data, dict) else data
    facts = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id") and (item.get("ko") or item.get("text")):
            facts.append({**item, "id": str(item["id"]), "ko": str(item.get("ko") or item.get("text"))})
    if not facts:
        raise ValueError("the oracle has no facts")
    return facts


def language(text: str) -> str:
    if re.search(r"[가-힣ᄀ-ᇿ㄰-㆏]", text):
        return "ko"
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    return "en"


def _fact_block(fact: dict, with_id: bool) -> str:
    body = fact["ko"].strip()
    if fact.get("en"):
        body += f"\n(English: {str(fact['en']).strip()})"
    if fact.get("topic"):
        body = f"[{fact['topic']}] {body}"
    return f'<fact id="{fact["id"]}">{body}</fact>' if with_id else f"- {body}"


def select_prompt(facts: list[dict]) -> str:
    return SELECT_PROMPT + "\n<facts>\n" + "\n".join(_fact_block(fact, True) for fact in facts) + "\n</facts>\n"


def answer_prompt(facts: list[dict], lang: str) -> str:
    rules = ANSWER_PROMPT.format(undecided=UNDECIDED_BY_LANGUAGE.get(lang, UNDECIDED_BY_LANGUAGE["en"]),
                                 language=LANGUAGE_NAMES.get(lang, "the language of the question"))
    return rules + "\n<decided>\n" + "\n".join(_fact_block(fact, False) for fact in facts) + "\n</decided>\n"


def parse_selection(text: str, known: list[str]) -> tuple[list[str] | None, list[str]]:
    """(selected ids in oracle order, ids the model named that do not exist); None when no JSON object came back."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("fact_ids"), list):
        return None, []
    named = [str(item) for item in data["fact_ids"]]
    return [fact_id for fact_id in known if fact_id in named], [item for item in named if item not in known]


# ─── calls ───

def argv(system: str, model: str) -> list[str]:
    # 질문은 stdin으로 넘긴다. `-`로 시작하는 질문이 옵션으로 읽히지 않는다. 도구도 설정도 MCP도 세션도 없다.
    return ["claude", "-p", "--model", model, "--output-format", "json", "--system-prompt", system,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence",
            "--max-budget-usd", f"{BUDGET_USD:g}"]


def allowlisted_env(parent: dict[str, str], env: dict) -> dict[str, str]:
    """The controller's allowlist and fixed extras only; the controller's own session variables do not reach it."""
    return {**{key: parent[key] for key in env["keys"] if key in parent}, **env["set"]}


def count_questions(text: str) -> int:
    if not text.strip():
        return 0
    return max(1, len(re.findall(r"[?？]", text)))


def _call(command: list[str], stdin: str, cwd: Path, env: dict[str, str],
          timeout: int) -> tuple[int | None, str, str, bool]:
    """(exit code, stdout, stderr, timed out). The whole process group is stopped at the timeout."""
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return None, "", str(exc), False
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        timed_out = True
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out


def _model_call(stage: str, system: str, stdin: str, config: dict, env: dict[str, str], call) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="user-sim-") as scratch:
        code, stdout, stderr, timed_out = call(argv(system, config.get("model") or MODEL), stdin, Path(scratch),
                                               env, TIMEOUT)
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    text = payload.get("result") if isinstance(payload.get("result"), str) else None
    ok = not timed_out and code == 0 and not payload.get("is_error") and bool(text and text.strip())
    models = payload.get("modelUsage")
    return {"stage": stage, "ok": ok, "text": text.strip() if ok else None, "exit": code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 1), "cost_usd": payload.get("total_cost_usd"),
            "usage": payload.get("usage"), "models": sorted(models) if isinstance(models, dict) else None,
            "error": None if ok else (("timed out" if timed_out else f"exit {code}") + f": {stderr[-400:]}"
                                      # the CLI's own error result (a usage limit, an outage) is kept for `outage`
                                      + (f" | {str(payload.get('result'))[:300]}" if payload.get("is_error") else ""))}


def _sum_usage(usages: list) -> dict | None:
    dicts = [usage for usage in usages if isinstance(usage, dict)]
    if not dicts:
        return None
    total: dict = {}
    for usage in dicts:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total


def _is_undecided(answer: str) -> bool:
    return any(sentence in answer for sentence in UNDECIDED_BY_LANGUAGE.values())


def ask(question: str, config: dict, *, call=_call, environ: dict[str, str] | None = None) -> dict:
    """One answer, logged to `<run_dir>/user-sim.jsonl`. `call` is injectable for tests."""
    oracle = Path(config["oracle"]).read_text(encoding="utf-8")
    facts = load_facts(oracle)
    by_id = {fact["id"]: fact for fact in facts}
    effort = config.get("effort") or EFFORT
    env = {**allowlisted_env(dict(os.environ if environ is None else environ), config["env"]),
           "CLAUDE_CODE_EFFORT_LEVEL": effort}
    lang = language(question)
    started = time.monotonic()
    calls = [_model_call("select", select_prompt(facts), f"<question>\n{question}\n</question>", config, env, call)]
    selected, invalid = parse_selection(calls[0]["text"] or "", list(by_id)) if calls[0]["ok"] else (None, [])
    answer, error = None, calls[0]["error"]
    if calls[0]["ok"] and selected is None:
        error = f"selection was not a JSON object with fact_ids: {calls[0]['text'][:200]!r}"
    elif selected == []:
        answer = UNDECIDED_BY_LANGUAGE.get(lang, UNDECIDED_BY_LANGUAGE["en"])
    elif selected:
        calls.append(_model_call("answer", answer_prompt([by_id[i] for i in selected], lang), question, config, env,
                                 call))
        answer, error = calls[-1]["text"], calls[-1]["error"]
    ok = answer is not None
    costs = [item["cost_usd"] for item in calls]
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": VERSION,
           "model": config.get("model") or MODEL, "effort": effort, "question": question,
           "questions": count_questions(question), "language": lang,
           "answer": answer if ok else None, "answer_language": language(answer) if ok else None,
           "undecided": bool(ok and _is_undecided(answer)),
           "disclosed_fact_ids": selected if ok else [],
           "disclosed_rules": sorted({str(by_id[i].get("rule") or i.split(".")[0]) for i in selected}) if ok else [],
           "invalid_fact_ids": invalid, "ok": ok, "exit": calls[-1]["exit"], "timed_out": calls[-1]["timed_out"],
           "seconds": round(time.monotonic() - started, 1),
           # 호출 하나라도 비용을 보고하지 않으면 합이 아니라 null이다.
           "cost_usd": round(sum(costs), 6) if None not in costs else None,
           "usage": _sum_usage([item["usage"] for item in calls]),
           "models_used": sorted({model for item in calls for model in item["models"] or []}),
           "calls": [{key: value for key, value in item.items() if key != "text"} for item in calls],
           "oracle_sha256": hashlib.sha256(oracle.encode("utf-8")).hexdigest(), "error": None if ok else error}
    with (Path(config["run_dir"]) / LOG).open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# ─── log ───

def read_log(log: Path) -> list[dict]:
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


def summary(log: Path) -> dict:
    """What `score` records: calls, questions, whether the agent asked at all, disclosed facts and the cost."""
    rows = read_log(log)
    costs = [row.get("cost_usd") for row in rows]
    disclosed = sorted({fact_id for row in rows if row.get("ok") for fact_id in row.get("disclosed_fact_ids") or []})
    return {"ask_user_calls": len(rows), "ask_user_questions": sum(row.get("questions") or 0 for row in rows),
            "asked_any": bool(rows), "ask_user_failed": sum(not row.get("ok") for row in rows),
            "ask_user_undecided": sum(bool(row.get("undecided")) for row in rows),
            "ask_user_disclosed_fact_ids": disclosed,
            # 호출이 없으면 0이다. 비용을 보고하지 않은 호출이 있으면 0이나 부분합이 아니라 null이다.
            "user_sim_cost_usd": (round(sum(costs), 4) if None not in costs else None) if rows else 0.0}


# ─── install ───

def install(run_dir: Path, oracle: Path, env: dict, model: str) -> dict:
    """`<run>/user-sim/` with this script, the oracle copy, the config and the `ask-user` wrapper; the record."""
    target = run_dir / DIR
    target.mkdir()
    script = target / "user_sim.py"
    shutil.copy2(Path(__file__), script)
    oracle_copy = target / ORACLE
    shutil.copy2(oracle, oracle_copy)
    facts = load_facts(oracle_copy.read_text(encoding="utf-8"))
    config = {"version": VERSION, "run_dir": str(run_dir.resolve()), "oracle": str(oracle_copy.resolve()),
              "model": model, "effort": EFFORT, "env": env}
    (target / CONFIG).write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
    wrapper = target / SCRIPT
    wrapper.write_text(f"#!/bin/sh\nexec python3 -I {shlex.quote(str(script.resolve()))} ask "
                       f"{shlex.quote(str((target / CONFIG).resolve()))} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return {"version": VERSION, "ask_user": str(wrapper.resolve()),
            "oracle_sha256": hashlib.sha256(oracle_copy.read_bytes()).hexdigest(), "facts": len(facts),
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "budget_usd": BUDGET_USD, "timeout_seconds": TIMEOUT, "effort": EFFORT,
            "undecided_answer": UNDECIDED, "undecided_answers": UNDECIDED_BY_LANGUAGE}


def set_model(run_dir: Path, model: str) -> None:
    """The simulator runs on the controller's model, which only `drive` knows."""
    path = run_dir / DIR / CONFIG
    config = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**config, "model": model}, indent=1) + "\n", encoding="utf-8")


def main(args: list[str]) -> int:
    if len(args) < 2 or args[0] != "ask":
        print("usage: user_sim.py ask <config.json> <question...>", file=sys.stderr)
        return 2
    config = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    question = " ".join(args[2:]).strip() or sys.stdin.read().strip()
    if not question:
        print("ask-user: give the question as an argument", file=sys.stderr)
        return 2
    row = ask(question, config)
    if not row["ok"]:
        print(FAILED)
        return 1
    print(row["answer"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
