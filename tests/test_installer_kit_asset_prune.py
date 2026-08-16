"""kit에서 사라진 자산이 설치본에 영원히 남는지 본다.

install은 자산을 갱신하고 교체하지만, kit이 더는 배포하지 않는 파일을 걷어내지
않았다. 실측: kit에서 `templates/{claude,codex,generic,omp}/stage.md`를 뺀 뒤로
install 직후마다 parity가 "has extra ... not in templates" 4건으로 실패했다.

지울 근거는 이미 매 install마다 쌓인다 - `.agent-flow/kit-assets.json`이 우리가 쓴
파일과 그 digest를 들고 있다. 그래서 판정은 "kit source를 다시 훑었는가"가 아니라
"기록에 있는데 이번에 쓰지 않은 것이 설치본에서 사라졌는가"로 본다.

두 진입점을 모두 태운다. 한쪽만 지우면 어느 CLI로 깔았는지에 따라 설치본이 갈린다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
SHARED_MODULE = KIT_ROOT / "lib" / "installer-shared.mjs"


# kit 사본에만 심는 자산. 실재하는 파일을 고르면 그 파일의 은퇴 여부에 테스트가
# 매이고, kit이 그것을 다시 배포하는 날 재현 조건이 조용히 사라진다.
RETIRED_DIR = "_retired"
UNTOUCHED_ASSET = "stage.md"
EDITED_ASSET = "notes.md"

# 사본 트리를 통째로 뜬다. `.git`은 크고 install이 읽지 않으며, `.agent-flow`는
# 이 checkout의 설치본이라 kit source가 아니다.
KIT_COPY_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".agent-flow")


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        # 이 파일의 모든 검사가 node를 태운다. 하나씩 skip하면 같은 사실이 검사 수만큼 쌓인다.
        pytest.skip("node를 찾을 수 없다", allow_module_level=True)
    return node


# 알림 접두사는 모듈에 하나씩만 있다. 테스트가 그것을 리터럴로 다시 적으면 표기를 바꾼
# 변경이 모듈과 테스트로 갈라지고, 옛 표기를 검사하는 테스트는 그대로 초록을 낸다 -
# 상수를 export한 이유가 그 단일 표기를 공유하는 것이다. 은퇴 hook 이름도 같은 이유로
# 모듈에서 읽는다: 목록이 바뀌면 여기 적은 이름은 아무것도 재현하지 못한다.
MODULE_PROBE = """
import * as shared from %(module)s;

process.stdout.write(JSON.stringify({
  prefixes: Object.fromEntries(
    Object.entries(shared).filter(([name]) => name.endsWith("_NOTICE_PREFIX")),
  ),
  retiredHookScripts: shared.retiredHookScripts(false),
}));
"""


def _shared_module_values() -> dict:
    result = subprocess.run(
        (
            _node(),
            "--input-type=module",
            "-e",
            MODULE_PROBE % {"module": json.dumps(SHARED_MODULE.as_uri())},
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_SHARED = _shared_module_values()
_NOTICES = _SHARED["prefixes"]

PRUNE_NOTICE_PREFIX = _NOTICES["PRUNE_NOTICE_PREFIX"]
SYMLINK_SKIP_NOTICE_PREFIX = _NOTICES["SYMLINK_SKIP_NOTICE_PREFIX"]
PRUNE_SOURCE_MISSING_NOTICE_PREFIX = _NOTICES["PRUNE_SOURCE_MISSING_NOTICE_PREFIX"]
PRUNE_SOURCE_UNREADABLE_NOTICE_PREFIX = _NOTICES["PRUNE_SOURCE_UNREADABLE_NOTICE_PREFIX"]
PRUNE_FAILED_NOTICE_PREFIX = _NOTICES["PRUNE_FAILED_NOTICE_PREFIX"]
PRUNE_UNREADABLE_NOTICE_PREFIX = _NOTICES["PRUNE_UNREADABLE_NOTICE_PREFIX"]
BACKUP_EXHAUSTED_NOTICE_PREFIX = _NOTICES["BACKUP_EXHAUSTED_NOTICE_PREFIX"]
ASSET_BACKUP_SKIP_NOTICE_PREFIX = _NOTICES["ASSET_BACKUP_SKIP_NOTICE_PREFIX"]
REMOVAL_BACKUP_SKIP_NOTICE_PREFIX = _NOTICES["REMOVAL_BACKUP_SKIP_NOTICE_PREFIX"]
PRUNE_BACKUP_FAILED_NOTICE_PREFIX = _NOTICES["PRUNE_BACKUP_FAILED_NOTICE_PREFIX"]
ASSET_BACKUP_FAILED_NOTICE_PREFIX = _NOTICES["ASSET_BACKUP_FAILED_NOTICE_PREFIX"]
ASSET_UPGRADE_NOTICE_PREFIX = _NOTICES["ASSET_UPGRADE_NOTICE_PREFIX"]
ASSET_BACKUP_NOTICE_PREFIX = _NOTICES["ASSET_BACKUP_NOTICE_PREFIX"]
SKILL_SKIP_NOTICE_PREFIX = _NOTICES["SKILL_SKIP_NOTICE_PREFIX"]

RETIRED_HOOK_SCRIPT = _SHARED["retiredHookScripts"][0]


def _kit_copy(destination: Path) -> Path:
    shutil.copytree(KIT_ROOT, destination, ignore=KIT_COPY_IGNORE, symlinks=True)
    return destination


def _install(kit: Path, project: Path, binary: str) -> str:
    result = subprocess.run(
        (_node(), str(kit / "bin" / binary), "install"),
        cwd=project,
        env={**os.environ},
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _recorded(project: Path) -> dict[str, str]:
    payload = json.loads((project / ".agent-flow" / "kit-assets.json").read_text(encoding="utf-8"))
    return payload["files"]


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_prunes_assets_the_kit_stopped_shipping(tmp_path: Path, binary: str) -> None:
    """반증: kit에서 뺀 파일이 설치본에 남으면 그 프로젝트의 parity는 영영 실패한다."""
    kit = _kit_copy(tmp_path / "kit")
    retired = kit / "templates" / RETIRED_DIR
    retired.mkdir(parents=True)
    (retired / UNTOUCHED_ASSET).write_text("stage template\n", encoding="utf-8")
    (retired / EDITED_ASSET).write_text("notes template\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    _install(kit, project, binary)

    installed = project / ".agent-flow" / "templates" / RETIRED_DIR
    assert (installed / UNTOUCHED_ASSET).is_file(), "심은 자산이 깔리지 않으면 이 검사는 아무것도 반증하지 못한다"
    recorded_first = _recorded(project)
    for name in (UNTOUCHED_ASSET, EDITED_ASSET):
        assert f".agent-flow/templates/{RETIRED_DIR}/{name}" in recorded_first

    # 사용자가 손댄 자산과 그대로인 자산을 함께 태운다. 판정 기준이 기록의 digest라
    # 둘의 결말이 갈라져야 한다.
    (installed / EDITED_ASSET).write_text("notes template\nmine\n", encoding="utf-8")
    survivor = project / ".agent-flow" / "templates" / "_shared" / "review" / "a11y.md"
    assert survivor.is_file()

    shutil.rmtree(retired)
    stdout = _install(kit, project, binary)

    untouched_label = f".agent-flow/templates/{RETIRED_DIR}/{UNTOUCHED_ASSET}"
    edited_label = f".agent-flow/templates/{RETIRED_DIR}/{EDITED_ASSET}"
    backup = project / ".agent-flow" / "backups" / "templates" / RETIRED_DIR / EDITED_ASSET

    assert not (installed / UNTOUCHED_ASSET).exists(), "kit이 더는 배포하지 않는 자산이 설치본에 남았다"
    assert not (installed / EDITED_ASSET).exists()
    assert f"{PRUNE_NOTICE_PREFIX}{untouched_label}" in stdout.splitlines(), stdout
    assert f"{PRUNE_NOTICE_PREFIX}{edited_label} (backup: .agent-flow/backups/templates/{RETIRED_DIR}/{EDITED_ASSET})" in stdout.splitlines(), stdout
    assert backup.read_text(encoding="utf-8") == "notes template\nmine\n", "사용자 편집을 사본 없이 지웠다"

    # 빈 껍데기가 남으면 kit이 더는 배포하지 않는 자리가 트리에 그대로 보인다.
    assert not installed.exists()
    assert (project / ".agent-flow" / "templates").is_dir(), "선언된 dest 루트까지 지우면 다음 install이 쓸 자리가 사라진다"

    recorded_second = _recorded(project)
    assert untouched_label not in recorded_second and edited_label not in recorded_second
    assert survivor.is_file(), "kit이 여전히 배포하는 자산까지 걷어냈다"
    assert recorded_second[".agent-flow/templates/_shared/review/a11y.md"]


# prune 대상 판정만 떼어 본다. install 한 판을 더 돌리는 대신 공유 모듈을 직접
# 호출하는 것은, 여기서 보려는 것이 "기록에 적힌 경로를 얼마나 믿는가"뿐이기 때문이다.
PRUNE_SCOPE_PROBE = """
import { syncRecordedKitAssets } from %(module)s;

