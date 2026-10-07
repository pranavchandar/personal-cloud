"""Inspection/maintenance helper: python q.py "<sql>"

Commits, and handles statements that return no rows. The first version did neither,
which made UPDATE/DELETE silently no-op (sqlite3 does not autocommit DML) while
crashing on cur.description -- a failure that invalidated a verification step.
"""
import sqlite3
import sys

conn = sqlite3.connect("photos.db")
sql = sys.argv[1] if len(sys.argv) > 1 else \
    "SELECT path, error FROM photos WHERE state='error'"
cur = conn.execute(sql)

if cur.description is None:          # INSERT / UPDATE / DELETE
    conn.commit()
    print(f"ok, {cur.rowcount} row(s) affected")
else:
    print(" | ".join(d[0] for d in cur.description))
    for row in cur:
        print(" | ".join("" if v is None else str(v)[:120] for v in row))
conn.close()
