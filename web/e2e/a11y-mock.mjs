// 접근성 자동 검사 — 실제 스택 없이 (09번 작업 15, 요구사항 11.2). CI web 작업에서 빌드 뒤에 실행한다.
//   cd web && npm run build && npm run test:a11y          (저장소 루트에서는 make web-a11y)
// 빌드한 웹(dist/)을 vite preview로 띄우고, 역할마다 OIDC 로그인 상태(세션 저장소의 사용자)와 /v1 응답(e2e/fixtures/api.mjs의
// 계약 모양 합성 값)을 흉내 내어 역할별 주요 화면·상세 화면·열린 대화상자를 검사한다. Keycloak·API·DB는 띄우지 않는다.
//
// 실패 기준 (하나라도 있으면 종료 코드 1):
//  1. axe-core(@axe-core/playwright, MPL-2.0) WCAG 2.0·2.1 A·AA 규칙 위반 중 영향도 serious·critical.
//     moderate·minor는 목록에만 적는다. 데스크톱(1280×800)과 휴대폰 폭(390×844) 둘 다 검사한다.
//  2. 휴대폰 폭 가로 넘침: document.documentElement.scrollWidth > innerWidth + 1, 또는 열린 대화상자·서랍이 화면 밖으로 나감 (KWCAG 재배치).
//  3. 키보드 초점: Tab으로 옮겨 간 링크(a[href])에 윤곽선·그림자가 없음 (axe가 보지 않는 항목, KWCAG 초점 표시).
//  4. 화면이 그려지지 않음(화면별 확인 문구가 안 보임), 페이지 예외·콘솔 오류, 흉내 내지 않은 /v1 호출, 외부 요청.
// 검사에서 빼는 것: 화면 코드로 고칠 수 없는 외부 라이브러리 내부 문제만, 규칙·요소·속성을 좁혀서 (아래 THIRD_PARTY). 영역 전체는 빼지 않는다.
//
// 브라우저: playwright-core가 설치한 Chromium (CI: node node_modules/playwright-core/cli.js install --with-deps chromium).
// 미리 받아 둔 다른 Chromium 실행 파일은 E2E_CHROMIUM_PATH, 설치된 Chrome은 E2E_BROWSER_CHANNEL=chrome.
// 빌드할 때 VITE_OIDC_AUTHORITY·VITE_OIDC_CLIENT_ID를 바꿨다면 같은 값을 주고 실행한다 (세션 저장소 키).
import { AxeBuilder } from '@axe-core/playwright'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'
import { preview } from 'vite'
import { AST_VIDEO, RPT_DRAFT, RPT_SUBMITTED, SES1, respond } from './fixtures/api.mjs'

const WEB = fileURLToPath(new URL('..', import.meta.url))
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
const FAIL_IMPACTS = new Set(['serious', 'critical'])
const VIEWPORTS = [
  { name: '데스크톱', width: 1280, height: 800 },
  { name: '휴대폰', width: 390, height: 844, phone: true },
]
const WORKERS = Math.max(1, Number(process.env.A11Y_WORKERS) || 4) // 역할×화면 폭 묶음을 동시에 몇 개 돌릴지
const AUTHORITY = process.env.VITE_OIDC_AUTHORITY ?? 'http://localhost:8080/realms/marine'
const CLIENT_ID = process.env.VITE_OIDC_CLIENT_ID ?? 'marine-web'
const media = (file, type) => `data:${type};base64,${readFileSync(new URL(`./fixtures/${file}`, import.meta.url)).toString('base64')}`
// 합성 시험 패턴 (ffmpeg testsrc, README 참고). 영상은 구간 이동이 되도록 data: URL로 준다
const MEDIA = { video: media('testsrc.webm', 'video/webm'), image: media('testsrc.jpg', 'image/jpeg') }
const FRAME = readFileSync(new URL('./fixtures/testsrc.jpg', import.meta.url))

