"""#423 평가 컨테이너 정의의 정적 점검. 이미지에 자격 증명이 들어가거나 버전이 풀리거나
배포 kit 트리에 섞이면 평가 환경이 재현되지 않거나 비밀이 새어 나간다."""
from __future__ import annotations

import json
import re
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
    ignore = (ENV / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    # 빌드 context는 전부 막고 필요한 것만 연다. 자격 증명 파일이 context에 들어갈 길이 없다.
    assert [line for line in ignore if line and not line.startswith("#")][0] == "*"


def test_no_secret_is_baked_into_env_or_arg():
    for keyword, rest in _instructions():
        if keyword in {"ENV", "ARG"}:
            for name, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(\S*)", rest):
                assert not (SECRET_NAME.search(name) and value), f"{keyword} {name} carries a value"


def test_tool_versions_are_pinned():
    rows = _instructions()
    base = next(rest for keyword, rest in rows if keyword == "FROM")
    assert re.fullmatch(r"node:\d+\.\d+\.\d+-\w+", base), base
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
