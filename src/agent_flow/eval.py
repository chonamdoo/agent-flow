from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class EvalResult:
    fixture_id: str
    passed: bool
    reason: str
    artifact: str | None = None


def run_eval(
    *,
    root: Path,
    fixture_path: Path | None = None,
    judge_command: tuple[str, ...] | None = None,
    run_dir: Path | None = None,
) -> list[EvalResult]:
    fixtures = _load_fixtures(fixture_path or root / ".agent-flow" / "evals")
    results = [_run_fixture(root=root, fixture=fixture, judge_command=judge_command) for fixture in fixtures]
    output_dir = run_dir if run_dir is not None else root / ".agent-flow" / "eval"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        f"{json.dumps([asdict(result) for result in results], indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return results


def _load_fixtures(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    paths = [path] if path.is_file() else sorted([*path.glob("*.json"), *path.glob("*.yaml"), *path.glob("*.yml")])
    fixtures: list[dict[str, Any]] = []
    for fixture_file in paths:
        payload = _read_payload(fixture_file)
        if isinstance(payload, list):
            fixtures.extend(_fixture_with_source(item, fixture_file) for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            items = payload.get("fixtures")
            if isinstance(items, list):
                fixtures.extend(_fixture_with_source(item, fixture_file) for item in items if isinstance(item, dict))
            else:
                fixtures.append(_fixture_with_source(payload, fixture_file))
    return fixtures


def _read_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _fixture_with_source(fixture: dict[str, Any], source: Path) -> dict[str, Any]:
    copy = dict(fixture)
    copy.setdefault("id", source.stem)
    copy["_source"] = str(source)
    return copy


def _run_fixture(*, root: Path, fixture: dict[str, Any], judge_command: tuple[str, ...] | None) -> EvalResult:
    fixture_id = str(fixture.get("id", "fixture"))
    actual = str(fixture.get("actual", ""))
    expected = fixture.get("expected")
    rubric = fixture.get("rubric", [])
    failures: list[str] = []
    if expected is not None and actual.strip() != str(expected).strip():
        failures.append("expected output mismatch")
    if isinstance(rubric, list):
        failures.extend(_check_rubric(actual, rubric))
    if judge_command is not None:
        judge_passed, judge_reason = _run_judge(root=root, command=judge_command, fixture=fixture)
        if not judge_passed:
            failures.append(judge_reason)
    return EvalResult(
        fixture_id=fixture_id,
        passed=not failures,
        reason="passed" if not failures else "; ".join(failures),
        artifact=fixture.get("_source"),
    )


def _check_rubric(actual: str, rubric: list[Any]) -> list[str]:
    failures: list[str] = []
    for index, item in enumerate(rubric):
        if not isinstance(item, dict):
            failures.append(f"rubric[{index}] must be an object")
            continue
        criterion = str(item.get("id") or item.get("criterion") or f"rubric[{index}]")
        must_include = item.get("must_include")
        if must_include is not None and str(must_include) not in actual:
            failures.append(f"{criterion}: missing {must_include!r}")
    return failures


def _run_judge(*, root: Path, command: tuple[str, ...], fixture: dict[str, Any]) -> tuple[bool, str]:
    payload = json.dumps(fixture, ensure_ascii=False)
    try:
        completed = subprocess.run(
            command,
            input=payload,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"judge failed: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return False, f"judge exit {completed.returncode}: {detail}"
    text = completed.stdout.strip()
    try:
        verdict = json.loads(text)
    except json.JSONDecodeError:
        return False, "judge output must be JSON object with boolean passed"
    if not isinstance(verdict, dict):
        return False, "judge output must be JSON object with boolean passed"
    raw_passed = verdict.get("passed", False)
    if not isinstance(raw_passed, bool):
        return False, "judge passed must be boolean"
    passed = raw_passed
    return passed, str(verdict.get("reason", "judge passed" if passed else "judge failed"))
