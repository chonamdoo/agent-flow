from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent_flow.artifact import create_run
from agent_flow.core.host_write_boundary import (
    HostWriteBoundaryError,
    assert_adoption_allowed,
    host_write_boundary_violation,
    record_host_checkout_binding,
)
from agent_flow.core.worktree_isolation import (
    LEADER_SNAPSHOT_VERSION,
    adopted_record_path,
)
from agent_flow.core.worktrees import (
    adopt_worktree,
    create_worktree,
    plan_worktree,
    list_registered_worktrees,
    worktree_runtime_root,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fast_forward_leader(root: Path, tmp_path: Path) -> tuple[str, str]:
    """leader를 같은 브랜치에서 clean fast-forward한다. 평범한 `git pull`의 모양이다.

    feeder checkout은 leader 밖에 만들고 바로 지운다 — 남겨 두면 등록 worktree가
    하나 늘어 판정 대상이 달라진다.
    """
    old = _git_out("rev-parse", "HEAD", cwd=root)
    feeder = tmp_path / "feeder"
    _git("worktree", "add", "-b", "feeder", str(feeder), "main", cwd=root)
    (feeder / "pulled.txt").write_text("pulled\n", encoding="utf-8")
    _git("add", "pulled.txt", cwd=feeder)
    _git("commit", "-m", "upstream commit", cwd=feeder)
    _git("merge", "--ff-only", "feeder", cwd=root)
    _git("worktree", "remove", "--force", str(feeder), cwd=root)
    return old, _git_out("rev-parse", "HEAD", cwd=root)



def _setup(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)

    statuses = []
    runs = []
    for name in ("first", "second"):
        status = create_worktree(
            root=root,
            plan=plan_worktree(root=root, name=name),
        )
        runtime_root = worktree_runtime_root(root=root, name=status.name)
        run_dir = create_run(
            runtime_root,
            "default",
            f"task-{name}",
            checkout_identity=f"worktree:{status.name}",
            checkout_registration_identity=status.registration_identity,
        )
        statuses.append(status)
        runs.append(run_dir)
    return root, statuses, runs


def _install_boundary_hooks(root: Path) -> Path:
    kit_root = Path(__file__).resolve().parents[1]
    installed = root / ".agent-flow"
    hooks = installed / "scripts" / "hooks"
    runtime = installed / "runtime" / "python"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        kit_root / "scripts" / "hooks" / "guard-host-worktree.sh",
        hooks / "guard-host-worktree.sh",
    )
    shutil.copy2(
        kit_root / "scripts" / "hooks" / "bind-host-worktree.py",
        hooks / "bind-host-worktree.py",
    )
    shutil.copytree(
        kit_root / "src" / "agent_flow",
        runtime / "agent_flow",
        dirs_exist_ok=True,
    )
    return hooks


def _status_payload(root: Path, status, run_dir: Path, session: str = "session-1"):
    next_command = (
        f"agent-flow continue --root {root} --worktree {status.name}"
    )
    return {
        "tool_name": "bash",
        "tool_input": {
            "command": (
                f"agent-flow status --root {root} --worktree {status.name}"
            )
        },
        "session_id": session,
        "exit_code": 0,
        "output": "status_json: "
        + json.dumps(
            {
                "status": "awaiting_host",
                "run": f"default/{run_dir.name}",
                "next_command": next_command,
            }
        ),
        "cwd": str(root),
    }


def _write_payload(
    path: Path | str,
    *,
    session: str = "session-1",
    host_cwd: Path | None = None,
) -> dict:
    payload = {
        "tool_name": "write",
        "tool_input": {"path": str(path), "content": "changed\n"},
        "session_id": session,
    }
    if host_cwd is not None:
        payload["cwd"] = str(host_cwd)
    return payload


def _command_payload(
    command: str,
    *,
    cwd: Path | None = None,
    session: str = "session-1",
    host_cwd: Path | None = None,
) -> dict:
    tool_input = {"command": command}
    if cwd is not None:
        tool_input["cwd"] = str(cwd)
    payload = {
        "tool_name": "bash",
        "tool_input": tool_input,
        "session_id": session,
    }
    if host_cwd is not None:
        payload["cwd"] = str(host_cwd)
    return payload


