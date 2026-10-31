#!/usr/bin/env python3
"""Steal Watch cleanup — does the jobs the Claude "deal hunter" used to do, for free.

Runs right after watcher.py on every scan (GitHub Actions). Standard library only.

1. Store links: swaps Slickdeals / DealNews / 9to5Toys post links for the store's
   own page (the post stays in `source_url`). Flights get a Google Flights search.
2. Sold-out check: re-checks the deals that were checked longest ago and removes
   only the ones that are clearly gone (post marked expired, or the store page
   says out of stock). Deals that can't be checked stay. No age limit.
3. Junk filter + category fixes (smartwatches are tech, not luxury watches).
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = [os.path.join(ROOT, "deals.json"), os.path.join(ROOT, "hunted.json")]
CONFIG = os.path.join(ROOT, "config.json")
BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
LINK_FIXES_PER_SCAN = 12
RECHECKS_PER_SCAN = 15
DEADLINE = time.time() + 150  # keep each scan short

POST_HOSTS = ("slickdeals.net", "dealnews.com", "9to5toys.com", "9to5mac.com", "macrumors.com",
              "dealsplus.com", "bradsdeals.com", "techbargains.com", "reddit.com", "news.google.com",
              "fly4free.com", "theflightdeal.com", "secretflying.com", "camelcamelcamel.com")
REDIRECT_HOSTS = POST_HOSTS + ("go.redirectingat.com", "redirect.viglink.com", "click.linksynergy.com",
                               "linksynergy.walmart.com", "goto.walmart.com", "bestbuy.7tiv.net",
                               "howl.me", "shop-links.co", "sjv.io", "pxf.io", "anrdoezrs.net",
                               "dpbolvw.net", "jdoqocy.com", "kqzyfj.com", "tkqlhce.com", "awin1.com",
                               "prf.hn", "avantlink.com", "shareasale.com", "rstyle.me", "narrativ.com",
                               "bit.ly", "amzn.to", "skimresources.com", "go.skimresources.com")
TRACKING = re.compile(r"^(utm_|tag$|ref$|ref_$|linkCode$|linkId$|ascsubtag$|camp$|creative|irgwc$|"
                      r"clickid$|irclickid$|afsrc$|sharedid$|subid|sid$|u1$|cjevent$|ranMID$|ranEAID$|"
                      r"ranSiteID$|mcid$|epik$|th$|psc$|smid$|sdtid$|aff|affiliate|wmlspartner$|veh$)", re.I)
SMARTWATCH = ("galaxy watch", "apple watch", "pixel watch", "garmin", "fitbit", "amazfit", "smartwatch",
              "smart watch", "whoop", "oura")
# Only the site's own "expired" marker counts; comments saying "sold out in my size" don't.
GONE_POST = re.compile(r"(this deal (has )?expired|meta-expired\W{0,12}yes|\"expired\"\s*:\s*true|"
                       r"class=\"[^\"]*\bexpired\b[^\"]*\"[^>]*>\s*expired)", re.I)
GONE_STORE = re.compile(r"(schema\.org/(OutOfStock|SoldOut|Discontinued)|currently unavailable\.|"
                        r"this item is no longer available|product (is )?no longer available)", re.I)
IN_STOCK = re.compile(r"schema\.org/(InStock|LimitedAvailability|OnlineOnly)", re.I)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def host(url):
    return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")


def is_post(url):
    h = host(url)
    return any(h == d or h.endswith("." + d) for d in POST_HOSTS)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def get(url, timeout=15):
    """Return (status, final_html_or_location)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER, "Accept": "text/html,*/*",
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(1_500_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.code, urllib.parse.urljoin(url, e.headers.get("Location", ""))
        return e.code, ""
    except Exception:
        return 0, ""


def clean(url):
    p = urllib.parse.urlparse(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    path = p.path
    if "amazon." in p.netloc:  # amazon.com/anything/dp/ASIN/... -> amazon.com/dp/ASIN
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", path)
        if m:
            return f"https://www.amazon.com/dp/{m.group(1)}"
    return urllib.parse.urlunparse((p.scheme, p.netloc, path, "", urllib.parse.urlencode(q), ""))


def embedded_target(url):
    """Affiliate links often carry the store URL in a parameter (u, url, murl, u2...)."""
    for k, v in urllib.parse.parse_qsl(urllib.parse.urlparse(url).query):
        if k.lower() in ("u", "url", "murl", "u2", "dest", "destination", "redirect", "r", "lp", "dl") \
                and v.startswith("http"):
            return v
    return None


def follow(url, hops=8):
    """Follow redirects until we land on a real store page."""
    for _ in range(hops):
        if not url:
            return None
        h = host(url)
        if not any(h == d or h.endswith("." + d) for d in REDIRECT_HOSTS):
            return clean(url)
        inner = embedded_target(url)
        if inner:
            url = inner
            continue
        status, body = get(url)
        if status in (301, 302, 303, 307, 308) and body:
            url = body
            continue
        m = re.search(r"""(?:http-equiv=["']refresh["'][^>]*url=|location(?:\.href)?\s*=\s*|window\.location\.replace\()\s*["']?(https?://[^"'\s>)]+)""", body or "", re.I)
        if m:
            url = m.group(1)
            continue
        return None
    return None


def store_link_from_post(post_url):
    status, html = get(post_url)
    if status != 200 or not html:
        return None
    hrefs = re.findall(r"""href=["']([^"']+)["']""", html)
    hrefs = [urllib.parse.urljoin(post_url, unescape_amp(h)) for h in hrefs]
    h = host(post_url)
    cands = []
    if "slickdeals.net" in h:
        cands = [x for x in hrefs if "/click?" in x and "Main%20CTA" in x] + \
                [x for x in hrefs if "/click?" in x and "Image" not in x]
    elif "dealnews.com" in h:
        cands = [x for x in hrefs if "/lw/click" in x or "/lw/" in x and "click" in x]
    elif "9to5toys.com" in h or "9to5mac.com" in h or "macrumors.com" in h:
        cands = [x for x in hrefs if not is_post(x) and host(x) and not host(x).endswith(("twitter.com", "x.com",
                 "facebook.com", "youtube.com", "instagram.com", "google.com", "apple.news", "wp.com"))]
    for c in cands[:4]:
        if time.time() > DEADLINE:
            return None
        target = follow(c)
        if target and not is_post(target):
            return target
    return None


def unescape_amp(s):
    return s.replace("&amp;", "&")


def flight_link(d):
    t = re.sub(r"\$\s?[\d,]+.*$", "", d.get("title", "")).strip(" -–—:|")
    t = re.sub(r"(?i)\b(round[- ]?trip|nonstop|r/?t|from only|for only|only)\b", "", t)
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote("Flights " + t.strip())


def recheck(d):
    """Return 'GONE', 'LIVE' or 'UNKNOWN'."""
    post = d.get("source_url") if is_post(d.get("source_url", "")) else (d["url"] if is_post(d.get("url", "")) else None)
    if post and "slickdeals.net" in host(post) or post and "dealnews.com" in host(post):
        status, html = get(post)
        if status in (404, 410):
            return "GONE"
        if status == 200:
            head = html[:200_000]
            if GONE_POST.search(head):
                return "GONE"
            return "LIVE"
    url = d.get("url", "")
    if url and not is_post(url) and d.get("category") != "flights" and "/search" not in url and "?q=" not in url:
        status, html = get(url)
        if status in (404, 410):
            return "GONE"
        if status == 200 and html:
            if GONE_STORE.search(html) and not IN_STOCK.search(html):
                return "GONE"
            return "LIVE"
    return "UNKNOWN"


def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    junk = [w.lower() for w in cfg.get("junk_words", [])]
    changed = False
    lists = {}
    for path in FILES:
        try:
            lists[path] = json.load(open(path, encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            lists[path] = []

    removed = fixed = live = 0
    for path, deals in lists.items():
        keep = []
        for d in deals:
            t = (d.get("title") or "").lower()
            if any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in junk):
                removed += 1
                changed = True
                continue
            if d.get("category") == "watches" and any(w in t for w in SMARTWATCH):
                d["category"] = "tech"
                changed = True
            keep.append(d)
        lists[path] = keep

    everything = [d for deals in lists.values() for d in deals]

    # 1. store links for new deals (newest first)
    todo = [d for d in everything if is_post(d.get("url", "")) and not d.get("link_tried")]
    todo.sort(key=lambda d: d.get("found_at", ""), reverse=True)
    for d in todo[:LINK_FIXES_PER_SCAN]:
        if time.time() > DEADLINE:
            break
        d["link_tried"] = True
        changed = True
        if d.get("category") == "flights":
            d["source_url"], d["url"] = d["url"], flight_link(d)
            fixed += 1
            continue
        target = store_link_from_post(d["url"])
        if target:
            d["source_url"], d["url"] = d["url"], target
            fixed += 1
            if not d.get("store"):
                d["store"] = host(target).split(".")[-2].title() if "." in host(target) else None

    # 2. sold-out re-checks, oldest check first (never removed for age alone)
    gone = set()
    order = sorted(everything, key=lambda d: d.get("checked_at") or "")
    for d in order[:RECHECKS_PER_SCAN]:
        if time.time() > DEADLINE:
            break
        verdict = recheck(d)
        if verdict == "GONE":
            gone.add(d["id"])
        else:
            d["checked_at"] = now_iso()
            live += verdict == "LIVE"
        changed = True
    if gone:
        for path in lists:
            before = len(lists[path])
            lists[path] = [d for d in lists[path] if d["id"] not in gone]
            removed += before - len(lists[path])

    if changed:
        for path, deals in lists.items():
            if not deals and not os.path.exists(path):
                continue
            deals.sort(key=lambda d: d.get("found_at", ""))
            if path.endswith("hunted.json"):
                deals.reverse()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(deals, f, indent=1, ensure_ascii=False)
        with open(os.path.join(ROOT, "status.json"), "w") as f:
            json.dump({"last_run": now_iso()}, f)
    print(f"cleanup: store links fixed {fixed}, re-checked live {live}, removed {removed}")


if __name__ == "__main__":
    main()
