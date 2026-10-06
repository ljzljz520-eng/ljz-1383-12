"""SQLite connection layer and schema for the reading/viewing/listening archive.

Design points
-------------
* ``works`` hold the abstract work ("1984"), ``versions`` a concrete edition /
  translation / recording / film release, ``events`` an actual experience
  (a read/watch/listen on a date). Re-reads are *new events*, never duplicates.
* External identifiers are never folded into a single string: every id is a
  separate row in ``work_identifiers`` / ``version_identifiers`` so that
  matching is evidence based, not title based.
* Every imported line keeps its verbatim ``raw_line`` plus batch/source info.
* Merge keeps full provenance (merge_groups + merge_log) so a wrong merge can
  always be split apart again.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Abstract works ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS works (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('book','film','music')),
    title       TEXT NOT NULL,
    creator     TEXT,                 -- author / director / artist
    origin_year INTEGER,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','merged')),
    merged_into INTEGER REFERENCES works(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_title ON works(title);
CREATE INDEX IF NOT EXISTS idx_works_status ON works(status);

CREATE TABLE IF NOT EXISTS work_identifiers (
    id         INTEGER PRIMARY KEY,
    work_id    INTEGER NOT NULL REFERENCES works(id),
    namespace  TEXT NOT NULL,        -- isbn_work/goodgrid/douban/imdb/mb_album ...
    ext_id     TEXT NOT NULL,
    UNIQUE(namespace, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_wid_work ON work_identifiers(work_id);

-- Concrete versions (translations / editions / recordings / releases) ----------
CREATE TABLE IF NOT EXISTS versions (
    id           INTEGER PRIMARY KEY,
    work_id      INTEGER NOT NULL REFERENCES works(id),
    kind         TEXT,               -- translation / edition / recording / release
    label        TEXT,               -- "刘绍铭 译本", "1984 英版蓝光"
    publisher    TEXT,
    medium       TEXT,               -- 纸质 / 电子书 / 影院 / 流媒体 / CD ...
    year         INTEGER,
    is_primary   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ver_work ON versions(work_id);

CREATE TABLE IF NOT EXISTS version_identifiers (
    id         INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    namespace  TEXT NOT NULL,        -- isbn / upc / ean / release_asin ...
    ext_id     TEXT NOT NULL,
    UNIQUE(namespace, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_vid_ver ON version_identifiers(version_id);

-- Themes ------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_renames (
    id         INTEGER PRIMARY KEY,
    topic_id   INTEGER NOT NULL REFERENCES topics(id),
    old_name   TEXT NOT NULL,
    new_name   TEXT NOT NULL,
    renamed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_topics (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    PRIMARY KEY (event_id, topic_id)
);

-- Experiences --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    work_id           INTEGER NOT NULL REFERENCES works(id),
    version_id        INTEGER REFERENCES versions(id),
    kind              TEXT NOT NULL CHECK (kind IN ('read','watch','listen')),
    date              TEXT NOT NULL,  -- ISO day YYYY-MM-DD
    year              INTEGER NOT NULL,
    rating            INTEGER CHECK (rating BETWEEN 1 AND 5),
    personal_gain     TEXT,           -- 个人收获（纯文本，来自站主本人）
    source_url        TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_work ON events(work_id);
CREATE INDEX IF NOT EXISTS idx_events_year ON events(year);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);

-- Personal notes with a 4-state visibility machine ------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER REFERENCES events(id) ON DELETE SET NULL,
    work_id      INTEGER NOT NULL REFERENCES works(id),
    visibility   TEXT NOT NULL
                 CHECK (visibility IN ('private','pending','published','archived')),
    excerpt      TEXT,                -- 摘录（第三方内容，默认 private）
    excerpt_from TEXT,                -- 摘录出处（页码/时间点等，仅作引用线索）
    body         TEXT,                -- 站主自己的感想
    from_import  INTEGER NOT NULL DEFAULT 0,   -- 1 = 导入产生，未被站主显式勾选
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_event ON notes(event_id);
CREATE INDEX IF NOT EXISTS idx_notes_work ON notes(work_id);
CREATE INDEX IF NOT EXISTS idx_notes_vis ON notes(visibility, published_at);

CREATE TABLE IF NOT EXISTS note_published_snapshots (
    id          INTEGER PRIMARY KEY,
    note_id     INTEGER NOT NULL REFERENCES notes(id),
    body        TEXT,
    excerpt     TEXT,
    excerpt_from TEXT,
    published_at TEXT NOT NULL,
    archived_at TEXT
);

-- Import batches & verbatim rows -------------------------------------------------
CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY,
    filename     TEXT,
    content_hash TEXT NOT NULL UNIQUE,   -- same file re-upload => idempotent
    raw_header   TEXT,                    -- header line kept for row replay
    total_rows   INTEGER NOT NULL DEFAULT 0,
    ok_rows      INTEGER NOT NULL DEFAULT 0,
    conflict_rows INTEGER NOT NULL DEFAULT 0,
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_rows (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES import_batches(id),
    line_no     INTEGER NOT NULL,
    raw_line    TEXT NOT NULL,          -- 原始行逐字保留
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','applied','conflict','skipped','error')),
    work_id     INTEGER REFERENCES works(id),
    version_id  INTEGER REFERENCES versions(id),
    event_id    INTEGER REFERENCES events(id),
    note_id     INTEGER REFERENCES notes(id),
    conflict_id INTEGER REFERENCES conflicts(id),
    error       TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT,
    kind        TEXT,                  -- manual / import / third_party_ref
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_link_changes (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    old_url     TEXT,
    new_url     TEXT,
    reason      TEXT,
    changed_at  TEXT NOT NULL
);

-- Conflict review queue -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS conflicts (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,  -- title_only|id_mismatch|changed_link|ambiguous
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','resolved_keep_separate',
                                   'resolved_merge','resolved_cancel','dismissed')),
    payload      TEXT NOT NULL,  -- JSON snapshot of candidates + raw row
    resolution   TEXT,
    resolved_by  TEXT,
    resolved_at  TEXT,
    created_at   TEXT NOT NULL
);

-- Merge provenance -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_groups (
    id          INTEGER PRIMARY KEY,
    canonical_work_id INTEGER NOT NULL REFERENCES works(id),
    created_at  TEXT NOT NULL,
    reason      TEXT,
    evidence    TEXT
);

CREATE TABLE IF NOT EXISTS merge_members (
    id              INTEGER PRIMARY KEY,
    group_id        INTEGER NOT NULL REFERENCES merge_groups(id),
    duplicate_work_id INTEGER NOT NULL REFERENCES works(id),
    title_snapshot  TEXT NOT NULL,
    creator_snapshot TEXT,
    merged_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_log (
    id          INTEGER PRIMARY KEY,
    action      TEXT NOT NULL,          -- merge / split
    work_id     INTEGER,
    other_id    INTEGER,
    payload     TEXT NOT NULL,          -- JSON detail
    created_at  TEXT NOT NULL
);

-- Search index (FTS5, content kept here so visibility rules are enforced) ---------
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    note_id UNINDEXED,
    body,
    excerpt,
    title
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
