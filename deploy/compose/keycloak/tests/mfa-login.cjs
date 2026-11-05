// 02번 요구사항 4.1 (ADR-0034): realm-marine.json을 올린 일회용 Keycloak에서 실제 브라우저로 marine-web 로그인
// (인가 코드 + PKCE)을 해, ORG_ADMIN·PLATFORM_OPERATOR만 OTP를 거치고 토큰의 acr/amr이 API의 MFA 판정과 맞는지 확인한다.
// verify-mfa.sh가 컨테이너를 띄운 뒤 부른다. 시험 컨테이너의 marine-web에 시험용 redirect URI(127.0.0.1 임시 포트)를 더한다.
// 운영·개발 Keycloak에 대고 돌리지 않는다 (OTP를 등록하고 사용자 상태를 바꾼다).
'use strict'
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { execSync } = require('node:child_process')

const KC = (process.env.KC_URL || 'http://127.0.0.1:18080').replace(/\/$/, '')
const REALM = `${KC}/realms/marine`
const ADMIN_USER = process.env.KC_ADMIN_USER || 'check-admin'
const ADMIN_PASSWORD = process.env.KC_ADMIN_PASSWORD
const EVIDENCE = process.env.EVIDENCE // 선택: 토큰과 결과를 적을 파일 (check_backend_mfa.py 입력)
const PASSWORD = 'dev-password' // realm-marine.json의 개발용 비밀번호
// API의 MFA 판정 (backend/app/adapters/keycloak_identity.py): amr에 otp/mfa/hwk/swk가 있거나 acr이 IDP_MFA_ACR_VALUES(기본 mfa,2)에 있으면 MFA
const MFA_ACR = new Set((process.env.IDP_MFA_ACR_VALUES || 'mfa,2').split(',').map((s) => s.trim()))
const apiMfa = (c) => (c.amr || []).some((a) => ['otp', 'mfa', 'hwk', 'swk'].includes(a)) || MFA_ACR.has(String(c.acr ?? ''))

function loadPlaywright() {
  const candidates = ['playwright-core', 'playwright', path.resolve(__dirname, '../../../../web/node_modules/playwright-core')]
  try {
    const globalRoot = execSync('npm root -g', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
    candidates.push(path.join(globalRoot, 'playwright'), path.join(globalRoot, 'playwright-core'))
  } catch { /* npm 없음 */ }
  for (const c of candidates) {
    try { return require(c) } catch { /* 다음 후보 */ }
  }
  throw new Error('playwright(-core)를 찾지 못했습니다. web에서 npm ci 하거나 NODE_PATH를 지정하세요')
}

const b64u = (b) => Buffer.from(b).toString('base64url')
const claims = (jwt) => JSON.parse(Buffer.from(jwt.split('.')[1], 'base64url').toString())

// RFC 6238 TOTP (HmacSHA1·6자리·30초 — realm otpPolicy와 같다)
function totpAt(secretB32, step) {
  const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  let bits = ''
  for (const ch of secretB32.replace(/[\s=]/g, '').toUpperCase()) bits += alpha.indexOf(ch).toString(2).padStart(5, '0')
  const key = Buffer.alloc(Math.floor(bits.length / 8))
  for (let i = 0; i < key.length; i++) key[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2)
  const counter = Buffer.alloc(8)
  counter.writeBigUInt64BE(BigInt(step))
  const h = crypto.createHmac('sha1', key).update(counter).digest()
  const o = h[h.length - 1] & 0xf
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1e6).padStart(6, '0')
}
const secrets = {}
const lastStep = {}
// Keycloak은 같은 시간 칸의 코드를 두 번 받지 않는다 (realmReusableOtpCode=false). 다음 칸(최대 30초)을 기다린다.
// 다음 칸 코드를 미리 쓰는 방법은 26.0.7에서 거절됐다.
async function nextCode(user) {
  const secret = secrets[user]
  assert.ok(secret, `${user}: OTP 비밀값이 없다 (등록 단계를 거치지 않음)`)
  while (Math.floor(Date.now() / 30000) <= (lastStep[user] ?? -1)) await new Promise((r) => setTimeout(r, 500))
  const step = Math.floor(Date.now() / 30000)
  lastStep[user] = step
  return totpAt(secret, step)
}

