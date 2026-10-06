"""Public/admin queries. ONE visibility rule is shared by lists, search
snippets, share cards and exports:

    public content == notes.visibility = 'published'
    private / pending / archived content never leaks to unauthenticated output.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Optional

from . import repository as repo

PUBLIC_NOTE = "n.visibility='published'"
PAGE_SIZE_DEFAULT = 20


# ---------------------------------------------------------------- pagination

def encode_cursor(published_at: str, note_id: int) -> str:
    raw = json.dumps([published_at, note_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Optional[tuple[str, int]]:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, nid = json.loads(base64.urlsafe_b64decode(cursor + pad))
        return ts, int(nid)
    except Exception:
        return None


def _public_note_columns(alias: str = "n") -> str:
    return (f"{alias}.id, {alias}.work_id, {alias}.event_id, {alias}.excerpt, "
            f"{alias}.excerpt_from, {alias}.body, {alias}.published_at")


def _note_dict(conn, r) -> dict:
    d = {k: r[k] for k in r.keys()}
    w = conn.execute("SELECT id,kind,title,creator,origin_year FROM works WHERE id=?",
                     (r["work_id"],)).fetchone()
    d["work"] = dict(w) if w else None
    if r["event_id"]:
        e = conn.execute(
            "SELECT id,kind,date,year,rating,version_id FROM events WHERE id=?",
            (r["event_id"],)).fetchone()
        d["event"] = dict(e) if e else None
    else:
        d["event"] = None
    return d


def public_feed(conn: sqlite3.Connection, *, limit: int = PAGE_SIZE_DEFAULT,
                cursor: Optional[str] = None) -> dict:
    """Keyset (seek) pagination ordered by (published_at DESC, id DESC).

    Stable while the owner publishes new notes mid-browsing: rows already seen
    never reappear or get skipped (验收: 分页中发布新条目).
    """
    sql = (f"SELECT {_public_note_columns()} FROM notes n "
           f"WHERE {PUBLIC_NOTE}")
    params: list = []
    cur = decode_cursor(cursor)
    if cur:
        sql += " AND (n.published_at < ? OR (n.published_at = ? AND n.id < ?))"
        params += [cur[0], cur[0], cur[1]]
    sql += " ORDER BY n.published_at DESC, n.id DESC LIMIT ?"
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()
    items = [_note_dict(conn, r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last["published_at"], last["id"])
    return {"items": items, "next_cursor": next_cursor,
        "counting_basis": "事件流按发布笔记分页；统计见 /api/public/stats"}


def public_events_by_year(conn: sqlite3.Connection, year: int) -> dict:
    """All experience events in a year, each with ONLY its public notes."""
    events = conn.execute(
        """SELECT e.*, w.title AS work_title, w.kind AS work_kind, w.creator,
                  v.label AS version_label
           FROM events e
           JOIN works w ON w.id=e.work_id
           LEFT JOIN versions v ON v.id=e.version_id
           WHERE e.year=? ORDER BY e.date DESC, e.id DESC""", (year,)).fetchall()
    out = []
    for e in events:
        d = dict(e)
        d["notes"] = [_note_dict(conn, r) for r in conn.execute(
            f"SELECT {_public_note_columns()} FROM notes n "
            f"WHERE n.event_id=? AND {PUBLIC_NOTE} ORDER BY n.published_at DESC",
            (e["id"],))]
        d["topics"] = [dict(t) for t in conn.execute(
            """SELECT t.id,t.slug,t.name FROM topics t
               JOIN event_topics et ON et.topic_id=t.id WHERE et.event_id=?""",
            (e["id"],))]
        out.append(d)
    return {"year": year, "counting_basis": "events=阅读/观看/聆听经历次数; "
            "works=去重后的作品数", "events": out}


def public_events_by_topic(conn: sqlite3.Connection, slug: str) -> dict:
    t = repo.resolve_topic(conn, slug)
    if not t:
        raise repo.DomainError("主题不存在或已更名（可尝试新名称）", "not_found", 404)
    rows = conn.execute(
        """SELECT e.*, w.title AS work_title, w.kind AS work_kind, w.creator,
                  v.label AS version_label
           FROM event_topics et
           JOIN events e ON e.id=et.event_id
           JOIN works w ON w.id=e.work_id
           LEFT JOIN versions v ON v.id=e.version_id
           WHERE et.topic_id=? ORDER BY e.date DESC, e.id DESC""",
        (t["id"],)).fetchall()
    events = []
    for e in rows:
        d = dict(e)
        d["notes"] = [_note_dict(conn, r) for r in conn.execute(
            f"SELECT {_public_note_columns()} FROM notes n "
            f"WHERE n.event_id=? AND {PUBLIC_NOTE} ORDER BY n.published_at DESC",
            (e["id"],))]
        events.append(d)
    renames = [dict(r) for r in conn.execute(
        "SELECT old_name,new_name,renamed_at FROM topic_renames WHERE topic_id=? "
        "ORDER BY renamed_at", (t["id"],))]
    return {"topic": {"id": t["id"], "slug": t["slug"], "name": t["name"]},
            "previous_names": renames, "events": events}


def public_work(conn: sqlite3.Connection, work_id: int) -> dict:
    wid = repo.canonical_work_id(conn, work_id)
    w = repo.get_work(conn, wid)
    if not w:
        raise repo.DomainError("作品不存在", "not_found", 404)
    public_notes = [dict(r) for r in conn.execute(
        f"SELECT {_public_note_columns()} FROM notes n "
        f"WHERE n.work_id=? AND {PUBLIC_NOTE}", (wid,))]
    if not public_notes:
        # nothing to show to visitors; private works stay undiscoverable.
        raise repo.DomainError("该作品暂无公开内容", "not_public", 404)
    events = [dict(r) for r in conn.execute(
        """SELECT e.*, v.label AS version_label FROM events e
           LEFT JOIN versions v ON v.id=e.version_id
           WHERE e.work_id=? ORDER BY e.date DESC""", (wid,))]
    return {"work": w, "events": events, "public_notes": public_notes,
            "merge_provenance": repo.merge_provenance(conn, wid)}


def public_years(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT e.year, COUNT(DISTINCT e.id) AS event_count,
                  COUNT(DISTINCT e.work_id) AS work_count
           FROM events e GROUP BY e.year ORDER BY e.year DESC""")]


