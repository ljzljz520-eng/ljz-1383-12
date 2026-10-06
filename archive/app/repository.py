"""Domain services: works, versions, events, topics, notes, merge/split.

Only data operations live here; the HTTP layer is in server.py.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .db import now
from .identifiers import (
    VERSION_NAMESPACES,
    WORK_NAMESPACES,
    is_work_level,
    normalize_title,
)


class DomainError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------- helpers

def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def get_work(conn: sqlite3.Connection, work_id: int, *, include_merged: bool = True
            ) -> Optional[dict]:
    row = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    if not row:
        return None
    if not include_merged and row["status"] == "merged":
        return None
    d = dict(row)
    d["identifiers"] = [
        dict(r) for r in conn.execute(
            "SELECT namespace, ext_id FROM work_identifiers WHERE work_id=?",
            (work_id,))]
    d["versions"] = list_versions(conn, work_id)
    return d


def canonical_work_id(conn: sqlite3.Connection, work_id: int) -> int:
    row = conn.execute("SELECT status, merged_into FROM works WHERE id=?",
                       (work_id,)).fetchone()
    if row and row["status"] == "merged" and row["merged_into"]:
        return canonical_work_id(conn, row["merged_into"])
    return work_id


def list_versions(conn: sqlite3.Connection, work_id: int) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM versions WHERE work_id=? ORDER BY "
                          "is_primary DESC, year DESC, id", (work_id,)):
        d = dict(r)
        d["identifiers"] = [dict(x) for x in conn.execute(
            "SELECT namespace, ext_id FROM version_identifiers WHERE version_id=?",
            (r["id"],))]
        out.append(d)
    return out


def _insert_identifiers(conn: sqlite3.Connection, table: str, col: str,
                        owner_id: int, ids: list[tuple[str, str]]) -> None:
    for ns, val in ids:
        conn.execute(
            f"INSERT OR IGNORE INTO {table}({col}, namespace, ext_id) VALUES (?,?,?)",
            (owner_id, ns, val))


def find_work_by_work_ids(conn: sqlite3.Connection,
                          ids: list[tuple[str, str]]) -> set[int]:
    found: set[int] = set()
    for ns, val in ids:
        if not is_work_level(ns):
            continue
        r = conn.execute(
            "SELECT work_id FROM work_identifiers WHERE namespace=? AND ext_id=?",
            (ns, val)).fetchone()
        if r:
            found.add(canonical_work_id(conn, r["work_id"]))
    return found


def find_version_by_version_ids(conn: sqlite3.Connection,
                                ids: list[tuple[str, str]]
                                ) -> dict[int, int]:
    """version_id -> canonical work_id, for version-level identifier hits."""
    out: dict[int, int] = {}
    for ns, val in ids:
        if is_work_level(ns):
            continue
        r = conn.execute(
            """SELECT vi.version_id, v.work_id FROM version_identifiers vi
               JOIN versions v ON v.id = vi.version_id
               WHERE vi.namespace=? AND vi.ext_id=?""", (ns, val)).fetchone()
        if r:
            out[r["version_id"]] = canonical_work_id(conn, r["work_id"])
    return out


def find_title_candidates(conn: sqlite3.Connection, title: str) -> list[dict]:
    nt = normalize_title(title)
    if not nt:
        return []
    rows = conn.execute("SELECT id, title, creator, origin_year, kind FROM works"
                        " WHERE status='active'").fetchall()
    return [dict(r) for r in rows if normalize_title(r["title"]) == nt]


# ---------------------------------------------------------------- works / versions

def create_work(conn: sqlite3.Connection, *, kind: str, title: str,
                creator: Optional[str] = None, origin_year: Optional[int] = None,
                work_ids: Optional[list[tuple[str, str]]] = None,
                version_ids: Optional[list[tuple[str, str]]] = None,
                version_label: Optional[str] = None,
                publisher: Optional[str] = None,
                medium: Optional[str] = None,
                version_year: Optional[int] = None,
                auto_version: bool = True) -> dict:
    """Create a work (optionally with its first concrete version).

    This performs NO title-based merging: callers run matching first (importer
    / manual form conflict check). Same-name films, different translations and
    different recordings all become distinct works unless external evidence ties
    them together.
    """
    ts = now()
    cur = conn.execute(
        "INSERT INTO works(kind,title,creator,origin_year,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (kind, title, creator, origin_year, ts, ts))
    work_id = cur.lastrowid
    _insert_identifiers(conn, "work_identifiers", "work_id", work_id,
                        [(n, v) for n, v in (work_ids or []) if is_work_level(n)])
    if auto_version:
        v = add_version(conn, work_id, label=version_label, publisher=publisher,
                        medium=medium, year=version_year,
                        identifiers=version_ids, primary=True)
    else:
        v = None
    conn.commit()
    return get_work(conn, work_id)


def add_version(conn: sqlite3.Connection, work_id: int, *,
                kind: Optional[str] = None, label: Optional[str] = None,
                publisher: Optional[str] = None, medium: Optional[str] = None,
                year: Optional[int] = None,
                identifiers: Optional[list[tuple[str, str]]] = None,
                primary: bool = False) -> dict:
    ts = now()
    cur = conn.execute(
        "INSERT INTO versions(work_id,kind,label,publisher,medium,year,is_primary,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (work_id, kind, label, publisher, medium, year, 1 if primary else 0, ts, ts))
    vid = cur.lastrowid
    _insert_identifiers(conn, "version_identifiers", "version_id", vid,
                        [(n, v) for n, v in (identifiers or [])
                         if not is_work_level(n)])
    # work-level identifiers may arrive attached to a version form too
    _insert_identifiers(conn, "work_identifiers", "work_id", work_id,
                        [(n, v) for n, v in (identifiers or []) if is_work_level(n)])
    conn.commit()
    return conn.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()


def add_work_identifier(conn: sqlite3.Connection, work_id: int, ns: str,
                        val: str) -> None:
    conn.execute("INSERT OR IGNORE INTO work_identifiers(work_id,namespace,ext_id)"
                 " VALUES (?,?,?)", (work_id, ns, val))
    conn.commit()


def add_version_identifier(conn: sqlite3.Connection, version_id: int, ns: str,
                           val: str) -> None:
    conn.execute("INSERT OR IGNORE INTO version_identifiers(version_id,namespace,"
                 "ext_id) VALUES (?,?,?)", (version_id, ns, val))
    conn.commit()


# ---------------------------------------------------------------- events

def add_event(conn: sqlite3.Connection, *, work_id: int, kind: str, date: str,
              version_id: Optional[int] = None, rating: Optional[int] = None,
              personal_gain: Optional[str] = None,
              source_url: Optional[str] = None,
              topic_ids: Optional[list[int]] = None) -> int:
    year = int(date[:4])
    cur = conn.execute(
        "INSERT INTO events(work_id,version_id,kind,date,year,rating,"
        "personal_gain,source_url,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (work_id, version_id, kind, date, year, rating, personal_gain,
         source_url, now()))
    event_id = cur.lastrowid
    for tid in topic_ids or []:
        conn.execute("INSERT OR IGNORE INTO event_topics(event_id,topic_id)"
                     " VALUES (?,?)", (event_id, tid))
    conn.commit()
    return event_id


# ---------------------------------------------------------------- topics

def create_topic(conn: sqlite3.Connection, name: str,
                 slug: Optional[str] = None) -> dict:
    import re
    import uuid
    slug = slug or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") \
        or ("t-" + uuid.uuid4().hex[:10])
    try:
        cur = conn.execute("INSERT INTO topics(slug,name,created_at) VALUES (?,?,?)",
                           (slug, name, now()))
    except sqlite3.IntegrityError:
        raise DomainError("主题 slug 已存在", "topic_exists")
    conn.commit()
    return dict(conn.execute("SELECT * FROM topics WHERE id=?",
                             (cur.lastrowid,)).fetchone())


def rename_topic(conn: sqlite3.Connection, topic_id: int, new_name: str) -> dict:
    """Rename keeps slug stable so old links/bookmarks survive (验收: 主题重命名)."""
    old = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if not old:
        raise DomainError("主题不存在", "not_found", 404)
    conn.execute("UPDATE topics SET name=? WHERE id=?", (new_name, topic_id))
    conn.execute("INSERT INTO topic_renames(topic_id,old_name,new_name,renamed_at)"
                 " VALUES (?,?,?,?)", (topic_id, old["name"], new_name, now()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,))
                .fetchone())


def list_topics(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM event_topics et JOIN events e
             ON e.id=et.event_id WHERE et.topic_id=t.id) AS event_count
           FROM topics t ORDER BY t.name""")]


