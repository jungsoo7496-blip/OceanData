import json, sys, os
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document
m = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))
# keys with / ~ and unicode inside a map-typed field (original_timestamps: additionalProperties {type:string,maxLength:64})
d = {**m, "original_timestamps": {"a/b": 1, "c~d": 2, "~1": 3, "": 4, "한글/키": 5, "x~1y": 6}}
for p in validate_document("upload-manifest", d)[0]:
    print(repr(p.pointer), p.code, p.message[:60])
print("----- ordering determinism (hash seed) -----")
d2 = {**m, "zzz": 1, "aaa": 2, "mmm": 3, "original_timestamps": {"k%d" % i: i for i in range(30)}}
out = [(p.pointer, p.code, p.message[:100]) for p in validate_document("upload-manifest", d2)[0]]
print(hash(json.dumps(out)) % 10**8 if False else __import__("hashlib").sha256(json.dumps(out).encode()).hexdigest()[:16], len(out))
for o in out[:6]: print(o)
