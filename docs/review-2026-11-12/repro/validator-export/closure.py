"""Compute the transitive runtime-dependency closure of the main [project.dependencies] (what `pip install .` pulls in)."""
import re, sys, tomllib
from importlib import metadata
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

pp = tomllib.load(open("/home/user/oceandata/backend/pyproject.toml", "rb"))
roots = [Requirement(r) for r in pp["project"]["dependencies"]]
seen = {}
stack = [(r, ()) for r in roots]
while stack:
    req, extras = stack.pop()
    name = canonicalize_name(req.name)
    ex = tuple(sorted(set(req.extras)))
    key = (name, ex)
    if key in seen:
        continue
    seen[key] = True
    try:
        dist = metadata.distribution(req.name)
    except metadata.PackageNotFoundError:
        print("not installed in venv:", req.name); continue
    for line in dist.requires or []:
        r = Requirement(line)
        if r.marker is not None:
            # evaluate for each requested extra (or none)
            ok = False
            for e in (list(ex) or [""]):
                if r.marker.evaluate({"extra": e}):
                    ok = True
            if not ok:
                continue
        stack.append((r, ()))
names = sorted({k[0] for k in seen})
print(len(names), "packages in closure")
for n in ["rfc3339-validator", "jsonpointer", "rfc3987", "fqdn", "idna", "isoduration", "uri-template", "webcolors", "rfc3986-validator"]:
    print(f"{n:22s}", "IN closure" if n in names else "NOT in closure")
