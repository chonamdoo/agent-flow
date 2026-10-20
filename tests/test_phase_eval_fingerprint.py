from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("fingerprint_test", ROOT / "evals/phase_eval_fingerprint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_evaluator_tree_retains_recorded_single_file_digest(tmp_path):
    directory = tmp_path / "evals"
    directory.mkdir()
    body = b"legacy evaluator bytes\n"
    (directory / "phase_eval.py").write_bytes(body)
    assert _module().evaluator_sha256(tmp_path) == hashlib.sha256(body).hexdigest()


def test_helper_changes_and_removal_change_new_evaluator_fingerprint(tmp_path):
    directory = tmp_path / "evals"
    directory.mkdir()
    for name in ("phase_eval.py", "phase_eval_environment.py", "phase_eval_web_verification.py", "phase_eval_fingerprint.py"):
        (directory / name).write_text(f"# {name}\n")
    module = _module()
    original = module.evaluator_sha256(tmp_path)
    assert module.evaluator_sha256(tmp_path) == original
    for name in ("phase_eval_environment.py", "phase_eval_web_verification.py", "phase_eval_fingerprint.py"):
        path = directory / name
        body = path.read_bytes()
        path.write_bytes(body + b"# changed\n")
        assert module.evaluator_sha256(tmp_path) != original, name
        path.write_bytes(body)
    path = directory / "phase_eval_web_verification.py"
    path.unlink()
    assert module.evaluator_sha256(tmp_path) != original


def test_comparator_uses_shared_fingerprint_for_helper_bound_measurement(tmp_path):
    from tests.test_phase_eval_compare import ROOT as GATE_ROOT

    spec = importlib.util.spec_from_file_location("bound_compare", GATE_ROOT / "evals/phase_eval_compare.py")
    comparison = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comparison)
    directory = tmp_path / "evals"
    directory.mkdir()
    (directory / "phase-cases").mkdir()
    (directory / "phase_eval.py").write_text("# main\n")
    (directory / "phase_eval_fingerprint.py").write_text("# fingerprint algorithm\n")
    expected = _module().evaluator_sha256(tmp_path)
    assert comparison.evaluation_fingerprint(tmp_path)["evaluator_sha256"] == expected
    (directory / "phase_eval_environment.py").write_text("# new environment behavior\n")
    assert comparison.evaluation_fingerprint(tmp_path)["evaluator_sha256"] != expected


def test_claim_audit_is_bound_but_its_absence_keeps_older_digests(tmp_path):
    directory = tmp_path / "evals"
    directory.mkdir()
    for name in ("phase_eval.py", "phase_eval_environment.py", "phase_eval_web_verification.py", "phase_eval_fingerprint.py"):
        (directory / name).write_text(f"# {name}\n")
    module = _module()
    without = module.evaluator_sha256(tmp_path)
    (directory / "claim_audit.py").write_text("# audit\n")
    with_audit = module.evaluator_sha256(tmp_path)
    assert with_audit != without
    (directory / "claim_audit.py").write_text("# audit changed\n")
    assert module.evaluator_sha256(tmp_path) not in {without, with_audit}