# ---------------------------------------------------------------- search

def _snippet(conn, note_row, terms: str) -> dict:
    """Build a snippet from PUBLISHED text only. FTS is populated solely with
    published notes, so private/pending excerpts can never appear here."""
    title = note_row["title"] if "title" in note_row.keys() else ""
    snip = conn.execute(
        "SELECT snippet(notes_fts, 1, '《', '》', '…', 18) AS s FROM notes_fts "
        "WHERE note_id=?", (note_row["note_id"],)).fetchone()
    body_snip = (snip["s"] if snip and snip["s"] else (note_row["body"] or "")[:80])
    return {"note_id": note_row["note_id"], "title": title,
            "snippet": body_snip, "visibility": "published"}


def public_search(conn: sqlite3.Connection, query: str, limit: int = 20) -> dict:
    q = (query or "").strip()
    if not q:
        return {"query": q, "results": []}
    # Published notes only. Third-party EXCERPTS are never searchable: a
    # successful import/crawl is not a licence to reproduce them, and the
    # owner decides per note whether an excerpt is public. Matching therefore
    # uses the owner body + title columns (FTS col 1 and col 3).
    try:
        fts_rows = conn.execute(
            "SELECT note_id FROM notes_fts WHERE notes_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            ("{body title}: " + q + "*", limit)).fetchall()
        ids = [r["note_id"] for r in fts_rows]
    except Exception:
        ids = []
    if ids:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT n.id AS note_id, n.body, w.title, n.published_at
                FROM notes n JOIN works w ON w.id=n.work_id
                WHERE n.id IN ({ph}) AND {PUBLIC_NOTE}
                ORDER BY n.published_at DESC""", ids).fetchall()
    else:
        like = f"%{q}%"
        rows = conn.execute(
            f"""SELECT n.id AS note_id, n.body, w.title, n.published_at
                FROM notes n JOIN works w ON w.id=n.work_id
                WHERE {PUBLIC_NOTE} AND (n.body LIKE ? OR w.title LIKE ?)
                ORDER BY n.published_at DESC LIMIT ?""",
            (like, like, limit)).fetchall()
    results = []
    for r in rows[:limit]:
        snip = conn.execute(
            "SELECT snippet(notes_fts, 1, '《', '》', '…', 20) AS s FROM "
            "notes_fts WHERE note_id=?", (r["note_id"],)).fetchone()
        results.append({"note_id": r["note_id"], "title": r["title"],
                        "snippet": ((snip and snip["s"]) or
                                    (r["body"] or "")[:80]),
                        "visibility": "published"})
    return {"query": q, "results": results}


def admin_search(conn: sqlite3.Connection, query: str, limit: int = 50) -> dict:
    """Owner-side search across every note, with visibility labels."""
    like = f"%{(query or '').strip()}%"
    rows = conn.execute(
        """SELECT n.*, w.title FROM notes n JOIN works w ON w.id=n.work_id
           WHERE n.body LIKE ? OR n.excerpt LIKE ? OR w.title LIKE ?
           ORDER BY n.updated_at DESC LIMIT ?""",
        (like, like, like, limit)).fetchall()
    return {"query": query, "results": [dict(r) for r in rows]}


# ---------------------------------------------------------------- statistics

def stats(conn: sqlite3.Connection, *, public_only: bool) -> dict:
    """Explicit counting basis (验收: 年度统计明确按作品还是事件计数).

    * events  = number of experiences; every re-read/watch/listen counts.
    * works   = DISTINCT canonical works experienced in that year
                (merged duplicates count once).
    * notes   = published notes only for public stats.
    """
    note_filter = f"AND {PUBLIC_NOTE}" if public_only else ""
    by_year_events = conn.execute(
        "SELECT year, COUNT(*) AS events, COUNT(DISTINCT work_id) AS works "
        "FROM events GROUP BY year").fetchall()
    by_kind = conn.execute(
        "SELECT kind, COUNT(*) AS events, COUNT(DISTINCT work_id) AS works "
        "FROM events GROUP BY kind").fetchall()
    note_row = conn.execute(
        f"SELECT COUNT(*) AS c FROM notes n WHERE 1=1 {note_filter}") \
        .fetchone()
    total_events = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    total_works = conn.execute(
        "SELECT COUNT(*) c FROM works WHERE status='active'").fetchone()["c"]
    return {
        "counting_basis": {
            "events": "阅读/观看/聆听经历次数：重读、重看、重听各算一次（不是重复数据）",
            "works": "去重后的作品数：同一作品的不同译本/版本只计一次",
            "notes": "公开统计只含 visibility=published 的笔记",
        },
        "totals": {"events": total_events, "works": total_works,
                   "notes": note_row["c"]},
        "by_year": [dict(r) for r in by_year_events],
        "by_kind": [dict(r) for r in by_kind],
    }


# ---------------------------------------------------------------- export

def export_public(conn: sqlite3.Connection) -> dict:
    """Public export: published notes + provenance. Same visibility filter as
    lists/search/cards."""
    notes = conn.execute(
        f"""SELECT n.id, n.body, n.excerpt, n.excerpt_from, n.published_at,
                   w.title, w.creator, w.kind
            FROM notes n JOIN works w ON w.id=n.work_id
            WHERE {PUBLIC_NOTE} ORDER BY n.published_at DESC""").fetchall()
    return {"exported_at": repo.now() if hasattr(repo, "now") else None,
            "visibility": "published-only",
            "notes": [dict(r) for r in notes]}


def export_public_markdown(conn: sqlite3.Connection) -> str:
    data = export_public(conn)
    lines = ["# 成长站 · 书影音档案（公开内容导出）", ""]
    for n in data["notes"]:
        lines.append(f"## 《{n['title']}》"
                     f"{(' · ' + n['creator']) if n['creator'] else ''}")
        lines.append(f"_{n['published_at'][:10] if n['published_at'] else ''}_  ")
        if n["excerpt"]:
            lines.append(f"> {n['excerpt']}"
                         f"{('（' + n['excerpt_from'] + '）') if n['excerpt_from'] else ''}  ")
        if n["body"]:
            lines.append(n["body"])
        lines.append("")
    return "\n".join(lines)


def export_admin(conn: sqlite3.Connection) -> dict:
    """Owner export: everything, including private/pending notes and raw
    import lines. Requires admin auth at the HTTP layer."""
    works = conn.execute("SELECT * FROM works ORDER BY id").fetchall()
    events = conn.execute("SELECT * FROM events ORDER BY date, id").fetchall()
    notes = conn.execute("SELECT * FROM notes ORDER BY id").fetchall()
    raw = conn.execute(
        """SELECT r.batch_id, r.line_no, r.raw_line, r.status, b.filename,
                  b.imported_at
           FROM import_rows r JOIN import_batches b ON b.id=r.batch_id
           ORDER BY r.batch_id, r.line_no""").fetchall()
    return {"visibility": "owner-full",
            "works": [dict(r) for r in works],
            "events": [dict(r) for r in events],
            "notes": [dict(r) for r in notes],
            "import_rows": [dict(r) for r in raw]}


# ---------------------------------------------------------------- share card

def share_card(conn: sqlite3.Connection, note_id: int) -> dict:
    n = conn.execute(
        """SELECT n.*, w.title, w.creator, w.kind FROM notes n
           JOIN works w ON w.id=n.work_id WHERE n.id=?""",
        (note_id,)).fetchone()
    if not n:
        raise repo.DomainError("笔记不存在", "not_found", 404)
    if n["visibility"] != "published":
        # Same rule as search/export: private & pending cards are 404 for
        # everyone except the owner UI, which uses the admin API.
        raise repo.DomainError("该笔记未发布，不能生成分享卡", "not_public", 404)
    return {"note_id": note_id, "title": n["title"], "creator": n["creator"],
            "kind": n["kind"], "body": (n["body"] or "")[:140],
            "excerpt": (n["excerpt"] or "")[:140],
            "excerpt_from": n["excerpt_from"],
            "published_at": n["published_at"]}
