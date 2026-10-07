"""Personal photo index: scan -> thumbnail + EXIF -> SQLite, then caption with a local VLM.

Resumable by construction: every row carries a state, so an interrupted run
continues from `state='pending'` with no checkpoint file.

    python photoindex.py init
    python photoindex.py scan "D:/Photos"
    python photoindex.py caption --limit 78
    python photoindex.py stats
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile
import pillow_heif

pillow_heif.register_heif_opener()
ImageFile.LOAD_TRUNCATED_IMAGES = True  # a few phone files are cut short; index them anyway

ROOT = Path(__file__).parent
# Per-machine settings (library location, locked folder, its password hash).
# Never committed -- see config.example.json.
CONFIG_PATH = ROOT / "config.json"
try:
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:
    CONFIG = {}
DB_PATH = ROOT / "photos.db"
THUMB_DB = ROOT / "thumbs.db"
THUMB_PX = 320
CAPTION_PX = 768

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif",
              ".tif", ".tiff", ".bmp"}
RAW_EXTS = {".dng", ".cr2", ".arw", ".nef", ".orf", ".rw2"}
# Indexed for date/place browsing only: they go straight to state='done' and are
# never captioned. DJI .LRF low-res proxies are deliberately absent -- each is a
# duplicate of an .MP4 beside it.
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".avi", ".webm", ".wmv"}

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS photos (
  path        TEXT PRIMARY KEY,
  mtime       REAL NOT NULL,
  bytes       INTEGER NOT NULL,
  width       INTEGER,
  height      INTEGER,
  taken_at    TEXT,
  lat         REAL,
  lon         REAL,
  place       TEXT,
  thumb       TEXT,
  description TEXT,
  state       TEXT NOT NULL DEFAULT 'pending',
  error       TEXT,
  updated_at  TEXT NOT NULL,
  duration    REAL            -- seconds; videos only
);
CREATE INDEX IF NOT EXISTS photos_state    ON photos(state);
CREATE INDEX IF NOT EXISTS photos_taken_at ON photos(taken_at);
CREATE INDEX IF NOT EXISTS photos_place    ON photos(place);

CREATE TABLE IF NOT EXISTS tags (
  path   TEXT NOT NULL REFERENCES photos(path) ON DELETE CASCADE,
  tag    TEXT NOT NULL,
  -- 'model' tags are replaced on every re-caption; 'manual' ones are curated by
  -- hand and must survive it. Without this distinction, re-captioning a photo
  -- silently erases tags you added yourself.
  source TEXT NOT NULL DEFAULT 'model',
  PRIMARY KEY (path, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
-- without this, "which tags are manual" is a full scan of every tag row
CREATE INDEX IF NOT EXISTS tags_source ON tags(source);
-- Covers the tag-chip query entirely: grouping by tag while reading `source` per
-- row otherwise costs one table lookup per row (456k of them, measured at 14.4s).
-- With source in the index it is a single sequential scan: 0.13s.
CREATE INDEX IF NOT EXISTS tags_tag_source ON tags(tag, source);

-- Remembered so new files inherit the tag. Applying a manual tag once only covers
-- the photos that existed at that moment; anything added to the folder later would
-- silently miss it.
CREATE TABLE IF NOT EXISTS tag_rules (
  tag    TEXT NOT NULL,
  prefix TEXT NOT NULL,
  PRIMARY KEY (tag, prefix)
);
"""


# Shared folders are virtual: rows pointing at library items, so nothing is moved
# or copied on disk and one item can sit in several shares. They are downloaded
# as a zip to send on; there is no public link. Items cascade away when prune
# drops a deleted file.
SHARE_SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS share_items (
  share_id TEXT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
  path     TEXT NOT NULL REFERENCES photos(path) ON DELETE CASCADE,
  added_at TEXT NOT NULL,
  PRIMARY KEY (share_id, path)
) WITHOUT ROWID;
"""


def ensure_source_column(conn) -> None:
    """Add tags.source to a database created before it existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN source TEXT NOT NULL "
                     "DEFAULT 'model'")
        conn.commit()
        print("migrated: added tags.source")
    conn.execute("CREATE INDEX IF NOT EXISTS tags_source ON tags(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS tags_tag_source ON tags(tag, source)")
    conn.execute("CREATE TABLE IF NOT EXISTS tag_rules ("
                 "tag TEXT NOT NULL, prefix TEXT NOT NULL, PRIMARY KEY (tag, prefix))")
    if "duration" not in {r[1] for r in conn.execute("PRAGMA table_info(photos)")}:
        conn.execute("ALTER TABLE photos ADD COLUMN duration REAL")
        print("migrated: added photos.duration")
    conn.executescript(SHARE_SCHEMA)
    conn.commit()


