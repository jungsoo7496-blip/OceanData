import json, sys, httpx
E="/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/e2e12"
creds=json.load(open(f"{E}/agentdir/credentials.json"))
cl=httpx.Client(base_url="http://127.0.0.1:18416",timeout=30)
tok=cl.post("/v1/auth/token",data={"grant_type":"client_credentials","client_id":creds["client_id"],"client_secret":creds["client_secret"]}).json()["access_token"]
H={"Authorization":f"Bearer {tok}"}
for aid in sys.argv[1:]:
    r=cl.get(f"/v1/assets/{aid}/receipt",headers=H)
    print(aid, r.status_code, r.text[:400])
