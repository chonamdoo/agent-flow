from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "mixed-architecture"


@pytest.fixture
def mixed_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(os.environ):
        if name.startswith("AGENT_FLOW_") or name in {
            "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_SANDBOX",
            "PI_CODING_AGENT_DIR", "OMP_SESSION_ID", "NODE_OPTIONS",
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        }:
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_FLOW_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("AGENT_FLOW_SKIP_CODEX_TRUST", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    assert shutil.which("node"), "Node is required for the real installer and ESLint"
    assert shutil.which("pnpm"), "Prepare the pinned fixture pnpm before running this suite"
    dependencies = FIXTURE / "node_modules"
    assert (dependencies / "eslint" / "package.json").is_file(), (
        "Run pnpm install --frozen-lockfile in tests/fixtures/mixed-architecture first"
    )
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project, ignore=shutil.ignore_patterns("node_modules", ".git"))
    (project / "node_modules").symlink_to(dependencies.resolve(), target_is_directory=True)
    return project


def _lint(project: Path, *, app: str | None = None) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    cwd = project / "apps" / app if app else project
    result = subprocess.run(
        ["pnpm", "run", "lint"], cwd=cwd, text=True, capture_output=True,
        check=False, timeout=60,
    )
    payloads = [line for line in result.stdout.splitlines() if line.startswith("[{")]
    assert len(payloads) == 1, result.stdout + result.stderr
    diagnostics = json.loads(payloads[0])
    source_roots = [cwd / "src"] if app else [project / "apps", project / "packages"]
    expected = {
        path.resolve()
        for source in source_roots
        for path in source.rglob("*")
        if path.suffix in {".ts", ".tsx"}
    }
    checked = {Path(item["filePath"]).resolve() for item in diagnostics}
    assert checked == expected and expected, result.stdout + result.stderr
    return result, diagnostics


