import gc, os, subprocess, sys, time, pathlib, tempfile
sys.path.insert(0, "/home/user/oceandata/backend")
for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"): os.environ.pop(k, None)
os.environ["NO_PROXY"] = "127.0.0.1"
from datetime import datetime, UTC
from app.services import monitoring
monitoring._ALERT_WEBHOOK_TIMEOUT_S = 0.3
S_DIR = pathlib.Path("/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/rv12-webhook")
tmp = pathlib.Path(tempfile.mkdtemp()); pf = str(tmp / "port")
p = subprocess.Popen([sys.executable, "-I", str(S_DIR / "stall_server.py"), pf, "raw-silent"])
for _ in range(50):
    if os.path.exists(pf) and open(pf).read(): break
    time.sleep(0.1)
port = int(open(pf).read())
def sockets(): return sum(1 for f in os.listdir("/proc/self/fd") if os.path.islink(f"/proc/self/fd/{f}") and os.readlink(f"/proc/self/fd/{f}").startswith("socket:"))
gc.disable(); base = sockets()
for i in range(15):
    monitoring.post_webhook(f"https://127.0.0.1:{port}/ops", None, {"type": "x"}, datetime.now(UTC))
print(f"alert path: client sockets base={base} after 15 TLS-stalled alerts={sockets()}; server saw {sum('EOF' in l for l in open(pf + '.log'))} client-closes")
gc.collect(); time.sleep(0.3)
print(f"after gc.collect(): sockets={sockets()}; server saw {sum('EOF' in l for l in open(pf + '.log'))} client-closes")
p.kill()
