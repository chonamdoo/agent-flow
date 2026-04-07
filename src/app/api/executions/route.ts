import { loadExecutions } from '@/lib/execution-store'
import { loadProjects } from '@/lib/project-store'
import { requireAuth, requireProject } from '@/lib/api-helpers'

export const dynamic = 'force-dynamic'

export async function GET(request: Request) {
  const authResult = await requireAuth()
  if (!authResult.ok) return authResult.response

  const { searchParams } = new URL(request.url)
  const projectIdParam = searchParams.get('projectId')

  // 명시적 projectId가 있으면 등록 여부 검증 후 해당 기록만 반환.
  if (projectIdParam !== null && projectIdParam !== '') {
    const projectResult = requireProject(projectIdParam)
    if (!projectResult.ok) return projectResult.response
    return Response.json(loadExecutions(projectResult.project.id))
  }

  // projectId 미지정: 등록된 프로젝트들의 기록만 반환.
  // 디스크에 남은 고아 실행 파일(삭제된 프로젝트 등)은 제외하여
  // 의도치 않은 정보 노출을 방지한다.
  const ownedIds = new Set(loadProjects().map((p) => p.id))
  const all = loadExecutions()
  const filtered = all.filter((e) => ownedIds.has(e.projectId))
  return Response.json(filtered)
}
