"""URL parser differential probe: what does check_url vet vs what does _post connect to."""
import sys
sys.path.insert(0, "/home/user/oceandata/backend")
import httpx
from app.services import webhooks

urls = [
    "https://127.0.0.1/x", "https://2130706433/x", "https://0x7f000001/x", "https://0177.0.0.1/x", "https://127.1/x",
    "https://010.0.0.1/x", "https://0x7f.1/x", "https://017700000001/x", "https://127.0.0.1./x", "https://127.0.0.1../x",
    "HTTPS://127.0.0.1/x", "https://EXAMPLE.com/x", "https://LOCALHOST/x", "https://localhost/x", "https://localhost./x",
    "https://localhost../x", "https://foo.localhost/x", "https://foo.LOCAL./x", "https://a@127.0.0.1/x", "https://a:b@1.1.1.1/x",
    "https://1.1.1.1\\@127.0.0.1/x", "https://127.0.0.1\\@1.1.1.1/x", "https://1.1.1.1#@127.0.0.1/x", "https://1.1.1.1?@127.0.0.1/x",
    "https://1.1.1.1:443@127.0.0.1/x", "https://[fe80::1%25eth0]/x", "https://[2606:4700:4700::1111%25eth0]/x",
    "https://[::ffff:127.0.0.1]/x", "https://[::ffff:7f00:1]/x", "https://[::127.0.0.1]/x", "https://[::7f00:1]/x",
    "https://[0:0:0:0:0:ffff:127.0.0.1]/x", "https://[::ffff:0:127.0.0.1]/x", "https://[2002:7f00:1::]/x", "https://[64:ff9b::7f00:1]/x",
    "https://[64:ff9b:1::7f00:1]/x", "https://[2001::1]/x", "https://[100::1]/x", "https://[2001:db8::1]/x", "https://[3fff::1]/x",
    "https://1.1.1.1:0/x", "https://1.1.1.1:65536/x", "https://1.1.1.1:65535/x", "https://1.1.1.1:/x", "https://1.1.1.1:0443/x",
    "https://1.1.1.1:+443/x", "https://1.1.1.1: 443/x", "https://exa mple.com/x", "https://ex%61mple.com/x", "https://%31.1.1.1/x",
    "https://1%2e1.1.1/x", "https://münchen.de/x", "https://xn--mnchen-3ya.de/x", "https://ｌocalhost/x", "https://localhost。/x",
    "https://127。0。0。1/x", "https://①②⑦.0.0.1/x", "https://example.com%00.evil/x", "https://example.com\t.evil/x",
    "https://169.254.169.254/x", "https://[fd00:ec2::254]/x", "https://100.100.100.200/x", "https://192.0.0.192/x", "https://168.63.129.16/x",
    "https:127.0.0.1/x", "https:/127.0.0.1/x", "https:///127.0.0.1/x", "https://./x", "https://.../x", "https://localhost.localdomain/x",
    "https://ip6-localhost/x", "https://0/x", "https://0.0.0.0/x", "https://1.1.1.1.nip.io/x",
]
print(f"{'url':45s} | httpx(host,raw_host,port,netloc) | check_url(resolve=True) | pinned-url")
for url in urls:
    try:
        u = httpx.URL(url)
        info = (u.host, u.raw_host, u.port, u.netloc)
    except Exception as e:
        info = f"httpx-reject {type(e).__name__}"
    try:
        r = webhooks.check_url(url, allow_private=False, resolve=True)
        res = f"OK {r}"
        pins = []
        for a in r[:1]:
            try:
                pins.append(str(httpx.URL(url).copy_with(host=a)))
            except Exception as e:
                pins.append(f"copy_with-fail {type(e).__name__}: {e}")
    except webhooks.TargetRefused as e:
        res = f"REFUSED({e.reason})"
        pins = []
    except Exception as e:
        res = f"EXC {type(e).__name__}: {e}"
        pins = []
    print(f"{url!r:45.45} | {info} | {res} | {pins}")
