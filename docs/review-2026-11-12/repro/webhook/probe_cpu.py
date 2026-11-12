"""Does a hostile receiver (1xx flood) steal CPU from the worker's main thread through the dispatcher thread (GIL)?"""
import os, subprocess, sys, threading, time, pathlib, tempfile
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services import webhooks
S_DIR = pathlib.Path("/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/rv12-webhook")
pf = str(pathlib.Path(tempfile.mkdtemp()) / "port")
p = subprocess.Popen([sys.executable, "-I", str(S_DIR / "flood_server.py"), pf])
for _ in range(50):
    if os.path.exists(pf) and open(pf).read(): break
    time.sleep(0.1)
port = int(open(pf).read())
def spin(seconds):
    n = 0; end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        for _ in range(1000): n += 1
    return n
base = spin(3.0)
res = {}
def dispatcher():
    t0 = time.monotonic()
    try:
        webhooks.http_send(f"http://hooks.example.test:{port}/h", b"{}", {}, 3.0, addresses=["127.0.0.1"])
    except Exception as e:
        res["exc"] = type(e).__name__
    res["took"] = time.monotonic() - t0
t = threading.Thread(target=dispatcher); t.start()
time.sleep(0.3)
under = spin(2.5)
t.join(10); p.kill()
print(f"main-thread throughput baseline={base/3.0:,.0f}/s, during 1xx flood={under/2.5:,.0f}/s -> {100*under/2.5/(base/3.0):.0f}% of baseline; send ended after {res.get('took'):.2f}s ({res.get('exc')})")
