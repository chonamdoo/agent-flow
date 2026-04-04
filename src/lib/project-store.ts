import fs from 'fs'
import path from 'path'
import type { Platform } from './types'

export interface Project {
  id: string
  name: string
  path: string
  platform: Platform
  icon: string
  lastRunAt?: number
  lastStatus?: 'completed' | 'failed'
}

const DATA_DIR = path.resolve(process.cwd(), 'data')
const PROJECTS_FILE = path.join(DATA_DIR, 'projects.json')

function ensureDataDir() {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true })
  }
}

export function loadProjects(): Project[] {
  ensureDataDir()
  if (!fs.existsSync(PROJECTS_FILE)) {
    return []
  }
  try {
    const raw = fs.readFileSync(PROJECTS_FILE, 'utf-8')
    return JSON.parse(raw) as Project[]
  } catch {
    return []
  }
}

export function saveProjects(projects: Project[]): void {
  ensureDataDir()
  fs.writeFileSync(PROJECTS_FILE, JSON.stringify(projects, null, 2), 'utf-8')
}

export function addProject(project: Project): Project[] {
  const projects = loadProjects()
  const existing = projects.findIndex((p) => p.id === project.id)
  if (existing >= 0) {
    projects[existing] = { ...projects[existing], ...project }
  } else {
    projects.push(project)
  }
  saveProjects(projects)
  return projects
}

export function removeProject(id: string): Project[] {
  const projects = loadProjects().filter((p) => p.id !== id)
  saveProjects(projects)
  return projects
}

export function updateProjectStatus(
  id: string,
  status: 'completed' | 'failed',
): void {
  const projects = loadProjects()
  const project = projects.find((p) => p.id === id)
  if (project) {
    project.lastRunAt = Date.now()
    project.lastStatus = status
    saveProjects(projects)
  }
}
