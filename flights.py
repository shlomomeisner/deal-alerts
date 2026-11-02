#!/usr/bin/env python3
"""Flight links that open the exact route and dates on Google Flights, with prices showing.

Google Flights reads a search from its `tfs` URL parameter: a small protobuf message
(route, dates, round trip, 1 adult, economy), base64 encoded. We build it by hand
(standard library only), so a tap opens the results page itself, not a search box.
"""
import base64
import re
import time
import urllib.parse

# City / island names that show up in deal titles -> airport code
AIRPORTS = {
    # home
    "new york": "NYC", "new york city": "NYC", "nyc": "NYC", "jfk": "JFK", "newark": "EWR", "ewr": "EWR", "lga": "LGA",
    "laguardia": "LGA",
    # Caribbean / tropical
    "st maarten": "SXM", "st. maarten": "SXM", "sint maarten": "SXM", "st martin": "SXM", "st. martin": "SXM",
    "saint martin": "SXM", "turks and caicos": "PLS", "turks & caicos": "PLS", "providenciales": "PLS", "aruba": "AUA",
    "curacao": "CUR", "curaçao": "CUR", "bonaire": "BON", "barbados": "BGI", "montego bay": "MBJ", "jamaica": "MBJ",
    "kingston": "KIN", "nassau": "NAS", "bahamas": "NAS", "exuma": "GGT", "punta cana": "PUJ", "santo domingo": "SDQ",
    "santiago dominican republic": "STI", "puerto plata": "POP", "dominican republic": "PUJ", "san juan": "SJU",
    "puerto rico": "SJU", "aguadilla": "BQN", "st thomas": "STT", "st. thomas": "STT", "st croix": "STX",
    "st. croix": "STX", "antigua": "ANU", "st lucia": "UVF", "st. lucia": "UVF", "saint lucia": "UVF",
    "grenada": "GND", "trinidad": "POS", "tobago": "TAB", "grand cayman": "GCM", "cayman": "GCM",
    "cayman islands": "GCM", "anguilla": "AXA", "st kitts": "SKB", "st. kitts": "SKB", "nevis": "NEV",
    "st barts": "SBH", "st. barts": "SBH", "martinique": "FDF", "guadeloupe": "PTP", "dominica": "DOM",
    "bermuda": "BDA", "cancun": "CUN", "cancún": "CUN", "tulum": "TQO", "cozumel": "CZM", "cabo": "SJD",
    "los cabos": "SJD", "cabo san lucas": "SJD", "puerto vallarta": "PVR", "mexico city": "MEX", "costa rica": "SJO",
    "san jose costa rica": "SJO", "san josé": "SJO", "liberia": "LIR", "belize": "BZE", "belize city": "BZE",
    "roatan": "RTB", "panama": "PTY", "panama city": "PTY", "cartagena": "CTG", "bogota": "BOG", "medellin": "MDE",
    "medellín": "MDE", "honolulu": "HNL", "hawaii": "HNL", "oahu": "HNL", "maui": "OGG", "kahului": "OGG",
    "kona": "KOA", "kauai": "LIH", "lihue": "LIH", "hilo": "ITO", "key west": "EYW", "miami": "MIA",
    "fort lauderdale": "FLL", "ft lauderdale": "FLL", "west palm beach": "PBI", "orlando": "MCO", "tampa": "TPA",
    "bali": "DPS", "denpasar": "DPS", "phuket": "HKT", "maldives": "MLE", "male": "MLE", "guatemala city": "GUA",
    "san salvador": "SAL", "lima": "LIM", "rio de janeiro": "GIG", "rio": "GIG", "sao paulo": "GRU",
    "são paulo": "GRU", "buenos aires": "EZE", "santiago": "SCL", "quito": "UIO", "havana": "HAV",
    # Europe
    "london": "LON", "paris": "PAR", "rome": "ROM", "milan": "MIL", "venice": "VCE", "florence": "FLR",
    "naples": "NAP", "barcelona": "BCN", "madrid": "MAD", "lisbon": "LIS", "porto": "OPO", "dublin": "DUB",
    "shannon": "SNN", "edinburgh": "EDI", "glasgow": "GLA", "manchester": "MAN", "amsterdam": "AMS",
    "brussels": "BRU", "frankfurt": "FRA", "munich": "MUC", "berlin": "BER", "zurich": "ZRH", "geneva": "GVA",
    "vienna": "VIE", "prague": "PRG", "budapest": "BUD", "warsaw": "WAW", "krakow": "KRK", "copenhagen": "CPH",
    "stockholm": "STO", "oslo": "OSL", "helsinki": "HEL", "reykjavik": "REK", "iceland": "KEF", "athens": "ATH",
    "santorini": "JTR", "mykonos": "JMK", "istanbul": "IST", "nice": "NCE", "malaga": "AGP", "seville": "SVQ",
    "palma": "PMI", "mallorca": "PMI", "ibiza": "IBZ", "tel aviv": "TLV", "dubai": "DXB", "doha": "DOH",
    "abu dhabi": "AUH", "cairo": "CAI", "marrakech": "RAK", "casablanca": "CMN", "nairobi": "NBO",
    "johannesburg": "JNB", "cape town": "CPT", "accra": "ACC", "lagos": "LOS",
    # Asia / Pacific
    "tokyo": "TYO", "osaka": "OSA", "seoul": "SEL", "beijing": "BJS", "shanghai": "SHA", "hong kong": "HKG",
    "taipei": "TPE", "singapore": "SIN", "bangkok": "BKK", "manila": "MNL", "ho chi minh city": "SGN",
    "hanoi": "HAN", "delhi": "DEL", "new delhi": "DEL", "mumbai": "BOM", "sydney": "SYD", "melbourne": "MEL",
    "auckland": "AKL", "tahiti": "PPT", "papeete": "PPT", "fiji": "NAN",
    # US / Canada
    "los angeles": "LAX", "san francisco": "SFO", "san diego": "SAN", "seattle": "SEA", "portland": "PDX",
    "las vegas": "LAS", "phoenix": "PHX", "denver": "DEN", "salt lake city": "SLC", "chicago": "CHI",
    "boston": "BOS", "washington": "WAS", "washington dc": "WAS", "atlanta": "ATL", "nashville": "BNA",
    "new orleans": "MSY", "austin": "AUS", "dallas": "DFW", "houston": "HOU", "charleston": "CHS",
    "savannah": "SAV", "anchorage": "ANC", "toronto": "YTO", "montreal": "YMQ", "vancouver": "YVR",
}
# Google Flights accepts the NYC and TYO city codes (tested); other city codes (ROM...) fail,
# so those cities use their main airport.
METRO = {"LON": "LHR", "PAR": "CDG", "ROM": "FCO", "MIL": "MXP", "STO": "ARN", "REK": "KEF", "BJS": "PEK",
         "SHA": "PVG", "SEL": "ICN", "OSA": "KIX", "CHI": "ORD", "WAS": "IAD", "HOU": "IAH", "YTO": "YYZ", "YMQ": "YUL"}
