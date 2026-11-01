#!/usr/bin/env python3
"""Text-message alerts through Twilio (no ntfy app needed).

Secrets (GitHub → Settings → Secrets and variables → Actions):
  TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM   your Twilio account SID, auth token and Twilio number (+1…)
  OWNER_PHONE                             your own cell (+1…) for the big-deal digest
  ALERT_KEY                               private key that unlocks the phone numbers people type on
                                          the Price alerts page (they're stored encrypted, never in plain text)

Run as a script it sends you the deal digest: price errors right away, everything else
bundled into at most one text per hour.
"""
import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(ROOT, "outbox.json")
PAGE = "https://shlomomeisner.github.io/deal-alerts/"
DIGEST_EVERY = 3600


def configured():
    return all(os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))


def send_sms(to, body):
    sid, token, frm = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"], os.environ["TWILIO_FROM"]
    data = urllib.parse.urlencode({"To": to, "From": frm, "Body": body[:600]}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# ---- RSA-OAEP (SHA-256) decryption, standard library only -------------------------------
def _mgf1(seed, length):
    out, c = b"", 0
    while len(out) < length:
        out += hashlib.sha256(seed + c.to_bytes(4, "big")).digest()
        c += 1
    return out[:length]


def decrypt(blob_b64):
    key = json.loads(os.environ["ALERT_KEY"])
    n, d = int(key["n"]), int(key["d"])
    k = (n.bit_length() + 7) // 8
    c = int.from_bytes(base64.b64decode(blob_b64), "big")
    em = pow(c, d, n).to_bytes(k, "big")
    hl = 32
    seed = bytes(a ^ b for a, b in zip(em[1:1 + hl], _mgf1(em[1 + hl:], hl)))
    db = bytes(a ^ b for a, b in zip(em[1 + hl:], _mgf1(seed, k - hl - 1)))
    if em[0] != 0 or db[:hl] != hashlib.sha256(b"").digest():
        raise ValueError("bad ciphertext")
    rest = db[hl:].lstrip(b"\x00")
    if not rest.startswith(b"\x01"):
        raise ValueError("bad padding")
    return rest[1:].decode()


# ---- owner digest -------------------------------------------------------------------------
def queue(items):
    """items: [{"title","url","urgent"}] from watcher.py."""
    box = _load()
    box["items"] = (box["items"] + items)[-50:]
    _save(box)


def _load():
    try:
        return json.load(open(OUTBOX, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_sent": 0, "items": []}


def _save(box):
    with open(OUTBOX, "w", encoding="utf-8") as f:
        json.dump(box, f, indent=1, ensure_ascii=False)


def flush():
    owner = os.environ.get("OWNER_PHONE", "").strip()
    box = _load()
    if not box["items"] or not owner or not configured():
        return
    urgent = [i for i in box["items"] if i.get("urgent")]
    if not urgent and time.time() - box.get("last_sent", 0) < DIGEST_EVERY:
        return
    pick = (urgent or box["items"])[:3]
    head = "🚨 Price error: " if urgent else f"🔥 {len(box['items'])} new steals: "
    lines = [f"• {i['title'][:90]}" + (f"\n{i['url']}" if len(pick) == 1 else "") for i in pick]
    body = head + "\n" + "\n".join(lines) + f"\nAll deals: {PAGE}"
    try:
        send_sms(owner, body)
        if urgent:   # price errors go out now; the rest wait for the hourly digest
            box["items"] = [i for i in box["items"] if i not in pick]
        else:
            box = {"last_sent": time.time(), "items": []}
        _save(box)
        print("sms digest sent")
    except Exception as e:
        print("WARN sms", e)


if __name__ == "__main__":
    flush()
