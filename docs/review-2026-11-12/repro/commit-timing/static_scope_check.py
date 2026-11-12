"""rv12 (1): stronger static check on the *runtime* dependants (FastAPI 0.162 builds per-inclusion copies)."""
import inspect, sys, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, ".")
from fastapi.routing import iter_route_contexts, APIRoute
from sqlalchemy import create_engine
from tests.conftest import test_settings, StaticIdentity, FakeStorage
from app.adapters.notifier import LogNotifier
from app.container import build_container
from app.main import create_app
from app.api.deps import get_db
import importlib, pkgutil, app.api

c = build_container(test_settings(), engine=create_engine("postgresql+pg8000://u:p@127.0.0.1:1/x"),
                    storage=FakeStorage(), identity=StaticIdentity(), notifier=LogNotifier())
app = create_app(c)

def is_gen(fn):
    return inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn)

def walk(d, acc, depth=0):
    for sub in d.dependencies:
        acc.append((sub.call, sub.scope, getattr(sub, "use_cache", True)))
        walk(sub, acc, depth + 1)

rows = []
for rc in iter_route_contexts(app.router.routes):
    d = getattr(rc, "dependant", None)
    if d is None:
        rows.append((rc.path, "NO-DEPENDANT", type(rc.original_route).__name__)); continue
    acc = []
    walk(d, acc)
    gens = [(call.__name__, scope) for call, scope, _ in acc if is_gen(call)]
    dbs = [(s) for call, s, _ in acc if call is get_db]
    other_gens = [g for g in gens if g[0] != "get_db"]
    rows.append((rc.path, sorted(rc.methods or []), len(dbs), sorted(set(map(str, dbs))), other_gens,
                 type(rc.original_route).__name__))

api_rows = [r for r in rows if len(r) == 6]
print("runtime route contexts:", len(rows), " with dependant:", len(api_rows))
print("routes with get_db:", sum(1 for r in api_rows if r[2] > 0), " without:", [ (r[0], r[1]) for r in api_rows if r[2] == 0])
print("non-function get_db scopes:", [(r[0], r[1], r[3]) for r in api_rows if r[3] not in ([], ["function"])])
print("other yield deps:", [(r[0], r[4]) for r in api_rows if r[4]])
print("non-APIRoute contexts:", [r for r in rows if len(r) != 6 or r[5] != "APIRoute"])

# compare with the repo unit test's view (module-level routers)
mod = set()
for info in pkgutil.iter_modules(app_api := __import__("app.api", fromlist=["x"]).__path__):
    m = importlib.import_module(f"app.api.{info.name}")
    rt = getattr(m, "router", None)
    if rt is not None:
        for r in rt.routes:
            if isinstance(r, APIRoute):
                for meth in r.methods:
                    mod.add((meth, r.path))
rt_set = {(m, r[0]) for r in api_rows for m in r[1]}
print("module-level routes:", len(mod), " runtime routes:", len(rt_set), " only-runtime:", sorted(rt_set - mod)[:10], " only-module:", sorted(mod - rt_set)[:10])
# how many APIRouter objects exist in app/api modules?
import re, pathlib
cnt = {}
for p in pathlib.Path("app/api").glob("*.py"):
    cnt[p.name] = len(re.findall(r"APIRouter\(", p.read_text()))
print("modules with !=1 APIRouter():", {k: v for k, v in cnt.items() if v != 1 and k != '__init__.py'})
