'use client'

import { useState, useCallback, useRef, type WheelEvent, type MouseEvent } from 'react'

interface Transform {
  x: number
  y: number
  scale: number
}

export function useCanvasInteraction() {
  const [transform, setTransform] = useState<Transform>({ x: 0, y: 0, scale: 1 })
  const isPanning = useRef(false)
  const lastMouse = useRef({ x: 0, y: 0 })

  const handleWheel = useCallback((e: WheelEvent<SVGSVGElement>) => {
    e.preventDefault()
    const scaleFactor = e.deltaY > 0 ? 0.95 : 1.05
    setTransform((prev) => {
      const newScale = Math.max(0.3, Math.min(3, prev.scale * scaleFactor))
      // 마우스 위치 기준 줌
      const rect = (e.target as SVGElement).closest('svg')?.getBoundingClientRect()
      if (!rect) return { ...prev, scale: newScale }
      const mx = e.clientX - rect.left
      const my = e.clientY - rect.top
      const dx = (mx - prev.x) * (1 - scaleFactor)
      const dy = (my - prev.y) * (1 - scaleFactor)
      return { x: prev.x + dx, y: prev.y + dy, scale: newScale }
    })
  }, [])

  const handleMouseDown = useCallback((e: MouseEvent<SVGSVGElement>) => {
    if (e.button === 0) {
      isPanning.current = true
      lastMouse.current = { x: e.clientX, y: e.clientY }
    }
  }, [])

  const handleMouseMove = useCallback((e: MouseEvent<SVGSVGElement>) => {
    if (!isPanning.current) return
    const dx = e.clientX - lastMouse.current.x
    const dy = e.clientY - lastMouse.current.y
    lastMouse.current = { x: e.clientX, y: e.clientY }
    setTransform((prev) => ({ ...prev, x: prev.x + dx, y: prev.y + dy }))
  }, [])

  const handleMouseUp = useCallback(() => {
    isPanning.current = false
  }, [])

  const resetView = useCallback(() => {
    setTransform({ x: 0, y: 0, scale: 1 })
  }, [])

  return {
    transform,
    handleWheel,
    handleMouseDown,
    handleMouseMove,
    handleMouseUp,
    resetView,
  }
}
