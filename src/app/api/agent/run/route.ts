import { spawn } from 'child_process'
import path from 'path'
import { getSystemPrompt, buildUserMessage, AGENT_MODEL_MAP, AGENT_ALLOWED_TOOLS } from '@/lib/agent-prompts'
import type { AgentRole } from '@/lib/types'

export const dynamic = 'force-dynamic'
export const maxDuration = 300

interface RunRequest {
  role: AgentRole
  userPrompt: string
  projectId: string
  projectPath?: string
  previousOutputs: Array<{ role: AgentRole; output: string }>
}

export async function POST(request: Request) {
  let body: RunRequest
  try {
    body = await request.json()
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 })
  }

  const { role, userPrompt, projectId, projectPath, previousOutputs } = body

  const systemPrompt = getSystemPrompt(role)
  if (!systemPrompt) {
    return Response.json({ error: `Unknown agent role: ${role}` }, { status: 400 })
  }

  const userMessage = buildUserMessage(role, { userPrompt, projectId, previousOutputs })
  const fullPrompt = `[시스템 지시]\n${systemPrompt}\n\n[사용자 요청]\n${userMessage}`

  const model = AGENT_MODEL_MAP[role]
  const allowedTools = AGENT_ALLOWED_TOOLS[role]
  const encoder = new TextEncoder()

  // MCP 설정 파일 경로
  const mcpConfigPath = path.resolve(process.cwd(), '.mcp.json')

  const stream = new ReadableStream({
    start(controller) {
      const startTime = Date.now()
      let fullOutput = ''

      // claude CLI 인자 구성
      const args = ['-p', fullPrompt, '--output-format', 'json']

      if (model) {
        args.push('--model', model)
      }

      // MCP 설정 전달
      args.push('--mcp-config', mcpConfigPath)

      // 에이전트별 도구 권한
      if (allowedTools) {
        args.push('--allowedTools', allowedTools.join(','))
      }

      // 프로젝트 디렉토리에서 실행 (Developer가 실제 파일 수정 가능)
      const cwd = projectPath || process.cwd()

      const proc = spawn('claude', args, {
        shell: true,
        timeout: 300000,
        cwd,
        env: { ...process.env },
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
      })
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
