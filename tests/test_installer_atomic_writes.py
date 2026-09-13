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


def _user_default_env() -> dict[str, str]:
    """사용자 install과 같은 구성. `AGENT_FLOW_INSTALL_FSYNC`를 지운다.

    스위트는 그 변수를 `0`으로 깔아 install의 fsync를 내린다(`tests/conftest.py`).
    아래 probe들은 그 값을 `1`로 덮는 대신 **지운다**. 덮으면 "변수를 준 실행"만
    보게 되어, `durableInstallWrites()`가 opt-in(`=== "1"`)으로 뒤집혀도 전 스위트가
    초록이고 정작 사용자 install만 조용히 fsync를 잃는다. 지우면 그 뒤집기가 여기서
    빨간색이 된다 — 이 probe가 보는 구성이 사용자의 구성이다.
    """
    env = dict(os.environ)
    env.pop("AGENT_FLOW_INSTALL_FSYNC", None)
    return env


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


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_root_context_update_is_atomic_and_follows_managed_symlink(
    tmp_path: Path, binary: str
) -> None:
    project = tmp_path / f"project-{binary}"
    project.mkdir()
    _observed_writes(project, binary)
    agents = project / "AGENTS.md"
    shared = tmp_path / f"shared-{binary}.md"
    shared.write_text(agents.read_text(encoding="utf-8"), encoding="utf-8")
    agents.unlink()
    agents.symlink_to(shared)
    skill = project / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    observed = _observed_writes(project, binary)

    assert agents.is_symlink()
    assert "writeFileSync" not in observed.get(str(agents), set())
    assert "renameSync" in observed.get(str(shared), set())
    assert "demo" in shared.read_text(encoding="utf-8")


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
        # 이 probe의 주입점이 `fsyncSync`다. 스위트 기본값(`0`)을 물려받으면 주입이
        # 아무 데도 닿지 않아, 중단을 재현하지 않은 실행이 통과로 보인다.
        env=_user_default_env(),
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
        # 이 probe가 세는 것이 fsync다. 스위트 기본값(`0`)을 물려받으면 아무것도 세지
        # 못한 실행을 "내려보냈다"로 읽을 수 없어야 한다.
        env=_user_default_env(),
    )
    assert result.returncode == 0, result.stderr
    flushed = json.loads(result.stdout)

    assert str(target.parent) in flushed, f"부모 디렉터리를 내려보내지 않았다: {flushed}"
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'


# host 설정을 공용 dotfile로 심링크해 두는 프로젝트가 있다. rename이 갈아 끼우는 것은
# 링크 대상이 아니라 링크 자신이라, writer가 링크를 따라가지 않으면 install은 canonical
# 파일을 그대로 두고 링크만 일반 파일로 바꾼다 - managed hook은 공용 설정에 반영되지
# 않고 사용자의 링크도 사라진다.
def test_install_updates_the_link_target_and_keeps_the_symlink(tmp_path: Path) -> None:
    """반증: `atomicWriteFileSync`가 링크를 따라가지 않으면 install 뒤
    `.claude/settings.json`은 일반 파일이 되고 공용 설정에는 hook이 없다.
    """
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    canonical = tmp_path / "shared" / "settings.json"
    canonical.parent.mkdir()
    canonical.write_text('{\n  "hooks": {}\n}\n', encoding="utf-8")
    link = project / ".claude" / "settings.json"
    link.symlink_to(canonical)

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr

    assert link.is_symlink(), "install이 사용자의 링크를 일반 파일로 갈아 끼웠다"
    assert os.readlink(link) == str(canonical), "링크가 다른 곳을 가리키게 됐다"
    settings = json.loads(canonical.read_text(encoding="utf-8"))
    assert settings.get("hooks"), "canonical 설정에 managed hook이 반영되지 않았다"
    assert "agent-flow" in canonical.read_text(encoding="utf-8")
    # 따라간 실체는 프로젝트 밖일 수 있다. 무엇을 어디에 썼는지 말하지 않으면
    # 사용자는 install이 남의 파일을 갈아 끼운 것을 알 방법이 없다.
    assert f"followed symlink: {link} -> {canonical}" in result.stdout, (
        f"링크를 따라간 사실과 대상을 알리지 않았다: {result.stdout}"
    )


# 끊어진 링크는 "새 파일"이 아니다. 사용자가 무엇을 가리켰는지 우리는 모르고, 조용히
# 일반 파일로 바꾸면 그 사실이 어디에도 남지 않는다. 권한 상속도 링크가 아니라 링크가
# 가리키는 실체를 봐야 한다 - 링크의 mode는 대개 0777이다. 따라가는 것은 host 소유
# 설정을 쓰는 호출부가 명시적으로 켰을 때뿐이고, 켜지 않은 kit 자산은 거부다.
SYMLINK_PROBE = """
import fs from "node:fs";
import { atomicWriteFileSync } from %(module)s;

const notices = [];
console.log = (line) => notices.push(String(line));

const dir = %(dir)s;
fs.writeFileSync(`${dir}/canonical.json`, "{}\\n");
fs.chmodSync(`${dir}/canonical.json`, 0o600);
fs.symlinkSync(`${dir}/canonical.json`, `${dir}/live.json`);
atomicWriteFileSync(`${dir}/live.json`, '{"a": 1}\\n', { followSymlink: true });

fs.symlinkSync(`${dir}/gone.json`, `${dir}/broken.json`);
let rejected = null;
try {
  atomicWriteFileSync(`${dir}/broken.json`, '{"a": 1}\\n', { followSymlink: true });
} catch (error) {
  rejected = String(error.message);
}

// kit 소유 자산에는 opt-in이 없다. 기본값은 거부다.
fs.writeFileSync(`${dir}/outside.json`, "outside\\n");
fs.symlinkSync(`${dir}/outside.json`, `${dir}/kit.json`);
let refusedDefault = null;
try {
  atomicWriteFileSync(`${dir}/kit.json`, '{"a": 1}\\n');
} catch (error) {
  refusedDefault = String(error.message);
}

process.stdout.write(JSON.stringify({
  inherited: fs.statSync(`${dir}/canonical.json`).mode & 0o777,
  rejected,
  refusedDefault,
  outside: fs.readFileSync(`${dir}/outside.json`, "utf8"),
  brokenIsSymlink: fs.lstatSync(`${dir}/broken.json`).isSymbolicLink(),
  kitIsSymlink: fs.lstatSync(`${dir}/kit.json`).isSymbolicLink(),
  notices,
  leftovers: fs.readdirSync(dir).filter((name) => name.includes(".tmp")),
}));
"""


