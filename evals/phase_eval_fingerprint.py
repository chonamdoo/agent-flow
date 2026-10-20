"""Bind a model measurement to its evaluator and shared execution helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path


EVALUATOR_FILES = (
    "phase_eval.py",
    "phase_eval_environment.py",
    "phase_eval_web_verification.py",
    "phase_eval_fingerprint.py",
    "claim_audit.py",
)


def evaluator_sha256(tree: Path) -> str:
    """Retain old single-file records; new trees hash named, length-framed helpers."""
    directory = tree / "evals"
    main = directory / "phase_eval.py"
    if not (directory / "phase_eval_fingerprint.py").is_file():
        return hashlib.sha256(main.read_bytes()).hexdigest()
    digest = hashlib.sha256(b"agent-flow phase evaluator fingerprint v1\0")
    for name in EVALUATOR_FILES:
        path = directory / name
        if name != "phase_eval.py" and not path.is_file():
            continue
        encoded = name.encode("utf-8")
        body = path.read_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()
