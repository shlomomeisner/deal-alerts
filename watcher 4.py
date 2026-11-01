#!/usr/bin/env python3
"""Deal Alerts watcher.

Polls deal feeds (Slickdeals, Reddit deal subs, Secret Flying, The Flight Deal,
9to5Toys ...), keeps only posts that match the rules in config.json, and sends a
push notification to your phone through ntfy (https://ntfy.sh).

Runs every 5 minutes from GitHub Actions. Uses only the Python standard library.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html import unescape

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
SEEN = os.path.join(ROOT, "seen.json")
DEALS = os.path.join(ROOT, "deals.json")
UA = "deal-alerts/1.0 (personal deal watcher; github.com/shlomomeisner/deal-alerts)"
MAX_SEEN = 5000
MAX_ALERTS_PER_RUN = 15
TROPICAL = ["st. maarten", "st maarten", "sint maarten", "st. martin", "st martin", "saint martin", "sxm",
            "turks", "caicos", "providenciales", "pls", "aruba", "curacao", "curaçao", "bonaire", "barbados",
            "jamaica", "montego bay", "mbj", "nassau", "bahamas", "exuma", "punta cana", "puj", "santo domingo",
            "dominican republic", "puerto rico", "san juan", "sju", "st. thomas", "st thomas", "st. croix",
            "st croix", "virgin islands", "antigua", "st. lucia", "st lucia", "saint lucia", "grenada",
            "trinidad", "tobago", "cayman", "anguilla", "st. kitts", "st kitts", "nevis", "st. barts",
            "st barts", "st. barth", "martinique", "guadeloupe", "dominica", "bermuda", "caribbean",
            "cancun", "cancún", "cun", "tulum", "cozumel", "cabo", "los cabos", "puerto vallarta",
            "costa rica", "liberia", "belize", "roatan", "panama", "cartagena", "hawaii", "honolulu",
            "maui", "kona", "kauai", "key west", "miami", "fort lauderdale", "bali", "phuket", "maldives",
            "fiji", "tahiti", "bora bora", "seychelles", "mauritius", "zanzibar", "thailand"]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_tags(s):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_feed(raw):
    """Return [{id, title, link, text}] from RSS 2.0 or Atom."""
    items = []
    root = ET.fromstring(raw)
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        text = strip_tags(it.findtext("description"))
        items.append({"id": guid, "title": title, "link": link, "text": text})
    for e in root.iter(atom + "entry"):
        title = strip_tags(e.findtext(atom + "title"))
        link_el = e.find(atom + "link")
        link = link_el.get("href", "") if link_el is not None else ""
        guid = (e.findtext(atom + "id") or link or title).strip()
        text = strip_tags(e.findtext(atom + "content") or e.findtext(atom + "summary"))
        items.append({"id": guid, "title": title, "link": link, "text": text})
    return items


def has_any(text, words):
    t = text.lower()
    for w in words:
        w = w.lower()
        # whole-word match for short tokens (e.g. "jfk", "lga"), substring otherwise
        if len(w) <= 4:
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t):
                return w
        elif w in t:
            return w
    return None


def max_percent_off(text):
    """Largest 'NN% off' figure mentioned (ignores 'extra 10% off' style small codes only if lower)."""
    nums = [int(n) for n in re.findall(r"(\d{1,2})\s?%\s?(?:off|discount|savings|below)", text, re.I)]
    # "$189 (reg $350)", "$189, was $350", "$189 (list $350)", "$189 retail $350"
    for now, was in re.findall(r"\$\s?([\d,]+(?:\.\d{2})?)[^$]{0,25}?\b(?:reg\.?|regularly|was|list|retail|orig\.?|msrp)\b[^$\d]{0,6}\$\s?([\d,]+(?:\.\d{2})?)", text, re.I):
        n, w = float(now.replace(",", "")), float(was.replace(",", ""))
        if 0 < n < w:
            nums.append(int(round((1 - n / w) * 100)))
    return max(nums) if nums else 0


def evaluate(item, rule, cfg):
    """Return (matched: bool, reason: str, urgent: bool)."""
    title, text = item["title"], item["text"]
    full = f"{title} {text}"
    urgent_word = has_any(full, cfg["urgent_words"])
    urgent = bool(urgent_word)
    pct = max_percent_off(full)
    kind = rule["filter"]

    if any(has_any(full, [w]) for w in cfg.get("exclude_words", [])):
        return False, "", False

    if kind == "flights":
        home = has_any(full, cfg["home_airports"])
        if not home:
            return False, "", False
        # Shlomo's rule: EVERY flight deal from NYC to the tropics, at any price
        island = has_any(full, TROPICAL)
        if island:
            return True, f"🌴 {home.upper()} → {island.title()}", urgent
        if urgent:
            return True, f"{urgent_word} from {home.upper()}", True
        if has_any(title, cfg["flight_class_words"]):
            return True, f"premium cabin from {home.upper()}", False
        price = re.search(r"\$\s?(\d{2,4})", title)
        if price and int(price.group(1)) <= cfg["flight_max_price"]:
            return True, f"${price.group(1)} from {home.upper()}", False
        return False, "", False

    if kind == "fashion":
        brand = has_any(full, cfg["luxury_brands"])
        if brand and (pct >= cfg["luxury_min_pct"] or urgent):
            return True, f"{brand.title()} {pct}% off" if pct else f"{brand.title()} {urgent_word}", urgent
        brand2 = has_any(full, cfg["fashion_brands"])
        if brand2 and (pct >= cfg["fashion_min_pct"] or urgent):
            return True, f"{brand2.title()} {pct}% off" if pct else f"{brand2.title()} {urgent_word}", urgent
        if pct >= cfg["any_min_pct"]:
            return True, f"{pct}% off", urgent
        if urgent:
            return True, urgent_word, True
        return False, "", False

    if kind == "tech":
        thing = has_any(full, cfg["tech_words"])
        if not thing:
            return False, "", False
        low = has_any(full, cfg["record_low_words"])
        if low or pct >= cfg["tech_min_pct"] or urgent:
            why = low or (f"{pct}% off" if pct else urgent_word)
            return True, f"{thing.title()} — {why}", urgent
        return False, "", False

    if kind == "watches":
        w = has_any(full, cfg.get("watch_words", []))
        if w and (pct >= cfg.get("watch_min_pct", 35) or urgent or has_any(full, cfg["record_low_words"])):
            return True, f"{w.title()} {pct}% off" if pct else f"{w.title()} deal", urgent
        return False, "", False

    if kind == "fragrance":
        f = has_any(full, cfg["fragrance_words"])
        if f and (pct >= cfg["fragrance_min_pct"] or urgent or has_any(full, cfg["record_low_words"])):
            return True, f"{f.title()} {pct}% off" if pct else f"{f.title()} deal", urgent
        return False, "", False

    if kind == "everything":
        for sub in ("watches", "fashion", "fragrance", "tech"):
            ok, why, urg = evaluate(item, {"filter": sub}, cfg)
            if ok:
                return ok, why, urg
        if pct >= cfg["any_min_pct"] or urgent:
            return True, f"{pct}% off" if pct else urgent_word, urgent
        return False, "", False

    if kind == "all":
        return True, "", urgent

    return False, "", False


HOTEL = re.compile(r"\b(hotel|resort|/double|/night|per night|inn\b|suites?\b|hostel|airbnb|cruise|car rental)", re.I)
FLIGHTY = re.compile(r"\b(flights?|fares?|round[- ]?trip|r/?t|one[- ]?way|nonstop|non-stop|basic economy|economy|"
                     r"business class|first class|airlines?|jetblue|delta|united|american|spirit|frontier|"
                     r"norse|la compagnie|icelandair|tap|aer lingus|emirates|\u2013|\u2014|\u2192| to )\b", re.I)


def is_nyc_flight(it, feed, cfg):
    """Every flight deal leaving New York gets saved for the Flights tab, any price."""
    if feed.get("category") != "flights":
        return False
    full = f"{it['title']} {it['text'][:300]}"
    return bool(has_any(it["title"], cfg["home_airports"]) and FLIGHTY.search(it["title"])
                and re.search(r"\$\s?\d", full) and not HOTEL.search(it["title"]))


def guess_category(text, feed, cfg):
    if feed["category"] == "flights" or has_any(text, ["flight", "airfare", "round-trip", "roundtrip", "nonstop"]):
        return "flights"
    if has_any(text, cfg["fragrance_words"]):
        return "fragrance"
    if feed["category"] == "watches" or has_any(text, cfg.get("watch_words", [])):
        return "watches"
    if has_any(text, cfg["luxury_brands"]) or has_any(text, cfg["fashion_brands"]) or has_any(text, ["shoes", "sneakers", "jacket", "coat", "jeans", "shirt", "dress", "boots", "loafers", "hoodie", "handbag", "wallet", "belt", "sunglasses"]):
        return "fashion"
    if has_any(text, cfg["tech_words"]):
        return "tech"
    if has_any(text, ["amazon"]) or feed["category"] == "amazon":
        return "amazon"
    return feed["category"] if feed["category"] in ("fashion", "fragrance", "tech", "flights") else "everything"


def first_price(title):
    m = re.search(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", title)
    return float(m.group(1).replace(",", "")) if m else None


def allowed(text, feed, cfg):
    if has_any(text, cfg.get("junk_words", [])):
        return False
    if has_any(text, cfg["luxury_words"]):
        return True
    if cfg.get("keep_tech") and has_any(text, cfg.get("tech_keep_words", [])):
        return True
    if cfg.get("keep_flights") and feed["category"] == "flights":
        return True
    return False


def notify(topic, server, title, body, link, urgent, tags):
    data = body.encode("utf-8")
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "ignore"),
        "Priority": "urgent" if urgent else "high",
        "Tags": ",".join(tags),
        "User-Agent": UA,
    }
    if link:
        headers["Click"] = link
        headers["Actions"] = f"view, Open deal, {link}"
    req = urllib.request.Request(f"{server.rstrip('/')}/{urllib.parse.quote(topic)}",
                                 data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def main():
    cfg = load(CONFIG, None)
    if not cfg:
        sys.exit("config.json missing or invalid")
    seen_list = load(SEEN, [])
    seen = set(seen_list)
    first_run = not seen_list
    # A feed added later gets the same quiet first look, so old posts don't flood alerts.
    known_feeds = {k.split("|", 1)[0] for k in seen_list}
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh"
    dry = os.environ.get("DRY_RUN") == "1" or not topic

    alerts, found, new_ids, errors = [], [], [], []
    # Core feeds every scan; "rotate" feeds split into 3 groups, one group per scan
    # (so each is checked every ~15 min) to stay polite to the sites.
    slot = int(time.time() // 300) % 3
    todo, k = [], 0
    for feed in cfg["feeds"]:
        if feed.get("tier") == "rotate":
            k += 1
            if k % 3 != slot:
                continue
        todo.append(feed)

    def grab(feed):
        try:
            return feed, parse_feed(fetch(feed["url"])), None
        except Exception as e:  # one bad feed never stops the rest
            return feed, [], e

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(grab, todo))
    print(f"checked {len(todo)} of {len(cfg['feeds'])} sources this scan")

    for feed, items, err in results:
        if err is not None:
            errors.append(f"{feed['name']}: {err}")
            continue
        for it in items:
            key = f"{feed['name']}|{it['id']}"
            if key in seen:
                continue
            new_ids.append(key)
            if first_run or feed["name"] not in known_feeds:
                # first look only records what's already out there — except NYC flights,
                # which are saved for the Flights tab right away (no alert)
                if not first_run and is_nyc_flight(it, feed, cfg):
                    found.append((False, feed, it, ""))
                continue
            if cfg.get("luxury_only") and not allowed(f"{it['title']} {it['text']}", feed, cfg):
                continue  # luxury, premium tech, sunglasses and flights only; no junk
            ok, why, urgent = evaluate(it, feed, cfg)
            if ok:
                alerts.append((urgent, feed, it, why))
            if ok or feed.get("save") == "all" or is_nyc_flight(it, feed, cfg):
                found.append((urgent, feed, it, why))

    alerts.sort(key=lambda a: not a[0])  # urgent first
    if not topic and alerts:  # no ntfy: text the owner instead (sms.py bundles them)
        import sms
        sms.queue([{"title": it["title"], "url": it["link"], "urgent": urgent, "now": "🌴" in why}
                   for urgent, feed, it, why in alerts[:MAX_ALERTS_PER_RUN]])
    sent = 0
    for urgent, feed, it, why in alerts[:MAX_ALERTS_PER_RUN]:
        title = ("🚨 " if urgent else "🔥 ") + (why or feed["category"].title())
        body = f"{it['title']}\n({feed['name']})"
        tags = [feed["category"], "rotating_light" if urgent else "fire"]
        if dry:
            print("[dry-run]", title, "|", it["title"], "|", it["link"])
        else:
            try:
                notify(topic, server, title, body, it["link"], urgent, tags)
                sent += 1
                time.sleep(1)
            except Exception as e:
                errors.append(f"notify: {e}")

    # Every deal from curated deal sites (plus every alert) goes to the Steal Watch app.
    if found:
        deals = load(DEALS, [])
        have = {d["id"] for d in deals}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for urgent, feed, it, why in found:
            did = hashlib.sha1(f"{feed['name']}|{it['id']}".encode()).hexdigest()[:16]
            if did in have:
                continue
            full = f"{it['title']} {it['text']}"
            deals.append({
                "id": did, "title": it["title"], "url": it["link"],
                "why": why or "", "category": guess_category(full, feed, cfg),
                "source": feed["name"], "urgent": urgent, "hot": bool(why) or urgent,
                "pct": max_percent_off(full) or None, "price": first_price(it["title"]),
                "found_at": now,
            })
        with open(DEALS, "w", encoding="utf-8") as f:
            json.dump(deals[-2000:], f, indent=1, ensure_ascii=False)

    seen_list.extend(new_ids)
    with open(SEEN, "w", encoding="utf-8") as f:
        json.dump(seen_list[-MAX_SEEN:], f, indent=0)

    print(f"new posts: {len(new_ids)}  matched: {len(alerts)}  sent: {sent}"
          + ("  (first run: recorded existing posts, no alerts)" if first_run else ""))
    for e in errors:
        print("WARN", e)


def run_extras():
    """Run the add-ons after each scan, so they work even with the original workflow file:
    store links + sold-out cleanup, price alerts and the text digest."""
    import subprocess
    for script in ("cleanup.py", "pricewatch.py", "sms.py"):
        if os.path.exists(os.path.join(ROOT, script)):
            try:
                subprocess.run([sys.executable, os.path.join(ROOT, script)], timeout=240, check=False)
            except Exception as e:
                print("WARN", script, e)
    extra = [f for f in ("hunted.json", "status.json", "watches.json", "outbox.json")
             if os.path.exists(os.path.join(ROOT, f))]
    if extra and os.path.isdir(os.path.join(ROOT, ".git")):
        subprocess.run(["git", "add", *extra], cwd=ROOT, check=False)


# ---- self-install: this one file carries the add-ons and the web pages --------------------
# On each run it writes cleanup.py, pricewatch.py, sms.py, index.html and alerts.html next to
# itself (only when they changed) and adds extra junk words to config.json, so updating the
# whole system is just uploading this watcher.py.
_BUNDLE = {
"cleanup.py": "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",
"pricewatch.py": "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",
"sms.py": "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",
"index.html": "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",
"alerts.html": "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"
}
_EXTRA_JUNK = ["movie", "blu-ray", "bluray", "dvd", "4k uhd blu", "digital code", "paperback", "hardcover", "kindle edition", "audiobook", "ebook", "soap", "shampoo", "conditioner", "body wash", "detergent", "toothpaste", "paper towel", "toilet paper", "trash bag", "diapers", "wipes", "vitamin", "supplement", "protein powder", "snack", "coffee pods", "k-cup", "cereal", "grocery", "pet food", "cat litter", "dog food", "lightbulb", "light bulb", "batteries", "phone case", "screen protector", "usb-c cable", "charging cable", "t-shirt 3-pack", "socks", "digital download", "digital game", "video game", "game key", "nintendo switch game", "(nintendo switch)", "ps5 game", "xbox digital", "pc digital", "steam key", "old navy", "devil wears prada", "gap factory", "kids'", "toddler", "stylus", "folio", "keyboard case", "tablet case", "usb hub", "palm rejection"]
_EXTRA_FEEDS = [{"name": "Google: NYC Caribbean flights", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=%22New+York%22+flights+%28Caribbean+OR+%22St.+Maarten%22+OR+%22Turks+and+Caicos%22+OR+Aruba+OR+%22Punta+Cana%22+OR+Bahamas%29+deal+when%3A3d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Google: NYC tropical fares", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=cheap+flights+%22from+New+York%22+%28Cancun+OR+%22Puerto+Rico%22+OR+Jamaica+OR+%22Costa+Rica%22+OR+Hawaii+OR+%22St.+Lucia%22%29+when%3A3d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Google: Secret Flying NYC", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=site%3Asecretflying.com+%22New+York%22+when%3A7d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Google: Flight Deal NYC", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=site%3Atheflightdeal.com+%22New+York%22+when%3A7d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Google: Going NYC", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=%22cheap+flights%22+%22New+York%22+%28Going+OR+%22Thrifty+Traveler%22+OR+%22Scott%27s+Cheap+Flights%22%29+when%3A3d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Google: JetBlue sale", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=JetBlue+sale+fares+when%3A3d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Google: NYC error fare", "category": "flights", "filter": "flights", "url": "https://news.google.com/rss/search?q=%28%22error+fare%22+OR+%22mistake+fare%22+OR+%22price+glitch%22%29+flights+when%3A3d&hl=en-US&gl=US&ceid=US%3Aen"}, {"name": "Slickdeals: flights", "category": "flights", "filter": "flights", "url": "https://slickdeals.net/newsearch.php?q=flight&searcharea=deals&searchin=first&rss=1"}, {"name": "Slickdeals: airfare", "category": "flights", "filter": "flights", "url": "https://slickdeals.net/newsearch.php?q=airfare&searcharea=deals&searchin=first&rss=1"}, {"name": "Slickdeals: JetBlue", "category": "flights", "filter": "flights", "url": "https://slickdeals.net/newsearch.php?q=jetblue&searcharea=deals&searchin=first&rss=1"}, {"name": "Reddit r/flightdeals", "category": "flights", "filter": "flights", "url": "https://www.reddit.com/r/flightdeals/.rss"}, {"name": "Reddit r/TravelDeals", "category": "flights", "filter": "flights", "url": "https://www.reddit.com/r/TravelDeals/.rss"}]


def self_install():
    import base64, zlib, subprocess
    changed = []
    for name, blob in _BUNDLE.items():
        data = zlib.decompress(base64.b64decode(blob))
        path = os.path.join(ROOT, name)
        try:
            old = open(path, "rb").read()
        except FileNotFoundError:
            old = None
        if old != data:
            with open(path, "wb") as f:
                f.write(data)
            changed.append(name)
    try:
        cfg = json.load(open(CONFIG, encoding="utf-8"))
        add = [w for w in _EXTRA_JUNK if w not in cfg.get("junk_words", [])]
        names = {f["name"] for f in cfg.get("feeds", [])}
        feeds = [f for f in _EXTRA_FEEDS if f["name"] not in names]
        if feeds:
            cfg["feeds"] = cfg.get("feeds", []) + feeds
        if add or feeds:
            cfg["junk_words"] = cfg.get("junk_words", []) + add
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            changed.append("config.json")
    except Exception as e:
        print("WARN config", e)
    if changed and os.path.isdir(os.path.join(ROOT, ".git")):
        subprocess.run(["git", "add", *changed], cwd=ROOT, check=False)
        print("installed:", ", ".join(changed))


if __name__ == "__main__":
    self_install()
    main()
    if os.environ.get("NO_EXTRAS") != "1":
        run_extras()
