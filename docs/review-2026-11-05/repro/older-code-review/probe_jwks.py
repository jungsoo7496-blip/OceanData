"""Unknown-kid tokens force a JWKS refetch per request (under a global lock)."""
import base64, json, sys
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters.keycloak_identity import KeycloakIdentity
from app.ports.identity import InvalidToken

calls = {"n": 0}
def provider():
    calls["n"] += 1
    return {"keys": []}

idp = KeycloakIdentity("http://idp.test/realms/marine", "marine-api", jwks_provider=provider)
def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
for i in range(50):
    tok = b64({"alg": "RS256", "kid": f"k{i}", "typ": "JWT"}) + "." + b64({"sub": "x"}) + ".AAAA"
    try:
        idp.verify_access_token(tok)
    except InvalidToken:
        pass
print("JWKS fetches for 50 garbage tokens:", calls["n"])
