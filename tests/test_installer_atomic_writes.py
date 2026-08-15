"""install이 host·hook이 읽는 설정 파일을 반쪽 상태로 노출하지 않는지 본다.

두 진입점은 `.claude/settings.json`·`.Codex/hooks.json`·`.agent-flow/kit.json`·
`.omp/extensions/agent-flow-hooks.ts`를 제자리 truncate로 썼다. install 중에는
host의 hook이 계속 돌고 hook_integrity가 kit.json을 읽으므로, 그 순간 잘린 JSON을
읽으면 파싱이 죽거나 등록이 사라진 것으로 보인다. 판정은 "무엇으로 썼는가"가 아니라
"관측자에게 무엇이 보였는가"라서, 실제 install의 파일시스템 호출을 가로채 본다.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SHARED_MODULE = KIT_ROOT / "lib" / "installer-shared.mjs"
sys.path.insert(0, str(KIT_ROOT / "src"))

from agent_flow.core.atomic_io import atomic_write_text  # noqa: E402


# install이 도는 동안 host와 hook_integrity가 읽는 파일. 여기에 반쪽 내용이 보이면
# 그 프롬프트의 승인이 조용히 죽거나 run 시작 게이트가 근거 없이 막힌다.
HOST_READ_CONFIG = (
    ".claude/settings.json",
    ".Codex/hooks.json",
    ".codex/hooks.json",
    ".agent-flow/kit.json",
    ".omp/extensions/agent-flow-hooks.ts",
)

# 로그는 가로챈 원본 `writeFileSync`로 남긴다. 래퍼로 남기면 자기 로그를 다시
# 가로채 무한 재귀가 된다.
WRITE_PROBE = """
import fs from "node:fs";

const log = process.env.AGENT_FLOW_WRITE_PROBE_LOG;
const realWrite = fs.writeFileSync;
const realRename = fs.renameSync;
let recording = false;
const record = (op, target) => {
  // fd로 쓰는 호출은 staging 파일 안쪽이라 관측 대상이 아니다.
  if (recording || typeof target !== "string") return;
  recording = true;
  try {
    realWrite.call(fs, log, `${JSON.stringify({ op, target })}\\n`, { encoding: "utf8", flag: "a" });
  } finally {
    recording = false;
  }
};
fs.writeFileSync = function (target, ...rest) {
  record("writeFileSync", target);
  return realWrite.call(this, target, ...rest);
};
fs.renameSync = function (from, to) {
  record("renameSync", to);
  return realRename.call(this, from, to);
};
"""

# fsync 직후에 죽는 것이 최악의 순간이다 - 내용은 이미 디스크에 있고 공개만 안 됐다.
INTERRUPTED_PUBLISH = """
import fs from "node:fs";
import { atomicWriteFileSync } from %(module)s;

const target = %(target)s;
const boom = new Error("simulated crash before publish");
const realFsync = fs.fsyncSync;
fs.fsyncSync = () => {
  throw boom;
};
let raised = null;
try {
  atomicWriteFileSync(target, %(payload)s);
} catch (error) {
  raised = error === boom ? "injected" : String(error);
} finally {
  fs.fsyncSync = realFsync;
}
process.stdout.write(JSON.stringify({
  raised,
  observed: fs.readFileSync(target, "utf8"),
  leftovers: fs.readdirSync(%(dir)s).filter((name) => name.includes(".tmp")),
}));
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node를 찾을 수 없다")
    return node


