from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "hooks" / "comment-checker.py"


def run_checker(tool_input: dict[str, object]) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Edit",
        "hook_event_name": "PostToolUse",
        "cwd": str(ROOT),
        "tool_input": tool_input,
    }
    return subprocess.run(
        (sys.executable, str(CHECKER)),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def run_payload(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, str(CHECKER)),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_blocks_new_low_value_comment() -> None:
    result = run_checker(
        {
            "file_path": "sample.py",
            "old_string": "value = 1\n",
            "new_string": "# Initialize value\nvalue = 1\n",
        }
    )

    assert result.returncode == 2
    assert "low-value comments" in result.stdout
    assert "Initialize value" in result.stdout


def test_blocks_new_inline_low_value_comment() -> None:
    result = run_checker(
        {
            "file_path": "sample.py",
            "old_string": "value = 1\n",
            "new_string": "value = 1  # Initialize value\n",
        }
    )

    assert result.returncode == 2
    assert "Initialize value" in result.stdout


def test_blocks_todo_and_section_even_with_detail_words() -> None:
    todo_result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": "const value = 1;\n// TODO: avoid\n",
        }
    )
    section_result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": "const value = 1;\n// ----- before -----\n",
        }
    )

    assert todo_result.returncode == 2
    assert section_result.returncode == 2


def test_allows_todo_with_owner_and_trigger() -> None:
    result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": "const value = 1;\n// TODO(alice): remove after API v2 ships.\n",
        }
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_blocks_decorative_section_even_with_allowed_reason_words() -> None:
    result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": "const value = 1;\n// ----- why -----\n",
        }
    )

    assert result.returncode == 2


def test_ignores_comment_markers_inside_string_literals() -> None:
    result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const label = '';\n",
            "new_string": "const hashLabel = '# Set value';\nconst slashLabel = '// Initialize value';\n",
        }
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_ignores_comment_markers_inside_multiline_strings_and_regex() -> None:
    python_result = run_checker(
        {
            "file_path": "sample.py",
            "old_string": "text = ''\n",
            "new_string": 'text = """\n# Initialize value\n"""\n',
        }
    )
    typescript_result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const text = '';\n",
            "new_string": "const text = `\n// Initialize value\n/* Set value */\n`;\nconst re = /# Set value/;\n",
        }
    )

    assert python_result.returncode == 0
    assert python_result.stdout == ""
    assert typescript_result.returncode == 0
    assert typescript_result.stdout == ""


def test_blocks_duplicate_low_value_comment_added_again() -> None:
    result = run_checker(
        {
            "file_path": "sample.py",
            "old_string": "# Initialize value\nvalue = 1\n",
            "new_string": "# Initialize value\n# Initialize value\nvalue = 1\n",
        }
    )

    assert result.returncode == 2
    assert "Initialize value" in result.stdout


