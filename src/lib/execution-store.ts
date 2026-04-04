import fs from 'fs'
import path from 'path'
import type { AgentRole } from './types'

export interface ExecutionRecord {
  id: string
  projectId: string
  userPrompt: string
  status: 'completed' | 'failed' | 'stopped'
  startedAt: number
  completedAt: number
  totalCost: number
  results: Array<{
    role: AgentRole
    status: 'success' | 'error'
    durationMs: number
    outputPreview: string // 처음 500자
  }>
}

const EXECUTIONS_DIR = path.resolve(process.cwd(), 'data', 'executions')

function ensureDir() {
  if (!fs.existsSync(EXECUTIONS_DIR)) {
    fs.mkdirSync(EXECUTIONS_DIR, { recursive: true })
  }
}

export function saveExecution(record: ExecutionRecord): void {
  ensureDir()
  const filename = `${record.projectId}_${record.startedAt}.json`
  const filepath = path.join(EXECUTIONS_DIR, filename)
  fs.writeFileSync(filepath, JSON.stringify(record, null, 2), 'utf-8')
}

export function loadExecutions(projectId?: string): ExecutionRecord[] {
  ensureDir()

  const files = fs.readdirSync(EXECUTIONS_DIR).filter((f) => f.endsWith('.json'))
  const records: ExecutionRecord[] = []

  for (const file of files) {
    if (projectId && !file.startsWith(`${projectId}_`)) continue

    try {
      const raw = fs.readFileSync(path.join(EXECUTIONS_DIR, file), 'utf-8')
      records.push(JSON.parse(raw) as ExecutionRecord)
    } catch {
      // 파싱 실패 무시
    }
  }

  // 최신순 정렬
  return records.sort((a, b) => b.startedAt - a.startedAt)
}
