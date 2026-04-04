'use client'

import type { SimNode } from '@/lib/types'
import { AGENT_STATE_COLORS, AGENT_ROLE_META } from '@/lib/types'

interface AgentNodeProps {
  node: SimNode
  isSelected: boolean
  onClick: (id: string) => void
}

export default function AgentNode({ node, isSelected, onClick }: AgentNodeProps) {
  const stateColor = AGENT_STATE_COLORS[node.state]
  const meta = AGENT_ROLE_META[node.role]
  const isRunning = node.state === 'running' || node.state === 'thinking' || node.state === 'tool_calling'
  const isOrchestrator = node.role === 'orchestrator'
  const radius = isOrchestrator ? 36 : 28

  return (
    <g
      transform={`translate(${node.x}, ${node.y})`}
      onClick={() => onClick(node.id)}
      style={{ cursor: 'pointer' }}
    >
      {/* 선택 링 */}
      {isSelected && (
        <circle
          r={radius + 8}
          fill="none"
          stroke="#34d399"
          strokeWidth={2}
          strokeDasharray="4 4"
          opacity={0.6}
        />
      )}

      {/* 실행 중 펄스 효과 */}
      {isRunning && (
        <>
          <circle
            r={radius}
            fill={stateColor}
            opacity={0.15}
            className="pulse-glow"
          />
          <circle
            r={radius}
            fill="none"
            stroke={stateColor}
            strokeWidth={1.5}
            opacity={0}
            className="pulse-ring"
          />
        </>
      )}

      {/* 메인 노드 원 */}
      <circle
        r={radius}
        fill="#18181b"
        stroke={node.color}
        strokeWidth={isSelected ? 3 : 2}
      />

      {/* 글로우 효과 */}
      <circle
        r={radius - 2}
        fill="none"
        stroke={stateColor}
        strokeWidth={1}
        opacity={isRunning ? 0.6 : 0.2}
      />

      {/* 아이콘 */}
      <text
        textAnchor="middle"
        dominantBaseline="central"
        fontSize={isOrchestrator ? 18 : 14}
        dy={-2}
      >
        {meta.icon}
      </text>

      {/* 이벤트 건수 뱃지 */}
      {node.eventCount > 0 && (
        <g transform={`translate(${radius - 4}, ${-radius + 4})`}>
          <rect
            x={-12}
            y={-8}
            width={24}
            height={16}
            rx={8}
            fill={node.color}
          />
          <text
            textAnchor="middle"
            dominantBaseline="central"
            fontSize={9}
            fontWeight={700}
            fill="white"
            fontFamily="var(--font-geist-mono)"
          >
            {node.eventCount}
          </text>
        </g>
      )}

      {/* 에이전트 이름 */}
      <text
        textAnchor="middle"
        y={radius + 16}
        fontSize={11}
        fontWeight={600}
        fill="white"
      >
        {node.name}
      </text>

      {/* 토큰 사용량 */}
      {node.tokensUsed > 0 && (
        <text
          textAnchor="middle"
          y={radius + 30}
          fontSize={9}
          fill="#71717a"
          fontFamily="var(--font-geist-mono)"
        >
          {(node.tokensUsed / 1000).toFixed(1)}k tokens
        </text>
      )}

      {/* 상태 인디케이터 점 */}
      <circle
        cx={radius - 6}
        cy={-radius + 6}
        r={4}
        fill={stateColor}
      />
    </g>
  )
}
