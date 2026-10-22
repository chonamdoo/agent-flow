"""The public half of oy-global-fullstack (#428) stays consistent: files, pinned data, ids."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "evals" / "ui-tasks" / "oy-global-fullstack"
MANIFEST = json.loads((TASK / "task.json").read_text(encoding="utf-8"))


def _interface_ids() -> set[str]:
    text = (TASK / "contract" / "interface.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| ") and "`" in line]
    return {match for row in rows for match in re.findall(r"`([a-z][a-z0-9-]*(?:<categoryId>)?)`", row.split("|")[2])}


def test_every_layer_file_exists_and_nothing_hidden_is_public():
    for files in MANIFEST["layers"].values():
        for name in files:
            assert (TASK / name).is_file(), name
    # 검사 내용은 비공개 평가기에만 둔다. 공개 저장소에 두면 에이전트가 그것에 맞춰 만든다.
    assert not (TASK / "acceptance.json").exists()
    assert not (TASK / "reference").exists()
    assert "prd.md" not in MANIFEST["layers"]["goal-only"]


def test_catalog_is_the_pinned_beauty_store_copy():
    entry = MANIFEST["fixtures"]["catalog.json"]
    data = (TASK / entry["from"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]


def test_commerce_data_covers_every_catalog_location():
    catalog = json.loads((TASK / MANIFEST["fixtures"]["catalog.json"]["from"]).read_text(encoding="utf-8"))
    commerce = json.loads((TASK / "fixtures" / "commerce.json").read_text(encoding="utf-8"))
    assert {location["id"] for location in catalog["locations"]} == set(commerce["shipping"])
    for location in catalog["locations"]:
        assert (commerce["shipping"][location["id"]] is not None) == location["shippable"]
        if location["currency"]:
            assert location["currency"] in commerce["currencies"]
    assert set(catalog["currencies"]) == set(commerce["currencies"])
    assert commerce["currencies"][commerce["baseCurrency"]]["perUsd"] == "1"


def test_prd_names_only_contract_test_ids():
    ids = _interface_ids()
    prd = (TASK / "prd.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z]+(?:-[a-z]+)+)`", prd)) - {"oy-global-fullstack", "beauty-store", "goal-only"}
    assert named - ids == set()


def test_openapi_contract_parses_and_names_every_api_path_the_prd_uses():
    spec = yaml.safe_load((TASK / "contract" / "openapi.yaml").read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.")
    prd = (TASK / "prd.md").read_text(encoding="utf-8")
    for path in re.findall(r"`(/api/[a-z/]+)`", prd):
        assert path in spec["paths"], path


def test_prefs_update_may_omit_the_currency_it_defaults():
    spec = yaml.safe_load((TASK / "contract" / "openapi.yaml").read_text(encoding="utf-8"))
    body = spec["paths"]["/api/prefs"]["put"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request = spec["components"]["schemas"][body.rsplit("/", 1)[-1]]
    assert request["required"] == ["country"]
    assert spec["components"]["schemas"]["Prefs"]["required"] == ["country", "currency"]
