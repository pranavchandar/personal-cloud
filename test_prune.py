"""Does prune refuse to wipe the index when the drive is missing?

That is the failure mode that matters: with G: unplugged every path looks deleted,
and an unguarded prune would erase all 55,000 rows. Tested against a throwaway
database rather than the real one.

    python test_prune.py
"""

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

import photoindex as pi

fails = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


def build(tmp: Path, real: int, ghost: int, ghost_drive="Q:\\gone"):
    """A DB with `real` existing files and `ghost` files that do not exist."""
    pi.DB_PATH = tmp / "t.db"
    pi.THUMB_DB = tmp / "thumbs.db"
    conn = sqlite3.connect(pi.DB_PATH)
    conn.executescript(pi.SCHEMA)
    tconn = pi.thumbs_db()
    src = tmp / "src"
    src.mkdir(exist_ok=True)
    for i in range(real):
        f = src / f"real{i}.jpg"
        f.write_bytes(b"x")
        rel = f"aa/real{i}.webp"
        tconn.execute("INSERT INTO thumbs VALUES (?, x'00')", (pi.thumb_id(rel),))
        conn.execute("INSERT INTO photos (path, mtime, bytes, thumb, state, updated_at)"
                     " VALUES (?,1,1,?, 'done', 'now')", (str(f), rel))
        conn.execute("INSERT INTO tags (path, tag) VALUES (?, 'sometag')", (str(f),))
    for i in range(ghost):
        p = f"{ghost_drive}\\ghost{i}.jpg"
        rel = f"bb/ghost{i}.webp"
        tconn.execute("INSERT INTO thumbs VALUES (?, x'00')", (pi.thumb_id(rel),))
        conn.execute("INSERT INTO photos (path, mtime, bytes, thumb, state, updated_at)"
                     " VALUES (?,1,1,?, 'done', 'now')", (p, rel))
        conn.execute("INSERT INTO tags (path, tag) VALUES (?, 'sometag')", (p,))
    tconn.commit()
    tconn.close()
    conn.commit()
    conn.close()


def thumbs(prefix):
    tconn = pi.thumbs_db()
    n = tconn.execute("SELECT COUNT(*) FROM thumbs WHERE id LIKE ?",
                      (prefix + "%",)).fetchone()[0]
    tconn.close()
    return n


def count():
    conn = sqlite3.connect(pi.DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    t = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    conn.close()
    return n, t


def run(**kw):
    opts = dict(root=None, dry_run=False, force=False, max_fraction=0.25)
    opts.update(kw)                      # merge, don't pass a key twice
    args = argparse.Namespace(**opts)
    try:
        pi.cmd_prune(args)
        return None
    except SystemExit as e:
        return str(e)


print("=== unreachable drive must abort, deleting nothing ===")
with tempfile.TemporaryDirectory() as d:
    build(Path(d), real=0, ghost=10)      # every row on a drive that isn't there
    before = count()
    msg = run()
    check("aborted", msg is not None and "not accessible" in (msg or ""))
    check("named the drive", "Q:" in (msg or ""))
    check("deleted nothing", count() == before)

print("\n=== an implausibly large prune must stop and ask ===")
with tempfile.TemporaryDirectory() as d:
    # ghosts on a drive that DOES exist, so the first guard passes and the
    # fraction guard is the one under test
    build(Path(d), real=2, ghost=18, ghost_drive=str(Path(d) / "vanished"))
    before = count()
    msg = run()
    check("aborted at 90% missing", msg is not None and "safety limit" in (msg or ""))
    check("deleted nothing", count() == before)
    print("  (with --force)")
    run(force=True)
    n, t = count()
    check("force actually prunes", n == 2)
    check("tags cascaded", t == 2)

print("\n=== a normal small deletion proceeds ===")
with tempfile.TemporaryDirectory() as d:
    build(Path(d), real=20, ghost=2, ghost_drive=str(Path(d) / "vanished"))
    msg = run()
    n, t = count()
    check("no abort", msg is None)
    check("2 rows removed", n == 20)
    check("2 tag rows cascaded", t == 20)
    check("ghost thumbnails deleted", thumbs("ghost") == 0)
    check("real thumbnails untouched", thumbs("real") == 20)

print("\n=== dry run changes nothing ===")
with tempfile.TemporaryDirectory() as d:
    build(Path(d), real=10, ghost=2, ghost_drive=str(Path(d) / "vanished"))
    before = count()
    run(dry_run=True)
    check("rows intact", count() == before)
    check("thumbnails intact", thumbs("ghost") == 2)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