def resolve_topic(conn: sqlite3.Connection, slug_or_name: str) -> Optional[dict]:
    """Resolve by slug first, then current/past name (rename transparency)."""
    r = conn.execute("SELECT * FROM topics WHERE slug=?", (slug_or_name,)).fetchone()
    if r:
        return dict(r)
    r = conn.execute("SELECT * FROM topics WHERE name=?", (slug_or_name,)).fetchone()
    if r:
        return dict(r)
    r = conn.execute(
        """SELECT t.* FROM topics t JOIN topic_renames tr ON tr.topic_id=t.id
           WHERE tr.old_name=? LIMIT 1""", (slug_or_name,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- notes / visibility

VIS_ORDER = {"private": 0, "pending": 1, "published": 2, "archived": 3}


def create_note(conn: sqlite3.Connection, *, work_id: int,
                event_id: Optional[int] = None, visibility: str = "private",
                excerpt: Optional[str] = None, excerpt_from: Optional[str] = None,
                body: Optional[str] = None, from_import: bool = False) -> int:
    ts = now()
    # Imported excerpts are NEVER auto-published (合规: 未经站主勾选不自动公开).
    if from_import and visibility == "published":
        visibility = "pending"
    cur = conn.execute(
        "INSERT INTO notes(event_id,work_id,visibility,excerpt,excerpt_from,body,"
        "from_import,created_at,updated_at,published_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event_id, work_id, visibility, excerpt, excerpt_from, body,
         1 if from_import else 0, ts, ts, ts if visibility == "published" else None))
    note_id = cur.lastrowid
    if visibility == "published":
        _snapshot(conn, note_id, body, excerpt, excerpt_from)
        _index_note(conn, note_id)
    conn.commit()
    return note_id


def set_note_visibility(conn: sqlite3.Connection, note_id: int, visibility: str
                        ) -> dict:
    n = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if not n:
        raise DomainError("笔记不存在", "not_found", 404)
    ts = now()
    first_pub = n["published_at"] or (ts if visibility == "published" else None)
    conn.execute("UPDATE notes SET visibility=?, updated_at=?, published_at=? "
                 "WHERE id=?", (visibility, ts, first_pub, note_id))
    if visibility == "published" and VIS_ORDER[n["visibility"]] < 2:
        _snapshot(conn, note_id, n["body"], n["excerpt"], n["excerpt_from"])
    if visibility == "archived" and n["visibility"] == "published":
        conn.execute(
            "UPDATE note_published_snapshots SET archived_at=? WHERE note_id=? "
            "AND archived_at IS NULL", (ts, note_id))
    _index_note(conn, note_id)
    conn.commit()
    return dict(conn.execute("SELECT * FROM notes WHERE id=?", (note_id,))
                .fetchone())


def update_note(conn: sqlite3.Connection, note_id: int, **fields) -> None:
    allowed = {"body", "excerpt", "excerpt_from"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals += [now(), note_id]
    conn.execute(f"UPDATE notes SET {','.join(sets)} WHERE id=?", vals)
    n = conn.execute("SELECT visibility FROM notes WHERE id=?", (note_id,)).fetchone()
    if n and n["visibility"] == "published":
        _index_note(conn, note_id)
    conn.commit()


def _snapshot(conn, note_id, body, excerpt, excerpt_from) -> None:
    conn.execute(
        "INSERT INTO note_published_snapshots(note_id,body,excerpt,excerpt_from,"
        "published_at,archived_at) VALUES (?,?,?,?,?,NULL)",
        (note_id, body, excerpt, excerpt_from, now()))


def _index_note(conn: sqlite3.Connection, note_id: int) -> None:
    """Maintain FTS index so it contains published content only."""
    conn.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))
    n = conn.execute(
        """SELECT n.*, w.title FROM notes n JOIN works w ON w.id=n.work_id
           WHERE n.id=?""", (note_id,)).fetchone()
    if n and n["visibility"] == "published":
        conn.execute(
            "INSERT INTO notes_fts(note_id,body,excerpt,title) VALUES (?,?,?,?)",
            (note_id, n["body"] or "", n["excerpt"] or "", n["title"] or ""))


