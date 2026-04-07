# Design Tokens — Agent Flow

Agent Flow의 디자인 시스템 토큰. Designer/Developer/Design Reviewer 엄수.
점수 채점 기준은 루트 `.claude/rules/evaluation-criteria.md`.

---

## 색상 팔레트 (다크 전용)

### 배경
| 용도 | 클래스 |
|------|--------|
| 페이지 | `bg-[#09090b]` |
| 사이드바 | `bg-zinc-950` |
| 카드 | `bg-zinc-900/50` |
| 입력 | `bg-zinc-900` |

### 테두리 / 텍스트
| 용도 | 클래스 |
|------|--------|
| 카드 보더 | `border border-zinc-800` |
| 제목 | `text-white` |
| 라벨 | `text-zinc-400` |
| 보조 | `text-zinc-600` |
| 숫자/토큰 | `font-mono` |

### 액센트 (상태)
| 용도 | 클래스 |
|------|--------|
| 성공/실행 | `emerald-500/400` |
| 에러/중지 | `red-500/400` |
| 경고 | `amber-500/400` |
| 검수 | `blue-500/400` |

### 에이전트 색상 (브랜드 — 변경 금지)
| 에이전트 | 헥스 |
|---------|------|
| PM/Planner | `#8b5cf6` |
| Designer | `#ec4899` |
| Developer | `#34d399` |
| Security Expert | `#f59e0b` |
| Code Reviewer | `#3b82f6` |

## 컴포넌트 패턴
| 요소 | 클래스 |
|------|--------|
| 카드 | `rounded-lg border border-zinc-800 bg-zinc-900/50` |
| 입력 | `bg-zinc-900 border border-zinc-800 rounded-xl` |
| 버튼 기본 | `rounded-xl px-4 py-2 border border-zinc-800 bg-zinc-900 hover:bg-zinc-800` |
| 버튼 CTA | `rounded-xl px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-black font-semibold` |
| 칸반 컬럼 헤더 | `text-zinc-400 uppercase text-xs tracking-wide` |
| 섹션 제목 | `text-zinc-400 uppercase text-xs tracking-wide font-semibold` |

## 반응형
- 사이드바: `w-14 md:w-56`
- 대시보드 그리드: `grid-cols-2 md:grid-cols-4`
- 칸반: 가로 스크롤 허용 (`overflow-x-auto`)

## 금지 사항 (Design Reviewer가 감점)
- `bg-gray-*`, `text-gray-*` — zinc 사용
- 임의 hex (위 에이전트 색상 제외)
- `rounded-xl`/`rounded-lg` 혼용 일관성 없음
- `violet`, `purple`, `green-400` 같은 비규격 액센트

## Design Reviewer 체크리스트 (4축 대응)
| 축 | 확인 |
|----|------|
| 디자인 품질 (40%) | 토큰 준수, 에이전트 색상 정확, 카드/입력 규격 |
| 독창성 (30%) | D3-force 노드, 칸반 6열 인라인 프롬프트, 실시간 이벤트 로그 |
| 기술 (15%) | 모바일 `w-14`, 키보드 칸반 탐색, 포커스 링 |
| 기능 (15%) | 빈 프로젝트 상태, 실행 실패 에러, REJECT 루프 표시 |
