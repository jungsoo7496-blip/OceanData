"""Time every `pattern` in contracts/schemas against adversarial inputs (python re, as jsonschema uses re.search)."""
import glob, json, re, time, itertools
pats = set()
def walk(n):
    if isinstance(n, dict):
        if isinstance(n.get("pattern"), str): pats.add(n["pattern"])
        for v in n.values(): walk(v)
    elif isinstance(n, list):
        for v in n: walk(v)
for f in glob.glob("/home/user/oceandata/contracts/schemas/*.json"): walk(json.load(open(f)))
print(len(pats), "distinct patterns")
N = 200_000
alphabet_inputs = ["a" * N, "a" * N + "!", "0" * N, "0" * N + "Z", "/" * N, "a/" * (N // 2), "a/" * (N // 2) + "!", ("a" * 50 + "/") * (N // 51),
                   "A_" * (N // 2), "dev_" + "A" * N, "1." * (N // 2), "1.1." * (N // 4), "2026-11-02T00:00:00" + "." * N, "x" * N + "\n", "-" * N, "+" * N, "." * N,
                   "[a-z0-9.+-]" * (N // 12)]
worst = []
for p in sorted(pats):
    rx = re.compile(p)
    for s in alphabet_inputs:
        t = time.perf_counter(); rx.search(s); dt = time.perf_counter() - t
        worst.append((dt, p, s[:12] + f"...(len {len(s)})"))
worst.sort(reverse=True)
for dt, p, s in worst[:6]: print(f"{dt*1000:9.2f} ms  {p[:60]:60s} {s!r}")