def test_atomic_write_follows_only_the_links_a_caller_opted_into(tmp_path: Path) -> None:
    """반증: writer가 모든 호출부에 대해 링크를 따라가면 kit 자산 자리의 링크가
    프로젝트 밖 파일을 원자적으로 갈아 끼운다. 반대로 링크를 통째로 무시하면 실체
    권한 대신 링크의 0777이 실리고 끊어진 링크는 아무 경고 없이 일반 파일이 된다.
    """
    workspace = tmp_path / "links"
    workspace.mkdir()
    script = SYMLINK_PROBE % {
        "module": json.dumps(str(SHARED_MODULE)),
        "dir": json.dumps(str(workspace)),
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

    assert (workspace / "live.json").is_symlink(), "링크가 일반 파일로 바뀌었다"
    assert (workspace / "canonical.json").read_text(encoding="utf-8") == '{"a": 1}\n'
    assert outcome["inherited"] == 0o600, "링크 대상의 권한 대신 링크의 mode를 실었다"
    assert outcome["rejected"] is not None, "끊어진 링크를 조용히 일반 파일로 바꿨다"
    assert outcome["brokenIsSymlink"], "거부하면서 링크를 건드렸다"
    assert outcome["refusedDefault"] is not None, (
        "opt-in 없는 호출부가 링크를 따라갔다 - kit 자산 자리의 링크가 쓰기 경계다"
    )
    assert outcome["outside"] == "outside\n", "거부한다면서 링크 너머 파일을 갈아 끼웠다"
    assert outcome["kitIsSymlink"], "거부하면서 링크를 건드렸다"
    assert any("followed symlink" in line for line in outcome["notices"]), (
        f"링크를 따라간 사실을 알리지 않았다: {outcome['notices']}"
    )
    assert outcome["leftovers"] == [], "거부한 자리에 staging 파일이 남았다"


# 같은 writer가 host 설정과 kit 자산을 같이 쓴다. 정책을 호출부가 아니라 writer에
# 두면 `.agent-flow/**` 자리의 링크 하나가 install 쓰기를 프로젝트 밖으로 돌린다.
def test_install_refuses_to_write_kit_assets_through_a_symlink(tmp_path: Path) -> None:
    """반증: writer가 모든 호출부에 대해 링크를 따라가면 `.agent-flow/kit.json`
    링크가 프로젝트 밖 파일을 install이 원자적으로 교체하는 통로가 된다.
    """
    project = tmp_path / "project"
    (project / ".agent-flow").mkdir(parents=True)
    outside = tmp_path / "outside" / "victim.json"
    outside.parent.mkdir()
    outside.write_text("do not touch\n", encoding="utf-8")
    link = project / ".agent-flow" / "kit.json"
    link.symlink_to(outside)

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-kit.mjs"), "install"),
        cwd=project,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )

    assert outside.read_text(encoding="utf-8") == "do not touch\n", (
        "install이 링크를 타고 프로젝트 밖 파일을 갈아 끼웠다"
    )
    assert link.is_symlink(), "거부하면서 사용자의 링크를 건드렸다"
    assert result.returncode != 0, "경계를 넘지 않았다면서 성공으로 끝냈다"


def test_install_reports_a_broken_host_config_link_without_a_stack_trace(tmp_path: Path) -> None:
    """반증: 진입점에 핸들러가 없으면 끊어진 `.claude/settings.json` 링크가
    `Error: ... at ...` 스택으로 끝나고 문제가 된 경로를 찾기 어렵다.
    """
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    missing = tmp_path / "shared" / "settings.json"
    link = project / ".claude" / "settings.json"
    link.symlink_to(missing)

    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / "agent-flow-install.mjs"), "install"),
        cwd=project,
        env={**os.environ, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )

    assert result.returncode != 0, "해소되지 않는 링크를 두고 성공으로 끝냈다"
    assert str(link) in result.stderr, "어느 경로가 문제인지 말하지 않았다"
    assert "    at " not in result.stderr, f"stack trace로 끝냈다: {result.stderr}"
    assert link.is_symlink(), "거부하면서 사용자의 링크를 건드렸다"
    assert link.readlink() == missing
    assert not missing.exists()


def _architecture_node(project: Path, script: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (_node(), "--input-type=module", "-e",
         f"import * as installer from {json.dumps(SHARED_MODULE.as_uri())};\n" + script),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "PROJECT": str(project), **environment},
        text=True, capture_output=True, check=False, timeout=120,
    )


@pytest.mark.parametrize("shadow", ["yaml", "json"])
def test_architecture_plan_fallback_does_not_import_checkout_modules(tmp_path: Path, shadow: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = project / "imported"
    (project / f"{shadow}.py").write_text(
        f"open({str(marker)!r}, 'w').write('untrusted import')\nraise RuntimeError('checkout import')\n",
        encoding="utf-8",
    )
    probe = tmp_path / "force-user-site.mjs"
    probe.write_text("""
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
const spawn = cp.spawnSync;
cp.spawnSync = (command, args, options) => {
  if (args.includes('-I') && args.some(arg => arg.includes('yaml'))) {
    return {status: 1, stdout: '', stderr: 'PyYAML requires user site'};
  }
  return spawn(command, args, options);
};
syncBuiltinESMExports();
""", encoding="utf-8")
    result = _architecture_node(
        project,
        "const plan = installer.architecturePlan(process.env.PROJECT, 'pending');"
        "console.log(JSON.stringify({mode: plan.mode, flag: installer.resolveManagedPython().flag}));",
        NODE_OPTIONS=f"--import {probe.as_uri()}",
    )
    assert not marker.exists()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"mode": "pending", "flag": "-E"}


