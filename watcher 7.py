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
        raw = (it.findtext("description") or "") + " " + (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
        text = strip_tags(it.findtext("description"))
        items.append({"id": guid, "title": title, "link": link, "text": text, "store_url": store_link_in(raw)})
    for e in root.iter(atom + "entry"):
        title = strip_tags(e.findtext(atom + "title"))
        link_el = e.find(atom + "link")
        link = link_el.get("href", "") if link_el is not None else ""
        guid = (e.findtext(atom + "id") or link or title).strip()
        text = strip_tags(e.findtext(atom + "content") or e.findtext(atom + "summary"))
        items.append({"id": guid, "title": title, "link": link, "text": text})
    return items


STORE_HOSTS = ("amazon.com", "walmart.com", "bestbuy.com", "target.com", "costco.com", "woot.com", "ebay.com",
               "macys.com", "nordstrom.com", "nordstromrack.com", "sephora.com", "ulta.com", "jomashop.com",
               "bhphotovideo.com", "newegg.com", "kohls.com", "samsclub.com", "gamestop.com", "apple.com",
               "samsung.com", "zappos.com", "saksfifthavenue.com", "mrporter.com", "farfetch.com", "ssense.com",
               "nike.com", "rei.com", "lululemon.com", "aloyoga.com", "bloomingdales.com", "touchofmodern.com",
               "gilt.com", "bobswatches.com", "chrono24.com", "ashford.com", "fragrancenet.com", "dell.com",
               "lenovo.com", "hp.com", "microsoft.com", "playstation.com", "garmin.com", "dji.com", "amzn.to")


def store_link_in(raw_html):
    """The real store link hidden in a deal post's RSS description (u2= redirect param or a direct link)."""
    raw = unescape(raw_html or "")
    for m in re.finditer(r"[?&]u2=([^&\"'\s<>]+)", raw):
        u = urllib.parse.unquote(m.group(1))
        if u.startswith("http"):
            return u
    for href in re.findall(r"href=[\"']([^\"']+)", raw):
        h = urllib.parse.urlparse(href).netloc.lower().removeprefix("www.")
        if any(h == s or h.endswith("." + s) for s in STORE_HOSTS):
            return href
    return None


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
        island = has_any(re.sub(r"(?i)hawaiian airlines?", "", full), TROPICAL)
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
    m = re.search(r"\$\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)", title)
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
                "found_at": now, **({"store_url": it["store_url"]} if it.get("store_url") else {}),
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
_EXTRA_JUNK = ["movie", "blu-ray", "bluray", "dvd", "4k uhd blu", "digital code", "paperback", "hardcover", "kindle edition", "audiobook", "ebook", "soap", "shampoo", "conditioner", "body wash", "detergent", "toothpaste", "paper towel", "toilet paper", "trash bag", "diapers", "wipes", "vitamin", "supplement", "protein powder", "snack", "coffee pods", "k-cup", "cereal", "grocery", "pet food", "cat litter", "dog food", "lightbulb", "light bulb", "batteries", "phone case", "screen protector", "usb-c cable", "charging cable", "t-shirt 3-pack", "socks", "digital download", "digital game", "video game", "game key", "nintendo switch game", "(nintendo switch)", "ps5 game", "xbox digital", "pc digital", "steam key", "old navy", "devil wears prada", "gap factory", "kids'", "toddler", "stylus", "folio", "keyboard case", "tablet case", "usb hub", "palm rejection", "free sample", "fragrance sample", "sample vial", "samples", "free trial", "giveaway", "sweepstakes"]
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