// 알려진 외부 라이브러리 문제: 화면 코드로 고칠 수 없어 이 규칙·요소·속성에 맞는 노드만 결과에서 뺀다 (뺀 수는 출력한다)
const THIRD_PARTY = [
  { rule: 'aria-allowed-attr', html: /^<div class="ant-select[\s"]/, attrs: ['aria-required'],
    // AntD Form.Item이 필수 항목에 붙이는 aria-required를 rc-select 14.16이 입력(role=combobox)과 바깥 div 둘 다에 붙인다.
    // 입력 쪽은 맞아 보조기기는 필수 항목으로 읽는다(아래 checkRequiredSelects가 확인). rc-select Select가 omitDomProps를 덮어써 뺄 수 없다
    why: 'rc-select 바깥 div의 aria-required' },
  { rule: 'aria-allowed-attr', html: /^<div [^>]*class="uppy-Dashboard-inner"/, attrs: ['aria-modal'],
    // Uppy Dashboard 4.1.3이 inline 모드에서 역할 없는 div에 aria-modal="false"를 남긴다 ({"aria-modal": !inline && 'true'}). ADR-0016
    why: 'Uppy inline 대시보드의 aria-modal' },
]
const disallowedAttrs = (node) => [...node.failureSummary.matchAll(/(aria-[a-z]+)=/g)].map((m) => m[1])
const thirdParty = (violation, node) => THIRD_PARTY.find((k) => k.rule === violation.id && k.html.test(node.html)
  && disallowedAttrs(node).every((a) => k.attrs.includes(a)))

// ---------------------------------------------------------------- 검사할 화면

const button = (name) => (page) => page.getByRole('button', { name, exact: true }).first().click()
/** 버튼을 눌러 대화상자(제목이 name으로 시작)를 연다 */
const dialog = (name, open, after) => ({
  name: `${name} 대화상자`,
  act: async (page) => {
    await open(page)
    await page.getByRole('dialog', { name: new RegExp(`^${name}`) }).waitFor()
    if (after) await after(page)
  },
})
/** 화면 안에서 펼치는 상태 (대화상자가 아닌 것) */
const state = (name, act) => ({ name, act })

const SCENARIOS = [
  { role: null, label: '로그인 전', visits: [{ path: '/', ready: '기관 계정으로 로그인하세요' }] },
  { role: 'ORG_ADMIN', visits: [
    { path: '/workspace', ready: '장비별 수집 상태', open: [dialog('장애 처리 기록', button('처리 기록'))] },
    { path: '/upload', ready: '업로드가 끊기면' },
    { path: '/assets', ready: '2026-11-03_항해01/bridge-cam_20261103_100000.mp4' },
    { path: `/assets/${AST_VIDEO}`, ready: '구간 클립 내보내기' },
    { path: '/sessions', ready: '회차 미상 자산', open: [dialog('회차 만들기', button('회차 만들기'))] },
    { path: `/sessions/${SES1}`, ready: '시간축', open: [dialog('겹치는 자산', button('겹치는 자산')),
      dialog('회차 수정', button('회차 수정'))] },
    { path: `/sessions/${SES1}/records`, ready: '1차 항해 실증일지', open: [dialog('회차 기록 작성', button('기록 작성'))] },
    { path: '/reports', ready: '사전 점검 보고서', open: [dialog('회차 보고서 초안', button('회차 보고서 초안 만들기'))] },
    { path: `/reports/${RPT_DRAFT}`, ready: '근거 손상', open: [state('지표 근거 서랍', async (page) => {
      await page.getByRole('button', { name: '소형 선박 이벤트 수 (v1) 근거 보기' }).press('Enter') // 키보드로 연다
      await page.getByText('근거 경로 — 소형 선박 이벤트 수').waitFor()
      await page.getByText('권한 범위 밖 근거 1건은 숨겼습니다').waitFor()
    })] },
    { path: '/access', ready: '이용 이력' },
    { path: '/devices', ready: '에이전트 설치 안내', open: [dialog('장비 등록', button('장비 등록')), dialog('원본 백업 신청', button('원본 백업 신청'))] },
    { path: '/settings', ready: '데이터 연계 카드', open: [dialog('연계 카드 등록', button('카드 등록'))] },
  ] },
  { role: 'ORG_MEMBER', visits: [
    { path: '/workspace', ready: '장비별 수집 상태' },
    { path: `/assets/${AST_VIDEO}`, ready: '구간 클립 내보내기', open: [state('프록시 재생·구간 지정', async (page) => {
      await button('재생')(page)
      await page.waitForFunction(() => (document.querySelector('video')?.readyState ?? 0) >= 1)
      await page.getByRole('button', { name: '현재 위치를 시작으로' }).click()
    })] },
    { path: '/devices', ready: '에이전트 설치 안내' },
  ] },
  { role: 'PLATFORM_OPERATOR', visits: [
    { path: '/operator', ready: '감사 로그 (최근)', open: [
      state('보존 dry-run 결과', async (page) => { await button('미리 보기')(page); await page.getByText('삭제 대상 14건').waitFor() }),
      dialog('약정 기록', button('약정 기록')),
      dialog('기관 정책', button('변경')),
      state('보존 정책 탭', async (page) => {
        await page.getByRole('tab', { name: '보존 정책' }).click()
        await page.getByText('QUARANTINE:UNKNOWN_SESSION').waitFor()
      }),
    ] },
    { path: '/monitoring', ready: '공공 Open API 수집 소스', open: [dialog('일 점검', button('기록'))] },
    { path: '/sharing', ready: '공유 심사 대기', open: [dialog('계보', button('1.0.0 · L3 승인 이용기관 · 3개 계보 보기'), (page) => page.getByText('deid-baseline 0.1.0').waitFor())] },
    { path: '/review', ready: '비식별 품질' },
  ] },
  { role: 'PROGRAM_VIEWER', visits: [
    { path: '/program', ready: '2026-11-03_항해01' },
    { path: '/reports', ready: '사전 점검 보고서' },
    { path: `/reports/${RPT_SUBMITTED}`, ready: '작성 항목' },
  ] },
  { role: 'REVIEWER', visits: [
    { path: '/review', ready: '배정된 비식별 검수', open: [dialog('검수', button('검수'), (page) => page.getByAltText('원본 0.5초').waitFor())] },
  ] },
  { role: 'CONSUMER', visits: [
    { path: '/catalog', ready: '데이터셋 카탈로그', open: [dialog('이용 신청', button('이용 신청')),
      dialog('다운로드', button('다운로드 링크 받기'), (page) => page.getByText('video/0003.mp4').waitFor())] },
  ] },
]