def test_host_binding_allows_only_its_checkout_and_runtime(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    binding = record_host_checkout_binding(
        _status_payload(root, first, runs[0]),
        root,
    )

    assert binding is not None and binding.is_file()
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None
    assert host_write_boundary_violation(
        _write_payload(runs[0] / "review.md"),
        root,
    ) is None

    leader_violation = host_write_boundary_violation(
        _write_payload(root / "leaked.py"),
        root,
    )
    sibling_violation = host_write_boundary_violation(
        _write_payload(second.path / "leaked.py"),
        root,
    )
    assert leader_violation is not None and "outside the bound worktree" in leader_violation
    assert sibling_violation is not None and "outside the bound worktree" in sibling_violation


def test_destructive_detection_sees_wrappers_and_splits_conditional_forms():
    """반증: 이름만 보면 조회를 막고, wrapper를 안 벗기면 파괴를 놓친다.

    셸 판정은 두 개뿐이다 — 보호 경로 리터럴(파서 불필요)과 이 파괴 목록. 리터럴이
    등장하지 않는 형태(`cd <leader> && env rm -rf .`)에서는 이 목록만 남으므로,
    `env`나 `VAR=1` 뒤에 숨은 이름을 놓치면 되돌릴 수 없는 명령이 그대로 통과한다.

    반대로 `git worktree`/`find`/`chmod`는 같은 이름이 조회로도 쓰인다. 이름만 보고
    막으면 `git worktree list`, `find -name`, `chmod +x`가 함께 죽는다. 그래서 이
    셋만 flag로 갈리고, 그 분기가 이 테스트의 본론이다.
    """
    from agent_flow.core.host_write_boundary import _destructive_segments

    assert _destructive_segments("env rm -rf /tmp/x")
    assert _destructive_segments("FOO=1 nohup rm -rf /tmp/y")
    assert _destructive_segments("echo hi && rm -rf /tmp/z")
    assert _destructive_segments("git -C /x checkout -- .")
    assert _destructive_segments("git clean -fd")
    assert _destructive_segments("git worktree remove /x")
    assert _destructive_segments("git -C /r worktree move /a /b")
    assert _destructive_segments("find . -name x -delete")
    assert _destructive_segments("find . -exec rm {} +")
    assert _destructive_segments("find . -execdir shred {} +")
    assert _destructive_segments("chmod -R 755 /x")
    assert _destructive_segments("chmod --recursive 700 .")

    # 파괴가 아닌 것을 목록에 넣는 것은 "셸이 무엇을 쓰는지 맞히기"로 되돌아가는
    # 길이다. 그 방향은 예외를 끝없이 만들고, 무엇이 막히는지 말할 수 없게 한다.
    assert not _destructive_segments("cp /a/one /b/two")
    assert not _destructive_segments("printf x > /tmp/q")
    assert not _destructive_segments("git commit -m 'rm stuff'")
    assert not _destructive_segments("git worktree list")
    assert not _destructive_segments("git worktree add /a -b feat/a")
    # prune은 이미 사라진 등록만 정리한다. 되돌릴 작업물이 없다.
    assert not _destructive_segments("git worktree prune")
    assert not _destructive_segments("find . -name '*.kt'")
    # `-exec`는 뒤에 오는 명령으로 갈린다. 조건 없이 막으면 이 흔한 조회가 죽는다.
    assert not _destructive_segments("find . -name '*.kt' -exec grep -l x {} +")
    assert not _destructive_segments("chmod +x script.sh")
    # chmod의 `-r`은 재귀가 아니라 read 권한 제거다. 재귀로 읽으면 오탐이다.
    assert not _destructive_segments("chmod -r file")


def test_destructive_command_is_blocked_before_it_reaches_another_checkout(
    tmp_path: Path,
):
    """불변: tripwire는 탐지 전용이라 `rm -rf`는 사후에 잡아도 되돌릴 수 없다.

    보호 경로를 **품는** 경로까지 본다(`rm -rf <leader의 부모>`). 반대로 자기
    checkout 안에서 도는 같은 명령은 통과해야 한다 — 그러지 않으면 worktree에서
    빌드 산출물 정리조차 못 한다.
    """
    root, _statuses, _runs = _setup(tmp_path)
    mine = tmp_path / "mine"
    _git("worktree", "add", "-b", "feat/destructive", str(mine), cwd=root)

    def judge(command: str, cwd: Path) -> str | None:
        return host_write_boundary_violation(
            _command_payload(command, cwd=cwd, session="mine"), root
        )

    assert judge("rm -rf build", mine) is None
    assert judge("git reset --hard HEAD~1", mine) is None

    ancestor = judge(f"rm -rf {tmp_path}", mine)
    leader_cwd = judge("rm -rf .", root)

    assert ancestor is not None
    assert "cannot be detected after the fact" in ancestor
    assert leader_cwd is not None


def test_shell_write_target_obeys_the_same_rule_as_the_write_tool(tmp_path: Path):
    """반증: `Write`가 막는 자리를 `bash`가 열어 주면 경계는 없는 것과 같다.

    대상은 idle 형제 checkout이다. 두 경로가 같은 대상을 같은 결론으로 판정하는지,
    그리고 무관한 자리는 둘 다 통과하는지 대조한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    (runs[1] / "active").unlink()

    leaked = second.path / "leaked.txt"
    write_violation = host_write_boundary_violation(_write_payload(leaked), root)
    shell_violation = host_write_boundary_violation(
        _command_payload(f"touch {leaked}", cwd=first.path),
        root,
    )
    assert write_violation is not None
    assert shell_violation is not None, "bash가 Write와 달리 형제 checkout 쓰기를 통과시켰다"
    assert "outside the bound worktree" in shell_violation

    # 리다이렉션도 같은 대상이다. 명령 이름만 보면 이 형태를 놓친다.
    redirect_violation = host_write_boundary_violation(
        _command_payload(f"printf x > {leaked}", cwd=first.path),
        root,
    )
    assert redirect_violation is not None

    # checkout 어디에도 속하지 않는 자리는 두 경로 모두 통과한다. leader가
    # `~/Downloads` 같은 폴더 안에 있을 때 무관한 파일까지 막지 않는다.
    unrelated = tmp_path / "outside.txt"
    assert host_write_boundary_violation(_write_payload(unrelated), root) is None
    assert host_write_boundary_violation(
        _command_payload(f"touch {unrelated}", cwd=first.path),
        root,
    ) is None


def test_shell_reads_outside_the_worktree_stay_allowed(tmp_path: Path):
    """불변: 쓰기 경계가 읽기까지 막으면 인터프리터·도구 호출이 전부 죽는다.

    무관한 자리는 읽기도 쓰기도 통과한다. 변이 케이스로 형제 checkout을 짝지어
    대조한다 — 그쪽은 읽기든 쓰기든 거부다. 그래야 "무관한 자리 허용"이 경계를
    통째로 끈 결과가 아님이 증명된다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    (runs[1] / "active").unlink()

    tool = tmp_path / "tool.py"
    tool.write_text("print('hi')\n", encoding="utf-8")
    assert host_write_boundary_violation(
        _command_payload(f"python3 {tool} --check", cwd=first.path),
        root,
    ) is None

    assert host_write_boundary_violation(
        _command_payload(f"truncate -s 0 {tool}", cwd=first.path),
        root,
    ) is None
    assert host_write_boundary_violation(
        _command_payload(f"python3 {second.path / 'README.md'} --check", cwd=first.path),
        root,
    ) is not None
    assert host_write_boundary_violation(
        _command_payload(f"git -C {second.path} checkout -- .", cwd=first.path),
        root,
    ) is not None


def test_shell_path_candidates_come_from_path_text_not_shell_syntax(tmp_path: Path):
    """반증: 셸 문법을 반쯤 해석하면 명령이 건드리지도 않는 경로가 위반이 된다.

    `2>/dev/null`은 fd 리다이렉션이다. 이 조각을 경로로 접어 cwd에 붙이면
    `<cwd>/2>/dev/null`이라는 없는 경로가 생기고, cwd가 보호 경로면 무해한 빌드가
    막힌다. 실제로 그 상태였다.

    같은 테스트에 세 방향을 함께 둔다. 후보 추출을 느슨하게 만들어 오탐을 없애면
    리다이렉션의 **실제** 대상이 함께 열리고, 반대로 조여서 판정 불가까지 막으면
    인용부호 하나 어긋난 명령이 죽는다. 대상은 상대 경로로 쓴다 — 절대 경로는
    `_command_literal_violation`이 후보 추출과 무관하게 잡아서, 이 규칙을 검증하지
    못한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses

    redirect_noise = host_write_boundary_violation(
        _command_payload("swift build 2>/dev/null", cwd=root),
        root,
    )
    redirect_target = host_write_boundary_violation(
        _command_payload("printf x 2> ./leaked.py", cwd=root),
        root,
    )

    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    unterminated_relative = host_write_boundary_violation(
        _command_payload(
            f"python3 -c \"open('../{second.path.name}/leaked.py', 'w')",
            cwd=first.path,
        ),
        root,
    )
    unterminated_absolute = host_write_boundary_violation(
        _command_payload(
            f"python3 -c \"open('{second.path / 'leaked.py'}', 'w')",
            cwd=first.path,
        ),
        root,
    )

    assert redirect_noise is None
    assert redirect_target is not None
    # 판정 불가는 차단이 아니다(AGENTS.md). 인용이 닫히지 않은 명령의 상대 경로는
    # 통과하고, 같은 명령이 보호 경로를 절대 경로로 적으면 리터럴 검사가 잡는다.
    assert unterminated_relative is None
    assert unterminated_absolute is not None


def test_quoted_and_escaped_path_words_stay_whole(tmp_path: Path):
    """반증: 인용을 무시하고 문자로만 자르면 공백이 든 경로가 판정을 빠져나간다.

    셸에서 ``'../a b/x'``는 낱말 하나다. 공백에서 자르면 ``../a``와 ``b/x``가 되고,
    둘 중 어느 것도 보호 경로 안으로 떨어지지 않아 상대 경로 참조가 통째로 열린다.
    역슬래시로 이스케이프한 형태도 같은 낱말이다.

    반대쪽도 같은 규칙에서 나온다: 인용된 ``'2>/dev/null'``은 리다이렉션이 아니라
    ``2>`` 디렉터리 아래의 이름이므로, 그 자리가 보호 경로면 막혀야 한다. 그리고
    ``"a\\ b"``는 ``a b``가 아니다 — `"` 안의 역슬래시는 POSIX가 정한 몇 글자
    앞에서만 이스케이프이므로, 이름이 다른 그 경로는 막을 근거가 없다.
    """
    spaced = tmp_path / "a b"
    spaced.mkdir()
    root, statuses, runs = _setup(spaced)
    first = statuses[0]

    quoted_operator_name = host_write_boundary_violation(
        _command_payload("touch '2>/dev/null'", cwd=root),
        root,
    )
    dynamic_word = host_write_boundary_violation(
        _command_payload("cat $ROOT/src/main.py", cwd=root),
        root,
    )

    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    relative = os.path.relpath(root / "README.md", first.path)
    # 이 테스트가 검증하는 것이 공백이므로, 경로에 공백이 있다는 사실을 우연에
    # 맡기지 않는다.
    assert " " in relative
    quoted_space = host_write_boundary_violation(
        _command_payload(f"cat '{relative}'", cwd=first.path),
        root,
    )
    escaped_space = host_write_boundary_violation(
        _command_payload(f"cat {relative.replace(' ', chr(92) + ' ')}", cwd=first.path),
        root,
    )
    literal_backslash = host_write_boundary_violation(
        _command_payload(
            f'cat "{relative.replace(" ", chr(92) + " ")}"',
            cwd=first.path,
        ),
        root,
    )
    escaped = relative.replace(" ", chr(92) + " ")
    head, _, tail = escaped.partition("/")
    continued = host_write_boundary_violation(
        _command_payload(f"cat {head}\\\n/{tail}", cwd=first.path),
        root,
    )
    quoted_assignment_name = host_write_boundary_violation(
        _command_payload(f"touch 'x={relative}'", cwd=first.path),
        root,
    )

    assert quoted_operator_name is not None
    assert quoted_space is not None
    assert escaped_space is not None
    assert literal_backslash is None
    # 줄 이음은 셸이 지운다. 지우지 않으면 이어진 경로가 판정을 빠져나간다.
    assert continued is not None
    # 인용 안의 `=`는 문법이 아니라 이름이다. 다시 자르면 없는 경로가 만들어진다.
    assert quoted_assignment_name is None
    # 값이 실행 시점에 정해지는 낱말은 정적으로 판정하지 않는다(tripwire 담당).
    assert dynamic_word is None


def test_dynamic_words_keep_their_static_prefix(tmp_path: Path):
    """반증: 낱말에 ``$``가 있다고 통째로 버리면 이미 정해진 참조를 놓친다.

    ``../feat-second/$name``은 이름을 몰라도 어느 checkout으로 가는지 정해져 있다.
    반대로 치환 **뒤**에 붙은 조각은 실제 인수에 그대로 나타나지 않는다 —
    ``$(printf p)../feat-second/f``의 인수는 ``p../feat-second/f``이므로, 거기서
    잘라낸 ``../feat-second/f``는 아무도 건드리지 않는 자리다. 두 방향을 함께 두어야
    한 쪽을 고치다 다른 쪽이 열리는 것을 막는다.

    ``$'...'``는 치환이 아니라 리터럴 인용이고, 경로 이름 안의 ``=``는 문법이 아니다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    sibling = second.path.name

    static_prefix = host_write_boundary_violation(
        _command_payload(f"cp x ../{sibling}/$name", cwd=first.path),
        root,
    )
    substitution_suffix = host_write_boundary_violation(
        _command_payload(f"printf %s $(printf p)../{sibling}/f", cwd=first.path),
        root,
    )
    ansi_c_literal = host_write_boundary_violation(
        _command_payload(f"touch $'../{sibling}/leaked.py'", cwd=first.path),
        root,
    )
    equals_in_name = host_write_boundary_violation(
        _command_payload(f"cat ../{sibling}=x/f", cwd=first.path),
        root,
    )
    unquoted_assignment_name = host_write_boundary_violation(
        _command_payload(f"touch x=../{sibling}/file", cwd=first.path),
        root,
    )
    brace_expansion = host_write_boundary_violation(
        _command_payload(f"cat ${{x:-foo>../{sibling}/file}}", cwd=first.path),
        root,
    )
    flag_value = host_write_boundary_violation(
        _command_payload(f"cmd --out=../{sibling}/f", cwd=first.path),
        root,
    )
    assignment_value = host_write_boundary_violation(
        _command_payload(f"VAR=../{sibling}/f cmd", cwd=first.path),
        root,
    )
    assignment_on_second_line = host_write_boundary_violation(
        _command_payload(f"echo hi\nVAR=../{sibling}/f cmd", cwd=first.path),
        root,
    )
    empty_quote_keeps_position = host_write_boundary_violation(
        _command_payload(f'cd ""; cat ../{sibling}/f', cwd=first.path),
        root,
    )
    substitution_body = host_write_boundary_violation(
        _command_payload(f"echo $(touch ../{sibling}/leaked.py)", cwd=first.path),
        root,
    )
    comment_text = host_write_boundary_violation(
        _command_payload(f"echo ok # ../{sibling}/file", cwd=first.path),
        root,
    )
    keyword_assignment = host_write_boundary_violation(
        _command_payload(
            f"if VAR=../{sibling}/f cmd; then echo ok; fi",
            cwd=first.path,
        ),
        root,
    )
    # 중첩이 깊은 치환 한 줄로 `RecursionError`가 나가면 훅이 통과로 처리되어 경계가
    # 통째로 열린다. 판정이 비어도 되지만 예외는 안 된다.
    deep_nesting = host_write_boundary_violation(
        _command_payload(
            "echo " + "$(" * 200 + f"touch ../{sibling}/f" + ")" * 200,
            cwd=first.path,
        ),
        root,
    )
    heredoc_body = host_write_boundary_violation(
        _command_payload(
            f"python3 - <<'PY'\nopen('../{sibling}/f','w')\nPY\n",
            cwd=first.path,
        ),
        root,
    )
    heredoc_redirect = host_write_boundary_violation(
        _command_payload(
            f"cat <<EOF > ../{sibling}/out\nx\nEOF\n",
            cwd=first.path,
        ),
        root,
    )
    case_subject = host_write_boundary_violation(
        _command_payload(f"case x=../{sibling}/f in *) :; esac", cwd=first.path),
        root,
    )
    redirect_after_dynamic = host_write_boundary_violation(
        _command_payload(f"printf x $name>../{sibling}/leaked.py", cwd=first.path),
        root,
    )
    separator_after_dynamic = host_write_boundary_violation(
        _command_payload(f"echo $x;cat ../{sibling}/f", cwd=first.path),
        root,
    )

    assert static_prefix is not None
    assert substitution_suffix is None
    assert ansi_c_literal is not None
    # `../feat-second=x`는 형제 checkout이 아니다. `=`에서 자르면 그 자리가 된다.
    assert equals_in_name is None
    # 셸은 `=`에서 인수를 자르지 않는다. 자르면 실제 인수에 없는 경로가 생긴다.
    assert unquoted_assignment_name is None
    # `${...}`는 낱말 하나다. 안의 `>`를 연산자로 읽으면 없는 경로가 생긴다.
    assert brace_expansion is None
    # 반대로 `--flag=`/`VAR=`의 값은 실제 경로다. 접두사를 떼고 판정해야 한다.
    assert flag_value is not None
    assert assignment_value is not None
    # 줄바꿈 다음도 명령의 시작이다. 아니면 둘째 줄의 대입이 인수로 읽힌다.
    assert assignment_on_second_line is not None
    # 빈 인용도 인수 하나다. 자리를 잃으면 `cd`가 다음 낱말을 대상으로 삼는다.
    assert empty_quote_keeps_position is not None
    # 동적인 부분은 공백이 아니라 연산자에서도 끝난다. 셸이 거기서 낱말을 끊으므로
    # 그 뒤의 리다이렉션 대상과 다음 명령은 정적이다.
    assert redirect_after_dynamic is not None
    assert separator_after_dynamic is not None
    # 치환 안의 명령은 실제로 실행된다. 통째로 건너뛰면 그 쓰기가 판정에서 빠진다.
    assert substitution_body is not None
    # 주석은 인수가 아니다. 주석 글자로 위반을 만들면 무해한 명령이 막힌다.
    assert comment_text is None
    # 예약어는 명령 이름이 아니다. `if` 뒤도 대입 자리다.
    assert keyword_assignment is not None
    assert deep_nesting is None
    # here-document 본문은 stdin 데이터다. 인수로 읽으면 무해한 스크립트가 막힌다.
    assert heredoc_body is None
    # 같은 줄의 리다이렉션 대상은 데이터가 아니라 열리는 경로다.
    assert heredoc_redirect is not None
    # `case`는 뒤에 값이 오는 예약어다. `x=`를 대입으로 보면 없는 경로가 생긴다.
    assert case_subject is None


def test_idle_registered_sibling_worktree_stays_protected(tmp_path: Path):
    """반증: run이 끝난 형제 checkout도 남의 작업이다.

    protected를 active 목록만으로 만들면, 정리 전 형제 worktree의 커밋 안 된
    작업이 bound 세션의 쓰기에 열린다. 무관한 자리 허용과 짝으로 대조한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    (runs[1] / "active").unlink()

    write_violation = host_write_boundary_violation(
        _write_payload(second.path / "leaked.py"),
        root,
    )
    shell_violation = host_write_boundary_violation(
        _command_payload(f"touch {second.path / 'leaked.py'}", cwd=first.path),
        root,
    )

    assert write_violation is not None
    assert "outside the bound worktree" in write_violation
    assert shell_violation is not None
    assert host_write_boundary_violation(
        _write_payload(tmp_path / "outside.txt"),
        root,
    ) is None


def test_absent_checkout_claim_is_skipped_but_a_present_one_fails_closed(tmp_path: Path):
    """반증: worktree 폴더를 지운 것만으로 hook이 저장소 전체에서 죽으면 복구가 없다.

    등록도 자리도 없는 claim은 잔재다 — 이어질 run도, 지킬 작업물도 없다. 반대로
    자리는 있는데 등록만 사라진 경우는 살아 있는 checkout의 등록이 뜯긴 것이므로
    계속 fail-closed다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    shutil.rmtree(second.path)
    _git("worktree", "prune", cwd=root)

    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None

    second.path.mkdir(parents=True)
    with pytest.raises(
        HostWriteBoundaryError,
        match="no provable registered worktree owner",
    ):
        host_write_boundary_violation(_write_payload(first.path / "feature.py"), root)


def test_boundary_blocks_paths_that_contain_or_drain_other_checkouts(tmp_path: Path):
    """반증: 보호 대상의 **안쪽**만 보면 두 구멍이 남는다.

    1) 보호 대상을 품는 경로 한 줄(`rm -rf <leader의 부모>`)이 leader를 통째로 지운다.
    2) `mv`는 목적지만 검사하면 형제 checkout의 파일을 밖으로 옮겨 그 checkout에서 지운다.
    """
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    ancestor = host_write_boundary_violation(
        _command_payload(f"rm -rf {root.parent}", cwd=first.path),
        root,
    )
    drained = host_write_boundary_violation(
        _command_payload(
            f"mv {second.path / 'README.md'} {tmp_path / 'stolen.md'}",
            cwd=first.path,
        ),
        root,
    )
    unrelated_move = host_write_boundary_violation(
        _command_payload(
            f"mv {tmp_path / 'a.md'} {tmp_path / 'b.md'}",
            cwd=first.path,
        ),
        root,
    )

    assert ancestor is not None
    assert drained is not None
    assert unrelated_move is None


def test_host_binding_rejects_recreated_active_run_checkout(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    _git("worktree", "remove", "--force", str(first.path), cwd=root)
    _git("worktree", "add", str(first.path), first.branch, cwd=root)

    # 같은 경로에 raw git으로 다시 만든 checkout은 등록 지문도 채택 기록도 원래
    # 것을 잇지 못한다. 관리 자리 밖이면 채택 증명이 먼저 깨지므로 경계 오류로 나온다.
    with pytest.raises(
        HostWriteBoundaryError,
        match="does not own its worktree|is not a provable managed worktree",
    ):
        record_host_checkout_binding(
            _status_payload(root, first, runs[0]),
            root,
        )


def test_bound_host_stops_after_an_undetected_leader_write(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    leaked = root / "dynamically-computed-leak.py"
    leaked.write_text("leaked\n", encoding="utf-8")

    violation = host_write_boundary_violation(
        _write_payload(first.path / "safe.py"),
        root,
    )

    assert violation is not None
    assert "leader checkout changed during the phase" in violation
    assert str(first.path) in violation
    assert leaked.read_text(encoding="utf-8") == "leaked\n"


def test_relative_write_target_uses_the_real_host_cwd(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    leader_relative = host_write_boundary_violation(
        _write_payload("src/leaked.py", host_cwd=root),
        root,
    )
    unknown_cwd = host_write_boundary_violation(
        _write_payload("src/unknown.py"),
        root,
    )
    worktree_relative = host_write_boundary_violation(
        _write_payload("src/feature.py", host_cwd=first.path),
        root,
    )
    leader_to_worktree = host_write_boundary_violation(
        _write_payload(
            # 새 기본 자리는 leader 밖이므로 leader 기준 상대 경로는 `..`로 시작한다.
            os.path.relpath(first.path / "src" / "feature.py", root),
            host_cwd=root,
        ),
        root,
    )

    assert leader_relative is not None and "outside the bound worktree" in leader_relative
    assert unknown_cwd is not None and "no trusted host cwd" in unknown_cwd
    assert worktree_relative is None
    assert leader_to_worktree is None


def test_codex_apply_patch_command_uses_the_host_cwd(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    patch = "*** Begin Patch\n*** Update File: src/feature.py\n*** End Patch\n"
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
        "session_id": "session-1",
        "cwd": str(first.path),
    }

    assert host_write_boundary_violation(payload, root) is None

    payload["cwd"] = str(root)
    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "outside the bound worktree" in violation


def test_host_boundary_resolves_symlinks_before_allowing_write(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    link = first.path / "sibling-link"
    link.symlink_to(second.path, target_is_directory=True)

    violation = host_write_boundary_violation(
        _write_payload(link / "leaked.py"),
        root,
    )

    assert violation is not None
    assert str(second.path) in violation


def test_unbound_host_cannot_write_while_worktree_runs_are_active(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)

    violation = host_write_boundary_violation(
        _write_payload(statuses[0].path / "feature.py", session="unbound"),
        root,
    )

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_active_run_without_registered_worktree_fails_closed(tmp_path: Path):
    """자리는 남아 있고 등록만 사라진 claim은 조작 신호다.

    자리까지 사라진 경우는 끝난 run으로 본다 —
    `test_absent_checkout_claim_is_skipped_but_a_present_one_fails_closed`가 그 짝이다.
    """
    root, statuses, _ = _setup(tmp_path)
    _git(
        "worktree",
        "remove",
        "--force",
        str(statuses[0].path),
        cwd=root,
    )
    statuses[0].path.mkdir(parents=True)

    with pytest.raises(
        HostWriteBoundaryError,
        match="no provable registered worktree owner",
    ):
        host_write_boundary_violation(
            _write_payload(root / "leaked.py", session="unbound"),
            root,
        )


def test_active_run_in_external_worktree_is_not_trusted(tmp_path: Path):
    root, _, _ = _setup(tmp_path)
    checkout = tmp_path / "external-worktree"
    _git(
        "worktree",
        "add",
        "-b",
        "feat/external-worktree",
        str(checkout),
        cwd=root,
    )
    registration = next(
        entry.registration_identity
        for entry in list_registered_worktrees(root)
        if entry.path.resolve() == checkout.resolve()
    )
    runtime_root = worktree_runtime_root(root=root, name=checkout.name)
    create_run(
        runtime_root,
        "default",
        "external",
        checkout_identity=f"worktree:{checkout.name}",
        checkout_registration_identity=registration,
    )

    # 채택 기록이 없으면 claim의 소유 checkout을 증명할 수 없다. 등록만 된 checkout이
    # 자기 이름의 런타임 상태를 만들어 놓는 것으로는 신뢰를 얻지 못한다.
    with pytest.raises(
        HostWriteBoundaryError,
        match="no provable registered worktree owner",
    ):
        host_write_boundary_violation(
            _write_payload(checkout / "feature.py", session="unbound"),
            root,
        )


def test_adoption_is_refused_while_a_run_is_active(tmp_path: Path):
    root, statuses, _runs = _setup(tmp_path)
    outside = tmp_path / "worker-made"
    _git("worktree", "add", "-b", "feat/worker-made", str(outside), cwd=root)

    # `git worktree add`는 경계의 write 명령 집합에 없어 워커도 실행할 수 있다. 채택까지
    # 실행할 수 있으면 워커가 스스로 임의 checkout을 인가해 쓰기 경계를 넓힌다. 명령
    # 문자열(`env agent-flow …`)도 cwd(`cd /tmp && …`)도 호출자가 고르므로, 호출자가
    # 고를 수 없는 것으로 판정한다 — 활성 run의 존재다.
    with pytest.raises(HostWriteBoundaryError, match="refusing to adopt while runs are active"):
        assert_adoption_allowed(root=root)

    for status in statuses:
        (worktree_runtime_root(root=root, name=status.name) / ".agent-flow" / "runs").rename(
            worktree_runtime_root(root=root, name=status.name) / ".agent-flow" / "runs-done"
        )
    assert assert_adoption_allowed(root=root) is None


def test_bound_session_cannot_write_the_adoption_record(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    record_host_checkout_binding(_status_payload(root, statuses[0], runs[0]), root)
    record = adopted_record_path(root=root, name=statuses[0].name)
    assert record is not None

    # 채택 기록이 워커의 쓰기 허용 구역(런타임 상태 디렉터리) 안에 있으면 워커가 자기
    # 이름의 기록에서 경로만 바꿔 관리 루트 밖 디렉터리를 자기 checkout으로 만든다.
    assert host_write_boundary_violation(_write_payload(record), root) is not None
    assert (
        host_write_boundary_violation(
            _command_payload(f"tee {record}", cwd=statuses[0].path), root
        )
        is not None
    )


def test_adopted_external_worktree_is_trusted(tmp_path: Path):
    """불변: 채택은 사람이 주는 인가다. 받은 checkout은 관리 루트 밖이어도 신뢰된다."""
    root, _, _ = _setup(tmp_path)
    checkout = tmp_path / "external-worktree"
    _git(
        "worktree",
        "add",
        "-b",
        "feat/external-worktree",
        str(checkout),
        cwd=root,
    )
    # 채택이 먼저다. runtime state가 이미 있으면 attach가 manifest를 쓰지 않고 돌아간다.
    status = adopt_worktree(root=root, path=checkout, allow_dirty=True)
    runtime_root = worktree_runtime_root(root=root, name=status.name)
    run_dir = create_run(
        runtime_root,
        "default",
        "external",
        checkout_identity=f"worktree:{status.name}",
        checkout_registration_identity=status.registration_identity,
    )
    binding = record_host_checkout_binding(
        _status_payload(root, status, run_dir),
        root,
    )

    assert binding is not None
    assert host_write_boundary_violation(
        _write_payload(checkout / "feature.py"),
        root,
    ) is None


def test_basename_collision_does_not_poison_unrelated_worktrees(tmp_path: Path):
    """반증: claim을 디렉터리 이름으로 묶으면 같은 basename 하나로 저장소 전역이 죽는다.

    소유자는 manifest에 적힌 경로가 지목한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    collision = tmp_path / "elsewhere" / first.path.name
    collision.parent.mkdir()
    _git(
        "worktree",
        "add",
        "-b",
        "feat/collision",
        str(collision),
        cwd=root,
    )
    binding = record_host_checkout_binding(
        _status_payload(root, first, runs[0]),
        root,
    )

    assert binding is not None
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None


def test_bound_shell_requires_current_worktree_cwd_and_blocks_sibling_paths(
    tmp_path: Path,
):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    assert host_write_boundary_violation(
        _command_payload("python3 -m pytest -q", cwd=first.path),
        root,
    ) is None
    wrong_cwd = host_write_boundary_violation(
        _command_payload("python3 -m pytest -q", host_cwd=root),
        root,
    )
    sibling_path = host_write_boundary_violation(
        _command_payload(
            f"python3 -c 'print(1)' {second.path / 'leaked.py'}",
            cwd=first.path,
        ),
        root,
    )
    assert wrong_cwd is not None and "shell cwd is not bound" in wrong_cwd
    assert sibling_path is not None and "outside the bound worktree" in sibling_path


def test_bound_shell_blocks_leader_literals_and_irreversible_symlink_escapes(
    tmp_path: Path,
):
    """경계 계약: 리터럴은 사전에 막고, 심링크 경유 쓰기는 종류로 갈린다.

    worktree 안에 만든 심링크가 leader를 가리킬 수 있다. 그 심링크로 **되돌릴 수
    없는** 명령이 나가면 사전에 막는다 — tripwire는 탐지 전용이라 늦다. 반대로
    보통 쓰기(`printf ... > leak`)는 통과시키고 phase 경계의 tripwire가 잡는다.
    심링크 이름만으로는 읽기와 쓰기를 구분할 수 없고, 구분하려고 명령별 규칙을
    다시 세우면 `./node_modules/.bin/tsc`(leader의 공유 의존성을 가리키는 심링크)
    같은 정상 호출까지 함께 막힌다 —
    `test_worktree_tripwire.py::test_symlinked_node_modules_binary_is_not_a_violation`이
    그 짝이다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    leak_link = first.path / "leak"
    leak_link.symlink_to(root / "leaked.py")

    embedded = host_write_boundary_violation(
        _command_payload(
            f"python3 -c \"from pathlib import Path; Path('{root / 'leaked.py'}').write_text('x')\"",
            cwd=first.path,
        ),
        root,
    )
    destructive = host_write_boundary_violation(
        _command_payload("rm -f leak", cwd=first.path),
        root,
    )
    reversible = host_write_boundary_violation(
        _command_payload("printf leaked > leak", cwd=first.path),
        root,
    )

    assert embedded is not None and "outside the bound worktree" in embedded
    assert destructive is not None
    assert "cannot be detected after the fact" in destructive
    # 정적 분석이 놓는 자리다. 놓은 채로 두는 근거는 tripwire가 내용까지 비교해
    # 같은 쓰기를 phase 경계에서 잡는다는 것이다.
    assert reversible is None


def test_path_qualified_installed_cli_counts_as_a_lifecycle_command(tmp_path: Path):
    """반증: 복구 명령을 이름으로만 인정하면 PATH에 없는 환경에서 복구가 불가능하다.

    실제로 그 상태가 있었다 — 활성 run 때문에 모든 write가 막혔는데, 유일한 해제
    명령이 PATH에 없어서 경로로 부르면 거부됐다. 경계의 복구 명령이 그 경계 뒤에
    있으면 그건 경계가 아니라 교착이다. 그래서 이름이 아니라 설치 산출물과의 경로
    동일성으로 인정하고, 이름만 흉내 낸 실행 파일은 계속 거부한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    shim = root / ".agent-flow" / "bin" / "agent-flow"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    impostor = tmp_path / "agent-flow"
    impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    impostor.chmod(0o755)

    installed = host_write_boundary_violation(
        _command_payload(
            f"{shim} status --root {root} --worktree {first.name}",
            session="unbound",
            host_cwd=root,
        ),
        root,
    )
    forged = host_write_boundary_violation(
        _command_payload(
            f"{impostor} status --root {root} --worktree {first.name}",
            session="unbound",
            host_cwd=root,
        ),
        root,
    )

    assert installed is None
    assert forged is not None
    # 같은 판정이 binding에도 쓰여야 한다. guard만 열리고 binding이 안 되면
    # 세션은 계속 unbound로 남아 복구가 끝나지 않는다.
    payload = _status_payload(root, first, runs[0], session="unbound")
    payload["tool_input"]["command"] = (
        f"{shim} status --root {root} --worktree {first.name}"
    )
    payload["cwd"] = str(root)
    assert record_host_checkout_binding(payload, root) is not None


def test_bound_shell_allows_dynamic_path_commands_at_static_check(
    tmp_path: Path,
):
    """정책 변경: 정적 분석은 인라인 코드를 더 이상 막지 않는다.

    실제 쓰기 탐지는 PostToolUse worktree-tripwire.py 가 담당한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    violation = host_write_boundary_violation(
        _command_payload(
            (
                "python3 -c \"from pathlib import Path; "
                "target = Path.cwd().parents[2] / 'dynamic-leak.py'; "
                "target.write_text('leaked')\""
            ),
            cwd=first.path,
        ),
        root,
    )

    # 정적 분석은 이제 통과한다. 실제 쓰기는 PostToolUse tripwire 가 탐지한다.
    assert violation is None
    assert not (root / "dynamic-leak.py").exists()



def test_bound_shell_accepts_explicit_cd_and_matching_lifecycle_command(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    assert host_write_boundary_violation(
        _command_payload(
            f"cd {first.path} && python3 -m pytest -q",
            host_cwd=root,
        ),
        root,
    ) is None
    assert host_write_boundary_violation(
        _command_payload(
            f"agent-flow continue --root {root} --worktree {first.name}",
            host_cwd=first.path,
        ),
        root,
    ) is None


def test_bound_host_blocks_new_run_and_foreign_lifecycle_root(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    new_run = host_write_boundary_violation(
        _command_payload(
            f"agent-flow run other --root {root}",
            cwd=first.path,
        ),
        root,
    )
    foreign_status = host_write_boundary_violation(
        _command_payload(
            f"agent-flow status --root {foreign}",
            cwd=first.path,
        ),
        root,
    )

    assert new_run is not None and "new run" in new_run
    assert foreign_status is not None and "another project root" in foreign_status


def test_live_binding_cannot_move_to_another_checkout(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    with pytest.raises(HostWriteBoundaryError, match="already bound"):
        record_host_checkout_binding(
            _status_payload(root, second, runs[1]),
            root,
        )


def test_unbound_lifecycle_command_is_allowed_to_establish_binding(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)
    first = statuses[0]

    assert host_write_boundary_violation(
        _command_payload(
            f"agent-flow status --root {root} --worktree {first.name}",
            session="new-session",
            host_cwd=root,
        ),
        root,
    ) is None


def test_compound_lifecycle_command_cannot_bypass_an_unbound_session(tmp_path: Path):
    root, statuses, _ = _setup(tmp_path)
    first = statuses[0]
    payload = _command_payload(
        (
            f"agent-flow status --root {root} --worktree {first.name}; "
            f"printf leaked > {root / 'leaked.py'}"
        ),
        session="unbound",
        host_cwd=root,
    )

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_unbound_session_does_not_trust_lifecycle_lookalike_executables(
    tmp_path: Path,
):
    root, statuses, _ = _setup(tmp_path)
    first = statuses[0]
    payload = _command_payload(
        (
            "node /tmp/agent-flow-kit.mjs status "
            f"--root {root} --worktree {first.name}"
        ),
        session="unbound",
        host_cwd=root,
    )

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_compound_lifecycle_output_cannot_create_a_binding(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    payload = _status_payload(root, statuses[0], runs[0])
    payload["tool_input"]["command"] += "; printf forged"

    assert record_host_checkout_binding(payload, root) is None


def test_binding_status_must_match_a_verified_active_run(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    payload = _status_payload(root, statuses[0], runs[0])
    payload["output"] = payload["output"].replace(runs[0].name, "forged-run")

    with pytest.raises(HostWriteBoundaryError):
        record_host_checkout_binding(payload, root)


def test_parallel_run_binds_from_lines_when_status_json_is_truncated(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    payload = _status_payload(root, first, runs[0])
    payload["tool_input"]["command"] = f"agent-flow run new-task --root {root}"
    payload["output"] = (
        f"run: default/{runs[0].name}\n"
        f"next_command: agent-flow continue --root {root} --worktree {first.name}\n"
        'status_json: {"next_command": "agent-flow continue'
    )

    binding = record_host_checkout_binding(payload, root)

    assert binding is not None
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"),
        root,
    ) is None


def test_patch_paths_are_checked_even_without_a_structured_path_field(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {
            "patch": (
                "*** Begin Patch\n"
                f"*** Update File: {second.path / 'leaked.py'}\n"
                "@@\n-old\n+new\n"
                "*** End Patch\n"
            )
        },
        "session_id": "session-1",
    }

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert str(second.path) in violation


def test_patch_move_destination_is_checked(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    first, second = statuses
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    payload = {
        "tool_name": "edit",
        "tool_input": {
            "input": (
                "*** Begin Patch\n"
                f"[{first.path / 'source.py'}#A1B2]\n"
                f"MV {second.path / 'leaked.py'}\n"
                "*** End Patch\n"
            )
        },
        "session_id": "session-1",
        "cwd": str(first.path),
    }

    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert str(second.path) in violation


def test_binding_file_symlink_is_not_trusted(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    binding = record_host_checkout_binding(
        _status_payload(root, statuses[0], runs[0]),
        root,
    )
    assert binding is not None
    replacement = binding.with_name("forged.json")
    replacement.write_text(binding.read_text(encoding="utf-8"), encoding="utf-8")
    binding.unlink()
    binding.symlink_to(replacement)

    violation = host_write_boundary_violation(
        _write_payload(statuses[0].path / "feature.py"),
        root,
    )

    assert violation is not None
    assert "not bound to an active worktree" in violation


def test_binding_directory_symlink_is_rejected(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    binding_directory = root / ".git" / "agent-flow" / "host-sessions"
    binding_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HostWriteBoundaryError, match="not a real directory"):
        record_host_checkout_binding(
            _status_payload(root, statuses[0], runs[0]),
            root,
        )

    assert not tuple(outside.iterdir())


def test_installed_hooks_bind_then_block_a_leader_write(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_boundary_hooks(root)

    binding = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "bind-host-worktree.py")),
        cwd=root,
        input=json.dumps(_status_payload(root, statuses[0], runs[0])),
        text=True,
        capture_output=True,
        check=False,
    )
    blocked = subprocess.run(
        ("/bin/bash", str(hooks / "guard-host-worktree.sh")),
        cwd=root,
        input=json.dumps(_write_payload(root / "leaked.py")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert binding.returncode == 0, binding.stderr
    assert blocked.returncode == 2
    assert "작업을 중단했습니다" in blocked.stderr


def test_installed_guard_stops_after_a_dynamic_leader_leak(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    hooks = _install_boundary_hooks(root)
    binding = subprocess.run(
        ("/usr/bin/python3", "-I", str(hooks / "bind-host-worktree.py")),
        cwd=root,
        input=json.dumps(_status_payload(root, statuses[0], runs[0])),
        text=True,
        capture_output=True,
        check=False,
    )
    (root / "dynamic-leak.py").write_text("leaked\n", encoding="utf-8")

    blocked = subprocess.run(
        ("/bin/bash", str(hooks / "guard-host-worktree.sh")),
        cwd=root,
        input=json.dumps(_write_payload(statuses[0].path / "safe.py")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert binding.returncode == 0, binding.stderr
    assert blocked.returncode == 2
    assert "leader checkout changed during the phase" in blocked.stderr
    assert str(statuses[0].path) in blocked.stderr


def test_installed_guard_allows_non_git_projects_without_active_worktrees(
    tmp_path: Path,
):
    root = tmp_path / "non-git"
    root.mkdir()
    hooks = _install_boundary_hooks(root)

    result = subprocess.run(
        ("/bin/bash", str(hooks / "guard-host-worktree.sh")),
        cwd=root,
        input=json.dumps(_write_payload(root / "feature.py")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_virtual_write_targets_do_not_escape_the_filesystem_boundary(tmp_path: Path):
    root, statuses, runs = _setup(tmp_path)
    record_host_checkout_binding(_status_payload(root, statuses[0], runs[0]), root)
    payload = {
        "tool_name": "write",
        "tool_input": {"path": "xd://lsp", "content": "{}"},
        "session_id": "session-1",
    }

    assert host_write_boundary_violation(payload, root) is None

    payload["tool_input"]["path"] = "file:///tmp/leaked.py"
    violation = host_write_boundary_violation(payload, root)

    assert violation is not None
    assert "virtual write target is not trusted" in violation


def test_a_normal_leader_pull_does_not_stop_the_worker(tmp_path: Path):
    """반증: 정상 pull 하나로 워커가 멈추면 그 정지를 푸는 수단이 따로 필요해진다.

    사람이 leader에서 fast-forward하면 그 이동은 leader 자신의 reflog에 남는다.
    워커가 건드린 것이 아니므로 차단 대상이 아니고, 워커의 쓰기는 계속 열려야 한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    assert record_host_checkout_binding(
        _status_payload(root, first, runs[0]), root
    ) is not None

    _fast_forward_leader(root, tmp_path)

    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"), root
    ) is None
    assert host_write_boundary_violation(
        _command_payload(
            f"agent-flow status --root {root} --worktree {first.name}",
            cwd=first.path,
        ),
        root,
    ) is None


def test_leader_branch_switch_still_stops_the_worker(tmp_path: Path):
    """불변: 브랜치가 바뀌면 워킹트리가 통째로 다른 커밋 내용이 된다.

    그 상태에서는 `git diff HEAD`가 clean이라 status 축이 아무 신호도 내지 않으므로,
    HEAD 축이 잡아야 한다. 완화는 **같은 브랜치**의 전진에만 준다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    assert record_host_checkout_binding(
        _status_payload(root, first, runs[0]), root
    ) is not None

    _git("checkout", "-q", "-b", "leader-side-branch", cwd=root)

    blocked = host_write_boundary_violation(
        _write_payload(first.path / "feature.py"), root
    )
    assert blocked is not None
    assert "branch switched" in blocked


def test_head_moved_without_the_leader_recording_it_is_blocked(tmp_path: Path):
    """불변: 공유 ref를 leader 밖에서 밀어 넣은 이동은 leader reflog에 남지 않는다.

    git은 HEAD reflog를 checkout마다 따로 쓴다. 그래서 다른 worktree가 공유 ref를
    직접 갱신하면 leader의 `logs/HEAD`는 그대로다. 마지막 줄을 지워 그 관측 상태를
    그대로 재현한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    assert record_host_checkout_binding(
        _status_payload(root, first, runs[0]), root
    ) is not None

    _fast_forward_leader(root, tmp_path)
    reflog = root / ".git" / "logs" / "HEAD"
    kept = reflog.read_text(encoding="utf-8").splitlines()[:-1]
    reflog.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")

    blocked = host_write_boundary_violation(
        _write_payload(first.path / "feature.py"), root
    )
    assert blocked is not None
    assert "HEAD moved" in blocked
    assert "without the leader checkout recording it" in blocked


def test_worker_writes_into_the_leader_are_still_caught(tmp_path: Path):
    """HEAD 비교를 완화해도 working tree 축은 등호 그대로여야 한다."""
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    assert record_host_checkout_binding(
        _status_payload(root, first, runs[0]), root
    ) is not None

    (root / "leaked.txt").write_text("worker spill\n", encoding="utf-8")

    blocked = host_write_boundary_violation(
        _write_payload(first.path / "feature.py"), root
    )
    assert blocked is not None
    assert "working tree gained" in blocked


def test_a_binding_with_a_legacy_snapshot_rebinds_instead_of_blocking(tmp_path: Path):
    """반증: 낡은 형식 스냅샷을 그대로 대조하면 bound 세션의 모든 write가 근거 없이
    막힌다. 업그레이드 한 번에 진행 중인 세션 전부가 그렇게 된다.

    binding을 없는 것으로 읽어 다음 lifecycle 명령이 새 형식으로 다시 맺게 한다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    binding_path = record_host_checkout_binding(
        _status_payload(root, first, runs[0]), root
    )
    assert binding_path is not None

    # 구버전이 남긴 binding: 스냅샷에 `version` 키가 없고 status 형식도 다르다.
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    snapshot = dict(payload["leader_snapshot"])
    assert snapshot.pop("version") is not None
    snapshot["status"] = "예전 형식에서는 이 줄들이 달랐다"
    payload["leader_snapshot"] = snapshot
    binding_path.write_text(json.dumps(payload), encoding="utf-8")

    # unbound로 읽히므로 leader는 계속 닫혀 있고 lifecycle 명령은 열려 있다.
    blocked = host_write_boundary_violation(_write_payload(root / "leaked.py"), root)
    assert blocked is not None and "not bound to an active worktree" in blocked
    assert host_write_boundary_violation(
        _command_payload(
            f"agent-flow status --root {root} --worktree {first.name}",
            cwd=first.path,
        ),
        root,
    ) is None

    # 그 lifecycle 명령의 출력이 새 형식으로 다시 맺는다.
    rebound = record_host_checkout_binding(
        _status_payload(root, first, runs[0]), root
    )
    assert rebound == binding_path
    fresh = json.loads(binding_path.read_text(encoding="utf-8"))
    assert fresh["leader_snapshot"]["version"] == LEADER_SNAPSHOT_VERSION
    assert host_write_boundary_violation(
        _write_payload(first.path / "feature.py"), root
    ) is None


def test_unbound_session_keeps_its_own_idle_checkout_writable(tmp_path: Path):
    """반증: 옆 worktree의 run 때문에 무관한 checkout의 commit/push까지 죽으면 안 된다.

    unbound 세션에게 열리는 자리는 자기가 서 있는 idle checkout 하나뿐이다. leader와
    활성 worktree는 그대로 닫혀 있다 — bind 전에 새는 워커가 노리는 자리가 거기다.
    """
    root, statuses, _ = _setup(tmp_path)
    mine = tmp_path / "mine"
    _git("worktree", "add", "-b", "feat/mine", str(mine), cwd=root)

    assert host_write_boundary_violation(
        _write_payload(mine / "feature.py", session="mine", host_cwd=mine),
        root,
    ) is None
    assert host_write_boundary_violation(
        _command_payload("git commit -am wip", cwd=mine, session="mine"),
        root,
    ) is None

    leader = host_write_boundary_violation(
        _write_payload(root / "leaked.py", session="mine", host_cwd=mine),
        root,
    )
    sibling = host_write_boundary_violation(
        _write_payload(statuses[0].path / "leaked.py", session="mine", host_cwd=mine),
        root,
    )

    assert leader is not None and "not bound to an active worktree" in leader
    assert sibling is not None and "not bound to an active worktree" in sibling


def test_pre_block_surface_stays_two_rules():
    """계약: 사전 차단은 두 개뿐이다 — 보호 경로 리터럴, 되돌릴 수 없는 명령 목록.

    명령별 "쓰기 대상" 표를 다시 들이면 무한한 셸 문법을 유한한 목록으로 쫓는
    예전 구조로 돌아간다. 그 구조가 표 6개(`_WRITE_OPERAND_COMMANDS`,
    `_WRITE_DEST_LAST_COMMANDS`, `_WRITE_ALL_OPERAND_COMMANDS`, 리다이렉트 2개,
    `_command_write_targets`)를 만들었고, 그 표들이 예외 행렬의 출처였다.

    파괴 목록의 판정 기준은 "무엇을 쓰는가"가 아니라 "복구가 불가능한가"다. 복구
    가능한 쓰기가 목록에 들어오는 순간 같은 미끄러짐이 다시 시작된다.
    """
    import agent_flow.core.host_write_boundary as boundary

    revived = sorted(
        name
        for name in vars(boundary)
        if name.startswith(("_WRITE_OPERAND", "_WRITE_DEST", "_WRITE_ALL_OPERAND"))
        or name in {"_WRITE_REDIRECTS", "_NON_FILE_REDIRECTS", "_command_write_targets"}
    )
    assert not revived, f"명령별 쓰기 대상 표가 되살아났다: {revived}"

    for reversible in (
        "touch", "cp", "tee", "chmod", "chown", "dd", "sed", "ln",
        "rsync", "install", "truncate", "mkdir",
    ):
        assert reversible not in boundary._DESTRUCTIVE_COMMANDS, reversible
    for recoverable in ("commit", "add", "status", "stash", "switch"):
        assert recoverable not in boundary._DESTRUCTIVE_GIT_SUBCOMMANDS, recoverable


def test_lifecycle_exemption_stays_narrow():
    """계약: lifecycle 면제는 워커가 자기 런을 진행시키는 명령에만 준다.

    면제된 경로는 R1 리터럴 검사·R2 파괴 목록·leader tripwire를 한꺼번에 건너뛴다.
    그래서 이 목록에 이름을 더하는 것은 그 셋을 그 명령에 대해 끄는 것과 같다.
    실행으로 확인된 결과: `eval --judge-command`는 임의 argv를 실행하고,
    `worktree remove --name`은 형제 checkout을 지우고, `handoff --run-dir`은
    남의 런 산출물을 덮어쓴다.
    """
    import agent_flow.core.host_write_boundary as boundary

    assert boundary._LIFECYCLE_COMMANDS == frozenset(
        {"continue", "run", "start", "status"}
    ), "lifecycle 면제 범위가 바뀌었다 — 늘렸다면 경로형 인자 검증이 먼저 필요하다"


def test_agent_flow_calls_outside_the_exemption_still_face_the_boundary(tmp_path: Path):
    """불변: 면제 밖 서브커맨드는 leader 경로를 인자로 받아도 경계를 통과 못 한다."""
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)

    for command in (
        f"agent-flow eval --root {root} --judge-command touch {root}/pwned",
        f"agent-flow worktree remove --root {root} --name {statuses[1].name}",
        f"agent-flow handoff --run-dir {runs[1]} --from-stage implement --to-stage review",
    ):
        assert host_write_boundary_violation(
            _command_payload(command, cwd=first.path), root
        ) is not None, f"면제 밖 명령이 통과했다: {command}"


def test_recovery_command_passes_in_every_blocked_state(tmp_path: Path):
    """불변: 경계가 만든 교착의 해제 명령은 그 경계 뒤에 있으면 안 된다.

    지금까지 나온 세 건 — unbound 전면 차단, stale baseline 교착, 경로 지정 호출
    거부 — 는 서로 다른 버그가 아니라 이 불변식이 깨진 세 표현이었다. 네 상태
    전부에서 lifecycle 명령이 통과해야 한다. 하나라도 막히면 그 상태에 갇힌
    세션에는 탈출구가 없다.
    """
    root, statuses, runs = _setup(tmp_path)
    first = statuses[0]
    lifecycle = f"agent-flow status --root {root} --worktree {first.name}"

    assert host_write_boundary_violation(
        _command_payload(lifecycle, session="nobody", host_cwd=root), root
    ) is None, "unbound 세션이 복구 명령을 실행할 수 없다"

    record_host_checkout_binding(_status_payload(root, first, runs[0]), root)
    assert host_write_boundary_violation(
        _command_payload(lifecycle, cwd=first.path), root
    ) is None, "bound 세션이 복구 명령을 실행할 수 없다"

    _fast_forward_leader(root, tmp_path)
    assert host_write_boundary_violation(
        _command_payload(lifecycle, cwd=first.path), root
    ) is None, "정상 fast-forward 뒤에 복구 명령이 막혔다"

    (root / "user-scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    assert host_write_boundary_violation(
        _command_payload(lifecycle, cwd=first.path), root
    ) is None, "leader가 dirty해진 뒤에 복구 명령이 막혔다"
