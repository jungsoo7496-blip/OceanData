// 가로 시간축 타임라인 (09번 요구사항 5.2). 새 의존성 없이 CSS 절대 위치로 그린다.
// 표시 하나하나에 aria-label·title로 설명을 달고, 같은 내용은 화면의 표로도 준다 (요구사항 11.2).
// 좁은 화면(md 미만)에서는 줄 이름을 막대 위로 올려 시간축이 화면 폭을 다 쓰게 한다.
import { Grid } from 'antd'
import { useId, type CSSProperties, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import './Timeline.css'

export type Tone = 'asset' | 'assetUnverified' | 'event' | 'eventUnverified' | 'eventCritical' | 'eventCriticalUnverified'
  | 'obs' | 'obsMissing' | 'env'

export interface Mark {
  key: string
  start: number // epoch ms
  end?: number // 없거나 start 이하면 순간
  label: string // 보조기기·툴팁 설명
  tone: Tone
  href?: string // 누르면 이동 (자산 상세)
  onClick?: () => void // 누르면 동작 (이벤트 겹침 보기)
}

export interface Row {
  key: string
  title: ReactNode
  sub?: string
  marks: Mark[]
  flat?: boolean // 겹쳐 쌓지 않고 한 줄에 (관측 구간처럼 이어지는 막대)
}

export interface Group {
  key: string
  title: string
  rows: Row[]
  empty: string
  note?: ReactNode
}

const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR
const KST_OFFSET = 9 * HOUR // KST는 일광 절약 시간이 없다
const LABEL_W = 240
const LANE_H = 18
const MARK_H = 12
const DOT = 10
const MAX_LANES = 8

const BLUE = '#0958d9'
const PURPLE = '#531dab'
const RED = '#a8071a'
const GREEN = '#237804'
const GREY = '#595959'
const TEAL = '#006d75'

// 흰 바탕 대비 3:1 이상 (KWCAG 비텍스트 대비). 색만으로 구분하지 않도록 채움·테두리·무늬를 함께 바꾼다
export const TONE_STYLE: Record<Tone, CSSProperties> = {
  asset: { background: BLUE },
  assetUnverified: { background: `repeating-linear-gradient(135deg, ${BLUE} 0 4px, #bcd2f5 4px 7px)`, border: `1px solid ${BLUE}` },
  event: { background: PURPLE },
  eventUnverified: { background: '#fff', border: `2px solid ${PURPLE}` },
  eventCritical: { background: RED },
  eventCriticalUnverified: { background: '#fff', border: `2px solid ${RED}` },
  obs: { background: GREEN },
  obsMissing: { background: `repeating-linear-gradient(135deg, ${GREY} 0 3px, #fff 3px 6px)`, border: `1px solid ${GREY}` },
  env: { background: TEAL, borderRadius: '50%' },
}

const DAY_FMT = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit' })
const TIME_FMT = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false })
const STEPS = [MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY]

// KST 경계(정각·자정)에 맞춘 눈금. 날짜가 바뀌는 눈금에만 날짜를 붙인다
export function ticks(from: number, to: number, max = 8): { t: number; label: string }[] {
  const span = to - from
  const step = STEPS.find((s) => span / s <= max) ?? Math.ceil(span / max / (30 * DAY)) * 30 * DAY
  const out: { t: number; label: string }[] = []
  let prevDay = ''
  for (let t = Math.ceil((from + KST_OFFSET) / step) * step - KST_OFFSET; t <= to; t += step) {
    const day = DAY_FMT.format(t)
    out.push({ t, label: step >= DAY ? day : day !== prevDay ? `${day} ${TIME_FMT.format(t)}` : TIME_FMT.format(t) })
    prevDay = day
  }
  return out
}

// 겹치는 표시는 아래 줄로 내린다 (순간 표시는 눈에 보이는 폭만큼 차지)
function stack(row: Row, span: number): { mark: Mark; lane: number }[] {
  if (row.flat) return row.marks.map((mark) => ({ mark, lane: 0 }))
  const minW = span * 0.008
  const ends: number[] = []
  return [...row.marks].sort((a, b) => a.start - b.start).map((mark) => {
    const end = Math.max(mark.end ?? mark.start, mark.start + minW)
    let lane = ends.findIndex((e) => e <= mark.start)
    if (lane < 0) lane = ends.length < MAX_LANES ? ends.length : MAX_LANES - 1 // 너무 많이 겹치면 마지막 줄에 겹쳐 그린다 (표에는 모두 있다)
    ends[lane] = Math.max(ends[lane] ?? 0, end)
    return { mark, lane }
  })
}

