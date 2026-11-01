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
                continue  # first run only records what's already out there
            if cfg.get("luxury_only") and not allowed(f"{it['title']} {it['text']}", feed, cfg):
                continue  # luxury, premium tech, sunglasses and flights only; no junk
            ok, why, urgent = evaluate(it, feed, cfg)
            if ok:
                alerts.append((urgent, feed, it, why))
            if ok or feed.get("save") == "all":
                found.append((urgent, feed, it, why))

    alerts.sort(key=lambda a: not a[0])  # urgent first
    if not topic and alerts:  # no ntfy: text the owner instead (sms.py bundles them)
        import sms
        sms.queue([{"title": it["title"], "url": it["link"], "urgent": urgent}
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
        if add:
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
