// T02-g: 실제 브라우저에서 한 파트를 S3에 저장한 뒤 탭을 닫고, 같은 파일을 재선택해 남은 파트만 보낸다.
// 인증은 시험 전용 서명 IdP 어댑터가 발급한 합성 사용자 토큰이며, 업로드 API·DB·S3 응답은 대체하지 않는다.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { appendFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'
import { createServer } from 'vite'

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const apiBase = process.env.E2E_API_URL
const session = JSON.parse(readFileSync(process.env.E2E_SESSION_FILE, 'utf8'))
const outFile = process.env.E2E_OUTPUT
const tmp = mkdtempSync(join(tmpdir(), 'marine-web-e2e-'))
const file = join(tmp, 'synthetic-resume.csv')
const data = Buffer.from('time,temp\n' + '2026-11-02T00:00:00Z,12.345\n'.repeat(440000))
writeFileSync(file, data)
const checksum = createHash('sha256').update(data).digest('hex')
const result = {
  nodeid: 'web/e2e/upload-resume.mjs::T02_g_tab_close_and_resume', covers: ['T02-g'], outcome: 'failed', log: '',
  reproduce: 'python tools/web_acceptance.py --out var/acceptance-results.jsonl', evidence: {},
}
const diagnostics = { puts: [], complete_statuses: [], page_errors: [] }
result.evidence = diagnostics
const scrub = (text) => String(text)
  .replace(/https?:\/\/[^\s"'<>]+/g, (url) => url.split(/[?#]/)[0])
  .replace(/Bearer\s+\S+/gi, 'Bearer [REDACTED]')
  .replace(/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[REDACTED]')
let browser, vite
const started = Date.now()

async function json(path) {
  const response = await fetch(apiBase + path, { headers: { Authorization: `Bearer ${session.user.access_token}` } })
  assert.equal(response.status, 200, `API ${path}: HTTP ${response.status}`)
  return response.json()
}

async function poll(fn, description, timeout = 30000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    const value = await fn()
    if (value) return value
    await new Promise((r) => setTimeout(r, 100))
  }
  throw new Error(`${description}: 시간 초과`)
}

async function uploadPage(context, base) {
  const page = await context.newPage()
  await page.goto(base + '/upload')
  await page.getByRole('combobox', { name: '원천 장비', exact: true }).click()
  await page.getByText('A 수질 센서 (C3)', { exact: true }).click()
  await page.getByRole('combobox', { name: '실증회차', exact: true }).click()
  await page.getByText('A-2026-11-01 A 1차 항해 시험', { exact: true }).click()
  await page.locator('.uppy-Dashboard-input').first().setInputFiles(file)
  return page
}

try {
  vite = await createServer({ configFile: join(webRoot, 'vite.config.ts'), root: webRoot, logLevel: 'error',
    server: { host: '127.0.0.1', port: 0, proxy: { '/v1': apiBase } } })
  await vite.listen()
  const base = `http://127.0.0.1:${vite.httpServer.address().port}`
  browser = await chromium.launch({ channel: process.env.E2E_BROWSER_CHANNEL || undefined,
    executablePath: process.env.E2E_BROWSER_EXECUTABLE || undefined })
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } })
  context.on('response', (response) => {
    if (new URL(response.url()).pathname.endsWith('/complete')) diagnostics.complete_statuses.push(response.status())
  })
  context.on('page', (page) => page.on('pageerror', (error) => diagnostics.page_errors.push(scrub(error.message))))
  await context.addInitScript(({ key, user, origin }) => {
    if (window.location.origin === origin) sessionStorage.setItem(key, JSON.stringify(user))
  }, {
    key: `oidc.user:${session.authority}:${session.client_id}`, user: session.user, origin: base,
  })
  let resuming = false
  const resumedPuts = []
  // 초기 전송의 2번 이후 파트는 끊고 1번만 실제 저장한다. API·성공 응답을 가로 만들어 주지 않는다.
  await context.route(/[?&]partNumber=\d+/, async (route) => {
    const part = Number(new URL(route.request().url()).searchParams.get('partNumber'))
    if (route.request().method() === 'PUT' && !resuming && part !== 1) {
      await route.abort('internetdisconnected')
      return
    }
    if (resuming && route.request().method() === 'PUT') resumedPuts.push(part)
    if (route.request().method() === 'PUT') diagnostics.puts.push({ phase: resuming ? 'resumed' : 'initial', part,
      byte_size: route.request().postDataBuffer()?.length })
    await route.continue()
  })
  let page = await uploadPage(context, base)
  const createdResponse = page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/uploads'
    && r.request().method() === 'POST' && r.status() === 201)
  const savedResponse = page.waitForResponse((r) => r.request().method() === 'PUT'
    && new URL(r.url()).searchParams.get('partNumber') === '1' && r.status() === 200)
  await page.locator('.uppy-StatusBar-actionBtn--upload').click()
  const first = await (await createdResponse).json()
  assert.ok(first.part_count >= 3, '분할 전송 시험에는 최소 3개 파트가 필요합니다')
  await savedResponse
  assert.equal(diagnostics.puts.find((put) => put.phase === 'initial' && put.part === 1)?.byte_size,
    first.part_size, '첫 파트의 실제 크기가 서버 part_size와 다릅니다')
  const before = await poll(async () => {
    const parts = await json(`/v1/uploads/${first.upload_id}/parts`)
    return parts.completed_parts.length === 1 ? parts : null
  }, '첫 파트 저장 확인')
  assert.deepEqual(before.completed_parts.map((p) => p.part_number), [1])
  await page.close()
  const afterClose = await json(`/v1/uploads/${first.upload_id}/parts`)
  assert.deepEqual(afterClose.completed_parts.map((p) => p.part_number), [1], '탭 종료 뒤 서버 파트 보존')
  resuming = true
  page = await uploadPage(context, base)
  const resumedResponse = page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/uploads'
    && r.request().method() === 'POST' && r.status() === 201)
  const completeResponse = page.waitForResponse((r) => new URL(r.url()).pathname.endsWith('/complete')
    && r.request().method() === 'POST' && r.status() === 202, { timeout: 60000 })
  await page.locator('.uppy-StatusBar-actionBtn--upload').click()
  const resumed = await (await resumedResponse).json()
  assert.equal(resumed.upload_id, first.upload_id, '다시 선택하면 같은 업로드 세션을 재개해야 합니다')
  assert.equal(resumed.asset_id, first.asset_id, '재개 시 중복 자산을 만들면 안 됩니다')
  await completeResponse
  assert.ok(!resumedPuts.includes(1), '이미 저장된 1번 파트를 다시 전송했습니다')
  assert.deepEqual([...new Set(resumedPuts)].sort((a, b) => a - b),
    Array.from({ length: first.part_count - 1 }, (_, i) => i + 2))
  const status = await poll(async () => {
    const s = await json(`/v1/uploads/${first.upload_id}`)
    if (s.ingest_status === 'QUARANTINED' || s.ingest_status === 'FAILED') throw new Error('재개 파일 등록 실패')
    return ['REGISTERED', 'READY_PRIVATE'].includes(s.ingest_status) ? s : null
  }, '서버 무결성 검증')
  const receipt = await json(`/v1/assets/${first.asset_id}/receipt`)
  assert.equal(receipt.checksum_sha256, checksum, '재개 후 서버의 전체 파일 sha256이 다릅니다')
  assert.equal(receipt.byte_size, data.length)
  assert.equal(status.parts_completed, first.part_count)
  assert.deepEqual(diagnostics.page_errors, [], '페이지 실행 오류가 발생했습니다')
  result.outcome = 'passed'
  result.evidence = { ...diagnostics, part_count: first.part_count, preserved_parts: [1], resumed_parts: [...new Set(resumedPuts)],
    same_upload_id: true, same_asset_id: true, checksum_verified: true, byte_size: data.length,
    server_ingest_status: status.ingest_status }
  console.log('T02-g 통과: 탭 종료·동일 세션 재개·완료 파트 건너뛰기·전체 해시 검증')
} catch (error) {
  result.log = scrub(error.stack || error.message || error)
  console.error('T02-g 실패:', scrub(error.message || error))
  process.exitCode = 1
} finally {
  if (browser) await browser.close()
  if (vite) await vite.close()
  result.duration_ms = Date.now() - started
  appendFileSync(outFile, JSON.stringify(result) + '\n', 'utf8')
  // mkdtemp가 방금 만든 절대 경로만 정리한다.
  rmSync(tmp, { recursive: true, force: true })
}