@pytest.mark.parametrize("corruption", [
    "noise", "array", "schema_version", "mode", "digest", "document_manifest",
    "excluded_skills", "selection_document", "source_document", "documents",
])
def test_architecture_plan_refuses_invalid_python_output(tmp_path: Path, corruption: str) -> None:
    probe = tmp_path / "corrupt-export.mjs"
    probe.write_text("""
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
const spawn = cp.spawnSync;
cp.spawnSync = (command, args, options) => {
  const result = spawn(command, args, options);
  if (!args.includes('architecture') || result.status !== 0) return result;
  const plan = JSON.parse(result.stdout);
  const field = process.env.CORRUPTION;
  if (field === 'noise') result.stdout = 'site startup output\\n' + result.stdout;
  else if (field === 'array') result.stdout = '[]';
  else {
    const replacements = {schema_version: 99, mode: 'unknown', digest: 'not-a-digest',
      document_manifest: [{}], excluded_skills: {}, selection_document: null,
      source_document: [], documents: ['../outside']};
    plan[field] = replacements[field];
    result.stdout = JSON.stringify(plan);
  }
  return result;
};
syncBuiltinESMExports();
""", encoding="utf-8")
    result = _architecture_node(
        tmp_path,
        "try { const tx = installer.prepareArchitectureInstall(process.env.PROJECT,"
        " ['--architecture-mode', 'pending']); tx.commit(); }"
        "catch (error) { console.error(error.message); process.exitCode = 2; }",
        NODE_OPTIONS=f"--import {probe.as_uri()}", CORRUPTION=corruption,
    )
    assert result.returncode == 2, result.stderr
    assert "refusing to install" in result.stderr
    assert not (tmp_path / ".agent-flow.project.yaml").exists()
    assert not (tmp_path / ".agent-flow/install-recovery/manifest.json").exists()


@pytest.mark.parametrize("proven_owner", [False, True], ids=["legacy", "ownership-preserved"])
def test_recovery_requires_owner_proof_for_previous_release_asset_list(
    tmp_path: Path, proven_owner: bool,
) -> None:
    recovery = tmp_path / ".agent-flow/install-recovery"
    relative = ".agent-flow/templates/retired-template.txt"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("interrupted upgrade", encoding="utf-8")
    backup = recovery / relative
    backup.parent.mkdir(parents=True)
    backup.write_text("previous release", encoding="utf-8")
    added = tmp_path / ".agent-flow/scripts/obsolete-script.sh"
    added.parent.mkdir(parents=True)
    added.write_text("incomplete addition", encoding="utf-8")
    entries = [
        {"relative": relative, "existed": True},
        {"relative": added.relative_to(tmp_path).as_posix(), "existed": False},
    ]
    if proven_owner:
        entries[0]["assetOwnershipPreserved"] = True
    (recovery / "manifest.json").write_text(
        json.dumps({"version": 1, "entries": entries}), encoding="utf-8",
    )
    result = _architecture_node(
        tmp_path,
        "const tx = installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']);"
        "tx.rollback();",
    )
    if proven_owner:
        assert result.returncode == 0, result.stderr
        assert target.read_text(encoding="utf-8") == "previous release"
        assert not added.exists()
        assert not recovery.exists()
    else:
        assert result.returncode != 0, result.stdout
        assert target.read_text(encoding="utf-8") == "interrupted upgrade"
        assert added.read_text(encoding="utf-8") == "incomplete addition"
        assert backup.read_text(encoding="utf-8") == "previous release"
        assert not (tmp_path / ".agent-flow/backups").exists()


