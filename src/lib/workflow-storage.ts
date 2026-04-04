import type { WorkflowTemplate } from './types'
import { WORKFLOW_PRESETS } from './workflow-templates'

/**
 * 워크플로우 영속성 시스템
 *
 * 저장 위치: 각 프로젝트의 `.claude/workflows/<id>.json`
 * 세션 클리어해도 파일로 저장되어 있으므로 동일하게 동작
 *
 * 현재 구현: localStorage (브라우저 기반 MVP)
 * 향후: 파일 시스템 API로 `.claude/workflows/`에 직접 저장
 */

const STORAGE_KEY = 'agent-flow:workflows'
const PROJECT_KEY = 'agent-flow:project-workflows' // projectPath → templateId 매핑

// ──────────────────────────────────────────
// 워크플로우 저장/로드
// ──────────────────────────────────────────

export function saveWorkflow(template: WorkflowTemplate): void {
  const stored = loadAllWorkflows()
  const idx = stored.findIndex((t) => t.id === template.id)

  const toSave: WorkflowTemplate = {
    ...template,
    savedAt: Date.now(),
  }

  if (idx >= 0) {
    stored[idx] = toSave
  } else {
    stored.push(toSave)
  }

  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored))
  } catch {
    // localStorage 사용 불가 (SSR 등)
  }
}

export function loadAllWorkflows(): WorkflowTemplate[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    return JSON.parse(raw) as WorkflowTemplate[]
  } catch {
    return []
  }
}

export function loadWorkflow(id: string): WorkflowTemplate | null {
  // 프리셋에서 먼저 찾기
  const preset = WORKFLOW_PRESETS.find((t) => t.id === id)
  if (preset) return preset

  // 커스텀에서 찾기
  const stored = loadAllWorkflows()
  return stored.find((t) => t.id === id) ?? null
}

export function deleteWorkflow(id: string): void {
  const stored = loadAllWorkflows().filter((t) => t.id !== id)
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored))
  } catch {
    // ignore
  }
}

// ──────────────────────────────────────────
// 프로젝트 ↔ 워크플로우 매핑
// 프로젝트별로 어떤 워크플로우를 사용하는지 기억
// ──────────────────────────────────────────

export function setProjectWorkflow(projectPath: string, templateId: string): void {
  try {
    const raw = localStorage.getItem(PROJECT_KEY)
    const map: Record<string, string> = raw ? JSON.parse(raw) : {}
    map[projectPath] = templateId
    localStorage.setItem(PROJECT_KEY, JSON.stringify(map))
  } catch {
    // ignore
  }
}

export function getProjectWorkflow(projectPath: string): string | null {
  try {
    const raw = localStorage.getItem(PROJECT_KEY)
    if (!raw) return null
    const map: Record<string, string> = JSON.parse(raw)
    return map[projectPath] ?? null
  } catch {
    return null
  }
}

// ──────────────────────────────────────────
// 전체 워크플로우 목록 (프리셋 + 커스텀)
// ──────────────────────────────────────────

export function getAllTemplates(): WorkflowTemplate[] {
  const custom = loadAllWorkflows()
  // 프리셋은 항상 포함, 커스텀은 중복 제거
  const customIds = new Set(custom.map((t) => t.id))
  const presets = WORKFLOW_PRESETS.filter((t) => !customIds.has(t.id))
  return [...presets, ...custom]
}
