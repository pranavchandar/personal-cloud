"""Serve the photo index over HTTP: thumbnail grid, tap for full resolution.

Designed to sit behind Tailscale, which supplies the authentication (only devices
on your tailnet can reach it). Hence no login of its own -- but also why the
default bind is 127.0.0.1: exposing this on 0.0.0.0 puts your whole library on
every network you join.

    python server.py                      # local only, for testing
    python server.py --host 100.x.y.z     # your Tailscale IP, reachable anywhere

Photos are addressed by the sha1 already stored in the thumb column, never by a
client-supplied path -- a path parameter would be a directory-traversal hole
straight to the filesystem.
"""

import argparse
import hashlib
import hmac
import io
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from PIL import Image

from photoindex import CONFIG, DB_PATH, VIDEO_EXTS, load_image, thumbs_db


FAV = "favourite"


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS

# --- locked folder ------------------------------------------------------------
# Both come from config.json, never from source: this repo is public, and an
# unsalted sha256 of a short password is cracked in seconds once published.
# With no locked folder configured, the prefix is a string no Windows path can
# contain ('|' is illegal there) -- an empty one would make LIKE '%' match, and
# NOT LIKE '%' hide the entire library.
LOCKED_PREFIX = CONFIG.get("locked_prefix") or "|no locked folder|"
PASSWORD_SHA256 = CONFIG.get("locked_password_sha256", "")   # "" never matches
SESSION_TTL = 6 * 3600          # unlocks expire; a server restart clears them all
_sessions: dict[str, float] = {}


def is_locked(path: str) -> bool:
    return path.lower().startswith(LOCKED_PREFIX.lower())


def unlocked(request: Request) -> bool:
    tok = request.cookies.get("unlock")
    if not tok:
        return False
    exp = _sessions.get(tok)
    if not exp or exp < time.time():
        _sessions.pop(tok, None)
        return False
    return True


def guard(path: str, request: Request) -> None:
    """Gate the *content*, not just the listings. Filtering search results alone
    would leave every locked photo fetchable by anyone who guessed or kept an id."""
    if is_locked(path) and not unlocked(request):
        raise HTTPException(403, "locked")

FULL_MAX_PX = 2560          # plenty for a phone or laptop; keeps conversion quick

# Whitelist, never string-interpolated user input: an ORDER BY built from a query
# parameter is SQL injection with extra steps.
SORTS = {
    "newest":  "p.taken_at DESC",                                 # default
    "oldest":  "p.taken_at ASC",
    "largest": "p.bytes DESC, p.taken_at DESC",
    "widest":  "p.width DESC, p.taken_at DESC",
    # NULL places last rather than first, which is where SQLite would put them
    "place":   "p.place IS NULL, p.place ASC, p.taken_at DESC",
    "random":  None,                                              # needs a seed
}
DEFAULT_SORT = "newest"

# Which formats collapse into folder groups. Raw only: these are camera burst and
# timelapse outputs, where a folder is a single sequence. JPEGs in a folder are
# usually unrelated photos and must stay as individual tiles.
RAW_PRED = ("LOWER(p.path) LIKE '%.dng' OR LOWER(p.path) LIKE '%.nef' "
            "OR LOWER(p.path) LIKE '%.cr2' OR LOWER(p.path) LIKE '%.arw'")

app = FastAPI(title="photo index")


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def path_for(pid: str) -> str:
    """sha1 -> original path, via the database. Never trust a client path."""
    if len(pid) != 40 or not all(c in "0123456789abcdef" for c in pid):
        raise HTTPException(400, "bad id")
    conn = db()
    row = conn.execute("SELECT path FROM photos WHERE thumb = ?",
                       (f"{pid[:2]}/{pid}.webp",)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "unknown id")
    return row["path"]


@app.post("/api/unlock")
def api_unlock(response: Response, payload: dict = Body(...)):
    given = str(payload.get("password", ""))
    # compare_digest, not ==: string equality short-circuits and leaks length and
    # prefix information through timing
    if not hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(),
                               PASSWORD_SHA256):
        raise HTTPException(403, "wrong password")
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + SESSION_TTL
    response.set_cookie("unlock", tok, httponly=True, samesite="lax",
                        max_age=SESSION_TTL)
    return {"ok": True, "expires_in": SESSION_TTL}


@app.post("/api/lock")
def api_lock(request: Request, response: Response):
    _sessions.pop(request.cookies.get("unlock", ""), None)
    response.delete_cookie("unlock")
    return {"ok": True}


@app.get("/api/locked_count")
def api_locked_count():
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM photos WHERE state='done' AND path LIKE ?",
                     (LOCKED_PREFIX + "%",)).fetchone()[0]
    conn.close()
    return {"total": n}


@app.get("/api/search")
def api_search(
    request: Request,
    q: str = "",
    tag: list[str] = Query(default=[]),
    match_all: bool = False,
    since: str = "",
    until: str = "",
    place: list[str] = Query(default=[]),
    sort: str = DEFAULT_SORT,
    seed: int = 0,
    scope: str = "all",
    limit: int = Query(default=100, le=500),
    offset: int = 0,
):
    if sort not in SORTS:
        raise HTTPException(400, f"sort must be one of {', '.join(SORTS)}")
    matched, params = matched_cte(request, q, tag, match_all, since, until, place, scope)

    # Raw frames collapse to one tile per source folder. Timelapse sequences live
    # one-folder-per-sequence (TIMELAPSE\001_0071\ alone holds 588 near-identical
    # frames), and filenames repeat across folders, so the folder is the sequence
    # -- grouping by filename would merge unrelated shoots.
    gkey = (f"CASE WHEN ({RAW_PRED}) THEN rtrim(p.path, replace(p.path, '\\', '')) "
            f"ELSE p.path END")
    return _search_rows(request, matched, params, gkey, q, tag, since, until, place,
                        sort, seed, scope, limit, offset)


def matched_cte(request, q, tag, match_all, since, until, place, scope):
    """`WITH matched AS (...)` selecting the paths a set of filters allows, plus its
    params. Shared by the grid and the week/month/year view, so both always agree
    on what is visible -- including the locked-folder rule."""
    if scope not in ("all", "locked"):
        raise HTTPException(400, "scope must be 'all' or 'locked'")

    # scope='all' excludes locked photos even once unlocked, so one unlock does not
    # leak them into every later search. scope='locked' searches only inside them,
    # which is how filters, sorting and paging work in there at all -- reusing this
    # one query instead of a second hand-written listing endpoint.
    if scope == "locked":
        if not unlocked(request):
            raise HTTPException(403, "locked")
        where = ["p.state = 'done'", "p.path LIKE ?"]
    else:
        where = ["p.state = 'done'", "p.path NOT LIKE ?"]
    params: list = [LOCKED_PREFIX + "%"]
    joins = ""
    if tag:
        joins = "JOIN tags t ON t.path = p.path"
        where.append(f"t.tag IN ({','.join('?' * len(tag))})")
        params += [x.strip().lower() for x in tag]
    elif q:
        # substring match so "nature" also finds "natural setting"; exact-match
        # alone is brittle when the model coins near-synonyms
        joins = "JOIN tags t ON t.path = p.path"
        where.append("t.tag LIKE ?")
        params.append(f"%{q.strip().lower()}%")
    if since:
        where.append("p.taken_at >= ?"); params.append(since)
    if until:
        where.append("p.taken_at <= ?"); params.append(until + " 23:59:59")
    if place:
        # Any of the chosen places. Exact match, not LIKE: the picker supplies
        # full "City, CC" values, and a substring match would make a shorter
        # place name also select every longer one containing it.
        named = [x for x in place if x != "__none__"]
        alts = []
        if named:
            alts.append(f"p.place IN ({','.join('?' * len(named))})")
            params += named
        if "__none__" in place:
            alts.append("p.place IS NULL")
        where.append("(" + " OR ".join(alts) + ")")

    inner_group = ""
    if tag and match_all:
        inner_group = "GROUP BY p.path HAVING COUNT(DISTINCT t.tag) = ?"
        params.append(len(tag))
    elif joins:
        inner_group = "GROUP BY p.path"
    return (f"WITH matched AS ("
            f"  SELECT p.path FROM photos p {joins} "
            f"  WHERE {' AND '.join(where)} {inner_group}"
            f") "), params


def _search_rows(request, matched, params, gkey, q, tag, since, until, place,
                 sort, seed, scope, limit, offset):
    # Aggregates, because one output row can now stand for many photos. Ordering on
    # a bare column under GROUP BY would silently pick an arbitrary row's value.
    agg_orders = {
        "newest":  "MAX(p.taken_at) DESC",
        "oldest":  "MIN(p.taken_at) ASC",
        "largest": "MAX(p.bytes) DESC, MAX(p.taken_at) DESC",
        "widest":  "MAX(p.width) DESC, MAX(p.taken_at) DESC",
        "place":   "MAX(p.place) IS NULL, MAX(p.place) ASC, MAX(p.taken_at) DESC",
    }
    if sort == "random":
        # A seeded shuffle, not ORDER BY RANDOM(): RANDOM() reorders on every
        # query, so page 2 of a random result would duplicate and skip photos.
        # The client sends one seed per search, giving a stable order across pages.
        order = "((MIN(p.rowid) * ?) % 1000003)"
        seed_param = [(abs(int(seed)) % 999983) or 7919]
    else:
        order, seed_param = agg_orders[sort], []

    # Two stages, not one. Filtering must resolve per photo first -- collapsing
    # folders in the same GROUP BY would turn "photo has ALL these tags" into
    # "this folder collectively has them", which is a different question.
    # MIN(p.path) then makes SQLite pick the earliest frame's row for the bare
    # columns, so a group's tile shows frame 1 rather than an arbitrary frame.
    sql = (f"{matched}"
           f"SELECT MIN(p.path) AS rep_path, p.thumb, p.taken_at, p.place, "
           f"       p.width, p.height, p.bytes, p.duration, COUNT(*) AS n, {gkey} AS gkey, "
           f"       MIN(p.taken_at) AS first_at, MAX(p.taken_at) AS last_at "
           f"FROM photos p JOIN matched m ON m.path = p.path "
           f"GROUP BY gkey ORDER BY {order} LIMIT ? OFFSET ?")

    conn = db()
    rows = conn.execute(sql, params + seed_param + [limit, offset]).fetchall()
    # ponytail: whole favourite set per search; fine while it is hand-picked (hundreds)
    favs = {r[0] for r in conn.execute("SELECT path FROM tags WHERE tag = ?", (FAV,))}
    conn.close()

    out = []
    for r in rows:
        item = {"id": Path(r["thumb"]).stem, "taken_at": r["taken_at"],
                "place": r["place"], "w": r["width"], "h": r["height"],
                "n": r["n"], "video": is_video(r["rep_path"]), "bytes": r["bytes"],
                "duration": r["duration"], "fav": r["rep_path"] in favs}
        if r["n"] > 1:
            folder = r["gkey"].rstrip("\\")
            item["group"] = folder
            item["label"] = Path(folder).name or folder
            item["first_at"], item["last_at"] = r["first_at"], r["last_at"]
        out.append(item)

    # One locked tile, first, on the first page only. No thumbnail and no preview
    # of what is inside -- that is the point of it. Never inside the locked scope
    # itself, which would nest a lock inside the thing it locks.
    if scope == "all" and offset == 0 and not (tag or q or since or until or place):
        conn = db()
        n = conn.execute("SELECT COUNT(*) FROM photos WHERE state='done' AND "
                         "path LIKE ?", (LOCKED_PREFIX + "%",)).fetchone()[0]
        conn.close()
        if n:
            out.insert(0, {"locked": True, "n": n, "label": "Locked",
                           "unlocked": unlocked(request)})
    return {"count": len(out), "offset": offset, "sort": sort, "photos": out}


