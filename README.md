# Photo Index: a private Google Photos that runs on your own PC

A self-hosted photo and video library. It indexes a 65,000-item phone backup on a
home PC, tags every photo with an AI model running locally on the graphics card,
and serves a searchable thumbnail grid you can open from your phone anywhere in
the world. No cloud upload, no subscription, no API costs.

---

## 1. At a glance

| | |
|---|---|
| Library | phone backups, a DJI drone and an iPhone on an external drive (`library_root` in `config.json`) |
| Indexed | **55,485 photos** and **9,486 videos** |
| AI tags | 705,619 tags (69,948 distinct), generated locally |
| Places | 18,381 items with GPS, 140 distinct places |
| Thumbnails | 65,103, in one 0.6 GB file (`thumbs.db`) |
| Database | one SQLite file, `photos.db` (~380 MB) |
| Cost | ₹0: open-source model on an RTX 3060 Ti, free Tailscale plan |
| Web address | **http://&lt;tailscale-ip&gt;:8765**, reachable only from your own Tailscale devices |

---

## Setup (first time)

**You need:** Windows 10/11, an NVIDIA GPU with 8 GB or more of VRAM (for
tagging), Python 3.12+, and [Tailscale](https://tailscale.com) installed on the PC
and on every device you'll browse from.

```powershell
git clone https://github.com/pranavchandar/personal-cloud.git photo-index
cd photo-index
python -m venv venv
.\venv\Scripts\pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
.\venv\Scripts\pip install -r requirements.txt
```

**Configure:** copy `config.example.json` to `config.json` and edit it.
`config.json` holds your personal paths and is never committed.

| key | meaning |
|---|---|
| `library_root` | the folder holding your photos and videos |
| `locked_prefix` | a folder to hide behind a password (delete the key for none) |
| `locked_password_sha256` | the password's SHA-256; generate it with the command below |

```powershell
.\venv\Scripts\python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

**Download the AI model** (Qwen2.5-VL-3B, ~7 GB). Tagging looks for it in
`hf-cache` on the same drive as the project, so download it there:

```powershell
$env:HF_HOME = (Split-Path (Get-Location) -Qualifier) + '\hf-cache'
.\venv\Scripts\python fetch_model.py
```

**Create the index and install the automatic tasks:**

```powershell
.\venv\Scripts\python photoindex.py init
tailscale up                          # sign in once
.\serve.ps1 -Register                 # web server, at every Windows sign-in
.\sync.ps1 -Register                  # scan + tag, every day at 03:00
.\run_captioning.ps1 -Register -TagsOnly   # resume tagging after a power cut
```

Then double-click **`Update Library.cmd`** for the first full index (expect hours
for a large library, at about 1 s per photo), and open
`http://<tailscale-ip>:8765` (`tailscale ip -4` prints the IP).

**Tests**, which need no GPU or network: `python test_photoindex.py`,
`python test_prune.py`, `python test_manual_tags.py`, `node test_periods.js`.
`test_lock.py` checks the locked folder against the running server.

---

## 2. The problem and the constraints

**Goal:** browse and search years of phone backups from anywhere, the way Google
Photos does, but without handing the photos to a cloud service.

**Constraints that shaped every decision:**

- **Private.** Photos never leave the PC. No cloud storage, no third-party AI API.
- **Free.** No paid API or subscription, so the AI has to run on local hardware.
- **Reachable from anywhere.** The home connection sits behind NAT/CGNAT, so
  port forwarding is not an option. Tailscale provides a private network instead.
- **Low maintenance.** It should keep itself up to date and survive power cuts.

---

## 3. How it works

```
   PHONE BACKUPS (USB disk)                      THIS PC (project folder)
 ┌──────────────────────────┐   1. SCAN     ┌──────────────────────────────┐
 │ JPG HEIC PNG DNG NEF ... │ ────────────▶ │ thumbs.db 320px previews     │
 │ MP4 MOV 3GP ...          │  thumbnail,   │ photos.db                    │
 └──────────────────────────┘  date, GPS,   │   photos: date, place, size, │
                               place name   │           duration, state    │
                                            │   tags:   AI + manual tags   │
                                  2. TAG    │                              │
              local AI model  ◀──────────── │  photos waiting for tags     │
              Qwen2.5-VL-3B   ────────────▶ │  10-18 search tags per photo │
              (RTX 3060 Ti)                 └──────────────┬───────────────┘
                                                           │ 3. SERVE
                                                           ▼
                                            ┌──────────────────────────────┐
                                            │ web server (port 8765)       │
                                            │ bound to the Tailscale IP    │
                                            └──────────────┬───────────────┘
                                                           │ encrypted
                                                           │ Tailscale tunnel
                                            ┌──────────────▼───────────────┐
                                            │ your phone / laptop browser  │
                                            └──────────────────────────────┘
```

### Step 1: Scan (`photoindex.py scan`)

It walks the backup folder and, for every photo or video:

- makes a small thumbnail (320 px WebP), stored in `thumbs.db`
- reads the **date taken** (EXIF for photos, recording time for videos, file
  date as a fallback)
- reads **GPS** and turns it into a place name ("Perungudi, IN") **offline**
  with a bundled city database
- for videos, also records the **duration** and grabs the first frame as the
  thumbnail

Files that haven't changed since the last scan are skipped, so a re-scan of the
whole library takes about a minute. The scan also **removes deleted files** from
the index, with a safety check: if the drive is unplugged it refuses to act,
rather than treating everything as deleted.

### Step 2: Tag (`photoindex.py caption`)

Each new photo goes to **Qwen2.5-VL-3B**, a vision-language model running on the
graphics card (4-bit, batches of 16, about 1 second per photo). It returns 10-18
search tags such as `beach, sunset, people, smiling, outdoor`. Readable text in
the image (signs, captions) also ends up as tags.

- Tags are cleaned up: lower-cased, duplicates and near-duplicates removed.
- **Videos are not tagged.** They're searchable by date and place only.
- **Manual tags** (like `camera reel` and `favourite`) are stored separately from
  AI tags, so re-tagging a photo never erases them.

### Step 3: Serve (`server.py`)

A small Python web server (FastAPI) shows the grid. It only listens on the PC's
**Tailscale address**, so only devices signed in to your Tailscale account can
reach it. Full-size photos are converted to JPEG on the fly, which is how RAW
(DNG/NEF) and HEIC files display in any browser. Videos are streamed with
seeking support.

### Every photo has a state

| state | meaning | visible in the web page? |
|---|---|---|
| `pending` | scanned, waiting for AI tags | no |
| `done` | ready (videos go straight here) | yes |
| `error` | file couldn't be read (corrupt, incomplete download) | no |

---

## 4. Features

| Feature | How it works |
|---|---|
| **Tag search** | Type a word ("beach") and it matches any tag containing it. |
| **Tag chips** | Tap one or more chips. With several, a photo must have **all** of them. |
| **Date range** | A range calendar with month dropdown and presets (Today … Last year). |
| **Places** | A searchable checklist, **several places at once**, plus a 🌍 **Map** where you tap pins to pick them. Each filter has its own × to clear it. |
| **Clickable details** | In the full view, tap the **date** to see that day/week/month/year, or the **place** to see everything from there. |
| **📍 Go to location** | In the full view, opens Google Maps at the exact GPS point where the photo or video was taken. Only shown for items with GPS. |
| **Month headers** | When sorted by date, the grid is split under headings like "September 2026". |
| **Zoom (− / +)** | The bar at the bottom switches between **All items**, **Weeks**, **Months** and **Years**. Each collection shows its newest item as the cover, a date/month/year sticker and an item count. Tap one to zoom in to just that period. All filters still apply. |
| **Sort** | Newest (default), oldest, largest, highest resolution, place A-Z, shuffle. |
| **Timelapse stacks** | RAW frames in one folder collapse into a single square with a count. Tap to see all frames. |
| **Locked folder** | One folder (`locked_prefix` in `config.json`) is hidden behind a password. Unlocking lasts 6 hours. |
| **Favourites** | ♡ in the full view. Favourites show a ♥ on their square and a `favourite` chip filters to them. |
| **Camera reel** | ★ chip: everything from the camera folders (DJI, OnePlus camera, Pixel camera, iPhone camera, photos19drive). New files there get it automatically. |
| **Videos** | The square shows the length (e.g. `1:28`). Videos of 250 MB or more carry a red size sticker, since they're slow on mobile data. |
| **Next / previous** | In the full view, tap the right side of the media for the next item and the left side for the previous one. |
| **Similar** | Finds photos sharing the most tags with the current one (photos only). |
| **Download original** | The untouched file: RAW, HEIC, video. |
| **Folders (sharing)** | ☑ **Select** items and add them to a named 📁 folder, then download the folder as one **zip** to send by WhatsApp, Drive or email. Folders are virtual: nothing on disk moves, and deleting a folder never deletes photos. |

---

## 5. What runs automatically

Three Windows scheduled tasks handle everything. You normally never touch them.

| Task | When | What it does |
|---|---|---|
| `PhotoIndexServer` | when you sign in to Windows | starts the web server |
| `PhotoIndexSync` | every day at **03:00** (or at next boot if the PC was off) | scans for new, changed and deleted files, **then tags new photos** |
| `PhotoIndexCaptioning` | when you sign in to Windows | finishes any tagging that a power cut interrupted |

Built-in safeguards:

- **Power cut mid-tagging:** progress is saved every 16 photos and the task
  resumes after you sign in.
- **Drive not plugged in yet:** tagging waits up to 10 minutes for the G: drive,
  and the sync refuses to run without it.
- **Two tagging runs at once** (manual run plus a scheduled one): the second
  sees the first and exits. Only one uses the GPU.
- **Broken files** are recorded once and skipped afterwards, not retried every
  night.

> **Note:** these tasks run **after you sign in to Windows**. A PC sitting at the
> lock screen after a restart won't start the server.

---

## 6. User guide

### 6.1 Opening the library

**One-time setup on a new phone or laptop:**

1. Install the **Tailscale** app and sign in with the **same account** as the PC.
2. Open **http://&lt;tailscale-ip&gt;:8765** in the browser. Running `tailscale ip -4`
   on the PC prints the IP.
3. Optional: add it to your home screen so it opens like an app.

**Every time:** the PC must be switched on, signed in, and connected to the
internet. The G: drive must be plugged in for full-size photos and videos
(thumbnails work without it).

### 6.2 Finding things

- **By content:** type in the search box (`waterfall`) or tap a tag chip.
  Combine chips to narrow down (`beach` + `sunset`).
- **By date:** tap the date box. Tap one day and close the calendar for that
  day, or tap a start day then an end day for a range. Use the month dropdown
  and year arrows to jump, or a preset (Today, This week, This month, Last 30
  days, This year, Last year). You can also open any photo and tap its date to
  choose that day, week, month or year.
- **By place:** tap **📍 All places** to get a checklist, where you can type to
  filter and tick **as many places as you like**. Or tap 🌍 **Map**, tap pins
  (they turn orange), then **Show photos**. **Back** on the map discards new
  picks. You can also tap the place under an open photo.
- **By time, visually:** tap **−** at the bottom to zoom out to weeks, then
  months, then years. Tap a year to see its months, a month to see its weeks,
  and a week to see its photos. **+** zooms back in, and **Clear** resets the
  dates.
- **Favourites:** tap the `favourite` chip.
- **Clear one filter:** each field (search, dates, places) shows its own **×**
  when it has a value, and clearing it keeps the other filters.
- **Start over:** tap **Clear** to reset everything.

### 6.3 Viewing

- Tap a square to open it. Tap the **right side** for the next item and the
  **left side** for the previous one. **← Back** returns to the grid.
- For videos, the middle of the screen and the control bar work as usual
  (play, pause, seek).
- Squares with a stack of sheets and a number are **timelapse sequences**. Tap
  to open all their frames.
- The **Locked** square asks for the password. **Lock again** hides it
  immediately.

### 6.4 Adding new photos and videos

1. Copy the files anywhere inside your library folder (`library_root`; new
   subfolders are fine).
2. Either **wait**: the 03:00 sync picks them up overnight. Or, to see them
   **now**, double-click **`Update Library.cmd`** in the project folder and leave the
   window open until it says **Done**.
3. Reload the web page.

Videos appear as soon as the scan finishes. Photos appear once tagged, at about
1 second per photo (1,000 photos ≈ 20 minutes). A HEIC scan is slower, at about
2 files per second.

### 6.5 Sharing photos with someone

1. Tap **☑ Select**, then tap the photos and videos you want. A ✓ appears on
   each; tap again to untick. Use search, dates or places first to find them.
   **Selecting many at once:**
   - **Range:** tick the first item, then **Shift+click** the last one (on a
     computer) or **press and hold** it (on a phone). Everything in between is
     selected.
   - **Whole month:** tap a month heading ("October 2026") to select all of
     that month, and tap it again to untick them all.
2. Tap **📁 Add to folder**, and either pick an existing folder or type a name
   and tap **Create & add**.
3. Tap **⬇ Download zip** in the confirmation, or later from **📁 Folders**.
4. Send the zip however you like (WhatsApp, Google Drive, email…).

- **📁 Folders** lists every folder with its size. You can **Open** one to view
  it, **⬇ Zip** it, or **🗑** delete it. Deleting removes only the folder,
  never the photos.
- Inside an open folder, **☑ Select** offers **Remove from folder**.
- The zip contains the **original files**, unchanged, which means **photos keep
  their GPS location** in their metadata. Leave out anything whose location you
  don't want to share.
- Locked items can only be added while the Locked folder is unlocked.
- Big folders make big zips (videos especially), so check the size shown
  before downloading on mobile data.

### 6.6 Deleting

Delete the files from your library folder. They disappear from the
web page after the next sync (overnight, or run **Update Library.cmd**).

### 6.7 Tagging a whole folder by hand (advanced)

Open PowerShell in the project folder:

```powershell
# tag everything under a folder, and remember it so future files get it too
.\venv\Scripts\python.exe photoindex.py tag "holiday 2024" --path "D:\Photos\Holiday 2024"

# remove the tag and forget the rule
.\venv\Scripts\python.exe photoindex.py tag "holiday 2024" --path "D:\Photos\Holiday 2024" --remove
```

Add `--once` to tag only the files that exist now. Manual tags show as gold
chips at the front of the list.

---

## 7. Troubleshooting

| Symptom | What to do |
|---|---|
| Page won't load | Check that the PC is on and signed in, and that Tailscale is connected on **both** devices. Then double-click **`Restart Server.cmd`**, which prints the address when the server is back. |
| New photos don't show | Run **Update Library.cmd**. Photos appear only after tagging. Videos should be there right after the scan. |
| Thumbnails load but full photos or videos don't | The G: drive is unplugged or asleep. Plug it in. |
| A video won't play | Most phone videos are HEVC. Safari, and Chrome/Edge with hardware support, play them; Firefox doesn't. Use **Download original**. |
| Video playback stutters on mobile data | Look for the red size sticker. Large videos stream at full quality; download it on Wi-Fi instead. |
| Locked folder asks for the password again | Unlocks expire after 6 hours and whenever the server restarts. |
| A file never appears | It may be corrupt: an unfinished recording, an incomplete download, or a photo the AI couldn't read. See the logs below. |

**Logs**, all in the project folder:

| File | Contains |
|---|---|
| `sync.log` | every scan: files added, changed, failed and pruned |
| `captioning.log` | every tagging run: how many were waiting, how many were done |
| `server.err` | web server errors |

---

## 8. Privacy and security

- Photos, thumbnails, tags and the AI model all stay on this PC. After the
  one-time model download, tagging runs fully offline.
- The server listens only on the Tailscale address, never on the public
  internet or the home Wi-Fi. Tailscale's own login is the access control.
- Photos are requested by a random-looking ID, never by file path, so the web
  page can't be used to read other files on the PC.
- Locked items are blocked at every level: thumbnails, full images, downloads
  and search, not just hidden from the grid. The password is stored only as a
  hash.
- **One thing does go online:** the 🌍 map's background tiles load from
  OpenStreetMap. Only map images are fetched; no photo data is sent.

---

## 9. Known limits

| Limit | Why / what would fix it |
|---|---|
| Tagging needs the GPU | ~1 s per photo on the RTX 3060 Ti. A large import takes hours, so let it run overnight. |
| Videos have no content tags | By choice: date and place are enough for them. |
| Text in screenshots and documents is only partly searchable | The AI sees a reduced image. OCR (Tesseract) would be the fix. |
| Only ~28% of items have a location | Many sources (WhatsApp, screenshots, DJI videos) carry no GPS. |
| Videos aren't converted | Originals are streamed as-is. Some formats won't play in some browsers, and large files are slow on mobile data. |
| Starts only after Windows sign-in | The tasks are logon-triggered. |
| 178 unreadable files | Corrupt or incomplete files. They're kept in the database as `error` and hidden. |

---

## 10. Project files

| File | Purpose |
|---|---|
| **`Update Library.cmd`** | double-click: scan and tag now |
| **`Restart Server.cmd`** | double-click: restart the web server |
| `photoindex.py` | scanner, AI tagger and command-line tools (`scan`, `caption`, `tag`, `retag`, `prune`, `verify`, `stats`, `search`) |
| `server.py` | web server and the whole web page |
| `sync.ps1` | scan + tag; `-Register` installs the 03:00 task |
| `run_captioning.ps1` | tagging with auto-retry; `-Register` installs the sign-in task |
| `serve.ps1` | starts the server; `-Register` installs the sign-in task |
| `config.json` | your paths and locked-folder password hash. Not in git; start from `config.example.json` |
| `requirements.txt` | Python packages (install PyTorch first, as in Setup) |
| `fetch_model.py` | downloads the AI model with timeouts and resume |
| `q.py` | run a SQL statement against the index: `python q.py "<sql>"` |
| `photos.db` | the index (SQLite) |
| `thumbs.db` | every thumbnail, in one file. On an exFAT drive with 1 MB clusters, 65k loose thumbnail files used 63.6 GB of disk; as one file they use 0.6 GB |
| `static\` | the map library (Leaflet), served locally |
| `venv\` | the Python environment |
| `test_*.py`, `test_periods.js` | self-checks (locked-folder protection, manual tags, prune safety, date maths) |

**Tech stack:** Python 3, FastAPI + Uvicorn, SQLite, Pillow (+ pillow-heif,
rawpy), PyAV (bundled ffmpeg) for video, Hugging Face Transformers +
bitsandbytes (Qwen2.5-VL-3B-Instruct, 4-bit), reverse_geocoder, Leaflet +
OpenStreetMap, Tailscale, Windows Task Scheduler.