// ---------------------------------------------------------------- 서버·브라우저

async function serve() {
  if (!existsSync(new URL('../dist/index.html', import.meta.url))) throw new Error('dist/가 없습니다. 먼저 npm run build')
  for (let port = 5201; port <= 5210; port++) {
    try {
      const server = await preview({ root: WEB, configFile: false, logLevel: 'error',
        preview: { host: '127.0.0.1', port, strictPort: true, open: false } })
      return { server, base: `http://127.0.0.1:${port}` }
    } catch (e) {
      if (!/already in use/i.test(String(e?.message))) throw e
    }
  }
  throw new Error('포트 5201~5210이 모두 사용 중입니다')
}

function oidcUser(role) {
  const now = Math.floor(Date.now() / 1000)
  return { id_token: 'test-id-token', access_token: 'test-access-token', token_type: 'Bearer', scope: 'openid profile', expires_at: now + 3600,
    profile: { sub: `test-${role.toLowerCase()}`, iss: AUTHORITY, aud: CLIENT_ID, exp: now + 3600, iat: now } }
}

/** 역할 하나·화면 폭 하나의 브라우저 문맥. /v1은 합성 응답, 정적 파일은 preview 서버, 나머지 요청은 막는다 */
async function newContext(browser, base, role, viewport) {
  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, locale: 'ko-KR',
    timezoneId: 'Asia/Seoul' })
  const issues = { unmocked: new Set(), external: new Set(), errors: [] }
  if (role) {
    await context.addInitScript(({ key, user }) => {
      try { sessionStorage.setItem(key, JSON.stringify(user)) } catch { /* about:blank 등 */ }
    }, { key: `oidc.user:${AUTHORITY}:${CLIENT_ID}`, user: oidcUser(role) })
  }
  await context.route('**/*', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (url.origin !== base) {
      issues.external.add(`${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    }
    if (!url.pathname.startsWith('/v1/')) return route.continue()
    if (/^\/v1\/deid-reviews\/[^/]+\/frame$/.test(url.pathname)) return route.fulfill({ status: 200, contentType: 'image/jpeg', body: FRAME })
    let body
    try { body = req.postData() ? JSON.parse(req.postData()) : undefined } catch { body = undefined }
    const res = respond(req.method(), url.pathname, { role, query: url.searchParams, body, media: MEDIA })
    if (!res) {
      issues.unmocked.add(`${req.method()} ${url.pathname}${url.search}`)
      return route.fulfill({ status: 501, contentType: 'application/problem+json',
        json: { type: 'about:blank', title: '합성 응답 없음', status: 501, code: 'INTERNAL', request_id: '01K9A11Y000000000000000000' } })
    }
    return route.fulfill({ status: res.status, json: res.json })
  })
  return { context, issues }
}

// ---------------------------------------------------------------- 검사

/** 응답·애니메이션·로딩 표시가 끝날 때까지 */
async function settle(page) {
  await page.waitForLoadState('networkidle').catch(() => {})
  await page.waitForFunction(() => !document.querySelector(
    '.ant-spin-spinning, .ant-skeleton-active, [class*="-appear-active"], [class*="-enter-active"], [class*="-leave-active"]'),
  null, { timeout: 15000 })
  await page.waitForTimeout(200)
}

/** axe 결과에서 알려진 외부 라이브러리 노드(THIRD_PARTY)를 빼고 돌려준다 */
async function axe(page) {
  const { violations } = await new AxeBuilder({ page }).withTags(TAGS).analyze()
  const skipped = []
  const kept = violations.map((v) => ({ ...v, nodes: v.nodes.filter((n) => {
    const k = thirdParty(v, n)
    if (k) skipped.push(k.why)
    return !k
  }) })).filter((v) => v.nodes.length)
  return { violations: kept, skipped }
}

/** THIRD_PARTY의 rc-select 예외가 맞는지: 바깥 div에 aria-required가 있으면 그 안 입력(role=combobox)에도 있어야 한다 */
function checkRequiredSelects(page) {
  return page.evaluate(() => [...document.querySelectorAll('div.ant-select[aria-required="true"]')]
    .filter((d) => !d.querySelector('[role="combobox"][aria-required="true"]')).length)
}

/** 휴대폰 폭: 문서 가로 넘침과 화면 밖으로 나간 대화상자·서랍. 넘치면 원인 후보(오른쪽 끝이 화면 밖인 바깥쪽 요소)를 적는다 */
function overflow(page) {
  return page.evaluate(() => {
    const w = window.innerWidth
    const describe = (el) => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${[...el.classList].slice(0, 3).map((c) => `.${c}`).join('')}`
    const out = []
    const scrollWidth = document.documentElement.scrollWidth
    if (scrollWidth > w + 1) {
      const wide = [...document.body.querySelectorAll('*')].filter((el) => el.getBoundingClientRect().right > w + 1)
      const outer = wide.filter((el) => !wide.includes(el.parentElement)).slice(0, 4)
      out.push(`문서 폭 ${scrollWidth}px > 화면 ${w}px (${outer.map(describe).join(', ')})`)
    }
    for (const el of document.querySelectorAll('.ant-modal, .ant-drawer-content-wrapper')) {
      const r = el.getBoundingClientRect()
      if (r.width && (r.left < -1 || r.right > w + 1)) out.push(`${describe(el)}가 화면 밖 (${Math.round(r.left)}~${Math.round(r.right)}px)`)
    }
    return out
  })
}

