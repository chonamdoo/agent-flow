import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  forceX,
  forceY,
} from 'd3-force'
import type { SimNode, SimLink, AgentNodeData, FlowEdge } from './types'

export interface SimulationConfig {
  width: number
  height: number
  onTick: (nodes: SimNode[]) => void
}

export function createFlowSimulation(
  agents: AgentNodeData[],
  edges: FlowEdge[],
  config: SimulationConfig,
) {
  const cx = config.width / 2
  const cy = config.height / 2

  // 오케스트레이터를 중앙에 고정, 나머지는 원형 배치
  const nodes: SimNode[] = agents.map((agent, i) => {
    if (agent.role === 'orchestrator') {
      return { ...agent, x: cx, y: cy, fx: cx, fy: cy }
    }
    const angle = ((i - 1) / (agents.length - 1)) * Math.PI * 2 - Math.PI / 2
    const radius = Math.min(config.width, config.height) * 0.3
    return {
      ...agent,
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
    }
  })

  const links: SimLink[] = edges.map((edge) => ({
    source: edge.source,
    target: edge.target,
    label: edge.label,
    active: edge.active,
  }))

  const simulation = forceSimulation<SimNode>(nodes)
    .force(
      'link',
      forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance(160)
        .strength(0.2),
    )
    .force('charge', forceManyBody<SimNode>().strength(-600))
    .force('center', forceCenter<SimNode>(cx, cy).strength(0.1))
    .force('collision', forceCollide<SimNode>(70))
    .force('x', forceX<SimNode>(cx).strength(0.08))
    .force('y', forceY<SimNode>(cy).strength(0.08))
    .alphaDecay(0.02)
    .velocityDecay(0.3)
    .on('tick', () => {
      config.onTick([...nodes])
    })

  return { simulation, nodes, links }
}
