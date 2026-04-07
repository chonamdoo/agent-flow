import { spawn } from 'child_process'
import path from 'path'
import {
  getSystemPrompt,
  buildUserMessage,
  AGENT_MODEL_MAP,
  AGENT_ALLOWED_TOOLS,
} from '@/lib/agent-prompts'
import {
  requireAuth,
  requireProject,
  tryAcquireRunSlot,
  releaseRunSlot,
  sanitizeUserPrompt,
} from '@/lib/api-helpers'
import type { AgentRole } from '@/lib/types'

export const dynamic = 'force-dynamic'
export const maxDuration = 300

interface PreviousOutput {
  role: AgentRole
  output: string
}

const KNOWN_ROLES: ReadonlySet<AgentRole> = new Set<AgentRole>([
  'orchestrator',
  'pm_planner',
  'designer',
  'developer',
  'security_expert',
  'reviewer',
  'build_test',
  'deployer',
  'custom',
])

function isAgentRole(v: unknown): v is AgentRole {
  return typeof v === 'string' && KNOWN_ROLES.has(v as AgentRole)
}

function parsePreviousOutputs(v: unknown): PreviousOutput[] {
  if (!Array.isArray(v)) return []
  const result: PreviousOutput[] = []
  for (const item of v) {
    if (
      typeof item === 'object' &&
      item !== null &&
      isAgentRole((item as Record<string, unknown>).role) &&
      typeof (item as Record<string, unknown>).output === 'string'
    ) {
      const r = item as { role: AgentRole; output: string }
      result.push({
        role: r.role,
        // 이전 출력도 길이 제한 (DoS 방어)
        output: r.output.slice(0, 50_000),
      })
    }
  }
  return result.slice(0, 20) // 최대 20개
}