function MarkView({ mark, lane, pct }: { mark: Mark; lane: number; pct: (t: number) => number }) {
  const top = 5 + lane * LANE_H
  const instant = mark.end === undefined || mark.end <= mark.start
  const shape: CSSProperties = instant
    ? { top: top + 1, left: `calc(${pct(mark.start)}% - ${DOT / 2}px)`, width: DOT, height: DOT,
      transform: mark.tone === 'env' ? undefined : 'rotate(45deg)' }
    : { top, left: `${pct(mark.start)}%`, width: `max(3px, ${pct(mark.end!) - pct(mark.start)}%)`, height: MARK_H, borderRadius: 3 }
  const style: CSSProperties = { position: 'absolute', display: 'block', boxSizing: 'border-box', padding: 0, margin: 0, ...shape, ...TONE_STYLE[mark.tone] }
  if (mark.href) return <Link className="tl-mark" to={mark.href} aria-label={mark.label} title={mark.label} style={style} />
  if (mark.onClick)
    return <button className="tl-mark" type="button" aria-label={mark.label} title={mark.label} onClick={mark.onClick} style={{ ...style, cursor: 'pointer' }} />
  return <span role="img" aria-label={mark.label} title={mark.label} style={style} />
}

const secondary: CSSProperties = { fontSize: 12, color: 'rgba(0,0,0,0.68)' }
const wrap: CSSProperties = { overflowWrap: 'anywhere' }

export function Timeline({ from, to, band, groups, label }: {
  from: number; to: number; band?: [number, number]; groups: Group[]; label: string
}) {
  const id = useId()
  const narrow = Grid.useBreakpoint().md === false
  const span = Math.max(to - from, 1)
  const pct = (t: number) => Math.min(100, Math.max(0, ((t - from) / span) * 100))
  const tickList = ticks(from, to, narrow ? 4 : 8)
  const labelCol: CSSProperties = narrow ? { padding: '4px 0 2px', fontSize: 13 } : { flex: `0 0 ${LABEL_W}px`, width: LABEL_W, minWidth: 0, padding: '3px 8px 3px 0', fontSize: 13 }
  const grid = (
    <>
      {band && <div aria-hidden style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(band[0])}%`, width: `${pct(band[1]) - pct(band[0])}%`, background: '#e6f4ff' }} />}
      {tickList.map((t) => <div aria-hidden key={t.t} style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(t.t)}%`, width: 1, background: '#e8e8e8' }} />)}
    </>
  )
  return (
    <div role="region" aria-label={label} style={{ overflowX: 'auto' }}>
      <div style={{ minWidth: narrow ? 0 : 760, paddingRight: narrow ? 0 : 8 }}>
        <div aria-hidden style={{ display: 'flex' }}>
          {!narrow && <div style={{ flex: `0 0 ${LABEL_W}px` }} />}
          <div style={{ position: 'relative', flex: 1, height: 24, borderBottom: '1px solid #bfbfbf' }}>
            {tickList.map((t) => {
              const p = pct(t.t) // 양 끝 눈금 글자는 안쪽으로 붙여 잘리지 않게
              return <span key={t.t} style={{ ...secondary, position: 'absolute', top: 2, left: `${p}%`, whiteSpace: 'nowrap',
                transform: p < 8 ? 'none' : p > 92 ? 'translateX(-100%)' : 'translateX(-50%)' }}>{t.label}</span>
            })}
          </div>
        </div>
        {groups.map((g) => (
          <section key={g.key} aria-labelledby={`${id}-${g.key}`} style={{ marginTop: 12 }}>
            <h3 id={`${id}-${g.key}`} style={{ fontSize: 14, fontWeight: 600, margin: '4px 0' }}>{g.title}</h3>
            {g.note}
            {g.rows.length === 0 ? (
              <p style={{ ...secondary, margin: '4px 0 0' }}>{g.empty}</p>
            ) : (
              <div role="list">
                {g.rows.map((row) => {
                  const placed = stack(row, span)
                  const lanes = placed.reduce((n, p) => Math.max(n, p.lane + 1), 1)
                  return (
                    <div role="listitem" key={row.key} style={{ display: narrow ? 'block' : 'flex', borderTop: '1px solid #f0f0f0' }}>
                      <div style={labelCol}>
                        <div style={wrap}>{row.title}</div>
                        {row.sub && <div style={{ ...secondary, ...wrap }}>{row.sub}</div>}
                      </div>
                      <div style={{ position: 'relative', flex: 1, ...(narrow ? { height: lanes * LANE_H + 10 } : { minHeight: Math.max(40, lanes * LANE_H + 10) }) }}>
                        {grid}
                        {placed.map(({ mark, lane }) => <MarkView key={mark.key} mark={mark} lane={lane} pct={pct} />)}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  )
}

// 범례: 표본 모양과 글자를 함께 둔다 (색만으로 구분하지 않기)
export function Legend({ items }: { items: { tone: Tone | 'band'; text: string; instant?: boolean }[] }) {
  return (
    <ul style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 16px', listStyle: 'none', padding: 0, margin: 0, fontSize: 13 }} aria-label="범례">
      {items.map((x) => (
        <li key={x.text} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span aria-hidden style={{
            display: 'inline-block', boxSizing: 'border-box',
            ...(x.tone === 'band' ? { width: 22, height: 12, background: '#e6f4ff', border: '1px solid #91caff' }
              : { width: x.instant ? DOT : 22, height: x.instant ? DOT : MARK_H, borderRadius: 3, ...TONE_STYLE[x.tone],
                ...(x.instant && x.tone !== 'env' ? { transform: 'rotate(45deg)', borderRadius: 0 } : {}) }),
          }} />
          {x.text}
        </li>
      ))}
    </ul>
  )
}
