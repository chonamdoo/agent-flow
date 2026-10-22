# 평가 환경 이미지 (#423)

클라우드·로컬 Linux·CI에서 같은 평가 환경을 재현한다. 세션마다 런북 절차로 CLI·SDK를 다시 설치하다 실패하던
자리(#350 자식 셸 PATH, #352 읽기 전용 HOME의 Dart analytics)를 이미지로 고정한다.

배포 kit 밖(`tools/`)에 있다. `package.json`의 `files`, `RECORDED_KIT_ASSET_TREES`, `KIT_SOURCE_DIGEST_ROOTS` 어디에도
들어가지 않는다(`tests/test_eval_env.py`).

## 고정한 것

| 도구 | 버전 | 근거 |
|---|---|---|
| base | `node:22.22.0-bookworm@sha256:20a4…911c`, `linux/amd64` (Python 3.11) | `requires-python >=3.11`. Flutter Linux 배포본이 x64뿐 |
| Claude CLI | `@anthropic-ai/claude-code@2.1.308` | 이 세션 실측 |
| Codex CLI | `@openai/codex@0.163.0` | 런북 「환경 준비」 |
| Playwright | `playwright@1.56.1` + chromium | `evals/ui_task_runner.mjs` |
| Flutter/Dart | `3.47.7` stable (archive SHA-256 검증) | 릴리스 게이트 app stack |
| Python 의존성 | `requirements.lock`(`pyproject.toml`의 `dependencies` + `dev`를 풀어 고정) | 저장소는 실행 시 mount |
| apt 패키지 | 버전 고정 안 함 | 미러가 옛 버전을 지운다. 시작 집합은 base digest가 고정 |

바꿀 때는 `Dockerfile`의 `ARG`를 고친다. 버전을 풀어 두면 같은 이미지 이름이 다른 환경을 뜻하게 된다.

## 빌드와 실행

저장소 루트에서:

```bash
docker build -f tools/eval-env/Dockerfile -t agent-flow-eval:local .
mkdir -p .eval-scratch && chmod 777 .eval-scratch          # 컨테이너 사용자(uid 1001)가 쓴다
docker compose -f tools/eval-env/compose.yml run --rm check            # 시작 점검만
docker compose -f tools/eval-env/compose.yml run --rm skill_routing    # 모델 없음, 결정적
```

모델을 부르는 서비스(`phase_eval`, `ui_task`)는 인증 mount를 겹친다:

```bash
docker compose -f tools/eval-env/compose.yml -f tools/eval-env/compose.auth.yml \
  run --rm phase_eval --kit after=/repo --stacks web --modes clean --scenario review --trials 1 \
  --review-providers codex --output /scratch/eval-web
```

- 모든 서비스는 시작할 때 `eval-env-check`를 돈다. CLI 버전, 자식 셸에서 `agent-flow`·`dart`·`flutter`가
  보이는지, HOME이 쓰기 가능한지, Dart·Flutter 분석이 꺼졌는지를 보고, 하나라도 실패하면 평가를 시작하지 않는다.
  Landlock ABI(#422)는 기록만 한다.
- 저장소는 `/repo`, scratch는 `/scratch`(`EVAL_SCRATCH`로 바꿈)에 mount한다.

## 자격 증명

이미지에는 없다. 빌드 context는 `Dockerfile.dockerignore`가 이름을 적은 파일 네 개(lock, `bin/` 두 개, `build/with-proxy-ca`)만 남긴다.

- Codex: `compose.auth.yml`이 `~/.codex/auth.json` 파일 하나만 읽기 전용으로 mount한다(`CODEX_AUTH_JSON`으로 바꿈).
  Codex는 `~/.codex`에 세션 기록을 쓰므로 폴더 전체를 읽기 전용으로 두지 않는다. 파일이 없으면 컨테이너 안에서
  `codex login --device-auth`를 쓴다(컨테이너가 끝나면 사라진다).
- Claude: `~/.claude/.credentials.json`을 읽기 전용으로 mount한다(`CLAUDE_CREDENTIALS_JSON`으로 바꿈).
- 컨테이너 사용자는 uid 1001이다. 호스트의 `0600` 파일은 그대로 mount하면 읽을 수 없다. 읽을 수 있는 사본을
  scratch에 두고 그 경로를 넘긴다. 시작 점검이 읽을 수 없는 인증 파일을 실패로 알린다.

  ```bash
  install -m 0444 ~/.codex/auth.json .eval-scratch/codex-auth.json
  CODEX_AUTH_JSON=$PWD/.eval-scratch/codex-auth.json docker compose -f tools/eval-env/compose.yml -f tools/eval-env/compose.auth.yml run --rm phase_eval --help
  ```

## 프록시 뒤에서 빌드

TLS를 가로채는 프록시 뒤(예: Claude Code 클라우드 세션)라면 프록시와 CA를 빌드에만 넘긴다. CA는 BuildKit secret으로만
들어오고, 쓰는 RUN 동안만 신뢰 저장소에 있다가 지워진다(`build/with-proxy-ca`).

```bash
P=http://127.0.0.1:36791
docker build --network host --build-arg HTTP_PROXY=$P --build-arg HTTPS_PROXY=$P \
  --build-arg http_proxy=$P --build-arg https_proxy=$P \
  --secret id=proxy_ca,src=/root/.ccr/ca-bundle.crt \
  -f tools/eval-env/Dockerfile -t agent-flow-eval:local .
```

실행할 때도 같은 프록시가 필요하면 `--network host -e HTTPS_PROXY=$P`와 CA를 읽기 전용으로 mount해 `NODE_EXTRA_CA_CERTS`·
`SSL_CERT_FILE`로 지정한다. 이 설정은 compose 기본값에 넣지 않는다(환경마다 다르다).

## 한계

- macOS `sandbox-exec`는 컨테이너 안에서 쓸 수 없다. runner multi-review까지 재려면 #422의 Linux backend가 필요하다.
  그 전까지 이 이미지는 `phase_eval`, `skill_routing`, `ui_task`, author 시나리오용이다.
