import NextAuth from 'next-auth'
import Google from 'next-auth/providers/google'

// 허용된 이메일 목록 (화이트리스트)
const ALLOWED_EMAILS = (process.env.ALLOWED_EMAILS ?? '').split(',').map((e) => e.trim()).filter(Boolean)

export const { handlers, signIn, signOut, auth } = NextAuth({
  providers: [
    Google({
      clientId: process.env.GOOGLE_CLIENT_ID ?? '',
      clientSecret: process.env.GOOGLE_CLIENT_SECRET ?? '',
    }),
  ],
  callbacks: {
    async signIn({ user }) {
      // 이메일 화이트리스트가 설정되어 있으면 체크
      if (ALLOWED_EMAILS.length > 0 && user.email) {
        return ALLOWED_EMAILS.includes(user.email)
      }
      // 화이트리스트가 비어있으면 모두 허용 (개발 모드)
      return true
    },
    async session({ session }) {
      return session
    },
  },
  pages: {
    signIn: '/login',
  },
})
