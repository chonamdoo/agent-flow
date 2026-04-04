import { getEventStore } from '@/lib/event-store'

export const dynamic = 'force-dynamic'

export async function GET(request: Request) {
  const store = getEventStore()
  const { searchParams } = new URL(request.url)
  const templateId = searchParams.get('templateId') ?? undefined

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
