'use client'

import { useState } from 'react'
import type { Platform } from '@/lib/types'

interface AddProjectModalProps {
  onClose: () => void
  onAdd: (project: { id: string; name: string; path: string; platform: Platform; icon: string }) => void
}

const PLATFORM_OPTIONS: Array<{ value: Platform; label: string; icon: string }> = [
  { value: 'web', label: 'Web (Next.js)', icon: '🌐' },
  { value: 'android', label: 'Android (Kotlin)', icon: '🤖' },
  { value: 'ios', label: 'iOS (Swift)', icon: '🍎' },
]

const ICON_OPTIONS = ['📁', '📈', '🦎', '🎮', '🛒', '📊', '💬', '🔧', '🎨', '🚀', '📱', '🏠']

export default function AddProjectModal({ onClose, onAdd }: AddProjectModalProps) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [platform, setPlatform] = useState<Platform>('web')
  const [icon, setIcon] = useState('📁')
  const [saving, setSaving] = useState(false)

  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')

  const handleSubmit = async () => {
    if (!name.trim() || !path.trim() || saving) return
    setSaving(true)

    try {
      const res = await fetch('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, name: name.trim(), path: path.trim(), platform, icon }),
      })
      if (res.ok) {
        onAdd({ id, name: name.trim(), path: path.trim(), platform, icon })
        onClose()
      }
    } catch {
      // 저장 실패
    }
    setSaving(false)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-zinc-800 bg-zinc-950 shadow-2xl">
        {/* 헤더 */}
        <div className="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
          <h2 className="text-sm font-semibold text-white">프로젝트 추가</h2>
          <button
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded text-zinc-500 hover:bg-zinc-800 hover:text-white transition-colors"
          >
            ×
          </button>
        </div>

        <div className="space-y-4 px-5 py-5">
          {/* 아이콘 선택 */}
          <div>
            <label className="mb-1.5 block text-[11px] font-medium text-zinc-500">아이콘</label>
            <div className="flex flex-wrap gap-1.5">
              {ICON_OPTIONS.map((ic) => (
                <button
                  key={ic}
                  onClick={() => setIcon(ic)}
                  className={`flex h-8 w-8 items-center justify-center rounded-lg text-sm transition-colors ${
                    icon === ic ? 'bg-zinc-700 ring-1 ring-emerald-500/50' : 'bg-zinc-800 hover:bg-zinc-700'
                  }`}
                >
                  {ic}
                </button>
              ))}
            </div>
          </div>

          {/* 프로젝트명 */}
          <div>
            <label className="mb-1.5 block text-[11px] font-medium text-zinc-500">프로젝트명</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My Project"
              className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-white placeholder:text-zinc-600 focus:outline-none focus:border-zinc-700"
              autoFocus
            />
            {id && <div className="mt-1 text-[10px] text-zinc-600">ID: {id}</div>}
          </div>

          {/* 경로 */}
          <div>
            <label className="mb-1.5 block text-[11px] font-medium text-zinc-500">프로젝트 경로</label>
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="C:/Users/user/Documents/my-project"
              className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-white placeholder:text-zinc-600 focus:outline-none focus:border-zinc-700 font-mono text-[12px]"
            />
          </div>

          {/* 플랫폼 */}
          <div>
            <label className="mb-1.5 block text-[11px] font-medium text-zinc-500">플랫폼</label>
            <div className="flex gap-2">
              {PLATFORM_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setPlatform(opt.value)}
                  className={`flex-1 rounded-lg border px-3 py-2 text-center text-xs transition-colors ${
                    platform === opt.value
                      ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
                      : 'border-zinc-800 bg-zinc-900 text-zinc-500 hover:border-zinc-700'
                  }`}
                >
                  <span className="text-sm">{opt.icon}</span>
                  <div className="mt-0.5 text-[10px]">{opt.label}</div>
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* 하단 버튼 */}
        <div className="flex justify-end gap-2 border-t border-zinc-800 px-5 py-4">
          <button
            onClick={onClose}
            className="rounded-lg border border-zinc-800 px-4 py-2 text-xs text-zinc-400 hover:bg-zinc-900 transition-colors"
          >
            취소
          </button>
          <button
            onClick={handleSubmit}
            disabled={!name.trim() || !path.trim() || saving}
            className={`rounded-lg px-4 py-2 text-xs font-medium transition-all ${
              name.trim() && path.trim() && !saving
                ? 'bg-emerald-500 text-white hover:bg-emerald-400'
                : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'
            }`}
          >
            {saving ? '저장 중...' : '추가'}
          </button>
        </div>
      </div>
    </div>
  )
}
