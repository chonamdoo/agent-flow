#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path


EVENT_NAMES = {
    "PreToolUse": "pre_tool_use",
    "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "Stop": "stop",
}


def main() -> int:
    if "--version" in sys.argv:
        print("codex-cli fake")
        return 0
    if sys.argv[1:] != ["app-server", "--stdio"]:
        return 2
    if os.environ.get("FAKE_CODEX_MODE") == "fail-query":
        return 3
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("id") == 1:
            respond({"id": 1, "result": {"codexHome": codex_home()}})
        elif request.get("id") == 2:
            cwd = Path(request["params"]["cwds"][0]).resolve()
            respond({"id": 2, "result": hooks_result(cwd)})
    return 0


def hooks_result(cwd: Path) -> dict[str, object]:
    source = cwd / ".codex/hooks.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    hooks: list[dict[str, object]] = []
    display_order = 0
    for event, entries in config.get("hooks", {}).items():
        event_name = EVENT_NAMES.get(event, event.lower())
        for entry_index, entry in enumerate(entries):
            for hook_index, hook in enumerate(entry.get("hooks", [])):
                command = hook.get("command", "")
                key = (
                    f"{source}:{event_name}:{entry_index}:{hook_index}"
                    f"{os.environ.get('FAKE_CODEX_HOOK_KEY_SUFFIX', '')}"
                )
                digest = hashlib.sha256(
                    json.dumps(
                        [event_name, entry.get("matcher"), hook],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                current_hash = f"sha256:{digest}"
                hooks.append(
                    {
                        "key": key,
                        "command": command,
                        "sourcePath": str(source),
                        "currentHash": current_hash,
                        "trustStatus": trust_status(key, current_hash),
                        "enabled": True,
                        "displayOrder": display_order,
                    }
                )
                display_order += 1
    mode = os.environ.get("FAKE_CODEX_MODE", "")
    script_mutation = os.environ.get("FAKE_CODEX_SCRIPT_MUTATION", "")
    query_count = 1
    if mode.startswith("verify-") or script_mutation:
        query_count = _next_query_count()
    if mode.startswith("verify-"):
        if query_count == 1:
            mode = ""
        else:
            mode = mode.removeprefix("verify-")
    _mutate_managed_hooks(hooks, mode)
    if script_mutation and query_count == int(
        os.environ.get("FAKE_CODEX_SCRIPT_MUTATION_QUERY", "1")
    ):
        _mutate_managed_script(hooks, script_mutation)
    return {"data": [{"cwd": str(cwd), "hooks": hooks, "warnings": [], "errors": []}]}


def _next_query_count() -> int:
    counter_path = Path(codex_home()) / ".fake-hooks-list-count"
    try:
        query_count = int(counter_path.read_text(encoding="utf-8")) + 1
    except (OSError, ValueError):
        query_count = 1
    counter_path.write_text(str(query_count), encoding="utf-8")
    return query_count


def _mutate_managed_hooks(hooks: list[dict[str, object]], mode: str) -> None:
    if mode == "subset-managed" and hooks:
        hooks.pop()
    elif mode == "extra-managed" and hooks:
        extra = dict(hooks[0])
        extra["key"] = f"{extra['key']}:extra"
        extra["displayOrder"] = len(hooks)
        hooks.append(extra)
    elif mode == "duplicate-managed" and len(hooks) > 1:
        hooks[-1] = {
            **hooks[-1],
            "command": hooks[0]["command"],
        }


def _mutate_managed_script(
    hooks: list[dict[str, object]], mutation: str
) -> None:
    script = next(
        (
            candidate
            for hook in hooks
            if (candidate := _managed_script_path(str(hook.get("command", ""))))
            is not None
            and candidate.name == "guard-worktree.sh"
        ),
        None,
    )
    if script is None or not script.is_file():
        raise RuntimeError("managed script mutation target is unavailable")
    if mutation == "content":
        script.write_bytes(script.read_bytes() + b"race mutation\n")
    elif mutation == "mode":
        script.chmod(0o700)
    elif mutation == "symlink":
        target = script.with_name(f".{script.name}.race-target")
        target.write_bytes(script.read_bytes())
        target.chmod(0o755)
        script.unlink()
        script.symlink_to(target.name)
    else:
        raise RuntimeError(f"unknown managed script mutation: {mutation}")


def _managed_script_path(command: str) -> Path | None:
    match = re.search(r" '([A-Za-z0-9+/=]+)' '[0-9a-f]{64}'$", command)
    if match is not None:
        try:
            decoded = base64.b64decode(match.group(1), validate=True)
            if base64.b64encode(decoded).decode("ascii") == match.group(1):
                return Path(decoded.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None
    normalized = command.strip("'\"")
    return Path(normalized) if normalized else None


def trust_status(key: str, current_hash: str) -> str:
    if os.environ.get("FAKE_CODEX_MODE") == "never-trust":
        return "untrusted"
    config_path = Path(codex_home()) / "config.toml"
    if not config_path.is_file():
        return "untrusted"
    config = config_path.read_text(encoding="utf-8")
    header = f"[hooks.state.{json.dumps(key, ensure_ascii=False)}]"
    table = re.search(
        rf"^{re.escape(header)}\n(?P<body>(?:(?!^\[).)*(?:\n|$))",
        config,
        flags=re.MULTILINE | re.DOTALL,
    )
    if table and f'trusted_hash = "{current_hash}"' in table.group("body"):
        return "trusted"
    return "untrusted"


def codex_home() -> str:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return str(Path(configured).resolve())
    return str((Path(os.environ["HOME"]) / ".codex").resolve())


def respond(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