export async function POST(request: Request) {
  // 1. 인증
  const authResult = await requireAuth()
  if (!authResult.ok) return authResult.response
  const userEmail = authResult.session.userEmail

  // 2. body 파싱
  let raw: unknown
  try {
    raw = await request.json()
  } catch {
    return Response.json({ error: 'invalid JSON body' }, { status: 400 })
  }

  if (typeof raw !== 'object' || raw === null) {
    return Response.json({ error: 'body must be object' }, { status: 400 })
  }
  const body = raw as Record<string, unknown>

  // 3. 필드 검증
  if (!isAgentRole(body.role)) {
    return Response.json({ error: 'invalid role' }, { status: 400 })
  }
  const role: AgentRole = body.role

  if (typeof body.userPrompt !== 'string' || body.userPrompt.length === 0) {
    return Response.json({ error: 'userPrompt required' }, { status: 400 })
  }

  // 4. 등록된 프로젝트만 허용 (path traversal 차단의 핵심)
  const projectResult = requireProject(body.projectId)
  if (!projectResult.ok) return projectResult.response
  const project = projectResult.project

  // 5. 시스템 프롬프트 로드
  const systemPrompt = getSystemPrompt(role)
  if (!systemPrompt) {
    return Response.json(
      { error: `unknown agent role: ${role}` },
      { status: 400 },
    )
  }

  // 6. 사용자 입력 sanitize (시스템 프롬프트 인젝션 방어)
  const safeUserPrompt = sanitizeUserPrompt(body.userPrompt)
  const previousOutputs = parsePreviousOutputs(body.previousOutputs)

  const userMessage = buildUserMessage(role, {
    userPrompt: safeUserPrompt,
    projectId: project.id,
    previousOutputs,
  })
  const fullPrompt = `[시스템 지시]\n${systemPrompt}\n[/시스템 지시]\n\n[사용자 요청 - UNTRUSTED]\n${userMessage}\n[/사용자 요청]`

  const model = AGENT_MODEL_MAP[role]
  const allowedTools = AGENT_ALLOWED_TOOLS[role]

  // 7. 동시 실행 제한 (사용자당 1개)
  if (!tryAcquireRunSlot(userEmail)) {
    return Response.json(
      { error: 'another run is already in progress for this user' },
      { status: 429 },
    )
  }

  // MCP 설정 파일 경로
  const mcpConfigPath = path.resolve(process.cwd(), '.mcp.json')

  const encoder = new TextEncoder()
  let slotReleased = false
  const releaseOnce = () => {
    if (slotReleased) return
    slotReleased = true
    releaseRunSlot(userEmail)
  }

  const stream = new ReadableStream({
    start(controller) {
      const startTime = Date.now()
      let fullOutput = ''

      // claude CLI 인자 구성 (사용자 입력은 args가 아닌 -p 값으로 단일 string 전달)
      const args = ['-p', fullPrompt, '--output-format', 'json']

      if (model) {
        args.push('--model', model)
      }

      args.push('--mcp-config', mcpConfigPath)

      if (allowedTools) {
        args.push('--allowedTools', allowedTools.join(','))
      }

      // 등록된 프로젝트의 path만 사용 (클라이언트 입력 무시)
      const cwd = project.path

      const isWindows = process.platform === 'win32'
      const command = isWindows ? 'cmd.exe' : 'claude'
      const fullArgs = isWindows ? ['/c', 'claude', ...args] : args

      const spawnEnv = { ...process.env }
      delete spawnEnv.SHELL

      const proc = spawn(command, fullArgs, {
        timeout: 300000,
        cwd,
        env: spawnEnv,
        windowsHide: true,
        // shell: false 가 기본값이지만 명시
        shell: false,
      })

      proc.stdout.on('data', (chunk: Buffer) => {
        const text = chunk.toString()
        fullOutput += text
        try {
          const event = JSON.stringify({ type: 'text_delta', text })
          controller.enqueue(encoder.encode(`data: ${event}\n\n`))
        } catch {
          // 스트림 종료됨
        }
      })

      proc.stderr.on('data', (chunk: Buffer) => {
        const errText = chunk.toString()
        if (process.env.NODE_ENV === 'development') {
          console.error(`[claude-cli:${role}]`, errText)
        }
      })

      proc.on('close', (code) => {
        const durationMs = Date.now() - startTime

        if (code === 0) {
          let parsedOutput = fullOutput
          try {
            const json = JSON.parse(fullOutput)
            if (json.result) {
              parsedOutput = json.result
            } else if (json.content) {
              parsedOutput = Array.isArray(json.content)
                ? json.content
                    .filter((b: Record<string, unknown>) => b.type === 'text')
                    .map((b: Record<string, unknown>) => b.text)
                    .join('')
                : String(json.content)
            }
          } catch {
            parsedOutput = fullOutput
          }

          const completeEvent = JSON.stringify({
            type: 'complete',
            output: parsedOutput,
            usage: {
              inputTokens: 0,
              outputTokens: 0,
              model: model ?? 'claude-code-cli',
              costUsd: 0,
            },
            durationMs,
          })

          try {
            controller.enqueue(encoder.encode(`data: ${completeEvent}\n\n`))
            controller.close()
          } catch {
            // 이미 닫힘
          }
        } else {
          const errorEvent = JSON.stringify({
            type: 'error',
            error: `claude CLI exited with code ${code}. Output: ${fullOutput.slice(0, 500)}`,
            retryable: false,
          })
          try {
            controller.enqueue(encoder.encode(`data: ${errorEvent}\n\n`))
            controller.close()
          } catch {
            // 이미 닫힘
          }
        }

        releaseOnce()
      })

      proc.on('error', (err) => {
        const errorEvent = JSON.stringify({
          type: 'error',
          error: `claude CLI 실행 실패: ${err.message}. claude가 설치되어 있는지 확인하세요.`,
          retryable: false,
        })
        try {
          controller.enqueue(encoder.encode(`data: ${errorEvent}\n\n`))
          controller.close()
        } catch {
          // 이미 닫힘
        }
        releaseOnce()
      })
    },
    cancel() {
      // 클라이언트가 스트림을 끊으면 슬롯 해제 (close 이벤트가 나중에 도착해도 idempotent)
      releaseOnce()
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
    },
  })
}
