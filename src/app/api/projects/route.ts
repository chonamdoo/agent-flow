import { loadProjects, addProject, removeProject } from '@/lib/project-store'
import type { Project } from '@/lib/project-store'
import {
  requireAuth,
  isValidProjectId,
  isValidAbsolutePath,
} from '@/lib/api-helpers'
import type { Platform } from '@/lib/types'

export const dynamic = 'force-dynamic'

const ALLOWED_PLATFORMS: ReadonlySet<Platform> = new Set<Platform>([
  'web',
  'android',
  'ios',
])

function isValidName(name: unknown): name is string {
  return typeof name === 'string' && name.trim().length > 0 && name.length <= 100
}

function isValidIcon(icon: unknown): icon is string | undefined {
  if (icon === undefined) return true
  return typeof icon === 'string' && icon.length > 0 && icon.length <= 8
}

function isValidPlatform(p: unknown): p is Platform {
  return typeof p === 'string' && ALLOWED_PLATFORMS.has(p as Platform)
}

export async function GET() {
  const authResult = await requireAuth()
  if (!authResult.ok) return authResult.response

  const projects = loadProjects()
  return Response.json(projects)
}

export async function POST(request: Request) {
  const authResult = await requireAuth()
  if (!authResult.ok) return authResult.response

  let raw: unknown
  try {
    raw = await request.json()
  } catch {
    return Response.json({ error: 'invalid JSON body' }, { status: 400 })
  }

  if (typeof raw !== 'object' || raw === null) {
    return Response.json({ error: 'body must be object' }, { status: 400 })
  }

  const body = raw as Record<string, unknown>

  if (!isValidProjectId(body.id)) {
    return Response.json(
      { error: 'invalid id (alphanumeric, dash, underscore, 1-64 chars)' },
      { status: 400 },
    )
  }
  if (!isValidName(body.name)) {
    return Response.json({ error: 'invalid name' }, { status: 400 })
  }
  if (!isValidAbsolutePath(body.path)) {
    return Response.json(
      { error: 'invalid path (must be absolute, no .. or shell metacharacters)' },
      { status: 400 },
    )
  }
  if (!isValidPlatform(body.platform)) {
    return Response.json(
      { error: 'invalid platform (must be web|android|ios)' },
      { status: 400 },
    )
  }
  if (!isValidIcon(body.icon)) {
    return Response.json({ error: 'invalid icon' }, { status: 400 })
  }

  const project: Project = {
    id: body.id,
    name: body.name.trim(),
    path: body.path,
    platform: body.platform,
    icon: typeof body.icon === 'string' ? body.icon : '📁',
  }

  const projects = addProject(project)
  return Response.json(projects)
}

export async function DELETE(request: Request) {
  const authResult = await requireAuth()
  if (!authResult.ok) return authResult.response

  const { searchParams } = new URL(request.url)
  const id = searchParams.get('id')

  if (!isValidProjectId(id)) {
    return Response.json(
      { error: 'invalid id parameter' },
      { status: 400 },
    )
  }

  const projects = removeProject(id)
  return Response.json(projects)
}