def _observed_writes(project: Path, binary: str) -> dict[str, set[str]]:
    """install을 실제로 돌리고, 대상 경로마다 어떤 호출이 그것을 만들었는지 모은다.

    `NODE_OPTIONS`로 심어야 `install.mjs`가 위임하는 자식 `kit.mjs` install까지
    같은 눈으로 본다 - 자식만 제자리로 쓰면 반쪽 파일은 그대로 보인다.
    """
    probe = project.parent / "watch-writes.mjs"
    probe.write_text(WRITE_PROBE, encoding="utf-8")
    log = project.parent / "writes.jsonl"
    log.write_text("", encoding="utf-8")
    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install"),
        cwd=project,
        env={
            **os.environ,
            "AGENT_FLOW_WRITE_PROBE_LOG": str(log),
            "NODE_OPTIONS": f"--import {probe.as_uri()}",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    observed: dict[str, set[str]] = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        observed.setdefault(record["target"], set()).add(record["op"])
    return observed


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_publishes_host_read_config_by_rename(tmp_path: Path, binary: str) -> None:
    """반증: 제자리 `writeFileSync`는 truncate와 쓰기 사이에 빈 파일을 노출한다.

    두 진입점을 모두 태운다. 한쪽만 고치면 "어느 CLI로 깔았는가"에 따라 안전성이
    갈리고, 그 차이는 재현 조건에 남지 않는다.
    """
    project = tmp_path / "project"
    project.mkdir()

    observed = _observed_writes(project, binary)

    for relative in HOST_READ_CONFIG:
        target = str(project / relative)
        ops = observed.get(target, set())
        assert ops, f"{relative}가 install에서 아예 기록되지 않았다"
        assert "writeFileSync" not in ops, (
            f"{relative}를 제자리로 썼다: install 중 hook이 반쪽 파일을 읽는다"
        )
        assert "renameSync" in ops, f"{relative}가 rename으로 공개되지 않았다"
        assert json_or_text_is_complete(project / relative), f"{relative}가 잘린 채 남았다"


def json_or_text_is_complete(target: Path) -> bool:
    text = target.read_text(encoding="utf-8")
    if target.suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return False
        return True
    return bool(text.strip())


def test_interrupted_publish_keeps_the_previous_config_readable(tmp_path: Path) -> None:
    """반증: 쓰기가 중단되면 제자리 쓰기는 잘린 JSON을 남기고, 다음 실행은 그것을
    "설정 없음"이 아니라 손상으로 만난다. staging+rename은 이전 판본을 남긴다.
    """
    target = tmp_path / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    previous = '{\n  "hooks": {}\n}\n'
    target.write_text(previous, encoding="utf-8")

    script = INTERRUPTED_PUBLISH % {
        "module": json.dumps(str(SHARED_MODULE)),
        "target": json.dumps(str(target)),
        "payload": json.dumps(f'{{"hooks": {{"PreToolUse": []}}}}\n{"x" * 4096}'),
        "dir": json.dumps(str(target.parent)),
    }
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    outcome = json.loads(result.stdout)
    assert outcome["raised"] == "injected", outcome
    assert outcome["observed"] == previous, "중단된 쓰기가 이전 판본을 갈아치웠다"
    assert outcome["leftovers"] == [], "staging 파일이 남아 다음 install이 그것을 본다"


# 두 writer는 같은 이름을 쓰고 parity가 쌍으로 묶는다. 권한 정책이 갈리면 같은
# 파일이 install(JS)과 run(Python) 중 누가 마지막에 썼는지에 따라 다른 권한을
# 갖는다. umask 077은 그 차이가 실제로 드러나는 자리다.
PERMISSION_PROBE = """
import fs from "node:fs";
import { atomicWriteFileSync } from %(module)s;

process.umask(%(umask)s);
const dir = %(dir)s;
const fresh = `${dir}/fresh.json`;
atomicWriteFileSync(fresh, "{}\\n");
const kept = `${dir}/kept.json`;
fs.writeFileSync(kept, "{}\\n");
fs.chmodSync(kept, 0o640);
atomicWriteFileSync(kept, '{"a": 1}\\n');
const explicit = `${dir}/explicit.json`;
atomicWriteFileSync(explicit, "{}\\n", { mode: 0o600 });
process.stdout.write(JSON.stringify({
  fresh: fs.statSync(fresh).mode & 0o777,
  kept: fs.statSync(kept).mode & 0o777,
  explicit: fs.statSync(explicit).mode & 0o777,
}));
"""


def _python_modes(directory: Path, umask: int) -> dict[str, int]:
    directory.mkdir(parents=True, exist_ok=True)
    kept = directory / "kept.json"
    previous = os.umask(umask)
    try:
        atomic_write_text(directory / "fresh.json", "{}\n")
        kept.write_text("{}\n", encoding="utf-8")
        kept.chmod(0o640)
        atomic_write_text(kept, '{"a": 1}\n')
    finally:
        os.umask(previous)
    return {
        "fresh": stat.S_IMODE((directory / "fresh.json").stat().st_mode),
        "kept": stat.S_IMODE(kept.stat().st_mode),
    }


def test_both_atomic_writers_answer_the_same_on_permissions(tmp_path: Path) -> None:
    """반증: JS는 신규 파일에 0o644를 명시하고 chmod로 umask를 무력화했고,
    Python은 mkstemp의 0600을 target에 실어 보냈다. 같은 이름의 두 writer가
    같은 입력에서 다른 권한을 남기면 어느 쪽이 정책인지 말할 수 없다.
    """
    umask = 0o077
    js_dir = tmp_path / "js"
    js_dir.mkdir()
    script = PERMISSION_PROBE % {
        "module": json.dumps(str(SHARED_MODULE)),
        "umask": f"0o{umask:03o}",
        "dir": json.dumps(str(js_dir)),
    }
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)

    # 신규 파일은 umask가 정한다. 077이면 0600이지 0644가 아니다.
    assert observed["fresh"] == 0o600
    # 기존 파일의 권한은 물려받는다.
    assert observed["kept"] == 0o640
    # 명시 mode는 umask를 배제한다 - hook이 권한까지 검사하는 launcher가 이걸 쓴다.
    assert observed["explicit"] == 0o600

    python_modes = _python_modes(tmp_path / "py", umask)
    assert python_modes == {"fresh": observed["fresh"], "kept": observed["kept"]}


