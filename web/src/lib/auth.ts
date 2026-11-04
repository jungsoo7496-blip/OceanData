// OIDC 로그인 (Keycloak, 인가 코드 + PKCE). 토큰은 세션 저장소에만 둔다.
import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'

const authority = import.meta.env.VITE_OIDC_AUTHORITY ?? 'http://localhost:8080/realms/marine'
const clientId = import.meta.env.VITE_OIDC_CLIENT_ID ?? 'marine-web'

export const userManager = new UserManager({
  authority,
  client_id: clientId,
  redirect_uri: `${window.location.origin}/callback`,
  post_logout_redirect_uri: `${window.location.origin}/`,
  response_type: 'code',
  scope: 'openid profile',
  automaticSilentRenew: true,
  userStore: new WebStorageStateStore({ store: window.sessionStorage }),
})

export async function currentUser(): Promise<User | null> {
  const u = await userManager.getUser()
  return u && !u.expired ? u : null
}

export const login = () => userManager.signinRedirect({ state: window.location.pathname })
export const logout = () => userManager.signoutRedirect()
