# Concise Output

agent-flow artifact, review, commit output은 짧게 쓰되 parser contract와 기술 의미를 보존한다.

## Preserve

- 코드, 명령, 경로, URL, API명, 함수명, env, 에러 문자열, 버전 숫자는 원문 유지.
- YAML/JSON key, CLI status, phase id, completion marker는 번역하거나 축약하지 않는다.
- `verdict: approve`, `verdict: request-changes`, `verdict: blocked`는 byte-preserve.
- `status: green`, `status: comments`, `status: ci-failed`, `status: pending`, `status: merged`, `status: skipped`, `status: closed`, `status: error`는 byte-preserve.
- `next_command`는 byte-preserve.

## Korean Adapter

- 사용자-facing 문장은 한국어로 쓴다.
- 코드/명령/식별자는 영어 그대로 둔다.
- 이모지 금지.
- 원시인 말투 금지. 짧은 기술 한국어만 쓴다.
- 조사는 의미가 흐려지지 않을 때만 줄인다.

## Review Findings

- 한 줄에 finding 하나만 쓴다.
- 형식: `path/to/file:L42: must-fix: 문제. 수정.`
- severity는 `must-fix`, `should-fix`, `note`만 쓴다.
- 칭찬, 배경 설명, 일반론은 생략한다.

## Commit Messages

- Conventional Commit 형식 유지.
- subject는 50자 목표, 72자 hard cap.
- body는 변경 이유가 subject만으로 불명확할 때만 쓴다.
- type/scope는 영어 유지.

## Compression Safety

- 자연어만 압축한다.
- 원본 memory/context 문서를 overwrite하지 않는다.
- 요약본을 만들 때 code block, inline code, URL, path, env, version number가 바뀌면 실패로 본다.