/** Tab으로 링크에 초점을 옮겼을 때 윤곽선이나 그림자가 보이는지 (최대 60번) */
async function linkFocus(page) {
  await page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur())
  const hidden = new Set()
  let first = null
  for (let i = 0; i < 60; i++) {
    await page.keyboard.press('Tab')
    const r = await page.evaluate(() => {
      const el = document.activeElement
      if (!el || el === document.body) return { key: null }
      const key = el.outerHTML.slice(0, 120)
      if (el.tagName !== 'A' || !el.hasAttribute('href')) return { key }
      const cs = getComputedStyle(el)
      const visible = (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0) || cs.boxShadow !== 'none'
      return { key, bad: visible ? null : `${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30)} (${el.getAttribute('href')})` }
    })
    if (r.key && r.key === first) break // 한 바퀴 돌았다
    first ??= r.key
    if (r.bad) hidden.add(r.bad)
  }
  return [...hidden]
}

const short = (v) => `[${v.impact}] ${v.id}: ${v.help} → ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}${v.nodes.length > 3 ? ` 외 ${v.nodes.length - 3}곳` : ''}`
const detail = (v) => v.nodes.slice(0, 3).map((n) => `      ${n.html.slice(0, 160)}\n        ${n.failureSummary.split('\n').slice(1).join(' ').trim().slice(0, 240)}`)

