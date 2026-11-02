set -e
python bundle.py --check
export NTFY_TOPIC=""   # never send real alerts from a test run
echo "::group::stores"; for i in 1 2 3 4; do python stores.py; done; echo "::endgroup::"
python - <<'PY'
import json; s=json.load(open("stores.json"))
for k,v in s["readers"].items(): print("READER", k, "ON" if v.get("on") else "OFF", "items", v.get("items"), "sale", v.get("sale_items"), "fails", v.get("fails"), v.get("off_reason",""))
PY
echo "::group::cleanup"; python cleanup.py; echo "::endgroup::"
python stores.py   # second pass: link checks on what was just added
python - <<'PY'
import json,collections; d=json.load(open("deals.json"))
print("DEALS", len(d), collections.Counter((x.get("store_reader") or "feeds", bool(x.get("exact"))) for x in d).most_common(40))
s=json.load(open("stores.json"))
for k,v in s["readers"].items(): print("CHECKS", k, v.get("checks"))
PY
export TEST_TOPIC="sw-e2e-test-$(date +%s)-$RANDOM"
cd tools && npm init -y >/dev/null && npm i playwright@1.48.2 >/dev/null 2>&1 && npx playwright install --with-deps webkit >/dev/null 2>&1
node e2e.js
cd .. && sleep 3 && TASTE_TOPIC=$TEST_TOPIC python taste.py && python - <<'PY'
import json; t=json.load(open("taste.json")); print("LEARNED votes", {k:(v["v"],v["t"][:50]) for k,v in t["votes"].items()})
m=t["model"]; print("TOP WEIGHTS", sorted(m["weights"].items(), key=lambda x:-abs(x[1]))[:12])
d=json.load(open("deals.json")); print("SCORED", sum(1 for x in d if x.get("taste")), "MUTED", sum(1 for x in d if x.get("muted")))
PY
