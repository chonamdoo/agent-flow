# Release notes

## Unreleased

설치본 탐색을 git root, HOME, 시작점 경계로 제한한다. 관측 hooks와 Python/OMP는
linked worktree에서 leader 설치본만 인정하며 worker나 worktree container의 사본을
사용하지 않는다. 경로의 대소문자 표기가 달라도 같은 디렉터리의 경계가 유지된다.
JS CLI는 git common root를 프로젝트 루트로 사용하는 기존 동작을 유지한다.

관리 hook의 내용과 digest가 바뀌므로 kit 업데이트 뒤 기존 프로젝트에도 재설치해야
한다. leader checkout에서 다음 명령을 실행한다.

```bash
node bin/agent-flow-kit.mjs install --hooks
```

재설치는 관리 hook과 기록된 digest, 설치된 Python runtime 및 OMP extension을 함께
갱신한다. linked worktree에서 실행하지 않는다.