const root = %(root)s;
const kitRoot = %(kit)s;
const recorded = new Map(Object.entries(%(recorded)s));
const written = new Map();
syncRecordedKitAssets(root, kitRoot, recorded, written);
process.stdout.write(JSON.stringify({ recorded: [...written.keys()].sort() }));
"""


def _seed_prune_scope_case(root: Path, kit: Path) -> dict[str, str]:
    """기록만 있고 kit source는 없는 자산 넷을 깐다. 하나만 지워져야 한다."""
    (kit / "templates").mkdir(parents=True)
    templates = root / ".agent-flow" / "templates"
    (templates / "_retired").mkdir(parents=True)
    (templates / "_retired" / "stage.md").write_text("kit\n", encoding="utf-8")
    # 기록 파일은 프로젝트 안에 있어 손으로 고칠 수 있다. `..`를 낀 label이 선언된
    # 자리 밖을 지우는 통로가 되면 안 된다.
    (root / "escape.md").write_text("outside\n", encoding="utf-8")
    (root / "README.md").write_text("unrelated\n", encoding="utf-8")
    # 링크 너머는 대개 프로젝트 밖이다. 판정 대상이 아니라 건너뛰어야 한다.
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "linked.md").write_text("linked\n", encoding="utf-8")
    (templates / "linked").symlink_to(outside)
    return {
        ".agent-flow/templates/_retired/stage.md": "not-the-digest",
        ".agent-flow/templates/../../escape.md": "not-the-digest",
        "README.md": "not-the-digest",
        ".agent-flow/templates/linked/linked.md": "not-the-digest",
    }


def test_prune_stays_inside_declared_asset_roots(tmp_path: Path) -> None:
    """반증: 기록을 그대로 따라가면 손으로 고친 한 줄이 임의 경로 삭제가 된다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    recorded = _seed_prune_scope_case(root, kit)

    probe = tmp_path / "probe.mjs"
    probe.write_text(
        PRUNE_SCOPE_PROBE % {
            "module": json.dumps(SHARED_MODULE.as_uri()),
            "root": json.dumps(str(root)),
            "kit": json.dumps(str(kit)),
            "recorded": json.dumps(recorded),
        },
        encoding="utf-8",
    )
    result = subprocess.run(
        (_node(), str(probe)),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    assert not (root / ".agent-flow" / "templates" / "_retired").exists()
    assert (root / "escape.md").is_file(), "선언된 트리 밖을 지웠다"
    assert (root / "README.md").is_file(), "선언된 자산 자리 밖을 지웠다"
    assert (tmp_path / "outside" / "linked.md").is_file(), "링크 너머를 지웠다"
    assert f"{SYMLINK_SKIP_NOTICE_PREFIX}.agent-flow/templates/linked/linked.md" in result.stdout

    # 링크 때문에 미룬 판정은 기록에 남아야 다음 install이 다시 시도한다. 나머지
    # 셋은 지웠거나(첫째) 판정 밖이라(둘·셋째) 기록에 남지 않는다.
    written = json.loads(result.stdout.splitlines()[-1])["recorded"]
    assert written == [".agent-flow/templates/linked/linked.md"]


# 여기부터는 prune이 "지우면 안 되는 것을 지운다"와 "지워야 할 것을 영영 못 지운다"의
# 양쪽 끝을 본다. 둘 다 이 기록 구조가 새로 만든 실패다.

# kit에서 통째로 사라뜨려 볼 트리. `.Codex/agents`는 `assertInstalled`가 요구하는
# 파일이 들어 있어, 지워지면 install이 다른 이유로 죽어 무엇을 반증했는지 흐려진다.
MISSING_TREE_SRC = Path(".Codex") / "rules" / "context"
MISSING_TREE_DEST = ".Codex/rules/context"

ASSET_LABEL = f".agent-flow/templates/{RETIRED_DIR}/{UNTOUCHED_ASSET}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _probe(tmp_path: Path, root: Path, kit: Path, recorded: dict[str, str]) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        PRUNE_SCOPE_PROBE % {
            "module": json.dumps(SHARED_MODULE.as_uri()),
            "root": json.dumps(str(root)),
            "kit": json.dumps(str(kit)),
            "recorded": json.dumps(recorded),
        },
        encoding="utf-8",
    )
    return subprocess.run(
        (_node(), str(probe)),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def _written(result: subprocess.CompletedProcess[str]) -> list[str]:
    return json.loads(result.stdout.splitlines()[-1])["recorded"]


def _seed_asset(root: Path, kit: Path, content: str) -> tuple[Path, Path]:
    """설치본 자산 하나와 그 사본 자리를 돌려준다. kit `templates`는 살려 둔다 -
    원본이 있는 트리라야 prune이 은퇴를 판정한다."""
    (kit / "templates").mkdir(parents=True, exist_ok=True)
    target = root / ".agent-flow" / "templates" / RETIRED_DIR / UNTOUCHED_ASSET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    backup = root / ".agent-flow" / "backups" / "templates" / RETIRED_DIR / UNTOUCHED_ASSET
    backup.parent.mkdir(parents=True, exist_ok=True)
    return target, backup


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_keeps_assets_when_the_kit_source_tree_is_gone(tmp_path: Path, binary: str) -> None:
    """반증: 패키징에서 트리 하나가 빠지면 install 한 번이 그 설치본을 통째로 지운다.

    "kit이 이 파일을 은퇴시켰다"와 "kit에 이 자리가 아예 없다"는 다른 사건이다. 없는
    디렉터리를 훑으면 둘 다 "이번에 쓴 것이 없다"로 보이고, 그 둘을 겹쳐 읽으면 배포
    사고 하나가 사용자 설치본을 지운다."""
    kit = _kit_copy(tmp_path / "kit")
    project = tmp_path / "project"
    project.mkdir()
    _install(kit, project, binary)

    installed = project / MISSING_TREE_SRC
    names = sorted(entry.name for entry in installed.iterdir() if entry.is_file())
    assert names, "이 트리가 깔리지 않으면 이 검사는 아무것도 반증하지 못한다"
    labels = [f"{MISSING_TREE_DEST}/{name}" for name in names]
    recorded_first = _recorded(project)
    for label in labels:
        assert label in recorded_first

    # 패키징 사고: kit에서 그 자리가 통째로 빠졌다.
    shutil.rmtree(kit / MISSING_TREE_SRC)
    stdout = _install(kit, project, binary)

    assert sorted(entry.name for entry in installed.iterdir() if entry.is_file()) == names, (
        "kit 원본이 사라졌다는 이유로 설치본을 지웠다"
    )
    recorded_second = _recorded(project)
    for label in labels:
        # 기록까지 잃으면 다음 install은 그 파일을 "우리 것이 아니다"로 읽는다.
        assert recorded_second.get(label) == recorded_first[label]
    assert f"{PRUNE_SOURCE_MISSING_NOTICE_PREFIX}{MISSING_TREE_DEST}" in stdout.splitlines(), stdout


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root는 권한 검사를 통과한다")
def test_prune_keeps_the_record_when_removal_fails(tmp_path: Path) -> None:
    """반증: 삭제 실패가 install을 던지면 그 자산은 지워지지도, 기록되지도 않는다.

    기록이 빠지면 다음 install은 그것을 "우리 것이 아니다"로 읽어 영영 재시도하지
    않는다 - 지우지도 못하고 기록도 잃는 상태다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, _ = _seed_asset(root, kit, "kit\n")
    # 우리가 쓴 그대로라 사본 없이 지우려 한다. unlink에는 부모 디렉터리 쓰기 권한이 필요하다.
    recorded = {ASSET_LABEL: _digest("kit\n")}
    target.parent.chmod(0o500)
    try:
        result = _probe(tmp_path, root, kit, recorded)
    finally:
        target.parent.chmod(0o700)

    assert result.returncode == 0, result.stderr
    assert target.is_file(), "지우지 못했는데 지운 것으로 굴었다"
    assert any(
        line.startswith(f"{PRUNE_FAILED_NOTICE_PREFIX}{ASSET_LABEL} (")
        for line in result.stdout.splitlines()
    ), result.stdout
    assert _written(result) == [ASSET_LABEL], "지우지 못한 자산을 기록에서 뺐다"


def test_backup_slot_symlink_is_not_taken_as_an_existing_copy(tmp_path: Path) -> None:
    """반증: 사본 자리에 원본을 가리키는 링크를 두면 사본도 원본도 사라진다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset(root, kit, "kit\nmine\n")
    backup.symlink_to(target)
    recorded = {ASSET_LABEL: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    real = Path(f"{backup}.1")
    assert real.is_file() and not real.is_symlink(), "링크를 사본으로 인정했다"
    assert real.read_text(encoding="utf-8") == "kit\nmine\n", "사용자 편집이 사본 없이 사라졌다"
    assert not target.exists()
    assert backup.is_symlink(), "우리가 만들지 않은 링크를 갈아 끼웠다"
    assert f"{PRUNE_NOTICE_PREFIX}{ASSET_LABEL} (backup: {real.relative_to(root).as_posix()})" in (
        result.stdout.splitlines()
    ), result.stdout


def test_prune_reuses_an_identical_backup_instead_of_giving_up(tmp_path: Path) -> None:
    """반증: 이미 안전하게 백업된 자산이 "사본 실패"로 읽혀 영영 prune되지 않는다.

    그 상태에서는 install이 매번 같은 자산을 남겨 두고 기록도 그대로 들고 가, 어떤
    설치본도 kit과 같아지지 않는다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset(root, kit, "kit\nmine\n")
    backup.write_text("older\n", encoding="utf-8")
    Path(f"{backup}.1").write_text("kit\nmine\n", encoding="utf-8")
    recorded = {ASSET_LABEL: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert not target.exists(), "이미 사본이 있는데도 지우지 못했다"
    assert Path(f"{backup}.1").read_text(encoding="utf-8") == "kit\nmine\n"
    assert not Path(f"{backup}.2").exists(), "같은 내용을 한 벌 더 남겼다"
    assert backup.read_text(encoding="utf-8") == "older\n", "먼저 있던 사본을 덮었다"
    assert f"{PRUNE_NOTICE_PREFIX}{ASSET_LABEL} (backup: {Path(f'{backup}.1').relative_to(root).as_posix()})" in (
        result.stdout.splitlines()
    ), result.stdout
    assert _written(result) == [], "지운 자산이 기록에 남았다"


# 여기부터는 사본이 원본을 **바이트로** 보존하는지 본다. `"utf8"`로 읽어 그 문자열을
# 저장한 것은 사본이 아니다 - UTF-8 디코딩은 나쁜 바이트마다 U+FFFD를 내는 손실
# 변환이고, 그 뒤 원본은 지워진다. 유효한 UTF-8이 아닌 설치 자산은 실재한다:
# `.agent-flow/skills/<skill>/references/` 아래 바이너리, Windows 도구가 latin-1로
# 다시 쓴 파일.
NON_UTF8_ASSET = b"kit\n\xff\xfe\xc3\n\x80\n"


def _seed_asset_bytes(root: Path, kit: Path, content: bytes) -> tuple[Path, Path]:
    target, backup = _seed_asset(root, kit, "")
    target.write_bytes(content)
    return target, backup


def test_prune_backup_keeps_bytes_that_are_not_valid_utf8(tmp_path: Path) -> None:
    """반증: prune은 자산을 `"utf8"`로 읽어 그 **문자열**을 유일한 사본으로 남기고
    원본을 지웠다. 그 사본은 나쁜 바이트마다 U+FFFD로 바뀌어 있어, 원본이 사라진 뒤에는
    무엇이 있었는지 알 수도 되돌릴 수도 없다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset_bytes(root, kit, NON_UTF8_ASSET)
    recorded = {ASSET_LABEL: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert not target.exists(), "은퇴 자산이 남았다"
    kept = backup.read_bytes()
    assert kept == NON_UTF8_ASSET, "사본이 원본 바이트와 다르다 - 되돌릴 수 없다"
    # U+FFFD의 UTF-8 인코딩. 사본에 이것이 있으면 디코딩을 거친 것이다.
    assert b"\xef\xbf\xbd" not in kept


def test_backup_slot_reuse_is_decided_on_bytes_not_decoded_text(tmp_path: Path) -> None:
    """반증: 사본 자리 비교를 디코딩해서 하면 서로 다른 바이트가 같은 U+FFFD로 접혀
    같아 보인다. prune은 그 자리를 "사본은 거기 있다"로 읽고 원본을 지우는데, 거기
    남아 있는 것은 다른 내용이다 - 사본이 있다는 답이 거짓이 된다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    original = b"kit\n\xff\n"
    target, backup = _seed_asset_bytes(root, kit, original)
    # 다른 바이트지만 UTF-8로 디코딩하면 원본과 같은 문자열이다.
    other = b"kit\n\xfe\n"
    backup.write_bytes(other)
    recorded = {ASSET_LABEL: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert backup.read_bytes() == other, "먼저 있던 사본을 덮었다"
    real = Path(f"{backup}.1")
    assert real.read_bytes() == original, "원본 바이트가 어디에도 남지 않았다"
    assert not target.exists()
    assert f"{PRUNE_NOTICE_PREFIX}{ASSET_LABEL} (backup: {real.relative_to(root).as_posix()})" in (
        result.stdout.splitlines()
    ), result.stdout


def _fill_backup_slots(backup: Path) -> None:
    """100개 이름을 서로 다른 내용으로 채운다. 어느 것도 지금 내용과 같지 않다."""
    backup.write_text("slot-0\n", encoding="utf-8")
    for index in range(1, 100):
        Path(f"{backup}.{index}").write_text(f"slot-{index}\n", encoding="utf-8")


def test_prune_keeps_the_asset_when_every_backup_slot_is_taken(tmp_path: Path) -> None:
    """반증: 사본을 못 남긴 사실을 알리지 않으면 사용자는 왜 안 지워지는지 알 수 없다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset(root, kit, "kit\nmine\n")
    _fill_backup_slots(backup)
    recorded = {ASSET_LABEL: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert target.read_text(encoding="utf-8") == "kit\nmine\n", "사본 없이 사용자 편집을 지웠다"
    # 알림은 root-relative label이다. 절대 경로를 실으면 이 파일의 형제 알림과
    # 어긋나고, 사용자에게 제 파일시스템 배치를 보여 준다.
    assert (
        f"{BACKUP_EXHAUSTED_NOTICE_PREFIX}{backup.relative_to(root).as_posix()}"
        in result.stdout.splitlines()
    ), result.stdout
    assert str(backup) not in result.stdout, "절대 경로를 알림에 실었다"
    assert _written(result) == [ASSET_LABEL]


def test_asset_upgrade_stops_when_no_backup_slot_is_free(tmp_path: Path) -> None:
    """반증: 사본을 못 남겼는데 덮으면 사용자가 고친 자산이 사본 없이 사라진다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset(root, kit, "mine\n")
    source = kit / "templates" / RETIRED_DIR / UNTOUCHED_ASSET
    source.parent.mkdir(parents=True)
    source.write_text("kit v2\n", encoding="utf-8")
    _fill_backup_slots(backup)

    # 기록이 없는 `.agent-flow/` 자산이다. 사본을 남기고 한 번 갱신하는 자리이므로,
    # 사본을 못 남기면 갱신도 멈춰야 한다.
    result = _probe(tmp_path, root, kit, {})
    assert result.returncode == 0, result.stderr

    assert target.read_text(encoding="utf-8") == "mine\n", "사본 없이 사용자 편집을 덮었다"
    assert f"{ASSET_BACKUP_SKIP_NOTICE_PREFIX}{ASSET_LABEL}" in result.stdout.splitlines(), result.stdout
    assert _written(result) == [], "우리가 쓰지 않은 내용을 우리 것으로 기록했다"


# 여기부터는 "사본을 만든다"는 행위 자체가 잃을 수 있는 것을 본다. 사본 자리에 이미
# 같은 내용이 들어 있으면 다시 쓸 것이 없고, 다시 쓰는 순간 그 쓰기가 유일한 사본을
# truncate하는 창이 된다.

BACKUP_IF_DIFFERENT_PROBE = """
import { backupIfDifferent } from %(module)s;

process.stdout.write(JSON.stringify(backupIfDifferent(%(root)s, %(target)s, %(content)s)));
"""


def _backup_if_different(
    tmp_path: Path,
    root: Path,
    target: Path,
    content: str,
) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "backup-if-different.mjs"
    probe.write_text(
        BACKUP_IF_DIFFERENT_PROBE % {
            "module": json.dumps(SHARED_MODULE.as_uri()),
            "root": json.dumps(str(root)),
            "target": json.dumps(str(target)),
            "content": json.dumps(content),
        },
        encoding="utf-8",
    )
    return subprocess.run(
        (_node(), str(probe)),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def _fill_bak_slots(target: Path) -> None:
    """`.bak` 이름 100개를 서로 다른 내용으로 채운다. `nextFreeBackupPath`가 사본
    자리를 하나도 내주지 못하는 상태다."""
    for index in range(100):
        suffix = "" if index == 0 else f".{index}"
        Path(f"{target}.bak{suffix}").write_text(f"slot-{index}\n", encoding="utf-8")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root는 권한 검사를 통과한다")
def test_backup_if_different_does_not_rewrite_an_identical_copy(tmp_path: Path) -> None:
    """반증: 이미 같은 내용이 든 사본 자리에 `copyFileSync`를 다시 걸면, 그 쓰기가

    유일한 사본을 truncate하는 창이 된다 - 호출부는 그 직후 원본을 덮으므로 창이
    열린 사이에 죽으면 지키려던 내용이 원본에도 사본에도 없다. 쓰기 권한을 뗀 사본
    자리로 그 쓰기를 관측한다: 다시 쓰려 들면 EACCES로 install이 죽는다."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text("mine\n", encoding="utf-8")
    backup = Path(f"{target}.bak")
    backup.write_text("mine\n", encoding="utf-8")
    backup.chmod(0o400)

    try:
        result = _backup_if_different(tmp_path, root, target, "kit\n")
    finally:
        backup.chmod(0o600)

    assert result.returncode == 0, result.stderr
    assert backup.read_text(encoding="utf-8") == "mine\n", "사본을 다시 써서 잃었다"
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"safeToWrite": True, "backup": str(backup)}, "사본이 거기 있는데 없다고 답했다"


def test_backup_if_different_compares_bytes_before_clearing_the_write(tmp_path: Path) -> None:
    """반증: "지킬 것이 없다"를 디코딩해서 판정하면 나쁜 바이트가 U+FFFD로 접혀 다른
    내용이 "이미 같다"로 읽히고, 호출부는 그 답을 받아 사본 없이 원본을 덮는다."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / ".agent-flow" / "scripts" / "hooks" / "guard-protected-branch.sh"
    target.parent.mkdir(parents=True)
    original = b"kit\n\xff\n"
    target.write_bytes(original)

    # 원본을 UTF-8로 디코딩하면 이 문자열이 된다. 바이트로 견주지 않으면 같아 보인다.
    result = _backup_if_different(tmp_path, root, target, "kit\n\ufffd\n")
    assert result.returncode == 0, result.stderr

    backup = Path(f"{target}.bak")
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"safeToWrite": True, "backup": str(backup)}, "사본 없이 덮어도 된다고 답했다"
    assert backup.read_bytes() == original, "사본이 원본 바이트와 다르다"


def test_backup_if_different_refuses_the_write_when_no_backup_slot_is_free(tmp_path: Path) -> None:
    """반증: 사본을 못 만들었는데 "덮어도 된다"고 답하면 호출부는 그 직후 원본을 덮고,

    사용자 편집은 사본 없이 사라진다. 답 셋("지킬 것이 없다"·"사본을 확보했다"·"사본을
    만들 수 없다")을 `null` 하나와 경로로 내던 것이 그 오독의 뿌리다 - 앞의 둘과
    마지막이 같은 falsy였다. 같은 파일의 삭제 경로들이 지키는 계약과 정반대다."""
    root = tmp_path / "project"
    target = root / ".agent-flow" / "scripts" / "hooks" / "guard-protected-branch.sh"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")
    _fill_bak_slots(target)

    result = _backup_if_different(tmp_path, root, target, "kit\n")
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"safeToWrite": False, "backup": None}, "사본이 없는데 덮어도 된다고 답했다"
    assert target.read_text(encoding="utf-8") == "mine\n", "판정 함수가 원본을 건드렸다"
    assert (
        f"{ASSET_BACKUP_SKIP_NOTICE_PREFIX}.agent-flow/scripts/hooks/guard-protected-branch.sh"
        in result.stdout.splitlines()
    ), result.stdout


@pytest.mark.parametrize("seed", ["absent", "identical"])
def test_backup_if_different_still_clears_the_write_when_there_is_nothing_to_protect(
    tmp_path: Path, seed: str
) -> None:
    """계약: 지킬 것이 없으면 사본도 없이 덮어도 된다. 이 답까지 막으면 첫 설치와
    무변경 재설치가 통째로 멈춘다 - 사본을 요구하는 것은 잃을 것이 있을 때뿐이다."""
    root = tmp_path / "project"
    target = root / ".agent-flow" / "profiles" / "generic.yaml"
    target.parent.mkdir(parents=True)
    if seed == "identical":
        target.write_text("kit\n", encoding="utf-8")

    result = _backup_if_different(tmp_path, root, target, "kit\n")
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"safeToWrite": True, "backup": None}


def test_asset_backup_refuses_a_symlinked_parent_chain(tmp_path: Path) -> None:
    """반증: 사본 자리의 마지막 이름만 검사하면 `backups/`를 링크로 바꿔 사본을

    프로젝트 밖에 만들게 하고, install은 그것을 "사본이 있다"로 읽어 원본을 지운다.
    사본이 프로젝트 밖에 있으면 그것은 사본이 아니라 유출이다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, _ = _seed_asset(root, kit, "kit\nmine\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    backups = root / ".agent-flow" / "backups"
    shutil.rmtree(backups)
    backups.symlink_to(outside)
    recorded = {ASSET_LABEL: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert list(outside.iterdir()) == [], "링크 너머에 사본을 만들었다"
    assert target.read_text(encoding="utf-8") == "kit\nmine\n", "사본 없이 사용자 편집을 지웠다"
    assert backups.is_symlink(), "우리가 만들지 않은 링크를 갈아 끼웠다"
    assert (
        f"{SYMLINK_SKIP_NOTICE_PREFIX}.agent-flow/backups/templates/{RETIRED_DIR}/{UNTOUCHED_ASSET}"
        in result.stdout.splitlines()
    ), result.stdout
    # 링크를 치운 뒤 다음 install이 다시 판정해야 한다. 기록을 떨어뜨리면 그 근거가 사라진다.
    assert _written(result) == [ASSET_LABEL]


LOCKED_DIR = "_locked"
LOCKED_LABEL = f".agent-flow/templates/{LOCKED_DIR}/{UNTOUCHED_ASSET}"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root는 권한 검사를 통과한다")
def test_one_unreadable_asset_does_not_stop_the_install(tmp_path: Path) -> None:
    """반증: 기록된 자산 하나를 읽을 수 없을 때 던지면 install 전체가 죽고, 호출부의

    `writeKitAssetRecord`에 닿지 못해 나머지 자산의 기록까지 잃는다 - `rmSync`를
    감싼 것과 정확히 같은 사건이다. 한 자산의 실패는 그 자산에서 멈춰야 한다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    prunable, _ = _seed_asset(root, kit, "kit\n")
    locked_dir = root / ".agent-flow" / "templates" / LOCKED_DIR
    locked_dir.mkdir(parents=True)
    locked = locked_dir / UNTOUCHED_ASSET
    locked.write_text("kit\n", encoding="utf-8")
    recorded = {LOCKED_LABEL: _digest("kit\n"), ASSET_LABEL: _digest("kit\n")}
    # 부모 디렉터리를 훑을 수 없으면 `lstat`부터 EACCES다. 상위 소유 자산이 프로젝트에
    # 섞인 실측 배치와 같은 모양이다.
    locked_dir.chmod(0o000)
    try:
        result = _probe(tmp_path, root, kit, recorded)
    finally:
        locked_dir.chmod(0o700)

    assert result.returncode == 0, result.stderr
    assert locked.is_file(), "읽을 수 없는 자산을 지웠다"
    assert not prunable.exists(), "한 자산의 실패가 나머지 판정까지 멈췄다"
    assert any(
        line.startswith(f"{PRUNE_UNREADABLE_NOTICE_PREFIX}{LOCKED_LABEL} (")
        for line in result.stdout.splitlines()
    ), result.stdout
    # 판정하지 못한 자산은 기록에 남아야 다음 install이 다시 시도한다.
    assert _written(result) == [LOCKED_LABEL]


# 은퇴한 bundled skill. `<skill>/SKILL.md`는 자산 기록에서 빠지므로(`isBundledSkillManifest`)
# prune이 지울 수 없고, 형제만 지우면 `SKILL.md`만 남은 껍데기가 된다. 그 껍데기는
# 사라지지 않는다 - `discoverSkills`는 디렉터리에 `SKILL.md` 하나만 있어도 그것을
# `index.json`에 싣고(`bin/agent-flow-kit.mjs`), `skill_resolver.py`의 `bundled` 후보도
# 같은 경로 하나를 본다. 그래서 은퇴 판정 단위를 파일이 아니라 skill 디렉터리로 올린다.
RETIRED_SKILL = "retired-demo"
RETIRED_SKILL_REFERENCE = f".agent-flow/skills/{RETIRED_SKILL}/references/guide.md"


def _seed_bundled_skill(root: Path, kit: Path, *, kit_ships_skill: bool) -> tuple[Path, Path]:
    """설치본 skill 하나를 깐다. kit `skills/`는 살려 둔다 - 트리가 없으면 prune이
    트리 단위로 판정을 미뤄 skill 단위 판정에 닿지 않는다."""
    (kit / "skills").mkdir(parents=True, exist_ok=True)
    if kit_ships_skill:
        kit_manifest = kit / "skills" / RETIRED_SKILL / "SKILL.md"
        kit_manifest.parent.mkdir(parents=True, exist_ok=True)
        kit_manifest.write_text("---\nname: retired-demo\n---\n", encoding="utf-8")
    installed = root / ".agent-flow" / "skills" / RETIRED_SKILL
    (installed / "references").mkdir(parents=True)
    manifest = installed / "SKILL.md"
    manifest.write_text("---\nname: retired-demo\n---\n", encoding="utf-8")
    reference = installed / "references" / "guide.md"
    reference.write_text("kit\n", encoding="utf-8")
    return manifest, reference


def test_prune_keeps_a_retired_bundled_skill_whole(tmp_path: Path) -> None:
    """계약: kit이 skill 하나를 통째로 은퇴시키면 그 아래는 하나도 지우지 않는다.

    `SKILL.md`는 prune이 지울 수 없으므로 형제만 지우면 껍데기가 남고, 그 껍데기는
    여전히 index에 실려 agent에게 "이 skill을 읽어라"라고 지시한다 - 그 skill이
    가리키는 `references/*`는 이미 없다. 통째로 걷어내는 일은 `--force-managed`의
    extraneous prune이 사본과 함께 한다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    manifest, reference = _seed_bundled_skill(root, kit, kit_ships_skill=False)
    recorded = {RETIRED_SKILL_REFERENCE: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert manifest.is_file(), "지울 수 없는 `SKILL.md`가 사라졌다"
    assert reference.is_file(), "반쪽만 지워 껍데기 skill을 남겼다"
    assert (
        f"{PRUNE_SOURCE_MISSING_NOTICE_PREFIX}.agent-flow/skills/{RETIRED_SKILL}"
        in result.stdout.splitlines()
    ), result.stdout
    assert _written(result) == [RETIRED_SKILL_REFERENCE], "판정을 미뤘는데 기록을 떨어뜨렸다"


def test_prune_still_removes_a_retired_sibling_of_a_living_skill(tmp_path: Path) -> None:
    """계약: kit이 그 skill을 여전히 배포하면 은퇴한 형제 파일은 지워진다.

    위 단위 판정이 이 자리까지 막으면 kit에서 뺀 `references/*`가 설치본에 영원히
    남아, 지금 이 파일이 막는 parity 실패가 skill 트리에서 되살아난다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    manifest, reference = _seed_bundled_skill(root, kit, kit_ships_skill=True)
    recorded = {RETIRED_SKILL_REFERENCE: _digest("kit\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert not reference.exists(), "kit이 더는 배포하지 않는 형제를 남겼다"
    assert manifest.is_file(), "index hash가 오라클인 파일을 자산 prune이 지웠다"
    assert f"{PRUNE_NOTICE_PREFIX}{RETIRED_SKILL_REFERENCE}" in result.stdout.splitlines(), result.stdout
    assert _written(result) == []


# 여기부터는 이 기록 구조보다 먼저 있던 삭제 경로 셋을 본다. `nextFreeBackupPath`의
# `null`은 이제 "사본을 만들 수 없다" 하나뿐인데, 셋은 그것을 여전히 "사본은 없어도
# 된다"로 읽고 원본을 지웠다 - 새 prune 경로가 지키는 계약과 정반대다.
LEGACY_REMOVAL_PROBE = """
import * as shared from %(module)s;

shared[%(fn)s](...%(args)s);
"""

OMP_EXTENSION_RELATIVE = ".omp/extensions/agent-flow-hooks.ts"
MANAGED_SCRIPT_RELATIVE = ".agent-flow/scripts/check-context-docs.mjs"
HOOK_SCRIPT_RELATIVE = f".agent-flow/scripts/hooks/{RETIRED_HOOK_SCRIPT}"


def _legacy_probe(
    tmp_path: Path,
    name: str,
    args: list[object],
) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / f"{name}.mjs"
    probe.write_text(
        LEGACY_REMOVAL_PROBE % {
            "module": json.dumps(SHARED_MODULE.as_uri()),
            "fn": json.dumps(name),
            "args": json.dumps(args),
        },
        encoding="utf-8",
    )
    return subprocess.run(
        (_node(), str(probe)),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def _seed_removal_target(root: Path, relative: str, content: str) -> Path:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _fill_removed_slots(target: Path) -> None:
    """`.removed` 이름 100개를 서로 다른 내용으로 채운다. `nextFreeBackupPath`가 사본
    자리를 하나도 내주지 못하는 상태이고, 그 답이 `null`이다."""
    for index in range(100):
        suffix = "" if index == 0 else f".{index}"
        Path(f"{target}.removed{suffix}").write_text(f"slot-{index}\n", encoding="utf-8")


def test_retired_managed_script_survives_when_no_backup_slot_is_free(tmp_path: Path) -> None:
    """반증: 사본을 못 만들었는데 지우면 사용자가 같은 이름으로 둔 스크립트가 사본
    없이 사라진다. `pruneRetiredManagedScripts`는 `null`을 무시하고 지웠다."""
    root = tmp_path / "project"
    target = _seed_removal_target(root, MANAGED_SCRIPT_RELATIVE, "mine\n")
    _fill_removed_slots(target)

    result = _legacy_probe(tmp_path, "pruneRetiredManagedScripts", [str(root)])
    assert result.returncode == 0, result.stderr

    assert target.read_text(encoding="utf-8") == "mine\n", "사본 없이 원본을 지웠다"
    assert (
        f"{REMOVAL_BACKUP_SKIP_NOTICE_PREFIX}{MANAGED_SCRIPT_RELATIVE}" in result.stdout.splitlines()
    ), result.stdout


def test_omp_hooks_extension_survives_when_no_backup_slot_is_free(tmp_path: Path) -> None:
    """반증: hook을 끄는 일이 되돌릴 수 없는 삭제가 되면 안 된다. 사본을 못 만들면
    남기고 알린다 - 남은 파일은 사용자가 직접 치울 수 있지만 지운 파일은 아니다."""
    root = tmp_path / "project"
    # kit 소유 표식이 없으면 다른 분기(`is not kit-managed`)에서 멈춰 무엇을 반증했는지 흐려진다.
    body = "export default function agentFlowHooks() {}\n"
    target = _seed_removal_target(root, OMP_EXTENSION_RELATIVE, body)
    _fill_removed_slots(target)

    result = _legacy_probe(tmp_path, "removeOmpHooksExtension", [str(root)])
    assert result.returncode == 0, result.stderr

    assert target.read_text(encoding="utf-8") == body, "사본 없이 원본을 지웠다"
    assert (
        f"{REMOVAL_BACKUP_SKIP_NOTICE_PREFIX}{OMP_EXTENSION_RELATIVE}" in result.stdout.splitlines()
    ), result.stdout


def test_retired_hook_script_survives_when_no_backup_slot_is_free(tmp_path: Path) -> None:
    """반증: 은퇴한 이름 자리에 사용자가 자기 스크립트를 뒀을 수 있다. 사본을 못
    만들었는데 지우면 그 스크립트가 사본 없이 사라진다."""
    root = tmp_path / "project"
    target = _seed_removal_target(root, HOOK_SCRIPT_RELATIVE, "mine\n")
    _fill_removed_slots(target)

    result = _legacy_probe(tmp_path, "pruneRetiredHookScripts", [str(root), False])
    assert result.returncode == 0, result.stderr

    assert target.read_text(encoding="utf-8") == "mine\n", "사본 없이 원본을 지웠다"
    assert (
        f"{REMOVAL_BACKUP_SKIP_NOTICE_PREFIX}{HOOK_SCRIPT_RELATIVE}" in result.stdout.splitlines()
    ), result.stdout


def test_retired_hook_notice_names_the_backup_it_actually_made(tmp_path: Path) -> None:
    """반증: 이 알림은 host 설정이 아직 가리키는 경로를 되돌리라고 있는 것이다.
    `.removed`를 무조건 말하면 실제 사본은 `.removed.1`에 있고, 사용자는 없는 파일을
    찾다가 되돌리기를 포기한다."""
    root = tmp_path / "project"
    target = _seed_removal_target(root, HOOK_SCRIPT_RELATIVE, "mine\n")
    # 첫 이름은 다른 내용이 이미 차지했다. 사본은 다음 이름으로 간다.
    taken = Path(f"{target}.removed")
    taken.write_text("older\n", encoding="utf-8")

    result = _legacy_probe(tmp_path, "pruneRetiredHookScripts", [str(root), False])
    assert result.returncode == 0, result.stderr

    assert not target.exists(), "지울 수 있는 은퇴 hook을 남겼다"
    assert taken.read_text(encoding="utf-8") == "older\n", "먼저 있던 사본을 덮었다"
    kept = Path(f"{target}.removed.1")
    assert kept.read_text(encoding="utf-8") == "mine\n", "사본을 만들지 않았다"
    assert (
        f"  - removed retired hook: {HOOK_SCRIPT_RELATIVE} "
        f"(backup: {kept.relative_to(root).as_posix()})" in result.stdout.splitlines()
    ), result.stdout


def test_prune_keeps_the_record_when_the_kit_source_cannot_be_stat(tmp_path: Path) -> None:
    """반증: `statSync`의 `throwIfNoEntry: false`는 ENOENT만 삼킨다. EACCES·ELOOP가
    그대로 올라오면 `syncRecordedKitAssets`가 던져 install 전체가 죽고, 호출부의
    `writeKitAssetRecord`에 닿지 못해 기록까지 잃는다. 판정 불가는 "kit이 뺐다"가
    아니므로 지우지도 않는다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target = root / ".agent-flow" / "templates" / RETIRED_DIR / UNTOUCHED_ASSET
    target.parent.mkdir(parents=True)
    target.write_text("kit\n", encoding="utf-8")
    # 자기 자신을 가리키는 링크. `statSync`는 ELOOP을 던진다 - 없는 자리와 다른 사건이다.
    loop = kit / "templates"
    loop.symlink_to(loop)

    result = _probe(tmp_path, root, kit, {ASSET_LABEL: _digest("kit\n")})
    assert result.returncode == 0, result.stderr

    assert target.is_file(), "kit이 배포하는지 말할 수 없는데 설치본을 지웠다"
    assert any(
        line.startswith(f"{PRUNE_SOURCE_UNREADABLE_NOTICE_PREFIX}.agent-flow/templates (")
        for line in result.stdout.splitlines()
    ), result.stdout
    assert _written(result) == [ASSET_LABEL], "판정을 미뤘는데 기록을 떨어뜨렸다"


# 여기부터는 그 판정을 실제로 소비하는 두 진입점을 태운다. 판정 함수만 고치고 호출부가
# 예전처럼 읽으면 파괴는 그대로다 - 실제로 지켰는지는 install을 돌려야만 보인다. 여기서는
# kit 사본이 아니라 이 checkout을 그대로 태운다: 재현 조건이 kit source가 아니라 설치본
# 쪽(사본 자리가 고갈된 hook)에 있다.
@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_keeps_a_user_edited_hook_when_no_backup_slot_is_free(
    tmp_path: Path, binary: str
) -> None:
    """반증: managed hook은 "내용이 달라도 덮되 사본을 남긴다"는 자리다. 사본을 못

    남겼는데 덮으면 사용자가 고친 hook이 사본 없이 사라진다. 실측(고치기 전): 두
    진입점 모두에서 편집이 kit 판본으로 덮여 사라졌고, `agent-flow-install.mjs`
    쪽은 파괴가 자식 kit install에서 일어나 알림 한 줄도 남지 않았다.

    덮지 않고 남긴 hook은 `hook_integrity`가 run 시작에서 잡아 주고 다음 install이
    다시 시도한다 - 되돌릴 수 없는 쪽은 삭제뿐이다."""
    project = tmp_path / "project"
    project.mkdir()
    _install(KIT_ROOT, project, binary)

    hooks = project / ".agent-flow" / "scripts" / "hooks"
    # 목록에서 이름을 고르지 않는다. 손으로 적으면 hook 하나를 은퇴시킨 날 이 검사가
    # 아무것도 재현하지 못한 채 초록을 낸다.
    target = sorted(path for path in hooks.iterdir() if path.is_file())[0]
    target.write_text("# mine\n", encoding="utf-8")
    _fill_bak_slots(target)

    stdout = _install(KIT_ROOT, project, binary)

    assert target.read_text(encoding="utf-8") == "# mine\n", "사본 없이 사용자 편집을 덮었다"
    label = target.relative_to(project).as_posix()
    assert f"{ASSET_BACKUP_SKIP_NOTICE_PREFIX}{label}" in stdout.splitlines(), stdout


@pytest.mark.parametrize("binary", ["agent-flow-kit.mjs", "agent-flow-install.mjs"])
def test_install_still_upgrades_a_user_edited_hook_when_a_backup_slot_is_free(
    tmp_path: Path, binary: str
) -> None:
    """계약: 사본을 남길 수 있으면 덮는다. 위 검사를 "사용자가 손댄 hook은 영영 안

    덮는다"로 넓히면 kit 개정이 hook에 닿지 않고, `hook_integrity`가 요구하는
    digest 일치가 영구히 깨진 채로 굳는다."""
    project = tmp_path / "project"
    project.mkdir()
    _install(KIT_ROOT, project, binary)

    hooks = project / ".agent-flow" / "scripts" / "hooks"
    target = sorted(path for path in hooks.iterdir() if path.is_file())[0]
    shipped = target.read_text(encoding="utf-8")
    target.write_text("# mine\n", encoding="utf-8")

    _install(KIT_ROOT, project, binary)

    assert target.read_text(encoding="utf-8") == shipped, "kit 개정이 hook에 닿지 않았다"
    assert Path(f"{target}.bak").read_text(encoding="utf-8") == "# mine\n", "사본 없이 덮었다"


# 여기부터는 "지워도 되는가"를 정하는 오라클 자체를 본다. 사본을 바이트로 보존해도
# 판정이 손실적이면 보존은 무의미하다: `toString("utf8")`은 나쁜 바이트마다 U+FFFD를
# 내는 손실 변환이라 서로 다른 바이트열이 한 digest로 접히고, 그 순간 사용자 편집이
# "배포본 그대로"로 오판되어 사본조차 없이 사라진다.
#
# 두 바이트열은 다르지만 UTF-8로 디코딩하면 같은 문자열이 된다.
SHIPPED_BYTES = b"kit\n\xff\n"
USER_EDITED_BYTES = b"kit\n\xfe\n"
FOLDED_TEXT = "kit\n\ufffd\n"

EDITED_LABEL = f".agent-flow/templates/{RETIRED_DIR}/{EDITED_ASSET}"


def _seed_kit_source(kit: Path, content: str, name: str = UNTOUCHED_ASSET) -> Path:
    """kit이 그 자리를 아직 배포하게 만든다. 배포하는 자산은 prune이 아니라
    `syncKitAsset`이 판정한다 - 같은 오라클을 쓰는 다른 자리다."""
    source = kit / "templates" / RETIRED_DIR / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    return source


def test_prune_does_not_fold_different_bytes_into_one_digest(tmp_path: Path) -> None:
    """반증: 기록의 digest를 디코딩한 문자열로 세면 서로 다른 바이트열이 한 값으로
    떨어진다. 그러면 사용자가 고친 자산이 "우리가 쓴 그대로"로 읽혀 사본 없이 지워지고,
    되돌릴 곳이 남지 않는다 - 파괴를 결정하는 오라클은 파괴만큼 정확해야 한다.

    기록에는 우리가 심은 바이트(`SHIPPED_BYTES`)의 접힌 값이 적혀 있고 파일에는 사용자가
    고친 다른 바이트가 있다. 접어 세면 둘이 같은 digest다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset_bytes(root, kit, USER_EDITED_BYTES)
    assert _digest(FOLDED_TEXT) == hashlib.sha256(
        SHIPPED_BYTES.decode("utf-8", "replace").encode("utf-8")
    ).hexdigest(), "두 바이트열이 같은 값으로 접히지 않으면 이 검사는 아무것도 반증하지 못한다"
    recorded = {ASSET_LABEL: _digest(FOLDED_TEXT)}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert backup.read_bytes() == USER_EDITED_BYTES, "사용자 편집을 사본 없이 지웠다"
    assert not target.exists()
    assert (
        f"{PRUNE_NOTICE_PREFIX}{ASSET_LABEL} (backup: {backup.relative_to(root).as_posix()})"
        in result.stdout.splitlines()
    ), result.stdout


def test_asset_upgrade_does_not_fold_different_bytes_into_one_digest(tmp_path: Path) -> None:
    """반증: 같은 오라클이 `syncKitAsset`에서도 "우리 것"을 정하고, 그 답이 참이면
    사본 없이 덮는다. 접어 세면 사용자가 고친 자산이 kit 판본으로 즉시 사라진다.

    이것이 예전 기록(문자열 hash)을 가진 설치본에서 유효한 UTF-8이 아닌 자산이 겪는
    자리다: 바이트로 세면 불일치이므로 사용자 편집으로 보아 그대로 둔다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset_bytes(root, kit, USER_EDITED_BYTES)
    _seed_kit_source(kit, "kit v2\n")
    recorded = {ASSET_LABEL: _digest(FOLDED_TEXT)}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert target.read_bytes() == USER_EDITED_BYTES, "사본도 없이 사용자 편집을 덮었다"
    assert not backup.exists(), "덮지 않았는데 사본을 남겼다"
    assert f"{SKILL_SKIP_NOTICE_PREFIX}{ASSET_LABEL}" in result.stdout.splitlines(), result.stdout
    # 기록은 들고 간다. 우리가 쓰지 않은 내용의 hash를 남기면 그것이 곧 "우리가 쓴 것"이 된다.
    assert _written(result) == [ASSET_LABEL]


def test_a_legacy_text_digest_still_identifies_a_valid_utf8_asset(tmp_path: Path) -> None:
    """계약(기록 호환): 이미 깔린 설치본의 기록은 문자열 hash로 적혀 있다. 유효한
    UTF-8은 디코딩-재인코딩이 무손실이라 그 값이 바이트 hash와 **같다** - 그래서 기준을
    바이트로 바꿔도 그 설치본들은 아무 일도 겪지 않는다.

    여기서 값이 갈리면 두 가지가 한꺼번에 일어난다: 은퇴 자산 전수가 한 번 사본되고
    (`.agent-flow/backups/` 폭증), 살아 있는 자산은 사용자 편집으로 오판되어 kit 개정이
    영영 닿지 않는다. 그 둘을 한 판에서 함께 본다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    # 은퇴 자산: 우리가 쓴 그대로다. 사본 없이 지워야 한다.
    retired, backup = _seed_asset(root, kit, "한글 kit\n")
    # 살아 있는 자산: kit이 개정했다. 예전 기록이 맞아야 갱신된다.
    live = root / ".agent-flow" / "templates" / RETIRED_DIR / EDITED_ASSET
    live.write_text("한글 v1\n", encoding="utf-8")
    _seed_kit_source(kit, "한글 v2\n", EDITED_ASSET)
    recorded = {ASSET_LABEL: _digest("한글 kit\n"), EDITED_LABEL: _digest("한글 v1\n")}

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert not retired.exists(), "예전 기록이 안 맞아 은퇴 자산을 지우지 못했다"
    assert not backup.exists(), "우리가 쓴 그대로인데 사본을 남겼다 - 설치본 전수가 한 번 사본된다"
    assert live.read_text(encoding="utf-8") == "한글 v2\n", "예전 기록이 사용자 편집으로 오판되어 자산이 굳었다"
    assert f"{ASSET_UPGRADE_NOTICE_PREFIX}{EDITED_LABEL}" in result.stdout.splitlines(), result.stdout
    assert not any(
        line.startswith(f"{ASSET_BACKUP_NOTICE_PREFIX}") for line in result.stdout.splitlines()
    ), result.stdout
    # 갱신한 자산의 기록은 새 기준으로 다시 적힌다 - 예전 값은 한 판 뒤에 사라진다.
    assert _written(result) == [EDITED_LABEL]
    assert f"{PRUNE_NOTICE_PREFIX}{ASSET_LABEL}" in result.stdout.splitlines(), result.stdout


# 사본이 디스크에 닿기 전에 원본이 사라지는 창을 본다. 새 이름에 쓰는 사본이라 rename
# 원자성이 아니라 fsync가 질문이다 - `fsyncSync`와 `rmSync`의 호출 순서로 관측한다.
BACKUP_DURABILITY_PROBE = """
import fs from "node:fs";
import { syncRecordedKitAssets } from %(module)s;

const trace = [];
const realFsync = fs.fsyncSync;
fs.fsyncSync = (descriptor) => {
  trace.push("fsync");
  return realFsync(descriptor);
};
const realRm = fs.rmSync;
fs.rmSync = (target, options) => {
  trace.push(`rm:${target}`);
  return realRm(target, options);
};

const written = new Map();
syncRecordedKitAssets(%(root)s, %(kit)s, new Map(Object.entries(%(recorded)s)), written);
process.stdout.write(JSON.stringify({ trace, recorded: [...written.keys()].sort() }));
"""


def test_prune_backup_reaches_the_disk_before_the_original_is_removed(tmp_path: Path) -> None:
    """반증: 새 사본을 `writeFileSync`로만 쓰면 그 바이트는 페이지 캐시에만 있다.
    호출부는 그 답을 받고 곧 원본을 `rmSync`로 지우므로, 그 창에서 전원이 끊기면 원본은
    사라졌는데 사본은 비어 있거나 반쪽이다 - 사본을 남기는 이유가 사라진다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset(root, kit, "kit\nmine\n")
    probe = tmp_path / "durability.mjs"
    probe.write_text(
        BACKUP_DURABILITY_PROBE % {
            "module": json.dumps(SHARED_MODULE.as_uri()),
            "root": json.dumps(str(root)),
            "kit": json.dumps(str(kit)),
            "recorded": json.dumps({ASSET_LABEL: _digest("kit\n")}),
        },
        encoding="utf-8",
    )
    result = subprocess.run(
        (_node(), str(probe)),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    trace = json.loads(result.stdout.splitlines()[-1])["trace"]
    assert f"rm:{target}" in trace, trace
    assert "fsync" in trace[: trace.index(f"rm:{target}")], (
        f"사본을 디스크에 내려보내지 않고 원본을 지웠다: {trace}"
    )
    assert backup.read_text(encoding="utf-8") == "kit\nmine\n"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root는 권한 검사를 통과한다")
def test_prune_does_not_report_a_backup_failure_as_unreadable(tmp_path: Path) -> None:
    """반증: stat·read·사본 쓰기를 한 `catch`로 감싸면 `mkdirSync` EACCES나
    `writeFileSync` ENOSPC까지 "prune skipped (unreadable)"로 보고된다. 사용자는 읽을 수
    없는 자산을 찾아 헤매고 진짜 원인(`.agent-flow/backups/` 권한)은 가려진다. 통과
    효과는 같아도 알림은 사실이어야 한다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    target, backup = _seed_asset(root, kit, "kit\nmine\n")
    # 사본 자리를 쓸 수 없다. 훑는 것(`lstat`)은 되고 만드는 것만 EACCES다.
    backup.parent.chmod(0o500)
    try:
        result = _probe(tmp_path, root, kit, {ASSET_LABEL: _digest("kit\n")})
    finally:
        backup.parent.chmod(0o700)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "kit\nmine\n", "사본을 못 만들었는데 지웠다"
    lines = result.stdout.splitlines()
    assert any(
        line.startswith(f"{PRUNE_BACKUP_FAILED_NOTICE_PREFIX}{ASSET_LABEL} (") for line in lines
    ), result.stdout
    assert not any(
        line.startswith(f"{PRUNE_UNREADABLE_NOTICE_PREFIX}{ASSET_LABEL}") for line in lines
    ), "사본 실패를 읽기 실패로 보고했다"
    assert _written(result) == [ASSET_LABEL], "판정을 미뤘는데 기록을 떨어뜨렸다"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root는 권한 검사를 통과한다")
def test_one_failed_asset_backup_does_not_stop_the_install(tmp_path: Path) -> None:
    """반증: sync 쪽 사본은 무방어였다. `writeKitAssetBackup`은 `mkdirSync`·
    `writeFileSync`·부모 체인 `lstat`으로 자유롭게 던지고, 그것이 그대로 올라가면
    `syncRecordedKitAssets`가 죽어 호출부의 `writeKitAssetRecord`에 닿지 못한다 - 자산
    하나의 권한 문제가 나머지 자산의 기록까지 지운다. prune 쪽에 이미 고친 사건이다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    # 기록 없는 `.agent-flow/` 자산이다. 사본을 남기고 한 번 갱신하는 자리라 사본을 탄다.
    target, backup = _seed_asset(root, kit, "mine\n")
    _seed_kit_source(kit, "kit v2\n")
    # 같은 트리의 형제는 kit 판본 그대로다. 이 기록이 살아남는지가 관측 대상이다.
    same = root / ".agent-flow" / "templates" / RETIRED_DIR / EDITED_ASSET
    same.write_text("same\n", encoding="utf-8")
    _seed_kit_source(kit, "same\n", EDITED_ASSET)
    backup.parent.chmod(0o500)
    try:
        result = _probe(tmp_path, root, kit, {})
    finally:
        backup.parent.chmod(0o700)

    assert result.returncode == 0, f"자산 하나의 사본 실패가 install 전체를 죽였다: {result.stderr}"
    assert target.read_text(encoding="utf-8") == "mine\n", "사본을 못 만들었는데 덮었다"
    assert any(
        line.startswith(f"{ASSET_BACKUP_FAILED_NOTICE_PREFIX}{ASSET_LABEL} (")
        for line in result.stdout.splitlines()
    ), result.stdout
    assert _written(result) == [EDITED_LABEL], "사본 실패로 형제 자산의 기록까지 잃었다"


# 은퇴 판정이 기록을 들고 갈지 떨어뜨릴지가 분기마다 갈린다. 지연 분기는 전부 들고
# 가는데(`written.set`) 선언된 자리 밖 label만 떨어뜨린다 - 그 비대칭을 고정한다.
SKILL_MANIFEST_LABEL = f".agent-flow/skills/{RETIRED_SKILL}/SKILL.md"
DEFERRED_LABEL = f"{MISSING_TREE_DEST}/scoped.md"


def test_prune_keeps_deferred_records_but_drops_out_of_scope_ones(tmp_path: Path) -> None:
    """계약: 판정을 미룬 자산은 기록을 들고 가고, 선언된 자리 밖 label은 떨어뜨린다.

    앞은 "다음 install이 다시 판정해야 한다"라서 근거가 필요하고, 뒤에는 다시 판정할
    자산이 없다: 자리 밖 label은 `syncKitAsset`이 쓰지도 않고 prune이 지우지도 않으므로
    들고 가면 손으로 심은 임의 경로가 기록에 영주한다. `SKILL.md`는 index hash가 오라클
    이라 자산 기록에서 빠지는 자리이고, 그것을 들고 가면 한 파일에 판정자가 둘이 된다."""
    root = tmp_path / "project"
    kit = tmp_path / "kit"
    root.mkdir()
    kit.mkdir()
    manifest, _ = _seed_bundled_skill(root, kit, kit_ships_skill=True)
    # kit에 `.Codex/rules/context`가 없다 - 원본 실종이라 판정을 미루는 자리다.
    deferred = root.joinpath(*MISSING_TREE_DEST.split("/"), "scoped.md")
    deferred.parent.mkdir(parents=True)
    deferred.write_text("kit\n", encoding="utf-8")
    recorded = {
        SKILL_MANIFEST_LABEL: _digest(manifest.read_text(encoding="utf-8")),
        DEFERRED_LABEL: _digest("kit\n"),
    }

    result = _probe(tmp_path, root, kit, recorded)
    assert result.returncode == 0, result.stderr

    assert manifest.is_file(), "다른 오라클이 판정하는 파일을 자산 prune이 지웠다"
    assert deferred.is_file(), "판정을 미뤘는데 지웠다"
    assert _written(result) == [DEFERRED_LABEL], (
        "미룬 판정의 근거를 잃었거나, 자리 밖 label을 기록에 영주시켰다"
    )


# 같은 질문을 사본을 남기고 원본을 없애는 나머지 세 자리에도 던진다. 세 함수는 각각
# 다른 이름의 사본을 만들지만 계약은 하나다 - 사본이 디스크에 닿기 전에 원본을 바꾸지
# 않는다. `copyFileSync`는 대상을 먼저 비우고 fsync를 하지 않아 그 계약을 깬다.
LEGACY_DURABILITY_PROBE = """
import fs from "node:fs";
import %(imports)s from %(module)s;

const trace = [];
const realFsync = fs.fsyncSync;
fs.fsyncSync = (descriptor) => {
  trace.push("fsync");
  return realFsync(descriptor);
};
for (const name of ["rmSync", "writeFileSync", "copyFileSync"]) {
  const real = fs[name];
  fs[name] = (first, ...rest) => {
    trace.push(`${name}:${first}`);
    return real(first, ...rest);
  };
}

%(call)s;
process.stdout.write(JSON.stringify({ trace }));
"""


def _durability_trace(tmp_path: Path, *, imports: str, call: str) -> list[str]:
    probe = tmp_path / f"durability-{abs(hash(call)) % 10**8}.mjs"
    probe.write_text(
        LEGACY_DURABILITY_PROBE
        % {
            "module": json.dumps(SHARED_MODULE.as_uri()),
            "imports": imports,
            "call": call,
        },
        encoding="utf-8",
    )
    result = subprocess.run(
        (_node(), str(probe)),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])["trace"]


def _assert_backup_precedes_destruction(trace: list[str], *, destroys: str) -> None:
    hit = next((entry for entry in trace if entry.startswith(destroys)), None)
    assert hit is not None, f"원본을 건드리지 않았다: {trace}"
    assert "fsync" in trace[: trace.index(hit)], (
        f"사본을 디스크에 내려보내지 않고 원본을 없앴다: {trace}"
    )


def test_backup_if_different_lands_before_the_caller_overwrites(tmp_path: Path) -> None:
    """반증: `backupIfDifferent`가 `copyFileSync`로만 사본을 두면, 호출부가 곧 원본을
    덮으므로 그 창에서 전원이 끊기면 사용자가 고친 내용이 양쪽에서 사라진다."""
    root = tmp_path / "project"
    target = root / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")

    trace = _durability_trace(
        tmp_path,
        imports="{ backupIfDifferent }",
        call=(
            f"const verdict = backupIfDifferent({json.dumps(str(root))}, "
            f"{json.dumps(str(target))}, \"kit\\n\");\n"
            "if (verdict.safeToWrite) fs.writeFileSync("
            f"{json.dumps(str(target))}, \"kit\\n\");"
        ),
    )

    _assert_backup_precedes_destruction(trace, destroys=f"writeFileSync:{target}")
    assert (root / ".claude" / "settings.json.bak").read_text(encoding="utf-8") == "mine\n"


def test_retired_managed_script_backup_lands_before_removal(tmp_path: Path) -> None:
    """반증: 은퇴 스크립트 사본도 같은 창을 갖는다 - 사본을 쓴 직후 원본을 지운다."""
    root = tmp_path / "project"
    target = root / ".agent-flow" / "scripts" / "check-context-docs.mjs"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")

    trace = _durability_trace(
        tmp_path,
        imports="{ pruneRetiredManagedScripts }",
        call=f"pruneRetiredManagedScripts({json.dumps(str(root))})",
    )

    _assert_backup_precedes_destruction(trace, destroys=f"rmSync:{target}")
    assert Path(f"{target}.removed").read_text(encoding="utf-8") == "mine\n"
