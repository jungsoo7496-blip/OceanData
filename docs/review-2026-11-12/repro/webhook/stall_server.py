"""Out-of-process tarpit: accepts, never speaks; reports client-close (EOF) events. Usage: stall_server.py PORT_FILE MODE"""
import socket, sys, threading, time
port_file, mode = sys.argv[1], sys.argv[2]
srv = socket.create_server(("127.0.0.1", 0)); open(port_file, "w").write(str(srv.getsockname()[1]))
open(port_file + ".log", "w").close()
def log(msg):
    with open(port_file + ".log", "a") as f: f.write(msg + "\n")
def handle(c, n):
    t0 = time.monotonic()
    try:
        if mode == "tls-ok-silent":
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(sys.argv[3], sys.argv[4])
            c = ctx.wrap_socket(c, server_side=True)
        c.settimeout(60)
        while True:
            d = c.recv(65536)
            if not d: break
        log(f"conn{n} EOF after {time.monotonic()-t0:.2f}s")
    except Exception as e:
        log(f"conn{n} {type(e).__name__} after {time.monotonic()-t0:.2f}s")
n = 0
while True:
    c, _ = srv.accept(); n += 1
    threading.Thread(target=handle, args=(c, n), daemon=True).start()