# parity 주장은 권한 축에서만 참이면 안 된다. 파일 fd만 fsync하고 rename을 내려보내지
# 않으면 내용은 살아도 새 이름이 디스크에 닿지 않아, 전원이 끊긴 뒤 대상 경로가
# 통째로 사라져 있을 수 있다. 무엇을 fsync했는지는 fd를 연 경로로 되짚어야 보인다.
DURABILITY_PROBE = """
import fs from "node:fs";
import { atomicWriteFileSync } from %(module)s;

const openedBy = new Map();
const realOpen = fs.openSync;
const realFsync = fs.fsyncSync;
const flushed = [];
fs.openSync = (target, ...rest) => {
  const descriptor = realOpen(target, ...rest);
  openedBy.set(descriptor, String(target));
  return descriptor;
};
fs.fsyncSync = (descriptor) => {
  flushed.push(openedBy.get(descriptor) ?? null);
  return realFsync(descriptor);
};
try {
  atomicWriteFileSync(%(target)s, '{"a": 1}\\n');
} finally {
  fs.openSync = realOpen;
  fs.fsyncSync = realFsync;
}
process.stdout.write(JSON.stringify(flushed));
"""


def test_js_atomic_write_flushes_the_rename_and_not_only_the_bytes(tmp_path: Path) -> None:
    """반증: JS writer는 파일 fd만 fsync했다. rename이 디스크에 닿지 않으면 전원이
    끊긴 뒤 대상 경로가 통째로 사라진다 — Python 쌍둥이는 `fsync_directory`로 그것을
    내려보내고, 파일 헤더는 두 구현이 같은 규칙을 따른다고 말한다.
    """
    target = tmp_path / "nested" / "kit.json"
    script = DURABILITY_PROBE % {
        "module": json.dumps(str(SHARED_MODULE)),
        "target": json.dumps(str(target)),
    }
    result = subprocess.run(
        (_node(), "--input-type=module", "-e", script),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    flushed = json.loads(result.stdout)

    assert str(target.parent) in flushed, f"부모 디렉터리를 내려보내지 않았다: {flushed}"
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
