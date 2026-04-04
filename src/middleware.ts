export { auth as middleware } from '@/lib/auth'

export const config = {
  // 인증이 필요한 경로 (API + 페이지)
  // /login, /api/auth, 정적 파일은 제외
  matcher: [
    '/((?!login|api/auth|_next/static|_next/image|favicon.ico).*)',
  ],
}