async function admin(method, p, body) {
  const t = await fetch(`${KC}/realms/master/protocol/openid-connect/token`, { method: 'POST',
    body: new URLSearchParams({ grant_type: 'password', client_id: 'admin-cli', username: ADMIN_USER, password: ADMIN_PASSWORD }) })
  assert.equal(t.status, 200, '시험 컨테이너 관리자 토큰 발급 실패')
  const { access_token: at } = await t.json()
  const r = await fetch(`${KC}/admin/realms/marine${p}`, { method, headers: { Authorization: `Bearer ${at}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined })
  assert.ok(r.ok, `관리 API ${method} ${p}: HTTP ${r.status}`)
  return r.status === 204 ? null : r.json()
}

async function tokenRequest(form) {
  const r = await fetch(`${REALM}/protocol/openid-connect/token`, { method: 'POST', body: new URLSearchParams(form) })
  const text = await r.text()
  // Deny access 인증기는 비밀번호 그랜트에서도 HTML 오류 화면을 돌려준다 (상태 코드는 401)
  try { return { status: r.status, body: JSON.parse(text) } } catch { return { status: r.status, body: { error: 'non_json_body' } } }
}

function summarize(tok) {
  const a = claims(tok.access_token)
  const id = tok.id_token ? claims(tok.id_token) : {}
  return { acr: a.acr, amr: a.amr || [], roles: ((a.realm_access || {}).roles || []).filter((r) => /^[A-Z_]+$|^mfa-required$/.test(r)).sort(),
    id_acr: id.acr, id_amr: id.amr, api_mfa: apiMfa(a), refresh_token: tok.refresh_token, access_token: tok.access_token }
}

// 한 브라우저 컨텍스트 = 한 사용자의 브라우저 (SSO 쿠키 공유)
async function browserSession(browser, redirect, user) {
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  async function login(extra = {}, { credentials = true } = {}) {
    const verifier = b64u(crypto.randomBytes(32))
    const q = new URLSearchParams({ client_id: 'marine-web', redirect_uri: redirect, response_type: 'code', scope: 'openid',
      code_challenge: b64u(crypto.createHash('sha256').update(verifier).digest()), code_challenge_method: 'S256',
      state: b64u(crypto.randomBytes(8)), ...extra })
    let codeUrl = null
    const onRequest = (r) => { if (r.url().startsWith(redirect)) codeUrl = r.url() }
    page.on('request', onRequest)
    const steps = []
    const submit = (sel) => Promise.all([page.waitForEvent('load'), page.click(sel)])
    try {
      await page.goto(`${REALM}/protocol/openid-connect/auth?${q}`)
      for (let i = 0; i < 6 && !codeUrl; i++) {
        if (await page.locator('#password').count()) {
          if (!credentials) { steps.push('password-form-shown'); break }
          if (steps.includes('password')) { steps.push('password-rejected'); break }
          steps.push('password')
          if (await page.locator('#username').count()) await page.fill('#username', user)
          await page.fill('#password', PASSWORD)
          await submit('#kc-login')
        } else if (await page.locator('#totp').count()) {
          if (steps.includes('otp-setup')) { steps.push('otp-setup-rejected'); break }
          steps.push('otp-setup')
          if (await page.locator('#mode-manual').count()) await page.click('#mode-manual')
          secrets[user] = (await page.locator('#kc-totp-secret-key').innerText()).replace(/\s/g, '')
          await page.fill('#totp', await nextCode(user))
          if (await page.locator('#userLabel').count()) await page.fill('#userLabel', 'mfa-check')
          await submit('#saveTOTPBtn')
        } else if (await page.locator('#otp').count()) {
          if (steps.includes('otp')) { steps.push('otp-rejected'); break }
          steps.push('otp')
          await page.fill('#otp', await nextCode(user))
          await submit('#kc-login')
        } else {
          await page.waitForTimeout(500)
          if (!codeUrl) steps.push('unexpected-page: ' + (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 160))
          break
        }
      }
    } finally {
      page.off('request', onRequest)
    }
    if (!codeUrl) return { steps, token: null }
    const cb = new URL(codeUrl)
    assert.equal(cb.searchParams.get('error'), null, `${user}: ${cb.searchParams.get('error')} ${cb.searchParams.get('error_description')}`)
    const t = await tokenRequest({ grant_type: 'authorization_code', client_id: 'marine-web', code: cb.searchParams.get('code'),
      redirect_uri: redirect, code_verifier: verifier })
    assert.equal(t.status, 200, `${user}: 코드 교환 실패 ${JSON.stringify(t.body)}`)
    return { steps, token: summarize(t.body) }
  }
  return { login, close: () => ctx.close() }
}

const results = []
const evidence = []
function record(scenario, user, how, r) {
  const t = r.token || {}
  evidence.push({ scenario, user, how, steps: r.steps, acr: t.acr, amr: t.amr, roles: t.roles, api_mfa: t.api_mfa,
    access_token: t.access_token })
  console.log(`  ${user.padEnd(9)} ${how.padEnd(34)} 단계=${JSON.stringify(r.steps)} acr=${JSON.stringify(t.acr)} amr=${JSON.stringify(t.amr)} MFA판정=${t.api_mfa}`)
}
async function scenario(name, fn) {
  console.log(`- ${name}`)
  try {
    await fn()
    results.push({ name, ok: true })
  } catch (e) {
    console.log(`  실패: ${e.message}`)
    results.push({ name, ok: false, error: e.message })
  }
}

;(async () => {
  assert.ok(ADMIN_PASSWORD, 'KC_ADMIN_PASSWORD가 필요하다 (시험 컨테이너의 임시 관리자)')
  const server = http.createServer((q, s) => s.end('ok'))
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const redirect = `http://127.0.0.1:${server.address().port}/callback`
  const [web] = await admin('GET', '/clients?clientId=marine-web')
  assert.equal((web.attributes || {})['default.acr.values'], '2', 'marine-web 기본 ACR이 2가 아니다')
  await admin('PUT', `/clients/${web.id}`, { ...web, redirectUris: [...web.redirectUris, `${redirect.replace(/\/callback$/, '')}/*`] })

  const browser = await loadPlaywright().chromium.launch()
  try {
    await scenario('일반 역할(ORG_MEMBER·PROGRAM_VIEWER·REVIEWER·CONSUMER)은 비밀번호만, acr 1 → MFA 아님', async () => {
      for (const user of ['member-a', 'member-b', 'viewer', 'reviewer', 'consumer']) {
        const s = await browserSession(browser, redirect, user)
        const r = await s.login()
        record('non_privileged', user, '첫 로그인', r)
        assert.deepEqual(r.steps, ['password'], `${user}: OTP 없이 비밀번호만 거쳐야 한다`)
        assert.equal(r.token.acr, '1'); assert.deepEqual(r.token.amr, ['pwd']); assert.equal(r.token.api_mfa, false)
        if (user === 'member-a') {
          const sso = await s.login()
          record('non_privileged_sso', user, 'SSO (같은 브라우저)', sso)
          assert.deepEqual(sso.steps, []); assert.equal(sso.token.acr, '1'); assert.equal(sso.token.api_mfa, false)
          const up = await s.login({ acr_values: '2' })
          record('non_privileged_sso_acr2', user, 'SSO + acr_values=2', up)
          assert.deepEqual(up.steps, [], 'LoA 2를 요청해도 일반 역할에 OTP를 강제하지 않는다')
          assert.equal(up.token.acr, '1'); assert.equal(up.token.api_mfa, false)
        }
        await s.close()
      }
    })

    for (const user of ['admin-a', 'operator']) {
      await scenario(`${user}: 첫 로그인에 OTP 등록 강제 → acr 2, 이후 로그인 OTP → acr 2 + amr otp`, async () => {
        const first = await browserSession(browser, redirect, user)
        const r1 = await first.login()
        record('privileged_enroll', user, '첫 로그인 (OTP 등록)', r1)
        assert.deepEqual(r1.steps, ['password', 'otp-setup'])
        assert.equal(r1.token.acr, '2'); assert.equal(r1.token.api_mfa, true)
        assert.ok(r1.token.roles.includes('mfa-required'))
        await first.close()
        const again = await browserSession(browser, redirect, user)
        const r2 = await again.login()
        record('privileged_login', user, '새 브라우저 로그인', r2)
        assert.deepEqual(r2.steps, ['password', 'otp'])
        assert.equal(r2.token.acr, '2'); assert.deepEqual(r2.token.amr, ['pwd', 'otp']); assert.equal(r2.token.api_mfa, true)
        const sso = await again.login()
        record('privileged_sso', user, 'SSO (같은 브라우저)', sso)
        assert.deepEqual(sso.steps, []); assert.equal(sso.token.acr, '2'); assert.equal(sso.token.api_mfa, true)
        const refreshed = await tokenRequest({ grant_type: 'refresh_token', client_id: 'marine-web', refresh_token: r2.token.refresh_token })
        assert.equal(refreshed.status, 200)
        const rt = { steps: ['refresh_token'], token: summarize(refreshed.body) }
        record('privileged_refresh', user, '토큰 갱신', rt)
        assert.equal(rt.token.acr, '2'); assert.equal(rt.token.api_mfa, true)
        await again.close()
      })
    }

    await scenario('admin-b: acr_values=1로 단계를 낮춰 요청해도 OTP를 거친다', async () => {
      const s1 = await browserSession(browser, redirect, 'admin-b')
      const r1 = await s1.login({ acr_values: '1' })
      record('privileged_tamper_enroll', 'admin-b', 'acr_values=1 첫 로그인', r1)
      assert.deepEqual(r1.steps, ['password', 'otp-setup'])
      // 등록 단계는 amr에 otp를 남기지 않고 LoA 2도 요청되지 않았으므로 이 토큰은 MFA로 인정되지 않는다 (API가 403 MFA_REQUIRED로 막는다)
      assert.equal(r1.token.acr, '1'); assert.equal(r1.token.api_mfa, false)
      await s1.close()
      const s2 = await browserSession(browser, redirect, 'admin-b')
      const r2 = await s2.login({ acr_values: '1' })
      record('privileged_tamper_login', 'admin-b', 'acr_values=1 새 브라우저 로그인', r2)
      assert.deepEqual(r2.steps, ['password', 'otp'])
      assert.equal(r2.token.acr, '1'); assert.ok(r2.token.amr.includes('otp')); assert.equal(r2.token.api_mfa, true)
      await s2.close()
    })

    await scenario('로그인 전 acr_values=0 요청은 로그인 화면으로 간다 (자격 증명 없이 코드가 나오지 않는다)', async () => {
      const s = await browserSession(browser, redirect, 'nobody')
      const r = await s.login({ acr_values: '0' }, { credentials: false })
      record('unauthenticated_acr0', '(없음)', 'acr_values=0', r)
      assert.deepEqual(r.steps, ['password-form-shown']); assert.equal(r.token, null)
      await s.close()
    })

    await scenario('개발용 비밀번호 그랜트(marine-dev-cli): 구성원은 통과, mfa-required 역할은 totp 필수', async () => {
      const grant = (user, extra = {}) => tokenRequest({ grant_type: 'password', client_id: 'marine-dev-cli', username: user, password: PASSWORD, ...extra })
      const m = await grant('member-a')
      assert.equal(m.status, 200)
      const mt = { steps: ['password_grant'], token: summarize(m.body) }
      record('direct_grant_member', 'member-a', '비밀번호 그랜트', mt)
      assert.equal(mt.token.acr, '1'); assert.deepEqual(mt.token.amr, ['pwd']); assert.equal(mt.token.api_mfa, false)
      const noOtp = await grant('admin-a')
      console.log(`  admin-a   비밀번호 그랜트 (totp 없음)            HTTP ${noOtp.status} ${noOtp.body.error}`)
      assert.equal(noOtp.status, 401, 'totp 없이 관리자 토큰을 주면 안 된다')
      const ok = await grant('admin-a', { totp: await nextCode('admin-a') })
      assert.equal(ok.status, 200, JSON.stringify(ok.body))
      const ot = { steps: ['password_grant+totp'], token: summarize(ok.body) }
      record('direct_grant_privileged', 'admin-a', '비밀번호 그랜트 + totp', ot)
      assert.deepEqual(ot.token.amr, ['pwd', 'otp']); assert.equal(ot.token.api_mfa, true)
      const unenrolled = await grant('operator2')
      console.log(`  operator2 비밀번호 그랜트 (OTP 미등록)          HTTP ${unenrolled.status} ${unenrolled.body.error}`)
      assert.equal(unenrolled.status, 401, 'OTP를 등록하지 않은 운영자에게 토큰을 주면 안 된다 (Deny access)')
    })
  } finally {
    await browser.close()
    server.close()
  }
  if (EVIDENCE) fs.writeFileSync(EVIDENCE, JSON.stringify({ issuer: REALM, mfa_acr_values: [...MFA_ACR], tokens: evidence.filter((e) => e.access_token) }))
  const failed = results.filter((r) => !r.ok)
  console.log(`\n결과: ${results.length - failed.length}/${results.length} 통과`)
  for (const f of failed) console.log(`  실패: ${f.name} — ${f.error}`)
  process.exit(failed.length ? 1 : 0)
})().catch((e) => { console.error(e); process.exit(1) })
