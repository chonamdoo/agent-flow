import { loadExecutions } from '@/lib/execution-store'

export const dynamic = 'force-dynamic'

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url)
  const projectId = searchParams.get('projectId') ?? undefined

  const executions = loadExecutions(projectId)
  return Response.json(executions)
}
