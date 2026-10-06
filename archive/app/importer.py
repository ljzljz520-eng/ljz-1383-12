"""Offline CSV import.

Guarantees
----------
* ``content_hash`` makes re-uploading the *same file* idempotent (验收:
  离线导入重传). A changed file is a new batch.
* Every source line is stored verbatim in ``import_rows.raw_line`` forever.
* Matching: strong work id > version id > manual evidence > title. Title alone
  never merges; when the title matches but creators clearly differ, the row is
  treated as a distinct work; when creators also agree, it opens a conflict.
* On conflict resolution the stored raw row is *replayed* through the normal
  pipeline against the chosen/new work, so the event/note still lands correctly.
* Imported excerpts are created as **private** notes (never auto-public).
* Third-party URLs are citation clues only; nothing is fetched.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from typing import Optional

from .db import now
from .identifiers import is_work_level, normalize_title, parse_external_ids
from . import repository as repo

REQUIRED = {"title", "kind", "event_date", "event_kind"}


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def find_batch(conn: sqlite3.Connection, content_hash: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM import_batches WHERE content_hash=?",
                     (content_hash,)).fetchone()
    return dict(r) if r else None


def _open_conflict(conn, kind: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO conflicts(kind,payload,created_at) VALUES (?,?,?)",
        (kind, json.dumps(payload, ensure_ascii=False), now()))
    return cur.lastrowid


def _candidate_summary(conn, work_id: int) -> dict:
    w = repo.get_work(conn, work_id)
    return {"work_id": work_id, "title": w["title"], "creator": w.get("creator"),
            "origin_year": w.get("origin_year"),
            "identifiers": w["identifiers"], "versions": w["versions"]}


def match_row(conn: sqlite3.Connection, *, title: str, kind: str,
              creator: Optional[str], origin_year: Optional[int],
              ids: list[tuple[str, str]], evidence_url: Optional[str]
              ) -> tuple[Optional[int], Optional[int], Optional[dict]]:
    """Return (work_id, version_id_or_None, conflict_or_None)."""
    work_hits = repo.find_work_by_work_ids(conn, ids)
    ver_hits = repo.find_version_by_version_ids(conn, ids)
    ver_work_ids = set(ver_hits.values())
    all_hits = work_hits | ver_work_ids

    if len(all_hits) > 1:
        cid = _open_conflict(conn, "id_mismatch", {
            "title": title, "kind": kind, "creator": creator,
            "origin_year": origin_year, "identifiers": ids,
            "evidence_url": evidence_url,
            "candidates": [_candidate_summary(conn, w) for w in all_hits]})
        return None, None, {"id": cid, "kind": "id_mismatch"}
    if len(all_hits) == 1:
        wid = next(iter(all_hits))
        return wid, next(iter(ver_hits), None), None

    title_cands = repo.find_title_candidates(conn, title)
    if title_cands:
        new_creator = (creator or "").strip()
        same_creator = [c for c in title_cands
                        if (c.get("creator") or "").strip() == new_creator] \
            if new_creator else []
        differing = [c for c in title_cands
                     if new_creator and (c.get("creator") or "").strip()
                     != new_creator]
        if same_creator or not new_creator or not differing:
            # Ambiguous: could be another translation OR a coincidental title.
            # Never merge on title; ask the human.
            cid = _open_conflict(conn, "title_only", {
                "title": title, "kind": kind, "creator": creator,
                "origin_year": origin_year, "identifiers": ids,
                "evidence_url": evidence_url,
                "note": "标题相同但缺少外部标识证据，不能自动合并",
                "candidates": [_candidate_summary(conn, c["id"])
                               for c in title_cands]})
            return None, None, {"id": cid, "kind": "title_only"}
        # all title twins clearly have different creators => distinct works.
        return None, None, None
    return None, None, None


def import_csv(conn: sqlite3.Connection, content: bytes,
               filename: str = "upload.csv") -> dict:
    text = content.decode("utf-8-sig")
    digest = _hash(content)
    if (existing := find_batch(conn, digest)):
        return {"batch_id": existing["id"], "idempotent": True,
                "total_rows": existing["total_rows"],
                "ok_rows": existing["ok_rows"],
                "conflict_rows": existing["conflict_rows"]}

    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        raise repo.DomainError(f"CSV 缺少必填列: {sorted(missing)}", "bad_csv")

    raw_lines = text.splitlines()
    header_line = raw_lines[0] if raw_lines else ""
    cur = conn.execute(
        "INSERT INTO import_batches(filename,content_hash,raw_header,imported_at) "
        "VALUES (?,?,?,?)", (filename, digest, header_line, now()))
    batch_id = cur.lastrowid

    total = ok = conflicts = errors = 0
    for i, row in enumerate(reader, start=2):
        total += 1
        raw = raw_lines[i - 1] if i - 1 < len(raw_lines) else ""
        # reserve the verbatim row first, once
        rc = conn.execute(
            "INSERT INTO import_rows(batch_id,line_no,raw_line,status) VALUES "
            "(?,?,?,'pending')", (batch_id, i, raw))
        row_pk = rc.lastrowid
        try:
            result = _apply_row(conn, batch_id, i, raw, row, row_pk=row_pk)
        except Exception as exc:
            errors += 1
            conn.execute("UPDATE import_rows SET status='error', error=? WHERE id=?",
                         (str(exc), row_pk))
            continue
        if result.get("conflict"):
            conflicts += 1
            conn.execute("UPDATE import_rows SET status='conflict', conflict_id=?,"
                         " work_id=? WHERE id=?",
                         (result["conflict"]["id"], result.get("work_id"), row_pk))
        else:
            ok += 1
            conn.execute("UPDATE import_rows SET status='applied', work_id=?,"
                         " version_id=?, event_id=?, note_id=? WHERE id=?",
                         (result["work_id"], result.get("version_id"),
                          result.get("event_id"), result.get("note_id"), row_pk))

    conn.execute("UPDATE import_batches SET total_rows=?, ok_rows=?, "
                 "conflict_rows=? WHERE id=?",
                 (total, ok, conflicts, batch_id))
    conn.commit()
    return {"batch_id": batch_id, "idempotent": False, "total_rows": total,
            "ok_rows": ok, "conflict_rows": conflicts, "error_rows": errors}


def parse_row(row: dict) -> dict:
    """Normalise a raw CSV dict into structured fields."""
    title = (row.get("title") or "").strip()
    kind = (row.get("kind") or "").strip()
    event_kind = (row.get("event_kind") or "").strip()
    event_date = (row.get("event_date") or "").strip()
    if not title or kind not in ("book", "film", "music"):
        raise ValueError("title 为空或 kind 非法")
    if event_kind not in ("read", "watch", "listen"):
        raise ValueError("event_kind 非法")
    if not event_date:
        raise ValueError("event_date 为空")
    return {
        "title": title, "kind": kind, "event_kind": event_kind,
        "event_date": event_date,
        "creator": (row.get("creator") or "").strip() or None,
        "origin_year": int(row["origin_year"]) if row.get("origin_year") else None,
        "ids": parse_external_ids(row.get("external_ids", "")),
        "evidence_url": (row.get("evidence_url") or "").strip() or None,
        "version_label": (row.get("version_label") or "").strip() or None,
        "publisher": (row.get("publisher") or "").strip() or None,
        "medium": (row.get("medium") or "").strip() or None,
        "version_year": int(row["version_year"]) if row.get("version_year") else None,
        "source_name": (row.get("source") or "").strip() or "manual-import",
        "source_url": (row.get("source_url") or "").strip()
                       or (row.get("evidence_url") or "").strip() or None,
        "topics": [t.strip() for t in (row.get("topics") or "").split("|")
                   if t.strip()],
        "rating": int(row["rating"]) if row.get("rating") else None,
        "gain": (row.get("personal_gain") or "").strip() or None,
        "excerpt": (row.get("excerpt") or "").strip() or None,
        "excerpt_from": (row.get("excerpt_from") or "").strip() or None,
        "body": (row.get("note") or "").strip() or None,
    }


def _apply_row(conn, batch_id, line_no, raw, row, *, row_pk=None) -> dict:
    f = parse_row(row)
    work_id, version_id, conflict = match_row(
        conn, title=f["title"], kind=f["kind"], creator=f["creator"],
        origin_year=f["origin_year"], ids=f["ids"],
        evidence_url=f["evidence_url"])
    if conflict:
        return {"conflict": conflict, "work_id": None}

    work_id, version_id = _ensure_work_and_version(conn, work_id, version_id, f)

    topic_objs = []
    for tname in f["topics"]:
        t = repo.resolve_topic(conn, tname) or repo.create_topic(conn, tname)
        topic_objs.append(t["id"])

    source = get_or_create_source(conn, f["source_name"], f["source_url"])
    event_id = repo.add_event(
        conn, work_id=work_id, kind=f["event_kind"], date=f["event_date"],
        version_id=version_id, rating=f["rating"], personal_gain=f["gain"],
        source_url=source["url"], topic_ids=topic_objs)

    note_id = None
    if f["excerpt"] or f["body"]:
        # imported note: private by default; owner must explicitly publish.
        note_id = repo.create_note(
            conn, work_id=work_id, event_id=event_id, visibility="private",
            excerpt=f["excerpt"], excerpt_from=f["excerpt_from"],
            body=f["body"], from_import=True)
    return {"work_id": work_id, "version_id": version_id,
            "event_id": event_id, "note_id": note_id}


def _ensure_work_and_version(conn, work_id, version_id, f):
    if work_id is None:
        w = repo.create_work(
            conn, kind=f["kind"], title=f["title"], creator=f["creator"],
            origin_year=f["origin_year"], work_ids=f["ids"],
            version_ids=[(n, v) for n, v in f["ids"] if not is_work_level(n)],
            version_label=f["version_label"], publisher=f["publisher"],
            medium=f["medium"], version_year=f["version_year"])
        return w["id"], (w["versions"][0]["id"] if w["versions"] else None)

    # Existing work: a previously unseen version id / distinct version label
    # becomes a NEW concrete version (different translation/recording).
    ver_ids = [(n, v) for n, v in f["ids"] if not is_work_level(n)]
    known = repo.find_version_by_version_ids(conn, ver_ids)
    if known:
        return work_id, next(iter(known))
    labels = {x["label"] for x in repo.list_versions(conn, work_id)}
    if ver_ids or (f["version_label"] and f["version_label"] not in labels):
        v = repo.add_version(
            conn, work_id, label=f["version_label"], publisher=f["publisher"],
            medium=f["medium"], year=f["version_year"], identifiers=ver_ids)
        return work_id, v["id"]
    return work_id, None


# ---------------------------------------------------------------- sources

def get_or_create_source(conn, name: str, url: Optional[str] = None,
                         kind: str = "manual") -> dict:
    r = conn.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
    if r:
        return dict(r)
    cur = conn.execute(
        "INSERT INTO sources(name,url,kind,created_at,updated_at) VALUES (?,?,?,?,?)",
        (name, url, kind, now(), now()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM sources WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def change_source_url(conn, source_id: int, new_url: str,
                      reason: str = "") -> dict:
    s = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not s:
        raise repo.DomainError("来源不存在", "not_found", 404)
    conn.execute(
        "INSERT INTO source_link_changes(source_id,old_url,new_url,reason,changed_at)"
        " VALUES (?,?,?,?,?)", (source_id, s["url"], new_url, reason, now()))
    conn.execute("UPDATE sources SET url=?, updated_at=? WHERE id=?",
                 (new_url, now(), source_id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM sources WHERE id=?",
                            (source_id,)).fetchone())


# ---------------------------------------------------------------- conflicts

def list_conflicts(conn, status: str = "open") -> list[dict]:
    rows = conn.execute("SELECT * FROM conflicts WHERE status=? ORDER BY id",
                        (status,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(r["payload"])
        out.append(d)
    return out


def resolve_conflict(conn, conflict_id: int, action: str,
                     *, target_work_id: Optional[int] = None,
                     reason: str = "", resolved_by: str = "admin") -> dict:
    """Review workflow:
      keep_separate -> create a new work, then replay the stored raw row
      merge         -> attach to ``target_work_id`` (reason + evidence logged
                       in merge provenance), then replay the raw row
      cancel        -> keep raw line but skip application
    """
    c = conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)) \
        .fetchone()
    if not c:
        raise repo.DomainError("冲突不存在", "not_found", 404)
    payload = json.loads(c["payload"])
    result: dict = {}

    # find pending import rows tied to this conflict (raw lines preserved)
    pending_rows = conn.execute(
        "SELECT * FROM import_rows WHERE conflict_id=? AND status='conflict'",
        (conflict_id,)).fetchall()

    if action == "keep_separate":
        ids = payload.get("identifiers", [])
        w = repo.create_work(
            conn, kind=payload["kind"], title=payload["title"],
            creator=payload.get("creator"),
            origin_year=payload.get("origin_year"), work_ids=ids,
            version_label=None,
            version_ids=[(n, v) for n, v in ids if not is_work_level(n)])
        target_work_id = w["id"]
        result["new_work_id"] = w["id"]
        new_status = "resolved_keep_separate"
    elif action == "merge":
        if not target_work_id:
            raise repo.DomainError("合并需要 target_work_id", "need_target")
        if not reason.strip():
            raise repo.DomainError("人工合并必须填写理由/证据说明", "need_reason")
        for ns, val in payload.get("identifiers", []):
            if is_work_level(ns):
                repo.add_work_identifier(conn, target_work_id, ns, val)
        conn.execute(
            "INSERT INTO merge_log(action,work_id,other_id,payload,created_at)"
            " VALUES (?,?,?,?,?)",
            ("manual_match", target_work_id, None,
             json.dumps({"conflict_id": conflict_id, "reason": reason,
                         "evidence": payload.get("evidence_url"),
                         "raw_title": payload["title"]}, ensure_ascii=False),
             now()))
        result["canonical_work_id"] = target_work_id
        new_status = "resolved_merge"
    elif action == "cancel":
        new_status = "resolved_cancel"
    else:
        raise repo.DomainError("未知的处理动作", "bad_action")

    conn.execute("UPDATE conflicts SET status=?, resolution=?, resolved_by=?,"
                 " resolved_at=? WHERE id=?",
                 (new_status, json.dumps(result, ensure_ascii=False),
                  resolved_by, now(), conflict_id))

    # replay stored raw rows so events/notes land on the chosen work.
    # The raw line + the batch header are all we need; nothing is fetched.
    replayed = []
    if action != "cancel":
        import csv as _csv
        for ir in pending_rows:
            batch = conn.execute(
                "SELECT raw_header FROM import_batches WHERE id=?",
                (ir["batch_id"],)).fetchone()
            parsed = next(_csv.DictReader(io.StringIO(
                batch["raw_header"] + "\n" + ir["raw_line"])))
            fields = parse_row(parsed)
            if payload.get("identifiers"):
                # reviewer decision supersedes raw identifiers
                fields["ids"] = payload["identifiers"]
            wid, vid = _ensure_work_and_version(
                conn, target_work_id, None, fields)
            topic_objs = []
            for tname in fields["topics"]:
                t = repo.resolve_topic(conn, tname) or \
                    repo.create_topic(conn, tname)
                topic_objs.append(t["id"])
            eid = repo.add_event(
                conn, work_id=wid, kind=fields["event_kind"],
                date=fields["event_date"], version_id=vid,
                rating=fields["rating"], personal_gain=fields["gain"],
                source_url=fields["source_url"], topic_ids=topic_objs)
            nid = None
            if fields["excerpt"] or fields["body"]:
                nid = repo.create_note(
                    conn, work_id=wid, event_id=eid, visibility="private",
                    excerpt=fields["excerpt"],
                    excerpt_from=fields["excerpt_from"], body=fields["body"],
                    from_import=True)
            conn.execute("UPDATE import_rows SET status='applied', work_id=?,"
                         " version_id=?, event_id=?, note_id=? WHERE id=?",
                         (wid, vid, eid, nid, ir["id"]))
            replayed.append({"import_row_id": ir["id"], "event_id": eid,
                             "note_id": nid})
    else:
        conn.execute("UPDATE import_rows SET status='skipped' WHERE conflict_id=?",
                     (conflict_id,))
    result["replayed"] = replayed
    conn.commit()
    result["status"] = new_status
    return result
