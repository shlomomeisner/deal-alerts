#!/usr/bin/env python3
"""Store readers — pull deals straight from store sale pages, with the exact product link.

Each reader returns real product pages (never a deal blog, never a search):
  shopify  brand stores on Shopify (Alo Yoga, Touch of Modern, Ksubi, ...): /products.json,
           kept when a variant's compare-at price is above its price
  rack     Nordstrom Rack clearance listing
  woot     Woot sellout (clearance) grid + each offer page for the list price

Health: every scan re-checks a few saved product links per store. A store whose links
start coming back dead (404 / no longer on sale), or that stops answering, is switched
off automatically (stores.json says why) and retried a day later. Deals whose page is
gone are removed. Runs inside the GitHub Action after watcher.py. Standard library only.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
DEALS = os.path.join(ROOT, "deals.json")
STATUS = os.path.join(ROOT, "stores.json")
BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/130.0 Safari/537.36")
DEADLINE = time.time() + 150
READERS_PER_SCAN = 6           # rotate: every store is read about every 15-25 minutes
CHECKS_PER_SCAN = 12           # saved product links re-checked per scan (spread over stores)
MAX_PER_STORE = 40             # newest/biggest discounts kept per store
FAILS_TO_OFF = 6               # store unreachable this many reads in a row -> off
DEAD_RATIO_OFF = 0.3           # >= 30% of recent link checks dead -> off
RETRY_AFTER = 24 * 3600        # an off store gets another try after a day
STALE_AFTER = 3 * 86400        # listing-only stores (Rack, Woot): drop a deal not seen for 3 days

# Stores tested from a GitHub runner on 2026-11-02. Off ones stay listed with the reason.
DEFAULT_READERS = [
    {"name": "Alo Yoga", "kind": "shopify", "domain": "www.aloyoga.com", "category": "fashion"},
    {"name": "Touch of Modern", "kind": "shopify", "domain": "touchofmodern.com", "min_pct": 25, "sellers": True,
     "collections": ["designer-sunglasses", "bvlgari-tag-heuer-omega-more-pre-owned-luxury-watches",
                     "breitling-pre-owned-luxury-swiss-watches",
                     "bottega-veneta-prada-fendi-more-pre-owned-designer-bags",
                     "cartier-chopard-bvlgari-more-pre-owned-designer-jewelry",
                     "designer-scents-in-smaller-sizes", "apparel-tomo"]},
    {"name": "Ksubi", "kind": "shopify", "domain": "ksubi.com", "category": "fashion"},
    {"name": "Represent", "kind": "shopify", "domain": "representclo.com", "category": "fashion"},
    {"name": "Taylor Stitch", "kind": "shopify", "domain": "www.taylorstitch.com", "category": "fashion"},
    {"name": "Outerknown", "kind": "shopify", "domain": "www.outerknown.com", "category": "fashion"},
    {"name": "Frame", "kind": "shopify", "domain": "frame-store.com", "category": "fashion"},
    {"name": "Noah", "kind": "shopify", "domain": "noahny.com", "category": "fashion"},
    {"name": "John Elliott", "kind": "shopify", "domain": "www.johnelliott.com", "category": "fashion"},
    {"name": "Quay", "kind": "shopify", "domain": "www.quay.com", "category": "fashion"},
    {"name": "MVMT", "kind": "shopify", "domain": "www.mvmt.com", "category": "watches"},
    {"name": "Marine Layer", "kind": "shopify", "domain": "www.marinelayer.com", "category": "fashion"},
    {"name": "Psycho Bunny", "kind": "shopify", "domain": "www.psychobunny.com", "category": "fashion"},
    {"name": "Faherty", "kind": "shopify", "domain": "fahertybrand.com", "category": "fashion"},
    {"name": "Allbirds", "kind": "shopify", "domain": "www.allbirds.com", "category": "fashion"},
    {"name": "Kith", "kind": "shopify", "domain": "kith.com", "category": "fashion"},
    {"name": "Fear of God", "kind": "shopify", "domain": "fearofgod.com", "category": "fashion"},
    {"name": "Aimé Leon Dore", "kind": "shopify", "domain": "www.aimeleondore.com", "category": "fashion"},
    {"name": "Amiri", "kind": "shopify", "domain": "www.amiri.com", "category": "fashion"},
    {"name": "Stüssy", "kind": "shopify", "domain": "www.stussy.com", "category": "fashion"},
    {"name": "Nordstrom Rack", "kind": "rack", "url": "https://www.nordstromrack.com/clearance", "min_pct": 50},
    {"name": "Woot", "kind": "woot", "url": "https://sellout.woot.com/", "min_pct": 40},
    {"name": "Jomashop", "kind": "off", "off_reason": "blocks automated reads (bot protection on its product API)"},
    {"name": "Mr Porter", "kind": "off", "off_reason": "blocks GitHub's servers (Access Denied on every page)"},
]


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def get(url, timeout=20, limit=6_000_000):
    """(status, final_url, text). Status 0 = network error."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER, "Accept": "text/html,application/json,*/*",
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(limit).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception:
        return 0, url, ""


def money(x):
    try:
        return round(float(str(x).replace(",", "").replace("$", "")), 2)
    except (TypeError, ValueError):
        return None


def did(store, key):
    return hashlib.sha1(f"store|{store}|{key}".encode()).hexdigest()[:16]


def search_link(reader, title):
    """Similar: the store's own search for the item (Google Shopping when the store has none)."""
    q = re.sub(r"\s+", " ", re.sub(r"[^\w&'. -]", " ", title)).strip()[:70]
    if reader["kind"] == "shopify":
        return f"https://{reader['domain']}/search?q=" + urllib.parse.quote_plus(q)
    return "https://www.google.com/search?tbm=shop&q=" + urllib.parse.quote_plus(q)


# ---------------------------------------------------------------- readers
def read_shopify(r):
    dom, out, seen = r["domain"], [], set()
    paths = [f"/collections/{c}/products.json" for c in r.get("collections", [])] or ["/products.json"]
    ok = False
    for path in paths:
        for page in (1, 2):
            if time.time() > DEADLINE:
                break
            status, _, body = get(f"https://{dom}{path}?limit=250&page={page}")
            try:
                prods = json.loads(body)["products"]
            except (ValueError, KeyError, TypeError):
                break
            ok = True
            for p in prods:
                best = None
                for v in p.get("variants", []):
                    price, was = money(v.get("price")), money(v.get("compare_at_price"))
                    if price and was and was > price and v.get("available", True):
                        if not best or (1 - price / was) > (1 - best[1] / best[2]):
                            best = (v, price, was)
                if not best or p["handle"] in seen:
                    continue
                seen.add(p["handle"])
                v, price, was = best
                vendor = (p.get("vendor") or "").strip()
                title = p.get("title", "").strip()
                if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", vendor) or vendor.lower().replace(" ", "") in dom.replace("-", ""):
            vendor = ""   # 'frame-denim' style vendor slugs are the store itself
        if vendor and not r.get("sellers") and vendor.lower() not in title.lower() and vendor.lower() not in (r["name"].lower(), dom) \
                        and not re.search(r"\.(com|net)$", vendor, re.I):
                    title = f"{vendor} {title}"
                img = (p.get("images") or [{}])[0].get("src")
                out.append({"key": p["handle"], "title": title, "price": price, "was": was,
                            "url": f"https://{dom}/products/{p['handle']}?variant={v['id']}",
                            "image": img, "tags": " ".join(p.get("tags", [])) if isinstance(p.get("tags"), list) else "",
                            "kind_text": p.get("product_type", "")})
            if len(prods) < 250:
                break
    return out if ok else None


def read_rack(r):
    status, _, body = get(r["url"], timeout=30, limit=8_000_000)
    arts = re.findall(r"<article\b.*?</article>", body, re.S)
    if status != 200 or not arts:
        return None
    out = []
    for a in arts:
        href = re.search(r'href="(/s/[^"?]+/\d+)[^"]*"', a)
        name = re.search(r'<h3[^>]*>.*?<a[^>]*>(.*?)</a>', a, re.S)
        brand = re.search(r'</div><div class="[^"]*">([^<]{2,60})</div><h3', a)
        now = re.search(r"Current Price \$([\d,.]+)", a)
        was = re.search(r"Comparable value \$([\d,.]+)", a)
        img = re.search(r'<img src="([^"]+)"', a)
        if not (href and name and now and was):
            continue
        t = html.unescape(re.sub(r"<[^>]+>", "", name.group(1))).strip()
        b = html.unescape(brand.group(1)).strip() if brand else ""
        out.append({"key": href.group(1).rsplit("/", 1)[1], "title": f"{b} {t}".strip() if b and b.lower() not in t.lower() else t,
                    "price": money(now.group(1)), "was": money(was.group(1)),
                    "url": "https://www.nordstromrack.com" + href.group(1), "image": html.unescape(img.group(1)) if img else None})
    return out


def read_woot(r, have):
    status, _, body = get(r["url"])
    links = re.findall(r'<a[^>]+href="(https://[a-z]+\.woot\.com/offers/[a-z0-9-]+)[^"]*"[^>]*class="discover-link".*?</a>', body, re.S)
    if status != 200 or not links:
        return None
    out, fetched = [], 0
    for m in re.finditer(r'<a[^>]+href="(https://[a-z]+\.woot\.com/offers/([a-z0-9-]+))[^"]*"[^>]*class="discover-link"(.*?)</a>', body, re.S):
        url, key, inner = m.groups()
        title = re.search(r'discover-link-title">\s*(.*?)\s*</div>', inner, re.S)
        img = re.search(r'<img class="discover-img" src="([^"]+)"', inner)
        if not title:
            continue
        item = {"key": key, "title": html.unescape(title.group(1)).strip(), "url": url,
                "image": img.group(1) if img else None}
        old = have.get(did(r["name"], key))
        if old:                                   # already have the list price: no need to open it again
            item.update(price=old.get("price"), was=old.get("was"))
        elif fetched < 10 and time.time() < DEADLINE:
            fetched += 1
            s, _, ob = get(url)
            lp = re.search(r'"ListPrice"\s*:\s*"?\$?([\d.]+)', ob)
            sp = re.search(r'"SalePrice"\s*:\s*"?\$?([\d.]+)', ob)
            if not (lp and sp) or re.search(r'"IsSoldOut"\s*:\s*true', ob):
                continue
            item.update(price=money(sp.group(1)), was=money(lp.group(1)))
        else:
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------- link checks
def check_link(d, reader):
    """'LIVE', 'DEAD' (page gone / sale over / sold out) or 'UNKNOWN'."""
    url = d.get("url", "")
    if reader["kind"] == "shopify":
        handle = re.search(r"/products/([^/?#]+)", url)
        if not handle:
            return "UNKNOWN"
        s, _, body = get(f"https://{reader['domain']}/products/{handle.group(1)}.js")
        if s in (404, 410):
            return "DEAD"
        if s != 200:
            return "UNKNOWN"
        try:
            p = json.loads(body)
        except ValueError:
            return "UNKNOWN"
        on_sale = any((v.get("compare_at_price") or 0) > (v.get("price") or 0) and v.get("available")
                      for v in p.get("variants", []))
        if not on_sale:
            return "DEAD"
        s, final, _ = get(url)   # the page a tap opens must still be the product (not a redirect home)
        if s in (404, 410) or (s == 200 and "/products/" not in final):
            return "DEAD"
        return "LIVE" if s == 200 else "UNKNOWN"
    if reader["kind"] == "woot":
        s, _, body = get(url)
        if s in (404, 410) or re.search(r'"IsSoldOut"\s*:\s*true', body):
            return "DEAD"
        return "LIVE" if s == 200 else "UNKNOWN"
    return "UNKNOWN"   # Rack product pages answer 200 even for bad ids: freshness (seen_at) handles Rack


# ---------------------------------------------------------------- main
def main():
    cfg = load(CONFIG, {})
    sys.path.insert(0, ROOT)
    from watcher import guess_category, evaluate, notify
    readers = cfg.get("store_readers") or DEFAULT_READERS
    status = load(STATUS, {})
    st = status.setdefault("readers", {})
    deals = load(DEALS, [])
    by_id = {d["id"]: d for d in deals}
    t0 = time.time()
    added, alerts, removed = 0, [], 0
    gone_new = set()

    # who reads this scan: active readers in rotation, plus off ones due for their retry
    live = []
    for r in readers:
        s = st.setdefault(r["name"], {"on": r["kind"] != "off", "fails": 0, "checks": []})
        if r["kind"] == "off":
            s.update(on=False, off_reason=r.get("off_reason", "off"))
            continue
        if not s.get("on") and t0 - s.get("off_at", 0) < RETRY_AFTER:
            continue
        live.append(r)
    live.sort(key=lambda r: st[r["name"]].get("read_at", ""))
    todo = live[:READERS_PER_SCAN]

    junk = [w.lower() for w in cfg.get("junk_words", [])]
    for r in todo:
        if time.time() > DEADLINE:
            break
        s = st[r["name"]]
        have = {i: d for i, d in by_id.items() if d.get("store_reader") == r["name"]}
        try:
            items = read_woot(r, have) if r["kind"] == "woot" else {"shopify": read_shopify, "rack": read_rack}[r["kind"]](r)
        except Exception as e:
            print("WARN store", r["name"], e)
            items = None
        s["read_at"] = now_iso()
        if items is None:
            s["fails"] = s.get("fails", 0) + 1
            if s["fails"] >= FAILS_TO_OFF and s.get("on"):
                s.update(on=False, off_at=time.time(), off_reason=f"stopped answering ({s['fails']} reads in a row)")
                print(f"store {r['name']}: switched OFF — {s['off_reason']}")
            elif not s.get("on"):
                s["off_at"] = time.time()          # retry failed: wait another day
            continue
        if not s.get("on"):
            print(f"store {r['name']}: back ON")
        s.update(on=True, fails=0, last_ok=now_iso(), items=len(items))
        s.pop("off_reason", None)
        min_pct = r.get("min_pct", 30)
        good = []
        for it in items:
            if not (it.get("price") and it.get("was") and it["was"] > it["price"]):
                continue
            pct = round((1 - it["price"] / it["was"]) * 100)
            t = it["title"].lower()
            if re.search(r"\b(test(ing)?|sample|gift ?card|e-?gift|donation|shipping protection|route)\b", t) \
                    or it["price"] < 5:
                continue   # hidden test items, gift cards, add-ons
            if pct < min_pct or pct > 95 or any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in junk):
                continue
            if r["kind"] == "rack" and pct < 70 and not any(b.lower() in t for b in cfg.get("luxury_brands", []) + cfg.get("fashion_brands", [])):
                continue   # Rack clearance is huge: brands you follow, or 70%+ off
            good.append((pct, it))
        good.sort(key=lambda x: -x[0])
        checked_new = 0
        s["sale_items"] = len(good)
        for pct, it in good[:MAX_PER_STORE]:
            i = did(r["name"], it["key"])
            d = by_id.get(i)
            if d:
                d.update(price=it["price"], was=it["was"], pct=pct, seen_at=now_iso(), url=it["url"])
                continue
            text = f"{it['title']} {it.get('kind_text', '')} {it.get('tags', '')[:200]}"
            cat = guess_category(text, {"category": r.get("category", "everything")}, cfg)
            d = {"id": i, "title": it["title"], "url": it["url"], "exact": True,
                 "similar": search_link(r, it["title"]), "why": f"{pct}% off at {r['name']}",
                 "category": cat, "source": f"{r['name']} sale", "store": r["name"], "store_reader": r["name"],
                 "urgent": False, "hot": True, "pct": pct, "price": it["price"], "was": it["was"],
                 "found_at": now_iso(), "seen_at": now_iso(), "checked_at": now_iso(),
                 **({"image": it["image"]} if it.get("image") else {})}
            deals.append(d)
            by_id[i] = d
            added += 1
            ok, why, urgent = evaluate({"title": f"{it['title']} {pct}% off", "text": ""}, {"filter": "everything"}, cfg)
            d["urgent"] = bool(urgent)
            if ok and s.get("seeded"):
                alerts.append((urgent, d, why))
            if checked_new < 2 and r["kind"] in ("shopify", "woot") and time.time() < DEADLINE:
                checked_new += 1   # test the store's fresh links right away, not only later
                v = check_link(d, r)
                if v != "UNKNOWN":
                    s["checks"] = (s.get("checks", []) + [1 if v == "DEAD" else 0])[-12:]
                if v == "DEAD":
                    gone_new.add(i)
        s["seeded"] = True   # the first read of a store only fills the page (no alert flood)

    # link checks: oldest-checked store deals first; dead ones removed, and they count against the store
    rd = {r["name"]: r for r in readers}
    gone = set(gone_new)
    order = sorted((d for d in deals if d.get("store_reader") in rd and rd[d["store_reader"]]["kind"] in ("shopify", "woot")),
                   key=lambda d: d.get("checked_at", ""))
    for d in order[:CHECKS_PER_SCAN]:
        if time.time() > DEADLINE:
            break
        v = check_link(d, rd[d["store_reader"]])
        s = st[d["store_reader"]]
        if v == "UNKNOWN":
            continue
        d["checked_at"] = now_iso()
        s["checks"] = (s.get("checks", []) + [1 if v == "DEAD" else 0])[-12:]
        if v == "DEAD":
            gone.add(d["id"])
    for name, s in st.items():
        c = s.get("checks", [])
        if s.get("on") and len(c) >= 6 and sum(c) / len(c) >= DEAD_RATIO_OFF:
            s.update(on=False, off_at=time.time(), off_reason=f"{sum(c)} of its last {len(c)} links were dead", checks=[])
            print(f"store {name}: switched OFF — {s['off_reason']}")
    # listing-only stores: gone from the sale listing for 3 days = gone
    cutoff = now_iso(t0 - STALE_AFTER)
    for d in deals:
        r = rd.get(d.get("store_reader"))
        if r and r["kind"] in ("rack", "woot") and d.get("seen_at", d.get("found_at", "")) < cutoff:
            gone.add(d["id"])
    # an off store's deals are hidden from the page until it's back (its links can't be trusted)
    off = {n for n, s in st.items() if not s.get("on")}
    for d in deals:
        d["store_off"] = d.get("store_reader") in off or None
        if not d["store_off"]:
            d.pop("store_off")
    if gone:
        before = len(deals)
        deals = [d for d in deals if d["id"] not in gone]
        removed = before - len(deals)

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh"
    try:
        import taste
        model = taste.load_model()
    except Exception:
        model = None
    sent = 0
    for urgent, d, why in sorted(alerts, key=lambda a: -a[1]["pct"]):
        if sent >= 5:
            break
        if model and taste.score(model, d)[1]:
            continue   # your taste says no
        title = ("🚨 " if urgent else "🔥 ") + (why or f"{d['pct']}% off")
        body = f"{d['title']} — ${d['price']:g} (was ${d['was']:g})\n{d['store']}"
        if topic:
            try:
                notify(topic, server, title, body, d["url"], urgent, ["shopping_bags", "fire"])
                sent += 1
                time.sleep(1)
            except Exception as e:
                print("WARN notify", e)
        else:
            print("[dry-run]", title, "|", d["title"], "|", d["url"])

    status["updated"] = now_iso()
    status["readers"] = {r["name"]: st[r["name"]] for r in readers if r["name"] in st}
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=1, ensure_ascii=False)
    if added or removed or todo:
        deals.sort(key=lambda d: d.get("found_at", ""))
        with open(DEALS, "w", encoding="utf-8") as f:
            json.dump(deals[-2000:], f, indent=1, ensure_ascii=False)
    on = [n for n, s in st.items() if s.get("on")]
    print(f"stores: read {', '.join(r['name'] for r in todo) or 'none'}; added {added}, removed {removed}, "
          f"alerts {sent}; on {len(on)}, off {len(st) - len(on)}")


if __name__ == "__main__":
    main()
