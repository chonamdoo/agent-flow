'use client'

import { useRef, useEffect, useState, useCallback } from 'react'
import type { AgentNodeData, FlowEdge, SimNode } from '@/lib/types'
import { useFlowSimulation } from '@/hooks/useFlowSimulation'
import { useCanvasInteraction } from '@/hooks/useCanvasInteraction'
import AgentNode from './AgentNode'
import EdgeLine from './EdgeLine'

interface FlowCanvasProps {
  agents: AgentNodeData[]
  edges: FlowEdge[]
  selectedAgentId: string | null
  onSelectAgent: (id: string | null) => void
  onUpdateNodeState: ((agentId: string, updates: Partial<AgentNodeData>) => void) | null
}

export default function FlowCanvas({
  agents,
  edges,
  selectedAgentId,
  onSelectAgent,
  onUpdateNodeState,
}: FlowCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 })

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setDimensions({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        })
      }
    })

    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  const { nodes, updateNodeState } = useFlowSimulation(
    agents,
    edges,
    dimensions.width,
    dimensions.height,
  )

  // 외부에서 노드 상태 업데이트 함수 전달
  useEffect(() => {
    if (onUpdateNodeState) return
  }, [onUpdateNodeState, updateNodeState])

  const {
    transform,
    handleWheel,
    handleMouseDown,
    handleMouseMove,
    handleMouseUp,
    resetView,
  } = useCanvasInteraction()

  const handleNodeClick = useCallback(
    (id: string) => {
      onSelectAgent(selectedAgentId === id ? null : id)
    },
    [selectedAgentId, onSelectAgent],
  )

  const handleBackgroundClick = useCallback(() => {
    onSelectAgent(null)
  }, [onSelectAgent])

  // 노드 맵 (에지 렌더용)
  const nodeMap = new Map<string, SimNode>()
  for (const n of nodes) {
    nodeMap.set(n.id, n)
  }

  return (
    <div ref={containerRef} className="absolute inset-0 overflow-hidden bg-[#09090b]">
      {/* 캔버스 배경 그리드 */}
      <svg
        width={dimensions.width}
        height={dimensions.height}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onClick={handleBackgroundClick}
        style={{ cursor: 'grab' }}
      >
        <defs>
          {/* 그리드 패턴 */}
          <pattern
            id="grid"
            width={40 * transform.scale}
            height={40 * transform.scale}
            patternUnits="userSpaceOnUse"
            x={transform.x % (40 * transform.scale)}
            y={transform.y % (40 * transform.scale)}
          >
            <circle
              cx={1}
              cy={1}
              r={0.5}
              fill="#27272a"
              opacity={0.5}
            />
          </pattern>

          {/* 글로우 필터 */}
          <filter id="glow">
            <feGaussianBlur stdDeviation="3" result="coloredBlur" />
            <feMerge>
              <feMergeNode in="coloredBlur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* 그리드 배경 */}
        <rect
          width={dimensions.width}
          height={dimensions.height}
          fill="url(#grid)"
        />

        {/* 트랜스폼 그룹 */}
        <g transform={`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`}>
          {/* 에지 레이어 */}
          {edges.map((edge) => {
            const source = nodeMap.get(edge.source)
            const target = nodeMap.get(edge.target)
            if (!source || !target) return null
            return (
              <EdgeLine
                key={edge.id}
                source={source}
                target={target}
                label={edge.label}
                active={edge.active}
              />
            )
          })}

          {/* 노드 레이어 */}
          {nodes.map((node) => (
            <AgentNode
              key={node.id}
              node={node}
              isSelected={selectedAgentId === node.id}
              onClick={handleNodeClick}
            />
          ))}
        </g>
      </svg>

      {/* 줌 컨트롤 */}
      <div className="absolute bottom-4 right-4 flex items-center gap-1">
        <button
          onClick={() =>
            handleWheel({
              deltaY: -100,
              clientX: dimensions.width / 2,
              clientY: dimensions.height / 2,
              target: containerRef.current?.querySelector('svg'),
              preventDefault: () => {},
            } as unknown as React.WheelEvent<SVGSVGElement>)
          }
          className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-800/80 text-zinc-400 hover:bg-zinc-700 hover:text-white transition-colors"
        >
          +
        </button>
        <button
          onClick={resetView}
          className="flex h-8 items-center justify-center rounded-md bg-zinc-800/80 px-2 text-xs text-zinc-400 hover:bg-zinc-700 hover:text-white font-mono transition-colors"
        >
          {Math.round(transform.scale * 100)}%
        </button>
        <button
          onClick={() =>
            handleWheel({
              deltaY: 100,
              clientX: dimensions.width / 2,
              clientY: dimensions.height / 2,
              target: containerRef.current?.querySelector('svg'),
              preventDefault: () => {},
            } as unknown as React.WheelEvent<SVGSVGElement>)
          }
          className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-800/80 text-zinc-400 hover:bg-zinc-700 hover:text-white transition-colors"
        >
          -
        </button>
      </div>

      {/* 범례 */}
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-4 rounded-lg bg-zinc-900/80 border border-zinc-800 px-4 py-2">
        {[
          { color: '#34d399', label: '활성' },
          { color: '#52525b', label: '대기' },
          { color: '#22c55e', label: '완료' },
          { color: '#ef4444', label: 'HITL' },
        ].map((item) => (
          <div key={item.label} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: item.color }}
            />
            <span className="text-xs text-zinc-500">{item.label}</span>
          </div>
        ))}
        <div className="flex items-center gap-1.5 border-l border-zinc-700 pl-4">
          <span className="text-xs text-zinc-600">Agent Team</span>
        </div>
      </div>
    </div>
  )
}
