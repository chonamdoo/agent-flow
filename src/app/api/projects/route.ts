import { loadProjects, addProject, removeProject } from '@/lib/project-store'
import type { Project } from '@/lib/project-store'

export const dynamic = 'force-dynamic'

export async function GET() {
  const projects = loadProjects()
  return Response.json(projects)
}

export async function POST(request: Request) {
  try {
    const body = await request.json() as Partial<Project>

    if (!body.id || !body.name || !body.path || !body.platform) {
      return Response.json(
        { error: 'id, name, path, platform 필드가 필요합니다.' },
        { status: 400 },
      )
    }

    const project: Project = {
      id: body.id,
      name: body.name,
      path: body.path,
      platform: body.platform,
      icon: body.icon ?? '📁',
    }

    const projects = addProject(project)
    return Response.json(projects)
  } catch {
    return Response.json({ error: 'Invalid request' }, { status: 400 })
  }
}

export async function DELETE(request: Request) {
  const { searchParams } = new URL(request.url)
  const id = searchParams.get('id')

  if (!id) {
    return Response.json({ error: 'id 파라미터가 필요합니다.' }, { status: 400 })
  }

  const projects = removeProject(id)
  return Response.json(projects)
}
