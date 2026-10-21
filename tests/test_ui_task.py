from __future__ import annotations

import importlib.util
import json
import re
import shutil
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "evals" / "ui-tasks"
BEAUTY = TASKS / "beauty-store"
PRD = """# PRD

## Test-id contract

| Area | Test ids |
|---|---|
| Header | `search-input`, `cart-badge` |
| Listing | `filter-<categoryId>` |

## Spec Items

SPEC-1: Search.

SPEC-2: Filters.
"""


def load_module():
    spec = importlib.util.spec_from_file_location("ui_task", ROOT / "evals" / "ui_task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(cid: str, spec: str, *steps: dict) -> dict:
    return {"id": cid, "spec": spec, "title": cid, "steps": list(steps)}


def test_runner_step_kinds_match_validator() -> None:
    ui_task = load_module()
    runner = (ROOT / "evals" / "ui_task_runner.mjs").read_text(encoding="utf-8")
    assert set(re.findall(r'case "(\w+)":', runner)) == ui_task.STEP_KINDS


@pytest.mark.parametrize("task", sorted(p.name for p in TASKS.iterdir() if (p / "acceptance.json").is_file()))
def test_shipped_tasks_validate(task: str) -> None:
    ui_task = load_module()
    prd, acceptance = ui_task.load_task(TASKS / task)
    assert ui_task.validate(prd, acceptance) == []


def test_validate_accepts_contract_ids_and_placeholders() -> None:
    ui_task = load_module()
    acceptance = {"checks": [
        check("a", "SPEC-1", {"goto": "/"}, {"fill": "search-input", "value": "x"},
              {"expectText": {"testid": "cart-badge", "within": {"testid": "search-input"}}, "equals": "1"}),
        check("b", "SPEC-2", {"check": "filter-111"}, {"expectUrl": True, "equals": "/"}),
    ]}
    assert ui_task.validate(PRD, acceptance) == []


def test_validate_reports_each_kind_of_problem() -> None:
    ui_task = load_module()
    acceptance = {"checks": [
        check("a", "SPEC-1", {"click": "buy-now"}),
        check("a", "SPEC-9", {"hover": "search-input"}),
        check("c", "SPEC-1", {"expectText": {"testid": "cart-badge", "within": {"testid": "side-panel"}}}),
        check("d", "SPEC-1"),
        check("e", "SPEC-1", {"expectSorted": "cart-badge", "by": ["search-input", "card-price"], "order": "asc"}),
    ]}
    problems = ui_task.validate(PRD, acceptance)
    assert "a step 0: test id 'buy-now' is not in the prd.md contract" in problems
    assert "a: duplicate check id" in problems
    assert "a: spec 'SPEC-9' is not a Spec Item in prd.md" in problems
    assert any(p.startswith("a step 0: unknown or ambiguous step") for p in problems)
    assert "c step 0: test id 'side-panel' is not in the prd.md contract" in problems
    assert "d: no steps" in problems
    assert "e step 0: test id 'card-price' is not in the prd.md contract" in problems
    assert "SPEC-2: no acceptance check" in problems


def test_merge_runs_requires_every_run_to_pass() -> None:
    ui_task = load_module()
    ok = {"id": "a", "spec": "SPEC-1", "title": "a", "passed": True, "failure": None, "page_errors": [], "ms": 1}
    bad = {**ok, "passed": False, "failure": {"step": 2, "action": {"click": "x"}, "message": "boom"}}
    merged = ui_task.merge_runs([{"results": [ok, dict(ok, id="b", spec="SPEC-2")]},
                                 {"results": [bad, dict(ok, id="b", spec="SPEC-2")]}])
    assert [(row["id"], row["passed"], row["passes"], row["runs"]) for row in merged] == [
        ("a", False, 1, 2), ("b", True, 2, 2)]
    assert merged[0]["failure"]["message"] == "boom"
    totals = ui_task.score(merged, ["SPEC-1", "SPEC-2", "SPEC-3"])
    assert (totals["checks_passed"], totals["checks_total"], totals["specs_passed"], totals["specs_total"]) == (1, 2, 1, 3)
    text = ui_task.summary_markdown("t", "build", totals, merged)
    assert "| SPEC-3 | 0/0 |" in text and "`a` (SPEC-1, 1/2 runs): step 2" in text


def test_static_server_falls_back_to_index_and_serves_fixtures(tmp_path: Path) -> None:
    ui_task = load_module()
    build, fixtures = tmp_path / "build", tmp_path / "fixtures"
    build.mkdir()
    fixtures.mkdir()
    (build / "index.html").write_text("<p>app</p>", encoding="utf-8")
    (build / "app.js").write_text("// js", encoding="utf-8")
    (fixtures / "catalog.json").write_text('{"ok": true}', encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    server, base = ui_task.serve_static(build, fixtures)
    try:
        def get(path: str) -> str:
            with urllib.request.urlopen(base + path) as response:  # noqa: S310 - local test server
                return response.read().decode()
        assert get("/category/100?sort=x") == "<p>app</p>"
        assert get("/app.js") == "// js"
        assert json.loads(get("/fixtures/catalog.json")) == {"ok": True}
        assert get("/fixtures/../secret.txt") == "<p>app</p>"
    finally:
        server.shutdown()


def test_catalog_is_reproducible_and_holds_the_fixed_cases(tmp_path: Path) -> None:
    generator = BEAUTY / "fixtures" / "generate.py"
    copy = tmp_path / "generate.py"
    shutil.copy(generator, copy)
    spec = importlib.util.spec_from_file_location("generate", copy)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    assert (tmp_path / "catalog.json").read_text(encoding="utf-8") == (
        BEAUTY / "fixtures" / "catalog.json").read_text(encoding="utf-8")
    catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    products = {p["id"]: p for p in catalog["products"]}
    assert [o["stock"] for o in products["P001"]["options"]] == [50, 3, 0]
    assert products["P002"]["options"] == [] and products["P002"]["salePrice"] == 18.0
    skincare = sum(p["categoryId"].startswith("1") for p in catalog["products"])
    assert skincare == 52 and catalog["popularSearches"][0] == "toner"


def test_run_rejects_an_unknown_spec_item(capsys) -> None:
    ui_task = load_module()
    with pytest.raises(SystemExit):
        ui_task.main(["run", str(BEAUTY), "--url", "http://127.0.0.1:9", "--spec", "SPEC-99"])
    assert "unknown Spec Item(s): SPEC-99" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("node") is None or not (BEAUTY / "reference" / "index.html").is_file(),
                    reason="needs node and the reference build")
def test_reference_build_passes_every_check(tmp_path: Path) -> None:
    ui_task = load_module()
    try:
        status = ui_task.main(["run", str(BEAUTY), "--static", str(BEAUTY / "reference"),
                               "--json", str(tmp_path / "result.json")])
    except RuntimeError as error:
        if "playwright is not installed" in str(error) or "Executable doesn't exist" in str(error):
            pytest.skip(str(error)[:200])
        raise
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert status == 0, [row for row in result["results"] if not row["passed"]]
    assert result["checks_passed"] == result["checks_total"] == len(
        json.loads((BEAUTY / "acceptance.json").read_text(encoding="utf-8"))["checks"])