def apply_tag_rules(conn) -> int:
    """Give every photo under a remembered prefix its manual tag.

    Idempotent (INSERT OR IGNORE), so it is safe to run after every scan -- which
    is the point: files added to a tagged folder later must inherit the tag rather
    than quietly miss it.
    """
    rules = conn.execute("SELECT tag, prefix FROM tag_rules").fetchall()
    added = 0
    for tag, prefix in rules:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tags (path, tag, source) "
            "SELECT path, ?, 'manual' FROM photos WHERE path LIKE ?",
            (tag, prefix.rstrip("\\") + "\\%"))
        added += cur.rowcount
    if rules:
        conn.commit()
        print(f"  tag rules: {len(rules)} rule(s), {added} tag(s) added", flush=True)
    return added


def db(migrate: bool = True):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    if migrate:
        ensure_source_column(conn)
    return conn


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- image loading ----------

def load_image(path: Path) -> Image.Image:
    """RGB image for any supported file. Raw files come from the embedded preview:
    full raw decode costs ~1s each and buys nothing for captioning or thumbnails."""
    if path.suffix.lower() in RAW_EXTS:
        import io, rawpy
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                return Image.fromarray(raw.postprocess(half_size=True)).convert("RGB")
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data)).convert("RGB")
            return Image.fromarray(thumb.data).convert("RGB")
    return Image.open(path).convert("RGB")  # animated GIF/WEBP land on frame 0


