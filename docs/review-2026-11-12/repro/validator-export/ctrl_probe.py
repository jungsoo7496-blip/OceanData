import io, sys, zipfile
sys.path.insert(0, "/home/user/oceandata/backend")
from app.adapters import report_renderers as rr
from hwpx_probe import mk_template  # noqa  (prints probe results on import; ignore)
