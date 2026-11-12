import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import koKR from 'antd/locale/ko_KR'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import './a11y.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } })

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* KWCAG 명도 대비 4.5:1 이상 (09번 요구사항 11.2, 검사 npm run test:a11y): 기본 파랑·보조 글자색을 진하게.
        오류 빨강(위험 버튼 글자·입력 오류 안내)은 AntD 기본 #ff4d4f가 흰 바탕 대비 3.0:1이라 red-7(5.6:1)로.
        마우스를 올렸을 때 색은 AntD가 밝게 만들어 대비가 떨어지므로(파랑 4.2:1) 한 단계 진하게.
        선택 상자 안내 문구(placeholder)는 실제 글자로 그려져 기본 1.8:1 → 4.6:1 */}
    <ConfigProvider locale={koKR} theme={{ token: {
      colorPrimary: '#0958d9', colorError: '#cf1322', colorTextSecondary: 'rgba(0,0,0,0.68)', colorTextDescription: 'rgba(0,0,0,0.68)',
      colorTextTertiary: 'rgba(0,0,0,0.62)', colorLink: '#0958d9', colorPrimaryHover: '#003eb3', colorLinkHover: '#003eb3',
      colorErrorHover: '#a8071a', colorTextPlaceholder: 'rgba(0,0,0,0.54)' },
      components: { Menu: { darkItemSelectedBg: '#003eb3', darkItemColor: 'rgba(255,255,255,0.85)' } } }}>
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
)
