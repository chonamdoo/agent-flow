import { getEventStore } from '@/lib/event-store'
import { requireAuth } from '@/lib/api-helpers'

export const dynamic = 'force-dynamic'

export async function GET(request: Request) {
  const authResult = await requireAuth()
  if (!authResult.ok) return authResult.response

  const store = getEventStore()
  const { searchParams } = new URL(request.url)
  const templateIdParam = searchParams.get('templateId')

  // templateId는 영숫자/언더스코어/대시만 허용 (workflow-templates 키만 매칭)
  const templateId =
    templateIdParam && /^[a-zA-Z0-9_-]{1,64}$/.test(templateIdParam)
      ? templateIdParam
      : undefined

  const encoder = new TextEncoder()
  let unsubscribe: (() => void) | null = null

  const stream = new ReadableStream({
    start(controller) {
      store.startSimulation(templateId)

      const existing = store.getEvents()
      for (const event of existing) {
        const data = JSON.stringify(event)
        controller.enqueue(encoder.encode(`data: ${data}\n\n`))
      }

      unsubscribe = store.subscribe((event) => {
        try {
          const data = JSON.stringify(event)
          controller.enqueue(encoder.encode(`data: ${data}\n\n`))
        } catch {
          // 스트림 종료됨
        }
      })
    },
    cancel() {
      unsubscribe?.()
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
    },
  })
}
