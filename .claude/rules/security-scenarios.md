# Security Scenarios — Agent Flow

루트 `.claude/rules/security.md`의 Vibecoding 3대 + Protected Pipeline을 이 프로젝트에 매핑.

스택: Next.js 16 + Claude Code CLI(child_process spawn) + 파일 기반 저장(`data/projects.json`, `data/executions/`)

**특이점**: Supabase를 쓰지 않는다. 대신 **CLI spawn 비용 제어**가 핵심이다 (Max 구독으로 호출당 비용은 0이지만, 다른 SaaS와 결합하거나 외부 키 도입 시 즉시 위험).

---

## Layer 1 — 데이터 (파일 기반 저장 + 권한)

Supabase RLS가 없으므로 다른 형태로 권한을 강제해야 함.

### 파일 시스템 권한 분리

| 보호해야 할 정보 | 위치 | 접근 권한 |
|-----------------|------|----------|
| 프로젝트 메타데이터 | `data/projects.json` | 인증된 사용자만 (Google OAuth 도입 시) |
| 실행 기록 (토큰/비용 포함) | `data/executions/<id>.json` | 본인 실행만. 향후 멀티유저 시 user_id 필드 필수 |
| Claude Code 시스템 프롬프트 | `src/lib/agent-prompts.ts` | 빌드 시 컴파일 — 클라이언트 노출 금지 |
| MCP 설정 | `.mcp.json` | 외부 키 포함 시 `.gitignore` |

### 멀티유저 전환 시 (Google OAuth 추가 후) 필수 작업

1. `data/projects.json` 단일 파일 → 사용자별 디렉토리: `data/users/<user_id>/projects.json`
2. API 라우트마다 `getUser()` 검증 후 자신의 디렉토리만 접근
3. 파일 경로에 사용자 입력 절대 사용 금지 (path traversal: `../../etc/passwd`)
4. JSON 스키마 Zod 검증 — 파싱 에러 시 안전한 폴백

### 시나리오 체크

| 시나리오 | 기대 결과 |
|---------|----------|
| GET `/api/projects?id=../other-user/secret.json` | 400 (path traversal 차단) |
| POST `/api/projects` body에 `user_id: '다른 사람'` | 401 또는 무시 (auth 기반으로 덮어쓰기) |
| 인증 없이 `/api/agent/run` 호출 | 401 |
| 실행 기록에 다른 사용자의 토큰 사용량이 보임 | 응답에서 마스킹 또는 0 처리 |

---

## Layer 2 — 통신 (CLI spawn 보안 + 향후 외부 키)

### CLI Command Injection 방지 (현재 가장 큰 위험)

`spawn('cmd.exe', ['/c', 'claude', ...args])` 패턴은 사용자 입력이 args로 들어가면 **즉시 RCE**.

**필수 규칙**:
1. **`shell: true` 절대 금지** — 현재 코드 점검 필요
2. 인자 배열 형태로만 전달 (문자열 보간 금지)
3. 사용자 프롬프트는 stdin으로 전달, args가 아닌 stdin으로
4. 허용 플래그 화이트리스트:
   ```typescript
   const ALLOWED_FLAGS = new Set([
     '--output-format', '--print', '--allowed-tools',
     '--disallowed-tools', '--max-turns'
   ])
   ```
