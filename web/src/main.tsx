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
    {/* KWCAG 명도 대비 4.5:1 이상 (09번 요구사항 11.2): 기본 파랑·보조 글자색을 진하게 */}
    <ConfigProvider locale={koKR} theme={{ token: {
      colorPrimary: '#0958d9', colorTextSecondary: 'rgba(0,0,0,0.68)', colorTextDescription: 'rgba(0,0,0,0.68)',
      colorTextTertiary: 'rgba(0,0,0,0.62)', colorLink: '#0958d9' },
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
