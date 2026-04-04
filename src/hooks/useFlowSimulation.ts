'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import type { AgentNodeData, FlowEdge, SimNode } from '@/lib/types'
import { createFlowSimulation } from '@/lib/simulation'
import type { Simulation } from 'd3-force'

export function useFlowSimulation(
  agents: AgentNodeData[],
  edges: FlowEdge[],
  width: number,
  height: number,
) {
  const [nodes, setNodes] = useState<SimNode[]>([])
  const simulationRef = useRef<Simulation<SimNode, never> | null>(null)

  useEffect(() => {
    if (width === 0 || height === 0) return

    const { simulation, nodes: initialNodes } = createFlowSimulation(
      agents,
      edges,
      {
        width,
        height,
        onTick: (updatedNodes) => {
          setNodes(updatedNodes)
        },
      },
    )

    simulationRef.current = simulation
    setNodes(initialNodes)

    return () => {
      simulation.stop()
    }
  }, [agents.length, edges.length, width, height])

  // 에이전트 상태 업데이트 시 노드 상태만 변경 (시뮬레이션 재시작 안 함)
  const updateNodeState = useCallback(
    (agentId: string, updates: Partial<AgentNodeData>) => {
      setNodes((prev) =>
        prev.map((n) => (n.id === agentId ? { ...n, ...updates } : n)),
      )
    },
    [],
  )

  return { nodes, updateNodeState }
}
