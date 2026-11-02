# Deal Alerts   

Checks deal feeds every 5 minutes and pushes the crazy ones to your iPhone:
luxury fashion (Gucci, Ferragamo, …), all clothing and sneakers, colognes,
Amazon price drops, iPhones and tech, and cheap / error-fare flights from NYC.

## Get alerts on your phone
1. Install **ntfy** from the App Store.
2. Tap **+**, subscribe to `shlomo-deals-5b8e55ffde` (set in .github/workflows/watch.yml, or override with an `NTFY_TOPIC` repo secret).
3. Done — 🚨 = error fare / price mistake, 🔥 = big deal. Tap an alert to open it.

## Real links only
- **Store sale pages, read directly** (`stores.py`): Shopify brand stores (Alo Yoga, Touch of
  Modern, Ksubi, Represent, Taylor Stitch, Frame, Noah, Quay, MVMT, …), Nordstrom Rack clearance
  and Woot. Every deal links to the exact product page. Each scan re-checks a few saved links per
  store; a store whose links start coming back dead (or that stops answering) is switched off
  automatically and retried a day later. `stores.json` shows which are on/off and why.
  Off for now: Jomashop (bot protection) and Mr Porter (blocks GitHub's servers).
- The big button on the page opens the exact product (or, for flights, Google Flights with the
  airports and the deal's sample dates filled in, prices showing). The small **Similar** button
  under it opens a store search / Google Shopping. Deals with no real product link only get
  Similar and sit at the bottom of the list.

## It learns your taste
Keep and ✕ on the page are sent to the scanner (`taste.py`), which learns which brands, stores,
categories, prices and words you like, ranks the page by it ("For you") and quietly skips deals
you'd clearly pass on — on the page and in alerts.

## Test it
Actions tab → *Test links on iPhone* → *Run workflow*: reads every store, then opens the page on an
iPhone 15 (Safari's engine) and checks product links, flight links and Keep/✕. Screenshots are
attached to the run.

## Editing the scripts
`watcher.py` carries a bundled copy of the other scripts and the page and rewrites them on each
run. After editing `cleanup.py`, `stores.py`, `taste.py`, `flights.py`, `index.html`, etc., run
`python bundle.py` so the bundle matches (or your edit is undone on the next scan).

## Change what it watches
Edit `config.json`:
- `feeds` — sources (any RSS/Atom feed works; Slickdeals search feeds look like
  `https://slickdeals.net/newsearch.php?q=YOUR+WORDS&searcharea=deals&searchin=first&rss=1`)
- brand lists and minimum % off per category
- `home_airports` and `flight_max_price`
- `store_readers` (optional) — your own list of store readers; defaults are in `stores.py`

Run it by hand from the **Actions** tab → *Deal watcher* → *Run workflow*.