AIRPORTS.update({"kigali": "KGL", "rwanda": "KGL", "addis ababa": "ADD", "dakar": "DSS", "zanzibar": "ZNZ",
                 "kilimanjaro": "JRO", "seychelles": "SEZ", "mauritius": "MRU", "kenya": "NBO", "morocco": "RAK"})
HOME = {"new york": "NYC", "new york city": "NYC", "nyc": "NYC", "jfk": "JFK", "ewr": "EWR", "newark": "EWR",
        "lga": "LGA", "laguardia": "LGA"}
LONG_HAUL = re.compile(r"hawaii|honolulu|maui|kona|kauai|tokyo|osaka|japan|seoul|asia|bali|thailand|bangkok|phuket|"
                       r"singapore|hong kong|taipei|manila|europe|london|paris|rome|milan|italy|spain|greece|lisbon|"
                       r"portugal|australia|sydney|new zealand|tahiti|maldives|dubai|africa|israel|tel aviv", re.I)


AIRPORT_NAMES = {}
for _k, _v in AIRPORTS.items():
    AIRPORT_NAMES.setdefault(_v, _k)
FAR = {"TYO", "OSA", "SEL", "BJS", "SHA", "HKG", "TPE", "SIN", "BKK", "MNL", "SGN", "HAN", "DEL", "BOM", "SYD", "MEL",
       "AKL", "PPT", "NAN", "DPS", "HKT", "MLE", "DXB", "DOH", "AUH", "JNB", "CPT", "NBO", "TLV", "HNL", "OGG", "KOA",
       "LIH", "ITO", "GRU", "GIG", "EZE", "SCL"}


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num, wire, payload):
    key = _varint((num << 3) | wire)
    if wire == 0:
        return key + _varint(payload)
    return key + _varint(len(payload)) + payload