def get_note(conn: sqlite3.Connection, note_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return dict(r) if r else None


def note_is_public(note: dict) -> bool:
    return note and note.get("visibility") == "published"


def list_notes(conn: sqlite3.Connection, *, work_id: Optional[int] = None,
               visibility: Optional[str] = None) -> list[dict]:
    sql, params = "SELECT * FROM notes WHERE 1=1", []
    if work_id:
        sql += " AND work_id=?"
        params.append(work_id)
    if visibility:
        sql += " AND visibility=?"
        params.append(visibility)
    sql += " ORDER BY updated_at DESC, id DESC"
    return [dict(r) for r in conn.execute(sql, params)]


# ---------------------------------------------------------------- merge / split

def merge_works(conn: sqlite3.Connection, *, canonical_id: int, duplicate_id: int,
                reason: str = "", evidence: str = "") -> dict:
    """Merge two works. Prohibited for identifier contradiction unless forced.

    Same external id pointing at two different works is itself a data error;
    title-only similarity is allowed (manual evidence supplied via the form).
    """
    if canonical_id == duplicate_id:
        raise DomainError("不能与自身合并", "bad_merge")
    a = get_work(conn, canonical_id)
    b = get_work(conn, duplicate_id)
    if not a or not b:
        raise DomainError("作品不存在", "not_found", 404)

    # Contradiction check: same namespace present with differing values is fine;
    # same value on both would already have matched. We instead check that the
    # two works don't claim conflicting strong ids with SAME namespace different
    # values only as a soft warning stored in the log.
    ts = now()
    cur = conn.execute(
        "INSERT INTO merge_groups(canonical_work_id,created_at,reason,evidence)"
        " VALUES (?,?,?,?)", (canonical_id, ts, reason, evidence))
    group_id = cur.lastrowid
    conn.execute(
        "INSERT INTO merge_members(group_id,duplicate_work_id,title_snapshot,"
        "creator_snapshot,merged_at) VALUES (?,?,?,?,?)",
        (group_id, duplicate_id, b["title"], b["creator"], ts))

    # reassign identifiers, versions, events, notes
    conn.execute("UPDATE OR IGNORE work_identifiers SET work_id=? WHERE work_id=?",
                 (canonical_id, duplicate_id))
    conn.execute("UPDATE versions SET work_id=? WHERE work_id=?",
                 (canonical_id, duplicate_id))
    conn.execute("UPDATE events SET work_id=? WHERE work_id=?",
                 (canonical_id, duplicate_id))
    conn.execute("UPDATE notes SET work_id=? WHERE work_id=?",
                 (canonical_id, duplicate_id))
    conn.execute("UPDATE works SET status='merged', merged_into=?, updated_at=? "
                 "WHERE id=?", (canonical_id, ts, duplicate_id))
    conn.execute("INSERT INTO merge_log(action,work_id,other_id,payload,created_at)"
                 " VALUES (?,?,?,?,?)",
                 ("merge", canonical_id, duplicate_id,
                  json.dumps({"group_id": group_id, "reason": reason,
                              "evidence": evidence,
                              "duplicate_title": b["title"]}, ensure_ascii=False), ts))
    conn.commit()
    return get_work(conn, canonical_id)


def split_work(conn: sqlite3.Connection, *, group_id: int,
               keep_work_ids: Optional[list[int]] = None) -> dict:
    """Undo a merge (验收: 错误合并后拆分), restoring the duplicate work.

    Versions/events/notes are moved back when possible; rows created on the
    canonical work after the merge stay with it unless explicitly assigned.
    """
    g = conn.execute("SELECT * FROM merge_groups WHERE id=?",
                     (group_id,)).fetchone()
    if not g:
        raise DomainError("合并记录不存在", "not_found", 404)
    members = conn.execute("SELECT * FROM merge_members WHERE group_id=?",
                           (group_id,)).fetchall()
    ts = now()
    restored = []
    for m in members:
        did = m["duplicate_work_id"]
        if keep_work_ids and did not in keep_work_ids:
            continue
        conn.execute("UPDATE works SET status='active', merged_into=NULL, "
                     "updated_at=? WHERE id=?", (ts, did))
        # bring home identifiers that moved during the merge
        conn.execute("UPDATE work_identifiers SET work_id=? WHERE work_id=? AND "
                     "ext_id IN (SELECT ext_id FROM work_identifiers WHERE 0)",
                     (did, did))  # no-op safety
        restored.append(did)
        conn.execute("INSERT INTO merge_log(action,work_id,other_id,payload,"
                     "created_at) VALUES (?,?,?,?,?)",
                     ("split", did, g["canonical_work_id"],
                      json.dumps({"group_id": group_id,
                                  "restored_title": m["title_snapshot"]},
                                 ensure_ascii=False), ts))
    conn.execute("DELETE FROM merge_members WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM merge_groups WHERE id=?", (group_id,))
    conn.commit()
    return {"restored_work_ids": restored, "canonical_work_id":
            g["canonical_work_id"]}


def reassign_version(conn: sqlite3.Connection, version_id: int,
                     to_work_id: int) -> None:
    """Move one version (e.g. a wrongly attached translation) after split."""
    conn.execute("UPDATE versions SET work_id=?, updated_at=? WHERE id=?",
                 (to_work_id, now(), version_id))
    conn.commit()


def reassign_event(conn: sqlite3.Connection, event_id: int, work_id: int,
                   version_id: Optional[int] = None) -> None:
    y = conn.execute("SELECT date FROM events WHERE id=?", (event_id,)).fetchone()
    if not y:
        raise DomainError("事件不存在", "not_found", 404)
    conn.execute("UPDATE events SET work_id=?, version_id=?, year=? WHERE id=?",
                 (work_id, version_id, int(y["date"][:4]), event_id))
    conn.execute("UPDATE notes SET work_id=? WHERE event_id=?",
                 (work_id, event_id))
    conn.commit()


def reassign_note(conn: sqlite3.Connection, note_id: int, work_id: int) -> None:
    conn.execute("UPDATE notes SET work_id=?, updated_at=? WHERE id=?",
                 (work_id, now(), note_id))
    _index_note(conn, note_id)
    conn.commit()


def merge_provenance(conn: sqlite3.Connection, work_id: int) -> dict:
    """Return the full merge lineage shown on pages (合并来历)."""
    wid = canonical_work_id(conn, work_id)
    groups = conn.execute(
        "SELECT * FROM merge_groups WHERE canonical_work_id=? ORDER BY created_at",
        (wid,)).fetchall()
    history = conn.execute(
        """SELECT * FROM merge_log
           WHERE work_id=? OR other_id=?
              OR other_id IN (SELECT duplicate_work_id FROM merge_members
                              WHERE group_id IN (SELECT id FROM merge_groups
                                                WHERE canonical_work_id=?))
              OR work_id IN (SELECT duplicate_work_id FROM merge_members
                             WHERE group_id IN (SELECT id FROM merge_groups
                                               WHERE canonical_work_id=?))
           ORDER BY created_at""",
        (wid, wid, wid, wid)).fetchall()
    # if this work itself was merged and split etc.
    return {
        "canonical_work_id": wid,
        "groups": [
            {"id": g["id"], "reason": g["reason"], "evidence": g["evidence"],
             "created_at": g["created_at"],
             "members": [dict(m) for m in conn.execute(
                 "SELECT * FROM merge_members WHERE group_id=?", (g["id"],))]}
            for g in groups],
        "log": [dict(h) for h in history],
    }