def _assert_lint_passes(project: Path, *, app: str | None = None) -> list[dict]:
    result, diagnostics = _lint(project, app=app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(not item["messages"] for item in diagnostics), result.stdout
    return diagnostics


@pytest.mark.parametrize(
    ("relative_importer", "dependency", "rule_id"),
    [
        ("entities/product/model/product.ts", "@/features/cart", "import-x/no-restricted-paths"),
        ("features/cart/model/cart.ts", "../../search", "import-x/no-restricted-paths"),
        ("widgets/catalog/ui/catalog.tsx", "@/entities/product/model/product", "import-x/no-restricted-paths"),
        ("entities/product/index.ts", "./index.server", "import-x/no-restricted-paths"),
        ("features/cart/ui/cart.client.tsx", "@/entities/product/index.server", "mixed/client-server"),
    ],
    ids=["upward", "sibling-relative", "private-entry", "universal-server-export", "client-server"],
)
def test_main_boundaries_detect_and_recover(
    mixed_project: Path, relative_importer: str, dependency: str, rule_id: str,
) -> None:
    _assert_violation_and_recovery(
        mixed_project,
        mixed_project / "apps/main/src" / relative_importer,
        f'import * as boundaryProbe from "{dependency}";\nexport {{ boundaryProbe }};',
        rule_id,
    )


def _assert_violation_and_recovery(
    project: Path, importer: Path, statement: str, rule_id: str,
) -> None:
    _assert_lint_passes(project)
    original = importer.read_text(encoding="utf-8")
    try:
        importer.write_text(
            original + f"\n{statement}\n",
            encoding="utf-8",
        )
        result, diagnostics = _lint(project)
        assert result.returncode != 0, f"Forbidden import passed: {importer.relative_to(project)}: {statement}"
        assert any(
            message["ruleId"] == rule_id
            for item in diagnostics if Path(item["filePath"]).resolve() == importer.resolve()
            for message in item["messages"]
        ), result.stdout
    finally:
        importer.write_text(original, encoding="utf-8")
    _assert_lint_passes(project)


@pytest.mark.parametrize(
    ("relative_importer", "dependency"),
    [
        ("app/demo/accounts/page.tsx", "../orders/_lib/route-label"),
        ("app/demo/orders/history/page.tsx", "../_lib/route-label"),
        ("app/demo/orders/history/page.tsx", "../new/_components/note-field"),
        ("app/demo/orders/page.tsx", "./new/_lib/schema"),
        ("shared/lib/format.ts", "@/app/demo/orders/_lib/route-label"),
    ],
    ids=["foreign-section", "parent-lib", "sibling-components", "child-private", "outside-app"],
)
def test_bo_route_boundaries_detect_and_recover(
    mixed_project: Path, relative_importer: str, dependency: str,
) -> None:
    _assert_violation_and_recovery(
        mixed_project,
        mixed_project / "apps/bo/src" / relative_importer,
        f'import * as boundaryProbe from "{dependency}";\nexport {{ boundaryProbe }};',
        "import-x/no-restricted-paths",
    )


@pytest.mark.parametrize(
    ("relative_importer", "dependency", "import_kind"),
    [
        ("app/demo/orders/new/_lib/schema.ts", "react", "value"),
        ("app/demo/orders/new/_lib/schema.ts", "react", "type"),
        ("app/demo/orders/new/_lib/schema.ts", "next/server", "value"),
        ("app/demo/orders/new/_lib/schema.ts", "next", "type"),
        ("app/demo/orders/new/_lib/schema.ts", "react-hook-form", "value"),
        ("app/demo/orders/new/_lib/schema.ts", "react-hook-form", "type"),
        ("app/demo/orders/history/_lib/rhf-path.ts", "react-hook-form", "type"),
        ("app/demo/orders/new/_lib/rhf-path.ts", "react-hook-form", "value"),
        ("app/demo/orders/new/_lib/rhf-path.ts", "react", "type"),
        ("ui/text-input.tsx", "react-hook-form", "type"),
        ("app/demo/orders/new/_lib/rhf-path.ts", "@form-probe", "value"),
        ("app/demo/orders/new/_lib/rhf-path.ts", "relative-rhf", "value"),
        ("app/demo/orders/new/_lib/schema.ts", "react", "import-type"),
        ("app/demo/orders/new/_lib/rhf-path.ts", "react-hook-form", "dynamic"),
    ],
    ids=[
        "react-value", "react-type", "next-value", "next-type", "rhf-value", "rhf-type",
        "same-name-other-route", "helper-value", "helper-react", "primitive-rhf",
        "alias-value", "relative-value", "inline-import-type", "dynamic-value",
    ],
)
def test_bo_purity_and_rhf_type_exception_detect_and_recover(
    mixed_project: Path, relative_importer: str, dependency: str, import_kind: str,
) -> None:
    bo_root = mixed_project / "apps/bo"
    config_path = bo_root / "tsconfig.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["compilerOptions"]["paths"]["@form-probe"] = ["../../node_modules/react-hook-form"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    helper = bo_root / "src/app/demo/orders/new/_lib/rhf-path.ts"
    helper.write_text(
        helper.read_text(encoding="utf-8")
        + '\nimport { type FieldPath as PerSpecifierPath } from "@form-probe";\n'
        + "export type AllowedBoundaryPath = PerSpecifierPath<{ value: string }>;\n",
        encoding="utf-8",
    )
    importer = bo_root / "src" / relative_importer
    if not importer.exists():
        importer.write_text('export const probeScope = "other-route";\n', encoding="utf-8")
    if dependency == "relative-rhf":
        dependency = Path(os.path.relpath(mixed_project / "node_modules/react-hook-form", importer.parent)).as_posix()
    if import_kind == "dynamic":
        statement = f'export const BoundaryProbe = import("{dependency}");'
    elif import_kind == "import-type":
        statement = f'export type BoundaryProbe = import("{dependency}").ReactNode;'
    elif import_kind == "type":
        statement = f'import type * as BoundaryProbe from "{dependency}";\nexport type {{ BoundaryProbe }};'
    else:
        statement = f'import * as BoundaryProbe from "{dependency}";\nexport {{ BoundaryProbe }};'
    _assert_violation_and_recovery(mixed_project, importer, statement, "mixed/purity")


@pytest.mark.parametrize(
    ("importer_name", "statement"),
    [
        (
            "schema.ts",
            'import { NoteField as BoundaryProbe } from "../_components/note-field";\n'
            'export { BoundaryProbe };',
        ),
        (
            "schema.ts",
            'import { SectionTitle as BoundaryProbe } from "../../_components/section-title";\n'
            'export { BoundaryProbe };',
        ),
        (
            "schema.ts",
            'import { TextInput as BoundaryProbe } from "@/ui/text-input";\n'
            'export { BoundaryProbe };',
        ),
        (
            "schema.ts",
            'import type * as BoundaryProbe from "../_components/note-field";\n'
            'export type { BoundaryProbe };',
        ),
        (
            "schema.ts",
            'export { NoteField as BoundaryProbe } from "../_components/note-field";',
        ),
        (
            "schema.ts",
            'export type * from "../_components/note-field";',
        ),
        (
            "rhf-path.ts",
            'import type * as BoundaryProbe from "../_components/note-field";\n'
            'export type { BoundaryProbe };',
        ),
    ],
    ids=[
        "same-route-ui", "allowed-parent-ui", "alias-primitive", "type-only-ui",
        "direct-reexport", "type-star-reexport", "rhf-helper-local-ui",
    ],
)
def test_bo_local_ui_purity_detects_and_recovers(
    mixed_project: Path, importer_name: str, statement: str,
) -> None:
    _assert_violation_and_recovery(
        mixed_project,
        mixed_project / "apps/bo/src/app/demo/orders/new/_lib" / importer_name,
        statement,
        "mixed/purity",
    )


def test_bo_ui_directory_purity_rejects_plain_ts_and_recovers(mixed_project: Path) -> None:
    route = mixed_project / "apps/bo/src/app/demo/orders/new"
    ui = route / "_ui"
    ui.mkdir()
    (ui / "purity-probe.ts").write_text("export const uiProbe = 1;\n", encoding="utf-8")
    _assert_violation_and_recovery(
        mixed_project,
        route / "_lib/schema.ts",
        'import { uiProbe } from "../_ui/purity-probe";\nexport { uiProbe };',
        "mixed/purity",
    )


def test_fixture_compiles_and_rejects_misspelled_rhf_leaf(mixed_project: Path) -> None:
    compiler = mixed_project / "node_modules/typescript/bin/tsc"
    assert compiler.is_file(), "Prepare the pinned fixture TypeScript dependency"

    def compile_app(app: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "node", str(compiler), "--project", f"apps/{app}/tsconfig.json",
                "--noEmit", "--incremental", "false", "--pretty", "false",
            ],
            cwd=mixed_project, text=True, capture_output=True, check=False, timeout=60,
        )

    probe = mixed_project / "apps/bo/src/app/demo/orders/new/_lib/leaf-probe.ts"
    valid = (
        'import { notePath } from "./rhf-path";\n'
        'export const leaf: ReturnType<typeof notePath> = "lines.0.note";\n'
    )
    probe.write_text(valid, encoding="utf-8")
    for app in ("main", "bo"):
        result = compile_app(app)
        assert result.returncode == 0, result.stdout + result.stderr

    probe.write_text(
        valid.replace('"lines.0.note"', '"lines.0.ntoe"'), encoding="utf-8",
    )
    rejected = compile_app("bo")
    assert rejected.returncode != 0, "The misspelled RHF leaf was accepted"
    assert any(
        "leaf-probe.ts(" in line and "error TS2322:" in line
        for line in rejected.stdout.splitlines()
    ), rejected.stdout + rejected.stderr
    probe.unlink()
    recovered = compile_app("bo")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr


