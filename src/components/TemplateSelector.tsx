'use client'

import { useState } from 'react'
import type { WorkflowTemplate, Platform } from '@/lib/types'
import { PLATFORM_META } from '@/lib/types'
import { WORKFLOW_PRESETS } from '@/lib/workflow-templates'

interface TemplateSelectorProps {
  onSelect: (template: WorkflowTemplate) => void
}

export default function TemplateSelector({ onSelect }: TemplateSelectorProps) {
  const [selectedPlatform, setSelectedPlatform] = useState<Platform | null>(null)
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  const platforms: Platform[] = ['web', 'android', 'ios']

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
      <div className="w-full max-w-3xl rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
        {/* 헤더 */}
        <div className="border-b border-zinc-800 px-8 py-6">
          <h1 className="text-xl font-bold text-white">워크플로우 선택</h1>
          <p className="mt-1 text-sm text-zinc-500">
            플랫폼별 프리셋을 선택하거나 커스텀 워크플로우를 만드세요
          </p>
        </div>

        {/* 플랫폼 탭 */}
        <div className="flex border-b border-zinc-800">
          {platforms.map((p) => {
            const meta = PLATFORM_META[p]
            const isActive = selectedPlatform === p
            return (
              <button
                key={p}
                onClick={() => setSelectedPlatform(isActive ? null : p)}
                className={`flex-1 px-4 py-3 text-sm font-medium transition-colors border-b-2 ${
                  isActive
                    ? 'border-emerald-500 text-white bg-zinc-900/50'
                    : 'border-transparent text-zinc-500 hover:text-zinc-300 hover:bg-zinc-900/30'
                }`}
              >
                <span className="mr-2">{meta.icon}</span>
                {meta.label}
              </button>
            )
          })}
        </div>

        {/* 템플릿 카드 */}
        <div className="grid grid-cols-1 gap-4 p-6">
          {WORKFLOW_PRESETS
            .filter((t) => !selectedPlatform || t.platform === selectedPlatform)
            .map((template) => {
              const platMeta = PLATFORM_META[template.platform]
              const isHovered = hoveredId === template.id
              return (
                <button
                  key={template.id}
                  onClick={() => onSelect(template)}
                  onMouseEnter={() => setHoveredId(template.id)}
                  onMouseLeave={() => setHoveredId(null)}
                  className={`group relative w-full rounded-xl border p-5 text-left transition-all ${
                    isHovered
                      ? 'border-emerald-500/50 bg-zinc-900 shadow-lg shadow-emerald-500/5'
                      : 'border-zinc-800 bg-zinc-900/50 hover:border-zinc-700'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <span
                          className="inline-flex h-6 w-6 items-center justify-center rounded-md text-xs"
                          style={{ backgroundColor: `${platMeta.color}20`, color: platMeta.color }}
                        >
                          {platMeta.icon}
                        </span>
                        <h3 className="text-sm font-semibold text-white">{template.name}</h3>
                        <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">
                          프리셋
                        </span>
                      </div>
                      <p className="text-xs text-zinc-500 leading-relaxed mt-1">
                        {template.description}
                      </p>
                    </div>

                    {/* 에이전트 미니 프리뷰 */}
                    <div className="ml-4 flex -space-x-2">
                      {template.agents.slice(0, 5).map((agent) => (
                        <span
                          key={agent.id}
                          className="inline-flex h-7 w-7 items-center justify-center rounded-full border-2 border-zinc-950 text-xs"
                          style={{ backgroundColor: `${agent.color}30` }}
                          title={agent.name}
                        >
                          {PLATFORM_META[template.platform] && (
                            <span style={{ color: agent.color, fontSize: 10 }}>
                              {agent.role === 'orchestrator' ? '●' : agent.name.charAt(0)}
                            </span>
                          )}
                        </span>
                      ))}
                      {template.agents.length > 5 && (
                        <span className="inline-flex h-7 w-7 items-center justify-center rounded-full border-2 border-zinc-950 bg-zinc-800 text-[10px] text-zinc-400">
                          +{template.agents.length - 5}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* 하단 메타 */}
                  <div className="mt-3 flex items-center gap-4 text-[11px] text-zinc-600">
                    <span>{template.agents.length} 에이전트</span>
                    <span>{template.edges.length} 연결</span>
                    <span>{template.agentTeams.length} 팀</span>
                    <span className="text-cyan-600">검증루프 {template.verificationLoop.maxIterations}회</span>
                  </div>

                  {/* 호버 화살표 */}
                  <span
                    className={`absolute right-4 top-1/2 -translate-y-1/2 text-emerald-400 transition-opacity ${
                      isHovered ? 'opacity-100' : 'opacity-0'
                    }`}
                  >
                    →
                  </span>
                </button>
              )
            })}

          {/* 커스텀 워크플로우 버튼 */}
          <button
            className="w-full rounded-xl border border-dashed border-zinc-700 p-5 text-left transition-colors hover:border-zinc-600 hover:bg-zinc-900/30"
            onClick={() => {
              // 빈 커스텀 템플릿으로 시작
              const custom: WorkflowTemplate = {
                id: `custom-${Date.now()}`,
                name: '커스텀 워크플로우',
                platform: selectedPlatform ?? 'web',
                description: '나만의 에이전트 워크플로우를 설계하세요',
                isPreset: false,
                agents: [
                  {
                    id: 'orchestrator',
                    name: 'Orchestrator',
                    role: 'orchestrator',
                    color: '#f43f5e',
                    description: '워크플로우 오케스트레이터',
                    systemPrompt: '',
                    tools: ['Agent'],
                    model: 'opus',
                  },
                ],
                edges: [],
                agentTeams: [],
                verificationLoop: {
                  enabled: true,
                  maxIterations: 3,
                  codeAgentId: 'orchestrator',
                  testCommand: 'npm run build && npm test',
                  buildCommand: 'npm run build',
                },
              }
              onSelect(custom)
            }}
          >
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-zinc-800 text-lg text-zinc-500">
                +
              </span>
              <div>
                <h3 className="text-sm font-semibold text-zinc-400">
                  커스텀 워크플로우 만들기
                </h3>
                <p className="text-xs text-zinc-600">
                  에이전트를 직접 정의하고 연결을 설계하세요
                </p>
              </div>
            </div>
          </button>
        </div>
      </div>
    </div>
  )
}
