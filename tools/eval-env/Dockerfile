# syntax=docker/dockerfile:1
# check=skip=FromPlatformFlagConstDisallowed
# agent-flow 평가 환경(#423). 저장소 루트에서 빌드한다:
#   docker build -f tools/eval-env/Dockerfile -t agent-flow-eval:local .
# 빌드 context는 tools/eval-env/Dockerfile.dockerignore가 이름을 적은 파일 네 개만 남긴다.
# 자격 증명은 이미지에 넣지 않는다. 실행 시 compose.auth.yml의 읽기 전용 mount나
# `codex login --device-auth`로만 들어온다.
# TLS를 가로채는 프록시 뒤라면 README의 「프록시 뒤에서 빌드」를 따른다(CA는 secret으로만 들어온다).
# Flutter 3.47.7의 Linux 배포본은 x64뿐이다. 다른 플랫폼에서 빌드해도 같은 바이너리가 돌도록 고정한다.
FROM --platform=linux/amd64 node:22.22.0-bookworm@sha256:20a424ecd1d2064a44e12fe287bf3dae443aab31dc5e0c0cb6c74bef9c78911c

# 버전은 런북(maintainer/improvement-history/README.md 「환경 준비」)과 이 세션의 실측에 맞춘다.
ARG CLAUDE_CODE_VERSION=2.1.308
ARG CODEX_VERSION=0.163.0
ARG PLAYWRIGHT_VERSION=1.56.1
ARG FLUTTER_VERSION=3.47.7
ARG FLUTTER_SHA256=af7455f540df951184c7953fbe3cf884766e7541dacf8571f9f582db03dd6230

COPY tools/eval-env/build/with-proxy-ca /usr/local/lib/eval-env/with-proxy-ca
# apt 패키지 버전은 고정하지 않는다(미러가 옛 버전을 지운다). 시작 집합은 base digest가 고정한다.

# apt도 https로 받는다. CONNECT만 통과시키는 프록시가 있고, 평문 저장소 경로를 남길 이유도 없다.
RUN --mount=type=secret,id=proxy_ca,required=false \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && /usr/local/lib/eval-env/with-proxy-ca sh -c '\
    apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git xz-utils unzip curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*'

# PATH는 ENV로 둔다. 로그인 셸 설정에만 두면 runner가 띄우는 자식 셸이 agent-flow·dart·flutter를 못 찾는다(#350).
ENV HOME=/home/eval \
    PUB_CACHE=/home/eval/.pub-cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    FLUTTER_SUPPRESS_ANALYTICS=true \
    PYTHONPATH=/repo/src \
    AGENT_FLOW_SKIP_CODEX_TRUST=1 \
    AGENT_FLOW_NO_UPDATE_CHECK=1 \
    EVAL_EXPECT_CLAUDE=${CLAUDE_CODE_VERSION} \
    EVAL_EXPECT_CODEX=${CODEX_VERSION} \
    EVAL_EXPECT_PLAYWRIGHT=${PLAYWRIGHT_VERSION} \
    EVAL_EXPECT_FLUTTER=${FLUTTER_VERSION} \
    PATH=/opt/venv/bin:/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:/usr/local/bin:/usr/bin:/bin

RUN --mount=type=secret,id=proxy_ca,required=false /usr/local/lib/eval-env/with-proxy-ca sh -c '\
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
      "playwright@${PLAYWRIGHT_VERSION}" \
 && npm cache clean --force \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*'

RUN --mount=type=secret,id=proxy_ca,required=false /usr/local/lib/eval-env/with-proxy-ca sh -c '\
    curl -fsSL -o /tmp/flutter.tar.xz \
      "https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz" \
 && echo "${FLUTTER_SHA256}  /tmp/flutter.tar.xz" | sha256sum -c - \
 && tar -xJf /tmp/flutter.tar.xz -C /opt \
 && rm /tmp/flutter.tar.xz' \
 && git config --system --add safe.directory /opt/flutter \
 && git config --system --add safe.directory /repo

# 평가기 의존성만 설치한다. 저장소는 실행 시 /repo로 mount하고 PYTHONPATH로 읽는다.
# 이미지에 소스를 굽지 않아야 평가기가 mount한 작업 트리와 어긋나지 않는다. 버전은 lock 파일이 고정한다.
COPY tools/eval-env/requirements.lock /tmp/requirements.lock
RUN --mount=type=secret,id=proxy_ca,required=false python3 -m venv /opt/venv \
 && /usr/local/lib/eval-env/with-proxy-ca /opt/venv/bin/python -m pip install --no-cache-dir -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

COPY tools/eval-env/bin/agent-flow tools/eval-env/bin/eval-env-check /usr/local/bin/

# 쓰기 가능한 HOME을 가진 비 root 사용자. Dart·Flutter 분석 설정은 이 HOME에 쓴다(#352).
# precache는 root로 받고(빌드 프록시 CA가 root 신뢰 저장소에만 있다) 소유권을 넘긴다.
# 인증 파일을 mount할 자리(.codex·.claude)를 미리 eval 소유로 만든다. 없으면 Docker가 root 소유로 만들어
# Codex가 auth.json 옆에 세션 기록을 쓰지 못한다.
RUN useradd --create-home --home-dir /home/eval --uid 1001 eval \
 && mkdir -p /repo /scratch /home/eval/.codex /home/eval/.claude
RUN --mount=type=secret,id=proxy_ca,required=false /usr/local/lib/eval-env/with-proxy-ca sh -c '\
    flutter config --no-analytics \
 && dart --disable-analytics \
 && flutter precache --universal' \
 && chown -R eval:eval /opt/flutter /home/eval /repo /scratch
USER eval
# 분석 설정은 사용자 HOME 기준이다. eval 사용자로 한 번 더 꺼서 그 HOME에 남긴다.
RUN flutter config --no-analytics && dart --disable-analytics

WORKDIR /repo
ENTRYPOINT ["eval-env-check", "--then"]
CMD ["bash"]
