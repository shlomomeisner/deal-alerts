#!/usr/bin/env python3
"""Price alerts — anyone types what they want, the price they'd pay and their cell number
on the Steal Watch page (alerts.html). When a deal for it shows up at or under that price,
they get a text message (Twilio, see sms.py). Runs inside the GitHub Action.

How requests arrive: the page posts a small JSON message to a public ntfy.sh mailbox
(WANTS_TOPIC) — nobody needs the ntfy app; it's only used as a free drop box. Phone
numbers are encrypted in the browser with the public key in alerts.html and can only be
read here with the ALERT_KEY secret, so they never appear in plain text anywhere. Each scan this script reads new messages from it,
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

from watcher import fetch, parse_feed
import sms

ROOT = os.path.dirname(os.path.abspath(__file__))
WATCHES = os.path.join(ROOT, "watches.json")
DEALS = os.path.join(ROOT, "deals.json")
HUNTED = os.path.join(ROOT, "hunted.json")
WANTS_TOPIC = os.environ.get("WANTS_TOPIC", "steal-watch-wants-c7e2a80833")
SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh"
MAX_WATCHES = 300
CHECKS_PER_SCAN = 25          # each watch is searched about every 15-30 min
HEX64 = re.compile(r"^[0-9a-f]{64}$")
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
        if r.get("action") == "search":
            q = re.sub(r"\s+", " ", str(r.get("q", ""))).strip()[:60]
            if len(words(q)) and not any(x["q"].lower() == q.lower() for x in SEARCHES):
                SEARCHES.append({"q": q, "at": now_iso(), "done": 0})
            continue
        wid = str(r.get("id", ""))[:40]
        if not re.match(r"^[A-Za-z0-9_-]{4,40}$", wid):
            continue
        if r.get("action") == "remove":
            w = by_id.get(wid)
            secret = str(r.get("token", ""))[:128]
            # only the browser that made it (it holds the secret token) can remove it
            if w and hashlib.sha256(secret.encode()).hexdigest() == w["owner"]:
                watches.remove(w)
                by_id.pop(wid)
            continue
        owner, contact = str(r.get("owner", "")), str(r.get("contact", ""))
        if not HEX64.match(owner) or not re.match(r"^[A-Za-z0-9+/=]{300,400}$", contact):
            continue
        q = re.sub(r"\s+", " ", str(r.get("q", ""))).strip()[:80]
        try:
            mx = round(float(r.get("max")), 2)
        except (TypeError, ValueError):
            continue
        if len(words(q)) == 0 or not (0 < mx < 100000) or wid in by_id:
            continue
        if sum(1 for w in watches if w["owner"] == owner) >= 25 or len(watches) >= MAX_WATCHES:
            continue
        w = {"id": wid, "q": q, "max": mx, "owner": owner, "contact": contact, "created": now_iso(),
             "checked": "", "hits": [], "seen": []}
        watches.append(w)
        by_id[wid] = w
        print(f"new price alert: {q!r} under ${mx}")


SEARCHES = []
AUTO_PER_SCAN = 12
BRANDS = ["Rolex", "Breitling", "Omega", "Tudor", "Cartier", "TAG Heuer", "IWC", "Valentino", "Gucci", "Prada",
          "Saint Laurent", "Bottega Veneta", "Ferragamo", "Balenciaga", "Burberry", "Tom Ford", "Moncler",
          "Canada Goose", "Golden Goose", "Loro Piana", "Brunello Cucinelli", "Louboutin", "Versace", "Fendi",
          "Givenchy", "Dior", "Chanel", "Hermes", "Celine", "Loewe", "Montblanc", "Stone Island", "Amiri",
          "Common Projects", "Ralph Lauren", "UGG", "Birkenstock", "Allen Edmonds", "On Cloud", "Hoka",
          "Ray-Ban", "Oakley", "Persol", "Oliver Peoples", "Maui Jim", "Lululemon", "Alo Yoga", "Creed",
          "Parfums de Marly", "Le Labo", "Byredo", "Maison Francis Kurkdjian", "Xerjoff", "Kilian"]
TECH = ["AirPods Pro", "AirPods Max", "iPhone", "iPad Pro", "MacBook Air", "MacBook Pro", "Apple Watch Ultra",
        "Beats", "JBL", "Sony WH-1000XM", "Bose QuietComfort", "Sonos", "PS5 Pro", "PlayStation 5", "Xbox Series X",
        "Nintendo Switch 2", "DualSense", "Ray-Ban Meta", "LG OLED", "Sony Bravia OLED", "Samsung Galaxy S",
        "Dyson", "DJI", "GoPro", "Garmin", "Trek bike", "Specialized bike", "Lectric e-bike", "Aventon e-bike"]
TRIPS = ["St Maarten", "Turks and Caicos", "Aruba", "Punta Cana", "Jamaica", "Bahamas", "Puerto Rico", "Cancun",
         "St Lucia", "Barbados", "Curacao", "Costa Rica", "Hawaii", "Cayman Islands", "Virgin Islands"]


def auto_queries():
    q = []
    for b in BRANDS:
        q += [f"{b} sale", f"{b} price drop"]
    for t in TECH:
        q += [f"{t} deal", f"{t} record low"]
    for t in TRIPS:
        q += [f"New York to {t} flight deal"]
    q += ["designer sale extra off", "luxury watch price drop", "error fare New York", "lululemon we made too much",
          "Nordstrom designer sale", "Saks designer sale", "Woot Apple deal", "Costco watch deal"]
    return q


def run_searches(state):
    """Search requests typed on the page: pull matching deal posts into deals.json (free)."""
    from watcher import load as wload, first_price, guess_category, max_percent_off
    cfg = wload(os.path.join(ROOT, "config.json"), {})
    junk = [j.lower() for j in cfg.get("junk_words", [])]
    keep = [x for x in state.get("searches", []) + SEARCHES
            if x["at"] >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400))]
    uniq = {}
    for x in keep:
        uniq.setdefault(x["q"].lower(), x)
    state["searches"] = list(uniq.values())[-60:]
    todo = sorted(state["searches"], key=lambda x: x.get("last", ""))[:8]
    allq = auto_queries()   # plus a never-ending rotation through every brand, gadget and trip
    i = int(state.get("auto_i", 0)) % len(allq)
    auto = [{"q": allq[(i + k) % len(allq)], "auto": True} for k in range(AUTO_PER_SCAN)]
    state["auto_i"] = (i + AUTO_PER_SCAN) % len(allq)
    todo = todo + auto
    deals = wload(DEALS, [])
    have = {d["id"] for d in deals}
    added = 0
    for x in todo:
        q = urllib.parse.quote_plus(" ".join(words(x["q"])))
        items = []
        for url in (f"https://slickdeals.net/newsearch.php?q={q}&searcharea=deals&searchin=first&rss=1",
                    f"https://news.google.com/rss/search?q={q}+when:7d&hl=en-US&gl=US&ceid=US%3Aen",
                    f"https://www.bing.com/news/search?q={q}&format=rss&qft=interval%3d%228%22"):
            try:
                items += parse_feed(fetch(url))
            except Exception as e:
                print("WARN search feed", e)
        need = x["q"]
        if x.get("auto"):
            need = re.sub(r"(?i)\b(sale|price drop|deal|record low|flight deal|extra off)\b", "", need).strip()
            need = re.sub(r"(?i)^new york to ", "", need)
        for it in items[:25]:
            t = it["title"]
            if not matches(t, need) or not re.search(r"\$\s?\d|\d+%\s*off", t, re.I):
                continue
            if x.get("auto") and re.search(r"(?i)\b(best|top \d+|\d+ best|review|vs\.?|how to|guide|stock|shares|earnings|lawsuit)\b", t):
                continue   # articles, not deals
            if any(re.search(r"(?<![a-z0-9])" + re.escape(j) + r"(?![a-z0-9])", t.lower()) for j in junk):
                continue
            did = hashlib.sha1(("search|" + it["id"]).encode()).hexdigest()[:16]
            if did in have:
                continue
            have.add(did)
            full = f"{t} {it['text']}"
            deals.append({"id": did, "title": t, "url": it["link"], "why": "" if x.get("auto") else f"Found for your search “{x['q']}”",
                          "category": guess_category(full, {"category": "everything"}, cfg),
                          "source": ("Web search: " if x.get("auto") else "Search: ") + x["q"], "urgent": False, "hot": False,
                          "pct": max_percent_off(full) or None, "price": first_price(t), "found_at": now_iso()})
            added += 1
        if not x.get("auto"):
            x["last"] = now_iso()
    if added:
        with open(DEALS, "w", encoding="utf-8") as f:
            json.dump(deals[-2000:], f, indent=1, ensure_ascii=False)
    print(f"searches: {len(todo)} run, {added} deals added")
    return added


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
    run_searches(state)

    collected = load(DEALS, []) + load(HUNTED, [])
    watches.sort(key=lambda w: w.get("checked", ""))
    sent = 0
    if not (sms.configured() and os.environ.get("ALERT_KEY")):
        print("price alerts: Twilio / ALERT_KEY secrets not set yet — saving watches only")
        watches_to_check = []
    else:
        watches_to_check = watches[:CHECKS_PER_SCAN]
    for w in watches_to_check:
        try:
            phone = sms.decrypt(w["contact"])
        except Exception:
            print("WARN can't read a phone number; skipping", w["id"])
            continue
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
                sms.send_sms(phone, f"🎯 Price alert: {w['q']} is ${p:g} (your price ${w['max']:g})\n"
                                    f"{it['title'][:120]}\n{it['url']}\nStop: reply STOP")
                sent += 1
                w["hits"] = (w.get("hits", []) + [{"title": it["title"], "price": p, "url": it["url"],
                                                    "at": now_iso()}])[-5:]
                time.sleep(1)
            except Exception as e:
                print("WARN notify", e)
        w["seen"] = list(seen)[-400:]
        w["checked"] = now_iso()

    state["texts_ready"] = bool(sms.configured() and os.environ.get("ALERT_KEY"))
    with open(WATCHES, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    print(f"price alerts: {len(watches)} watching, {sent} sent")


if __name__ == "__main__":
    main()
