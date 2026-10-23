"""Workflow variants for the reduced-workflow search (#409 program v2 §5, Q2 autoresearch).

    python evals/variants.py show '{"planning": false, "reviewers": 2}'

A variant is a point in a fixed, discrete space; there is no free-text prompt editing, so the search cannot tune
wording toward the evaluator. Every variant is derived from the kit's own `full-feature` workflow and lands in the
project only: the project's installed runtime (`.agent-flow/runtime/python/agent_flow/workflows/<name>.yaml`, where
`agent-flow run --workflow` looks; a copy also goes to `.agent-flow/workflows/` for `declared_phases`) and, for the
reviewer count, `.agent-flow/profiles/<profile>.local.yaml`. The runner pins the definition at run start. The kit
source is not changed; the variant is a new file beside the shipped workflows, none of which is touched.

Dimensions (absent keys take the `full-feature` value):
- planning: False drops domain-grill, product-brief, prd, slice-plan and plan-review.
- design: False drops ddd-design.
- tdd: False drops red (green still writes the tests with the code).
- architecture_review: False drops architecture-review.
- reviewers: 2 or 5 keeps that many reviewers (the generalist plus the profile's first N-1 angles); absent keeps
  the profile's full list.
The fix-loop cap, gates and delivery phases are never changed.

A dropped phase's place in the order and every route that pointed at it go to the next phase that is kept, so the
remaining graph is the original one with that step removed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = "full-feature"
DROPS = {
    "planning": ("domain-grill", "product-brief", "prd", "slice-plan", "plan-review"),
    "design": ("ddd-design",),
    "tdd": ("red",),
    "architecture_review": ("architecture-review",),
}
REVIEWER_COUNTS = (2, 5)
KEYS = (*DROPS, "reviewers")


def normalize(spec: dict) -> dict:
    """The variant with its keys checked; the name is derived from it, so equal specs share one name."""
    unknown = sorted(set(spec) - set(KEYS))
    if unknown:
        raise ValueError(f"unknown variant keys: {unknown}")
    out: dict = {}
    for key in DROPS:
        if key in spec:
            if not isinstance(spec[key], bool):
                raise ValueError(f"{key} must be true or false")
            if spec[key] is False:
                out[key] = False
    if "reviewers" in spec:
        if spec["reviewers"] not in REVIEWER_COUNTS:
            raise ValueError(f"reviewers must be one of {REVIEWER_COUNTS}")
        out["reviewers"] = spec["reviewers"]
    return out


def name(spec: dict) -> str:
    digest = hashlib.sha256(json.dumps(normalize(spec), sort_keys=True).encode("utf-8")).hexdigest()
    return f"s-{digest[:10]}"


def base_definition(kit: Path) -> dict:
    return yaml.safe_load((kit / "src" / "agent_flow" / "workflows" / f"{BASE}.yaml").read_text(encoding="utf-8"))


def workflow(kit: Path, spec: dict) -> dict:
    spec = normalize(spec)
    definition = base_definition(kit)
    dropped = {phase for key, phases in DROPS.items() if spec.get(key) is False for phase in phases}
    order = [phase["id"] for phase in definition["phases"]]
    missing = sorted(dropped - set(order))
    if missing:
        raise ValueError(f"{BASE} no longer has phases {missing}")

    def successor(phase_id: str) -> str:
        index = order.index(phase_id)
        for later in order[index + 1:]:
            if later not in dropped:
                return later
        raise ValueError(f"no phase left after {phase_id}")

    def target(phase_id: str) -> str:
        return successor(phase_id) if phase_id in dropped else phase_id

    kept = []
    for phase in definition["phases"]:
        if phase["id"] in dropped:
            continue
        phase = dict(phase)
        if isinstance(phase.get("routes"), dict):
            phase["routes"] = {verdict: target(to) if to in order else to for verdict, to in phase["routes"].items()}
        kept.append(phase)
    variant_name = name(spec)
    return {**definition, "id": variant_name,
            "description": f"{definition.get('description', BASE)} (search variant {json.dumps(spec, sort_keys=True)})",
            "phases": kept}


def profile_override(kit: Path, profile: str, spec: dict) -> dict | None:
    """The reviewer count as a project override of the profile's angles; None keeps the profile's list."""
    count = normalize(spec).get("reviewers")
    if count is None:
        return None
    shipped = yaml.safe_load((kit / "src" / "agent_flow" / "profiles" / f"{profile}.yaml").read_text(encoding="utf-8"))
    angles = list(shipped.get("review_angles") or [])
    if len(angles) < count - 1:
        raise ValueError(f"profile {profile} has {len(angles)} angles; {count} reviewers need {count - 1}")
    return {"review_angles": angles[:count - 1]}


def write(kit: Path, project: Path, profile: str, spec: dict) -> dict:
    """Writes the variant into the project and returns what the run record keeps."""
    definition = workflow(kit, spec)
    text = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True)
    runtime = project / ".agent-flow" / "runtime" / "python" / "agent_flow" / "workflows"
    if not runtime.is_dir():
        raise ValueError(f"no installed runtime workflows in {project}: install the kit first")
    for directory in (runtime, project / ".agent-flow" / "workflows"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{definition['id']}.yaml").write_text(text, encoding="utf-8")
    override = profile_override(kit, profile, spec)
    override_sha = None
    if override is not None:
        path = project / ".agent-flow" / "profiles" / f"{profile}.local.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        merged = {**(existing or {}), **override}
        override_text = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
        path.write_text(override_text, encoding="utf-8")
        override_sha = hashlib.sha256(override_text.encode("utf-8")).hexdigest()
    return {"name": definition["id"], "spec": normalize(spec), "base": BASE,
            "workflow_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "phases": [phase["id"] for phase in definition["phases"]], "profile_override_sha256": override_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show")
    show.add_argument("spec")
    args = parser.parse_args(argv)
    spec = json.loads(args.spec)
    definition = workflow(ROOT, spec)
    print(json.dumps({"name": definition["id"], "phases": [phase["id"] for phase in definition["phases"]]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
