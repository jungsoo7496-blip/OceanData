"""Soak: fd/thread leaks across many _attempt rounds (ok, stall-before-TLS, hung DNS)."""
import asyncio, os, sys, threading, time, pathlib, tempfile, socket
sys.path.insert(0, "/home/user/oceandata/backend"); sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
from app.services import webhooks
from tests.conftest import test_settings
import test_webhook_send as T

tmp = pathlib.Path(tempfile.mkdtemp())
server_ctx, trusted = T.self_signed(tmp, "hooks.example.test")
webhooks._tls_context.cache_clear(); webhooks._tls_context = lambda: trusted
gate = threading.Event()
def fake_lookup(host, port):
    if host.startswith("hang"):
        gate.wait(30); raise socket.gaierror(socket.EAI_NONAME, "x")
    return ["127.0.0.1"]
webhooks._lookup = fake_lookup
S = test_settings(webhook_allow_private_targets=True, webhook_timeout_s=0.2)
def nfd(): return len(os.listdir("/proc/self/fd"))
ok = T.SyntheticReceiver(T.respond_204, tls=server_ctx)
stall = T.SyntheticReceiver(T.silent)   # raw: never speaks TLS
time.sleep(0.2)
base_fd, base_th = nfd(), threading.active_count()
print("baseline fds", base_fd, "threads", base_th)
def attempt(url):
    cl = webhooks._Claim("d", "w", 1, url, b"{}", {"Content-Type": "application/json"})
    return asyncio.run(webhooks._attempt(cl, S, None))
t0 = time.monotonic()
for i in range(60):
    attempt(f"https://hooks.example.test:{ok.port}/h")
print("after 60 ok:   fds", nfd(), "threads", threading.active_count(), f"{time.monotonic()-t0:.1f}s")
for i in range(30):
    attempt(f"https://hooks.example.test:{stall.port}/h")
print("after 30 stalls: fds", nfd(), "threads", threading.active_count())
for i in range(30):
    attempt(f"https://hang{i}.example.test/h")
print("after 30 hung DNS: fds", nfd(), "threads", threading.active_count(), "(webhook-io threads:", sum(1 for t in threading.enumerate() if t.name == 'webhook-io'), ")")
gate.set(); time.sleep(1.0)
print("after DNS release: fds", nfd(), "threads", threading.active_count(), "webhook-io:", sum(1 for t in threading.enumerate() if t.name == 'webhook-io'))
