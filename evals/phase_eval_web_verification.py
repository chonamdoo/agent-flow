from __future__ import annotations

from pathlib import Path

from phase_eval_environment import _command, _executable, environment_for_project, fixture_typecheck


def verify_web_review(project: Path) -> list[dict]:
    project = project.resolve()
    runtime = Path(__file__).parent / "phase-cases" / "web" / "verification" / "orders-component.cjs"
    checks = [{"kind": "typecheck", **fixture_typecheck(project)}]
    checks.append({
        "kind": "component-runtime",
        "scope": "OrdersClient SSR and DOM hydration with controlled API responses; framework build and deployed browser verification remain CI/integration owned.",
        **_command([_executable("node"), str(runtime), str(project)], project,
                   environment_for_project(project)),
    })
    return checks
