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
