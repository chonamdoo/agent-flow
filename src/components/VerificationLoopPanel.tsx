'use client'

import type { VerificationLoop, VerificationLoopConfig } from '@/lib/types'

interface VerificationLoopPanelProps {
  loop: VerificationLoop
  config: VerificationLoopConfig
}

const STEP_META = [
  { key: 'coding', label: 'CODE', sublabel: '코드 작성', color: '#22d3ee', icon: '▶' },
  { key: 'testing', label: 'TEST', sublabel: '테스트 실행', color: '#a78bfa', icon: '↓' },
  { key: 'fixing', label: 'FIX', sublabel: '실패 수정', color: '#f87171', icon: '←' },
  { key: 'passed', label: 'PASS', sublabel: '검증 통과', color: '#22c55e', icon: '↑' },
] as const

export default function VerificationLoopPanel({ loop, config }: VerificationLoopPanelProps) {
  const activeStep = STEP_META.findIndex((s) => s.key === loop.status)

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/95 p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-bold uppercase tracking-wider text-zinc-500">
          검증 루프
        </h3>
        <span className="font-mono text-xs text-zinc-600">
          {loop.currentIteration}/{config.maxIterations}회
        </span>
      </div>

      {/* 원형 루프 다이어그램 */}
      <div className="relative mx-auto mb-3" style={{ width: 160, height: 160 }}>
        <svg width={160} height={160} viewBox="0 0 160 160">
          {/* 원형 경로 (대시) */}
          <circle
            cx={80} cy={80} r={55}
            fill="none"
            stroke="#27272a"
            strokeWidth={1.5}
            strokeDasharray="4 6"
          />

          {/* 4개 노드 */}
          {STEP_META.map((step, i) => {
            const angle = (i / 4) * Math.PI * 2 - Math.PI / 2
            const x = 80 + Math.cos(angle) * 55
            const y = 80 + Math.sin(angle) * 55
            const isActive = loop.status === step.key
            const isPassed = activeStep > i || loop.status === 'passed'

            return (
              <g key={step.key}>
                {/* 글로우 */}
                {isActive && (
                  <circle
                    cx={x} cy={y} r={20}
                    fill={step.color}
                    opacity={0.15}
                    className="pulse-glow"
                  />
                )}

                {/* 노드 */}
                <circle
                  cx={x} cy={y} r={16}
                  fill={isActive || isPassed ? `${step.color}30` : '#18181b'}
                  stroke={isActive ? step.color : isPassed ? step.color : '#3f3f46'}
                  strokeWidth={isActive ? 2 : 1}
                />

                {/* 라벨 */}
                <text
                  x={x} y={y}
                  textAnchor="middle"
                  dominantBaseline="central"
                  fontSize={8}
                  fontWeight={700}
                  fill={isActive || isPassed ? step.color : '#71717a'}
                >
                  {step.label}
                </text>
              </g>
            )
          })}

          {/* 중앙 반복 횟수 */}
          <text
            x={80} y={72}
            textAnchor="middle"
            fontSize={20}
            fontWeight={800}
            fill="#22d3ee"
            fontFamily="var(--font-geist-mono)"
          >
            {loop.currentIteration > 0 ? `${loop.currentIteration}x` : '-'}
          </text>
          <text
            x={80} y={90}
            textAnchor="middle"
            fontSize={8}
            fill="#52525b"
          >
            품질 향상
          </text>
        </svg>
      </div>

      {/* 라운드 히스토리 */}
      {loop.history.length > 0 && (
        <div className="space-y-1.5">
          {loop.history.map((round) => (
            <div
              key={round.round}
              className="flex items-center justify-between rounded-md bg-zinc-900/50 px-2.5 py-1.5"
            >
              <span className="text-[10px] text-zinc-500">
                Round {round.round}
              </span>
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                  round.testResult === 'pass'
                    ? 'bg-emerald-500/10 text-emerald-400'
                    : round.testResult === 'fail'
                      ? 'bg-red-500/10 text-red-400'
                      : 'bg-zinc-700/50 text-zinc-400'
                }`}
              >
                {round.testResult === 'pass' ? 'PASS' : round.testResult === 'fail' ? 'FAIL → FIX' : 'PENDING'}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* 명령어 정보 */}
      <div className="mt-3 space-y-1 border-t border-zinc-800 pt-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-zinc-600 w-8">TEST</span>
          <code className="text-[10px] text-zinc-500 font-mono truncate flex-1">
            {config.testCommand}
          </code>
        </div>
        {config.buildCommand && (
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-zinc-600 w-8">BUILD</span>
            <code className="text-[10px] text-zinc-500 font-mono truncate flex-1">
              {config.buildCommand}
            </code>
          </div>
        )}
        {config.lintCommand && (
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-zinc-600 w-8">LINT</span>
            <code className="text-[10px] text-zinc-500 font-mono truncate flex-1">
              {config.lintCommand}
            </code>
          </div>
        )}
      </div>
    </div>
  )
}
