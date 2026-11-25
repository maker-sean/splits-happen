"""Flatten the richest saved response per endpoint into a field inventory (path, type, non-null count).

Usage: uv run python -I scripts/inventory.py > <out>.txt
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def walk(obj, prefix, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        if obj and all(isinstance(x, (int, float)) or x is None for x in obj) and prefix.endswith("]"):
            out[prefix + "[]"].append(type(obj[0]).__name__)
            return
        if not obj:
            out[prefix + "[]"].append("empty")
        for x in obj[:50]:
            walk(x, prefix + "[]", out)
    else:
        out[prefix].append("null" if obj is None else type(obj).__name__)


def main():
    for d in sorted(p for p in RAW.iterdir() if p.is_dir() and not p.name.startswith("_")):
        files = [f for f in d.glob("*.json")]
        bodies = []
        for f in files:
            env = json.loads(f.read_text())
            if env.get("status") == 200 and env.get("body") not in (None, "", [], {}):
                bodies.append(env["body"])
        print(f"\n### {d.name}  ({len(files)} files, {len(bodies)} with 200+body)")
        out = defaultdict(list)
        for b in bodies:
            walk(b, "", out)
        for k, types in out.items():
            nonnull = sum(t not in ("null", "empty") for t in types)
            kinds = sorted(set(types) - {"null"}) or ["null"]
            print(f"  {k or '<root>'}: {'/'.join(kinds)}  nonnull={nonnull}/{len(types)}")


if __name__ == "__main__":
    sys.exit(main())
