"""Prove/refute: TTL-0 DNS rebinding between vetting and connect is moot (pinned), and the wire request is signed/pinned correctly."""
import asyncio, socket, sys, tempfile, pathlib, threading, time, json
sys.path.insert(0, "/home/user/oceandata/backend"); sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
from app.services import webhooks
from tests.conftest import test_settings
import test_webhook_send as T

tmp = pathlib.Path(tempfile.mkdtemp())
server_ctx, trusted = T.self_signed(tmp, "rebind.example.test")
webhooks._tls_context.cache_clear(); webhooks._tls_context = lambda: trusted

# Two listeners on different loopback addresses, same port: 127.0.0.1 (first answer) and 127.0.0.2 (the flipped answer)
def listener(host, port, tag, hits):
    s = socket.create_server((host, port))
    def serve():
        while True:
            try: c, _ = s.accept()
            except OSError: return
            hits.append(tag)
            def one(c=c):
                try:
                    c = server_ctx.wrap_socket(c, server_side=True)
                    req = T.read_request(c); hits.append((tag, req)); c.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                except Exception: pass
                finally: c.close()
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return s
hits = []
s1 = listener("127.0.0.1", 0, "FIRST-127.0.0.1", hits)
port = s1.getsockname()[1]
s2 = listener("127.0.0.2", port, "REBOUND-127.0.0.2", hits)

calls = []
real = socket.getaddrinfo
def flipping(host, port_, *a, **k):
    if host == "rebind.example.test":
        calls.append(time.monotonic())
        ip = "127.0.0.1" if len(calls) == 1 else "127.0.0.2"     # TTL-0: every later lookup returns the other address
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port_))]
    return real(host, port_, *a, **k)
socket.getaddrinfo = flipping
S = test_settings(webhook_allow_private_targets=True, webhook_timeout_s=3.0)
cl = webhooks._Claim("d", "w", 1, f"https://rebind.example.test:{port}/hook", b'{"k":1}', {"Content-Type": "application/json", "X-Event-Id": "EV1"})
res = asyncio.run(webhooks._attempt(cl, S, None))
print("result", res, "| DNS lookups for the name during ONE attempt:", len(calls), "| connections seen:", [h if isinstance(h, str) else h[0] for h in hits])
