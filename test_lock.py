"""Can locked photos be reached without the password?

Hiding them from search is the easy half. The half that matters is whether the
bytes are still fetchable by id, which is how a "locked album" usually leaks.
Runs against the live server and your real index.

    set PHOTO_SERVER=http://<tailscale-ip>:8765
    set LOCKED_PASSWORD=<your password>
    python test_lock.py
"""
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import json
import http.cookiejar

from photoindex import CONFIG

BASE = os.environ.get("PHOTO_SERVER", "http://127.0.0.1:8765")
LOCKED = CONFIG.get("locked_prefix") or sys.exit("no locked_prefix in config.json")
PASSWORD = os.environ.get("LOCKED_PASSWORD") or sys.exit("set LOCKED_PASSWORD")

conn = sqlite3.connect("photos.db")
row = conn.execute("SELECT thumb FROM photos WHERE state='done' AND path LIKE ? "
                   "LIMIT 1", (LOCKED + "%",)).fetchone()
if not row:
    sys.exit("no captioned locked photo to test with")
pid = row[0].split("/")[1].replace(".webp", "")
open_row = conn.execute("SELECT thumb FROM photos WHERE state='done' AND path NOT LIKE ? "
                        "LIMIT 1", (LOCKED + "%",)).fetchone()
open_pid = open_row[0].split("/")[1].replace(".webp", "")
conn.close()

fails = []


def get(url, jar=None, method="GET", body=None):
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)) \
        if jar is not None else urllib.request.build_opener()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"}
                                 if data else {})
    try:
        with op.open(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")
    if not ok:
        fails.append(label)


print("=== without password ===")
check("locked thumbnail blocked", get(f"{BASE}/thumb/{pid}")[0], 403)
check("locked full image blocked", get(f"{BASE}/full/{pid}")[0], 403)
check("locked original blocked", get(f"{BASE}/original/{pid}")[0], 403)
check("locked metadata blocked", get(f"{BASE}/api/photo/{pid}")[0], 403)
check("locked scope search blocked",
      get(f"{BASE}/api/search?scope=locked")[0], 403)
check("locked group blocked",
      get(f"{BASE}/api/group?folder=" + urllib.request.quote(LOCKED))[0], 403)
check("wrong password rejected",
      get(f"{BASE}/api/unlock", method="POST", body={"password": "wrong"})[0], 403)
check("empty password rejected",
      get(f"{BASE}/api/unlock", method="POST", body={"password": ""})[0], 403)

# an unlocked photo must keep working, or the guard is too broad
check("normal thumbnail still served", get(f"{BASE}/thumb/{open_pid}")[0], 200)

print("\n=== locked photos absent from search ===")
_, body = get(f"{BASE}/api/search?limit=500")
data = json.loads(body)
locked_tiles = [p for p in data["photos"] if p.get("locked")]
print(f"  locked tile present: {len(locked_tiles) == 1} "
      f"(n={locked_tiles[0]['n'] if locked_tiles else '-'})")
if len(locked_tiles) != 1:
    fails.append("locked tile missing")
# no locked photo id should be returned anywhere in results
ids = {p.get("id") for p in data["photos"]}
check("locked id not leaked in results", pid in ids, False)

print("\n=== with correct password ===")
jar = http.cookiejar.CookieJar()
code, _ = get(f"{BASE}/api/unlock", jar=jar, method="POST",
              body={"password": PASSWORD})
check("correct password accepted", code, 200)
check("locked scope search now allowed",
      get(f"{BASE}/api/search?scope=locked", jar=jar)[0], 200)
check("locked thumbnail now allowed", get(f"{BASE}/thumb/{pid}", jar=jar)[0], 200)
check("locked full now allowed", get(f"{BASE}/full/{pid}", jar=jar)[0], 200)

print("\n=== paging and searching inside the locked scope ===")
_, b1 = get(f"{BASE}/api/search?scope=locked&limit=100&offset=0", jar=jar)
_, b2 = get(f"{BASE}/api/search?scope=locked&limit=100&offset=100", jar=jar)
p1 = {p["id"] for p in json.loads(b1)["photos"]}
p2 = {p["id"] for p in json.loads(b2)["photos"]}
check("page 1 full", len(p1), 100)
check("page 2 full", len(p2), 100)
check("pages do not overlap", len(p1 & p2), 0)
# every id on both pages must actually be a locked photo, not an unlocked one
conn = sqlite3.connect("photos.db")
leaked = [i for i in (p1 | p2) if not conn.execute(
    "SELECT 1 FROM photos WHERE thumb = ? AND path LIKE ?",
    (f"{i[:2]}/{i}.webp", LOCKED + "%")).fetchone()]
conn.close()
check("no unlocked photo leaked into locked pages", len(leaked), 0)
_, b3 = get(f"{BASE}/api/search?scope=locked&q=woman&limit=5", jar=jar)
print(f"  search 'woman' inside locked -> {json.loads(b3)['count']} results")

print("\n=== locked photos STILL excluded from general search once unlocked ===")
_, body = get(f"{BASE}/api/search?limit=500", jar=jar)
ids2 = {p.get("id") for p in json.loads(body)["photos"]}
check("still not in general results", pid in ids2, False)

print("\n=== after re-locking ===")
get(f"{BASE}/api/lock", jar=jar, method="POST")
check("thumbnail blocked again", get(f"{BASE}/thumb/{pid}", jar=jar)[0], 403)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
