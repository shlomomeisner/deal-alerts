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
    for feed in cfg["feeds"]:
        try:
            items = parse_feed(fetch(feed["url"]))
        except Exception as e:  # one bad feed never stops the rest
            errors.append(f"{feed['name']}: {e}")
            continue
        for it in items:
            key = f"{feed['name']}|{it['id']}"
            if key in seen:
                continue
            new_ids.append(key)
            if first_run or feed["name"] not in known_feeds:
                continue  # first run only records what's already out there
            if cfg.get("luxury_only") and not has_any(f"{it['title']} {it['text']}", cfg["luxury_words"]):
                continue  # luxury only for now
            ok, why, urgent = evaluate(it, feed, cfg)
            if ok:
                alerts.append((urgent, feed, it, why))
            if ok or feed.get("save") == "all":
                found.append((urgent, feed, it, why))

    alerts.sort(key=lambda a: not a[0])  # urgent first
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
            json.dump(deals[-600:], f, indent=1, ensure_ascii=False)

    seen_list.extend(new_ids)
    with open(SEEN, "w", encoding="utf-8") as f:
        json.dump(seen_list[-MAX_SEEN:], f, indent=0)

    print(f"new posts: {len(new_ids)}  matched: {len(alerts)}  sent: {sent}"
          + ("  (first run: recorded existing posts, no alerts)" if first_run else ""))
    for e in errors:
        print("WARN", e)


if __name__ == "__main__":
    main()