def _leg(date, frm, to):
    s = lambda x: x.encode()
    return (_field(2, 2, s(date))
            + _field(13, 2, _field(2, 2, s(frm)))
            + _field(14, 2, _field(2, 2, s(to))))


def tfs(frm, to, depart, ret=None):
    """Google Flights search code: round trip (or one way), 1 adult, economy."""
    msg = _field(3, 2, _leg(depart, frm, to))
    if ret:
        msg += _field(3, 2, _leg(ret, to, frm))
    msg += _field(8, 0, 1) + _field(9, 0, 1) + _field(19, 0, 1 if ret else 2)
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def google_flights(frm, to, depart=None, ret=None):
    """Exact search when we know codes + dates; otherwise the route search with codes."""
    to = METRO.get(to, to)
    depart, ret = fix_trip(to, depart, ret)
    if frm and to and depart and re.fullmatch(r"[A-Z]{3}", frm) and re.fullmatch(r"[A-Z]{3}", to):
        return ("https://www.google.com/travel/flights/search?tfs=" + tfs(frm, to, depart, ret)
                + "&hl=en&gl=us&curr=USD")
    q = f"Flights from {frm or 'NYC'} to {to}"
    if depart:
        q += f" on {depart}" + (f" through {ret}" if ret else "")
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) + "&hl=en&gl=us&curr=USD"


def fix_trip(to, depart, ret):
    """A trip has to make sense: at least 2 nights short-haul / 4 long-haul, at most 30."""
    if not (depart and ret):
        return depart, ret
    d0 = time.mktime(time.strptime(depart, "%Y-%m-%d"))
    n = (time.mktime(time.strptime(ret, "%Y-%m-%d")) - d0) / 86400
    far = bool(LONG_HAUL.search(f"{to} {AIRPORT_NAMES.get(to, '')}")) or to in FAR
    if n < (4 if far else 2) or n > 30:
        ret = time.strftime("%Y-%m-%d", time.localtime(d0 + (9 if far else 5) * 86400 + 7200))
    return depart, ret


def code_for(place):
    """'St. Maarten (SXM)' -> SXM, 'Lisbon, Portugal' -> LIS, unknown -> None."""
    if not place:
        return None
    m = re.search(r"\(([A-Z]{3})\)", place) or re.fullmatch(r"\s*([A-Z]{3})\s*", place)
    if m:
        return METRO.get(m.group(1), m.group(1))
    p = re.sub(r"\([^)]*\)", "", place).lower().replace("st.", "st").strip(" ,.-")
    p = re.sub(r"\s+", " ", p)
    for cand in (p, p.split(",")[0].strip(), p.split("/")[0].strip(), p.split(" or ")[0].strip()):
        if cand in AIRPORTS:
            return METRO.get(AIRPORTS[cand], AIRPORTS[cand])
        if cand.replace("st ", "st. ") in AIRPORTS:
            return METRO.get(AIRPORTS[cand.replace("st ", "st. ")], AIRPORTS[cand.replace("st ", "st. ")])
    for name in sorted(AIRPORTS, key=len, reverse=True):   # 'Aruba nonstop' -> AUA
        if len(name) > 3 and re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", p):
            return METRO.get(AIRPORTS[name], AIRPORTS[name])
    return None


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def window_dates(text, now=None):
    """No sample dates in the deal: pick a mid-week trip inside its travel window
    (2nd Tuesday of the first upcoming month it names, at least 2 weeks out)."""
    now = now or time.time()
    soon = now + 14 * 86400
    t = time.gmtime(now)
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+(20[2-3]\d))?\b",
                         text or "", re.I):
        mo = MONTHS.index(m.group(1).lower()) + 1
        y = int(m.group(2)) if m.group(2) else (t.tm_year if mo >= t.tm_mon else t.tm_year + 1)
        for day in range(1, 29):
            ts = time.mktime((y, mo, day, 12, 0, 0, 0, 0, 0)) - time.timezone
            g = time.gmtime(ts)
            if g.tm_wday == 1 and day >= 8 and ts >= soon:   # Tuesday, 2nd week or later, 2+ weeks out
                nights = 9 if LONG_HAUL.search(text or "") else 5
                return time.strftime("%Y-%m-%d", g), time.strftime("%Y-%m-%d", time.gmtime(ts + nights * 86400))
    return None, None
