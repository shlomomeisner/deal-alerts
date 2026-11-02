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
LINK_FIXES_PER_SCAN = 150
RECHECKS_PER_SCAN = 15
DEADLINE = time.time() + 200  # keep each scan short

POST_HOSTS = ("spaceexplored.com", "electrek.co", "dronedj.com", "appleinsider.com", "9to5google.com", "engadget.com", "theverge.com", "tomsguide.com", "bgr.com",
              "thepointsguy.com", "onemileatatime.com", "viewfromthewing.com", "traveloffpath.com", "slickdeals.net", "dealnews.com", "9to5toys.com", "9to5mac.com", "macrumors.com",
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
# chargers, cables, docks... are junk unless the deal is really for a headphone, TV, speaker etc.
GAME = re.compile(r"\((?:[^)]*\b)?(ps5|ps4|playstation [45]|switch 2|nintendo switch|xbox series x\|?s?|xbox one|pc|steam)"
                  r"\b[^)]*\)|\bdigital (code|download)\b|\bvideo game\b", re.I)
HARDWARE = re.compile(r"\b(console|controller|dualsense|headset|bundle|portal|pro|slim|system|joy-?con|digital edition|"
                      r"disc edition|oled|vr2?)\b", re.I)
PLATFORM = re.compile(r"\b(nintendo switch|switch 2|ps5|ps4|playstation [45]|xbox( series [xs]| one)?)\b|\[digital\]|pre-purchase", re.I)
CHEAP_JEWELRY = re.compile(r"\b(gold|silver|rhodium)[- ]plated\b|\bcate (&|and) chloe\b|\bsimulated\b|"
                           r"\bfashion jewelry\b", re.I)
ACCESSORY = re.compile(r"\b(charger|charging|cable|adapter|power bank|dock|mount|stand)\b", re.I)
REAL_PRODUCT = re.compile(r"\b(oled|qled|tv|headphones?|earbuds|airpods|speaker|soundbar|camera|console|"
                          r"playstation|ps5|xbox|switch 2|iphone 1\d (pro|max|plus)|macbook|ipad (pro|air|mini)\b(?! (case|keyboard)))", re.I)
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


def get(url, timeout=8):
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


LAST_HINT = {"store": ""}
DOUBT = re.compile(r"(woot\.com/search|jomashop\.com/search|ulta\.com/search|staples\.com/|fragrancenet\.com/search|"
                   r"saksfifthavenue\.com/search|typekit|fonts\.|videoplayer|\.(css|js|png|jpe?g|gif|webp)(\?|$))", re.I)


BAD_HOST = re.compile(r"(^|\.)(twitter|x|facebook|youtube|instagram|google|apple\.news|wp|wordpress|gravatar|"
                      r"videoplayerhub|jwplayer|vimeo|tiktok|pinterest|reddit|threads|bsky|disqus|feedburner|"
                      r"gstatic|cloudfront|doubleclick|typekit|fonts|9to5mac|9to5google|9to5toys|electrek|dronedj|spaceexplored|space\.com)"
                      r"(\.|$)|videoplayer|cdn\.|static\.|img\.|\.(jpg|png|gif|webp)$", re.I)


def store_link_from_post(post_url):
    LAST_HINT["store"] = ""
    status, html = get(post_url)
    if status != 200 or not html:
        return None
    m = re.search(r"Get(?:%20|\s|\+)Deal(?:%20|\s|\+)at(?:%20|\s|\+)([A-Za-z0-9&'.%+ -]{2,40}?)(?:&|\"|<)", html)
    if m:
        LAST_HINT["store"] = urllib.parse.unquote_plus(m.group(1))
    hrefs = re.findall(r"""href=["']([^"']+)["']""", html)
    hrefs = [urllib.parse.urljoin(post_url, unescape_amp(h)) for h in hrefs]
    h = host(post_url)
    cands = []
    if "slickdeals.net" in h:
        return None   # Slickdeals blocks following its buttons; the store name (hint) is enough
    elif "dealnews.com" in h:
        cands = [x for x in hrefs if "/lw/click" in x or "/lw/" in x and "click" in x]
    elif "9to5toys.com" in h or "9to5mac.com" in h or "macrumors.com" in h:
        cands = [x for x in hrefs if not is_post(x) and host(x) and not BAD_HOST.search(host(x))]
    for c in cands[:4]:
        if time.time() > DEADLINE:
            return None
        target = follow(c)
        if target and not is_post(target):
            return target
    return None


def unescape_amp(s):
    return s.replace("&amp;", "&")


STORES = {  # name in the deal -> the store's own search page
    "amazon": "https://www.amazon.com/s?k={q}", "woot": "https://www.google.com/search?q=site%3Awoot.com+{q}",
    "best buy": "https://www.bestbuy.com/site/searchpage.jsp?st={q}", "bestbuy": "https://www.bestbuy.com/site/searchpage.jsp?st={q}",
    "walmart": "https://www.walmart.com/search?q={q}", "target": "https://www.target.com/s?searchTerm={q}",
    "costco": "https://www.costco.com/CatalogSearch?keyword={q}", "samsung": "https://www.samsung.com/us/search/searchMain/?searchTerm={q}",
    "apple": "https://www.apple.com/us/search/{q}", "b&h": "https://www.bhphotovideo.com/c/search?q={q}",
    "bhphotovideo": "https://www.bhphotovideo.com/c/search?q={q}", "newegg": "https://www.newegg.com/p/pl?d={q}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}", "macy's": "https://www.macys.com/shop/featured/{q}",
    "macys": "https://www.macys.com/shop/featured/{q}", "nordstrom rack": "https://www.nordstromrack.com/sr?keyword={q}",
    "nordstrom": "https://www.nordstrom.com/sr?keyword={q}", "kohl's": "https://www.kohls.com/search.jsp?search={q}",
    "kohls": "https://www.kohls.com/search.jsp?search={q}", "gamestop": "https://www.gamestop.com/search/?q={q}",
    "sephora": "https://www.sephora.com/search?keyword={q}", "ulta": "https://www.google.com/search?q=site%3Aulta.com+{q}",
    "dell": "https://www.google.com/search?q=site%3Adell.com+{q}", "lenovo": "https://www.google.com/search?q=site%3Alenovo.com+{q}",
    "home depot": "https://www.homedepot.com/s/{q}", "lowe's": "https://www.lowes.com/search?searchTerm={q}",
    "rei": "https://www.rei.com/search?q={q}", "zappos": "https://www.zappos.com/search?term={q}",
    "saks": "https://www.google.com/search?q=site%3Asaksfifthavenue.com+{q}", "bloomingdale's": "https://www.bloomingdales.com/shop/search?keyword={q}",
    "jomashop": "https://www.google.com/search?q=site%3Ajomashop.com+{q}", "fragrancenet": "https://www.google.com/search?q=site%3Afragrancenet.com+{q}",
    "lululemon": "https://shop.lululemon.com/search?Ntt={q}", "nike": "https://www.nike.com/w?q={q}",
    "adidas": "https://www.adidas.com/us/search?q={q}", "microsoft": "https://www.google.com/search?q=site%3Amicrosoft.com+{q}",
    "playstation": "https://www.google.com/search?q=site%3Adirect.playstation.com+{q}", "verizon": "https://www.google.com/search?q=site%3Averizon.com+{q}",
    "sam's club": "https://www.samsclub.com/s/{q}", "bjs": "https://www.google.com/search?q=site%3Abjs.com+{q}",
    "p.c. richard": "https://www.google.com/search?q=site%3Apcrichard.com+{q}", "staples": "https://www.google.com/search?q=site%3Astaples.com+{q}",
}


def store_search(d, store_hint=""):
    """Last resort: the store's own search page for the item (never the deal post)."""
    title = d.get("title", "")
    text = (store_hint + " " + title).lower()
    m = re.search(r"(?:\bat|@|from|via)\s+([a-z0-9&'. ]{2,25}?)(?:\s*[-–(,!]|\s+for\b|\s+\$|$)", title.lower())
    names = sorted(STORES, key=len, reverse=True)
    store = next((n for n in names if store_hint and n in store_hint.lower()), None) \
        or next((n for n in names if m and n in m.group(1)), None) \
        or next((n for n in names if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", text)), None)
    if not store:
        store = None
    q = re.sub(r"\$\s?[\d,.]+.*$", "", title)                       # drop price and everything after
    if store:
        q = re.sub(r"(?i)\b(at|@|from|via)\s+" + re.escape(store) + r".*$", "", q)
    q = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", q)
    q = re.sub(r"(?i)\b(ymmv|targeted|in[- ]store|online|free shipping|w/|deal|sale|new|price drop|all[- ]time low)\b", " ", q)
    q = re.sub(r"[^A-Za-z0-9&'.+ -]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()[:80]
    if len(q) < 3:
        return None, None
    if not store:   # store unknown: Google Shopping lists the stores selling it (never the deal post)
        return "https://www.google.com/search?tbm=shop&q=" + urllib.parse.quote_plus(q), None
    tpl = STORES[store]
    return tpl.format(q=urllib.parse.quote(q) if "{q}/" in tpl or tpl.endswith("{q}") and "/s?" not in tpl and "?" not in tpl
                      else urllib.parse.quote_plus(q)), store



ROUTE = re.compile(r"(?:from\s+)?(new york(?: city)?|nyc|jfk|ewr|lga|newark)(?:\s*\([A-Z]{3}\))?\s*(?:to|-|\u2013|\u2014|\u2192|\u2194|>)\s*"
                   r"([A-Za-z\u00c0-\u017f .,'&()/-]{2,60}?)(?=\s+(?:for|from|in|on|only|starting|\$)|\s*[.$:|!]|\s+[\u2013-]\s|$)", re.I)


MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
SAMPLE = re.compile(MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*(?:[-\u2013\u2014]|to|through)\s*(?:" + MON + r"\.?\s+)?(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d\d)?", re.I)


def page_text(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h\d)>", "\n", html)
    from html import unescape
    return unescape(re.sub(r"<[^>]+>", " ", html))


def flight_dates(post_url):
    """Read the deal post for its travel window and a sample round-trip date."""
    status, html = get(post_url)
    if status != 200 or not html:
        return {}
    text = re.sub(r"[ \t\xa0]+", " ", page_text(html))
    out = {}
    w = re.search(r"(?i)(valid for travel|travel (?:dates?|period|window)|dates?|travel)\s*(?:is|are|:)?\s*((?:in|from|between|during)?[^\n.]{0,10}" + MON + r"[^\n.]{0,90})", text)
    if w:
        out["window"] = re.sub(r"\s+", " ", w.group(2)).strip(" :-")[:110]
    i = re.search(r"(?i)sample (?:travel )?dates?|example dates?|dates? (?:found|available|seen)|e\.g\.", text)
    m = SAMPLE.search(text, i.end()) if i else None
    if not m and "slickdeals.net" in post_url:   # Slickdeals posts list dates in the write-up without a label
        body = text[text.find("Deal Details"):] if "Deal Details" in text else text
        m = SAMPLE.search(body[:6000])
    if m:
        m1, d1, m2, d2, yr = m.groups()
        mo1 = MONTHS[m1[:3].lower()]
        mo2 = MONTHS[m2[:3].lower()] if m2 else mo1
        now = time.gmtime()
        y1 = int(yr) if yr else (now.tm_year if mo1 >= now.tm_mon else now.tm_year + 1)
        y2 = y1 + (1 if mo2 < mo1 else 0)
        if yr and mo2 < mo1:
            y1 -= 1
            y2 = int(yr)
        dep = f"{y1:04d}-{mo1:02d}-{int(d1):02d}"
        if dep >= time.strftime("%Y-%m-%d", time.gmtime()):   # never a date in the past
            out["depart"] = dep
            out["return"] = f"{y2:04d}-{mo2:02d}-{int(d2):02d}"
    return out


def shopping(d):
    q = re.sub(r"\$\s?[\d,.]+.*$", "", d.get("title", ""))
    q = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()[:80]
    return "https://www.google.com/search?tbm=shop&q=" + urllib.parse.quote_plus(q)


def verified(url, d):
    """Open the link like a phone would; a dead store link (404) becomes a Google Shopping search."""
    if "google.com/" in url:
        return url
    status, _ = get(url)
    return shopping(d) if status in (404, 410) else url


def flight_link(d):
    base = _flight_route_link(d)
    if d.get("depart") and d.get("return") and "%20through%20" not in base:
        base += urllib.parse.quote(f" on {d['depart']} through {d['return']}")
    return base


def _flight_route_link(d):
    m = ROUTE.search(re.sub(r"\b(St|Ste|Mt|Ft|Pt)\.", r"\1", d.get("title", "")))
    if m:
        dest = re.sub(r"\([^)]*\)", "", m.group(2)).strip(" ,.-")
        orig = {"jfk": "JFK", "ewr": "EWR", "lga": "LGA", "newark": "EWR"}.get(m.group(1).lower(), "New York")
        return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(f"Flights from {orig} to {dest}")
    return _flight_link_plain(d)


def _flight_link_plain(d):
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
    if url and "google.com/" not in url and re.search(r"search|/s\?|/sr\?|[?&](q|k|keyword|st|term|query|_nkw)=|/featured/", url, re.I):
        status, _ = get(url)
        if status in (404, 410):
            return "DEADLINK"
    if url and not is_post(url) and d.get("category") != "flights" and not re.search(r"search|/s\?|/sr\?|[?&](q|k|keyword|st|term|query|_nkw)=|/featured/", url, re.I):
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
            if (any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in junk)
                    or (ACCESSORY.search(t) and not REAL_PRODUCT.search(t))
                    or ((GAME.search(t) or PLATFORM.search(t)) and not HARDWARE.search(t))
                    or CHEAP_JEWELRY.search(t)):
                removed += 1
                changed = True
                continue
            if d.get("category") == "flights" and re.search(r"\b(hotel|resort|/double|/night|hostel)\b", t):
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
    for d in everything:   # camelcamelcamel price pages -> the Amazon product itself
        m = re.search(r"camelcamelcamel\.com/(?:[^/]+/)?product/([A-Z0-9]{10})", d.get("url", ""), re.I)
        if m:
            d["source_url"], d["url"], changed = d["url"], "https://www.amazon.com/dp/" + m.group(1).upper(), True
    for d in everything:   # links saved by an older version that point at a video player, font, bad search page...
        if d.get("source_url") and (BAD_HOST.search(host(d.get("url", ""))) or DOUBT.search(d.get("url", ""))):
            d["url"], changed = d["source_url"], True
            d.pop("link_tried2", None)
            d.pop("link_tried3", None)
    for d in everything:   # travel dates from the flight post (once per deal)
        if d.get("category") == "flights" and not d.get("dates_tried3") and time.time() < DEADLINE:
            post = d.get("source_url") or (d["url"] if is_post(d.get("url", "")) else None)
            if post and "news.google.com" not in post:
                for k in ("depart", "return", "window"):
                    d.pop(k, None)
                d.update(flight_dates(post))
                d["route_fixed"] = False
            d["dates_tried3"], changed = True, True
    for d in everything:
        if d.get("category") == "flights" and d.get("source_url") and not d.get("route_fixed"):
            d["url"], d["route_fixed"], changed = flight_link(d), True, True
    todo = [d for d in everything if is_post(d.get("url", "")) and not d.get("link_tried3")]
    todo.sort(key=lambda d: d.get("found_at", ""), reverse=True)
    for d in todo[:LINK_FIXES_PER_SCAN]:
        if time.time() > DEADLINE:
            break
        d["link_tried3"] = True
        changed = True
        if d.get("category") == "flights":
            d["source_url"], d["url"], d["route_fixed"] = d["url"], flight_link(d), True
            fixed += 1
            continue
        target = None
        if d.get("store_url"):   # real store link found in the feed itself
            target = follow(d["store_url"]) or (d["store_url"] if not is_post(d["store_url"]) else None)
        target = target or store_link_from_post(d["url"])
        if not target:
            target, _ = store_search(d, LAST_HINT["store"])
        if target:
            target = verified(target, d)
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
        if verdict == "DEADLINK":
            d["url"] = shopping(d)
            d["checked_at"] = now_iso()
            changed = True
            continue
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
