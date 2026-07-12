from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_flow.core.execution_state_ledger import (
    capture_execution_state,
    execution_state_prompt_block,
    initialize_execution_state_ledger,
    observe_execution_state_injection,
    record_execution_state_usage,
    resolve_ledger_mode,
    validate_execution_state_ledger,
)
from agent_flow.runner import _ledger_experiment_controls_from_environment


ROOT = Path(__file__).resolve().parents[1]
NODE_MODULE = (ROOT / "lib" / "execution-state-ledger.mjs").as_uri()
GENERATED_AT = "2026-07-11T00:00:00.000Z"
RUN_ID = "run-1"
WORKFLOW_ID = "default"
ATTESTATION_PUBLIC_KEY = {
    "kty": "RSA",
    "n": "vqho9mBYkUbz5uC4JATIe1pZpDm7SynIfIVWzMRc1CZzHlWOkOa7bhYSMYIP1jo7oAWu9d2wX5Fa4wrgMtPT3afMCmkvfmYfnyRC3897pF5a0n09JxNaJBpxtaBjtExtXGzw50w4ZTeHfHts5hnt5TqQ0xtZ2POpRzhPTdRPatc",
    "e": "AQAB",
}
ATTESTATION_PRIVATE_KEY = {
    **ATTESTATION_PUBLIC_KEY,
    "d": "dqW3LBupAj91aShPb5rKaHlBb8G9nHjUGymfaq6IVj3XRflYTzRHT6rMh6K42EhE8sCWsMrVB6QdO015WCgan7H7TS3Dz5jrnSZUKmMuZhz_nRqkgMdoru3uq5ODmwuUMzOCQgtg9mCyJ-d-0MgmluJyZtikeALudROuwPxiCgE",
    "p": "76DkhGBt8h-Uh8kP8edn8cfmt7vajgfhO4bY73CrVTBq_-kUDYOsMTBinWB83UwWfUit39vAWBdmv55WZ-ZZsQ",
    "q": "y68FGKAUMIpMLktOw9A8WkJpcondRK84ldF-X0271udiD8u7dR734m6_uD55dojoF4LwoyrVksqru0DbCCknBw",
    "dp": "aBnWjKezu-b6SM8RTT8BiikU0ycZ-G_16j1XyxWAaT7ijRB9tK1KRghGHyaGuEDQ2FaVqtW1xs9LxN0Nno-U0Q",
    "dq": "EAyebi5O6PQ8xHkSn8NMvh_1hxzt3negEc4MEx5g6rIYu_3lq3jhN2pamP3zPC_VeeTLaU_6vDJUDdEycRYtCQ",
    "qi": "qC7c5LcGB-zDh5kIvyY0e-_cDHr2ExsgFvcDm_5WtLeJE-TKTpUd4QW1xf7OikbVw9jXfID1z919iiZAta8ThQ",
}
PRICING = {
    "currency": "USD",
    "input_per_million": 2,
    "output_per_million": 8,
    "snapshot_id": "pilot-price-v1",
}
EXPERIMENT = {
    "experiment_id": "experiment-1",
    "model_id": "model-1",
    "tool_permissions_sha256": "b" * 64,
    "system_prompt_sha256": "c" * 64,
    "caps_sha256": "d" * 64,
    "provider_retry_policy_sha256": "4" * 64,
    "provider_max_retries": 2,
    "pricing_snapshot": PRICING,
    "provider_attestation_key_id": "test-provider-key-1",
    "provider_attestation_public_key": ATTESTATION_PUBLIC_KEY,
}
WORKFLOW_PHASES = [
    {"id": "implement"},
    {"id": "gates", "routes": {"green": "commit", "request-changes": "fix-loop"}},
    {
        "id": "final-review",
        "multi_review": True,
        "routes": {"approve": "gates", "request-changes": "fix-loop"},
    },
    {"id": "fix-loop", "routes": {"default": "comment-authoring"}},
    {"id": "comment-authoring"},
    {"id": "commit"},
]
RUN_SNAPSHOT = {
    "runtime_id": "test",
    "profile_snapshot_sha256": "e" * 64,
    "installed_skill_plan_sha256": "f" * 64,
    "local_skill_plan_sha256": "1" * 64,
    "lore_snapshot_sha256": "2" * 64,
    "prompt_controls_sha256": "3" * 64,
}


def _initialize(
    run_dir: Path,
    mode: str,
    workflow_phases: list[dict[str, object]] = WORKFLOW_PHASES,
) -> dict[str, object]:
    return initialize_execution_state_ledger(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        task="repair tests",
        workflow_id=WORKFLOW_ID,
        workflow_phases=workflow_phases,
        base_commit="a" * 40,
        experiment=EXPERIMENT,
        run_snapshot=RUN_SNAPSHOT,
    )