@pytest.mark.parametrize("corruption", [
    "traversal", "project-file", "run-data", "duplicate", "overlap", "version", "missing-backup",
    "missing-version", "unversioned-array",
])
def test_recovery_rejects_unsafe_manifest_before_any_mutation(tmp_path: Path, corruption: str) -> None:
    recovery = tmp_path / ".agent-flow/install-recovery"
    recovery.mkdir(parents=True)
    protected = tmp_path / ".agent-flow/kit.json"
    protected.write_text("keep current bytes", encoding="utf-8")
    entries = [{"relative": ".agent-flow/kit.json", "existed": False}]
    unsafe = {
        "traversal": "../victim", "project-file": "src/important.py",
        "run-data": ".agent-flow/runs/current", "duplicate": ".agent-flow/kit.json",
        "overlap": ".agent-flow/templates/nested",
        "missing-backup": ".agent-flow/templates/absent",
    }
    if corruption == "overlap":
        entries.append({"relative": ".agent-flow/templates", "existed": False})
    if corruption in unsafe:
        entries.append({"relative": unsafe[corruption], "existed": corruption == "missing-backup"})
    manifest = recovery / "manifest.json"
    payload: object = {"version": 99 if corruption == "version" else 1, "entries": entries}
    if corruption == "missing-version":
        payload = {"entries": entries}
    elif corruption == "unversioned-array":
        payload = entries
    original = json.dumps(payload)
    manifest.write_text(original, encoding="utf-8")
    result = _architecture_node(
        tmp_path,
        "try { installer.prepareArchitectureInstall(process.env.PROJECT, []); }"
        "catch (error) { console.error(error.message); process.exitCode = 2; }",
    )
    assert result.returncode == 2
    assert "recovery" in result.stderr
    assert protected.read_text(encoding="utf-8") == "keep current bytes"
    assert manifest.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_interrupted_real_installer_blocks_python_until_resumed(tmp_path: Path, binary: str) -> None:
    from agent_flow.core.installation import assert_install_complete

    project = tmp_path / "project"
    project.mkdir()
    probe = tmp_path / "interrupt-install.mjs"
    probe.write_text("""
import fs from 'node:fs';
const rename = fs.renameSync;
fs.renameSync = (source, target, ...args) => {
  const result = rename(source, target, ...args);
  if (String(target).endsWith('/install-recovery/manifest.json')) {
    process.kill(process.pid, 'SIGKILL');
  }
  return result;
};
""", encoding="utf-8")
    command = (_node(), str(KIT_ROOT / "bin" / binary), "install",
               "--profile", "python", "--architecture-mode", "pending")
    environment = {**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"}
    interrupted = subprocess.run(
        command, cwd=project, env={**environment, "NODE_OPTIONS": f"--import {probe.as_uri()}"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert interrupted.returncode != 0
    with pytest.raises(ValueError, match="install recovery"):
        assert_install_complete(project)
    resumed = subprocess.run(
        command, cwd=project, env=environment, text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert_install_complete(project)
    assert "mode: pending" in (project / ".agent-flow.project.yaml").read_text(encoding="utf-8")


def _interrupt_architecture_transaction(project: Path) -> None:
    result = _architecture_node(
        project,
        "installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']);"
        "process.kill(process.pid, 'SIGKILL');",
    )
    assert result.returncode < 0, result.stderr
    assert (project / ".agent-flow/install-recovery/manifest.json").is_file()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("existed", [False, True], ids=["new-asset", "existing-asset"])
def test_recovery_preserves_post_crash_user_changes(
    tmp_path: Path, binary: str, existed: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    relative = Path(".claude/skills/user-notes/notes.txt")
    target = project / relative
    if existed:
        target.parent.mkdir(parents=True)
        target.write_text("before install", encoding="utf-8")
    _interrupt_architecture_transaction(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("irreplaceable post-crash notes", encoding="utf-8")
    target.chmod(0o600)
    sibling = target.with_name("new-note.txt")
    sibling.write_text("new post-crash note", encoding="utf-8")
    link = target.with_name("linked-note")
    link.symlink_to("notes.txt")
    resumed = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode == 0, resumed.stderr
    snapshots = list((project / ".agent-flow/backups").glob("install-recovery-*"))
    retained = [snapshot for snapshot in snapshots if (snapshot / relative).is_file()]
    assert len(retained) == 1, resumed.stdout
    snapshot = retained[0]
    assert str(snapshot) in resumed.stdout + resumed.stderr
    assert (snapshot / relative).read_text(encoding="utf-8") == "irreplaceable post-crash notes"
    assert stat.S_IMODE((snapshot / relative).stat().st_mode) == 0o600
    assert (snapshot / relative.with_name("new-note.txt")).read_text(encoding="utf-8") == "new post-crash note"
    assert (snapshot / relative.with_name("linked-note")).readlink() == Path("notes.txt")
    assert not (project / ".agent-flow/install-recovery").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_interrupted_recovery_keeps_private_parent_protection(
    tmp_path: Path, binary: str,
) -> None:
    project = tmp_path / "project"
    target = project / ".claude/settings.json"
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_text('{"private": "original"}\n', encoding="utf-8")
    target.chmod(0o644)
    probe = tmp_path / "interrupt-private-copy.mjs"
    probe.write_text("""
import fs from 'node:fs';
process.umask(0o022);
const open = fs.openSync;
const write = fs.writeFileSync;
let backupFd;
fs.openSync = (target, ...args) => {
  const fd = open(target, ...args);
  if (String(target).endsWith('/install-recovery/.claude/settings.json')) backupFd = fd;
  return fd;
};
fs.writeFileSync = (target, ...args) => {
  const result = write(target, ...args);
  if (target === backupFd) process.kill(process.pid, 'SIGKILL');
  return result;
};
""", encoding="utf-8")
    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
             "NODE_OPTIONS": f"--import {probe.as_uri()}"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    recovery = project / ".agent-flow/install-recovery"
    assert result.returncode != 0
    assert (recovery / ".claude/settings.json").read_bytes() == target.read_bytes()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o700


def _interrupt_private_host_install(project: Path, binary: str, probe: Path) -> None:
    probe.write_text("""
import fs from 'node:fs';
process.umask(0o022);
const rename = fs.renameSync;
fs.renameSync = (source, target, ...args) => {
  const result = rename(source, target, ...args);
  if (String(target).endsWith('/install-recovery/manifest.json')) {
    process.kill(process.pid, 'SIGKILL');
  }
  return result;
};
""", encoding="utf-8")
    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
             "NODE_OPTIONS": f"--import {probe.as_uri()}"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert result.returncode != 0
    assert (project / ".agent-flow/install-recovery/manifest.json").is_file()


def _assign_supplementary_file_group(target: Path) -> tuple[int, int]:
    for group in os.getgroups():
        if group == target.parent.stat().st_gid:
            continue
        try:
            os.chown(target, -1, group)
        except PermissionError:
            continue
        return target.stat().st_uid, group
    pytest.skip("Changing a file to an alternate supplementary group is required")


HOST_OWNERSHIP_PROBE = """
import fs from 'node:fs';
process.umask(0o022);
const open = fs.openSync;
const write = fs.writeFileSync;
const rename = fs.renameSync;
const chown = fs.fchownSync;
const stages = new Set();
const record = (phase, fd) => {
  const stat = fs.fstatSync(fd);
  fs.appendFileSync(process.env.OWNER_LOG, JSON.stringify({
    phase, uid: stat.uid, gid: stat.gid, mode: stat.mode & 0o777
  }) + '\\n');
};
fs.openSync = (target, ...args) => {
  const fd = open(target, ...args);
  if (String(target).startsWith(process.env.PRIVATE_TARGET + '.') && String(target).endsWith('.tmp')) {
    stages.add(fd);
    record('open', fd);
  }
  return fd;
};
fs.writeFileSync = (fd, ...args) => {
  const result = write(fd, ...args);
  if (stages.has(fd)) record('write', fd);
  return result;
};
fs.fchownSync = (fd, uid, gid) => {
  if (stages.has(fd) && process.env.OWNER_FAIL === '1') {
    record('ownership-refused', fd);
    throw new Error('injected ownership restoration failure');
  }
  return chown(fd, uid, gid);
};
const close = fs.closeSync;
fs.closeSync = (fd) => {
  stages.delete(fd);
  return close(fd);
};
fs.renameSync = (source, target, ...args) => {
  const result = rename(source, target, ...args);
  if (String(target) === process.env.PRIVATE_TARGET && process.env.OWNER_STOP === '1') {
    process.kill(process.pid, 'SIGKILL');
  }
  return result;
};
"""


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_real_recovery_preserves_file_owner_through_normal_reinstall(
    tmp_path: Path, binary: str,
) -> None:
    project = tmp_path / "project"
    target = project / ".claude/settings.json"
    target.parent.mkdir(parents=True, mode=0o755)
    original = b'{"private": "original"}\n'
    target.write_bytes(original)
    owner = _assign_supplementary_file_group(target)
    target.chmod(0o640)
    probe = tmp_path / "ownership-probe.mjs"
    _interrupt_private_host_install(project, binary, probe)
    target.unlink()
    probe.write_text(HOST_OWNERSHIP_PROBE, encoding="utf-8")
    log = tmp_path / "ownership.jsonl"
    env = {
        **os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
        "NODE_OPTIONS": f"--import {probe.as_uri()}", "PRIVATE_TARGET": str(target),
        "OWNER_LOG": str(log),
    }
    command = (_node(), str(KIT_ROOT / "bin" / binary), "install",
               "--profile", "python", "--architecture-mode", "pending")
    resumed = subprocess.run(
        command, cwd=project, env={**env, "OWNER_STOP": "1"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode != 0
    assert target.read_bytes() == original
    assert (target.stat().st_uid, target.stat().st_gid) == owner
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    recovery = project / ".agent-flow/install-recovery"
    assert (recovery / ".claude/settings.json").read_bytes() == original
    for _ in range(2):
        installed = subprocess.run(
            command, cwd=project, env=env,
            text=True, capture_output=True, check=False, timeout=600,
        )
        assert installed.returncode == 0, installed.stderr
        assert json.loads(target.read_text(encoding="utf-8"))["private"] == "original"
        assert (target.stat().st_uid, target.stat().st_gid) == owner
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
    observations = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert {entry["phase"] for entry in observations} == {"open", "write"}
    assert all(entry["mode"] & 0o077 == 0 for entry in observations)
    assert not recovery.exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("missing", [False, True], ids=["existing-file", "missing-file"])
def test_real_recovery_owner_failure_never_publishes_private_content(
    tmp_path: Path, binary: str, missing: bool,
) -> None:
    project = tmp_path / "project"
    target = project / ".claude/settings.json"
    target.parent.mkdir(parents=True, mode=0o755)
    original = b'{"private": "original"}\n'
    target.write_bytes(original)
    owner = _assign_supplementary_file_group(target)
    target.chmod(0o640)
    probe = tmp_path / "ownership-failure.mjs"
    _interrupt_private_host_install(project, binary, probe)
    if missing:
        target.unlink()
    else:
        target.write_bytes(b'{"private": "post-crash"}\n')
    recovery = project / ".agent-flow/install-recovery"
    manifest = (recovery / "manifest.json").read_bytes()
    probe.write_text(HOST_OWNERSHIP_PROBE, encoding="utf-8")
    log = tmp_path / "ownership.jsonl"
    result = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
             "NODE_OPTIONS": f"--import {probe.as_uri()}", "PRIVATE_TARGET": str(target),
             "OWNER_LOG": str(log), "OWNER_FAIL": "1"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert result.returncode != 0, result.stdout
    assert "injected ownership restoration failure" in result.stderr
    assert (recovery / "manifest.json").read_bytes() == manifest
    assert (recovery / ".claude/settings.json").read_bytes() == original
    if missing:
        assert not target.exists()
    else:
        assert target.read_bytes() == b'{"private": "post-crash"}\n'
        assert (target.stat().st_uid, target.stat().st_gid) == owner
    observations = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert observations[-1]["phase"] == "ownership-refused"
    assert all(entry["mode"] & 0o077 == 0 for entry in observations)
    assert not list(target.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "owner", [None, {}, {"uid": -1, "gid": 0}, {"uid": 0, "gid": 0xffffffff}],
    ids=["legacy", "incomplete", "negative-uid", "sentinel-gid"],
)
def test_recovery_refuses_unproven_file_owner_before_mutation(
    tmp_path: Path, owner: dict[str, int] | None,
) -> None:
    target = tmp_path / ".claude/settings.json"
    target.parent.mkdir()
    original = b'{"private": "original"}\n'
    target.write_bytes(original)
    _interrupt_architecture_transaction(tmp_path)
    recovery = tmp_path / ".agent-flow/install-recovery"
    manifest = recovery / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    entry = next(entry for entry in payload["entries"] if entry["relative"] == ".claude/settings.json")
    if owner is None:
        entry.pop("hostOwner", None)
    else:
        entry["hostOwner"] = owner
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    journal = manifest.read_bytes()
    target.unlink()
    result = _architecture_node(
        tmp_path,
        "installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']);",
    )
    assert result.returncode != 0, result.stdout
    assert not target.exists()
    assert manifest.read_bytes() == journal
    assert (recovery / ".claude/settings.json").read_bytes() == original
    assert not (tmp_path / ".agent-flow/backups").exists()


def test_atomic_writer_rejects_invalid_owner_before_creating_parent(tmp_path: Path) -> None:
    target = tmp_path / "absent/settings.json"
    result = _architecture_node(tmp_path, """
installer.atomicWriteFileSync(process.env.TARGET, 'private', {ownership: {uid: -1, gid: 0}});
""", TARGET=str(target))
    assert result.returncode != 0, result.stdout
    assert not target.parent.exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_real_recovery_preserves_private_asset_copy_ownership(
    tmp_path: Path, binary: str,
) -> None:
    project = tmp_path / "project"
    relative = ".agent-flow/templates/private.txt"
    target = project / relative
    target.parent.mkdir(parents=True)
    original = b"irreplaceable private template\n"
    target.write_bytes(original)
    owner = _assign_supplementary_file_group(target)
    target.chmod(0o640)
    probe = tmp_path / "copy-ownership.mjs"
    _interrupt_private_host_install(project, binary, probe)
    recovery = project / ".agent-flow/install-recovery"
    backup = recovery / relative
    assert backup.read_bytes() == original
    target.unlink()
    probe.write_text("""
import fs from 'node:fs';
process.umask(0o022);
const open = fs.openSync;
const write = fs.writeFileSync;
const chmod = fs.fchmodSync;
const close = fs.closeSync;
const copies = new Set();
const record = (phase, fd) => {
  const stat = fs.fstatSync(fd);
  fs.appendFileSync(process.env.OWNER_LOG, JSON.stringify({
    phase, uid: stat.uid, gid: stat.gid, mode: stat.mode & 0o777
  }) + '\\n');
};
fs.openSync = (target, ...args) => {
  const fd = open(target, ...args);
  if (String(target) === process.env.PRIVATE_TARGET && args[0] === 'wx') {
    copies.add(fd);
    record('open', fd);
  }
  return fd;
};
fs.writeFileSync = (fd, ...args) => {
  const result = write(fd, ...args);
  if (copies.has(fd)) record('write', fd);
  return result;
};
fs.fchmodSync = (fd, ...args) => {
  const result = chmod(fd, ...args);
  if (copies.has(fd)) {
    record('published-mode', fd);
    process.kill(process.pid, 'SIGKILL');
  }
  return result;
};
fs.closeSync = (fd) => {
  copies.delete(fd);
  return close(fd);
};
""", encoding="utf-8")
    log = tmp_path / "copy-ownership.jsonl"
    command = (_node(), str(KIT_ROOT / "bin" / binary), "install",
               "--profile", "python", "--architecture-mode", "pending")
    env = {**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"}
    result = subprocess.run(
        command, cwd=project,
        env={**env, "NODE_OPTIONS": f"--import {probe.as_uri()}",
             "PRIVATE_TARGET": str(target), "OWNER_LOG": str(log)},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert result.returncode != 0
    assert target.read_bytes() == original
    assert (target.stat().st_uid, target.stat().st_gid) == owner
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert (backup.stat().st_uid, backup.stat().st_gid) == owner
    assert backup.read_bytes() == original
    observations = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [entry["phase"] for entry in observations] == ["open", "write", "published-mode"]
    assert all(entry["mode"] & 0o077 == 0 for entry in observations[:-1])
    assert (observations[-1]["uid"], observations[-1]["gid"]) == owner
    resumed = subprocess.run(
        command, cwd=project, env=env,
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert target.read_bytes() == original
    assert (target.stat().st_uid, target.stat().st_gid) == owner
    assert not recovery.exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_real_recovery_preserves_asset_directory_special_permissions(
    tmp_path: Path, binary: str,
) -> None:
    project = tmp_path / "project"
    directory = project / ".agent-flow/templates"
    directory.mkdir(parents=True)
    owner = _assign_supplementary_file_group(directory)
    directory.chmod(0o3770)
    target = directory / "private.txt"
    original = b"irreplaceable template in a protected directory\n"
    target.write_bytes(original)
    probe = tmp_path / "directory-recovery.mjs"
    _interrupt_private_host_install(project, binary, probe)
    target.unlink()
    directory.rmdir()

    resumed = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert stat.S_IMODE(directory.stat().st_mode) == 0o3770
    assert (directory.stat().st_uid, directory.stat().st_gid) == owner
    assert target.read_bytes() == original
    subsequent = directory / "subsequent.txt"
    subsequent.write_bytes(b"created after recovery\n")
    assert subsequent.stat().st_gid == owner[1]
    assert not (project / ".agent-flow/install-recovery").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("state", [
    "matching", "no-selection-write", "mismatching", "missing-ready",
    "invalid-marker", "unsafe-path",
])
def test_real_completed_legacy_journal_cleanup(
    tmp_path: Path, binary: str, state: str,
) -> None:
    project = tmp_path / "project"
    target = project / ".agent-flow/templates/private.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original private asset\n")
    declaration = project / ".agent-flow.project.yaml"
    command = [_node(), str(KIT_ROOT / "bin" / binary), "install", "--profile", "python"]
    if state == "no-selection-write":
        declaration.write_text("schema_version: 1\narchitecture:\n  mode: pending\n", encoding="utf-8")
    else:
        command.extend(["--architecture-mode", "pending"])
    probe = tmp_path / "completed-install.mjs"
    probe.write_text("""
import fs from 'node:fs';
import path from 'node:path';
const remove = fs.rmSync;
fs.rmSync = (target, ...args) => {
  if (path.resolve(String(target)) === process.env.RECOVERY
      && fs.existsSync(path.join(target, 'ready.json'))) {
    process.kill(process.pid, 'SIGKILL');
  }
  return remove(target, ...args);
};
""", encoding="utf-8")
    recovery = project / ".agent-flow/install-recovery"
    environment = {**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"}
    interrupted = subprocess.run(
        command, cwd=project,
        env={**environment, "NODE_OPTIONS": f"--import {probe.as_uri()}", "RECOVERY": str(recovery)},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert interrupted.returncode != 0, interrupted.stderr
    ready_path = recovery / "ready.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    if state == "no-selection-write":
        assert ready["selectionDocument"] is None
    else:
        assert ready["selectionDocument"] == declaration.read_text(encoding="utf-8")
    manifest = recovery / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in payload["entries"]:
        entry.pop("assetOwnershipPreserved", None)
    if state == "invalid-marker":
        next(entry for entry in payload["entries"]
             if entry["relative"] == ".agent-flow/templates")["assetOwnershipPreserved"] = False
    elif state == "unsafe-path":
        payload["entries"].append({"relative": "../outside", "existed": False})
    elif state == "mismatching":
        declaration.write_text("schema_version: 1\narchitecture:\n  mode: clean\n", encoding="utf-8")
    elif state == "missing-ready":
        ready_path.unlink()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    journal = manifest.read_bytes()
    current = b"irreplaceable edit after completed install\n"
    target.write_bytes(current)
    resumed = subprocess.run(
        command, cwd=project, env=environment,
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert target.read_bytes() == current
    if state in {"matching", "no-selection-write"}:
        assert resumed.returncode == 0, resumed.stderr
        assert not recovery.exists()
    else:
        assert resumed.returncode != 0, resumed.stdout
        assert manifest.read_bytes() == journal
        assert (recovery / ".agent-flow/templates/private.txt").read_bytes() == b"original private asset\n"
        assert not (project / ".agent-flow/backups").exists()


def test_recovery_refuses_legacy_asset_backup_without_ownership_proof(tmp_path: Path) -> None:
    relative = ".agent-flow/templates/private.txt"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    original = b"private original template\n"
    target.write_bytes(original)
    owner = _assign_supplementary_file_group(target)
    target.chmod(0o640)
    _interrupt_architecture_transaction(tmp_path)
    recovery = tmp_path / ".agent-flow/install-recovery"
    manifest = recovery / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in payload["entries"]:
        entry.pop("assetOwnershipPreserved", None)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    backup = recovery / relative
    os.chown(backup, -1, target.parent.stat().st_gid)
    target.write_bytes(b"irreplaceable post-crash edit\n")
    journal = manifest.read_bytes()
    result = _architecture_node(
        tmp_path,
        "installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']);",
    )
    assert result.returncode != 0, result.stdout
    assert target.read_bytes() == b"irreplaceable post-crash edit\n"
    assert (target.stat().st_uid, target.stat().st_gid) == owner
    assert manifest.read_bytes() == journal
    assert backup.read_bytes() == original
    assert not (tmp_path / ".agent-flow/backups").exists()


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize(
    ("parent_mode", "group_owned"),
    [(0o700, False), (0o3700, False), (0o750, True)],
    ids=["private", "sticky-setgid", "group-private"],
)
def test_real_recovery_recreates_private_host_parent_before_publishing(
    tmp_path: Path, binary: str, parent_mode: int, group_owned: bool,
) -> None:
    project = tmp_path / "project"
    target = project / ".claude/settings.json"
    target.parent.mkdir(parents=True, mode=0o700)
    if group_owned:
        group = next((gid for gid in os.getgroups() if gid != target.parent.stat().st_gid), None)
        if group is None:
            pytest.skip("An alternate supplementary group is required")
        os.chown(target.parent, -1, group)
    target.parent.chmod(parent_mode)
    original_group = target.parent.stat().st_gid
    original = b'{"private": "original"}\n'
    target.write_bytes(original)
    target.chmod(0o644)
    probe = tmp_path / "interrupt-private-restore.mjs"
    _interrupt_private_host_install(project, binary, probe)
    shutil.rmtree(target.parent)
    post_crash = project / ".agent-flow/templates/post-crash.txt"
    post_crash.parent.mkdir(parents=True, exist_ok=True)
    post_crash.write_text("irreplaceable post-crash notes", encoding="utf-8")
    probe.write_text("""
import fs from 'node:fs';
process.umask(0o022);
const rename = fs.renameSync;
fs.renameSync = (source, target, ...args) => {
  const result = rename(source, target, ...args);
  if (String(target) === process.env.PRIVATE_TARGET) process.kill(process.pid, 'SIGKILL');
  return result;
};
""", encoding="utf-8")
    resumed = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1",
             "NODE_OPTIONS": f"--import {probe.as_uri()}", "PRIVATE_TARGET": str(target.resolve())},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode != 0
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(target.parent.stat().st_mode) == parent_mode
    assert target.parent.stat().st_gid == original_group
    recovery = project / ".agent-flow/install-recovery"
    assert (recovery / ".claude/settings.json").read_bytes() == original
    snapshots = (project / ".agent-flow/backups").glob("install-recovery-*")
    assert any(
        (snapshot / ".agent-flow/templates/post-crash.txt").read_text(encoding="utf-8")
        == "irreplaceable post-crash notes"
        for snapshot in snapshots
    )


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
@pytest.mark.parametrize("boundary", ["legacy-missing-parent", "widened-canonical-ancestor"])
def test_real_recovery_refuses_unproven_host_directory_protection(
    tmp_path: Path, binary: str, boundary: str,
) -> None:
    project = tmp_path / "project"
    target = project / ".claude/settings.json"
    target.parent.mkdir(parents=True, mode=0o700)
    private = tmp_path / "private"
    if boundary == "widened-canonical-ancestor":
        private.mkdir(mode=0o700)
        canonical = private / "nested/settings.json"
        canonical.parent.mkdir(mode=0o755)
        target.symlink_to(canonical)
    else:
        canonical = target
    original = b'{"private": "original"}\n'
    canonical.write_bytes(original)
    canonical.chmod(0o644)
    _interrupt_private_host_install(project, binary, tmp_path / "interrupt-boundary.mjs")
    recovery = project / ".agent-flow/install-recovery"
    manifest = recovery / "manifest.json"
    if boundary == "legacy-missing-parent":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for entry in payload["entries"]:
            entry.pop("hostParents", None)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        shutil.rmtree(target.parent)
    else:
        canonical.write_bytes(b'{"private": "post-crash"}\n')
        private.chmod(0o755)
    journal = manifest.read_bytes()
    post_crash = project / ".agent-flow/templates/post-crash.txt"
    post_crash.parent.mkdir(parents=True, exist_ok=True)
    post_crash.write_text("irreplaceable post-crash notes", encoding="utf-8")
    resumed = subprocess.run(
        (_node(), str(KIT_ROOT / "bin" / binary), "install",
         "--profile", "python", "--architecture-mode", "pending"),
        cwd=project,
        env={**os.environ, "PYTHON": sys.executable, "AGENT_FLOW_SKIP_CODEX_TRUST": "1"},
        text=True, capture_output=True, check=False, timeout=600,
    )
    assert resumed.returncode != 0, resumed.stdout
    assert "protection" in resumed.stderr
    assert manifest.read_bytes() == journal
    assert (recovery / ".claude/settings.json").read_bytes() == original
    assert post_crash.read_text(encoding="utf-8") == "irreplaceable post-crash notes"
    if boundary == "legacy-missing-parent":
        assert not target.parent.exists()
    else:
        assert target.is_symlink()
        assert canonical.read_bytes() == b'{"private": "post-crash"}\n'

@pytest.mark.parametrize("current_state", ["missing", "widened", "stable-symlink"])
def test_recovery_restores_private_host_config_permissions(
    tmp_path: Path, current_state: str,
) -> None:
    target = tmp_path / ".claude/settings.json"
    target.parent.mkdir()
    canonical = tmp_path / "private-settings.json" if current_state == "stable-symlink" else target
    canonical.write_text('{"private": "original"}\n', encoding="utf-8")
    canonical.chmod(0o600)
    if current_state == "stable-symlink":
        target.symlink_to(canonical)
    _interrupt_architecture_transaction(tmp_path)
    if current_state == "missing":
        target.unlink()
    else:
        canonical.write_text('{"private": "post-crash"}\n', encoding="utf-8")
        canonical.chmod(0o644)
    result = _architecture_node(
        tmp_path,
        "const tx = installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']);"
        "tx.commit();",
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == '{"private": "original"}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if current_state == "stable-symlink":
        assert target.is_symlink()
    if current_state != "missing":
        snapshots = (tmp_path / ".agent-flow/backups").glob("install-recovery-*")
        assert any(
            (snapshot / ".claude/settings.json").read_text(encoding="utf-8")
            == '{"private": "post-crash"}\n'
            for snapshot in snapshots
        )


@pytest.mark.parametrize("failure", ["read", "flush"])
def test_recovery_preservation_failure_leaves_current_and_backup_untouched(
    tmp_path: Path, failure: str,
) -> None:
    target = tmp_path / ".agent-flow/templates/user.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before install", encoding="utf-8")
    _interrupt_architecture_transaction(tmp_path)
    target.write_text("post-crash edit", encoding="utf-8")
    recovery = tmp_path / ".agent-flow/install-recovery"
    manifest = (recovery / "manifest.json").read_bytes()
    result = _architecture_node(tmp_path, """
import fs from 'node:fs';
const read = fs.readFileSync;
fs.readFileSync = (target, ...args) => {
  if (process.env.FAILURE === 'read'
      && target === process.env.PROJECT + '/.agent-flow/templates/user.txt') {
    throw new Error('injected preservation read failure');
  }
  return read(target, ...args);
};
if (process.env.FAILURE === 'flush') {
  fs.fsyncSync = () => { throw new Error('injected preservation flush failure'); };
}
try { installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']); }
catch (error) { console.error(error.message); process.exitCode = 2; }
""", FAILURE=failure)
    assert result.returncode == 2, result.stderr
    assert "preserv" in result.stderr
    assert str(recovery) in result.stderr
    assert target.read_text(encoding="utf-8") == "post-crash edit"
    assert (recovery / ".agent-flow/templates/user.txt").read_text(encoding="utf-8") == "before install"
    assert (recovery / "manifest.json").read_bytes() == manifest


@pytest.mark.parametrize("link_change", ["retargeted", "broken", "new-link", "parent-retargeted"])
def test_recovery_refuses_changed_host_symlink_before_rollback(
    tmp_path: Path, link_change: str,
) -> None:
    host = tmp_path / ".claude"
    original = tmp_path / "original"
    original.mkdir()
    original_config = original / "settings.json"
    original_config.write_text('{"original": true}\n', encoding="utf-8")
    if link_change == "parent-retargeted":
        host.symlink_to(original, target_is_directory=True)
    else:
        host.mkdir()
    target = host / "settings.json"
    if link_change == "new-link":
        target.write_text('{"original": true}\n', encoding="utf-8")
    elif link_change != "parent-retargeted":
        target.symlink_to(original_config)
    protected = tmp_path / ".agent-flow/templates/user.txt"
    protected.parent.mkdir(parents=True)
    protected.write_text("before install", encoding="utf-8")
    _interrupt_architecture_transaction(tmp_path)
    protected.write_text("post-crash edit", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    destination = unrelated / "settings.json"
    if link_change != "broken":
        destination.write_text('{"unrelated": true}\n', encoding="utf-8")
    if link_change == "parent-retargeted":
        host.unlink()
        host.symlink_to(unrelated, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(destination)
    recovery = tmp_path / ".agent-flow/install-recovery"
    manifest = (recovery / "manifest.json").read_bytes()
    result = _architecture_node(
        tmp_path,
        "try { installer.prepareArchitectureInstall(process.env.PROJECT, []); }"
        "catch (error) { console.error(error.message); process.exitCode = 2; }",
    )
    assert result.returncode == 2, result.stderr
    assert "symlink" in result.stderr
    assert str(recovery) in result.stderr
    assert protected.read_text(encoding="utf-8") == "post-crash edit"
    assert (recovery / "manifest.json").read_bytes() == manifest
    assert original_config.read_text(encoding="utf-8") == '{"original": true}\n'
    if link_change == "broken":
        assert not destination.exists()
    else:
        assert destination.read_text(encoding="utf-8") == '{"unrelated": true}\n'


def test_inherited_architecture_transaction_can_delegate_without_owning_commit(tmp_path: Path) -> None:
    child = tmp_path / "child.mjs"
    child.write_text(
        f"import * as installer from {json.dumps(SHARED_MODULE.as_uri())};\n" + """
import fs from 'node:fs';
import cp from 'node:child_process';
import assert from 'node:assert/strict';
const consumed = Number(process.env.AGENT_FLOW_INSTALL_PLAN_FD);
const tx = installer.prepareArchitectureInstall(process.env.PROJECT, []);
assert.throws(() => fs.fstatSync(consumed), {code: 'EBADF'});
assert.equal(tx.plan.mode, 'pending');
tx.commit();
tx.rollback();
assert.equal(fs.existsSync(process.env.PROJECT + '/.agent-flow.project.yaml'), false);
assert.equal(fs.existsSync(process.env.PROJECT + '/.agent-flow/install-recovery/manifest.json'), true);
if (!process.env.GRANDCHILD) {
  const delegated = tx.delegatedOptions();
  let result;
  try {
    result = cp.spawnSync(process.execPath, [process.argv[1]], {
      ...delegated.options, encoding: 'utf8',
      env: {...delegated.options.env, GRANDCHILD: '1'},
    });
  } finally { delegated.close(); }
  assert.equal(result.status, 0, result.stderr);
}
""", encoding="utf-8")
    result = _architecture_node(tmp_path, """
import fs from 'node:fs';
import cp from 'node:child_process';
import assert from 'node:assert/strict';
const lease = fs.openSync(process.env.PROJECT + '/lease', 'w+');
process.env.AGENT_FLOW_INSTALL_LEASE_FD = String(lease);
const tx = installer.prepareArchitectureInstall(process.env.PROJECT, ['--architecture-mode', 'pending']);
const delegated = tx.delegatedOptions();
let result;
try {
  result = cp.spawnSync(process.execPath, [process.env.PROJECT + '/child.mjs'], {
    ...delegated.options, encoding: 'utf8',
  });
} finally { delegated.close(); }
assert.equal(result.status, 0, result.stderr);
tx.commit();
fs.closeSync(lease);
""")
    assert result.returncode == 0, result.stderr
    assert "mode: pending" in (tmp_path / ".agent-flow.project.yaml").read_text(encoding="utf-8")
    assert not (tmp_path / ".agent-flow/install-recovery").exists()


def test_invalid_inherited_plan_closes_descriptor_and_reports_refusal(tmp_path: Path) -> None:
    result = _architecture_node(tmp_path, """
import fs from 'node:fs';
import assert from 'node:assert/strict';
const file = process.env.PROJECT + '/invalid-plan.json';
fs.writeFileSync(file, '{invalid json');
const fd = fs.openSync(file, 'r');
process.env.AGENT_FLOW_INSTALL_PLAN_FD = String(fd);
assert.throws(() => installer.prepareArchitectureInstall(process.env.PROJECT, []),
  /invalid architecture plan descriptor/);
assert.throws(() => fs.fstatSync(fd), {code: 'EBADF'});
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("valid_json", [False, True], ids=["parse-and-close", "close-only"])
def test_inherited_plan_preserves_primary_error_when_close_fails(
    tmp_path: Path, valid_json: bool,
) -> None:
    result = _architecture_node(tmp_path, """
import fs from 'node:fs';
import assert from 'node:assert/strict';
const file = process.env.PROJECT + '/plan.json';
fs.writeFileSync(file, process.env.VALID_JSON === 'yes' ? '{}' : '{invalid json');
const fd = fs.openSync(file, 'r');
process.env.AGENT_FLOW_INSTALL_PLAN_FD = String(fd);
const close = fs.closeSync;
fs.closeSync = descriptor => {
  close(descriptor);
  if (descriptor === fd) throw Object.assign(new Error('injected close failure'), {code: 'EIO'});
};
let failure;
try { installer.prepareArchitectureInstall(process.env.PROJECT, []); }
catch (error) { failure = error; }
finally { fs.closeSync = close; }
if (process.env.VALID_JSON === 'yes') {
  assert.equal(failure?.code, 'EIO');
} else {
  assert.ok(failure?.cause instanceof SyntaxError);
}
assert.equal(process.env.AGENT_FLOW_INSTALL_PLAN_FD, undefined);
assert.throws(() => fs.fstatSync(fd), {code: 'EBADF'});
""", VALID_JSON="yes" if valid_json else "no")
    assert result.returncode == 0, result.stderr
    if not valid_json:
        assert "injected close failure" in result.stderr