def test_bo_schema_normalization_and_field_validation(mixed_project: Path) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", """
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import ts from "typescript";

for (const name of ["schema", "transform"]) {
  const source = readFileSync(`apps/bo/src/app/demo/orders/new/_lib/${name}.ts`, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  writeFileSync(`${name}-runtime.mjs`, compiled.outputText);
}
const { orderSchema, validateReference, validateNote } = await import("./schema-runtime.mjs");
const { toPayload } = await import("./transform-runtime.mjs");
const draft = {
  reference: "  ORD-225  ",
  lines: [{ kind: "note", note: "  Ship carefully  " }, { kind: "quantity", quantity: "2" }],
};
const parsed = orderSchema.parse(draft);
assert.deepEqual(toPayload(parsed), {
  reference: "ORD-225",
  lines: [{ kind: "note", text: "Ship carefully" }, { kind: "quantity", units: 2 }],
});
assert.equal(draft.reference, "  ORD-225  ");
assert.equal(draft.lines[1].quantity, "2");
assert.equal(validateReference(draft.reference), undefined);
assert.equal(validateNote(draft.lines[0].note), undefined);
const invalid = orderSchema.safeParse({ reference: " ", lines: [{ kind: "note", note: " " }] });
assert.equal(invalid.success, false);
assert.equal(validateReference(" "), invalid.error.issues.find(i => i.path[0] === "reference").message);
assert.equal(validateNote(" "), invalid.error.issues.find(i => i.path[2] === "note").message);
const fraction = orderSchema.safeParse({
  reference: "ORD-225", lines: [{ kind: "quantity", quantity: "2.5" }],
});
assert.equal(fraction.success, false);
assert.deepEqual(fraction.error.issues[0].path, ["lines", 0, "quantity"]);
"""],
        cwd=mixed_project, text=True, capture_output=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