def test_allows_why_and_public_api_comments() -> None:
    result = run_checker(
        {
            "file_path": "Sample.swift",
            "old_string": "func load() {}\n",
            "new_string": "/// Public API contract: call on MainActor.\nfunc load() {}\n// Workaround: iOS 17 reports stale permission state.\n",
        }
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_apply_patch_detects_added_comment() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
+// Loop through items
+items.forEach(run)
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 2
    assert "src/demo.ts" in result.stdout


def test_freeform_apply_patch_detects_added_comment() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
+// Set active flag
+active = true
*** End Patch
"""
    result = run_payload(
        {
            "tool_name": "apply_patch",
            "hook_event_name": "PostToolUse",
            "cwd": str(ROOT),
            "tool_input": patch,
        }
    )

    assert result.returncode == 2
    assert "Set active flag" in result.stdout


def test_apply_patch_detects_added_inline_comment() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
+const value = 1 // Set value
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 2
    assert "Set value" in result.stdout


def test_apply_patch_ignores_comment_markers_inside_multiline_strings() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
+const text = `
+// Initialize value
+`;
+const re = /# Set value/;
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 0
    assert result.stdout == ""


def test_apply_patch_uses_context_for_multiline_string_state() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
 const text = `
+// Initialize value
 `;
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 0
    assert result.stdout == ""


def test_apply_patch_detects_multiline_block_comment() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
+/*
+ * Initialize value
+ */
+const value = 1;
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 2
    assert "Initialize value" in result.stdout


def test_apply_patch_detects_block_comment_when_delimiters_are_context() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
 /*
+ * Initialize value
 */
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 2
    assert "Initialize value" in result.stdout


def test_write_detects_multiline_block_comment() -> None:
    result = run_payload(
        {
            "tool_name": "Write",
            "hook_event_name": "PostToolUse",
            "cwd": str(ROOT),
            "tool_input": {
                "file_path": "sample.ts",
                "content": "/* Set value */\nconst value = 1;\n",
            },
        }
    )

    assert result.returncode == 2
    assert "Set value" in result.stdout


def test_blocks_low_value_line_added_inside_allowed_block_comment() -> None:
    result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "/**\n * Why: external API requires this shape.\n */\nconst value = 1;\n",
            "new_string": (
                "/**\n"
                " * Why: external API requires this shape.\n"
                " * Set value\n"
                " */\n"
                "const value = 1;\n"
            ),
        }
    )

    assert result.returncode == 2
    assert "Set value" in result.stdout


def test_allows_constraint_detail_line_inside_block_comment() -> None:
    result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": (
                "/**\n"
                " * Why: external API requires this shape.\n"
                " * Set value before calling submit.\n"
                " */\n"
                "const value = 1;\n"
            ),
        }
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_allows_constraint_detail_line_with_block_context() -> None:
    result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": (
                "/**\n"
                " * External API uses string IDs.\n"
                " * Convert IDs to strings.\n"
                " */\n"
                "const value = 1;\n"
            ),
        }
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_blocks_todo_and_section_inside_allowed_block_context() -> None:
    todo_result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": (
                "/**\n"
                " * Why: external API requires this shape.\n"
                " * TODO: avoid\n"
                " */\n"
                "const value = 1;\n"
            ),
        }
    )
    section_result = run_checker(
        {
            "file_path": "sample.ts",
            "old_string": "const value = 1;\n",
            "new_string": (
                "/**\n"
                " * Why: external API requires this shape.\n"
                " * ----- before -----\n"
                " */\n"
                "const value = 1;\n"
            ),
        }
    )

    assert todo_result.returncode == 2
    assert section_result.returncode == 2


def test_apply_patch_blocks_low_value_line_added_inside_allowed_block_comment() -> None:
    patch = """*** Begin Patch
*** Update File: src/demo.ts
@@
 /**
  * Why: external API requires this shape.
+ * Set value
  */
 const value = 1;
*** End Patch
"""
    result = run_checker({"command": patch})

    assert result.returncode == 2
    assert "Set value" in result.stdout


def test_multi_edit_detects_added_comment() -> None:
    result = run_payload(
        {
            "tool_name": "MultiEdit",
            "hook_event_name": "PostToolUse",
            "cwd": str(ROOT),
            "tool_input": {
                "file_path": "sample.ts",
                "edits": [
                    {
                        "old_string": "const value = 1;\n",
                        "new_string": "// Return value\nconst value = 1;\n",
                    }
                ],
            },
        }
    )

    assert result.returncode == 2
    assert "Return value" in result.stdout


def test_multi_edit_detects_added_inline_comment() -> None:
    result = run_payload(
        {
            "tool_name": "MultiEdit",
            "hook_event_name": "PostToolUse",
            "cwd": str(ROOT),
            "tool_input": {
                "file_path": "sample.ts",
                "edits": [
                    {
                        "old_string": "const active = true;\n",
                        "new_string": "const active = true; // Check active\n",
                    }
                ],
            },
        }
    )

    assert result.returncode == 2
    assert "Check active" in result.stdout
