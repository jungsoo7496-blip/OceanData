// 역할별 진입 (09번 요구사항 1): ORG 역할 → Workspace, PROGRAM_VIEWER → Program_Board, PLATFORM_OPERATOR → Operator_Console.
// 메뉴는 역할이 쓸 수 있는 것만 보인다. 서버 권한 검사는 따로 유지된다.
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Layout, Menu, Result, Space, Spin, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api, type Capabilities, type Me } from './lib/api'
import { currentUser, login, logout, userManager } from './lib/auth'
import { AssetDetail, Assets } from './pages/Assets'
import { Dashboard } from './pages/Dashboard'
import { Devices } from './pages/Devices'
import { Monitoring } from './pages/Monitoring'
import { ReportDetail, Reports, SessionRecords } from './pages/Reports'
import { ReviewQueue } from './pages/Review'
import { Catalog, ProviderAccess, SharingConsole } from './pages/Sharing'
import { OperatorConsole } from './pages/Operator'
import { ProgramBoard } from './pages/Program'
import { Sessions } from './pages/Sessions'
import { OrgSettings } from './pages/Settings'
import { Upload } from './pages/Upload'

const ORG = ['ORG_ADMIN', 'ORG_MEMBER']

export function homeFor(roles: string[]): string {
  if (roles.some((r) => ORG.includes(r))) return '/workspace'
  if (roles.includes('PLATFORM_OPERATOR')) return '/operator'
  if (roles.includes('PROGRAM_VIEWER')) return '/program'
  if (roles.includes('CONSUMER')) return '/catalog'
  if (roles.includes('REVIEWER')) return '/review'
  return '/forbidden'
}

function menuFor(roles: string[]) {
  const items: { key: string; label: JSX.Element }[] = []
  const add = (path: string, label: string) => items.push({ key: path, label: <Link to={path}>{label}</Link> })
  if (roles.some((r) => ORG.includes(r))) {
    add('/workspace', '대시보드')
    add('/upload', '업로드')
    add('/assets', '자산')
    add('/sessions', '실증회차')
    add('/reports', '보고서')
    add('/access', '이용 신청')
    add('/devices', '장비')
    if (roles.includes('ORG_ADMIN')) add('/settings', '기관 설정')
  }
  if (roles.includes('CONSUMER')) add('/catalog', '데이터셋 카탈로그')
  if (roles.includes('REVIEWER') || roles.includes('PLATFORM_OPERATOR')) add('/review', '비식별 검수')
  if (roles.includes('PROGRAM_VIEWER')) {
    add('/program', '관리기관 보드')
    add('/reports', '제출된 보고서')
  }
  if (roles.includes('PLATFORM_OPERATOR')) {
    add('/operator', '운영 콘솔')
    add('/monitoring', '수집 모니터링')
    add('/sharing', '공유·데이터셋')
  }
  return items
}

function Callback() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    userManager
      .signinRedirectCallback()
      .then(() => navigate('/', { replace: true }))
      .catch((e: Error) => setError(e.message))
  }, [navigate])
  return error ? <Result status="error" title="로그인 실패" subTitle={error} /> : <Spin fullscreen tip="로그인 중" />
}

// 기능이 꺼져 있거나 GPU가 없을 때 사용자 영향 안내 (09번 요구사항 2.3)
function CapabilityBanner() {
  const { data } = useQuery({ queryKey: ['capabilities'], queryFn: () => api<Capabilities>('/v1/system/capabilities') })
  if (!data) return null
  const notes: string[] = []
  if (!data.gpu_enabled) notes.push('GPU가 연결되지 않아 영상 처리(프록시·비식별)가 늦어질 수 있습니다')
  if (!data.report_formats.length) notes.push('보고서 출력 기능은 아직 꺼져 있습니다')
  return notes.length ? <Alert type="info" showIcon banner message={notes.join(' · ')} /> : null
}

function Shell({ me }: { me: Me }) {
  const loc = useLocation()
  const selected = '/' + (loc.pathname.split('/')[1] ?? '')
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <Typography.Text style={{ color: '#fff', fontWeight: 600, whiteSpace: 'nowrap' }}>해양 실증데이터 플랫폼</Typography.Text>
        <Menu theme="dark" mode="horizontal" selectedKeys={[selected]} items={menuFor(me.roles)} style={{ flex: 1 }} />
        <Space>
          <Typography.Text style={{ color: '#ddd' }}>{me.roles.join(', ')}</Typography.Text>
          <Button size="small" onClick={() => void logout()}>로그아웃</Button>
        </Space>
      </Layout.Header>
      <CapabilityBanner />
      <Layout.Content style={{ padding: 24 }} role="main">
        <Routes>
          <Route path="/" element={<Navigate to={homeFor(me.roles)} replace />} />
          <Route path="/workspace" element={<Dashboard me={me} />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/assets" element={<Assets me={me} />} />
          <Route path="/assets/:assetId" element={<AssetDetail me={me} />} />
          <Route path="/sessions" element={<Sessions me={me} />} />
          <Route path="/devices" element={<Devices me={me} />} />
          <Route path="/settings" element={<OrgSettings me={me} />} />
          <Route path="/program" element={<ProgramBoard />} />
          <Route path="/operator" element={<OperatorConsole />} />
          <Route path="/monitoring" element={<Monitoring />} />
          <Route path="/reports" element={<Reports me={me} />} />
          <Route path="/access" element={<ProviderAccess me={me} />} />
          <Route path="/sharing" element={<SharingConsole />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/review" element={<ReviewQueue me={me} />} />
          <Route path="/reports/:reportId" element={<ReportDetail me={me} />} />
          <Route path="/sessions/:sessionId/records" element={<SessionRecords me={me} />} />
          <Route path="*" element={<Result status="403" title="접근할 수 없는 화면입니다" />} />
        </Routes>
      </Layout.Content>
    </Layout>
  )
}

export function App() {
  const loc = useLocation()
  const [authed, setAuthed] = useState<boolean | null>(null)
  useEffect(() => {
    if (loc.pathname === '/callback') return
    void currentUser().then((u) => setAuthed(!!u))
  }, [loc.pathname])
  const me = useQuery({ queryKey: ['me'], queryFn: () => api<Me>('/v1/me'), enabled: authed === true })
  if (loc.pathname === '/callback') return <Callback />
  if (authed === false)
    return (
      <Result title="해양 실증데이터 플랫폼" subTitle="기관 계정으로 로그인하세요"
        extra={<Button type="primary" onClick={() => void login()}>로그인</Button>} />
    )
  if (me.error) return <Result status="error" title="사용자 정보를 불러오지 못했습니다" subTitle={String(me.error)} />
  if (!me.data) return <Spin fullscreen tip="불러오는 중" />
  return <Shell me={me.data} />
}
