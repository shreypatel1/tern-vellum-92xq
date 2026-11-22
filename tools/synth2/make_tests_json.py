#!/usr/bin/env python3
"""Build a tests.json for selector.py from index.bin.

selector.py maps test numbers<->ids via tests.json. We synthesize it directly
from the index so selector runs standalone on the box. seconds come from the
index's recorded per-test cpu (used only for packing/cost; truth is from pytest).
"""
import sys, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import analyze

def main(index_path, out):
    ix = analyze.Index(index_path)
    tests = [{"id": tid, "number": i, "seconds": float(ix.cpu[i])}
             for i, tid in enumerate(ix.ids)]
    with open(out, "w") as f:
        json.dump({"tests": tests}, f)
    print(f"{len(tests)} tests -> {out}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
