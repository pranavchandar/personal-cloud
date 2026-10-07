"""Do manual tags survive a re-caption?

Runs the exact statements cmd_caption uses -- source-scoped DELETE then INSERT of
model tags -- against a throwaway database. The real library cannot prove this yet:
the captioner has not reached the tagged folders since the tag was applied, so
"the rows are still there" says nothing about whether re-captioning erases them.
"""
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


with tempfile.TemporaryDirectory() as d:
    pi.DB_PATH = Path(d) / "t.db"
    conn = sqlite3.connect(pi.DB_PATH)
    conn.executescript(pi.SCHEMA)
    conn.execute("INSERT INTO photos (path, mtime, bytes, state, updated_at) "
                 "VALUES ('P', 1, 1, 'done', 'now')")
    # one curated tag, three from an earlier caption
    conn.execute("INSERT INTO tags (path, tag, source) VALUES ('P','camera reel','manual')")
    for t in ("sky", "clouds", "aerial"):
        conn.execute("INSERT INTO tags (path, tag, source) VALUES ('P',?, 'model')", (t,))
    conn.commit()

    def tags(source=None):
        q = "SELECT tag FROM tags WHERE path='P'"
        if source:
            q += f" AND source='{source}'"
        return sorted(r[0] for r in conn.execute(q))

    print("before re-caption:", tags())
    check("starts with 1 manual + 3 model", tags("manual") == ["camera reel"]
          and len(tags("model")) == 3)

    # --- exactly what cmd_caption does on a re-caption ---
    conn.execute("DELETE FROM tags WHERE path=? AND source='model'", ("P",))
    conn.executemany(
        "INSERT OR IGNORE INTO tags (path, tag, source) VALUES (?,?, 'model')",
        [("P", t) for t in ("sunset", "horizon")])
    conn.commit()

    print("after  re-caption:", tags())
    check("manual tag survived", "camera reel" in tags())
    check("old model tags replaced", "sky" not in tags() and "clouds" not in tags())
    check("new model tags present", tags("model") == ["horizon", "sunset"])
    check("manual still marked manual", tags("manual") == ["camera reel"])

    # --- the old, unscoped statement must be shown to destroy it ---
    conn.execute("DELETE FROM tags WHERE path=?", ("P",))
    conn.commit()
    check("unscoped delete would have erased it (why the fix matters)",
          tags() == [])
    conn.close()

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
