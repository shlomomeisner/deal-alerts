#!/usr/bin/env python3
"""Price alerts — anyone types what they want and the price they'd pay on the
Steal Watch page (alerts.html). When a deal for it shows up at or under that price,
their phone gets an ntfy notification. Free, runs inside the GitHub Action.

How requests arrive: the page posts a small JSON message to the public ntfy topic
WANTS_TOPIC (no login, no server). Each scan this script reads new messages from it,
keeps the list in watches.json, and checks each watch against Slickdeals' search feed
plus every deal the watcher already collected.
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from watcher import fetch, parse_feed, notify, UA

ROOT = os.path.dirname(os.path.abspath(__file__))
WATCHES = os.path.join(ROOT, "watches.json")
DEALS = os.path.join(ROOT, "deals.json")
HUNTED = os.path.join(ROOT, "hunted.json")
WANTS_TOPIC = os.environ.get("WANTS_TOPIC", "steal-watch-wants-c7e2a80833")
SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh"
MAX_WATCHES = 300
CHECKS_PER_SCAN = 25          # each watch is searched about every 15-30 min
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
STOP = {"the", "a", "an", "for", "and", "or", "with", "of", "in", "on", "to", "new", "deal", "sale"}


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def words(q):
    return [w for w in re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", q.lower()) if w not in STOP]


def matches(title, q):
    t = " " + re.sub(r"[^a-z0-9]+", " ", title.lower()) + " "
    return all(f" {w} " in t or (len(w) > 4 and w in t) for w in words(q))


def price_of(title):
    """The deal price: first $ amount in the title (Slickdeals puts it there)."""
    m = re.search(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", title)
    return float(m.group(1).replace(",", "")) if m else None


def pull_requests(state):
    """Read new add/remove messages from the public wants topic."""
    since = state.get("since") or "all"
    url = f"{SERVER.rstrip('/')}/{WANTS_TOPIC}/json?poll=1&since={since}"
    try:
        raw = fetch(url).decode("utf-8", "ignore")
    except Exception as e:
        print("WARN wants topic:", e)
        return []
    out = []
    for line in raw.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") != "message":
            continue
        state["since"] = m.get("id", since)
        try:
            out.append(json.loads(m.get("message", "")))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def apply_requests(watches, reqs):
    by_id = {w["id"]: w for w in watches}
    for r in reqs:
        if not isinstance(r, dict):
            continue
        topic = str(r.get("topic", ""))
        wid = str(r.get("id", ""))[:40]
        if not TOPIC_RE.match(topic) or not re.match(r"^[A-Za-z0-9_-]{4,40}$", wid):
            continue
        if r.get("action") == "remove":
            w = by_id.get(wid)
            if w and w["topic"] == topic:      # only the person who made it can remove it
                watches.remove(w)
                by_id.pop(wid)
            continue
        q = re.sub(r"\s+", " ", str(r.get("q", ""))).strip()[:80]
        try:
            mx = round(float(r.get("max")), 2)
        except (TypeError, ValueError):
            continue
        if len(words(q)) == 0 or not (0 < mx < 100000) or wid in by_id:
            continue
        if sum(1 for w in watches if w["topic"] == topic) >= 25 or len(watches) >= MAX_WATCHES:
            continue
        w = {"id": wid, "q": q, "max": mx, "topic": topic, "created": now_iso(),
             "checked": "", "hits": [], "seen": []}
        watches.append(w)
        by_id[wid] = w
        print(f"new price alert: {q!r} under ${mx}")


def candidates(w, collected):
    """Deal posts for this watch: Slickdeals search feed + everything already collected."""
    items = []
    q = urllib.parse.quote_plus(" ".join(words(w["q"])))
    try:
        for it in parse_feed(fetch(f"https://slickdeals.net/newsearch.php?q={q}&searcharea=deals&searchin=first&rss=1")):
            items.append({"title": it["title"], "url": it["link"], "key": it["id"]})
    except Exception as e:
        print("WARN search", w["q"], e)
    for d in collected:
        items.append({"title": d.get("title", ""), "url": d.get("url", ""), "key": d.get("id", ""),
                      "price": d.get("price")})
    return items


def main():
    state = load(WATCHES, {"since": "", "watches": []})
    if isinstance(state, list):
        state = {"since": "", "watches": state}
    watches = state["watches"]
    apply_requests(watches, pull_requests(state))

    collected = load(DEALS, []) + load(HUNTED, [])
    watches.sort(key=lambda w: w.get("checked", ""))
    sent = 0
    for w in watches[:CHECKS_PER_SCAN]:
        first_look = not w.get("checked")
        seen = set(w.get("seen", []))
        best = []
        for it in candidates(w, collected):
            key = hashlib.sha1((it["key"] or it["url"]).encode()).hexdigest()[:12]
            if key in seen or not matches(it["title"], w["q"]):
                continue
            seen.add(key)
            p = it.get("price") if it.get("price") and price_of(it["title"]) is None else price_of(it["title"])
            if p is not None and p <= w["max"]:
                best.append((p, it))
        best.sort(key=lambda x: x[0])
        # first look: only the single cheapest current match; afterwards every new one (max 3 per scan)
        for p, it in best[: 1 if first_look else 3]:
            try:
                notify(w["topic"], SERVER, f"🎯 {w['q']} — ${p:g}",
                       f"{it['title']}\nYour price: ${w['max']:g}", it["url"], False, ["dart"])
                sent += 1
                w["hits"] = (w.get("hits", []) + [{"title": it["title"], "price": p, "url": it["url"],
                                                    "at": now_iso()}])[-5:]
                time.sleep(1)
            except Exception as e:
                print("WARN notify", e)
        w["seen"] = list(seen)[-400:]
        w["checked"] = now_iso()

    with open(WATCHES, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    print(f"price alerts: {len(watches)} watching, {sent} sent")


if __name__ == "__main__":
    main()