# Monday-start weeks: 'weekday 0' moves to the coming Sunday (or stays on one),
# minus 6 days is that week's Monday.
PERIOD_KEYS = {
    "week":  "date(p.taken_at, 'weekday 0', '-6 days')",
    "month": "substr(p.taken_at, 1, 7)",
    "year":  "substr(p.taken_at, 1, 4)",
}


@app.get("/api/periods")
def api_periods(
    request: Request,
    level: str,
    q: str = "",
    tag: list[str] = Query(default=[]),
    match_all: bool = False,
    since: str = "",
    until: str = "",
    place: list[str] = Query(default=[]),
    sort: str = DEFAULT_SORT,
    scope: str = "all",
):
    """One collection per week/month/year for the zoomed-out grid: its size, date
    span and the newest item as cover. Same filters as /api/search. Unpaged --
    even weeks top out around a thousand rows."""
    if level not in PERIOD_KEYS:
        raise HTTPException(400, f"level must be one of {', '.join(PERIOD_KEYS)}")
    matched, params = matched_cte(request, q, tag, match_all, since, until, place, scope)
    # SQLite fills bare columns (p.thumb) from the row holding MAX(), so the cover
    # is the period's newest item
    sql = (f"{matched}"
           f"SELECT {PERIOD_KEYS[level]} AS k, COUNT(*) AS n, MAX(p.taken_at) AS last_at, "
           f"       p.thumb, MIN(p.taken_at) AS first_at "
           f"FROM photos p JOIN matched m ON m.path = p.path "
           f"WHERE p.taken_at IS NOT NULL "
           f"GROUP BY k ORDER BY k {'ASC' if sort == 'oldest' else 'DESC'}")
    conn = db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {"level": level, "periods": [
        {"key": r["k"], "n": r["n"], "id": Path(r["thumb"]).stem,
         "first_at": r["first_at"], "last_at": r["last_at"]} for r in rows]}


@app.get("/api/group")
def api_group(folder: str, request: Request,
              limit: int = Query(default=1000, le=5000), offset: int = 0):
    if is_locked(folder) and not unlocked(request):
        raise HTTPException(403, "locked")
    """Every raw frame inside one sequence folder.

    `folder` only ever reaches a parameterised LIKE against the database -- it
    never touches the filesystem, so it cannot be used to read arbitrary paths.
    """
    conn = db()
    rows = conn.execute(
        f"SELECT thumb, taken_at, place, width, height, path FROM photos "
        f"WHERE state='done' AND ({RAW_PRED.replace('p.path', 'path')}) "
        f"AND path LIKE ? AND path NOT LIKE ? "
        f"ORDER BY path LIMIT ? OFFSET ?",
        (folder.rstrip("\\") + "\\%", folder.rstrip("\\") + "\\%\\%",
         limit, offset)).fetchall()
    conn.close()
    return {"folder": folder, "count": len(rows), "photos": [
        {"id": Path(r["thumb"]).stem, "taken_at": r["taken_at"],
         "place": r["place"], "w": r["width"], "h": r["height"],
         "filename": Path(r["path"]).name} for r in rows]}


@app.get("/api/map")
def api_map(request: Request, scope: str = "all"):
    """One pin per place: centroid of its photos, plus a count.

    Only places whose photos carry real coordinates appear -- a place name alone
    cannot be plotted. AVG is fine here because a reverse-geocoded place is by
    definition a small area.
    """
    if scope not in ("all", "locked"):
        raise HTTPException(400, "scope must be 'all' or 'locked'")
    if scope == "locked" and not unlocked(request):
        raise HTTPException(403, "locked")
    op = "LIKE" if scope == "locked" else "NOT LIKE"
    conn = db()
    rows = conn.execute(
        f"SELECT place, COUNT(*) c, AVG(lat) lat, AVG(lon) lon, "
        f"       MIN(taken_at) first_at, MAX(taken_at) last_at "
        f"FROM photos WHERE state='done' AND place IS NOT NULL "
        f"AND lat IS NOT NULL AND path {op} ? "
        f"GROUP BY place ORDER BY c DESC", (LOCKED_PREFIX + "%",)).fetchall()
    conn.close()
    return {"pins": [{"place": r["place"], "count": r["c"],
                      "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
                      "first_at": r["first_at"], "last_at": r["last_at"]}
                     for r in rows]}


@app.get("/static/{name}")
def static_file(name: str):
    """Leaflet and flatpickr (4.6.13, MIT), served locally so the page has no CDN
    dependency at runtime."""
    if name not in ("leaflet.js", "leaflet.css", "flatpickr.min.js",
                    "flatpickr.min.css", "flatpickr-dark.css"):
        raise HTTPException(404, "not found")
    f = Path(__file__).parent / "static" / name
    if not f.is_file():
        raise HTTPException(404, "not found")
    media = "text/javascript" if name.endswith(".js") else "text/css"
    return Response(f.read_bytes(), media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/places")
def api_places(request: Request, scope: str = "all"):
    """Places that actually have viewable photos, alphabetically.

    Restricted to state='done' on purpose: the library holds 120 places but only
    the captioned ones can be displayed, so listing the rest would offer choices
    that return an empty grid. The list grows as captioning proceeds.
    """
    if scope not in ("all", "locked"):
        raise HTTPException(400, "scope must be 'all' or 'locked'")
    if scope == "locked" and not unlocked(request):
        raise HTTPException(403, "locked")
    op = "LIKE" if scope == "locked" else "NOT LIKE"
    conn = db()
    rows = conn.execute(
        f"SELECT place, COUNT(*) c FROM photos WHERE state='done' "
        f"AND place IS NOT NULL AND path {op} ? "
        f"GROUP BY place ORDER BY place COLLATE NOCASE",
        (LOCKED_PREFIX + "%",)).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM photos WHERE state='done' AND place IS NULL "
        f"AND path {op} ?", (LOCKED_PREFIX + "%",)).fetchone()[0]
    conn.close()
    return {"places": [{"place": r["place"], "count": r["c"]} for r in rows],
            "no_place": total}


_tags_cache: tuple[float, list] = (0.0, [])
TAGS_TTL = 60
# Bumped on every favourite toggle. A chip query that started before the toggle
# (it takes seconds on a cold disk) must not store its stale counts afterwards.
_tags_gen = 0


@app.get("/api/tags")
def api_tags(limit: int = 300):
    """Manual tags first, then model tags by frequency.

    Curated tags outrank generated ones -- that is what makes 'camera reel' sit at
    the head of the chip list instead of being buried behind 'clouds'. Cached for
    the same reason as stats: GROUP BY over 456k rows is not a per-keystroke query.
    """
    global _tags_cache
    ts, cached = _tags_cache
    if cached and time.time() - ts < TAGS_TTL:
        return cached[:limit]
    gen = _tags_gen
    conn = db()
    rows = conn.execute(
        "SELECT tag, COUNT(*) c, MAX(source = 'manual') pinned FROM tags "
        "GROUP BY tag ORDER BY pinned DESC, c DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    out = [{"tag": r["tag"], "count": r["c"], "pinned": bool(r["pinned"])}
           for r in rows]
    if gen == _tags_gen:
        _tags_cache = (time.time(), out)
    return out


@app.get("/api/similar/{pid}")
def api_similar(pid: str, request: Request, limit: int = 60):
    path = path_for(pid)
    guard(path, request)
    conn = db()
    # locked photos must not surface as "similar" to an unlocked one either
    rows = conn.execute(
        """SELECT p.thumb, p.taken_at, p.place, COUNT(*) shared
           FROM tags t1 JOIN tags t2 ON t2.tag = t1.tag AND t2.path <> t1.path
           JOIN photos p ON p.path = t2.path
           WHERE t1.path = ? AND p.path NOT LIKE ? GROUP BY t2.path
           ORDER BY shared DESC, p.taken_at DESC LIMIT ?""",
        (path, LOCKED_PREFIX + "%", limit)).fetchall()
    conn.close()
    return {"photos": [{"id": Path(r["thumb"]).stem, "taken_at": r["taken_at"],
                        "place": r["place"], "shared": r["shared"]} for r in rows]}


@app.get("/api/photo/{pid}")
def api_photo(pid: str, request: Request):
    path = path_for(pid)
    guard(path, request)
    conn = db()
    row = conn.execute("SELECT taken_at, place, width, height, description, bytes, lat, lon "
                       "FROM photos WHERE path = ?", (path,)).fetchone()
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE path = ? ORDER BY tag", (path,))]
    conn.close()
    return {"id": pid, "filename": Path(path).name, "taken_at": row["taken_at"],
            "place": row["place"], "w": row["width"], "h": row["height"],
            "bytes": row["bytes"], "description": row["description"], "tags": tags,
            "video": is_video(path), "fav": FAV in tags,
            "lat": row["lat"], "lon": row["lon"]}


@app.post("/api/fav/{pid}")
def api_fav(pid: str, request: Request, payload: dict = Body(...)):
    """Favourite = a manual tag, so the existing chip, tag filter and search all
    work on it for free, and re-captioning leaves it alone."""
    global _tags_cache, _tags_gen
    path = path_for(pid)
    guard(path, request)
    conn = db()
    conn.execute("PRAGMA busy_timeout = 30000")  # wait out a scan's commit window
    if payload.get("on"):
        conn.execute("INSERT OR IGNORE INTO tags (path, tag, source) "
                     "VALUES (?, ?, 'manual')", (path, FAV))
    else:
        conn.execute("DELETE FROM tags WHERE path = ? AND tag = ?", (path, FAV))
    conn.commit()
    conn.close()
    _tags_gen += 1
    _tags_cache = (0.0, [])   # so the chip appears/updates immediately
    return {"fav": bool(payload.get("on"))}


@app.get("/thumb/{pid}")
def thumb(pid: str, request: Request):
    if len(pid) != 40 or not all(c in "0123456789abcdef" for c in pid):
        raise HTTPException(400, "bad id")
    # one indexed lookup per thumbnail, so a locked photo cannot be pulled by id
    guard(path_for(pid), request)
    return thumb_response(pid)


