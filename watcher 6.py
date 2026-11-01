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