def read_video(path: Path):
    """(first frame, taken_at, lat, lon) from the container, via PyAV's bundled ffmpeg.

    creation_time is UTC in every phone and DJI file sampled (it matched mtime at
    +05:30), so it is converted to local time to line up with EXIF photo dates.
    GPS is ISO 6709 ("+12.9513+80.2462/") in Android's `location` tag or Apple's key.
    """
    import re
    import av
    from datetime import timezone
    with av.open(str(path)) as c:
        md = c.metadata
        duration = c.duration / 1_000_000 if c.duration else None  # µs -> s
        # Packet by packet, not c.decode(): some Signal/WhatsApp clips open with a
        # corrupt packet that makes decode() raise, though the next one is fine.
        frame, bad = None, 0
        for pkt in c.demux(video=0):
            try:
                frames = pkt.decode()
            except av.error.InvalidDataError:
                bad += 1
                if bad > 100:
                    raise
                continue
            if frames:
                frame = frames[0]
                break
        if frame is None:
            raise ValueError("no decodable video frame")
        # portrait phone clips are stored landscape plus a display rotation
        img = frame.to_image().rotate(frame.rotation, expand=True)

    taken = None
    ct = md.get("creation_time", "")
    try:
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        if dt.year >= 2000:  # some encoders write 1904/1970 for "unknown"
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            taken = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    lat = lon = None
    m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)",
                 md.get("location") or md.get("com.apple.quicktime.location.ISO6709", ""))
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if (lat, lon) == (0.0, 0.0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
    return img, taken, lat, lon, duration


# ---------- EXIF ----------

def _dms(values, ref) -> float | None:
    try:
        d, m, s = (float(v) for v in values)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    return -deg if str(ref).upper() in ("S", "W") else deg


def read_exif(path: Path):
    """(taken_at, lat, lon). Raw files keep EXIF where PIL can't reach it, so
    they return (None, None, None) and fall back to filesystem mtime."""
    if path.suffix.lower() in RAW_EXTS:
        return None, None, None
    try:
        exif = Image.open(path).getexif()
    except Exception:
        return None, None, None

    taken = None
    for tag in (36867, 36868, 306):  # DateTimeOriginal, DateTimeDigitized, DateTime
        raw = exif.get(tag)
        if raw:
            try:
                taken = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S") \
                                .strftime("%Y-%m-%d %H:%M:%S")
                break
            except ValueError:
                continue

    lat = lon = None
    try:
        gps = exif.get_ifd(0x8825)
        if gps and 2 in gps and 4 in gps:
            lat = _dms(gps[2], gps.get(1, "N"))
            lon = _dms(gps[4], gps.get(3, "E"))
            if lat is not None and not (-90 <= lat <= 90):
                lat = None
            if lon is not None and not (-180 <= lon <= 180):
                lon = None
            # Exactly (0, 0) is "null island" in the Gulf of Guinea -- it means the
            # camera wrote empty GPS tags, not that the photo was taken at sea.
            # 352 photos were being reverse-geocoded to Takoradi, Ghana because of
            # this. Treat it as no location.
            if lat == 0.0 and lon == 0.0:
                lat = lon = None
    except Exception:
        pass
    return taken, lat, lon


# ---------- thumbnails ----------

def thumb_rel(path: Path) -> str:
    h = hashlib.sha1(str(path).encode("utf-8", "surrogateescape")).hexdigest()
    return f"{h[:2]}/{h}.webp"  # sharded: 54k files in one NTFS dir is miserable to browse


def thumbs_db():
    """Thumbnails live as blobs in one SQLite file, not as 65k loose files.

    F: is exFAT with 1 MB clusters, so every ~8 KB .webp occupied a full 1 MB:
    0.5 GB of thumbnails took 63.6 GB of disk. Kept apart from photos.db so the
    index stays small and fast to search. Keyed by the sha1 in photos.thumb."""
    conn = sqlite3.connect(THUMB_DB, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS thumbs (id TEXT PRIMARY KEY, data BLOB NOT NULL)")
    return conn


def thumb_id(rel: str) -> str:
    return Path(rel).stem          # "ab/<sha1>.webp" -> "<sha1>"


def write_thumb(img: Image.Image, rel: str, tconn) -> None:
    import io
    t = img.copy()
    t.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "WEBP", quality=75, method=4)
    tconn.execute("INSERT OR REPLACE INTO thumbs (id, data) VALUES (?, ?)",
                  (thumb_id(rel), buf.getvalue()))


# ---------- commands ----------

def cmd_init(_args):
    thumbs_db().close()
    with db(migrate=False) as conn:   # migrations assume the tables already exist
        conn.executescript(SCHEMA)
        conn.executescript(SHARE_SCHEMA)
    print(f"schema ready: {DB_PATH}")


def cmd_scan(args):
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"not a directory: {root}")
    exts = IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS

    conn = db()
    tconn = thumbs_db()
    known = {p: m for p, m in conn.execute("SELECT path, mtime FROM photos")}
    added = updated = skipped = failed = 0
    t0 = time.time()

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        key = str(path)
        try:
            st = path.stat()
        except OSError as e:
            print(f"  stat failed {path}: {e}", file=sys.stderr)
            failed += 1
            continue

        if key in known and abs(known[key] - st.st_mtime) < 1:
            skipped += 1
            continue
        is_new = key not in known

        try:
            is_video = path.suffix.lower() in VIDEO_EXTS
            duration = None
            if is_video:
                img, taken, lat, lon, duration = read_video(path)
            else:
                img = load_image(path)
                taken, lat, lon = read_exif(path)
            rel = thumb_rel(path)
            write_thumb(img, rel, tconn)
            w, h = img.size
            if taken is None:
                taken = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO photos (path, mtime, bytes, width, height, taken_at,
                                       lat, lon, thumb, state, updated_at, duration)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     mtime=excluded.mtime, bytes=excluded.bytes,
                     width=excluded.width, height=excluded.height,
                     taken_at=excluded.taken_at, lat=excluded.lat, lon=excluded.lon,
                     thumb=excluded.thumb, state=excluded.state, error=NULL,
                     updated_at=excluded.updated_at, duration=excluded.duration""",
                (key, st.st_mtime, st.st_size, w, h, taken, lat, lon, rel,
                 "done" if is_video else "pending", now(), duration),
            )
            # the file changed, so the model's tags describe an image that no longer
            # exists; manual ones (favourite, camera reel) are about the file, not its
            # pixels, and stay
            if not is_new:
                conn.execute("DELETE FROM tags WHERE path = ? AND source = 'model'",
                             (key,))
            added += is_new
            updated += not is_new
        except Exception as e:
            conn.execute(
                """INSERT INTO photos (path, mtime, bytes, state, error, updated_at)
                   VALUES (?,?,?, 'error', ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     state='error', error=excluded.error, updated_at=excluded.updated_at,
                     mtime=excluded.mtime""",   # else a broken file is retried every scan
                (key, st.st_mtime, st.st_size, f"{type(e).__name__}: {e}"[:400], now()),
            )
            failed += 1

        n = added + updated + failed
        # short transactions: the web server writes favourites into this database,
        # and 200 videos between commits held the write lock for ~50s
        if n % 20 == 0:
            tconn.commit()   # thumbnails first: a row must never outlive its thumb
            conn.commit()
        if n % 200 == 0:
            rate = n / max(time.time() - t0, 0.001)
            # flush: stdout is block-buffered when redirected, which makes a healthy
            # multi-hour run look hung for minutes at a time
            print(f"  {n} processed ({rate:.1f}/s, {skipped} unchanged, {failed} failed)",
                  flush=True)

    tconn.commit()
    tconn.close()
    conn.commit()
    resolve_places(conn)
    conn.commit()
    conn.close()
    print(f"scan done: +{added} new, {updated} changed, {skipped} unchanged, "
          f"{failed} failed, {time.time() - t0:.0f}s")

    # New files under a tagged folder must inherit its manual tag.
    conn2 = db()
    apply_tag_rules(conn2)
    conn2.close()

    # A scan that only ever adds leaves deleted files visible in the web UI
    # forever. Prune the same subtree that was just walked.
    if not args.no_prune:
        print("pruning deleted files...", flush=True)
        cmd_prune(argparse.Namespace(root=str(root), dry_run=False, force=False,
                                     max_fraction=0.25))


def resolve_places(conn, _unused=None):
    """Geocode every coordinate still missing a place name — read from the DB, not
    from this run's in-memory list, so an interrupted run leaves no orphans behind.
    Idempotent: safe to re-run any time.

    One batched lookup: reverse_geocoder is vectorised, and per-photo calls would
    redo the KD-tree query each time."""
    rows = conn.execute(
        "SELECT path, lat, lon FROM photos WHERE lat IS NOT NULL AND place IS NULL"
    ).fetchall()
    if not rows:
        return
    print(f"  reverse-geocoding {len(rows)} coordinates...", flush=True)
    try:
        import reverse_geocoder as rg
        hits = rg.search([(lat, lon) for _, lat, lon in rows])
    except Exception as e:
        print(f"  reverse geocode skipped ({e}); lat/lon still stored", file=sys.stderr)
        return
    conn.executemany(
        "UPDATE photos SET place = ? WHERE path = ?",
        [(f"{h.get('name','')}, {h.get('cc','')}".strip(", "), path)
         for (path, _, _), h in zip(rows, hits)],
    )


CAPTION_PROMPT = """Describe this image, then tag it for search.

First, one or two plain sentences naming what is actually visible.

Then 10-18 lowercase search tags. A waterfall in a forest at sunset should give: \
water, waterfall, forest, leaf, foliage, vegetation, sunset, golden hour, evening, \
warm light, nature, landscape. Every tag must be a separate idea. Never pad the list \
with variations on one word - "fashion humor", "fashion meme", "fashion contrast" is \
one tag, not three. Tag what the image is (photo, screenshot, meme, artwork, document, \
selfie), what is in it, where it is, and how it looks. Fewer good tags beat more \
repetitive ones. Never use words from these instructions as tags.

Reply with JSON only, no commentary:
{"description": "...", "tags": ["...", "..."]}"""

TAGS_ONLY_PROMPT = """Tag this image for search.

List 10-18 lowercase search tags. A waterfall in a forest at sunset should give: \
water, waterfall, forest, leaf, foliage, vegetation, sunset, golden hour, evening, \
warm light, nature, landscape. Every tag must be a separate idea. Never pad the list \
with variations on one word - "fashion humor", "fashion meme", "fashion contrast" is \
one tag, not three. Tag what the image is (photo, screenshot, meme, artwork, document, \
selfie), what is in it, where it is, and how it looks. Fewer good tags beat more \
repetitive ones. Never use words from these instructions as tags.

Reply with JSON only, no commentary:
{"tags": ["...", "..."]}"""

# Facet names the model echoes back from the prompt instead of describing the image.
# Useless as search facets (every photo has a "mood") and observed in the first run.
TAG_STOPWORDS = {
    "mood", "subject", "setting", "materials", "lighting", "dominant colors",
    "dominant colours", "colors", "colours", "tags", "description", "image",
    "kind of image", "search tags", "photo type", "image type",
}


MAX_TAGS = 18
MAX_PER_HEAD = 2


def clean_tags(raw) -> list[str]:
    """Normalise, then de-pad.

    Normalisation matters because the tags index is only as good as it: 'Sunset '
    and 'sunset' must not become two facets.

    De-padding matters because the model pads to fill the requested count with
    variations on one word -- 14 of 21 tags for one image were 'fashion <x>'.
    Asking it not to in the prompt did not work (small models follow negative
    instructions poorly), so cap each head word instead. Shortest-first keeps the
    general term ('mythical') and one specific ('mythical lion') while dropping
    eleven paraphrases of the same idea.
    """
    seen = []
    for t in raw if isinstance(raw, list) else []:
        if t is None or isinstance(t, bool):
            continue  # str(None) would otherwise index the literal tag "none"
        # hyphens and underscores to spaces: the model emits the same facet as
        # "black-and-white", "black and white" and "3d-rendering"/"3d rendering",
        # which exact-match tag search would treat as unrelated rows
        t = str(t).strip().lower().strip(".,;:#").replace("_", " ").replace("-", " ")
        t = " ".join(t.split())
        if 1 < len(t) <= 40 and t not in seen and t not in TAG_STOPWORDS:
            seen.append(t)

    kept, heads = [], {}
    for t in sorted(seen, key=len):  # shortest wins its head-word slots
        head = t.split()[0]
        if heads.get(head, 0) >= MAX_PER_HEAD:
            continue
        heads[head] = heads.get(head, 0) + 1
        kept.append(t)
    # restore the model's original ordering among survivors; it is roughly
    # salience-ordered, and the caller may show the first few as a summary
    return sorted(kept, key=seen.index)[:MAX_TAGS]


def parse_caption(text: str):
    """(description, tags). Models drop the occasional stray token around the JSON,
    so fall back to the outermost brace pair, then to salvaging a truncated array."""
    import json
    import re
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1]):
        if not candidate.strip():
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            desc = str(data.get("description", "")).strip()
            tags = clean_tags(data.get("tags"))
            if desc or tags:
                return desc, tags

    # Last resort: output truncated at max_new_tokens leaves a valid prefix of the
    # array. Observed at 0.11% of photos -- the model pretty-prints one tag per
    # indented line, which costs far more tokens than a compact array. Throwing
    # away 15 good tags because the 16th was cut mid-word is pure loss.
    m = re.search(r'"tags"\s*:\s*\[(.*)', text, re.S)
    if m:
        tags = clean_tags(re.findall(r'"([^"\n]{2,40})"', m.group(1)))
        if tags:
            d = re.search(r'"description"\s*:\s*"([^"]*)"', text)
            return (d.group(1).strip() if d else ""), tags
    raise ValueError(f"unparseable model output: {text[:200]!r}")


def load_vlm(model_id: str, four_bit: bool, max_pixels: int):
    """4-bit is ~1.4x slower per image than fp16 but uses 2.4GB instead of 7.2GB on an
    8GB card. That headroom is what allows batching, which is worth far more: measured
    17.2s/img at batch=1 vs 1.82s/img at batch=8."""
    import torch
    from transformers import AutoProcessor, AutoModelForImageTextToText
    kwargs = {"dtype": torch.float16, "device_map": "cuda:0"}
    if four_bit:
        from transformers import BitsAndBytesConfig
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)
    print(f"loading {model_id} ({'4-bit' if four_bit else 'fp16'}, "
          f"max_pixels={max_pixels})...", flush=True)
    model = AutoModelForImageTextToText.from_pretrained(model_id, **kwargs).eval()
    # padding_side=left is required for correct batched generation on a decoder model
    proc = AutoProcessor.from_pretrained(model_id, max_pixels=max_pixels,
                                         padding_side="left")
    return model, proc


def mark_error(conn, path, msg):
    conn.execute("UPDATE photos SET state='error', error=?, updated_at=? WHERE path=?",
                 (msg[:400], now(), path))


def cmd_caption(args):
    import torch
    model, proc = load_vlm(args.model, args.four_bit, args.max_pixels)
    # Descriptions are ~28% of generated tokens for ~1 extra searchable word per
    # photo (measured: 2.7 new terms, mostly "wearing"/"holding"/"atop"). Tags
    # already capture legible image text. Hence the option to skip them.
    prompt = TAGS_ONLY_PROMPT if args.tags_only else CAPTION_PROMPT
    print(f"prompt: {'tags only' if args.tags_only else 'describe + tag'}", flush=True)
    chat = proc.apply_chat_template(
        [{"role": "user", "content": [{"type": "image"},
                                      {"type": "text", "text": prompt}]}],
        add_generation_prompt=True)

    conn = db()
    rows = [r[0] for r in conn.execute(
        "SELECT path FROM photos WHERE state = 'pending' ORDER BY path"
        + (" LIMIT ?" if args.limit else ""),
        (args.limit,) if args.limit else ())]
    print(f"{len(rows)} pending, batch={args.batch}\n", flush=True)

    done = failed = 0
    consecutive_load_failures = 0
    batch = args.batch
    t0 = time.time()
    i = 0
    while i < len(rows):
        chunk = rows[i:i + batch]
        # Decode first: a file that won't open must not take its whole batch down.
        loaded = []
        for path in chunk:
            try:
                img = load_image(Path(path))
                img.thumbnail((CAPTION_PX, CAPTION_PX), Image.LANCZOS)
                loaded.append((path, img))
            except Exception as e:
                mark_error(conn, path, f"load: {type(e).__name__}: {e}")
                failed += 1
                consecutive_load_failures += 1
            else:
                consecutive_load_failures = 0
        i += len(chunk)

        # The photos live on an external USB drive. If it is unplugged or not yet
        # mounted (very likely right after a power cut, when a logon task fires
        # early), every load fails and we would permanently mark thousands of
        # good rows as 'error'. Stop instead, loudly, and leave them pending.
        if consecutive_load_failures >= 32:
            conn.commit()
            conn.close()
            sys.exit(f"aborting: {consecutive_load_failures} consecutive load "
                     f"failures - is the source drive connected? "
                     f"{done} captioned this run are saved.")
        if not loaded:
            continue

        try:
            inputs = proc(text=[chat] * len(loaded), images=[im for _, im in loaded],
                          padding=True, return_tensors="pt").to(model.device)
            with torch.inference_mode():
                out = model.generate(**inputs, max_new_tokens=args.max_new_tokens,
                                     do_sample=False)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if batch > 1:
                batch = max(1, batch // 2)
                i -= len(chunk)  # retry this chunk smaller
                print(f"  OOM -> batch={batch}, retrying", flush=True)
                continue
            for path, _ in loaded:
                mark_error(conn, path, "caption: CUDA OOM at batch=1")
                failed += 1
            conn.commit()
            continue

        prompt_len = inputs.input_ids.shape[1]
        for j, (path, _) in enumerate(loaded):
            text = proc.decode(out[j][prompt_len:], skip_special_tokens=True)
            try:
                desc, tags = parse_caption(text)
            except ValueError as e:
                mark_error(conn, path, f"caption: {e}")
                failed += 1
                continue
            conn.execute(
                "UPDATE photos SET description=?, state='done', error=NULL, updated_at=? "
                "WHERE path=?", (desc, now(), path))
            # only the model's own tags: a manual tag must survive re-captioning
            conn.execute("DELETE FROM tags WHERE path=? AND source='model'", (path,))
            conn.executemany(
                "INSERT OR IGNORE INTO tags (path, tag, source) VALUES (?,?, 'model')",
                [(path, t) for t in tags])
            done += 1

        conn.commit()
        rate = (done + failed) / max(time.time() - t0, 0.001)
        eta = (len(rows) - i) / rate / 3600
        print(f"  {i}/{len(rows)}  {rate:.2f} img/s  {failed} failed  ETA {eta:.1f}h",
              flush=True)

    conn.close()
    print(f"\ncaptioned {done}, failed {failed}, {(time.time() - t0)/60:.1f}min")


def cmd_tag(args):
    """Add or remove a manual tag across one or more path prefixes.

    Stored lowercase like every other tag: SQLite's `=` is case sensitive, so a
    'Camera Reel' row would be a different facet from 'camera reel' and would not
    match the tag search at all.
    """
    tag = " ".join(args.tag.strip().lower().split()).replace("-", " ")
    if not tag:
        sys.exit("empty tag")
    conn = db()
    total = 0
    for prefix in args.path:
        like = prefix.rstrip("\\") + "\\%"
        n = conn.execute("SELECT COUNT(*) FROM photos WHERE path LIKE ?",
                         (like,)).fetchone()[0]
        if not n:
            print(f"  0 photos under {prefix}  (check the path)")
            continue
        if args.remove:
            cur = conn.execute("DELETE FROM tags WHERE tag = ? AND path LIKE ?",
                               (tag, like))
            conn.execute("DELETE FROM tag_rules WHERE tag = ? AND prefix = ?",
                         (tag, prefix.rstrip("\\")))
            print(f"  -{cur.rowcount:<6} {prefix}")
            total += cur.rowcount
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tags (path, tag, source) "
                "SELECT path, ?, 'manual' FROM photos WHERE path LIKE ?",
                (tag, like))
            # remember it, so files added to this folder later inherit the tag
            if not args.once:
                conn.execute("INSERT OR IGNORE INTO tag_rules (tag, prefix) "
                             "VALUES (?,?)", (tag, prefix.rstrip("\\")))
            print(f"  +{cur.rowcount:<6} of {n} photos under {prefix}")
            total += cur.rowcount
    conn.commit()
    held = conn.execute("SELECT COUNT(*) FROM tags WHERE tag = ?", (tag,)).fetchone()[0]
    conn.close()
    verb = "removed from" if args.remove else "added to"
    print(f"'{tag}' {verb} {total} photos; now on {held} in total")


def cmd_prune(args):
    """Drop rows whose source file is gone, so deletions on disk reach the web UI.

    The dangerous case is an unplugged drive: every path then looks missing, and a
    naive prune would erase the entire index. So the drive roots are checked first,
    and a prune large enough to suggest a mount problem stops and asks.
    """
    conn = db()
    try:
        rows = conn.execute(
            "SELECT path, thumb FROM photos"
            + (" WHERE path LIKE ?" if args.root else ""),
            (args.root.rstrip("\\") + "\\%",) if args.root else ()).fetchall()
        if not rows:
            print("nothing to check")
            return

        # Every drive referenced must be reachable, or "missing" means nothing.
        # (db() here returns plain tuples -- no row_factory -- index positionally.)
        roots = {Path(path).anchor for path, _ in rows}
        unreachable = [r for r in roots if not Path(r).is_dir()]
        if unreachable:
            sys.exit(f"aborting: {', '.join(sorted(unreachable))} not accessible. "
                     f"Connect the drive before pruning - every file would look "
                     f"deleted.")

        missing = [(path, thumb) for path, thumb in rows if not Path(path).exists()]
        frac = len(missing) / len(rows)
        print(f"checked {len(rows)} rows: {len(missing)} missing ({frac:.1%})")
        if not missing:
            return

        if frac > args.max_fraction and not args.force:
            for p, _ in missing[:5]:
                print(f"    {p}")
            sys.exit(f"aborting: {frac:.1%} of rows would be removed, above the "
                     f"{args.max_fraction:.0%} safety limit. That usually means a "
                     f"half-mounted drive or the wrong --root. Re-run with --force "
                     f"if the deletion really was that large.")

        if args.dry_run:
            for p, _ in missing[:20]:
                print(f"  would remove {p}")
            print(f"dry run: {len(missing)} rows left untouched")
            return

        # tags cascade via ON DELETE CASCADE (foreign_keys is ON in db())
        conn.executemany("DELETE FROM photos WHERE path = ?",
                         [(p,) for p, _ in missing])
        conn.commit()
        tconn = thumbs_db()
        thumbs = tconn.executemany("DELETE FROM thumbs WHERE id = ?",
                                   [(thumb_id(rel),) for _, rel in missing if rel]
                                   ).rowcount
        tconn.commit()
        tconn.close()
        print(f"removed {len(missing)} rows and {thumbs} thumbnails")
    finally:
        # close on every path, including sys.exit: an open handle keeps the
        # database file locked on Windows
        conn.close()


def cmd_verify(args):
    """Find thumbnails that are missing or unreadable.

    The scan skips unchanged source files on mtime, so a lost thumbnail would
    never be rebuilt on its own. --fix clears mtime so the next scan rebuilds
    those rows.
    """
    import io
    conn = db()
    rows = conn.execute(
        "SELECT path, thumb FROM photos WHERE thumb IS NOT NULL").fetchall()
    tconn = thumbs_db()
    sizes = dict(tconn.execute("SELECT id, length(data) FROM thumbs"))
    missing, truncated = [], []
    for path, rel in rows:
        n = sizes.get(thumb_id(rel))
        if n is None:
            missing.append(path)
            continue
        # Size alone is not evidence: a 2-colour avatar compresses to 66 bytes
        # and is perfectly valid. 53 such files tripped a <200B threshold.
        # Only decode the suspicious ones, so the check stays fast.
        if n < 200:
            data = tconn.execute("SELECT data FROM thumbs WHERE id = ?",
                                 (thumb_id(rel),)).fetchone()[0]
            try:
                with Image.open(io.BytesIO(data)) as im:
                    im.verify()
            except Exception:
                truncated.append(path)
    tconn.close()

    print(f"checked {len(rows)} thumbnails")
    print(f"  missing   {len(missing)}")
    print(f"  truncated {len(truncated)}")
    for p in (missing + truncated)[:10]:
        print(f"    {p}")

    bad = missing + truncated
    if bad and args.fix:
        conn.executemany("UPDATE photos SET mtime = 0 WHERE path = ?",
                         [(p,) for p in bad])
        conn.commit()
        print(f"\ncleared mtime on {len(bad)} rows - rerun scan to rebuild them")
    elif bad:
        print("\nrerun with --fix to mark them for rebuild")
    conn.close()


def cmd_stats(_args):
    conn = db()
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    print(f"photos        {q('SELECT COUNT(*) FROM photos')}")
    for state in ("pending", "done", "error"):
        print(f"  {state:<11} {q('SELECT COUNT(*) FROM photos WHERE state=?', state)}")
    print(f"with GPS      {q('SELECT COUNT(*) FROM photos WHERE lat IS NOT NULL')}")
    print(f"with place    {q('SELECT COUNT(*) FROM photos WHERE place IS NOT NULL')}")
    print(f"tag rows      {q('SELECT COUNT(*) FROM tags')}")
    print(f"unique tags   {q('SELECT COUNT(DISTINCT tag) FROM tags')}")
    top = conn.execute(
        "SELECT tag, COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC LIMIT 15").fetchall()
    if top:
        print("top tags      " + ", ".join(f"{t}({c})" for t, c in top))
    rng = conn.execute(
        "SELECT MIN(taken_at), MAX(taken_at) FROM photos WHERE taken_at IS NOT NULL").fetchone()
    if rng[0]:
        print(f"date range    {rng[0]} .. {rng[1]}")
    conn.close()


def cmd_search(args):
    """Tag + time + place, combined. Every filter here is index-backed."""
    conn = db()
    sql = ["SELECT p.path, p.taken_at, p.place, p.thumb FROM photos p"]
    params = []
    if args.tag:
        sql.append("JOIN tags t ON t.path = p.path")
    where = ["p.state = 'done'"]
    if args.tag:
        where.append(f"t.tag IN ({','.join('?' * len(args.tag))})")
        params += [t.lower() for t in args.tag]
    if args.since:
        where.append("p.taken_at >= ?"); params.append(args.since)
    if args.until:
        where.append("p.taken_at <= ?"); params.append(args.until)
    if args.place:
        where.append("p.place LIKE ?"); params.append(f"%{args.place}%")
    sql.append("WHERE " + " AND ".join(where))
    if args.tag and args.match_all:
        # every requested tag must be present, not just any one of them
        sql.append("GROUP BY p.path HAVING COUNT(DISTINCT t.tag) = ?")
        params.append(len(args.tag))
    elif args.tag:
        sql.append("GROUP BY p.path")
    # whitelist, never interpolate a user string into ORDER BY
    orders = {"newest": "p.taken_at DESC", "oldest": "p.taken_at ASC",
              "largest": "p.bytes DESC, p.taken_at DESC",
              "widest": "p.width DESC, p.taken_at DESC",
              "place": "p.place IS NULL, p.place ASC, p.taken_at DESC",
              "random": "RANDOM()"}
    sql.append(f"ORDER BY {orders[args.sort]} LIMIT ?")
    params.append(args.limit)

    rows = conn.execute(" ".join(sql), params).fetchall()
    print(f"{len(rows)} match\n")
    for path, taken, place, thumb in rows:
        print(f"{taken}  {place or '-':<22}  {thumb}  {path}")
    conn.close()


def cmd_similar(args):
    """Ranked by shared tags — 'group similar images' without embeddings."""
    conn = db()
    rows = conn.execute(
        """SELECT t2.path, COUNT(*) shared, p.taken_at FROM tags t1
           JOIN tags t2 ON t2.tag = t1.tag AND t2.path <> t1.path
           JOIN photos p ON p.path = t2.path
           WHERE t1.path = ? GROUP BY t2.path ORDER BY shared DESC, p.taken_at DESC
           LIMIT ?""", (args.path, args.limit)).fetchall()
    if not rows:
        print("no overlap — is that path captioned yet? (state must be 'done')")
    for path, shared, taken in rows:
        print(f"{shared:>3} shared  {taken}  {path}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("scan"); p.add_argument("root")
    p.add_argument("--no-prune", action="store_true",
                   help="skip removing rows for files deleted on disk")
    p.set_defaults(fn=cmd_scan)
    c = sub.add_parser("caption")
    c.add_argument("--limit", type=int, default=0, help="0 = every pending row")
    c.add_argument("--model", default="Qwen/Qwen2.5-VL-3B-Instruct")
    c.add_argument("--4bit", dest="four_bit", action="store_true", default=True)
    c.add_argument("--fp16", dest="four_bit", action="store_false",
                   help="faster per image, but 7.2GB leaves no room to batch")
    # 16 measured fastest on an 8GB 3060 Ti (1.05s/img). Larger is slower, not OOM:
    # batched generation waits for the longest sequence, so one verbose outlier
    # stalls the whole batch. 32 -> 1.27s/img, 48 -> 2.88s/img.
    c.add_argument("--batch", type=int, default=16)
    c.add_argument("--tags-only", action="store_true",
                   help="skip prose descriptions: ~14h instead of ~22h for 54k")
    c.add_argument("--max-pixels", type=int, default=512 * 512)
    # 300 truncated ~0.11% of outputs: the model pretty-prints one tag per
    # indented line. Generation stops at EOS anyway, so a higher ceiling costs
    # nothing for the photos that finish normally.
    c.add_argument("--max-new-tokens", type=int, default=420)
    c.set_defaults(fn=cmd_caption)
    s = sub.add_parser("search")
    s.add_argument("--tag", action="append", help="repeatable")
    s.add_argument("--match-all", action="store_true", help="require every --tag")
    s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--place"); s.add_argument("--limit", type=int, default=50)
    s.add_argument("--sort", default="newest",
                   choices=["newest", "oldest", "largest", "widest", "place", "random"])
    s.set_defaults(fn=cmd_search)
    m = sub.add_parser("similar")
    m.add_argument("path"); m.add_argument("--limit", type=int, default=20)
    m.set_defaults(fn=cmd_similar)
    # machine-readable single number, for the restart wrapper to branch on
    sub.add_parser("pending").set_defaults(
        fn=lambda _a: print(db().execute(
            "SELECT COUNT(*) FROM photos WHERE state='pending'").fetchone()[0]))
    tg = sub.add_parser("tag", help="add or remove a manual tag by path prefix")
    tg.add_argument("tag")
    tg.add_argument("--path", action="append", required=True, help="repeatable prefix")
    tg.add_argument("--remove", action="store_true")
    tg.add_argument("--once", action="store_true",
                    help="do not remember the rule for future files")
    tg.set_defaults(fn=cmd_tag)
    sub.add_parser("retag", help="re-apply all remembered tag rules").set_defaults(
        fn=lambda _a: (lambda c: (apply_tag_rules(c), c.close()))(db()))
    pr = sub.add_parser("prune", help="remove rows for files deleted on disk")
    pr.add_argument("--root", help="limit to one subtree (default: whole index)")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--force", action="store_true",
                    help="allow a prune above the safety limit")
    pr.add_argument("--max-fraction", type=float, default=0.25)
    pr.set_defaults(fn=cmd_prune)
    v = sub.add_parser("verify")
    v.add_argument("--fix", action="store_true", help="mark bad thumbs for rebuild")
    v.set_defaults(fn=cmd_verify)
    sub.add_parser("geocode").set_defaults(
        fn=lambda _a: (lambda c: (resolve_places(c), c.commit(), c.close()))(db()))
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
