#!/usr/bin/env python3
"""Taste — learns what you like from your Keep and ✕ taps on the page.

The page posts each tap to a public ntfy.sh drop box (TASTE_TOPIC). Every scan this
reads the new taps, keeps them in taste.json, and scores every deal:
  features: brand, store, category, price band and title words
  weight of a feature = log((keeps + 1) / (hides + 1)), so it moves only with evidence
A deal's taste score is its brand/store/category weights plus the average of its word
weights. Deals you'd clearly skip (score <= -2 with at least 3 taps behind it, or the
same item you ✕'d) are marked muted: hidden on the page and never alerted. Everything
else is ranked by score on the page (For you). Standard library only.
"""
import json
import math
import os
import re
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
TASTE = os.path.join(ROOT, "taste.json")
CONFIG = os.path.join(ROOT, "config.json")
FILES = [os.path.join(ROOT, "deals.json"), os.path.join(ROOT, "hunted.json")]
TOPIC = os.environ.get("TASTE_TOPIC", "steal-watch-taste-4f9a1c27e3")
SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh"
MUTE_AT = -2.0
MIN_EVIDENCE = 3
STOP = set("the and for with new from off sale deal deals free shipping only just one all now get your you men mens "
           "women womens size sizes pack set color colors black white more up to in on at of a an or by w".split())


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _brands():
    cfg = load(CONFIG, {})
    names = cfg.get("luxury_brands", []) + cfg.get("fashion_brands", []) + cfg.get("watch_words", [])
    return sorted({n.lower() for n in names if len(n) > 2}, key=len, reverse=True)


BRANDS = _brands()


def band(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    for top, name in ((50, "<50"), (150, "50-150"), (500, "150-500"), (1500, "500-1500")):
        if p < top:
            return name
    return "1500+"


def features(d):
    """[(feature, kind)] for a deal or a tap record."""
    t = str(d.get("title") or d.get("t") or "").lower()
    out = []
    for b in BRANDS:
        if re.search(r"(?<![a-z0-9])" + re.escape(b) + r"(?![a-z0-9])", t):
            out.append(("brand:" + b, "brand"))
            break
    store = (d.get("store") or d.get("s") or "").strip().lower()
    if store:
        out.append(("store:" + store, "store"))
    cat = d.get("category") or d.get("c")
    if cat:
        out.append(("cat:" + cat, "cat"))
    b = band(d.get("price") if d.get("price") is not None else d.get("p"))
    if b:
        out.append(("price:" + b, "price"))
    words = {w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", re.sub(r"\$[\d,.]+", " ", t)) if w not in STOP}
    out += [("w:" + w, "word") for w in sorted(words)[:25]]
    return out


def build(votes):
    counts = {}
    for v in votes.values():
        k = 0 if v["v"] > 0 else 1
        for f, _ in features(v):
            c = counts.setdefault(f, [0, 0])
            c[k] += 1
    return {"weights": {f: round(math.log((c[0] + 1) / (c[1] + 1)), 3) for f, c in counts.items()},
            "counts": counts, "hidden_keys": sorted({key(v) for v in votes.values() if v["v"] < 0}),
            "votes": len(votes), "keeps": sum(1 for v in votes.values() if v["v"] > 0)}


def key(d):
    t = str(d.get("title") or d.get("t") or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\$[\d,.]+.*$", "", t)).strip()[:60]


def score(model, d):
    """(score, muted)."""
    w, counts = model.get("weights", {}), model.get("counts", {})
    if not w:
        return 0.0, False
    s, words, evidence = 0.0, [], 0
    for f, kind in features(d):
        if f not in w:
            continue
        evidence += sum(counts.get(f, [0, 0]))
        if kind == "word":
            words.append(w[f])
        elif kind == "price":
            s += 0.5 * w[f]
        else:
            s += w[f]
    if words:
        s += sum(words) / len(words) * 1.5
    s = max(-5.0, min(5.0, s))
    muted = (s <= MUTE_AT and evidence >= MIN_EVIDENCE) or key(d) in set(model.get("hidden_keys", []))
    return round(s, 2), muted


def load_model():
    return load(TASTE, {}).get("model", {})


def pull(state):
    since = state.get("since") or "all"
    url = f"{SERVER.rstrip('/')}/{TOPIC}/json?poll=1&since={since}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "deal-alerts/1.0"})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    except Exception as e:
        print("WARN taste topic:", e)
        return []
    out = []
    for line in raw.splitlines():
        try:
            m = json.loads(line)
            if m.get("event") == "message":
                state["since"] = m.get("id", since)
                out.append(json.loads(m.get("message", "")))
        except (ValueError, TypeError):
            continue
    return out


def apply(votes, taps):
    n = 0
    for t in taps:
        if not isinstance(t, dict) or t.get("a") not in ("keep", "unkeep", "hide", "unhide"):
            continue
        i = re.sub(r"[^A-Za-z0-9_-]", "", str(t.get("id", "")))[:64]
        if not i:
            continue
        if t["a"] in ("unkeep", "unhide"):
            votes.pop(i, None)
        else:
            votes[i] = {"v": 1 if t["a"] == "keep" else -1, "t": str(t.get("t", ""))[:160],
                        "s": str(t.get("s", ""))[:40], "c": str(t.get("c", ""))[:20],
                        "p": t.get("p") if isinstance(t.get("p"), (int, float)) else None,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        n += 1
    return n


def main():
    state = load(TASTE, {})
    votes = state.get("votes", {})
    n = apply(votes, pull(state))
    model = build(votes) if (n or "model" not in state) else state["model"]
    state.update(votes=votes, model=model, updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    muted = 0
    for path in FILES:
        deals = load(path, None)
        if not isinstance(deals, list):
            continue
        changed = False
        for d in deals:
            s, m = score(model, d) if model.get("weights") else (0.0, False)
            if d.get("taste") != s or bool(d.get("muted")) != m:
                changed = True
                if s:
                    d["taste"] = s
                else:
                    d.pop("taste", None)
                if m:
                    d["muted"] = True
                else:
                    d.pop("muted", None)
            muted += m
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(deals, f, indent=1, ensure_ascii=False)
    with open(TASTE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    print(f"taste: {n} new taps, {len(votes)} total ({model.get('keeps', 0)} kept), {muted} deals muted")


if __name__ == "__main__":
    main()
