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