5. 사용자 입력에서 ``` ` ``` , `$`, `;`, `|`, `&`, `>` 같은 셸 메타문자 검증

### 시스템 프롬프트 인젝션 방지

사용자 프롬프트가 에이전트 시스템 프롬프트를 덮어쓰지 못하게:
1. 시스템 프롬프트 끝에 명확한 경계 토큰: `--- USER PROMPT BELOW (UNTRUSTED) ---`
2. 사용자 입력에서 다음 패턴 감지 시 차단 또는 escape:
   - "ignore previous instructions"
   - "system:", "assistant:" 형태
   - `</system>` 같은 태그
3. 에이전트별 도구 권한은 코드에서 강제 — 사용자 입력으로 변경 불가

### 향후 외부 API 추가 시

| API | 백엔드 프록시 | 환경변수 |
|-----|-------------|---------|
| OpenAI / Anthropic 직접 (Max 구독 외 모델) | `/api/llm/[provider]` | `OPENAI_API_KEY` 등 (서버 전용) |
| GitHub (PR/이슈 자동화) | `/api/github/*` | `GITHUB_TOKEN` |
| Slack 알림 | `/api/notify/slack` | `SLACK_WEBHOOK_URL` |

### `NEXT_PUBLIC_*` 화이트리스트

| 변수 | 허용 |
|------|------|
| `NEXT_PUBLIC_SITE_URL` | ✅ |
| `NEXT_PUBLIC_APP_NAME` | ✅ |
| `ANTHROPIC_API_KEY` | ❌ Max CLI 사용 시엔 불필요. 도입 시 서버 전용 |
| `GITHUB_TOKEN` | ❌ |

---

## Layer 3 — 방어 (Rate Limit + 비용 캡)

### CLI 실행 Rate Limit (필수)

Max 구독이라 호출당 비용은 0이지만:
- 무한 루프 / 폭주하는 오케스트레이터가 컴퓨터 리소스 고갈
- 향후 종량제 모델 도입 시 즉시 비용 폭탄

| 엔드포인트 | 사용자 분당 | 사용자 시간당 | IP 분당 | 동시 실행 |
|----------|------------|--------------|---------|----------|
| `/api/agent/run` | 3 | 30 | 5 | 사용자당 1개 |
| `/api/projects` (POST) | 10 | 100 | 20 | - |
| `/api/executions` (GET) | 30 | - | 50 | - |

### 오케스트레이터 안전장치

`runWorkflow`에서 다음을 강제:
1. **REJECT 루프 상한 3회** (이미 설계되어 있음 — 코드에서 강제 검증)
2. 전체 실행 시간 상한 (예: 30분) — 초과 시 SIGTERM
3. 실행당 토큰 사용량 캡 — 초과 시 중단
4. 동시 실행 제한 — 사용자당 1개 (Redis 또는 in-memory mutex)
5. CLI 프로세스 timeout — 단계당 10분

### 클라우드 예산 하드 캡

- [ ] **Vercel** (배포 시): Spending Limit
- [ ] (도입 시) **Anthropic API**: 월 한도 + 알림
- [ ] (도입 시) **OpenAI**: Hard Limit
- [ ] **Railway/Render** (배포 시): 사용량 알림

### Chrome DevTools MCP 권한

- `developer` 에이전트만 접근 (이미 설계됨)
- 자동화 대상 URL 화이트리스트 검증
- 시크릿 입력 자동화 금지 (사용자 직접 입력)

---

## Definition of Done

1. **"사용자가 다른 사용자의 실행 기록(토큰 사용량 포함)을 볼 수 있는가?"**
   - `/api/executions?id=other_user_execution` 호출 → 403/404
   - 응답에서 다른 사용자 데이터 노출 0건

2. **"Network 탭에 Anthropic/OpenAI API Key가 보이는가?"**
   - 모든 클라이언트 요청 검사 → 시크릿 0건
   - 빌드 산출물 검색 → 0건
   - `agent-prompts.ts`의 시스템 프롬프트가 클라이언트 번들에 포함되었는지 검사

3. **"공격자가 `/api/agent/run`을 1만 번 호출하거나 악성 프롬프트로 무한 루프를 유도하면 시스템이 안전한가?"**
   - Rate Limit으로 차단
   - REJECT 루프 3회 강제
   - 동시 실행 1개 제한
   - 단계 timeout 10분
   - 컴퓨터 CPU/메모리 안정 유지

추가 — Agent Flow 특화 4번째 질문:
4. **"악성 사용자 프롬프트가 셸 명령을 주입하거나 시스템 프롬프트를 우회할 수 있는가?"**
   - `spawn` args에 사용자 입력 raw 주입 0건
   - `shell: true` 0건
   - 시스템 프롬프트 인젝션 패턴 감지 0건 통과

---

## 우선순위

1. **HIGH** — 현재 `spawn` 호출 코드에 `shell: true` 또는 인자 보간 있는지 즉시 점검
2. **HIGH** — REJECT 루프/timeout/동시 실행 제한이 코드에서 강제되는지 확인
3. **HIGH** — 시스템 프롬프트가 클라이언트 번들에 노출되지 않는지 확인
4. **MEDIUM** — Google OAuth 추가 시 멀티유저 파일 분리 + path traversal 방어
5. **MEDIUM** — Rate Limit 도입 (`@upstash/ratelimit` + Upstash Redis)
6. **LOW** — 외부 API/MCP 추가 시 백엔드 프록시 패턴 준수
