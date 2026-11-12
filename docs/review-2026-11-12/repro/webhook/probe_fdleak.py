"""Do timed-out attempts leave the client socket open (fd leak window)? Server is out-of-process."""
import asyncio, gc, os, subprocess, sys, time, pathlib, tempfile
sys.path.insert(0, "/home/user/oceandata/backend"); sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
from app.services import webhooks
from tests.conftest import test_settings
import test_webhook_send as T
S_DIR = pathlib.Path("/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/rv12-webhook")
tmp = pathlib.Path(tempfile.mkdtemp())
server_ctx, trusted = T.self_signed(tmp, "hooks.example.test")
webhooks._tls_context.cache_clear(); webhooks._tls_context = lambda: trusted
webhooks._lookup = lambda host, port: ["127.0.0.1"]
S = test_settings(webhook_allow_private_targets=True, webhook_timeout_s=0.3)
def nfd(): return len(os.listdir("/proc/self/fd"))
def sockets(): return sum(1 for f in os.listdir("/proc/self/fd") if os.path.islink(f"/proc/self/fd/{f}") and os.readlink(f"/proc/self/fd/{f}").startswith("socket:"))

def scenario(mode, label, disable_gc):
    pf = str(tmp / f"port_{mode}_{label.replace(chr(32), chr(95))}_{disable_gc}")
    args = [sys.executable, "-I", str(S_DIR / "stall_server.py"), pf, mode, str(tmp / "hooks.example.test.crt"), str(tmp / "hooks.example.test.key")]
    p = subprocess.Popen(args)
    for _ in range(50):
        if os.path.exists(pf) and open(pf).read(): break
        time.sleep(0.1)
    port = int(open(pf).read())
    if disable_gc: gc.disable()
    else: gc.enable()
    base = sockets()
    for i in range(20):
        cl = webhooks._Claim("d", "w", 1, f"https://hooks.example.test:{port}/h", b"{}", {})
        r = asyncio.run(webhooks._attempt(cl, S, None))
    after_attempts = sockets()
    time.sleep(0.5)
    log1 = open(pf + ".log").read().splitlines()
    print(f"[{label} gc_disabled={disable_gc}] client sockets: base={base} after 20 timed-out attempts={after_attempts}; server saw {sum('EOF' in l for l in log1)} client-closes of 20 (last result {r})")
    gc.enable(); n = gc.collect(); time.sleep(0.3)
    log2 = open(pf + ".log").read().splitlines()
    print(f"    after gc.collect(): client sockets={sockets()}; server saw {sum('EOF' in l for l in log2)} client-closes")
    p.kill()

scenario("raw-silent", "stall before TLS", True)
scenario("raw-silent", "stall before TLS", False)
scenario("tls-ok-silent", "TLS ok then no response", True)
scenario("tls-ok-silent", "TLS ok then no response", False)
