"""Does the webhook send path leak URL path/query secrets into the JSON application log (INFO default)?"""
import sys, io, logging, socket, ssl, threading, tempfile, pathlib
sys.path.insert(0, "/home/user/oceandata/backend")
sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
from app.logging_setup import configure_logging
from app.services import webhooks, monitoring
from datetime import datetime, UTC
import test_webhook_send as T  # reuse SyntheticReceiver + self_signed helpers

buf = io.StringIO()
configure_logging("INFO")
root = logging.getLogger()
root.handlers[0].stream = buf   # capture the app's JSON handler

tmp = pathlib.Path(tempfile.mkdtemp())
server, trusted = T.self_signed(tmp, "hooks.example.test")
rx = T.SyntheticReceiver(T.respond_204, tls=server)
URL = f"https://hooks.example.test:{rx.port}/services/T0001/B0002/PATHSECRET123?token=QUERYSECRET456"
code = webhooks.http_send(URL, b'{"a":1}', {"Content-Type": "application/json"}, 3.0, addresses=["127.0.0.1"], verify=trusted)
print("status", code)

# operator alert path (plain http to local receiver, trust_env)
import os
os.environ["NO_PROXY"] = "127.0.0.1"
rx2 = T.SyntheticReceiver(T.respond_204)
monitoring.post_webhook(f"http://127.0.0.1:{rx2.port}/services/T0001/B0002/OPSPATHSECRET789?token=OPSQUERY", None, {"type": "watch.opened"}, datetime.now(UTC))
print("---- captured app log ----")
print(buf.getvalue())
