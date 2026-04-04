'use client'

import type { SimNode } from '@/lib/types'

interface EdgeLineProps {
  source: SimNode
  target: SimNode
  label?: string
  active: boolean
}

export default function EdgeLine({ source, target, label, active }: EdgeLineProps) {
  const dx = target.x - source.x
  const dy = target.y - source.y
  const dist = Math.sqrt(dx * dx + dy * dy)

  if (dist === 0) return null

  // 노드 반지름만큼 시작/끝 오프셋
  const sourceR = source.role === 'orchestrator' ? 38 : 30
  const targetR = target.role === 'orchestrator' ? 38 : 30

  const sx = source.x + (dx / dist) * sourceR
  const sy = source.y + (dy / dist) * sourceR
  const tx = target.x - (dx / dist) * targetR
  const ty = target.y - (dy / dist) * targetR

  // 곡선을 위한 컨트롤 포인트 (직각 방향으로 약간 벤딩)
  const mx = (sx + tx) / 2
  const my = (sy + ty) / 2
  const perpX = -(ty - sy) * 0.1
  const perpY = (tx - sx) * 0.1
  const cx = mx + perpX
  const cy = my + perpY

  const pathD = `M ${sx} ${sy} Q ${cx} ${cy} ${tx} ${ty}`

  return (
    <g>
      {/* 메인 라인 */}
      <path
        d={pathD}
        fill="none"
        stroke={active ? '#34d399' : '#3f3f46'}
        strokeWidth={active ? 2 : 1}
        strokeDasharray={active ? '6 4' : undefined}
        className={active ? 'edge-active' : undefined}
        opacity={active ? 0.9 : 0.4}
      />

      {/* 화살표 */}
      <circle
        cx={tx}
        cy={ty}
        r={3}
        fill={active ? '#34d399' : '#3f3f46'}
        opacity={active ? 0.9 : 0.4}
      />

      {/* 라벨 */}
      {label && (
        <g transform={`translate(${cx}, ${cy})`}>
          <text
            textAnchor="middle"
            dominantBaseline="central"
            fontSize={8}
            fill="#52525b"
            paintOrder="stroke"
            stroke="#09090b"
            strokeWidth={3}
          >
            {label.length > 12 ? `${label.slice(0, 12)}...` : label}
          </text>
        </g>
      )}
    </g>
  )
}