/** 역할 하나 × 화면 폭 하나: 화면마다 기본 상태와 열린 상태를 차례로 검사한다. 출력은 묶어서 돌려준다 */
async function runJob(browser, base, scenario, viewport) {
  const who = scenario.label ?? scenario.role
  const lines = []
  const failures = []
  const skipped = new Map()
  let checked = 0
  const { context, issues } = await newContext(browser, base, scenario.role, viewport)
  const page = await context.newPage()
  page.on('pageerror', (e) => issues.errors.push(`페이지 예외: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') issues.errors.push(`콘솔 오류: ${m.text()}`) })
  try {
    for (const visit of scenario.visits) {
      for (const open of [null, ...(visit.open ?? [])]) {
        const where = `[${who}] ${visit.path}${open ? ` · ${open.name}` : ''} (${viewport.name})`
        const problems = []
        const notes = []
        try {
          await page.goto(base + visit.path)
          await page.getByText(visit.ready).first().waitFor({ timeout: 15000 })
          await settle(page)
          if (open) {
            await open.act(page)
            await settle(page)
          }
          const result = await axe(page)
          for (const why of result.skipped) skipped.set(why, (skipped.get(why) ?? 0) + 1)
          for (const v of result.violations) {
            if (FAIL_IMPACTS.has(v.impact)) problems.push(`axe ${short(v)}`, ...detail(v))
            else notes.push(`참고 ${short(v)}`)
          }
          const bad = await checkRequiredSelects(page)
          if (bad) problems.push(`필수 선택 상자 ${bad}개의 입력(role=combobox)에 aria-required가 없음 (THIRD_PARTY 예외 전제가 깨짐)`)
          if (viewport.phone) for (const o of await overflow(page)) problems.push(`가로 넘침: ${o}`)
          if (!viewport.phone && !open) for (const l of await linkFocus(page)) problems.push(`링크 초점 표시 없음: ${l}`)
          checked++
          lines.push(`${problems.length ? '✗' : '✓'} ${where} — axe 위반 ${result.violations.length}건`)
        } catch (e) {
          problems.push(`화면 확인 실패: ${String(e?.message ?? e).split('\n')[0]}`)
          lines.push(`✗ ${where}`)
        }
        lines.push(...problems.map((p) => `    ${p}`), ...notes.map((n) => `    ${n}`))
        failures.push(...problems.filter((p) => !p.startsWith(' ')).map((p) => `${where} ${p}`))
      }
    }
  } finally {
    await context.close()
  }
  for (const u of issues.unmocked) failures.push(`[${who}] (${viewport.name}) 합성 응답 없는 호출: ${u}`)
  for (const x of issues.external) failures.push(`[${who}] (${viewport.name}) 외부 요청: ${x}`)
  for (const x of new Set(issues.errors)) failures.push(`[${who}] (${viewport.name}) ${x}`)
  return { lines, failures, skipped, checked }
}

async function main() {
  const { server, base } = await serve()
  const browser = await chromium.launch({ executablePath: process.env.E2E_CHROMIUM_PATH || undefined,
    channel: process.env.E2E_BROWSER_CHANNEL || undefined })
  console.log(`접근성 검사: ${base} · axe ${TAGS.join(', ')} · 실패 기준 영향도 ${[...FAIL_IMPACTS].join('·')} · 동시 ${WORKERS}`)
  const jobs = SCENARIOS.flatMap((s) => VIEWPORTS.map((v) => [s, v]))
  const results = new Array(jobs.length)
  let next = 0
  try {
    await Promise.all(Array.from({ length: Math.min(WORKERS, jobs.length) }, async () => {
      while (next < jobs.length) {
        const i = next++
        results[i] = await runJob(browser, base, ...jobs[i])
        console.log(results[i].lines.join('\n'))
      }
    }))
  } finally {
    await browser.close()
    await server.close()
  }
  const failures = results.flatMap((r) => r.failures)
  const checked = results.reduce((n, r) => n + r.checked, 0)
  const skipped = new Map()
  for (const r of results) for (const [why, n] of r.skipped) skipped.set(why, (skipped.get(why) ?? 0) + n)
  console.log('')
  for (const [why, n] of skipped) console.log(`알려진 외부 라이브러리 문제로 뺀 노드: ${why} ${n}곳`)
  if (failures.length) {
    console.error(`실패: 검사한 상태 ${checked}개, 문제 ${failures.length}건`)
    for (const f of failures) console.error(`  - ${f}`)
    process.exit(1)
  }
  const roles = SCENARIOS.filter((s) => s.role).length
  console.log(`통과: 검사한 상태 ${checked}개 (로그인 전·역할 ${roles}개 × 데스크톱·휴대폰), serious·critical 위반 0건, 휴대폰 폭 가로 넘침 없음, 링크 초점 표시 있음`)
}

await main()
