// 09번 스펙(workspace-portal)에서 Ant Design(ko_KR)·TanStack Query·Uppy로 화면을 만든다.
export function App() {
  return (
    <main style={{ fontFamily: 'sans-serif', padding: 24 }}>
      <h1>해양 실증데이터 플랫폼</h1>
      <p>개발 환경이 준비되었습니다. 화면은 09번 스펙에서 구현합니다.</p>
      <ul>
        <li>API 문서: <a href="http://localhost:8000/docs">http://localhost:8000/docs</a></li>
        <li>로그인(Keycloak): <a href="http://localhost:8080/realms/marine/account">계정 화면</a></li>
      </ul>
    </main>
  )
}
