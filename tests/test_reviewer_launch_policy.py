"""reviewer subprocess의 실행 identity가 선언되고 기록되는가.

이 파일이 있는 이유는 증명할 수 없는 주장이었다. reviewer는 provider만 고르고
model/effort는 아무도 지정하지 않았으며, artifact에도 남지 않았다. 그래서
"Claude Fable xhigh로 리뷰했다"를 산출물로 뒷받침할 방법이 없었다.

반증 짝을 함께 둔다: 선언이 없을 때 argv가 예전 그대로여야 하고(회귀), 선언이
있는데 해석할 수 없으면 막혀야 한다(조용한 기본값 접기 금지).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_flow import multi_review
from agent_flow.cli_detect import CliInfo
from agent_flow.core import reviewer_launch

PROJECT_ROOT = Path("/tmp/reviewer-launch-project")
_CLAUDE = CliInfo("claude", ("claude",), ("-p",))
_CODEX = CliInfo("codex", ("codex",), ("exec",))
_CLIS = {"claude": _CLAUDE, "codex": _CODEX, "omp": CliInfo("omp", ("omp",), ("-p",))}

# 수정 전 argv. 여기를 손대면 "선언하지 않은 저장소는 아무것도 달라지지 않는다"가
# 깨진 것이므로, 기대값을 고치기 전에 그 변경이 의도된 것인지 먼저 판단해야 한다.
_LEGACY_CLAUDE_ARGV = (
    "-p",
    "--safe-mode",
    "--permission-mode",
    "plan",
    "--no-session-persistence",
    "review",
)
_LEGACY_CODEX_ARGV = (
    "exec",
    "--ephemeral",
    "--ignore-user-config",
    "--sandbox",
    "danger-full-access",
    "--cd",
    str(PROJECT_ROOT.resolve()),
    "review",
)


@pytest.fixture(autouse=True)
def _hermetic_launch(monkeypatch: pytest.MonkeyPatch):
    """CLI 버전 관측을 테스트가 정한다. 실제 provider를 띄우지 않는다."""
    monkeypatch.setattr(multi_review, "cli_by_name", _CLIS.get)
    monkeypatch.setattr(multi_review, "_cli_version", lambda binary: f"{binary} 9.9.9")


def _resolve(profile, *, cli: CliInfo, angle_id="architecture-design", phase_id="final-review"):
    return multi_review._resolve_reviewer_launch(
        phase_id=phase_id,
        angle_id=angle_id,
        profile=profile,
        cli=cli,
        prompt="review",
        project_root=PROJECT_ROOT,
    )


def _policy(*candidates, match=None):
    rule = {"candidates": list(candidates)}
    if match is not None:
        rule["match"] = match
    return {"execution": {"reviewers": [rule]}}


@pytest.mark.parametrize(
    "cli, expected",
    [
        (_CLAUDE, ("--model", "fable", "--effort", "xhigh")),
        (_CODEX, ("--model", "gpt-5.6-sol", "-c", 'model_reasoning_effort="xhigh"')),
    ],
)
def test_declared_model_and_effort_reach_the_provider_argv(cli, expected):
    """선언한 identity가 실제 argv에 들어간다. provider마다 철자가 다르다 —
    claude는 `--effort`를 받고, codex는 effort 플래그가 없어 `-c` config override로만
    받는다(`claude --help`, `codex exec --help` 실측).
    """
    profile = _policy(
        {"provider": "claude", "model": "fable", "effort": "xhigh"},
        {"provider": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
    )

    launch = _resolve(profile, cli=cli)

    assert launch.provider == cli.name
    assert launch.effort == "xhigh"
    window = launch.argv[1 : 1 + len(expected)]
    assert window == expected, launch.argv
    # identity는 invoke 접두 뒤, 기존 플래그 앞에 온다. prompt는 계속 마지막이다.
    assert launch.argv[0] == cli.invoke[0]
    assert launch.argv[-1] == "review"


@pytest.mark.parametrize(
    "candidate",
    [
        # reviewer pool은 claude/codex뿐이다. omp는 host 전용이라 pool에 없다.
        {"provider": "omp", "model": "whatever"},
        {"provider": "claude", "effort": "ultra"},
        {"provider": "claude", "effort": "max"},
        {"provider": "claude", "model": "--dangerously-skip-permissions"},
        {"provider": "claude", "model": "fable", "reasoning": "xhigh"},
    ],
)
def test_unsupported_declarations_fail_closed(candidate):
    """해석할 수 없는 선언은 기본값으로 접지 않고 막는다. 접으면 artifact가
    선언하지 않은 model로 돈 리뷰를 선언대로 돈 것처럼 보이게 한다.
    """
    with pytest.raises(multi_review.ReviewerLaunchError):
        _resolve(_policy(candidate), cli=_CLAUDE)


@pytest.mark.parametrize(
    "cli, legacy",
    [(_CLAUDE, _LEGACY_CLAUDE_ARGV), (_CODEX, _LEGACY_CODEX_ARGV)],
)
def test_no_declaration_keeps_the_previous_argv(cli, legacy):
    """선언하지 않은 저장소에서는 argv가 한 글자도 달라지지 않는다."""
    assert _resolve(None, cli=cli).argv == legacy
    assert _resolve({}, cli=cli).argv == legacy
    # phase/angle이 맞지 않는 rule도 "선언 없음"과 같아야 한다.
    unmatched = _policy(
        {"provider": "claude", "model": "fable"},
        match={"angle": "compose-stability"},
    )
    assert _resolve(unmatched, cli=cli).argv == legacy
    assert _resolve(unmatched, cli=cli).model is None


def test_declaration_decorates_the_assigned_provider_and_never_switches_it():
    """provider를 고르는 권한은 `distribute()`에만 있다.

    선언이 provider를 갈아 끼우면 claude만 선언한 rule이 두 슬롯을 모두 claude로
    띄우면서 artifact는 `<angle>-codex.md`로 남는다 — "독립된 두 provider가 봤다"가
    존재하지 않는 다양성으로 만족된 것처럼 읽힌다.
    """
    claude_only = _policy({"provider": "claude", "model": "fable", "effort": "xhigh"})

    decorated = _resolve(claude_only, cli=_CLAUDE)
    untouched = _resolve(claude_only, cli=_CODEX)

    assert (decorated.provider, decorated.model, decorated.effort) == (
        "claude",
        "fable",
        "xhigh",
    )
    # 배정된 provider가 선언에 없으면 model/effort 없는 기본 argv다.
    assert (untouched.provider, untouched.model, untouched.effort) == (
        "codex",
        None,
        None,
    )
    assert untouched.argv == _LEGACY_CODEX_ARGV

    # 같은 provider가 여러 번 선언되면 선언 순서가 우선순위다.
    ordered = _policy(
        {"provider": "claude", "model": "fable"},
        {"provider": "claude", "model": "sonnet"},
    )
    assert _resolve(ordered, cli=_CLAUDE).model == "fable"

    # 쓰이지 않는 candidate의 오타도 여기서 막힌다. 그 provider가 배정되는 날에만
    # 터지면 선언을 쓴 사람은 이미 자리를 떠났다.
    typo_elsewhere = _policy(
        {"provider": "claude", "model": "fable"},
        {"provider": "codex", "effort": "ultra"},
    )
    with pytest.raises(reviewer_launch.ReviewerLaunchError):
        _resolve(typo_elsewhere, cli=_CLAUDE)


def test_unknown_execution_keys_fail_closed():
    """`execution` 바로 아래의 오타도 거부한다. `reviewer:`가 통과하면 선언 전체가
    조용히 버려지고, artifact는 선언하지 않은 기본 model로 돈 리뷰를 남긴다.
    """
    typo = {"reviewer": [{"candidates": [{"provider": "claude", "model": "fable"}]}]}

    with pytest.raises(
        reviewer_launch.ReviewerLaunchError, match="execution supports only reviewers"
    ):
        reviewer_launch.validate_reviewer_launch_declaration(typo)
    # 실행 경로도 같은 파서로 막힌다.
    with pytest.raises(reviewer_launch.ReviewerLaunchError):
        _resolve({"execution": typo}, cli=_CLAUDE)


def test_artifact_records_identity_but_not_raw_argv_or_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """artifact가 실행 identity를 증명한다. argv 원문과 prompt 본문은 남기지 않는다 —
    호스트 경로와 프롬프트 전문이 리뷰 산출물로 새면 안 된다.
    """
    from agent_flow.subprocess_pool import SubprocessResult

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "generic.local.yaml").write_text(
        "execution:\n"
        "  reviewers:\n"
        "    - match:\n"
        "        phase: final-review\n"
        "      candidates:\n"
        "        - provider: claude\n"
        "          model: fable\n"
        "          effort: xhigh\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_FLOW_PROFILE", raising=False)
    monkeypatch.setattr(
        multi_review, "assert_managed_hooks_registered", lambda *a, **k: None
    )
    launched: list[tuple[str, ...]] = []

    def fake_run_parallel(jobs):
        launched.extend(job.args for job in jobs)
        return [
            SubprocessResult(
                job_id=job.job_id,
                returncode=0,
                stdout="reviewer-source: sub-agent\nverdict: pass",
            )
            for job in jobs
        ]

    monkeypatch.setattr(multi_review, "run_parallel", fake_run_parallel)

    secret_prompt = "review this: SECRET-PROMPT-BODY"
    output = tmp_path / "claude-generalist.md"
    distribution = multi_review.Distribution(
        by_cli={
            "claude": [
                multi_review.ReviewerJob(
                    "generalist", secret_prompt, output, tmp_path
                )
            ]
        },
        phase_id="final-review",
    )

    multi_review.run_distribution(distribution, tmp_path)

    assert launched and "--model" in launched[0]
    artifact = output.read_text(encoding="utf-8")
    assert "- provider: claude" in artifact
    assert "- model: fable" in artifact
    assert "- effort: xhigh" in artifact
    assert "- cli-version: claude 9.9.9" in artifact
    assert f"- argv-digest: {multi_review._argv_digest(launched[0])}" in artifact
    assert f"- prompt-digest: {multi_review._text_digest(secret_prompt)}" in artifact
    # 원문은 새지 않는다.
    assert "SECRET-PROMPT-BODY" not in artifact
    assert "--no-session-persistence" not in artifact
    assert str(tmp_path) not in artifact
    # 기존 marker 파서가 읽는 줄은 그대로다.
    assert "reviewer-source: sub-agent" in artifact
    assert "verdict: pass" in artifact
    assert artifact.startswith("# claude-generalist\n")


def test_unspecified_identity_is_named_not_blank(tmp_path: Path):
    """model을 선언하지 않았다는 사실도 기록이다. 빈 칸으로 두면 "기록이 없다"와
    구분되지 않는다.
    """
    from agent_flow.subprocess_pool import SubprocessResult

    launch = _resolve(None, cli=_CLAUDE)
    rendered = multi_review._render_angle_result(
        SubprocessResult(
            job_id="claude-generalist",
            returncode=0,
            stdout="reviewer-source: sub-agent\n",
        ),
        launch=launch,
        prompt="review",
    )

    assert "- model: unspecified" in rendered
    assert "- effort: unspecified" in rendered


def test_rate_limited_artifact_still_records_what_was_attempted():
    """막힌 리뷰에서 model이 가장 중요한 기록이다. 어떤 model로 시도해 막혔는지가
    다음 수(같은 model 재시도인지, 다른 model로 내려가는지)를 정한다.
    """
    from agent_flow.subprocess_pool import SubprocessResult

    launch = _resolve(
        _policy({"provider": "claude", "model": "fable", "effort": "xhigh"}),
        cli=_CLAUDE,
    )
    rendered = multi_review._render_angle_result(
        SubprocessResult(
            job_id="claude-generalist",
            returncode=1,
            stderr="You've hit your usage limit. Resets in 30 minutes.",
        ),
        launch=launch,
        prompt="review",
    )

    assert "reason: reviewer_rate_limited" in rendered
    assert "- provider: claude" in rendered
    assert "- model: fable" in rendered
    assert "- effort: xhigh" in rendered
    assert f"- prompt-digest: {multi_review._text_digest('review')}" in rendered


def test_schema_and_resolver_declare_the_same_efforts_and_providers():
    """`_schema.yaml`이 문서고 resolver가 판정이다. 갈리면 선언한 대로 적은
    저장소가 실행에서 거부된다.
    """
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_flow"
        / "profiles"
        / "_schema.yaml"
    )
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    candidate = schema["optional"]["execution"]["reviewers"][0]["candidates"][0]

    providers = tuple(part.strip() for part in candidate["provider"].split("|"))
    efforts = tuple(part.strip() for part in candidate["effort"].split("|"))

    assert providers == multi_review.REVIEW_CLI_NAMES
    assert efforts == reviewer_launch.REVIEWER_EFFORTS


def test_profile_override_rejects_an_unresolvable_declaration(tmp_path: Path):
    """선언이 틀렸으면 선언한 자리에서 막힌다. 리뷰가 시작될 때까지 기다리지 않는다."""
    from agent_flow.core.profiles import load_profile_payload

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    override = profiles / "generic.local.yaml"
    override.write_text(
        "execution:\n"
        "  reviewers:\n"
        "    - candidates:\n"
        "        - provider: omp\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid profile override execution"):
        load_profile_payload("generic", tmp_path)

    override.write_text("execution:\n", encoding="utf-8")
    with pytest.raises(ValueError, match="profile override execution must be a mapping"):
        load_profile_payload("generic", tmp_path)

    override.write_text(
        "execution:\n"
        "  reviewers:\n"
        "    - candidates:\n"
        "        - provider: codex\n"
        "          model: gpt-5.6-sol\n",
        encoding="utf-8",
    )
    payload = load_profile_payload("generic", tmp_path)
    assert payload["execution"]["reviewers"][0]["candidates"][0]["model"] == "gpt-5.6-sol"


def test_override_rejects_an_unknown_execution_key(tmp_path: Path):
    """`execution` 아래 오타는 선언한 자리에서 막힌다. 통과시키면 사용자는 선언이
    반영됐다고 믿고, 리뷰는 기본 model로 돈다.
    """
    from agent_flow.core.profiles import load_profile_payload

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "generic.local.yaml").write_text(
        "execution:\n"
        "  reviewer:\n"
        "    - candidates:\n"
        "        - provider: claude\n"
        "          model: fable\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="execution supports only reviewers"):
        load_profile_payload("generic", tmp_path)


def test_declaration_is_read_from_the_config_root_not_the_worker_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """선언은 leader에 있다. `branching.worktree: required` 프로젝트의 managed
    checkout에는 `.agent-flow/`가 아예 없어서(gitignored) checkout을 profile 소스로
    쓰면 선언 전체가 조용히 무시된다.

    같은 실행으로 `match.phase`가 일반 review phase에서 발동하는 것도 확인한다 —
    `phase_id`가 `None`이면 phase를 지정한 rule은 절대 매치되지 않는다.
    """
    from agent_flow.subprocess_pool import SubprocessResult

    leader_profiles = tmp_path / "leader" / ".agent-flow" / "profiles"
    leader_profiles.mkdir(parents=True)
    (leader_profiles / "generic.local.yaml").write_text(
        "execution:\n"
        "  reviewers:\n"
        "    - match:\n"
        "        phase: review\n"
        "      candidates:\n"
        "        - provider: claude\n"
        "          model: fable\n"
        "          effort: xhigh\n",
        encoding="utf-8",
    )
    checkout = tmp_path / "worktrees" / "feat-x"
    checkout.mkdir(parents=True)
    assert not (checkout / ".agent-flow").exists()

    monkeypatch.delenv("AGENT_FLOW_PROFILE", raising=False)
    monkeypatch.setattr(
        multi_review, "assert_managed_hooks_registered", lambda *a, **k: None
    )
    # checkout은 실제 linked worktree가 아니다. tripwire를 무장시키지 않고 선언
    # 소스만 본다.
    monkeypatch.setattr(multi_review, "leader_root_for", lambda root: None)
    launched: list[tuple[str, ...]] = []

    def fake_run_parallel(jobs):
        launched.extend(job.args for job in jobs)
        return [
            SubprocessResult(
                job_id=job.job_id,
                returncode=0,
                stdout="reviewer-source: sub-agent\nverdict: pass",
            )
            for job in jobs
        ]

    monkeypatch.setattr(multi_review, "run_parallel", fake_run_parallel)

    output = checkout / "review-generalist.md"
    distribution = multi_review.Distribution(
        by_cli={
            "claude": [
                multi_review.ReviewerJob("generalist", "review", output, checkout)
            ]
        },
        phase_id="review",
    )

    multi_review.run_distribution(
        distribution, checkout, config_root=tmp_path / "leader"
    )

    assert launched
    assert ("--model", "fable") == launched[0][1:3]
    assert "- model: fable" in output.read_text(encoding="utf-8")


def test_review_phases_carry_their_phase_id_into_the_launch_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`distribute()`가 phase를 세우지 않으면 `match.phase` 선언은 final-review
    밖에서 죽는다. 호출부는 도는 phase와 config root를 이미 알고 있다.
    """
    from types import SimpleNamespace

    from agent_flow.adapters import hosted

    monkeypatch.delenv("AGENT_FLOW_REVIEWERS", raising=False)
    monkeypatch.setattr(
        multi_review, "detect_available_clis", lambda: [_CLAUDE, _CODEX]
    )
    jobs = [multi_review.ReviewerJob("generalist", "review", tmp_path / "g.md", tmp_path)]

    distribution = multi_review.distribute(jobs, host="claude", phase_id="review")

    assert distribution.phase_id == "review"
    declared = _policy(
        {"provider": "claude", "model": "fable"}, match={"phase": "review"}
    )
    assert (
        _resolve(declared, cli=_CLAUDE, phase_id=distribution.phase_id).model == "fable"
    )

    # 그 phase를 아는 자리가 실제로 넘기는가. config root도 같은 자리에서 온다.
    seen: dict[str, object] = {}

    def fake_distribute(jobs, host=None, phase_id=None):
        seen["phase_id"] = phase_id
        return multi_review.Distribution(phase_id=phase_id)

    def fake_run_distribution(dist, project_root, timeout_s=600, config_root=None):
        seen["config_root"] = config_root
        return multi_review.ReviewExecution()

    monkeypatch.setattr(
        hosted, "_write_review_input_snapshot", lambda *a, **k: tmp_path / "input.md"
    )
    monkeypatch.setattr(hosted, "_reviewer_jobs", lambda *a, **k: jobs)
    monkeypatch.setattr(hosted, "distribute", fake_distribute)
    monkeypatch.setattr(hosted, "run_distribution", fake_run_distribution)
    adapter = hosted.HostedAdapter("claude")
    adapter._config_root = tmp_path / "leader"

    hosted._run_multi_review_distribution(
        SimpleNamespace(id="review", multi_review=True),
        tmp_path / "run",
        tmp_path / "checkout",
        adapter,
    )

    assert seen == {"phase_id": "review", "config_root": tmp_path / "leader"}


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: reviewer_launch.reviewer_launch_rules({"execution": {1: "x"}}),
            id="execution",
        ),
        pytest.param(
            lambda: reviewer_launch.rule_matches(
                {1: "x"}, phase_id=None, angle_id=""
            ),
            id="rule",
        ),
        pytest.param(
            lambda: reviewer_launch.rule_matches(
                {"match": {2: "y"}}, phase_id=None, angle_id=""
            ),
            id="match",
        ),
        pytest.param(
            lambda: reviewer_launch.parse_launch_candidate(
                {3: "z", "provider": "claude"}
            ),
            id="candidate",
        ),
    ],
)
def test_non_string_declaration_keys_stay_inside_the_module_error(call):
    """YAML mapping의 키는 `Any`다. `1:`이나 `on:`처럼 문자열이 아닌 키가 섞이면
    미지의 키를 세는 `sorted`/`join`이 raw `TypeError`를 냈고, 그것은 이 모듈의
    예외 계약 밖이라 호출자의 `except ReviewerLaunchError`도 CLI 핸들러도 잡지
    못한 채 traceback으로 사용자에게 닿았다.
    """
    with pytest.raises(reviewer_launch.ReviewerLaunchError, match="keys must be strings"):
        call()


