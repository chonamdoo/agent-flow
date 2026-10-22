"""#423 평가 컨테이너 정의의 정적 점검. 이미지에 자격 증명이 들어가거나 버전이 풀리거나
배포 kit 트리에 섞이면 평가 환경이 재현되지 않거나 비밀이 새어 나간다."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "tools" / "eval-env"
DOCKERFILE = ENV / "Dockerfile"
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
CREDENTIAL_PATH = re.compile(r"(\.codex|\.claude|auth\.json|credentials|\.env\b|\.netrc|\.npmrc|\.ssh)", re.I)


def _instructions() -> list[tuple[str, str]]:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, _, rest = line.partition(" ")
            rows.append((keyword.upper(), rest))
    return rows


def test_the_image_copies_no_credentials():
    for keyword, rest in _instructions():
        if keyword in {"COPY", "ADD"}:
            assert not CREDENTIAL_PATH.search(rest), rest
            # 폴더째 복사하면 그 안에 잘못 둔 .env·.npmrc까지 이미지에 들어간다. 파일 이름만 적는다.
            sources = [part for part in rest.split()[:-1] if not part.startswith("--")]
            assert all((ROOT / source).is_file() for source in sources), rest
    ignore = [line for line in (ENV / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
              if line and not line.startswith("#")]
    # 빌드 context는 전부 막고 이름을 적은 파일만 연다.
    assert ignore[0] == "*"
    for line in ignore[1:]:
        assert line.startswith("!") and not any(char in line for char in "*?[") and (ROOT / line[1:]).is_file(), line


def test_no_secret_is_baked_into_env_or_arg():
    for keyword, rest in _instructions():
        if keyword in {"ENV", "ARG"}:
            for name, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(\S*)", rest):
                assert not (SECRET_NAME.search(name) and value), f"{keyword} {name} carries a value"


def test_tool_versions_are_pinned():
    rows = _instructions()
    base = next(rest for keyword, rest in rows if keyword == "FROM").split()[-1]
    assert re.fullmatch(r"node:\d+\.\d+\.\d+-\w+@sha256:[0-9a-f]{64}", base), base
    args = dict(re.findall(r"ARG ([A-Z0-9_]+)=(\S+)", DOCKERFILE.read_text(encoding="utf-8")))
    for name in ("CLAUDE_CODE_VERSION", "CODEX_VERSION", "PLAYWRIGHT_VERSION", "FLUTTER_VERSION"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", args[name]), name
    assert re.fullmatch(r"[0-9a-f]{64}", args["FLUTTER_SHA256"])
    installs = [rest for keyword, rest in rows if keyword == "RUN" and "npm install -g" in rest]
    packages = re.findall(r'"([^"\s]+)"', installs[0])
    assert packages and all(re.search(r"@\$\{[A-Z_]+_VERSION\}$", package) for package in packages)


def test_child_shells_inherit_path_and_home_is_writable():
    text = DOCKERFILE.read_text(encoding="utf-8")
    env = " ".join(rest for keyword, rest in _instructions() if keyword == "ENV")
    for directory in ("/opt/venv/bin", "/opt/flutter/bin", "/usr/local/bin"):
        assert directory in env
    assert "HOME=/home/eval" in env and "FLUTTER_SUPPRESS_ANALYTICS=true" in env
    assert "--no-analytics" in text and "--disable-analytics" in text
    assert (ENV / "bin" / "agent-flow").stat().st_mode & 0o111


def test_compose_mounts_credentials_read_only_and_bakes_none():
    compose = yaml.safe_load((ENV / "compose.yml").read_text(encoding="utf-8"))
    assert {"check", "skill_routing", "phase_eval", "ui_task"} <= set(compose["services"])
    text = (ENV / "compose.yml").read_text(encoding="utf-8")
    assert not CREDENTIAL_PATH.search(text.split("services:", 1)[1])
    auth = yaml.safe_load((ENV / "compose.auth.yml").read_text(encoding="utf-8"))
    for service in auth["services"].values():
        for volume in service["volumes"]:
            assert volume.endswith(":ro"), volume
        assert "environment" not in service


def test_the_tree_is_not_a_shipped_kit_asset():
    """tools/는 배포되지 않는다. 여기 둔 것이 설치본이나 kit 지문에 섞이면 모든 프로젝트로 퍼진다."""
    files = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"]
    assert not any(entry == "tools" or entry.startswith("tools/") for entry in files)
    trees = re.search(r"RECORDED_KIT_ASSET_TREES = Object\.freeze\(\[(.*?)\]\);",
                      (ROOT / "lib" / "installer-shared.mjs").read_text(encoding="utf-8"), re.S).group(1)
    assert "tools" not in re.findall(r'src: "([^"]+)"', trees)
    from agent_flow.core.kit_digest import KIT_SOURCE_DIGEST_ROOTS
    assert not any(root == "tools" or root.startswith("tools/") for root in KIT_SOURCE_DIGEST_ROOTS)
    js_roots = re.search(r"const KIT_SOURCE_DIGEST_ROOTS = \[(.*?)\];",
                         (ROOT / "bin" / "agent-flow-kit.mjs").read_text(encoding="utf-8"), re.S).group(1)
    assert "tools" not in js_roots


def test_python_dependencies_are_locked_to_what_pyproject_asks_for():
    import tomllib
    lock = {line.split("==")[0].lower(): line.split("==")[1] for line in
            (ENV / "requirements.lock").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")}
    assert all(re.fullmatch(r"\d+(\.\d+)*", version) for version in lock.values())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for requirement in [*project["dependencies"], *project["optional-dependencies"]["dev"]]:
        assert re.split(r"[<>=!~ ]", requirement, maxsplit=1)[0].lower() in lock, requirement


def _check(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    import os
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, output in (("claude", "2.1.308 (Claude Code)"), ("codex", "codex-cli 0.163.0"), ("node", "v22"),
                         ("flutter", "Flutter 3.47.7"), ("dart", "Dart 3"), ("playwright", "Version 1.56.1"),
                         ("agent-flow", "")):
        tool = bin_dir / name
        tool.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
        tool.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run([sys.executable, str(ENV / "bin" / "eval-env-check")], capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:{os.path.dirname(sys.executable)}:/usr/bin:/bin", "HOME": str(home), **env})


def test_the_startup_check_rejects_a_wrong_cli_version_and_unrecorded_analytics(tmp_path):
    telemetry = tmp_path / "home" / ".dart-tool" / "dart-flutter-telemetry.config"
    telemetry.parent.mkdir(parents=True)
    telemetry.write_text("reporting=0\n", encoding="utf-8")
    assert _check(tmp_path, {"EVAL_EXPECT_CODEX": "0.163.0"}).returncode == 0
    wrong = _check(tmp_path, {"EVAL_EXPECT_CODEX": "0.170.0"})
    assert wrong.returncode == 1 and "expected version 0.170.0" in wrong.stderr
    telemetry.unlink()
    missing = _check(tmp_path, {})
    assert missing.returncode == 1 and "analytics is not recorded as off" in missing.stderr


def test_the_startup_check_reports_an_unreadable_auth_mount(tmp_path):
    import os
    if os.getuid() == 0:
        import pytest
        pytest.skip("root reads any file")
    telemetry = tmp_path / "home" / ".dart-tool" / "dart-flutter-telemetry.config"
    telemetry.parent.mkdir(parents=True)
    telemetry.write_text("reporting=0\n", encoding="utf-8")
    auth = tmp_path / "home" / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0)
    result = _check(tmp_path, {})
    assert result.returncode == 1 and "not readable" in result.stderr
