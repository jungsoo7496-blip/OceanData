// 포털 E2E + 접근성 자동 검사 (09번 요구사항 8, 11.2). make up && make seed 후:
//   cd web && node e2e/portal.mjs [기본 URL]
// Keycloak 시험 계정 member-a로 로그인 → 대시보드(장비별 수집 상태·경보) → CSV 업로드 → 같은 파일 재선택(이미 등록) → 자산 목록.
// axe(@axe-core/playwright, MPL-2.0)로 주요 화면의 심각(critical) 위반이 없는지 본다.
import { AxeBuilder } from '@axe-core/playwright'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// 명시한 playwright-core와 설치한 Chromium/Chrome을 쓴다.
import { chromium } from 'playwright-core'
const base = process.argv[2] ?? 'http://localhost:5173'
const dir = mkdtempSync(join(tmpdir(), 'portal-'))
const csv = join(dir, `e2e_${Date.now()}.csv`)
writeFileSync(csv, `time,temp\n2026-11-02T00:00:00Z,${(Math.random() * 30).toFixed(3)}\n`)
const fail = (m) => { console.error('실패:', m); process.exit(1) }

const browser = await chromium.launch({ channel: process.env.E2E_BROWSER_CHANNEL || undefined })
const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage()
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
async function axe(name) {
  // Uppy 대시보드 내부의 ARIA 문제는 외부 라이브러리 이슈라 따로 기록하고 검사에서 뺀다 (09번 tasks 참고)
  const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).exclude('.uppy-Dashboard').analyze()
  const critical = r.violations.filter((v) => v.impact === 'critical')
  console.log(`axe ${name}: 위반 ${r.violations.length}건 (critical ${critical.length})`)
  for (const v of r.violations) console.log(`  - [${v.impact}] ${v.id}: ${v.help} → ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`)
  return critical.length
}
try {
  await page.goto(base)
  await page.getByRole('button', { name: '로그인' }).click()
  await page.fill('#username', 'member-a')
  await page.fill('#password', 'dev-password')
  await page.click('#kc-login')
  await page.waitForURL('**/workspace', { timeout: 30000 })
  await page.getByText('누적 실증회차').waitFor()
  await page.getByText('장비별 수집 상태').waitFor()
  await page.getByText(/^경보 \d+건$/).waitFor()  // 11번: 자기 기관 경보와 조치 안내
  let critical = await axe('대시보드')
  await page.getByRole('link', { name: '업로드' }).click()
  await page.locator('.ant-select').first().click()
  await page.getByText('A 수질 센서').last().click()
  critical += await axe('업로드')
  await page.setInputFiles('.uppy-Dashboard-input', csv)
  await page.locator('.uppy-StatusBar-actionBtn--upload').click()
  await page.getByText('수신 완료', { exact: false }).waitFor({ timeout: 60000 })
  await page.waitForTimeout(4000)
  await page.setInputFiles('.uppy-Dashboard-input', csv)
  await page.locator('.uppy-StatusBar-actionBtn--upload').click()
  await page.getByText('이미 등록된 파일').waitFor({ timeout: 30000 })
  await page.getByRole('link', { name: '자산' }).click()
  await page.getByText(csv.split('/').pop()).first().waitFor()
  critical += await axe('자산')
  if (errors.length) fail(`페이지 오류 ${errors.join('; ')}`)
  if (critical) fail(`접근성 critical 위반 ${critical}건`)
  console.log('통과')
} catch (e) {
  fail(e.message)
} finally {
  await browser.close()
}