@app.get("/full/{pid}")
def full(pid: str, request: Request):
    """Always JPEG. Browsers cannot display DNG/NEF/HEIC, and 4,349 of these
    photos are raw -- serving the original bytes would render nothing."""
    path = path_for(pid)
    guard(path, request)
    return full_response(path)


@app.get("/video/{pid}")
def video(pid: str, request: Request):
    path = path_for(pid)
    guard(path, request)
    return video_response(path)


@app.get("/original/{pid}")
def original(pid: str, request: Request):
    path = path_for(pid)
    guard(path, request)
    return original_response(path)


# --- shared folders -------------------------------------------------------------
# Named, virtual collections (rows in share_items, nothing moved on disk) that
# download as one zip to send by Drive/WhatsApp/etc. Private like everything
# else here: there is deliberately no public link.

SHARE_ITEMS_SQL = ("SELECT p.path, p.thumb, p.taken_at, p.place, p.width, p.height, "
                   "p.duration, p.bytes FROM share_items si "
                   "JOIN photos p ON p.path = si.path "
                   "WHERE si.share_id = ? AND p.thumb IS NOT NULL "
                   "ORDER BY p.taken_at DESC")


def _folder_or_404(conn, sid: str):
    row = conn.execute("SELECT id, name FROM shares WHERE id = ?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "no such folder")
    return row


@app.get("/api/shares")
def api_shares():
    conn = db()
    rows = conn.execute(
        "SELECT s.id, s.name, COUNT(p.path) n, COALESCE(SUM(p.bytes), 0) bytes "
        "FROM shares s LEFT JOIN share_items si ON si.share_id = s.id "
        "LEFT JOIN photos p ON p.path = si.path "
        "GROUP BY s.id ORDER BY s.created_at DESC").fetchall()
    conn.close()
    return {"folders": [dict(r) for r in rows]}


@app.post("/api/shares")
def api_share_create(payload: dict = Body(...)):
    name = " ".join(str(payload.get("name", "")).split())[:80]
    if not name:
        raise HTTPException(400, "name required")
    sid = secrets.token_urlsafe(16)
    conn = db()
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("INSERT INTO shares (id, name, created_at) VALUES (?, ?, ?)",
                 (sid, name, time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return {"id": sid, "name": name}


@app.get("/api/shares/{sid}")
def api_share_items(sid: str):
    conn = db()
    folder = _folder_or_404(conn, sid)
    rows = conn.execute(SHARE_ITEMS_SQL, (sid,)).fetchall()
    conn.close()
    return {"id": sid, "name": folder["name"], "photos": [
        {"id": Path(r["thumb"]).stem, "taken_at": r["taken_at"], "place": r["place"],
         "w": r["width"], "h": r["height"], "n": 1, "video": is_video(r["path"]),
         "duration": r["duration"], "bytes": r["bytes"]} for r in rows]}


@app.post("/api/shares/{sid}/items")
def api_share_edit(sid: str, request: Request, payload: dict = Body(...)):
    """Add items by id, or take them out with remove=true. Locked items can only
    be added while unlocked -- the same rule as viewing them."""
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids required")
    paths = []
    for pid in ids:
        p = path_for(str(pid))
        guard(p, request)
        paths.append(p)
    conn = db()
    conn.execute("PRAGMA busy_timeout = 30000")
    _folder_or_404(conn, sid)
    if payload.get("remove"):
        conn.executemany("DELETE FROM share_items WHERE share_id = ? AND path = ?",
                         [(sid, p) for p in paths])
    else:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany("INSERT OR IGNORE INTO share_items (share_id, path, added_at) "
                         "VALUES (?, ?, ?)", [(sid, p, stamp) for p in paths])
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM share_items WHERE share_id = ?",
                     (sid,)).fetchone()[0]
    conn.close()
    return {"n": n}


@app.delete("/api/shares/{sid}")
def api_share_delete(sid: str):
    """Deletes the folder only; the photos stay in the library."""
    conn = db()
    conn.execute("PRAGMA busy_timeout = 30000")
    _folder_or_404(conn, sid)
    conn.execute("DELETE FROM share_items WHERE share_id = ?", (sid,))
    conn.execute("DELETE FROM shares WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return {"ok": True}


class _Pipe:
    """Write-only sink that zipfile streams into; the generator drains it."""
    def __init__(self):
        self.chunks, self.pos = [], 0

    def write(self, b):
        self.chunks.append(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def close(self):
        pass

    def drain(self):
        out, self.chunks = b"".join(self.chunks), []
        return out


def zip_stream(paths: list[str]):
    """Zip the originals on the fly: STORED, since photos and videos are already
    compressed, and streamed so a 4 GB video never needs a temp copy or 4 GB of
    RAM. Same-named files from different folders get " (2)" etc."""
    import zipfile
    pipe = _Pipe()
    used: dict[str, int] = {}
    with zipfile.ZipFile(pipe, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for p in paths:
            src = Path(p)
            if not src.is_file():
                continue                       # drive unplugged or file deleted
            name = src.name
            k = used.get(name.lower(), 0) + 1
            used[name.lower()] = k
            if k > 1:
                name = f"{src.stem} ({k}){src.suffix}"
            info = zipfile.ZipInfo(name, time.localtime(src.stat().st_mtime)[:6])
            with src.open("rb") as f, zf.open(info, "w", force_zip64=True) as w:
                while chunk := f.read(1 << 20):
                    w.write(chunk)
                    yield pipe.drain()
            yield pipe.drain()
    yield pipe.drain()                         # central directory


@app.get("/api/shares/{sid}/zip")
def api_share_zip(sid: str, request: Request):
    conn = db()
    folder = _folder_or_404(conn, sid)
    paths = [r["path"] for r in conn.execute(SHARE_ITEMS_SQL, (sid,))]
    conn.close()
    if not paths:
        raise HTTPException(404, "folder is empty")
    for p in paths:
        guard(p, request)                      # locked items need an unlock first
    safe = "".join(c if c.isalnum() or c in " -_()" else "_" for c in folder["name"])
    return StreamingResponse(zip_stream(paths), media_type="application/zip",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{safe.strip() or "photos"}.zip"'})


# --- file responses -------------------------------------------------------------
# Callers authorise first (guard); these only serve.

def thumb_response(pid: str):
    tconn = thumbs_db()
    row = tconn.execute("SELECT data FROM thumbs WHERE id = ?", (pid,)).fetchone()
    tconn.close()
    if not row:
        raise HTTPException(404, "no thumbnail")
    # content-addressed, so it can be cached forever
    return Response(row[0], media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


def full_response(path: str):
    """Always JPEG. Browsers cannot display DNG/NEF/HEIC, and 4,349 of these
    photos are raw -- serving the original bytes would render nothing."""
    try:
        img = load_image(Path(path))
    except Exception as e:
        raise HTTPException(500, f"cannot decode: {type(e).__name__}")
    img.thumbnail((FULL_MAX_PX, FULL_MAX_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/jpeg",
                             headers={"Cache-Control": "public, max-age=86400"})


def video_response(path: str):
    """The original file, streamed. FileResponse honours Range requests, which the
    <video> element needs to seek and which iOS Safari requires to play at all.
    No transcoding: the browser plays what it can (H.264 everywhere, HEVC on
    Safari/Chrome with hardware support); anything else has Download original."""
    if not is_video(path) or not Path(path).is_file():
        raise HTTPException(404, "no such video")
    # .mov is served as mp4: same container family, and Chrome refuses
    # video/quicktime even when it can decode the H.264 inside
    mt = "video/webm" if path.lower().endswith(".webm") else "video/mp4"
    return FileResponse(path, media_type=mt)


def original_response(path: str):
    """The untouched file, for download. Raw files land as .DNG etc. Streamed from
    disk -- reading it into memory first cost 4 GB of RAM per 4 GB video."""
    if not Path(path).is_file():
        raise HTTPException(404, "file missing - is the drive connected?")
    return FileResponse(path, media_type="application/octet-stream",
                        filename=Path(path).name)


_stats_cache: tuple[float, dict] = (0.0, {})
_tagcount_cache: tuple[float, int] = (0.0, -1)
STATS_TTL = 15
TAGCOUNT_TTL = 300


@app.get("/api/stats")
def api_stats():
    """Deliberately cheap: both counts are index-backed and measured ~0.07s.

    COUNT(DISTINCT tag) used to live here and measured 9-15 seconds over 456k tag
    rows while captioning was writing. Since the UI calls this after every search,
    that one query made the entire server look hung. It now lives in
    /api/tagcount, which the UI fetches once per page load instead.
    """
    global _stats_cache
    ts, cached = _stats_cache
    if cached and time.time() - ts < STATS_TTL:
        return cached
    conn = db()
    g = lambda s: conn.execute(s).fetchone()[0]
    out = {"done": g("SELECT COUNT(*) FROM photos WHERE state='done'"),
           "pending": g("SELECT COUNT(*) FROM photos WHERE state='pending'")}
    conn.close()
    _stats_cache = (time.time(), out)
    return out


@app.get("/api/tagcount")
def api_tagcount():
    """The expensive one, kept off the search path and cached for 5 minutes."""
    global _tagcount_cache
    ts, cached = _tagcount_cache
    if cached >= 0 and time.time() - ts < TAGCOUNT_TTL:
        return {"tags": cached}
    conn = db()
    n = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
    conn.close()
    _tagcount_cache = (time.time(), n)
    return {"tags": n}


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photos</title>
<link rel="stylesheet" href="/static/leaflet.css">
<link rel="stylesheet" href="/static/flatpickr-dark.css">
<style>
  :root{--bg:#111;--fg:#eee;--dim:#888;--line:#2a2a2a;--accent:#6ea8fe}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
  header{position:sticky;top:0;z-index:5;background:#161616;
         border-bottom:1px solid var(--line);padding:10px 12px}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  input,button,select{background:#222;color:var(--fg);border:1px solid var(--line);
               border-radius:8px;padding:9px 11px;font-size:15px}
  select{cursor:pointer}
  input[type=search]{flex:1;min-width:180px}
  input[type=search]::-webkit-search-cancel-button{display:none}  /* ours below */
  /* every filter gets its own × that appears only when it holds a value */
  .field{position:relative;display:flex}
  .field.grow{flex:1;min-width:180px}
  .field input,.field #placebtn{padding-right:30px;width:100%}
  .field #dates{width:215px;cursor:pointer}
  #placebtn{max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
            text-align:left}
  #placebtn.on,#dates.on{border-color:var(--accent);color:var(--accent)}
  .clr{position:absolute;right:3px;top:50%;transform:translateY(-50%);border:0;
       background:transparent;color:var(--dim);font-size:20px;line-height:1;
       padding:2px 7px;visibility:hidden}
  .clr:hover{color:var(--fg)}
  .field.has .clr{visibility:visible}
  /* date range picker (flatpickr, dark theme) */
  .flatpickr-calendar{font-family:inherit}
  .fp-presets{display:flex;flex-wrap:wrap;gap:4px;padding:6px;
              border-top:1px solid #3f4458}
  .fp-presets button{font-size:12px;padding:4px 8px;border-radius:999px}
  /* place picker */
  #placedlg{width:min(420px,94vw);height:min(620px,90vh);background:#191919;
            border-radius:12px;padding:0;color:var(--fg)}
  #placewrap{display:flex;flex-direction:column;height:100%}
  #placewrap .top{padding:12px 12px 8px;border-bottom:1px solid var(--line)}
  #placewrap .top input{width:100%}
  #placelist{flex:1;overflow-y:auto;padding:4px 12px}
  #placelist label{display:flex;gap:8px;align-items:center;padding:7px 2px;
                   border-bottom:1px solid #1f1f1f;cursor:pointer}
  #placelist label span{flex:1}
  #placelist label em{color:var(--dim);font-style:normal;font-size:12px}
  #placelist input{width:18px;height:18px;accent-color:var(--accent)}
  #placewrap .bot{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line)}
  .apply{background:var(--accent);color:#000;border-color:var(--accent);font-weight:600}
  .apply:disabled{opacity:.4;cursor:default}
  #placewrap .bot .apply{margin-left:auto}
  #placewrap .bot button{white-space:nowrap}
  button{cursor:pointer}
  button:hover{border-color:var(--accent)}
  #status{color:var(--dim);font-size:13px;padding:6px 12px}
  #chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 12px 8px;max-height:84px;
         overflow-y:auto}
  .chip{background:#1e1e1e;border:1px solid var(--line);border-radius:999px;
        padding:4px 10px;font-size:13px;cursor:pointer;color:var(--dim)}
  .chip.on{background:var(--accent);color:#000;border-color:var(--accent)}
  /* curated tags read as different from the ~26k the model invented */
  .chip.pinned{color:#ffd479;border-color:#5a4a20;background:#241f12;font-weight:600}
  .chip.pinned.on{background:#ffd479;color:#000;border-color:#ffd479}
  #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));
        gap:4px;padding:4px}
  #grid img{width:100%;aspect-ratio:1;object-fit:cover;display:block;
            background:#1a1a1a;border-radius:4px;cursor:pointer}
  /* A group must read as "more inside" before you tap it: offset sheets behind
     the tile, a count badge, and the sequence name. */
  .cell{position:relative}
  .cell.stack{padding:5px 5px 0 0}
  .cell.stack::before,.cell.stack::after{content:"";position:absolute;
      border-radius:4px;background:#2b2b2b;border:1px solid #3a3a3a;
      left:5px;right:0;top:0;bottom:5px;z-index:0}
  .cell.stack::after{left:2.5px;top:2.5px;bottom:2.5px;right:2.5px;background:#242424}
  .cell.stack img{position:relative;z-index:1;border:1px solid #3a3a3a}
  .badge{position:absolute;bottom:9px;right:4px;z-index:2;
         background:#000b;color:#fff;border-radius:6px;padding:2px 7px;
         font-size:12px;font-weight:600;display:flex;gap:4px;align-items:center;
         pointer-events:none}
  .glabel{position:absolute;bottom:9px;left:4px;z-index:2;background:#000b;
          color:#ddd;border-radius:6px;padding:2px 7px;font-size:11px;
          max-width:62%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
          pointer-events:none}
  #crumb{padding:8px 12px;background:#161616;border-bottom:1px solid var(--line);
         display:none;align-items:center;gap:10px}
  #crumb.on{display:flex}
  /* deliberately no thumbnail: a preview would defeat the lock */
  .cell.locked{display:flex;align-items:center;justify-content:center;
      aspect-ratio:1;background:#1c1c22;border:1px solid #3b3b4a;border-radius:4px;
      cursor:pointer;flex-direction:column;gap:4px;color:#9aa}
  .cell.locked:hover{border-color:var(--accent)}
  .cell.locked .lk{font-size:30px;line-height:1}
  .cell.locked .lt{font-size:11px;letter-spacing:.04em;text-transform:uppercase}
  #pwdlg{width:min(340px,92vw);height:auto;background:#191919;border-radius:12px;
         padding:18px;color:var(--fg)}
  #pwdlg h3{margin:0 0 4px;font-size:16px}
  #pwdlg p{margin:0 0 12px;color:var(--dim);font-size:13px}
  #pwdlg input{width:100%;margin-bottom:10px}
  #pwerr{color:#ff6b6b;font-size:13px;min-height:18px;margin:0 0 6px}
  #mapdlg{width:min(980px,100vw);height:min(760px,100vh);background:#141414;
          border-radius:10px;padding:0;overflow:hidden}
  #mapwrap{display:flex;flex-direction:column;height:100%}
  #map{flex:1;min-height:0;background:#222}
  #mapbar{display:flex;gap:8px;align-items:center;padding:8px 12px;
          background:#161616;border-bottom:1px solid var(--line);font-size:13px;
          color:var(--dim);flex-wrap:wrap}
  /* dark-ish tiles without a second tile provider: just dim the raster */
  .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.9)}
  .leaflet-container{background:#222}
  .pin-label{background:transparent;border:0;box-shadow:none;color:#fff;
             font:600 11px system-ui;text-shadow:0 0 3px #000,0 0 3px #000}
  #more{margin:14px auto 80px;display:block;padding:11px 22px}
  /* month separators in the full grid */
  .mhdr{grid-column:1/-1;padding:16px 4px 4px;font-weight:600;font-size:15px;
        color:var(--fg)}
  .mhdr:first-child{padding-top:4px}
  /* week/month/year collections */
  .psticker{position:absolute;top:4px;left:4px;z-index:2;background:#000c;
            color:#fff;border-radius:6px;padding:3px 8px;font-size:12px;
            font-weight:700;pointer-events:none;max-width:85%;line-height:1.25}
  .cell.period.y .psticker{font-size:16px}
  #zoombar{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:5;
           display:flex;align-items:center;gap:2px;background:#1b1b1bee;
           border:1px solid var(--line);border-radius:999px;padding:4px;
           box-shadow:0 4px 16px #0008}
  #zoombar button{border-radius:999px;width:38px;height:38px;padding:0;
                  font-size:20px;line-height:1}
  #zoombar button:disabled{opacity:.35}
  #zlabel{min-width:78px;text-align:center;font-size:13px;color:var(--fg)}
  /* select mode */
  #selbar{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:5;
          display:flex;align-items:center;gap:6px;background:#1b1b1bf2;
          border:1px solid var(--accent);border-radius:999px;padding:5px 6px 5px 14px;
          box-shadow:0 4px 16px #0008;white-space:nowrap;max-width:96vw}
  #selbar[hidden]{display:none}
  #selcount{font-size:13px;margin-right:4px}
  #selbar button{border-radius:999px}
  #selbtn.on{background:var(--accent);color:#000;border-color:var(--accent)}
  .cell.sel img{outline:3px solid var(--accent);outline-offset:-3px;opacity:.7}
  .cell.sel::before{content:"\2713";position:absolute;top:5px;left:5px;z-index:3;
      width:22px;height:22px;border-radius:50%;background:var(--accent);color:#000;
      display:flex;align-items:center;justify-content:center;font-weight:800;
      font-size:14px;pointer-events:none}
  /* sequences and the lock can't be put in a folder; say so visually */
  body.selecting .cell.stack, body.selecting .cell.locked{opacity:.35;pointer-events:none}
  /* long-press must select, not pop the browser's save-image menu or highlight text */
  body.selecting #grid{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
  body.selecting #grid img{-webkit-touch-callout:none;pointer-events:none}
  body.selecting .mhdr{cursor:pointer}
  body.selecting .mhdr::after{content:"  · tap to select all";font-weight:400;
                              font-size:12px;color:var(--accent)}
  .fdlg{width:min(440px,94vw);height:auto;background:#191919;border-radius:12px;
        padding:16px;color:var(--fg)}
  .fdlg h3{margin:0 0 10px;font-size:16px}
  .fdlg .hint{margin:-4px 0 12px;color:var(--dim);font-size:13px}
  .frow{display:flex;gap:6px;align-items:center;margin-bottom:8px}
  .frow input{flex:1;min-width:0}
  .frow .fname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .frow .fmeta{color:var(--dim);font-size:12px;white-space:nowrap}
  #fdmsg{min-height:18px;font-size:13px;margin:4px 0 10px}
  #fdmsg a,.frow a{color:var(--accent)}
  dialog{border:0;padding:0;background:#000;max-width:100vw;max-height:100vh;
         width:100%;height:100%}
  dialog::backdrop{background:#000d}
  .viewer{display:flex;flex-direction:column;height:100%}
  .stage{flex:1;min-height:0;position:relative;display:flex}
  .viewer img,.viewer video{flex:1;min-height:0;object-fit:contain;width:100%}
  /* tap zones: left/right 35% page through the grid; the middle and the bottom
     60px stay free so a video's own tap-to-play and control bar still work */
  .navz{position:absolute;top:0;bottom:60px;width:35%;z-index:1;cursor:pointer}
  .navz.prev{left:0} .navz.next{right:0}
  .favmark{position:absolute;top:3px;right:5px;color:#ff4d6d;font-size:17px;
           text-shadow:0 0 3px #000,0 0 2px #000;pointer-events:none;z-index:2}
  #favBtn.on{color:#ff4d6d;border-color:#ff4d6d}
  .big{position:absolute;top:4px;left:4px;background:#c0392bdd;color:#fff;
       border-radius:5px;padding:1px 6px;font-size:11px;font-weight:700;
       pointer-events:none}
  .meta{padding:10px 12px;background:#141414;border-top:1px solid var(--line);
        font-size:13px;color:var(--dim);max-height:38vh;overflow-y:auto}
  .meta b{color:var(--fg);font-weight:600}
  .meta .tag{display:inline-block;background:#1e1e1e;border-radius:999px;
             padding:2px 9px;margin:2px 3px 0 0;cursor:pointer;color:#bbb}
  .meta .link{color:var(--accent);cursor:pointer;border-bottom:1px dotted var(--accent);
              text-decoration:none}
  .meta .link:hover{color:#fff;border-bottom-color:#fff}
  #datedlg{width:min(320px,92vw);height:auto;background:#191919;border-radius:12px;
           padding:16px;color:var(--fg)}
  #datedlg h3{margin:0 0 10px;font-size:15px}
  #datedlg button{display:block;width:100%;text-align:left;margin-bottom:6px}
  #datedlg .rng{color:var(--dim);font-size:12px;margin-left:6px}
  .bar{display:flex;gap:8px;padding:8px 12px;background:#141414}
</style></head><body>

<header>
  <div class="row">
    <div class="field grow">
      <input type="search" id="q" placeholder="search tags e.g. beach, selfie, waterfall">
      <button class="clr" id="clrQ" title="clear search">&times;</button>
    </div>
    <div class="field">
      <input id="dates" placeholder="Any date" title="pick a day or a range">
      <button class="clr" id="clrDates" title="clear dates">&times;</button>
    </div>
    <!-- the picker writes these; everything else reads them -->
    <input type="hidden" id="since"><input type="hidden" id="until">
    <div class="field">
      <button id="placebtn" title="choose one or more places">&#128205; All places</button>
      <button class="clr" id="clrPlaces" title="clear places">&times;</button>
    </div>
    <button id="mapbtn" title="pick a place on a map">&#127757; Map</button>
    <button id="selbtn" title="select items to put in a folder">&#9745; Select</button>
    <button id="foldersbtn" title="your folders, downloadable as zip">&#128193; Folders</button>
    <select id="sort" title="sort order">
      <option value="newest" selected>Newest first</option>
      <option value="oldest">Oldest first</option>
      <option value="largest">Largest file</option>
      <option value="widest">Highest resolution</option>
      <option value="place">Place A–Z</option>
      <option value="random">Shuffle</option>
    </select>
    <button id="go">Search</button><button id="clear">Clear</button>
  </div>
</header>
<div id="chips"></div>
<div id="crumb"><button id="back">← All photos</button><span id="crumbText"></span></div>
<div id="status">loading…</div>
<div id="grid"></div>
<button id="more" hidden>Load more</button>
<div id="zoombar">
  <button id="zout" title="zoom out: group by week, month, year">&minus;</button>
  <span id="zlabel">All items</span>
  <button id="zin" title="zoom in">+</button>
</div>
<div id="selbar" hidden>
  <span id="selcount">0 selected</span>
  <button id="selAdd">&#128193; Add to folder</button>
  <button id="selRemove" hidden>Remove from folder</button>
  <button id="selDone">Done</button>
</div>

<dialog id="folderdlg" class="fdlg">
  <h3 id="fdhdr">Add to folder</h3>
  <div id="fdlist"></div>
  <div class="frow"><input id="fdname" placeholder="New folder name" maxlength="80">
    <button id="fdcreate">Create &amp; add</button></div>
  <p id="fdmsg"></p>
  <button id="fdclose" style="width:100%">Close</button>
</dialog>

<dialog id="folderslist" class="fdlg">
  <h3>Folders</h3>
  <p class="hint">Download a folder as a zip, then send it by WhatsApp, Drive, email…</p>
  <div id="fllist"></div>
  <button id="flclose" style="width:100%">Close</button>
</dialog>

<dialog id="datedlg">
  <h3 id="datehdr">Filter by period</h3>
  <div id="dateopts"></div>
  <button id="datecancel" style="text-align:center">Cancel</button>
</dialog>

<dialog id="mapdlg"><div id="mapwrap">
  <div id="mapbar">
    <button id="mapclose">← Back</button>
    <span id="mapinfo"></span>
    <span style="margin-left:auto" id="maphint">tap pins to choose places</span>
    <button id="mapnone" hidden>Clear</button>
    <button id="mapgo" class="apply" disabled>Show photos</button>
  </div>
  <div id="map"></div>
</div></dialog>

<dialog id="placedlg"><div id="placewrap">
  <div class="top"><input type="search" id="placeq" placeholder="Filter places…"></div>
  <div id="placelist"></div>
  <div class="bot">
    <button id="placecancel">Cancel</button><button id="placenone">Clear all</button>
    <button id="placego" class="apply">Show photos</button>
  </div>
</div></dialog>

<dialog id="pwdlg">
  <h3>Locked</h3>
  <p id="pwcount"></p>
  <input type="password" id="pw" placeholder="Password" autocomplete="current-password">
  <p id="pwerr"></p>
  <div class="row"><button id="pwok">Unlock</button><button id="pwcancel">Cancel</button></div>
</dialog>

<dialog id="dlg"><div class="viewer">
  <div class="bar">
    <button id="close">← Back</button><button id="favBtn">&#9825; Favourite</button>
    <button id="simBtn">Similar</button>
    <a id="dl" download><button>Download original</button></a>
  </div>
  <div class="stage">
    <img id="big" alt="">
    <video id="vid" controls playsinline hidden></video>
    <div class="navz prev" id="navPrev" title="previous"></div>
    <div class="navz next" id="navNext" title="next"></div>
  </div>
  <div class="meta" id="meta"></div>
</div></dialog>

<script src="/static/leaflet.js"></script>
<script src="/static/flatpickr.min.js"></script>
<script>
const $ = s => document.querySelector(s);
const PAGE = 100;
let offset = 0, active = new Set(), seed = 1;
// Which view "Load more" should continue. Without this it always resumed a normal
// search, so paging inside the locked folder dumped unlocked photos into the grid.
let view = 'search', groupFolder = null, groupLabel = '', groupTotal = 0;
let tagCount = 0;   // fetched once, not per search: the query behind it is slow

async function jget(u){ const r = await fetch(u); if(!r.ok) throw new Error(r.status);
                        return r.json(); }

function params(off){
  const p = new URLSearchParams();
  active.forEach(t => p.append('tag', t));
  if(!active.size && $('#q').value.trim()) p.set('q', $('#q').value.trim());
  if(active.size > 1) p.set('match_all','true');
  for(const k of ['since','until']) if($('#'+k).value) p.set(k,$('#'+k).value);
  places.forEach(x => p.append('place', x));
  p.set('sort', $('#sort').value);
  // one seed per search keeps Shuffle stable while paging; a fresh one per
  // search means re-shuffling gives a genuinely different order
  if($('#sort').value === 'random') p.set('seed', seed);
  if(view === 'locked') p.set('scope', 'locked');
  p.set('offset', off); p.set('limit', PAGE);
  return p;
}

function fmtDur(s){
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), ss = String(s%60).padStart(2,'0');
  return h ? `${h}:${String(m).padStart(2,'0')}:${ss}` : `${m}:${ss}`;
}

function cell(ph){
  if(ph.locked){
    const d = document.createElement('div');
    d.className = 'cell locked';
    d.innerHTML = '<span class="lk">&#128274;</span><span class="lt">Locked</span>'
                + '<span class="lt">' + ph.n + ' items</span>';
    d.title = ph.n + ' locked items — password required';
    d.onclick = () => ph.unlocked ? openLocked() : askPassword(ph.n);
    return d;
  }
  const d = document.createElement('div');
  d.className = 'cell' + (ph.group ? ' stack' : '');
  const im = document.createElement('img');
  im.src = '/thumb/' + ph.id; im.loading = 'lazy';
  d.appendChild(im);
  if(ph.group){
    const b = document.createElement('span');
    b.className = 'badge';
    b.innerHTML = '&#x29C9; ' + ph.n;          // stacked-squares glyph + count
    const l = document.createElement('span');
    l.className = 'glabel'; l.textContent = ph.label;
    d.append(b, l);
    const span = ph.first_at && ph.last_at && ph.first_at.slice(0,10) !== ph.last_at.slice(0,10)
        ? ph.first_at.slice(0,10) + ' – ' + ph.last_at.slice(0,10) : (ph.taken_at||'');
    d.title = `${ph.label} · ${ph.n} frames · ${span}\nTap to open the sequence`;
    d.onclick = () => openGroup(ph.group, ph.label, ph.n);
  } else {
    d.title = (ph.taken_at||'') + (ph.place ? ' · ' + ph.place : '');
    d._open = () => open_(ph.id, ph.video, d);   // also used by prev/next
    d._pid = ph.id;
    d.onclick = e => {
      if(!selecting) return d._open();
      if(d._longPressed){ d._longPressed = false; return; }   // range already done
      e.shiftKey ? rangeSel(d) : toggleSel(ph.id, d);
    };
    // touch has no Shift: press-and-hold does the range instead
    d.addEventListener('pointerdown', e => {
      if(!selecting || e.pointerType === 'mouse') return;
      clearTimeout(d._lp);
      d._lp = setTimeout(() => { d._longPressed = true; rangeSel(d);
                                 navigator.vibrate?.(25); }, 450);
    });
    for(const ev of ['pointerup', 'pointercancel', 'pointerleave'])
      d.addEventListener(ev, () => clearTimeout(d._lp));
    if(selected.has(ph.id)){ selected.set(ph.id, d); d.classList.add('sel'); }
    if(ph.fav){
      const f = document.createElement('span');
      f.className = 'favmark'; f.innerHTML = '&#9829;';
      d.appendChild(f);
    }
    if(ph.video){
      // the duration badge is what marks a square as a video
      const b = document.createElement('span');
      b.className = 'badge';
      b.textContent = ph.duration != null ? fmtDur(ph.duration) : 'video';
      d.appendChild(b);
      // ponytail: fixed 250 MB cut (~top 12% of videos) -- slow to stream on mobile data
      if(ph.bytes >= 250e6){
        const s = document.createElement('span');
        s.className = 'big';
        s.textContent = ph.bytes >= 1e9 ? (ph.bytes/1e9).toFixed(1) + ' GB'
                                        : Math.round(ph.bytes/1e6) + ' MB';
        d.appendChild(s);
        d.title += ' · large file';
      }
    }
  }
  return d;
}

// Every view pages through this one function, so "Load more" can never load the
// wrong kind of content.
// --- zoom: 0 = every item, then collections by week, month, year ---------------
const ZOOM = [{level:null,    label:'All items'},
              {level:'week',  label:'Weeks'},
              {level:'month', label:'Months'},
              {level:'year',  label:'Years'}];
let zoom = 0, lastMonth = null;
// Every reset starts a new generation; a slower, older load that finishes later
// must not append its results to the new grid.
let loadSeq = 0;

function monthName(ym, style){       // '2026-09' -> 'September 2026' / 'Sep 2026'
  const [y, m] = ym.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleString('en', {month: style, year: 'numeric'});
}

// A period's date range, for the From/To filter when a collection is opened.
function periodRange(level, key){
  if(level === 'year') return [key + '-01-01', key + '-12-31'];
  const [y, m, d] = key.split('-').map(Number);
  if(level === 'month') return [key + '-01', fmt(new Date(y, m, 0))];   // day 0 = last
  const s = new Date(y, m - 1, d), e = new Date(y, m - 1, d + 6);       // Mon..Sun
  return [fmt(s), fmt(e)];
}

function periodLabel(level, key){
  if(level === 'year') return key;
  if(level === 'month') return monthName(key, 'short');
  const [s, e] = periodRange('week', key).map(x => new Date(x + 'T00:00'));
  const dm = d => d.getDate() + ' ' + d.toLocaleString('en', {month: 'short'});
  if(s.getFullYear() !== e.getFullYear())
    return `${dm(s)} ${s.getFullYear()} – ${dm(e)} ${e.getFullYear()}`;
  if(s.getMonth() !== e.getMonth()) return `${dm(s)} – ${dm(e)} ${e.getFullYear()}`;
  return `${s.getDate()}–${dm(e)} ${e.getFullYear()}`;
}

function periodCell(level, p){
  const d = document.createElement('div');
  d.className = 'cell stack period' + (level === 'year' ? ' y' : '');
  const im = document.createElement('img');
  im.src = '/thumb/' + p.id; im.loading = 'lazy';
  const s = document.createElement('span');
  s.className = 'psticker'; s.textContent = periodLabel(level, p.key);
  const b = document.createElement('span');
  b.className = 'badge'; b.textContent = p.n.toLocaleString();
  d.append(im, s, b);
  d.title = `${periodLabel(level, p.key)} · ${p.n.toLocaleString()} items\nTap to open`;
  // opening a collection = filter to its dates and zoom in one step
  // Intersected with any From/To already set: a week straddling the filter's edge
  // must open to the same items its badge counted, not spill past the filter.
  d.onclick = () => {
    let [s, e] = periodRange(level, p.key);
    if($('#since').value > s) s = $('#since').value;
    if($('#until').value && $('#until').value < e) e = $('#until').value;
    $('#since').value = s; $('#until').value = e;
    setZoom(zoom - 1);
  };
  return d;
}

async function loadPeriods(my){
  const lv = ZOOM[zoom].level;
  $('#status').textContent = 'grouping…';
  const p = params(0); p.delete('offset'); p.delete('limit'); p.set('level', lv);
  const data = await jget('/api/periods?' + p);
  if(my !== loadSeq) return;
  for(const x of data.periods) $('#grid').appendChild(periodCell(lv, x));
  $('#more').hidden = true;
  const total = data.periods.reduce((a, x) => a + x.n, 0);
  $('#status').textContent = `${data.periods.length} ${ZOOM[zoom].label.toLowerCase()}`
      + ` · ${total.toLocaleString()} items`;
}

function setZoom(z){
  zoom = Math.max(0, Math.min(ZOOM.length - 1, z));
  $('#zlabel').textContent = ZOOM[zoom].label;
  $('#zin').disabled = zoom === 0; $('#zout').disabled = zoom === ZOOM.length - 1;
  load(true);
}

async function load(reset){
  if(reset){ offset = 0; lastMonth = null; $('#grid').innerHTML = '';
             seed = Math.floor(Math.random()*999983); loadSeq++; }
  const my = loadSeq;
  paintFilters();
  // a timelapse sequence or a folder is one small set; grouping by date is
  // meaningless there, and the selection bar takes the zoom bar's place
  $('#zoombar').hidden = view === 'group' || view === 'share' || selecting;

  if(view === 'share'){
    const s = await jget('/api/shares/' + shareId);
    if(my !== loadSeq) return;
    for(const ph of s.photos) $('#grid').appendChild(cell(ph));
    $('#more').hidden = true;
    const bytes = s.photos.reduce((a, p) => a + (p.bytes || 0), 0);
    $('#status').textContent = s.photos.length
        ? `${items(s.photos.length)} · ${fmtBytes(bytes)} · tap Select to remove items`
        : 'This folder is empty. Go back, tap Select, pick items, then "Add to folder".';
    paintFolderCrumb(s.name, s.photos.length, bytes);
    return;
  }

  if(view === 'group'){
    const g = await jget(`/api/group?folder=${encodeURIComponent(groupFolder)}`
                         + `&offset=${offset}&limit=${PAGE}`);
    if(my !== loadSeq) return;
    for(const ph of g.photos){
      const d = cell(ph);
      d.title = ph.filename + (ph.taken_at ? ' · ' + ph.taken_at : '');
      $('#grid').appendChild(d);
    }
    offset += g.photos.length;
    $('#more').hidden = g.photos.length < PAGE;
    $('#status').textContent = `${offset} of ${groupTotal} frames in ${groupLabel}`;
    return;
  }

  if(zoom > 0) return loadPeriods(my);

  $('#status').textContent = 'searching…';
  let data;
  try { data = await jget('/api/search?' + params(offset));
        if(my !== loadSeq) return; }
  catch(e){
    if(view === 'locked'){            // unlock expired mid-browse
      view = 'search'; $('#crumb').classList.remove('on');
      $('#status').textContent = 'unlock expired'; return load(true);
    }
    throw e;
  }
  // month headers only make sense when the grid is in date order
  const dated = ['newest','oldest'].includes($('#sort').value);
  for(const ph of data.photos){
    const mk = ph.taken_at && ph.taken_at.slice(0,7);
    if(dated && mk && mk !== lastMonth){
      lastMonth = mk;
      const h = document.createElement('div');
      h.className = 'mhdr';
      h.textContent = monthName(mk, 'long');
      h.onclick = () => { if(selecting) monthSel(h); };
      $('#grid').appendChild(h);
    }
    $('#grid').appendChild(cell(ph));
  }
  offset += data.photos.length;
  $('#more').hidden = data.photos.length < PAGE;

  const groups = data.photos.filter(p => p.group).length;
  if(view === 'locked'){
    $('#status').textContent = `${offset} locked items shown`
        + (groups ? ` (${groups} sequence${groups>1?'s':''} collapsed)` : '');
  } else {
    const st = await jget('/api/stats');
    $('#status').textContent = `${offset} shown`
        + (groups ? ` (${groups} sequence${groups>1?'s':''} collapsed)` : '')
        + ` · ${st.done.toLocaleString()} indexed`
        + (st.pending ? ` · ${st.pending.toLocaleString()} still processing` : '')
        + (tagCount ? ` · ${tagCount.toLocaleString()} tags` : '');
  }
}

// a normal search always leaves the locked scope
async function search(reset){
  if(reset){
    const wasLocked = view === 'locked';
    view = 'search'; $('#crumb').classList.remove('on');
    // the locked folder has its own set of places; reload the list on scope change
    if(wasLocked){ places.clear(); await loadPlaces(); }
  }
  return load(reset);
}

function askPassword(n){
  $('#pwcount').textContent = n + ' items are locked.';
  $('#pw').value = ''; $('#pwerr').textContent = '';
  $('#pwdlg').showModal(); $('#pw').focus();
}

async function tryUnlock(){
  const r = await fetch('/api/unlock', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: $('#pw').value})});
  if(!r.ok){ $('#pwerr').textContent = 'Wrong password'; $('#pw').select(); return; }
  $('#pwdlg').close();
  openLocked();
}

async function openLocked(){
  view = 'locked';
  places.clear(); await loadPlaces();           // locked scope, different places
  const { total } = await jget('/api/locked_count');
  $('#crumb').classList.add('on');
  $('#crumbText').innerHTML =
      `&#128274; <b>Locked</b> — ${total} items · search and filters apply in here `
    + `<button id="relock" style="margin-left:8px">Lock again</button>`;
  $('#relock').onclick = async () => {
    await fetch('/api/lock', {method:'POST'});
    search(true);
  };
  await load(true);
}

// --- select mode and folders ---------------------------------------------------
let selecting = false, shareId = null;
const selected = new Map();                  // id -> tile, kept across Load more

const esc = s => String(s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const items = n => n + (n === 1 ? ' item' : ' items');
function fmtBytes(b){
  return b >= 1e9 ? (b/1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b/1e6)) + ' MB';
}

let anchor = null;                           // last tile ticked by hand: start of a range

function setSel(d, on){
  if(on){ selected.set(d._pid, d); d.classList.add('sel'); }
  else { selected.delete(d._pid); d.classList.remove('sel'); }
}
function toggleSel(id, d){
  setSel(d, !selected.has(id));
  anchor = d;
  paintSel();
}
// Shift+click (desktop) or long-press (touch): select everything from the last
// ticked tile to this one, in grid order. Headers and stacks are skipped.
function rangeSel(d){
  const tiles = [...$('#grid').children].filter(x => x._pid);
  const i = tiles.indexOf(anchor), j = tiles.indexOf(d);
  if(i < 0) return toggleSel(d._pid, d);     // no anchor yet: plain tick
  for(let k = Math.min(i, j); k <= Math.max(i, j); k++) setSel(tiles[k], true);
  anchor = d;
  paintSel();
}
// A month header ticks every loaded item under it; tapped again, unticks them.
async function monthSel(h){
  // The month may run past what's loaded: page in until its last item is on
  // screen (another header appears) or there is nothing more to load.
  const ends = () => { for(let x = h.nextElementSibling; x; x = x.nextElementSibling)
                         if(x.classList.contains('mhdr')) return true; return false; };
  while(!ends() && !$('#more').hidden){
    $('#selcount').textContent = 'loading month…';
    const before = $('#grid').children.length;
    await load(false);
    if($('#grid').children.length === before) break;
  }
  const tiles = [];
  for(let x = h.nextElementSibling; x && !x.classList.contains('mhdr'); x = x.nextElementSibling)
    if(x._pid) tiles.push(x);
  const on = !tiles.every(x => selected.has(x._pid));
  tiles.forEach(x => setSel(x, on));
  paintSel();
}
function paintSel(){
  $('#selcount').textContent = selected.size + ' selected';
  $('#selAdd').disabled = $('#selRemove').disabled = !selected.size;
}
function setSelecting(on){
  selecting = on; anchor = null;
  selected.forEach(d => d.classList.remove('sel')); selected.clear();
  document.body.classList.toggle('selecting', on);
  $('#selbar').hidden = !on;
  $('#selAdd').hidden = view === 'share'; $('#selRemove').hidden = view !== 'share';
  $('#selbtn').classList.toggle('on', on);
  $('#zoombar').hidden = on || view === 'group' || view === 'share';
  if(on && zoom > 0) setZoom(0);             // only single items can be picked
  paintSel();
}

async function openAddDialog(){
  const {folders} = await jget('/api/shares');
  $('#fdhdr').textContent = `Add ${selected.size} item${selected.size > 1 ? 's' : ''} to…`;
  $('#fdmsg').textContent = ''; $('#fdname').value = '';
  $('#fdlist').innerHTML = folders.length ? '' : '<p class="hint">No folders yet. Name one below.</p>';
  for(const f of folders){
    const row = document.createElement('div'); row.className = 'frow';
    row.innerHTML = `<span class="fname">&#128193; ${esc(f.name)}</span>`
                  + `<span class="fmeta">${items(f.n)}</span><button>Add here</button>`;
    row.querySelector('button').onclick = () => addTo(f.id, f.name);
    $('#fdlist').appendChild(row);
  }
  $('#folderdlg').showModal();
}

async function addTo(id, name){
  const r = await fetch(`/api/shares/${id}/items`, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids: [...selected.keys()]})});
  if(!r.ok){
    $('#fdmsg').textContent = r.status === 403
        ? 'Some items are locked: unlock the Locked folder first.' : 'Could not add (' + r.status + ')';
    return;
  }
  const {n} = await r.json();
  $('#fdmsg').innerHTML = `Added to <b>${esc(name)}</b>, which now has ${n} items. `
      + `<a href="/api/shares/${id}/zip">&#11015; Download zip</a>`;
  $('#fdlist').innerHTML = ''; $('#fdname').value = '';
  setSelecting(false);
}

async function createAndAdd(){
  const name = $('#fdname').value.trim();
  if(!name){ $('#fdmsg').textContent = 'Type a folder name first.'; $('#fdname').focus(); return; }
  const r = await fetch('/api/shares', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  if(!r.ok){ $('#fdmsg').textContent = 'Could not create folder (' + r.status + ')'; return; }
  const f = await r.json();
  await addTo(f.id, f.name);
}

async function removeSelected(){
  const r = await fetch(`/api/shares/${shareId}/items`, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids: [...selected.keys()], remove: true})});
  if(!r.ok) return alert('Could not remove (' + r.status + ')');
  setSelecting(false); load(true);
}

async function openFoldersList(){
  const {folders} = await jget('/api/shares');
  $('#fllist').innerHTML = folders.length ? ''
      : '<p class="hint">No folders yet. Tap &#9745; Select, pick items, then "Add to folder".</p>';
  for(const f of folders){
    const row = document.createElement('div'); row.className = 'frow';
    row.innerHTML = `<span class="fname">&#128193; ${esc(f.name)}</span>`
        + `<span class="fmeta">${f.n} · ${fmtBytes(f.bytes)}</span>`
        + `<button class="o">Open</button>`
        + (f.n ? `<a href="/api/shares/${f.id}/zip" title="download as zip"><button>&#11015; Zip</button></a>` : '')
        + `<button class="x" title="delete folder (photos stay in the library)">&#128465;</button>`;
    row.querySelector('.o').onclick = () => { $('#folderslist').close(); openFolder(f.id); };
    row.querySelector('.x').onclick = async () => {
      if(!confirm(`Delete folder "${f.name}"? The photos stay in your library.`)) return;
      await fetch('/api/shares/' + f.id, {method:'DELETE'});
      if(shareId === f.id && view === 'share') search(true);
      openFoldersList();
    };
    $('#fllist').appendChild(row);
  }
  if(!$('#folderslist').open) $('#folderslist').showModal();
}

async function openFolder(id){
  if(selecting) setSelecting(false);
  view = 'share'; shareId = id;
  $('#crumb').classList.add('on'); $('#crumbText').textContent = 'loading folder…';
  await load(true);
}

function paintFolderCrumb(name, n, bytes){
  $('#crumbText').innerHTML = `&#128193; <b>${esc(name)}</b> — ${items(n)} `
      + (n ? `<a href="/api/shares/${shareId}/zip"><button style="margin-left:8px">`
             + `&#11015; Download zip (${fmtBytes(bytes)})</button></a>` : '');
}

async function openGroup(folder, label, n){
  view = 'group'; groupFolder = folder; groupLabel = label; groupTotal = n;
  $('#crumb').classList.add('on');
  $('#crumbText').textContent = `${label} — ${n} frames`;
  $('#status').textContent = 'loading sequence…';
  await load(true);
}

function paintFav(on){
  $('#favBtn').classList.toggle('on', on);
  $('#favBtn').innerHTML = on ? '&#9829; Favourited' : '&#9825; Favourite';
}

// Grid order is the viewing order. Locked tiles and sequence stacks have no
// _open and are skipped; at the end of what's loaded, the next page is fetched.
let curTile = null, openSeq = 0;
async function step(dir){
  if(!curTile) return;
  let el = curTile;
  for(;;){
    el = dir > 0 ? el.nextElementSibling : el.previousElementSibling;
    if(el && el._open) return el._open();
    if(!el){
      if(dir > 0 && !$('#more').hidden){ el = curTile; await load(false); continue; }
      return;                                 // first/last item: stay put
    }
  }
}

async function open_(id, isVideo, tile){
  curTile = tile || null;
  $('#big').hidden = !!isVideo; $('#vid').hidden = !isVideo;
  $('#simBtn').hidden = !!isVideo;          // videos carry no tags to compare
  const v = $('#vid');
  if(isVideo){ $('#big').removeAttribute('src'); v.src = '/video/' + id; }
  else {
    // stepping from a video to a photo must stop the video, not just hide it
    v.pause(); v.removeAttribute('src'); v.load();
    $('#big').src = '/full/' + id;
  }
  $('#dl').href = '/original/' + id;
  $('#simBtn').onclick = async () => {
    const s = await jget('/api/similar/' + id);
    $('#dlg').close(); $('#grid').innerHTML = ''; offset = 0;
    $('#crumb').classList.remove('on');
    for(const ph of s.photos){
      const d = cell(ph);
      d.title = ph.shared + ' shared tags';
      $('#grid').appendChild(d);
    }
    $('#more').hidden = true;
    $('#status').textContent = s.photos.length + ' similar photos';
  };
  const my = ++openSeq;
  const m = await jget('/api/photo/' + id);
  if(my !== openSeq) return;     // tapped on to another item while this loaded
  let fav = m.fav; paintFav(fav);
  $('#favBtn').onclick = async () => {
    const r = await fetch('/api/fav/' + id, {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({on: !fav})});
    if(!r.ok){ $('#favBtn').textContent = 'Failed, tap to retry'; return; }
    fav = !fav; paintFav(fav);
    if(tile){                                   // keep the grid square in step
      tile.querySelector('.favmark')?.remove();
      if(fav){ const f = document.createElement('span');
               f.className = 'favmark'; f.innerHTML = '&#9829;'; tile.appendChild(f); }
    }
    allTags = await jget('/api/tags'); paintChips();   // favourite chip count
  };
  const dateBit = m.taken_at
      ? `<span class="link" id="mdate" title="filter by day, week, month or year">`
        + `${m.taken_at}</span>`
      : 'no date';
  const placeBit = m.place
      ? ` · <span class="link" id="mplace" title="show everything from here">`
        + `${m.place}</span>`
      : '';
  // exact GPS point, not the area name; opens the Maps app on phones
  const gotoBit = m.lat != null && m.lon != null
      ? ` · <a class="link" target="_blank" rel="noopener noreferrer" `
        + `href="https://www.google.com/maps/search/?api=1&query=${m.lat},${m.lon}" `
        + `title="${m.lat.toFixed(6)}, ${m.lon.toFixed(6)}">&#128205; Go to location</a>`
      : '';
  $('#meta').innerHTML =
    `<b>${m.filename}</b> · ${dateBit}${placeBit}${gotoBit} · ${m.w}×${m.h}`
    + (m.description ? `<br>${m.description}` : '')
    + '<br>' + m.tags.map(t => `<span class="tag" data-t="${t}">${t}</span>`).join('');
  $('#meta').querySelectorAll('.tag').forEach(el => el.onclick = () => {
    active = new Set([el.dataset.t]); $('#dlg').close(); paintChips(); load(true);
  });
  if(m.taken_at) $('#mdate').onclick = () => askPeriod(m.taken_at);
  if(m.place) $('#mplace').onclick = () => filterByPlace(m.place);
  if(!$('#dlg').open) $('#dlg').showModal();   // already open when stepping
}

// ---- filters: places (several at once), dates, per-field clear ---------------
const places = new Set();            // applied; '__none__' = items without location
let placeList = [], noPlace = 0, draft = new Set();

// Refetched each time the picker opens: new items keep arriving from tagging.
async function loadPlaces(){
  const scope = view === 'locked' ? '?scope=locked' : '';
  try {
    const d = await jget('/api/places' + scope);
    placeList = d.places; noPlace = d.no_place;
  } catch(e){}
}

function placeLabel(p){ return p === '__none__' ? 'No location' : p; }

// Button text, active styling and the × buttons, all from current state.
function paintFilters(){
  const n = places.size;
  $('#placebtn').innerHTML = '&#128205; ' + (n === 0 ? 'All places'
      : n === 1 ? esc(placeLabel([...places][0])) : n + ' places');
  $('#placebtn').title = n ? [...places].map(placeLabel).join('\n') : 'choose one or more places';
  $('#placebtn').classList.toggle('on', n > 0);
  $('#clrPlaces').parentElement.classList.toggle('has', n > 0);
  $('#clrQ').parentElement.classList.toggle('has', !!$('#q').value);
  const s = $('#since').value, u = $('#until').value;
  $('#clrDates').parentElement.classList.toggle('has', !!(s || u));
  if(window.fp){
    // mirror From/To set elsewhere (zoom tiles, viewer date link) without
    // firing the picker's own change handler
    const want = [s, u].filter(Boolean);
    const have = fp.selectedDates.map(fmt);
    if(want.join() !== have.join()) fp.setDate(want, false);
    fp.altInput.classList.toggle('on', !!(s || u));
  }
}

function renderPlaceList(){
  const f = $('#placeq').value.trim().toLowerCase();
  const rows = [...placeList.map(p => [p.place, p.count])];
  if(noPlace) rows.push(['__none__', noPlace]);
  // chosen ones first so they're easy to untick, then alphabetical
  rows.sort((a, b) => (draft.has(b[0]) - draft.has(a[0])));
  $('#placelist').innerHTML = '';
  for(const [p, c] of rows){
    if(f && !placeLabel(p).toLowerCase().includes(f)) continue;
    const l = document.createElement('label');
    l.innerHTML = `<input type="checkbox"${draft.has(p) ? ' checked' : ''}>`
                + `<span>${esc(placeLabel(p))}</span><em>${c.toLocaleString()}</em>`;
    l.querySelector('input').onchange = e => {
      e.target.checked ? draft.add(p) : draft.delete(p); paintPlaceGo(); };
    $('#placelist').appendChild(l);
  }
  paintPlaceGo();
}
function paintPlaceGo(){
  $('#placego').textContent = draft.size ? `Show photos (${draft.size})` : 'Show all places';
}
async function openPlacePicker(){
  draft = new Set(places);
  $('#placeq').value = '';
  $('#placelist').innerHTML = '<p class="hint">loading…</p>';
  $('#placedlg').showModal();
  await loadPlaces();
  renderPlaceList();
}
function applyPlaces(set){
  places.clear(); set.forEach(p => places.add(p));
  if(view !== 'group') load(true);
}

// ---- date period filters ------------------------------------------------
// Built from the date part only, with local Date(y, m-1, d). Parsing
// "2025-09-30 17:16:24" directly is non-standard and drifts by a day in some
// browsers/timezones, which is exactly where off-by-one bugs live.
const fmt = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`
               + `-${String(d.getDate()).padStart(2,'0')}`;

function periods(takenAt){
  const [y, mo, dd] = takenAt.slice(0,10).split('-').map(Number);
  const base = new Date(y, mo - 1, dd);
  const wkStart = new Date(base);
  wkStart.setDate(base.getDate() - ((base.getDay() + 6) % 7));   // Monday
  const wkEnd = new Date(wkStart); wkEnd.setDate(wkStart.getDate() + 6);
  return [
    ['Same day',   base,                    base],
    ['Same week',  wkStart,                 wkEnd],
    ['Same month', new Date(y, mo - 1, 1),  new Date(y, mo, 0)],   // day 0 = last
    ['Same year',  new Date(y, 0, 1),       new Date(y, 11, 31)],
  ].map(([label, s, e]) => ({label, since: fmt(s), until: fmt(e)}));
}

function askPeriod(takenAt){
  $('#datehdr').textContent = 'Photos from ' + takenAt.slice(0,10);
  $('#dateopts').innerHTML = '';
  for(const p of periods(takenAt)){
    const b = document.createElement('button');
    b.innerHTML = `${p.label}<span class="rng">`
                + (p.since === p.until ? p.since : `${p.since} → ${p.until}`)
                + `</span>`;
    b.onclick = () => {
      $('#datedlg').close(); $('#dlg').close();
      $('#since').value = p.since; $('#until').value = p.until;
      load(true);
    };
    $('#dateopts').appendChild(b);
  }
  $('#datedlg').showModal();
}

function filterByPlace(place){
  $('#dlg').close();
  applyPlaces([place]);
}

// ---- map ----------------------------------------------------------------
let map = null, pinLayer = null;

function paintMapBar(){
  const n = draft.size;
  $('#maphint').textContent = n ? [...draft].join(', ') : 'tap pins to choose places';
  $('#mapnone').hidden = !n;
  $('#mapgo').disabled = !n;
  $('#mapgo').textContent = n ? `Show photos (${n})` : 'Show photos';
}

async function openMap(){
  const scope = view === 'locked' ? '?scope=locked' : '';
  let d;
  try { d = await jget('/api/map' + scope); }
  catch(e){ alert('Could not load map data'); return; }

  $('#mapdlg').showModal();
  if(!map){
    map = L.map('map', {attributionControl:false}).setView([13.0, 80.2], 5);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                {maxZoom:18, crossOrigin:true}).addTo(map);
    L.control.attribution({prefix:false})
     .addAttribution('&copy; OpenStreetMap').addTo(map);
  }
  // Leaflet mis-measures a container that was hidden when created
  setTimeout(() => map.invalidateSize(), 60);

  if(pinLayer) pinLayer.remove();
  pinLayer = L.layerGroup().addTo(map);
  draft = new Set([...places].filter(p => p !== '__none__'));
  paintMapBar();

  if(!d.pins.length){
    $('#mapinfo').textContent = 'no photos with coordinates yet';
    return;
  }
  const max = Math.max(...d.pins.map(p => p.count));
  for(const p of d.pins){
    // area ~ count so a 1000-photo place does not dwarf the map
    const r = 7 + 16 * Math.sqrt(p.count / max);
    const m = L.circleMarker([p.lat, p.lon], {radius:r, weight:2}).addTo(pinLayer);
    const paintPin = () => m.setStyle(draft.has(p.place)
        ? {color:'#ffb347', fillColor:'#ffb347', fillOpacity:.75, weight:3}
        : {color:'#6ea8fe', fillColor:'#6ea8fe', fillOpacity:.45, weight:2});
    paintPin();
    m._paint = paintPin;
    const span = (p.first_at||'').slice(0,10) === (p.last_at||'').slice(0,10)
        ? (p.first_at||'').slice(0,10)
        : (p.first_at||'').slice(0,10) + ' – ' + (p.last_at||'').slice(0,10);
    m.bindTooltip(`<b>${p.place}</b><br>${p.count} photo${p.count>1?'s':''}<br>${span}`,
                  {direction:'top'});
    // tapping toggles; nothing is applied until "Show photos"
    m.on('click', () => {
      draft.has(p.place) ? draft.delete(p.place) : draft.add(p.place);
      paintPin(); paintMapBar();
    });
    if(p.count / max > 0.08){
      const icon = L.divIcon({className:'pin-label', html:String(p.count),
                              iconSize:[36,14], iconAnchor:[18,7]});
      L.marker([p.lat, p.lon], {icon, interactive:false}).addTo(pinLayer);
    }
  }
  const b = L.latLngBounds(d.pins.map(p => [p.lat, p.lon]));
  map.fitBounds(b.pad(0.25));
  const tot = d.pins.reduce((a,p) => a + p.count, 0);
  $('#mapinfo').textContent =
      `${d.pins.length} places · ${tot.toLocaleString()} located photos`;
}

let allTags = [];
function paintChips(){
  $('#chips').innerHTML = '';
  for(const t of allTags.slice(0,120)){
    const c = document.createElement('span');
    c.className = 'chip' + (t.pinned ? ' pinned' : '') + (active.has(t.tag) ? ' on' : '');
    c.textContent = (t.pinned ? '★ ' : '') + `${t.tag} ${t.count}`;
    c.onclick = () => { active.has(t.tag) ? active.delete(t.tag) : active.add(t.tag);
                        paintChips(); search(true); };
    $('#chips').appendChild(c);
  }
}

// Searching/sorting inside the locked folder must stay inside it, so these keep
// the current view instead of resetting to 'search'.
$('#go').onclick = () => { active.clear(); paintChips(); load(true); };
$('#q').addEventListener('keydown', e => { if(e.key === 'Enter') $('#go').click(); });
$('#sort').onchange = () => { if(view === 'group') return; load(true); };
$('#mapbtn').onclick = openMap;
$('#mapclose').onclick = () => $('#mapdlg').close();      // Back = discard pin picks
$('#mapnone').onclick = () => {
  draft.clear(); pinLayer.eachLayer(l => l._paint && l._paint()); paintMapBar(); };
$('#mapgo').onclick = () => { $('#mapdlg').close(); applyPlaces(draft); };
$('#placebtn').onclick = openPlacePicker;
$('#placeq').addEventListener('input', renderPlaceList);
$('#placecancel').onclick = () => $('#placedlg').close();
$('#placenone').onclick = () => { draft.clear(); renderPlaceList(); };
$('#placego').onclick = () => { $('#placedlg').close(); applyPlaces(draft); };
$('#datecancel').onclick = () => $('#datedlg').close();
$('#q').addEventListener('input', paintFilters);
$('#clrQ').onclick = () => { $('#q').value = ''; paintFilters(); $('#go').click(); };
$('#clrPlaces').onclick = () => applyPlaces([]);
$('#clrDates').onclick = () => {
  $('#since').value = $('#until').value = ''; fp.clear();
  if(view !== 'group') load(true); };
$('#clear').onclick = () => { active.clear(); ['q','since','until']
  .forEach(k => $('#'+k).value = ''); places.clear(); $('#sort').value = 'newest';
  paintChips(); search(true); };

// Range calendar. One tap + close = that single day; two taps = a range.
// Presets cover the common jumps that are tedious to click through.
const fp = flatpickr('#dates', {
  mode: 'range', dateFormat: 'Y-m-d', altInput: true, altFormat: 'j M Y',
  maxDate: 'today', disableMobile: true, monthSelectorType: 'dropdown',
  locale: {rangeSeparator: ' – ', firstDayOfWeek: 1},
  onReady(_, __, inst){
    const today = new Date(), y = today.getFullYear(), m = today.getMonth();
    const back = n => { const d = new Date(today); d.setDate(d.getDate() - n); return d; };
    const mon = back((today.getDay() + 6) % 7);
    const presets = [['Today', today, today], ['This week', mon, today],
                     ['This month', new Date(y, m, 1), today],
                     ['Last 30 days', back(29), today],
                     ['This year', new Date(y, 0, 1), today],
                     ['Last year', new Date(y - 1, 0, 1), new Date(y - 1, 11, 31)]];
    const box = document.createElement('div'); box.className = 'fp-presets';
    for(const [label, s, e] of presets){
      const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
      b.onclick = () => { inst.setDate([s, e], true); inst.close(); };
      box.appendChild(b);
    }
    inst.calendarContainer.appendChild(box);
  },
  onClose(sel){
    const s = sel[0] ? fmt(sel[0]) : '', e = sel[1] ? fmt(sel[1]) : s;
    if(s === $('#since').value && e === $('#until').value) return;
    $('#since').value = s; $('#until').value = e;
    if(view !== 'group') load(true);
  },
});
window.fp = fp;
$('#close').onclick = () => $('#dlg').close();
$('#zin').onclick = () => setZoom(zoom - 1);
$('#zout').onclick = () => setZoom(zoom + 1);
$('#zin').disabled = true;
$('#navPrev').onclick = () => step(-1);
$('#navNext').onclick = () => step(1);
// every path that closes the viewer (Back, Esc, tag/date/place links) must stop
// playback and drop the connection, or audio and a large download keep running
$('#dlg').addEventListener('close', () => {
  const v = $('#vid'); v.pause(); v.removeAttribute('src'); v.load();
});
$('#back').onclick = () => { if(selecting) setSelecting(false); search(true); };
$('#selbtn').onclick = () => setSelecting(!selecting);
$('#selDone').onclick = () => setSelecting(false);
$('#selAdd').onclick = openAddDialog;
$('#selRemove').onclick = removeSelected;
$('#foldersbtn').onclick = openFoldersList;
$('#fdcreate').onclick = createAndAdd;
$('#fdname').addEventListener('keydown', e => { if(e.key === 'Enter') createAndAdd(); });
$('#fdclose').onclick = () => $('#folderdlg').close();
$('#flclose').onclick = () => $('#folderslist').close();
$('#more').onclick = () => load(false);
$('#pwok').onclick = tryUnlock;
$('#pwcancel').onclick = () => $('#pwdlg').close();
$('#pw').addEventListener('keydown', e => { if(e.key === 'Enter') tryUnlock(); });

(async () => {
  allTags = await jget('/api/tags'); paintChips();
  await loadPlaces();
  await search(true);                       // grid first: don't wait on slow counts
  jget('/api/tagcount').then(d => { tagCount = d.tags; }).catch(() => {});
})();
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 (default, local only) or your Tailscale IP")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--tailscale", action="store_true",
                    help="bind to this machine's Tailscale IP: reachable from your "
                         "other signed-in devices, and nothing else")
    args = ap.parse_args()

    if args.tailscale:
        import subprocess
        exe = r"C:\Program Files\Tailscale\tailscale.exe"
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                                 timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            raise SystemExit(f"could not run tailscale: {e}")
        ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        if not ip:
            raise SystemExit(
                "no Tailscale IP yet. Run:  tailscale up\n"
                "then sign in when the browser opens, and try again.\n"
                f"(tailscale said: {out.stderr.strip() or 'nothing'})")
        args.host = ip
        print(f"Tailscale IP: {ip}")

    from photoindex import db as index_db
    index_db().close()          # applies pending migrations (e.g. shared folders)

    import uvicorn
    print(f"serving on http://{args.host}:{args.port}")
    if args.host == "0.0.0.0":
        print("WARNING: 0.0.0.0 exposes your library on every network this "
              "machine joins, including public wifi. Prefer --tailscale.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
