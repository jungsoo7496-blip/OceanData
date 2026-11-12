"""Does the cancel-during-TLS socket leak accumulate under *default* GC with a mostly idle worker? (py3.12, standalone copy of webhooks network code)"""
import asyncio, concurrent.futures, gc, os, re, ssl, subprocess, sys, threading, time, pathlib, tempfile
VENV = "/home/user/oceandata/backend/.venv/lib/python3.13/site-packages"
sys.path.insert(0, VENV)
import httpx
from functools import lru_cache
from collections.abc import Callable
from typing import Any, TypeVar
T = TypeVar("T")
src = open("/home/user/oceandata/backend/app/services/webhooks.py", encoding="utf-8").read()
a = src.index("@lru_cache(maxsize=1)\ndef _tls_context")
b = src.index("# ------------------------------------------------------------------ 전송: 가져오기·기록")
ns = dict(asyncio=asyncio, concurrent=concurrent, threading=threading, ssl=ssl, httpx=httpx, lru_cache=lru_cache, Callable=Callable, Any=Any, T=T)
class TargetRefused(Exception): pass
ns.update(TargetRefused=TargetRefused, REFUSED="refused", _lookup=lambda h, p: ["127.0.0.1"])
exec(src[a:b], ns)
_post = ns["_post"]
print("python", sys.version.split()[0], "gc thresholds", gc.get_threshold(), flush=True)

S_DIR = pathlib.Path("/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/rv12-webhook")
tmp = pathlib.Path(tempfile.mkdtemp()); pf = str(tmp / "port")
p = subprocess.Popen(["/usr/local/bin/python3.13", "-I", str(S_DIR / "stall_server.py"), pf, "raw-silent"])
for _ in range(50):
    if os.path.exists(pf) and open(pf).read(): break
    time.sleep(0.1)
port = int(open(pf).read())
def sockets(): return sum(1 for f in os.listdir("/proc/self/fd") if os.path.islink(f"/proc/self/fd/{f}") and os.readlink(f"/proc/self/fd/{f}").startswith("socket:"))

stop = threading.Event()
def churn():   # light "idle worker" allocation: ~ a few hundred container objects per second
    while not stop.is_set():
        for _ in range(20):
            d = {"a": [1, 2, 3], "b": {"c": (1, 2)}}; d["self"] = d
        time.sleep(0.05)
threading.Thread(target=churn, daemon=True).start()

start = time.monotonic(); peak = 0; n = 0; last = 0
while time.monotonic() - start < 100:
    async def one():
        try:
            await asyncio.wait_for(_post(f"https://hooks.example.test:{port}/h", b"{}", {}, 0.4, addresses=["127.0.0.1"]), 0.4)
        except Exception:
            pass
    asyncio.run(one()); n += 1
    s = sockets(); peak = max(peak, s)
    if time.monotonic() - last > 10:
        last = time.monotonic(); print(f"t={time.monotonic()-start:5.0f}s attempts={n:4d} open client sockets={s:4d} peak={peak}", flush=True)
stop.set(); p.kill()
