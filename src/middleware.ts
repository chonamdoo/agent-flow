// Google OAuth 인증 가드 — 배포 시 활성화
// import { auth as middleware } from '@/lib/auth'
// export { middleware }

// 현재: 인증 비활성화 (로컬 개발용)
export default function middleware() {
  // pass-through
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
}
