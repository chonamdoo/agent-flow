from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from agent_flow.core.skill_plan import hash_skill_tree


ROOT = Path(__file__).resolve().parents[1]

MATT_V1_1_0_GRILLING = """---
name: grilling
description: Grill the user relentlessly about a plan or design. Use when the user wants to stress-test a plan before building, or uses any 'grill' trigger phrases.
---

Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time, waiting for feedback on each question before continuing. Asking multiple questions at once is bewildering.

If a *fact* can be found by exploring the codebase, look it up rather than asking me. The *decisions*, though, are mine — put each one to me and wait for my answer.

Do not enact the plan until I confirm we have reached a shared understanding.
"""


def test_matt_v1_1_0_grilling_is_vendored_exactly() -> None:
    lock = json.loads((ROOT / "skills" / "upstream-lock.json").read_text(encoding="utf-8"))
    grilling = (ROOT / "skills" / "grilling" / "SKILL.md").read_text(encoding="utf-8")

    assert lock["release"] == "v1.1.0"
    assert lock["release_commit"] == "d574778f94cf620fcc8ce741584093bc650a61d3"
    assert lock["exact_copies"]["grilling"] == "skills/productivity/grilling/SKILL.md"
    assert "grilling" not in lock["local_adaptations"]
    assert "grill-me" not in lock["local_adaptations"]
    assert grilling == MATT_V1_1_0_GRILLING


def test_matt_v1_1_0_review_records_both_grilling_guards() -> None:
    manifest = yaml.safe_load(
        (ROOT / "skills" / "skill-consolidation.yaml").read_text(encoding="utf-8")
    )
    adopted = manifest["upstream_review"]["mattpocock_skills_v1_1_0"]["adopted"]

    assert "grilling-confirmation" in adopted
    assert "grilling-facts-vs-decisions" in adopted


def test_matt_main_setup_and_local_ticket_updates_are_adapted() -> None:
    lock = json.loads((ROOT / "skills" / "upstream-lock.json").read_text(encoding="utf-8"))
    setup = (ROOT / "skills" / "setup-matt-pocock-skills" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    local_tracker = (
        ROOT / "skills" / "setup-matt-pocock-skills" / "issue-tracker-local.md"
    ).read_text(encoding="utf-8")

    assert lock["commit"] == "391a2701dd948f94f56a39f7533f8eea9a859c87"
    assert "Skip this section if `triage` is not installed" in setup
    assert "only when exploration found monorepo signals" in setup
    assert "one file per ticket" in local_tracker
    assert ".scratch/<feature-slug>/spec.md" in local_tracker


def test_matt_project_snapshots_match_committed_local_tree_hashes() -> None:
    lock = json.loads((ROOT / "skills" / "upstream-lock.json").read_text(encoding="utf-8"))
    exact_names = set(lock["exact_copies"])
    adapted_names = set(lock["local_adaptations"])

    assert lock["whole_tree_required"] is True
    assert exact_names.isdisjoint(adapted_names)
    assert set(lock["project_tree_hashes"]) == exact_names | adapted_names
    for name, expected_hash in lock["project_tree_hashes"].items():
        assert hash_skill_tree(ROOT / "skills" / name) == expected_hash


def test_android_official_project_snapshots_match_pinned_provenance() -> None:
    lock = json.loads((ROOT / "skills" / "upstream-lock.json").read_text(encoding="utf-8"))
    official = lock["android_official"]

    assert official["source"] == "https://github.com/android/skills"
    assert official["commit"] == "57ff3c7d02a53781954f9ce2df92f14b7fbb2ded"
    assert official["runtime_fetch"] is False
    assert official["policy"] == "offline-catalog-lock-and-indexed-project-snapshot"
    assert official["runtime_tree_verification"] == "installed-index"
    source_policy = yaml.safe_load(
        (ROOT / "skills" / "source-policy.yaml").read_text(encoding="utf-8")
    )["official_project_snapshots"]
    assert source_policy["commit"] == official["commit"]
    assert source_policy["runtime_fetch"] is False
    assert (ROOT / "profiles" / "android.yaml").read_bytes() == (
        ROOT / "src" / "agent_flow" / "profiles" / "android.yaml"
    ).read_bytes()
    profile = yaml.safe_load((ROOT / "profiles" / "android.yaml").read_text(encoding="utf-8"))
    official_catalog = {
        item["skill"] for item in profile["android_skills"]["implementation"]
    }
    assert official_catalog == set(official["snapshots"])
    assert "testing-localization" not in official_catalog
    assert {
        item["skill"]
        for item in profile["android_ecosystem_skills"]["implementation"]
    } == {"testing-localization"}
    license_reference = ROOT / "skills" / official["license_reference"]
    assert hashlib.sha256(license_reference.read_bytes()).hexdigest() == (
        official["license_sha256"]
    )
    for name, provenance in official["snapshots"].items():
        assert len(provenance["upstream_tree_hash"]) == 64
        assert len(provenance["upstream_skill_sha256"]) == 64
        if provenance["snapshot_mode"] == "install-time-indexed":
            assert provenance["project_tree_hash"] is None
            continue
        skill_root = ROOT / "skills" / name
        assert hash_skill_tree(skill_root) == provenance["project_tree_hash"]
        assert len((skill_root / "SKILL.md").read_text(encoding="utf-8").splitlines()) <= 200

    intent_reference = (
        ROOT
        / "skills"
        / "android-intent-security"
        / "references"
        / "official-guidance.md"
    )
    assert hashlib.sha256(intent_reference.read_bytes()).hexdigest() == (
        official["snapshots"]["android-intent-security"]["upstream_skill_sha256"]
    )
    for relative in (
        "scripts/orchestrator.py",
        "scripts/scanner.py",
        "scripts/generate_report.py",
        "resources/policies.json",
        "resources/scanner_config.json",
    ):
        assert (ROOT / "skills" / "play-policy-insights" / relative).is_file()
