import os, sys, time, threading, socket, logging
sys.path.insert(0, "/home/user/oceandata/backend")
for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
from datetime import datetime, UTC
from app.logging_setup import configure_logging
configure_logging("INFO")
from app.services import monitoring
monitoring._ALERT_WEBHOOK_TIMEOUT_S = 1.0
orig = socket.getaddrinfo
gate = threading.Event()
calls = []
def slow_dns(host, *a, **k):
    calls.append((host, time.monotonic()))
    h = host.decode() if isinstance(host, bytes) else (host or "")
    if h.endswith(".hang.test"):
        gate.wait(6); raise socket.gaierror(socket.EAI_NONAME, "hung")
    return orig(host, *a, **k)
socket.getaddrinfo = slow_dns
for i in range(6):
    t0 = time.monotonic()
    monitoring.post_webhook(f"https://h{i}.hang.test/ops", None, {"type": "x"}, datetime.now(UTC))
    print(f"alert #{i} returned in {time.monotonic()-t0:5.2f}s; slots free={monitoring._ALERT_DNS_SLOTS._value}; dns calls so far={len(calls)}", flush=True)
gate.set(); time.sleep(0.3)
print("slots free after release:", monitoring._ALERT_DNS_SLOTS._value)
