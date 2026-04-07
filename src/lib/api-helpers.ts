import { auth } from './auth'
import { loadProjects, type Project } from './project-store'

/**
 * API 라우트 공통 보안 헬퍼.
 * 모든 변경/조회 API는 requireAuth → requireProject 순으로 호출.
 */

// ──────────────────────────────────────────
// 1. 인증
// ──────────────────────────────────────────

export interface AuthedSession {
  userEmail: string
}

export type AuthResult =
  | { ok: true; session: AuthedSession }
  | { ok: false; response: Response }

/**
 * NextAuth 세션을 강제. 없으면 401 Response를 ok:false로 반환.
 */
export async function requireAuth(): Promise<AuthResult> {
  const session = await auth()
  const email = session?.user?.email
  if (!email) {
    return {
      ok: false,
      response: Response.json({ error: 'unauthorized' }, { status: 401 }),
    }
  }
  return { ok: true, session: { userEmail: email } }
}

// ──────────────────────────────────────────
// 2. 프로젝트 검증 (등록된 것만 허용)
// ──────────────────────────────────────────

/**
 * id 필드 형식 검증. path traversal / 셸 메타문자 차단.
 */
export function isValidProjectId(id: unknown): id is string {
  return typeof id === 'string' && /^[a-zA-Z0-9_-]{1,64}$/.test(id)
}

/**
 * 절대경로 검증. `..`, 셸 메타문자, 비절대경로 차단.
 * 운영체제의 path 모듈에 의존하지 않고 보수적으로 판단.
 */
export function isValidAbsolutePath(p: unknown): p is string {
  if (typeof p !== 'string' || p.length === 0 || p.length > 1024) return false
  if (p.includes('..')) return false
  if (/[`$;|&<>\n\r\0]/.test(p)) return false
  // POSIX 절대경로 또는 Windows 절대경로
  return p.startsWith('/') || /^[a-zA-Z]:[\\/]/.test(p)
}

export type ProjectResult =
  | { ok: true; project: Project }
  | { ok: false; response: Response }

/**
 * 등록된 프로젝트인지 확인. 클라이언트가 보낸 path는 절대 신뢰하지 않고
 * 서버 저장소의 정식 경로만 사용.
 */
export function requireProject(projectId: unknown): ProjectResult {
  if (!isValidProjectId(projectId)) {
    return {
      ok: false,
      response: Response.json(
        { error: 'invalid projectId' },
        { status: 400 },
      ),
    }
  }
  const project = loadProjects().find((p) => p.id === projectId)
  if (!project) {
    return {
      ok: false,
      response: Response.json({ error: 'project not found' }, { status: 404 }),
    }
  }
  return { ok: true, project }
}

// ──────────────────────────────────────────
// 3. 동시 실행 제한 (in-memory mutex)
// ──────────────────────────────────────────

const runningCounts = new Map<string, number>()
const MAX_CONCURRENT_PER_USER = 1

export function tryAcquireRunSlot(userEmail: string): boolean {
  const current = runningCounts.get(userEmail) ?? 0
  if (current >= MAX_CONCURRENT_PER_USER) return false
  runningCounts.set(userEmail, current + 1)
  return true
}

export function releaseRunSlot(userEmail: string): void {
  const current = runningCounts.get(userEmail) ?? 0
  if (current <= 1) {
    runningCounts.delete(userEmail)
  } else {
    runningCounts.set(userEmail, current - 1)
  }
}

// ──────────────────────────────────────────
// 4. 시스템 프롬프트 인젝션 방어
// ──────────────────────────────────────────

/**
 * 사용자 입력에서 시스템/사용자 마커 흉내내기를 무력화.
 * 전각 괄호로 치환하여 시각적으로는 동일하지만 토큰 비교에서 다르게 인식되게 함.
 */
export function sanitizeUserPrompt(input: unknown): string {
  if (typeof input !== 'string') return ''
  if (input.length > 50_000) {
    // 너무 긴 프롬프트는 잘라낸다 (DoS 방지)
    return sanitizeUserPrompt(input.slice(0, 50_000))
  }
  return input
    .replace(/\[시스템 지시\]/g, '［시스템 지시］')
    .replace(/\[\/시스템 지시\]/g, '［／시스템 지시］')
    .replace(/\[사용자 요청\]/g, '［사용자 요청］')
    .replace(/\[\/사용자 요청\]/g, '［／사용자 요청］')
    .replace(/<\s*system\s*>/gi, '＜system＞')
    .replace(/<\s*\/\s*system\s*>/gi, '＜/system＞')
}