def test_override_with_non_string_keys_fails_as_a_declaration_error(tmp_path: Path):
    """override loader가 `ReviewerLaunchError`로 받아 `ValueError`로 다시 세운다.

    `TypeError`로 새면 `_validate_override_execution`의 `except ReviewerLaunchError`가
    비껴가 프로필 로딩 전체가 traceback으로 끝난다.
    """
    from agent_flow.core.profiles import load_profile_payload

    profiles = tmp_path / ".agent-flow" / "profiles"
    profiles.mkdir(parents=True)
    # `on:`은 YAML 1.1에서 bool 키다. 사람이 실수로 적을 수 있는 비문자열 키다.
    (profiles / "generic.local.yaml").write_text(
        "execution:\n"
        "  reviewers:\n"
        "    - on: true\n"
        "      candidates:\n"
        "        - provider: claude\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="keys must be strings"):
        load_profile_payload("generic", tmp_path)


def test_final_review_dispatch_reads_the_shared_phase_id_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """final-review를 고르는 자리와 `match.phase`를 세우는 자리가 같은 값을 봐야
    한다. 한쪽만 리터럴로 두면 상수를 바꾼 날 선언이 조용히 죽는다.
    """
    from types import SimpleNamespace

    from agent_flow.adapters import hosted

    assert hosted.FINAL_REVIEW_PHASE_ID is multi_review.FINAL_REVIEW_PHASE_ID

    chosen: list[str] = []
    monkeypatch.setattr(
        hosted, "_write_review_input_snapshot", lambda *a, **k: tmp_path / "input.md"
    )
    monkeypatch.setattr(hosted, "_reviewer_jobs", lambda *a, **k: [])
    monkeypatch.setattr(
        hosted,
        "distribute",
        lambda jobs, host=None, phase_id=None: chosen.append("distribute")
        or multi_review.Distribution(phase_id=phase_id),
    )
    monkeypatch.setattr(
        hosted,
        "distribute_final_review",
        lambda jobs, host=None: chosen.append("final")
        or multi_review.Distribution(phase_id=multi_review.FINAL_REVIEW_PHASE_ID),
    )
    monkeypatch.setattr(
        hosted,
        "run_distribution",
        lambda *a, **k: multi_review.ReviewExecution(),
    )
    adapter = hosted.HostedAdapter("claude")

    for phase_id in (multi_review.FINAL_REVIEW_PHASE_ID, "review"):
        hosted._run_multi_review_distribution(
            SimpleNamespace(id=phase_id, multi_review=True),
            tmp_path / "run",
            tmp_path / "checkout",
            adapter,
        )

    assert chosen == ["final", "distribute"]


def test_host_and_reviewer_envelopes_are_observed_under_distinct_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """multi-review phase는 envelope를 두 번 렌더한다 — host에게 출력하는 것과
    reviewer subprocess prompt의 바탕. 둘이 같은 payload 이름으로 관측되면 trace
    독자는 host가 실제로 받은 것이 어느 쪽인지 sha를 다시 계산하지 않고는 고를 수
    없다. 관측의 목적이 "주입된 프롬프트 재현"이므로 이름이 갈려야 한다.
    """
    from types import SimpleNamespace

    from agent_flow.adapters import hosted

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    phase = SimpleNamespace(
        id="final-review",
        description="d",
        prompt="p",
        artifact=None,
        multi_review=True,
        required_markers=(),
        skills=None,
    )
    observed: list[tuple[str, str, str]] = []
    adapter = hosted.HostedAdapter("claude")
    adapter._observer = lambda phase_id, payload_name, envelope: observed.append(
        (phase_id, payload_name, envelope)
    )

    jobs = hosted._reviewer_jobs(phase, run_dir, tmp_path, adapter)
    host_envelope = adapter.render_envelope(
        phase, run_dir, tmp_path, host_hint="host guidance"
    )

    assert jobs
    names = [name for _, name, _ in observed]
    assert names == ["prompt-final-review-reviewer-base", "prompt-final-review"]
    assert {phase_id for phase_id, _, _ in observed} == {"final-review"}
    # 이름이 갈릴 뿐 아니라 실제로 다른 텍스트다 — host hint는 reviewer 바탕에 없다.
    reviewer_envelope = observed[0][2]
    assert "host guidance" in host_envelope
    assert "host guidance" not in reviewer_envelope
    assert reviewer_envelope in jobs[0].prompt