def _mark_python_complete(run_dir: Path) -> None:
    (run_dir / "active").unlink(missing_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "workflow": WORKFLOW_ID,
                "current_phase": None,
                "phase_index": len(WORKFLOW_PHASES),
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _usage_receipt(
    run_dir: Path, values: dict[str, object], suffix: str = "usage"
) -> tuple[Path, str]:
    config = json.loads(
        (run_dir / "artifacts/execution-ledger/config.json").read_text()
    )
    unsigned_payload = {
        "schema_version": 1,
        "kind": "provider-usage-receipt",
        "provider": "test-provider",
        "request_id": f"request-{suffix}",
        "receipt_id": f"receipt-{suffix}",
        "event_id": values["event_id"],
        "run_id": RUN_ID,
        "generated_at": values["generated_at"],
        "scope": values["scope"],
        "phase_id": values["phase_id"],
        "round": values["round"],
        "model_id": values["model_id"],
        "control_receipt": {
            "schema_version": 1,
            "kind": "execution-control-receipt",
            "control_receipt_id": f"control-{suffix}",
            "run_id": RUN_ID,
            "experiment_id": "experiment-1",
            "model_id": values["model_id"],
            "runner_control_snapshot_sha256": config[
                "runner_control_snapshot_sha256"
            ],
            "tool_permissions_sha256": "b" * 64,
            "system_prompt_sha256": "c" * 64,
            "caps_sha256": "d" * 64,
            "provider_retry_policy_sha256": EXPERIMENT[
                "provider_retry_policy_sha256"
            ],
            "provider_max_retries": EXPERIMENT["provider_max_retries"],
            "pricing_snapshot_sha256": config["pricing_snapshot_sha256"],
            "provider_attestation_key_id": EXPERIMENT[
                "provider_attestation_key_id"
            ],
            "provider_attestation_public_key_sha256": config[
                "provider_attestation_public_key_sha256"
            ],
            "execution_controls_sha256": config["execution_controls_sha256"],
            "exposure_policy_sha256": config["exposure_policy_sha256"],
            "per_exposure_token_cap": config["per_exposure_token_cap"],
        },
        "usage": {
            "input_tokens": values["input_tokens"],
            "output_tokens": values["output_tokens"],
            "additional_tokens": values["additional_tokens"],
            "additional_token_scope": "condition-total",
            "latency_ms": values["latency_ms"],
            "estimated_cost_usd": values["estimated_cost_usd"],
        },
    }
    payload = _attest_usage_payload(unsigned_payload)
    receipt_path = run_dir / "artifacts" / f"{suffix}-usage-receipt.json"
    content = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    receipt_path.write_bytes(content)
    return receipt_path, hashlib.sha256(content).hexdigest()


def _attest_usage_payload(
    unsigned_payload: dict[str, object],
) -> dict[str, object]:
    signed_content = json.dumps(
        unsigned_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    modulus = int.from_bytes(_b64url_decode(ATTESTATION_PUBLIC_KEY["n"]), "big")
    private_exponent = int.from_bytes(
        _b64url_decode(ATTESTATION_PRIVATE_KEY["d"]), "big"
    )
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signed_content).digest()
    key_size = (modulus.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (key_size - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus).to_bytes(key_size, "big")
    payload = {
        **unsigned_payload,
        "attestation": {
            "schema_version": 1,
            "kind": "provider-usage-attestation",
            "algorithm": "RS256",
            "key_id": EXPERIMENT["provider_attestation_key_id"],
            "signed_payload_sha256": hashlib.sha256(signed_content).hexdigest(),
            "signature_base64url": _b64url_encode(signature),
        },
    }
    return payload


def _b64url_decode(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _record_verified_usage(
    run_dir: Path,
    mode: str,
    values: dict[str, object],
    suffix: str = "usage",
) -> dict[str, object]:
    receipt_path, receipt_sha256 = _usage_receipt(run_dir, values, suffix)
    return record_execution_state_usage(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
    )


def _node_scenario(run_dir: Path, mode: str, scenario: str) -> dict[str, object]:
    script = f"""
import fs from "node:fs";
import path from "node:path";
import {{ createHash, createPrivateKey, sign }} from "node:crypto";
import {{
  initializeExecutionStateLedger,
  captureExecutionState,
  observeExecutionStateInjection,
  executionStatePromptBlock,
}} from {json.dumps(NODE_MODULE)};
const runDir = {json.dumps(str(run_dir))};
const mode = {json.dumps(mode)};
const init = initializeExecutionStateLedger({{
  runDir,
  runId: {json.dumps(RUN_ID)},
  mode,
  experimentEnabled: true,
  task: "repair tests",
  workflowId: {json.dumps(WORKFLOW_ID)},
  workflowPhases: {json.dumps(WORKFLOW_PHASES)},
  baseCommit: {json.dumps('a' * 40)},
  experiment: {json.dumps(EXPERIMENT)},
  runSnapshot: {json.dumps(RUN_SNAPSHOT)},
}});
let output = {{ init }};
if ({json.dumps(scenario)} === "gate") {{
  const artifactPath = path.join(runDir, "artifacts", "gate-results.json");
  fs.mkdirSync(path.dirname(artifactPath), {{ recursive: true }});
  fs.writeFileSync(artifactPath, JSON.stringify({{
    passed: false,
    results: [{{
      gate_id: "unit",
      argv: ["python3", "-m", "pytest"],
      required: true,
      passed: false,
      exit_code: 1,
      stderr: "\\u001b[31mFAILED test_example\\u001b[0m\\nmore detail",
      stdout: "",
    }}],
  }}) + "\\n");
  const gates = {{ id: "gates", routes: {{ green: "commit", "request-changes": "fix-loop" }} }};
  output.capture = captureExecutionState({{
    runDir,
    runId: {json.dumps(RUN_ID)},
    mode,
    experimentEnabled: true,
    phase: gates,
    artifactPath,
    projectRoot: runDir,
    round: 1,
    fixLoopRounds: 1,
    generatedAt: {json.dumps(GENERATED_AT)},
    workflowId: {json.dumps(WORKFLOW_ID)},
    routeKey: "request-changes",
    routedTo: "fix-loop",
  }});
  const fix = {{ id: "fix-loop", multi_review: false }};
  output.observe = observeExecutionStateInjection({{
    runDir,
    runId: {json.dumps(RUN_ID)},
    mode,
    experimentEnabled: true,
    phase: fix,
    projectRoot: runDir,
    round: 1,
    generatedAt: {json.dumps(GENERATED_AT)},
    promptBytes: 400,
  }});
  output.block = executionStatePromptBlock({{
    runDir,
    runId: {json.dumps(RUN_ID)},
    mode,
    experimentEnabled: true,
    phase: fix,
    projectRoot: runDir,
    round: 1,
  }});
}}
if ({json.dumps(scenario)} === "mixed-gate") {{
  const artifactPath = path.join(runDir, "artifacts", "gate-results.json");
  fs.mkdirSync(path.dirname(artifactPath), {{ recursive: true }});
  const payload = {{
    passed: false,
    results: [
      {{ gate_id: "failed", argv: ["npm", "test"], required: true, passed: false,
        exit_code: 1, stderr: "failed", stdout: "" }},
      {{ gate_id: "passed", argv: ["npm", "run", "lint"], required: true, passed: true,
        exit_code: 0, stderr: "", stdout: "ok" }},
      {{ gate_id: "optional", argv: ["npm", "run", "audit"], required: false, passed: false,
        exit_code: 1, stderr: "optional", stdout: "" }},
      {{ argv: ["invalid"], required: true, passed: false }},
    ],
  }};
  const gates = {{ id: "gates", routes: {{ "request-changes": "fix-loop" }} }};
  for (const [index, generatedAt] of [
    {json.dumps(GENERATED_AT)},
    "2026-07-11T00:01:00.000Z",
  ].entries()) {{
    fs.writeFileSync(artifactPath, JSON.stringify(payload) + "\\n");
    output[`capture${{index + 1}}`] = captureExecutionState({{
      runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true, phase: gates,
      artifactPath, projectRoot: runDir, round: index + 1, generatedAt,
      workflowId: {json.dumps(WORKFLOW_ID)}, routeKey: "request-changes", routedTo: "fix-loop",
    }});
  }}
}}
if ({json.dumps(scenario)} === "unicode-gate") {{
  const artifactPath = path.join(runDir, "artifacts", "gate-results.json");
  fs.mkdirSync(path.dirname(artifactPath), {{ recursive: true }});
  const payload = {{
    passed: false,
    results: [0, 1, 2, 3].map((index) => ({{
      gate_id: `gate-${{index}}`,
      argv: ["gate", String(index)],
      required: true,
      passed: false,
      exit_code: 1,
      stderr: "𠀀".repeat(120),
      stdout: "",
    }})),
  }};
  fs.writeFileSync(artifactPath, JSON.stringify(payload) + "\\n");
  const gates = {{ id: "gates", routes: {{ green: "commit", "request-changes": "fix-loop" }} }};
  output.capture = captureExecutionState({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true, phase: gates,
    artifactPath, projectRoot: runDir, round: 1, fixLoopRounds: 1,
    generatedAt: {json.dumps(GENERATED_AT)}, workflowId: {json.dumps(WORKFLOW_ID)},
    routeKey: "request-changes", routedTo: "fix-loop",
  }});
  const fix = {{ id: "fix-loop", multi_review: false }};
  output.observe = observeExecutionStateInjection({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true, phase: fix,
    projectRoot: runDir, round: 1, generatedAt: "2026-07-11T00:01:00.000Z",
    promptBytes: 400,
  }});
  output.block = executionStatePromptBlock({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true,
    phase: fix, projectRoot: runDir, round: 1,
  }});
}}
if ({json.dumps(scenario)} === "review") {{
  const artifactPath = path.join(runDir, "final-review.md");
  const summaryPath = path.join(runDir, "review-summary.json");
  fs.writeFileSync(artifactPath, "# Review\\n\\n## Reviewer 1\\n\\n- Exact   verified finding\\n");
  fs.writeFileSync(summaryPath, JSON.stringify({{ findings: ["stale global finding"] }}) + "\\n");
  const phase = {{ id: "final-review", multi_review: true,
    routes: {{ approve: "gates", "request-changes": "fix-loop" }} }};
  output.capture = captureExecutionState({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true, phase, artifactPath,
    projectRoot: runDir, round: 1, generatedAt: {json.dumps(GENERATED_AT)},
    workflowId: {json.dumps(WORKFLOW_ID)}, routeKey: "request-changes", routedTo: "fix-loop",
  }});
}}
if ({json.dumps(scenario)} === "review-resolution") {{
  const artifactPath = path.join(runDir, "final-review.md");
  const phase = {{ id: "final-review", multi_review: true,
    routes: {{ approve: "gates", "request-changes": "fix-loop" }} }};
  fs.writeFileSync(artifactPath, "# Review\\n\\n## Findings\\n\\n- Fix checkout ordering.\\n");
  output.capture1 = captureExecutionState({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true, phase, artifactPath,
    projectRoot: runDir, round: 1, generatedAt: {json.dumps(GENERATED_AT)},
    workflowId: {json.dumps(WORKFLOW_ID)}, routeKey: "request-changes", routedTo: "fix-loop",
  }});
  fs.writeFileSync(artifactPath, "# Review\\n\\n## Overall\\n\\nverdict: approve\\n");
  output.capture2 = captureExecutionState({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true, phase, artifactPath,
    projectRoot: runDir, round: 2, generatedAt: "2026-07-11T00:01:00.000Z",
    workflowId: {json.dumps(WORKFLOW_ID)}, routeKey: "approve", routedTo: "gates",
  }});
  output.block = executionStatePromptBlock({{
    runDir, runId: {json.dumps(RUN_ID)}, mode, experimentEnabled: true,
    phase: {{ id: "fix-loop" }}, projectRoot: runDir, round: 2,
  }});
}}
process.stdout.write(JSON.stringify(output));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _python_gate_scenario(run_dir: Path, mode: str) -> dict[str, object]:
    output = {"init": _initialize(run_dir, mode)}
    artifact = run_dir / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "passed": False,
                "results": [
                    {
                        "gate_id": "unit",
                        "argv": ["python3", "-m", "pytest"],
                        "required": True,
                        "passed": False,
                        "exit_code": 1,
                        "stderr": "\x1b[31mFAILED test_example\x1b[0m\nmore detail",
                        "stdout": "",
                    }
                ],
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    gates = {"id": "gates", "routes": {"green": "commit", "request-changes": "fix-loop"}}
    output["capture"] = capture_execution_state(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=run_dir,
        round=1,
        fix_loop_rounds=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    fix = {"id": "fix-loop", "multi_review": False}
    output["observe"] = observe_execution_state_injection(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase=fix,
        project_root=run_dir,
        round=1,
        generated_at=GENERATED_AT,
        prompt_bytes=400,
    )
    output["block"] = execution_state_prompt_block(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase=fix,
        project_root=run_dir,
        round=1,
    )
    return output


def _node_usage_scenario(run_dir: Path) -> dict[str, object]:
    script = f"""
import fs from "node:fs";
import path from "node:path";
import {{ createHash, createPrivateKey, sign }} from "node:crypto";
import {{ initializeExecutionStateLedger, recordExecutionStateUsage }} from {json.dumps(NODE_MODULE)};
const init = initializeExecutionStateLedger({{
  runDir: {json.dumps(str(run_dir))}, runId: {json.dumps(RUN_ID)}, mode: "ledger-always",
  experimentEnabled: true, task: "repair tests", workflowId: {json.dumps(WORKFLOW_ID)},
  workflowPhases: {json.dumps(WORKFLOW_PHASES)}, baseCommit: {json.dumps('a' * 40)},
  experiment: {json.dumps(EXPERIMENT)},
  runSnapshot: {json.dumps(RUN_SNAPSHOT)},
}});
const phase = recordExecutionStateUsage({{
  runDir: {json.dumps(str(run_dir))}, runId: {json.dumps(RUN_ID)}, mode: "ledger-always",
  experimentEnabled: true, eventId: "phase-1", generatedAt: {json.dumps(GENERATED_AT)},
  scope: "phase", phaseId: "fix-loop", round: 1, inputTokens: 10, outputTokens: 2,
  additionalTokens: 1, latencyMs: 5, estimatedCostUsd: "0.0000001", modelId: "model-1",
}});
fs.writeFileSync(path.join({json.dumps(str(run_dir))}, "manifest.json"), JSON.stringify({{
  run_id: {json.dumps(RUN_ID)}, workflow: {json.dumps(WORKFLOW_ID)}, status: "complete",
  phase: "complete", phase_index: {len(WORKFLOW_PHASES)},
}}) + "\\n");
const unsignedReceipt = {{ schema_version: 1, kind: "provider-usage-receipt", provider: "test-provider",
  request_id: "request-run-total-1", receipt_id: "receipt-run-total-1", event_id: "run-total-1",
  run_id: {json.dumps(RUN_ID)}, generated_at: {json.dumps(GENERATED_AT)}, scope: "run-total",
  phase_id: null, round: null, model_id: "model-1", control_receipt: {{
      schema_version: 1, kind: "execution-control-receipt", control_receipt_id: "control-run-total-1",
      run_id: {json.dumps(RUN_ID)}, experiment_id: "experiment-1", model_id: "model-1",
      runner_control_snapshot_sha256: init.config.runner_control_snapshot_sha256,
      tool_permissions_sha256: {json.dumps('b' * 64)}, system_prompt_sha256: {json.dumps('c' * 64)},
      caps_sha256: {json.dumps('d' * 64)}, exposure_policy_sha256: init.config.exposure_policy_sha256,
      per_exposure_token_cap: init.config.per_exposure_token_cap,
      provider_retry_policy_sha256: init.config.provider_retry_policy_sha256,
      provider_max_retries: init.config.provider_max_retries,
      pricing_snapshot_sha256: init.config.pricing_snapshot_sha256,
      provider_attestation_key_id: init.config.provider_attestation_key_id,
      provider_attestation_public_key_sha256: init.config.provider_attestation_public_key_sha256,
      execution_controls_sha256: init.config.execution_controls_sha256 }}, usage: {{ input_tokens: 100,
    output_tokens: 20, additional_tokens: 0, additional_token_scope: "condition-total",
    latency_ms: 50, estimated_cost_usd: "2.000" }} }};
const stableJson = (value) => Array.isArray(value)
  ? `[${{value.map(stableJson).join(",")}}]`
  : value && typeof value === "object"
    ? `{{${{Object.keys(value).sort().map((key) => `${{JSON.stringify(key)}}:${{stableJson(value[key])}}`).join(",")}}}}`
    : JSON.stringify(value);
const signedBytes = Buffer.from(stableJson(unsignedReceipt));
const receipt = {{ ...unsignedReceipt, attestation: {{
  schema_version: 1, kind: "provider-usage-attestation", algorithm: "RS256",
  key_id: init.config.provider_attestation_key_id,
  signed_payload_sha256: createHash("sha256").update(signedBytes).digest("hex"),
  signature_base64url: sign("RSA-SHA256", signedBytes,
    createPrivateKey({{ key: {json.dumps(ATTESTATION_PRIVATE_KEY)}, format: "jwk" }})).toString("base64url"),
}} }};
const receiptPath = path.join({json.dumps(str(run_dir))}, "artifacts", "run-total-1-usage-receipt.json");
const receiptBytes = Buffer.from(stableJson(receipt) + "\\n");
fs.writeFileSync(receiptPath, receiptBytes);
const args = {{
  runDir: {json.dumps(str(run_dir))}, runId: {json.dumps(RUN_ID)}, mode: "ledger-always",
  experimentEnabled: true, receiptPath,
  receiptSha256: createHash("sha256").update(receiptBytes).digest("hex"),
}};
const first = recordExecutionStateUsage(args);
const replay = recordExecutionStateUsage(args);
process.stdout.write(JSON.stringify({{ init, phase, first, replay }}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _node_timestamp_scenario(run_dir: Path, generated_at: str) -> dict[str, object]:
    script = f"""
import {{ initializeExecutionStateLedger, recordExecutionStateUsage }} from {json.dumps(NODE_MODULE)};
const init = initializeExecutionStateLedger({{
  runDir: {json.dumps(str(run_dir))}, runId: {json.dumps(RUN_ID)}, mode: "ledger-always",
  experimentEnabled: true, task: "repair tests", workflowId: {json.dumps(WORKFLOW_ID)},
  workflowPhases: {json.dumps(WORKFLOW_PHASES)}, baseCommit: {json.dumps('a' * 40)},
  experiment: {json.dumps(EXPERIMENT)},
  runSnapshot: {json.dumps(RUN_SNAPSHOT)},
}});
const result = recordExecutionStateUsage({{
  runDir: {json.dumps(str(run_dir))}, runId: {json.dumps(RUN_ID)}, mode: "ledger-always",
  experimentEnabled: true, eventId: "timestamp-1", generatedAt: {json.dumps(generated_at)},
  scope: "phase", phaseId: "fix-loop", round: 1, inputTokens: 1, outputTokens: 0,
  additionalTokens: 0, latencyMs: 1, estimatedCostUsd: "0", modelId: "model-1",
}});
process.stdout.write(JSON.stringify({{ init, result }}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _sidecar_files(run_dir: Path) -> dict[str, bytes]:
    root = run_dir / "artifacts" / "execution-ledger"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_sidecar_parity_except_runtime_root(
    python_dir: Path, node_dir: Path
) -> None:
    python_files = _sidecar_files(python_dir)
    node_files = _sidecar_files(node_dir)
    excluded = {"captures.jsonl", "injections.jsonl", "events.jsonl"}
    assert set(python_files) == set(node_files)
    for name in set(python_files) - excluded:
        assert python_files[name] == node_files[name], name


def test_resolve_ledger_mode_contract() -> None:
    assert resolve_ledger_mode(None) == "artifacts-only"
    assert resolve_ledger_mode(" LEDGER-ALWAYS ") == "ledger-always"
    with pytest.raises(ValueError, match="invalid AGENT_FLOW_LEDGER_MODE"):
        resolve_ledger_mode("unknown")


def test_python_runner_reads_all_pinned_experiment_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "AGENT_FLOW_EXPERIMENT_ID": EXPERIMENT["experiment_id"],
        "AGENT_FLOW_EXPERIMENT_MODEL_ID": EXPERIMENT["model_id"],
        "AGENT_FLOW_EXPERIMENT_TOOL_PERMISSIONS_SHA256": EXPERIMENT[
            "tool_permissions_sha256"
        ],
        "AGENT_FLOW_EXPERIMENT_SYSTEM_PROMPT_SHA256": EXPERIMENT[
            "system_prompt_sha256"
        ],
        "AGENT_FLOW_EXPERIMENT_CAPS_SHA256": EXPERIMENT["caps_sha256"],
        "AGENT_FLOW_EXPERIMENT_PROVIDER_RETRY_POLICY_SHA256": EXPERIMENT[
            "provider_retry_policy_sha256"
        ],
        "AGENT_FLOW_EXPERIMENT_PROVIDER_MAX_RETRIES": str(
            EXPERIMENT["provider_max_retries"]
        ),
        "AGENT_FLOW_EXPERIMENT_PRICING_JSON": json.dumps(PRICING),
        "AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_KEY_ID": EXPERIMENT[
            "provider_attestation_key_id"
        ],
        "AGENT_FLOW_EXPERIMENT_PROVIDER_ATTESTATION_PUBLIC_KEY_JWK": json.dumps(
            ATTESTATION_PUBLIC_KEY
        ),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, str(value))
    assert _ledger_experiment_controls_from_environment() == EXPERIMENT


def test_disabled_initialization_creates_no_sidecar(tmp_path: Path) -> None:
    result = initialize_execution_state_ledger(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=False,
        workflow_id=WORKFLOW_ID,
    )
    assert result == {"ok": True, "enabled": False}
    assert not (tmp_path / "artifacts" / "execution-ledger").exists()


def test_artifacts_only_initial_bytes_match_node(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()

    python_result = _initialize(python_dir, "artifacts-only")
    node_result = _node_scenario(node_dir, "artifacts-only", "init")

    assert python_result == node_result["init"]
    assert _sidecar_files(python_dir) == _sidecar_files(node_dir)
    assert set(_sidecar_files(python_dir)) == {
        "captures.jsonl",
        "config.json",
        "events.jsonl",
        "injections.jsonl",
        "metrics.json",
        "usage.jsonl",
        "workflow.json",
    }


@pytest.mark.parametrize(
    "mode",
    ("artifacts-only", "ledger-always", "ledger-selective", "action-self-review"),
)
def test_gate_capture_injection_and_bytes_match_node(
    tmp_path: Path,
    mode: str,
) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()

    python_result = _python_gate_scenario(python_dir, mode)
    node_result = _node_scenario(node_dir, mode, "gate")

    assert python_result == node_result
    _assert_sidecar_parity_except_runtime_root(python_dir, node_dir)
    block = python_result["block"]
    if mode == "artifacts-only":
        assert block == ""
        assert not (python_dir / "artifacts/execution-ledger/ledger.json").exists()
    elif mode == "action-self-review":
        assert block.startswith(
            "## Action self-review (bounded verified artifact)\n"
        )
        assert all(
            value not in block
            for value in ("Execution ledger", RUN_ID, "round", "Inspect `", ".agent-flow/")
        )
        assert "FAILED" in block
        assert "- Raw: " in block
    else:
        assert block.startswith(f"## Execution ledger (advisory; run {RUN_ID}, round 1/3)\n")
        assert len(block.splitlines()) <= 5
        assert len(block) <= 720


def test_same_root_committed_gate_bytes_match_node(tmp_path: Path) -> None:
    run_dir = tmp_path / "same-root"
    run_dir.mkdir()
    python_result = _python_gate_scenario(run_dir, "ledger-always")
    python_files = _sidecar_files(run_dir)
    shutil.rmtree(run_dir / "artifacts" / "execution-ledger")
    node_result = _node_scenario(run_dir, "ledger-always", "gate")
    assert python_result == node_result
    assert python_files == _sidecar_files(run_dir)


def test_utf8_prompt_caps_and_token_proxies_match_node(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    _initialize(python_dir, "ledger-always")
    artifact = python_dir / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "passed": False,
        "results": [
            {
                "gate_id": f"gate-{index}",
                "argv": ["gate", str(index)],
                "required": True,
                "passed": False,
                "exit_code": 1,
                "stderr": "𠀀" * 120,
                "stdout": "",
            }
            for index in range(4)
        ],
    }
    artifact.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    gates = {
        "id": "gates",
        "routes": {"green": "commit", "request-changes": "fix-loop"},
    }
    capture = capture_execution_state(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=python_dir,
        round=1,
        fix_loop_rounds=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    fix = {"id": "fix-loop", "multi_review": False}
    observe = observe_execution_state_injection(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=fix,
        project_root=python_dir,
        round=1,
        generated_at="2026-07-11T00:01:00.000Z",
        prompt_bytes=400,
    )
    block = execution_state_prompt_block(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=fix,
        project_root=python_dir,
        round=1,
    )
    node_result = _node_scenario(node_dir, "ledger-always", "unicode-gate")

    assert capture == node_result["capture"]
    assert observe == node_result["observe"]
    assert block == node_result["block"]
    assert len(block.splitlines()) == 5
    assert len(block.encode("utf-8")) <= 720
    _assert_sidecar_parity_except_runtime_root(python_dir, node_dir)


def test_python_gate_green_and_snapshot_expiry_follow_canonical_route(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path, "ledger-always")
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    gates = {
        "id": "gates",
        "routes": {"green": "commit", "request-changes": "fix-loop"},
    }

    def write_failure(gate_id: str, argv: list[str], *, reported_passed: bool) -> None:
        artifact.write_text(
            json.dumps(
                {
                    "passed": reported_passed,
                    "results": [
                        {
                            "gate_id": gate_id,
                            "argv": argv,
                            "required": True,
                            "passed": False,
                            "exit_code": 1,
                            "stderr": f"{gate_id} failure",
                            "stdout": "",
                        }
                    ],
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    write_failure("old", ["npm", "test"], reported_passed=True)
    first = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        fix_loop_rounds=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert first["ok"] is True
    metrics = json.loads(
        (tmp_path / "artifacts/execution-ledger/metrics.json").read_text()
    )
    assert metrics["gate_green_le_3"] is None

    write_failure("current", ["npm", "run", "lint"], reported_passed=False)
    second = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=tmp_path,
        round=2,
        fix_loop_rounds=2,
        generated_at="2026-07-11T00:01:00.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert second["ok"] is True
    ledger = json.loads(
        (tmp_path / "artifacts/execution-ledger/ledger.json").read_text()
    )
    old_entry = next(
        entry for entry in ledger["entries"]["status"] if entry["payload"]["gate_id"] == "old"
    )
    assert old_entry["resolution"]["kind"] == "gate-snapshot-absent"
    block = execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop"},
        project_root=tmp_path,
        round=2,
    )
    assert "current failure" in block
    assert "old failure" not in block


def test_python_terminal_state_is_bound_and_active_or_early_runs_are_rejected(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path, "artifacts-only")
    args = {
        "run_dir": tmp_path,
        "run_id": RUN_ID,
        "mode": "artifacts-only",
        "experiment_enabled": True,
        "event_id": "python-terminal-total",
        "generated_at": GENERATED_AT,
        "scope": "run-total",
        "phase_id": None,
        "round": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "additional_tokens": 1,
        "latency_ms": 10,
        "estimated_cost_usd": "0.1",
        "model_id": "model-1",
    }
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "workflow": WORKFLOW_ID,
                "current_phase": None,
                "phase_index": len(WORKFLOW_PHASES) - 1,
            }
        )
        + "\n"
    )
    assert record_execution_state_usage(**args)["ok"] is False
    _mark_python_complete(tmp_path)
    (tmp_path / "active").write_text("")
    active = record_execution_state_usage(**args)
    assert active["ok"] is False
    assert "not complete" in active["error"]
    (tmp_path / "active").unlink()
    recorded = record_execution_state_usage(**args)
    assert recorded["ok"] is True
    validation = validate_execution_state_ledger(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="artifacts-only",
        require_completion=True,
    )
    assert validation["ok"] is True
    assert validation["verified"] is True
    assert validation["completed"] is True
    assert validation["usage"][0]["additional_token_scope"] == "condition-total"
    completion = json.loads(
        (tmp_path / "artifacts/execution-ledger/completion.json").read_text()
    )
    assert completion["terminal_state"]["runtime"] == "python"
    assert completion["terminal_state"]["sha256"]


def test_usage_missing_keeps_experiment_invalid_and_recording_is_fail_open(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    init = _initialize(python_dir, "ledger-always")
    metrics_path = python_dir / "artifacts" / "execution-ledger" / "metrics.json"
    assert json.loads(metrics_path.read_text())["experiment_valid"] is False

    phase_usage = record_execution_state_usage(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        event_id="phase-1",
        generated_at=GENERATED_AT,
        scope="phase",
        phase_id="fix-loop",
        round=1,
        input_tokens=10,
        output_tokens=2,
        additional_tokens=1,
        latency_ms=5,
        estimated_cost_usd="0.0000001",
        model_id="model-1",
    )
    assert phase_usage["recorded"] is True
    assert json.loads(metrics_path.read_text())["actual_usage_coverage"] is False

    _mark_python_complete(python_dir)
    total_values = {
        "event_id": "run-total-1",
        "generated_at": GENERATED_AT,
        "scope": "run-total",
        "phase_id": None,
        "round": None,
        "input_tokens": 100,
        "output_tokens": 20,
        "additional_tokens": 0,
        "latency_ms": 50,
        "estimated_cost_usd": "2.000",
        "model_id": "model-1",
    }
    result = _record_verified_usage(
        python_dir, "ledger-always", total_values, "run-total-1"
    )
    assert result["ok"] is True
    metrics = json.loads(metrics_path.read_text())
    assert metrics["actual_total_tokens"] == 120
    assert metrics["actual_usage_coverage"] is True
    assert metrics["experiment_valid"] is False

    replay = _record_verified_usage(
        python_dir, "ledger-always", total_values, "run-total-1"
    )
    assert replay["recorded"] is False
    usage_lines = (python_dir / "artifacts/execution-ledger/usage.jsonl").read_text().splitlines()
    assert len(usage_lines) == 2
    assert json.loads(usage_lines[0])["estimated_cost"] == "0.0000001"
    assert json.loads(usage_lines[1])["estimated_cost"] == "2"
    node = _node_usage_scenario(node_dir)
    assert {"init": init, "phase": phase_usage, "first": result, "replay": replay} == node
    python_files = _sidecar_files(python_dir)
    node_files = _sidecar_files(node_dir)
    for name in set(python_files) - {"completion.json"}:
        assert python_files[name] == node_files[name], name

    bad = record_execution_state_usage(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        event_id="bad-event",
        generated_at=GENERATED_AT,
        scope="phase",
        phase_id="fix-loop",
        round=1,
        input_tokens=1,
        output_tokens=1,
        additional_tokens=0,
        latency_ms=1,
        estimated_cost_usd=-1,
        model_id="model-1",
    )
    assert bad["ok"] is False
    second_total = record_execution_state_usage(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        event_id="run-total-2",
        generated_at="2026-07-11T00:00:01.000Z",
        scope="run-total",
        phase_id=None,
        round=None,
        input_tokens=100,
        output_tokens=20,
        additional_tokens=0,
        latency_ms=50,
        estimated_cost_usd="2.000",
        model_id="model-1",
    )
    assert second_total["ok"] is False
    assert len((python_dir / "artifacts/execution-ledger/usage.jsonl").read_text().splitlines()) == 2


def test_provider_receipt_hash_and_archive_projection_are_mandatory(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path, "ledger-always")
    _mark_python_complete(tmp_path)
    values = {
        "event_id": "receipt-proof",
        "generated_at": GENERATED_AT,
        "scope": "run-total",
        "phase_id": None,
        "round": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "additional_tokens": 0,
        "latency_ms": 5,
        "estimated_cost_usd": "0.1",
        "model_id": "model-1",
    }
    receipt_path, receipt_sha256 = _usage_receipt(
        tmp_path, values, "receipt-proof"
    )
    missing_hash = record_execution_state_usage(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=receipt_path,
    )
    assert missing_hash["ok"] is False
    assert "receipt-sha256" in str(missing_hash["error"])

    unsigned_payload = json.loads(receipt_path.read_text())
    unsigned_payload.pop("attestation")
    unsigned_path = tmp_path / "artifacts/caller-made-receipt.json"
    unsigned_content = (
        json.dumps(unsigned_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    unsigned_path.write_bytes(unsigned_content)
    unsigned = record_execution_state_usage(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=unsigned_path,
        receipt_sha256=hashlib.sha256(unsigned_content).hexdigest(),
    )
    assert unsigned["ok"] is False
    assert any(
        marker in str(unsigned["error"])
        for marker in ("attestation", "receipt fields")
    )
    assert not (tmp_path / "artifacts/execution-ledger/usage.jsonl").read_text()

    forged_payload = json.loads(receipt_path.read_text())
    signature = forged_payload["attestation"]["signature_base64url"]
    forged_payload["attestation"]["signature_base64url"] = (
        ("B" if signature[0] == "A" else "A") + signature[1:]
    )
    forged_path = tmp_path / "artifacts/forged-receipt.json"
    forged_content = (
        json.dumps(forged_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    forged_path.write_bytes(forged_content)
    forged = record_execution_state_usage(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=forged_path,
        receipt_sha256=hashlib.sha256(forged_content).hexdigest(),
    )
    assert forged["ok"] is False
    assert "signature verification failed" in str(forged["error"])
    assert not (tmp_path / "artifacts/execution-ledger/usage.jsonl").read_text()

    recorded = record_execution_state_usage(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
    )
    assert recorded["ok"] is True
    usage = json.loads(
        (tmp_path / "artifacts/execution-ledger/usage.jsonl").read_text()
    )
    assert usage["evidence_status"] == "verified-provider-receipt"
    assert usage["usage_provenance"]["sha256"] == receipt_sha256
    archive = tmp_path / usage["usage_provenance"]["archive_path"]
    archive.write_text("{}\n")
    validation = validate_execution_state_ledger(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        require_completion=True,
    )
    assert validation["verified"] is False


def test_external_controls_require_commitments_and_receipts_bind_the_snapshot(
    tmp_path: Path,
) -> None:
    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    invalid = initialize_execution_state_ledger(
        run_dir=invalid_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        task="repair tests",
        workflow_id=WORKFLOW_ID,
        workflow_phases=WORKFLOW_PHASES,
        base_commit="a" * 40,
        experiment={
            "experiment_id": "experiment-1",
            "model_id": "model-1",
            "tool_permissions_sha256": "tool-policy-v1",
            "system_prompt_sha256": "c" * 64,
            "caps_sha256": "d" * 64,
        },
        run_snapshot=RUN_SNAPSHOT,
    )
    assert invalid["ok"] is False
    assert "tool_permissions_sha256" in str(invalid["error"])
    assert not (invalid_dir / "artifacts/execution-ledger").exists()

    unsafe_pricing = initialize_execution_state_ledger(
        run_dir=invalid_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        task="repair tests",
        workflow_id=WORKFLOW_ID,
        workflow_phases=WORKFLOW_PHASES,
        base_commit="a" * 40,
        experiment={
            **EXPERIMENT,
            "pricing_snapshot": {
                **PRICING,
                "input_per_million": 9_007_199_254_740_993,
            },
        },
        run_snapshot=RUN_SNAPSHOT,
    )
    assert unsafe_pricing["ok"] is False
    assert "pricing input_per_million" in str(unsafe_pricing["error"])
    noncanonical_pricing = initialize_execution_state_ledger(
        run_dir=invalid_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        task="repair tests",
        workflow_id=WORKFLOW_ID,
        workflow_phases=WORKFLOW_PHASES,
        base_commit="a" * 40,
        experiment={
            **EXPERIMENT,
            "pricing_snapshot": {**PRICING, "input_per_million": 0.000001},
        },
        run_snapshot=RUN_SNAPSHOT,
    )
    assert noncanonical_pricing["ok"] is False
    assert "pricing input_per_million" in str(noncanonical_pricing["error"])

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    missing = initialize_execution_state_ledger(
        run_dir=missing_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        task="repair tests",
        workflow_id=WORKFLOW_ID,
        workflow_phases=WORKFLOW_PHASES,
        base_commit="a" * 40,
        experiment={"experiment_id": "experiment-1", "model_id": "model-1"},
        run_snapshot=RUN_SNAPSHOT,
    )
    assert missing["ok"] is True
    _mark_python_complete(missing_dir)
    values = {
        "event_id": "missing-controls",
        "generated_at": GENERATED_AT,
        "scope": "run-total",
        "phase_id": None,
        "round": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "additional_tokens": 0,
        "latency_ms": 5,
        "estimated_cost_usd": "0.1",
        "model_id": "model-1",
    }
    receipt_path, receipt_sha256 = _usage_receipt(
        missing_dir, values, "missing-controls"
    )
    missing_result = record_execution_state_usage(
        run_dir=missing_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
    )
    assert missing_result["ok"] is False
    assert "trusted independent attestation" in str(missing_result["error"])
    assert not (missing_dir / "artifacts/execution-ledger/usage.jsonl").read_text()

    pinned_dir = tmp_path / "pinned"
    pinned_dir.mkdir()
    _initialize(pinned_dir, "ledger-always")
    _mark_python_complete(pinned_dir)
    retry_path, _ = _usage_receipt(pinned_dir, values, "retry-mismatch")
    retry_unsigned = json.loads(retry_path.read_text())
    retry_unsigned.pop("attestation")
    retry_unsigned["control_receipt"]["provider_max_retries"] += 1
    retry_payload = _attest_usage_payload(retry_unsigned)
    retry_content = (
        json.dumps(retry_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    retry_path.write_bytes(retry_content)
    retry_mismatch = record_execution_state_usage(
        run_dir=pinned_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=retry_path,
        receipt_sha256=hashlib.sha256(retry_content).hexdigest(),
    )
    assert retry_mismatch["ok"] is False
    assert "does not match the pinned run" in str(retry_mismatch["error"])

    receipt_path, _ = _usage_receipt(pinned_dir, values, "snapshot-mismatch")
    payload = json.loads(receipt_path.read_text())
    payload["control_receipt"]["runner_control_snapshot_sha256"] = "9" * 64
    content = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    receipt_path.write_bytes(content)
    mismatch = record_execution_state_usage(
        run_dir=pinned_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        receipt_path=receipt_path,
        receipt_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert mismatch["ok"] is False
    assert "payload commitment mismatch" in str(mismatch["error"])
    assert not (pinned_dir / "artifacts/execution-ledger/usage.jsonl").read_text()


def test_record_usage_cli_derives_verified_values_from_receipt(tmp_path: Path) -> None:
    _initialize(tmp_path, "ledger-always")
    _mark_python_complete(tmp_path)
    values = {
        "event_id": "cli-receipt",
        "generated_at": GENERATED_AT,
        "scope": "run-total",
        "phase_id": None,
        "round": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "additional_tokens": 0,
        "latency_ms": 5,
        "estimated_cost_usd": "0.1",
        "model_id": "model-1",
    }
    receipt_path, receipt_sha256 = _usage_receipt(
        tmp_path, values, "cli-receipt"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_flow.cli",
            "experiment",
            "record-usage",
            "--run-dir",
            str(tmp_path),
            "--receipt",
            str(receipt_path),
            "--receipt-sha256",
            receipt_sha256,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["evidence_status"] == "verified-provider-receipt"
    metrics = json.loads(
        (tmp_path / "artifacts/execution-ledger/metrics.json").read_text()
    )
    assert metrics["actual_usage_coverage"] is True


@pytest.mark.parametrize("point", ("target", "event", "ledger", "metrics"))
def test_python_transaction_journal_recovers_each_append_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    _initialize(tmp_path, "ledger-always")
    artifact = tmp_path / "artifacts/gate-results.json"
    _write_gate_result(artifact, passed=False)
    monkeypatch.setenv("AGENT_FLOW_LEDGER_FAULT_AFTER", point)
    failed = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "gates", "routes": {"request-changes": "fix-loop"}},
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        fix_loop_rounds=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert failed["ok"] is False
    journal = tmp_path / "artifacts/execution-ledger/transaction.json"
    assert journal.exists()
    monkeypatch.delenv("AGENT_FLOW_LEDGER_FAULT_AFTER")
    if point == "target":
        dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_owner.wait(timeout=10)
        (tmp_path / "artifacts/execution-ledger/transaction.lock").write_text(
            json.dumps({"pid": dead_owner.pid, "token": "stale-owner"}) + "\n"
        )
    recovered = validate_execution_state_ledger(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
    )
    assert recovered["ok"] is True, recovered.get("error")
    assert len(recovered["captures"]) == 1
    assert recovered["event_count"] == 1
    assert not journal.exists()


def test_python_transaction_journal_recovers_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _initialize(tmp_path, "ledger-always")
    _mark_python_complete(tmp_path)
    values = {
        "event_id": "crash-completion",
        "generated_at": GENERATED_AT,
        "scope": "run-total",
        "phase_id": None,
        "round": None,
        "input_tokens": 10,
        "output_tokens": 2,
        "additional_tokens": 0,
        "latency_ms": 5,
        "estimated_cost_usd": "0.1",
        "model_id": "model-1",
    }
    monkeypatch.setenv("AGENT_FLOW_LEDGER_FAULT_AFTER", "completion")
    failed = _record_verified_usage(
        tmp_path, "ledger-always", values, "crash-completion"
    )
    assert failed["ok"] is False
    monkeypatch.delenv("AGENT_FLOW_LEDGER_FAULT_AFTER")
    recovered = validate_execution_state_ledger(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        require_completion=True,
    )
    assert recovered["ok"] is True, recovered.get("error")
    assert recovered["completed"] is True
    assert len(recovered["usage"]) == 1


def test_python_rejects_tampered_journal_before_completing_partial_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _initialize(tmp_path, "ledger-always")
    artifact = tmp_path / "artifacts/gate-results.json"
    _write_gate_result(artifact, passed=False)
    monkeypatch.setenv("AGENT_FLOW_LEDGER_FAULT_AFTER", "target")
    failed = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "gates", "routes": {"request-changes": "fix-loop"}},
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        fix_loop_rounds=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert failed["ok"] is False
    monkeypatch.delenv("AGENT_FLOW_LEDGER_FAULT_AFTER")
    sidecar = tmp_path / "artifacts/execution-ledger"
    journal_path = sidecar / "transaction.json"
    journal = json.loads(journal_path.read_text())
    journal["completed_at"] = GENERATED_AT
    journal_path.write_text(json.dumps(journal) + "\n")
    validation = validate_execution_state_ledger(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
    )
    assert validation["ok"] is False
    assert "transaction journal" in str(validation["error"])
    assert (sidecar / "events.jsonl").read_text() == ""
    assert json.loads((sidecar / "ledger.json").read_text())["entries"]["status"] == []
    assert journal_path.exists()


def _write_gate_result(path: Path, *, passed: bool) -> None:
    results = [
        {
            "gate_id": "unit",
            "argv": ["python3", "-m", "pytest"],
            "required": True,
            "passed": passed,
            "exit_code": 0 if passed else 1,
            "stderr": "" if passed else "FAILED test_example",
            "stdout": "ok" if passed else "",
        }
    ]
    path.write_text(
        json.dumps({"passed": passed, "results": results}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_artifacts_only_keeps_repeat_metrics_without_ledger_or_prompt_injection(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path, "artifacts-only")
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    gates = {"id": "gates", "routes": {"green": "commit", "request-changes": "fix-loop"}}

    for round_number in (1, 2):
        _write_gate_result(artifact, passed=False)
        result = capture_execution_state(
            run_dir=tmp_path,
            run_id=RUN_ID,
            mode="artifacts-only",
            experiment_enabled=True,
            phase=gates,
            artifact_path=artifact,
            project_root=tmp_path,
            round=round_number,
            fix_loop_rounds=round_number,
            generated_at=f"2026-07-11T00:00:0{round_number}.000Z",
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
        assert result["captured"] is True
    _write_gate_result(artifact, passed=True)
    capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=tmp_path,
        round=3,
        fix_loop_rounds=2,
        generated_at="2026-07-11T00:00:03.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="green",
        routed_to="commit",
    )
    observed = observe_execution_state_injection(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=True,
        phase={"id": "implement", "multi_review": False},
        project_root=tmp_path,
        round=1,
        generated_at="2026-07-11T00:00:04.000Z",
        prompt_bytes=400,
    )
    assert observed["block"] == ""

    sidecar = tmp_path / "artifacts" / "execution-ledger"
    metrics = json.loads((sidecar / "metrics.json").read_text())
    assert not (sidecar / "ledger.json").exists()
    assert len((sidecar / "injections.jsonl").read_text().splitlines()) == 1
    assert metrics["repeated_command_fingerprint_count"] == 2
    assert metrics["repeated_failure_fingerprint_count"] == 1
    assert metrics["repeated_diagnosis_fingerprint_count"] == 1
    assert metrics["gate_green_attempt"] == 3
    assert metrics["gate_green_le_3"] is True
    assert metrics["fix_loop_rounds"] == 2
    assert metrics["prompt_bytes"] == 400
    assert metrics["prompt_token_proxy"] == 100
    assert metrics["injected_bytes"] == 0


def test_canonical_route_coverage_requires_workflow_start_to_complete(
    tmp_path: Path,
) -> None:
    workflow_phases = [
        {"id": "implement"},
        {"id": "gates", "routes": {"green": "complete"}},
    ]
    complete = tmp_path / "complete"
    incomplete = tmp_path / "incomplete"
    complete.mkdir()
    incomplete.mkdir()
    _initialize(
        complete,
        "artifacts-only",
        workflow_phases=workflow_phases,
    )
    implement_artifact = complete / "artifacts/implement.md"
    implement_artifact.parent.mkdir(parents=True, exist_ok=True)
    implement_artifact.write_text("# Implement\n", encoding="utf-8")
    first = capture_execution_state(
        run_dir=complete,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=True,
        phase=workflow_phases[0],
        artifact_path=implement_artifact,
        project_root=complete,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="sequential",
        routed_to="gates",
        transition_occurrence_id="1" * 64,
    )
    assert first["ok"] is True
    gate_artifact = complete / "artifacts/gate-results.json"
    _write_gate_result(gate_artifact, passed=True)
    terminal = capture_execution_state(
        run_dir=complete,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=True,
        phase=workflow_phases[1],
        artifact_path=gate_artifact,
        project_root=complete,
        round=1,
        generated_at="2026-07-11T00:00:01.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="green",
        routed_to="complete",
        transition_occurrence_id="2" * 64,
    )
    assert terminal["ok"] is True
    complete_metrics = json.loads(
        (complete / "artifacts/execution-ledger/metrics.json").read_text()
    )
    assert complete_metrics["canonical_route_coverage_complete"] is True
    assert complete_metrics["canonical_route_coverage_gap_count"] == 0

    _initialize(
        incomplete,
        "artifacts-only",
        workflow_phases=workflow_phases,
    )
    incomplete_gate = incomplete / "artifacts/gate-results.json"
    _write_gate_result(incomplete_gate, passed=True)
    only_terminal = capture_execution_state(
        run_dir=incomplete,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=True,
        phase=workflow_phases[1],
        artifact_path=incomplete_gate,
        project_root=incomplete,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="green",
        routed_to="complete",
        transition_occurrence_id="3" * 64,
    )
    assert only_terminal["ok"] is True
    incomplete_metrics = json.loads(
        (incomplete / "artifacts/execution-ledger/metrics.json").read_text()
    )
    assert incomplete_metrics["canonical_route_coverage_complete"] is False
    assert incomplete_metrics["canonical_route_coverage_gap_count"] > 0
    assert incomplete_metrics["experiment_valid"] is False


def test_review_summary_is_structured_and_current_artifact_is_preserved(
    tmp_path: Path,
) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    init = _initialize(python_dir, "ledger-always")
    artifact = python_dir / "final-review.md"
    artifact.write_text(
        "# Review\n\n## Reviewer 1\n\n- Exact   verified finding\n",
        encoding="utf-8",
    )
    summary = python_dir / "review-summary.json"
    summary.write_text(
        json.dumps({"findings": ["stale global finding"]}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    phase = {
        "id": "final-review",
        "multi_review": True,
        "routes": {"approve": "gates", "request-changes": "fix-loop"},
    }

    result = capture_execution_state(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        artifact_path=artifact,
        project_root=python_dir,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    replay = capture_execution_state(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        artifact_path=artifact,
        project_root=python_dir,
        round=1,
        generated_at="2026-07-11T00:00:01.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )

    assert result["ok"] is True
    assert replay["captured"] is False
    original = artifact.read_bytes()
    assert original == b"# Review\n\n## Reviewer 1\n\n- Exact   verified finding\n"
    captures = [json.loads(line) for line in (python_dir / "artifacts/execution-ledger/captures.jsonl").read_text().splitlines()]
    assert {item["selector"] for item in captures[0]["source_provenance"]} == {"/", "/review-artifact"}
    ledger = json.loads((python_dir / "artifacts/execution-ledger/ledger.json").read_text())
    entry = ledger["entries"]["knowledge"][0]
    assert entry["payload"]["finding"] == "Exact verified finding"
    assert entry["provenance"]["selector"] == "/findings/0"
    archived = [path.read_bytes() for path in (python_dir / "artifacts/execution-ledger/sources").iterdir()]
    assert original in archived
    assert summary.read_bytes() not in archived
    canonical_summaries = list(
        (python_dir / "artifacts/execution-ledger/review-summaries").glob("*.json")
    )
    assert len(canonical_summaries) == 1
    canonical = json.loads(canonical_summaries[0].read_text())
    assert canonical["artifact_sha256"] == entry["payload"]["review_artifact_sha256"]
    assert canonical["findings"] == ["Exact verified finding"]
    node = _node_scenario(node_dir, "ledger-always", "review")
    assert {"init": init, "capture": result} == node
    _assert_sidecar_parity_except_runtime_root(python_dir, node_dir)


def test_stale_candidate_is_excluded_and_prior_reminder_is_counted(
    tmp_path: Path,
) -> None:
    _python_gate_scenario(tmp_path, "ledger-always")
    sidecar = tmp_path / "artifacts" / "execution-ledger"
    ledger = json.loads((sidecar / "ledger.json").read_text())
    archive = tmp_path / ledger["entries"]["status"][0]["provenance"]["archive_path"]
    archive.write_text("tampered\n", encoding="utf-8")
    block = execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop", "multi_review": False},
        project_root=tmp_path,
        round=2,
    )
    assert block == ""
    observed = observe_execution_state_injection(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop", "multi_review": False},
        project_root=tmp_path,
        round=2,
        generated_at="2026-07-11T00:00:02.000Z",
        prompt_bytes=200,
    )
    assert observed["block"] == ""
    assert observed["ok"] is False
    metrics = json.loads((sidecar / "metrics.json").read_text())
    assert metrics["stale_candidate_count"] == 0
    assert metrics["stale_reminder_count"] == 0
    assert metrics["stale_count"] == 0
    assert metrics["unsupported_source_count"] == 0
    assert metrics["unsupported_reminder_count"] == 0
    assert metrics["experiment_valid"] is False


def test_multi_review_never_receives_a_prompt_block(tmp_path: Path) -> None:
    _python_gate_scenario(tmp_path, "ledger-always")
    assert execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "architecture-review", "multi_review": True},
        project_root=tmp_path,
        round=1,
    ) == ""


def test_canonical_routing_mismatch_is_measured(tmp_path: Path) -> None:
    _initialize(tmp_path, "artifacts-only")
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    _write_gate_result(artifact, passed=False)
    capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="artifacts-only",
        experiment_enabled=True,
        phase={"id": "gates", "routes": {"request-changes": "fix-loop"}},
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="commit",
    )
    metrics = json.loads((tmp_path / "artifacts/execution-ledger/metrics.json").read_text())
    assert metrics["canonical_routing_violations"] == 1
    assert metrics["canonical_transition_count"] == 1
    assert len(metrics["routing_provenance"]) == 1
    routing = dict(metrics["routing_provenance"][0])
    occurrence_id = routing.pop("transition_occurrence_id")
    assert len(occurrence_id) == 64
    assert set(occurrence_id) <= set("0123456789abcdef")
    assert routing == {
        "expected_target": "fix-loop",
        "phase": "gates",
        "round": 1,
        "route_key": "request-changes",
        "routed_to": "commit",
        "workflow_id": WORKFLOW_ID,
    }


def test_selective_mode_injects_only_after_cross_round_repeat(tmp_path: Path) -> None:
    _initialize(tmp_path, "ledger-selective")
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    gates = {"id": "gates", "routes": {"request-changes": "fix-loop"}}
    _write_gate_result(artifact, passed=False)
    for round_number in (1, 2):
        capture_execution_state(
            run_dir=tmp_path,
            run_id=RUN_ID,
            mode="ledger-selective",
            experiment_enabled=True,
            phase=gates,
            artifact_path=artifact,
            project_root=tmp_path,
            round=round_number,
            fix_loop_rounds=round_number,
            generated_at=f"2026-07-11T00:00:0{round_number}.000Z",
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
    block = execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-selective",
        experiment_enabled=True,
        phase={"id": "implement", "multi_review": False},
        project_root=tmp_path,
        round=2,
    )
    assert block.startswith(f"## Execution ledger (advisory; run {RUN_ID}, round 2/3)\n")
    assert "Before retrying, change the action" in block
    assert len(block.splitlines()) <= 5
    assert all(len(line) <= 160 for line in block.splitlines()[1:])


@pytest.mark.parametrize(
    "mode",
    ("artifacts-only", "ledger-always", "ledger-selective", "action-self-review"),
)
def test_bugfix_implement_fix_is_a_selective_fix_target(
    tmp_path: Path, mode: str
) -> None:
    workflow_phases = [{"id": "implement-fix"}, *WORKFLOW_PHASES]
    initialized = _initialize(tmp_path, mode, workflow_phases=workflow_phases)
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    _write_gate_result(artifact, passed=False)
    captured = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase={
            "id": "gates",
            "routes": {"green": "commit", "request-changes": "fix-loop"},
        },
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert captured["ok"] is True
    block = execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase={"id": "implement-fix"},
        project_root=tmp_path,
        round=1,
    )
    if mode == "artifacts-only":
        assert block == ""
    elif mode == "action-self-review":
        assert "Action self-review (bounded verified artifact)" in block
        assert "Execution ledger" not in block
    else:
        assert "Execution ledger" in block
    config = initialized["config"]
    assert "implement-fix" in config["exposure_policy"]["eligible_phases"]
    assert "implement-fix" in config["exposure_policy"]["selective_fix_phases"]


@pytest.mark.parametrize("mode", ["ledger-selective", "action-self-review"])
def test_selective_arms_expose_first_failure_only_in_literal_fix_loop(
    tmp_path: Path,
    mode: str,
) -> None:
    workflow_phases = [
        *WORKFLOW_PHASES,
        {"id": "pr-ci-fix"},
        {"id": "pr-comment-fix"},
    ]
    _initialize(tmp_path, mode, workflow_phases)
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    _write_gate_result(artifact, passed=False)
    gates = {"id": "gates", "routes": {"green": "commit", "request-changes": "fix-loop"}}
    first = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        fix_loop_rounds=0,
        generated_at="2026-07-11T00:00:01.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert first["captured"] is True
    prompt_args = {
        "run_dir": tmp_path,
        "run_id": RUN_ID,
        "mode": mode,
        "experiment_enabled": True,
        "project_root": tmp_path,
        "round": 1,
    }
    assert execution_state_prompt_block(**prompt_args, phase={"id": "fix-loop"})
    assert execution_state_prompt_block(**prompt_args, phase={"id": "pr-ci-fix"}) == ""
    assert execution_state_prompt_block(**prompt_args, phase={"id": "pr-comment-fix"}) == ""

    second = capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode=mode,
        experiment_enabled=True,
        phase=gates,
        artifact_path=artifact,
        project_root=tmp_path,
        round=2,
        fix_loop_rounds=1,
        generated_at="2026-07-11T00:00:02.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert second["captured"] is True
    prompt_args["round"] = 2
    assert execution_state_prompt_block(**prompt_args, phase={"id": "pr-ci-fix"})
    assert execution_state_prompt_block(**prompt_args, phase={"id": "pr-comment-fix"})


def test_action_self_review_inspects_latest_direct_failure_before_older_repeat(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path, "action-self-review")
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    gates = {"id": "gates", "routes": {"green": "commit", "request-changes": "fix-loop"}}

    def write_failure(diagnosis: str, suffix: str = "") -> None:
        artifact.write_text(
            json.dumps(
                {
                    "passed": False,
                    "results": [
                        {
                            "gate_id": "unit",
                            "argv": ["python3", "-m", "pytest"],
                            "required": True,
                            "passed": False,
                            "exit_code": 1,
                            "stderr": f"{diagnosis}{suffix}",
                            "stdout": "",
                        }
                    ],
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    for round_number, diagnosis, suffix in (
        (1, "failure-A", ""),
        (2, "failure-A", " "),
        (3, "failure-B", ""),
    ):
        write_failure(diagnosis, suffix)
        captured = capture_execution_state(
            run_dir=tmp_path,
            run_id=RUN_ID,
            mode="action-self-review",
            experiment_enabled=True,
            phase=gates,
            artifact_path=artifact,
            project_root=tmp_path,
            round=round_number,
            fix_loop_rounds=round_number - 1,
            generated_at=f"2026-07-11T00:00:0{round_number}.000Z",
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
        assert captured["captured"] is True

    block = execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="action-self-review",
        experiment_enabled=True,
        phase={"id": "fix-loop"},
        project_root=tmp_path,
        round=3,
    )
    assert "failure-B" in block
    assert "Inspect `" not in block
    assert ".agent-flow/" not in block
    repeated_block = execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="action-self-review",
        experiment_enabled=True,
        phase={"id": "implement"},
        project_root=tmp_path,
        round=3,
    )
    assert "failure-A" in repeated_block
    assert "failure-B" not in repeated_block


def test_capture_replay_ignores_timestamp_without_incrementing_attempt_or_repeat(
    tmp_path: Path,
) -> None:
    _initialize(tmp_path, "ledger-selective")
    artifact = tmp_path / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    _write_gate_result(artifact, passed=False)
    gates = {"id": "gates", "routes": {"green": "commit", "request-changes": "fix-loop"}}
    common = {
        "run_dir": tmp_path,
        "run_id": RUN_ID,
        "mode": "ledger-selective",
        "experiment_enabled": True,
        "phase": gates,
        "artifact_path": artifact,
        "project_root": tmp_path,
        "round": 1,
        "fix_loop_rounds": 1,
        "workflow_id": WORKFLOW_ID,
        "route_key": "request-changes",
        "routed_to": "fix-loop",
    }
    first = capture_execution_state(
        **common,
        generated_at="2026-07-11T00:00:01.000Z",
    )
    replay = capture_execution_state(
        **common,
        generated_at="2026-07-11T00:00:02.000Z",
    )

    assert first["captured"] is True
    assert first["gate_attempt"] == 1
    assert replay["captured"] is False
    assert replay["gate_attempt"] == 1
    captures = (tmp_path / "artifacts/execution-ledger/captures.jsonl").read_text().splitlines()
    assert len(captures) == 1
    assert json.loads(captures[0])["measurement"]["gate_attempt"] == 1
    metrics = json.loads((tmp_path / "artifacts/execution-ledger/metrics.json").read_text())
    assert metrics["gate_green_attempt"] is None
    assert metrics["repeated_command_fingerprint_count"] == 0
    assert metrics["repeated_failure_fingerprint_count"] == 0
    assert metrics["repeated_diagnosis_fingerprint_count"] == 0
    assert execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-selective",
        experiment_enabled=True,
        phase={"id": "implement"},
        project_root=tmp_path,
        round=1,
    ) == ""

    alternate = tmp_path / "artifacts" / "alternate-gate-results.json"
    alternate.write_bytes(artifact.read_bytes())
    distinct = capture_execution_state(
        **{**common, "artifact_path": alternate},
        generated_at="2026-07-11T00:00:03.000Z",
    )
    assert distinct["captured"] is True
    assert distinct["gate_attempt"] == 2


def test_malformed_review_is_source_quality_not_unsafe_reminder(tmp_path: Path) -> None:
    _initialize(tmp_path, "ledger-always")
    artifact = tmp_path / "final-review.md"
    artifact.write_text("verdict: request-changes\n", encoding="utf-8")
    capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={
            "id": "final-review",
            "multi_review": True,
            "routes": {"request-changes": "fix-loop"},
        },
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    observe_execution_state_injection(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop", "multi_review": False},
        project_root=tmp_path,
        round=1,
        generated_at=GENERATED_AT,
        prompt_bytes=100,
    )
    metrics = json.loads((tmp_path / "artifacts/execution-ledger/metrics.json").read_text())
    assert metrics["unsupported_source_count"] == 1
    assert metrics["malformed_source_count"] == 1
    assert metrics["excluded_source_count"] == 0
    assert metrics["unsupported_reminder_count"] == 0
    assert metrics["unsupported_count"] == 0


def test_mixed_gate_command_denominator_and_bytes_match_node(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    output: dict[str, object] = {"init": _initialize(python_dir, "ledger-always")}
    artifact = python_dir / "artifacts" / "gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "passed": False,
        "results": [
            {
                "gate_id": "failed",
                "argv": ["npm", "test"],
                "required": True,
                "passed": False,
                "exit_code": 1,
                "stderr": "failed",
                "stdout": "",
            },
            {
                "gate_id": "passed",
                "argv": ["npm", "run", "lint"],
                "required": True,
                "passed": True,
                "exit_code": 0,
                "stderr": "",
                "stdout": "ok",
            },
            {
                "gate_id": "optional",
                "argv": ["npm", "run", "audit"],
                "required": False,
                "passed": False,
                "exit_code": 1,
                "stderr": "optional",
                "stdout": "",
            },
            {"argv": ["invalid"], "required": True, "passed": False},
        ],
    }
    phase = {"id": "gates", "routes": {"request-changes": "fix-loop"}}
    for round_number, generated_at in (
        (1, GENERATED_AT),
        (2, "2026-07-11T00:01:00.000Z"),
    ):
        artifact.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        output[f"capture{round_number}"] = capture_execution_state(
            run_dir=python_dir,
            run_id=RUN_ID,
            mode="ledger-always",
            experiment_enabled=True,
            phase=phase,
            artifact_path=artifact,
            project_root=python_dir,
            round=round_number,
            generated_at=generated_at,
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
    node = _node_scenario(node_dir, "ledger-always", "mixed-gate")
    assert output == node
    _assert_sidecar_parity_except_runtime_root(python_dir, node_dir)
    sidecar = python_dir / "artifacts/execution-ledger"
    captures = [json.loads(line) for line in (sidecar / "captures.jsonl").read_text().splitlines()]
    assert len(captures[0]["measurement"]["command_fingerprints"]) == 3
    assert len(captures[0]["measurement"]["failure_fingerprints"]) == 1
    ledger = json.loads((sidecar / "ledger.json").read_text())
    assert len(ledger["entries"]["status"]) == 2
    metrics = json.loads((sidecar / "metrics.json").read_text())
    assert metrics["repeated_command_fingerprint_count"] == 3
    assert metrics["repeated_failure_fingerprint_count"] == 1


def test_gate_success_semantically_resolves_prior_failure(tmp_path: Path) -> None:
    _initialize(tmp_path, "ledger-always")
    artifact = tmp_path / "artifacts/gate-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    phase = {
        "id": "gates",
        "routes": {"green": "commit", "request-changes": "fix-loop"},
    }
    failure = {
        "passed": False,
        "results": [{
            "gate_id": "unit",
            "argv": ["python3", "-m", "pytest"],
            "required": True,
            "passed": False,
            "exit_code": 1,
            "stderr": "transient failure",
            "stdout": "",
        }],
    }
    artifact.write_text(json.dumps(failure, separators=(",", ":")) + "\n")
    capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        artifact_path=artifact,
        project_root=tmp_path,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert "transient failure" in execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop"},
        project_root=tmp_path,
        round=1,
    )
    success = {**failure, "passed": True}
    success["results"] = [{**failure["results"][0], "passed": True, "exit_code": 0, "stderr": ""}]
    artifact.write_text(json.dumps(success, separators=(",", ":")) + "\n")
    capture_execution_state(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        artifact_path=artifact,
        project_root=tmp_path,
        round=2,
        generated_at="2026-07-11T00:01:00.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="green",
        routed_to="commit",
    )
    ledger = json.loads((tmp_path / "artifacts/execution-ledger/ledger.json").read_text())
    assert ledger["entries"]["status"][0]["resolution"]["kind"] == "gate-success"
    assert execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop"},
        project_root=tmp_path,
        round=2,
    ) == ""


def test_approved_review_resolves_findings_and_bytes_match_node(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    output: dict[str, object] = {"init": _initialize(python_dir, "ledger-always")}
    artifact = python_dir / "final-review.md"
    phase = {
        "id": "final-review",
        "multi_review": True,
        "routes": {"approve": "gates", "request-changes": "fix-loop"},
    }
    artifact.write_text("# Review\n\n## Findings\n\n- Fix checkout ordering.\n")
    output["capture1"] = capture_execution_state(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        artifact_path=artifact,
        project_root=python_dir,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    artifact.write_text("# Review\n\n## Overall\n\nverdict: approve\n")
    output["capture2"] = capture_execution_state(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        artifact_path=artifact,
        project_root=python_dir,
        round=2,
        generated_at="2026-07-11T00:01:00.000Z",
        workflow_id=WORKFLOW_ID,
        route_key="approve",
        routed_to="gates",
    )
    output["block"] = execution_state_prompt_block(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop"},
        project_root=python_dir,
        round=2,
    )
    node = _node_scenario(node_dir, "ledger-always", "review-resolution")
    assert output == node
    _assert_sidecar_parity_except_runtime_root(python_dir, node_dir)
    ledger = json.loads((python_dir / "artifacts/execution-ledger/ledger.json").read_text())
    assert ledger["entries"]["knowledge"][0]["resolution"]["kind"] == "review-result"
    assert output["block"] == ""
    assert execution_state_prompt_block(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase=phase,
        project_root=python_dir,
        round=2,
    ) == ""


@pytest.mark.parametrize("field", ("payload", "id", "extractor", "selector"))
def test_tampered_ledger_entry_is_reextracted_before_prompt(
    tmp_path: Path,
    field: str,
) -> None:
    _python_gate_scenario(tmp_path, "ledger-always")
    ledger_path = tmp_path / "artifacts/execution-ledger/ledger.json"
    ledger = json.loads(ledger_path.read_text())
    entry = ledger["entries"]["status"][0]
    if field == "payload":
        entry["payload"]["diagnosis"] = "forged diagnosis"
    elif field == "id":
        entry["id"] = "0" * 64
    elif field == "extractor":
        entry["provenance"]["extractor_version"] = "forged"
    else:
        entry["provenance"]["selector"] = "/results/99"
    ledger_path.write_text(json.dumps(ledger, separators=(",", ":")) + "\n")
    assert execution_state_prompt_block(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "fix-loop"},
        project_root=tmp_path,
        round=1,
    ) == ""


@pytest.mark.parametrize("leaf", ("captures.jsonl", "injections.jsonl", "usage.jsonl"))
def test_jsonl_leaf_symlinks_never_write_outside_run(tmp_path: Path, leaf: str) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _initialize(run_dir, "ledger-always")
    outside = tmp_path / f"outside-{leaf}"
    outside.write_text("sentinel\n")
    target = run_dir / "artifacts/execution-ledger" / leaf
    target.unlink()
    target.symlink_to(outside)
    if leaf == "captures.jsonl":
        artifact = run_dir / "artifacts/gate-results.json"
        _write_gate_result(artifact, passed=False)
        result = capture_execution_state(
            run_dir=run_dir,
            run_id=RUN_ID,
            mode="ledger-always",
            experiment_enabled=True,
            phase={"id": "gates", "routes": {"request-changes": "fix-loop"}},
            artifact_path=artifact,
            project_root=run_dir,
            round=1,
            generated_at=GENERATED_AT,
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
    elif leaf == "injections.jsonl":
        result = observe_execution_state_injection(
            run_dir=run_dir,
            run_id=RUN_ID,
            mode="ledger-always",
            experiment_enabled=True,
            phase={"id": "fix-loop"},
            project_root=run_dir,
            round=1,
            generated_at=GENERATED_AT,
        )
    else:
        result = record_execution_state_usage(
            run_dir=run_dir,
            run_id=RUN_ID,
            mode="ledger-always",
            experiment_enabled=True,
            event_id="usage.phase.1",
            generated_at=GENERATED_AT,
            scope="phase",
            phase_id="fix-loop",
            round=1,
            input_tokens=1,
            output_tokens=0,
            additional_tokens=0,
            latency_ms=1,
            estimated_cost_usd="0",
            model_id="model-1",
        )
    assert result["ok"] is False
    assert outside.read_text() == "sentinel\n"


@pytest.mark.parametrize("leaf", ("config.json", "ledger.json", "metrics.json"))
def test_json_leaf_symlinks_never_write_outside_run(tmp_path: Path, leaf: str) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _initialize(run_dir, "ledger-always")
    outside = tmp_path / f"outside-{leaf}"
    outside.write_text("sentinel\n")
    target = run_dir / "artifacts/execution-ledger" / leaf
    target.unlink()
    target.symlink_to(outside)
    artifact = run_dir / "artifacts/gate-results.json"
    _write_gate_result(artifact, passed=False)
    result = capture_execution_state(
        run_dir=run_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        phase={"id": "gates", "routes": {"request-changes": "fix-loop"}},
        artifact_path=artifact,
        project_root=run_dir,
        round=1,
        generated_at=GENERATED_AT,
        workflow_id=WORKFLOW_ID,
        route_key="request-changes",
        routed_to="fix-loop",
    )
    assert result["ok"] is False
    assert outside.read_text() == "sentinel\n"


def test_sidecar_parent_symlink_never_writes_outside_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    (run_dir / "artifacts").mkdir(parents=True)
    outside.mkdir()
    (outside / "sentinel").write_text("unchanged")
    (run_dir / "artifacts/execution-ledger").symlink_to(outside, target_is_directory=True)
    result = _initialize(run_dir, "ledger-always")
    assert result["ok"] is False
    assert [path.name for path in outside.iterdir()] == ["sentinel"]


def test_manual_terminal_usage_is_summary_only_and_invalid(tmp_path: Path) -> None:
    _initialize(tmp_path, "ledger-always")
    _mark_python_complete(tmp_path)
    result = record_execution_state_usage(
        run_dir=tmp_path,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        event_id="run-total-mismatch",
        generated_at=GENERATED_AT,
        scope="run-total",
        phase_id=None,
        round=None,
        input_tokens=10,
        output_tokens=2,
        additional_tokens=1,
        latency_ms=10,
        estimated_cost_usd="0",
        model_id="model-1",
    )
    assert result["ok"] is True
    metrics = json.loads((tmp_path / "artifacts/execution-ledger/metrics.json").read_text())
    assert metrics["gate_green_le_3"] is False
    assert metrics["gate_green_attempt"] is None
    assert metrics["actual_usage_budget_matched"] is False
    assert metrics["actual_usage_coverage"] is False
    assert metrics["summary_usage_coverage"] is True
    assert metrics["experiment_valid"] is False


def test_selective_and_action_have_equal_trigger_and_token_proxy_budgets(
    tmp_path: Path,
) -> None:
    def run_arm(mode: str) -> tuple[list[bool], dict[str, object]]:
        run_dir = tmp_path / mode
        run_dir.mkdir()
        _initialize(run_dir, mode)
        artifact = run_dir / "artifacts/gate-results.json"
        _write_gate_result(artifact, passed=False)
        phase = {"id": "gates", "routes": {"request-changes": "fix-loop"}}
        capture_execution_state(
            run_dir=run_dir,
            run_id=RUN_ID,
            mode=mode,
            experiment_enabled=True,
            phase=phase,
            artifact_path=artifact,
            project_root=run_dir,
            round=1,
            generated_at=GENERATED_AT,
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
        triggers: list[bool] = []
        for phase_id, round_number, generated_at in (
            ("implement", 1, GENERATED_AT),
            ("fix-loop", 1, "2026-07-11T00:00:01.000Z"),
        ):
            observed = observe_execution_state_injection(
                run_dir=run_dir,
                run_id=RUN_ID,
                mode=mode,
                experiment_enabled=True,
                phase={"id": phase_id},
                project_root=run_dir,
                round=round_number,
                generated_at=generated_at,
            )
            triggers.append(bool(observed["block"]))
        capture_execution_state(
            run_dir=run_dir,
            run_id=RUN_ID,
            mode=mode,
            experiment_enabled=True,
            phase=phase,
            artifact_path=artifact,
            project_root=run_dir,
            round=2,
            generated_at="2026-07-11T00:01:00.000Z",
            workflow_id=WORKFLOW_ID,
            route_key="request-changes",
            routed_to="fix-loop",
        )
        observed = observe_execution_state_injection(
            run_dir=run_dir,
            run_id=RUN_ID,
            mode=mode,
            experiment_enabled=True,
            phase={"id": "implement"},
            project_root=run_dir,
            round=2,
            generated_at="2026-07-11T00:01:01.000Z",
        )
        triggers.append(bool(observed["block"]))
        metrics = json.loads((run_dir / "artifacts/execution-ledger/metrics.json").read_text())
        return triggers, metrics

    selective_triggers, selective = run_arm("ledger-selective")
    action_triggers, action = run_arm("action-self-review")
    assert selective_triggers == action_triggers == [False, True, True]
    assert selective["injected_event_count"] == action["injected_event_count"] == 2
    assert selective["injected_token_proxy"] == action["injected_token_proxy"]


@pytest.mark.parametrize(
    "generated_at",
    (
        "2026-02-29T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T00:00:00",
        "0000-01-01T00:00:00Z",
        "0001-01-01T00:00:00+23:59",
        "2026-01-01T00:00:00+24:00",
        "2026-01-01T00:00:00.1234567Z",
    ),
)
def test_invalid_rfc3339_timestamp_rejection_matches_node(
    tmp_path: Path,
    generated_at: str,
) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    initialized = _initialize(python_dir, "ledger-always")
    result = record_execution_state_usage(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        event_id="timestamp-1",
        generated_at=generated_at,
        scope="phase",
        phase_id="fix-loop",
        round=1,
        input_tokens=1,
        output_tokens=0,
        additional_tokens=0,
        latency_ms=1,
        estimated_cost_usd="0",
        model_id="model-1",
    )
    node = _node_timestamp_scenario(node_dir, generated_at)
    assert {"init": initialized, "result": result} == node
    assert result["ok"] is False
    assert _sidecar_files(python_dir) == _sidecar_files(node_dir)


def test_offset_timestamp_normalization_bytes_match_node(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    node_dir = tmp_path / "node"
    python_dir.mkdir()
    node_dir.mkdir()
    initialized = _initialize(python_dir, "ledger-always")
    generated_at = "2026-07-11T09:30:00.123456+09:30"
    result = record_execution_state_usage(
        run_dir=python_dir,
        run_id=RUN_ID,
        mode="ledger-always",
        experiment_enabled=True,
        event_id="timestamp-1",
        generated_at=generated_at,
        scope="phase",
        phase_id="fix-loop",
        round=1,
        input_tokens=1,
        output_tokens=0,
        additional_tokens=0,
        latency_ms=1,
        estimated_cost_usd="0",
        model_id="model-1",
    )
    node = _node_timestamp_scenario(node_dir, generated_at)
    assert {"init": initialized, "result": result} == node
    assert _sidecar_files(python_dir) == _sidecar_files(node_dir)
    usage = json.loads((python_dir / "artifacts/execution-ledger/usage.jsonl").read_text())
    assert usage["generated_at"] == "2026-07-11T00:00:00.123Z"
