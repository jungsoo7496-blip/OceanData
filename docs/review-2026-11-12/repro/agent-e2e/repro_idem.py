"""Minimal reproduction: same content, different file name, same source/session/rule -> agent-style Idempotency-Key
(file name NOT part of the key) collides with a different body -> 409 IDEMPOTENCY_CONFLICT instead of 200 ALREADY_REGISTERED."""
import hashlib, json, httpx
E="/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/e2e12"
creds=json.load(open(f"{E}/agentdir/credentials.json"))
cl=httpx.Client(base_url="http://127.0.0.1:18416",timeout=30)
tok=cl.post("/v1/auth/token",data={"grant_type":"client_credentials","client_id":creds["client_id"],"client_secret":creds["client_secret"]}).json()["access_token"]
H={"Authorization":f"Bearer {tok}"}
src="dev_00000000000000000000000302"; sha=hashlib.sha256(open(f"{E}/data/clip_small.mp4","rb").read()).hexdigest()
key="lc-"+hashlib.sha256("|".join([src,sha,"CLIP","A-2026-11-01","cam01"]).encode()).hexdigest()[:32]
def manifest(name):
    return {"schema_version":"1.0.0","source_system_id":src,"watch_rule_id":"cam01","data_type":"VIDEO","variant":"CLIP",
            "data_role":"CORE","session_label":"A-2026-11-01",
            "file":{"original_filename":name,"original_relative_path":f"A-2026-11-01/cam01/{name}","byte_size":604837,
                    "checksum_sha256":sha,"mime_type":"video/mp4"},
            "client":{"kind":"AGENT","version":"0.1.0-dev","local_collection_id":key},
            "capture_start_at":"2026-11-01T01:02:03Z","timestamp_quality":{"source":"DEVICE","verified":False}}
for label,name,hdr in (("same name, agent key (replay of original upload)","clip_small.mp4",{"Idempotency-Key":key}),
                       ("other name, agent key  (what the agent sent for clip_dup.mp4)","clip_dup.mp4",{"Idempotency-Key":key}),
                       ("other name, NO key     (server dedupe path)","clip_dup.mp4",{})):
    r=cl.post("/v1/uploads",headers={**H,**hdr},json=manifest(name))
    print(f"{label:70s} -> {r.status_code} {r.json().get(chr(99)+chr(111)+chr(100)+chr(101)) or r.json().get(chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))} replayed={r.headers.get(chr(73)+chr(100)+chr(101)+chr(109)+chr(112)+chr(111)+chr(116)+chr(101)+chr(110)+chr(116)+chr(45)+chr(82)+chr(101)+chr(112)+chr(108)+chr(97)+chr(121)+chr(101)+chr(100))}")
