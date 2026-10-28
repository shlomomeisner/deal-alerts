# Deal Alerts

Checks deal feeds every 5 minutes and pushes the crazy ones to your iPhone:
luxury fashion (Gucci, Ferragamo, …), all clothing and sneakers, colognes,
Amazon price drops, iPhones and tech, and cheap / error-fare flights from NYC.

## Get alerts on your phone
1. Install **ntfy** from the App Store.
2. Tap **+**, subscribe to the topic stored in this repo's `NTFY_TOPIC` secret.
3. Done — 🚨 = error fare / price mistake, 🔥 = big deal. Tap an alert to open it.

## Change what it watches
Edit `config.json`:
- `feeds` — sources (any RSS/Atom feed works; Slickdeals search feeds look like
  `https://slickdeals.net/newsearch.php?q=YOUR+WORDS&searcharea=deals&searchin=first&rss=1`)
- brand lists and minimum % off per category
- `home_airports` and `flight_max_price`

Run it by hand from the **Actions** tab → *Deal watcher* → *Run workflow*.
