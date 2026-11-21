"""Replace index.bin's module-state snapshot with one from state.py (pytest import order).

usage: python tools/update_state.py STATE_JSON [KEEP_KEY ...]
KEEP_KEY: snapshot keys whose old value is kept (environment-dependent keys, e.g. tests skipped
by SQLite version, when the new snapshot was taken outside the scorer's image)."""
import json
import os
import pickle
import sys
import zlib

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(here, "index.bin")
d = pickle.loads(zlib.decompress(open(path, "rb").read()))
new = json.load(open(sys.argv[1]))
old = d["state"]
for k in sys.argv[2:]:
    for snap in ("snap", "snapl"):
        if k in old[snap]:
            new[snap][k] = old[snap][k]
        else:
            new[snap].pop(k, None)
st = dict(old, bad=new["bad"], snap=new["snap"], snapl=new["snapl"])
d["state"] = st
open(path, "wb").write(zlib.compress(pickle.dumps(d, protocol=pickle.HIGHEST_PROTOCOL), 9))
print("state keys", len(st["snap"]), "bad", st["bad"])
