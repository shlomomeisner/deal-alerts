#!/usr/bin/env python3
"""Repack the add-on files into watcher.py's self-install bundle.

watcher.py rewrites these files from its bundle on every run, so after editing any
of them run `python bundle.py` (or the edit gets reverted on the next scan).
`python bundle.py --check` exits 1 if the bundle is out of date.
"""
import base64, os, re, sys, zlib

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["cleanup.py", "pricewatch.py", "sms.py", "stores.py", "flights.py", "taste.py", "index.html", "alerts.html"]


def packed():
    out = []
    for name in FILES:
        if os.path.exists(os.path.join(ROOT, name)):
            blob = base64.b64encode(zlib.compress(open(os.path.join(ROOT, name), "rb").read(), 9)).decode()
            out.append(f'"{name}": "{blob}",')
    return "_BUNDLE = {\n" + "\n".join(out) + "\n}"


def main():
    path = os.path.join(ROOT, "watcher.py")
    src = open(path, encoding="utf-8").read()
    new = re.sub(r"_BUNDLE = \{\n.*?\n\}", lambda m: packed(), src, count=1, flags=re.S)
    if "--check" in sys.argv:
        sys.path.insert(0, ROOT)
        import watcher
        have = {k: zlib.decompress(base64.b64decode(v)) for k, v in watcher._BUNDLE.items()}
        want = {n: open(os.path.join(ROOT, n), "rb").read() for n in FILES if os.path.exists(os.path.join(ROOT, n))}
        sys.exit(0 if have == want else "bundle out of date: run python bundle.py")
    if new != src:
        open(path, "w", encoding="utf-8").write(new)
        print("bundle updated")
    else:
        print("bundle already up to date")


if __name__ == "__main__":
    main()
