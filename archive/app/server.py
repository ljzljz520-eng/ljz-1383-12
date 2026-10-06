"""HTTP layer (stdlib only). Run:

    python3 -m app.server --db data/archive.db --port 8000

Admin endpoints require header ``X-Admin-Token`` (env ARCHIVE_ADMIN_TOKEN,
default 'dev-token' for local development).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse, unquote

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .db import connect, init_db, now
from . import repository as repo
from . import importer
from . import query as q

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web")


class App:
    def __init__(self, db_path: str, admin_token: str):
        self.db_path = db_path
        self.admin_token = admin_token
        conn = connect(db_path)
        init_db(conn)
        conn.close()

    def conn(self) -> sqlite3.Connection:
        return connect(self.db_path)


# Route table: (method, prefix, needs_admin, handler)
def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GrowthArchive/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------- plumbing
        def _json(self, obj, status: int = 200, extra_headers=None):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str, status: int = 200):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise repo.DomainError("请求体不是合法 JSON", "bad_json")

        def _admin_ok(self) -> bool:
            tok = self.headers.get("X-Admin-Token", "")
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "admin_token" in cookie:
                tok = tok or cookie["admin_token"].value
            return tok == app.admin_token

        def _require_admin(self) -> bool:
            if not self._admin_ok():
                self._json({"error": "需要管理员令牌", "code": "unauthorized"}, 401)
                return False
            return True

        # ---------- entry
        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PATCH(self):
            self.route("PATCH")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method: str):
            try:
                self._dispatch(method)
            except repo.DomainError as e:
                self._json({"error": str(e), "code": e.code}, e.status)
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover
                self._json({"error": f"服务器错误: {e}", "code": "internal"}, 500)

        def _dispatch(self, method: str):
            p = urlparse(self.path)
            path = unquote(p.path)
            qs = parse_qs(p.query)
            conn = app.conn()
            try:
                # ---- static pages
                if method == "GET" and path in ("/", "/index.html"):
                    return self._serve_static("index.html", "text/html")
                if method == "GET" and path in ("/admin", "/admin.html"):
                    return self._serve_static("admin.html", "text/html")
                if method == "GET" and path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):], None)

                # ---- admin auth
                if method == "POST" and path == "/api/admin/session":
                    data = self._read_json()
                    if data.get("token") == app.admin_token:
                        return self._json({"ok": True})
                    return self._json({"ok": False}, 401)

                # ---- public APIs
                if method == "GET" and path == "/api/public/feed":
                    return self._json(q.public_feed(
                        conn, limit=int(qs.get("limit", [20])[0]),
                        cursor=qs.get("cursor", [None])[0]))
                if method == "GET" and path == "/api/public/years":
                    return self._json({"years": q.public_years(conn)})
                if method == "GET" and path.startswith("/api/public/years/"):
                    year = int(path.rsplit("/", 1)[-1])
                    return self._json(q.public_events_by_year(conn, year))
                if method == "GET" and path.startswith("/api/public/topics/"):
                    slug = path.rsplit("/", 1)[-1]
                    return self._json(q.public_events_by_topic(conn, slug))
                if method == "GET" and path == "/api/public/topics":
                    return self._json({"topics": repo.list_topics(conn)})
                if method == "GET" and path == "/api/public/search":
                    return self._json(q.public_search(
                        conn, qs.get("q", [""])[0]))
                if method == "GET" and path == "/api/public/stats":
                    return self._json(q.stats(conn, public_only=True))
                if method == "GET" and path == "/api/public/export":
                    if qs.get("format", ["json"])[0] == "markdown":
                        md = q.export_public_markdown(conn)
                        body = md.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type",
                                         "text/markdown; charset=utf-8")
                        self.send_header("Content-Disposition",
                                         "attachment; filename=archive-public.md")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        return self.wfile.write(body)
                    return self._json(q.export_public(conn))
                if method == "GET" and path.startswith("/api/public/works/"):
                    wid = int(path.rsplit("/", 1)[-1])
                    return self._json(q.public_work(conn, wid))
                if method == "GET" and path.startswith("/share/"):
                    nid = int(path.rsplit("/", 1)[-1])
                    return self._render_share(conn, nid)

                # ---- everything below is admin-only
                if not self._require_admin():
                    return

                if method == "GET" and path == "/api/admin/whoami":
                    return self._json({"ok": True})
                # works
                if method == "POST" and path == "/api/admin/works":
                    d = self._read_json()
                    w = repo.create_work(
                        conn, kind=d["kind"], title=d["title"],
                        creator=d.get("creator"), origin_year=d.get("origin_year"),
                        work_ids=d.get("work_ids", []),
                        version_ids=d.get("version_ids", []),
                        version_label=d.get("version_label"),
                        publisher=d.get("publisher"), medium=d.get("medium"),
                        version_year=d.get("version_year"))
                    return self._json(w, 201)
                if method == "GET" and path == "/api/admin/works":
                    rows = conn.execute(
                        "SELECT * FROM works ORDER BY updated_at DESC").fetchall()
                    return self._json({"works": [dict(r) for r in rows]})
                if method == "GET" and path.startswith("/api/admin/works/"):
                    wid = int(path.rsplit("/", 1)[-1])
                    w = repo.get_work(conn, wid)
                    if not w:
                        raise repo.DomainError("作品不存在", "not_found", 404)
                    w["events"] = [dict(r) for r in conn.execute(
                        "SELECT * FROM events WHERE work_id=? ORDER BY date DESC",
                        (repo.canonical_work_id(conn, wid),))]
                    w["notes"] = repo.list_notes(conn, work_id=wid)
                    w["merge_provenance"] = repo.merge_provenance(conn, wid)
                    return self._json(w)
                if method == "POST" and path.endswith("/versions"):
                    seg = path.split("/")
                    wid = int(seg[4])
                    d = self._read_json()
                    v = repo.add_version(
                        conn, wid, kind=d.get("kind"), label=d.get("label"),
                        publisher=d.get("publisher"), medium=d.get("medium"),
                        year=d.get("year"),
                        identifiers=d.get("identifiers", []),
                        primary=bool(d.get("primary")))
                    return self._json(dict(v), 201)

                # events
                if method == "POST" and path == "/api/admin/events":
                    d = self._read_json()
                    eid = repo.add_event(
                        conn, work_id=d["work_id"], kind=d["kind"],
                        date=d["date"], version_id=d.get("version_id"),
                        rating=d.get("rating"),
                        personal_gain=d.get("personal_gain"),
                        source_url=d.get("source_url"),
                        topic_ids=d.get("topic_ids", []))
                    return self._json({"event_id": eid}, 201)

                # topics
                if method == "GET" and path == "/api/admin/topics":
                    return self._json({"topics": repo.list_topics(conn)})
                if method == "POST" and path == "/api/admin/topics":
                    d = self._read_json()
                    return self._json(repo.create_topic(
                        conn, d["name"], d.get("slug")), 201)
                if method == "POST" and path.startswith("/api/admin/topics/") \
                        and path.endswith("/rename"):
                    tid = int(path.split("/")[4])
                    d = self._read_json()
                    return self._json(repo.rename_topic(conn, tid, d["name"]))

                # notes
                if method == "GET" and path == "/api/admin/notes":
                    vis = qs.get("visibility", [None])[0]
                    wid = qs.get("work_id", [None])[0]
                    return self._json({"notes": repo.list_notes(
                        conn, work_id=int(wid) if wid else None,
                        visibility=vis)})
                if method == "POST" and path == "/api/admin/notes":
                    d = self._read_json()
                    nid = repo.create_note(
                        conn, work_id=d["work_id"],
                        event_id=d.get("event_id"),
                        visibility=d.get("visibility", "private"),
                        excerpt=d.get("excerpt"),
                        excerpt_from=d.get("excerpt_from"),
                        body=d.get("body"),
                        from_import=bool(d.get("from_import")))
                    return self._json({"note_id": nid}, 201)
                if method == "PATCH" and path.startswith("/api/admin/notes/"):
                    nid = int(path.rsplit("/", 1)[-1])
                    d = self._read_json()
                    if "visibility" in d:
                        n = repo.set_note_visibility(conn, nid, d["visibility"])
                        return self._json(n)
                    repo.update_note(conn, nid, **d)
                    return self._json(repo.get_note(conn, nid))

                # sources
                if method == "GET" and path == "/api/admin/sources":
                    rows = conn.execute("SELECT * FROM sources ORDER BY id") \
                        .fetchall()
                    return self._json({"sources": [dict(r) for r in rows]})
                if method == "POST" and path == "/api/admin/sources":
                    d = self._read_json()
                    s = importer.get_or_create_source(
                        conn, d["name"], d.get("url"), d.get("kind", "manual"))
                    return self._json(s, 201)
                if method == "POST" and path.startswith(
                        "/api/admin/sources/") and path.endswith("/url"):
                    seg = path.split("/")
                    sid = int(seg[4])
                    d = self._read_json()
                    sc = importer.change_source_url(
                        conn, sid, d["url"], d.get("reason", ""))
                    return self._json(sc)
                if method == "GET" and path == "/api/admin/source-changes":
                    rows = conn.execute(
                        "SELECT * FROM source_link_changes ORDER BY id DESC") \
                        .fetchall()
                    return self._json({"changes": [dict(r) for r in rows]})

                # conflicts
                if method == "GET" and path == "/api/admin/conflicts":
                    status = qs.get("status", ["open"])[0]
                    return self._json({"conflicts":
                                       importer.list_conflicts(conn, status)})
                if method == "POST" and path.startswith(
                        "/api/admin/conflicts/") and path.endswith("/resolve"):
                    cid = int(path.split("/")[4])  # '',api,admin,conflicts,<id>,resolve
                    d = self._read_json()
                    return self._json(importer.resolve_conflict(
                        conn, cid, d["action"],
                        target_work_id=d.get("target_work_id"),
                        reason=d.get("reason", ""),
                        resolved_by=d.get("resolved_by", "admin")))

                # merge / split
                if method == "POST" and path == "/api/admin/merge":
                    d = self._read_json()
                    w = repo.merge_works(
                        conn, canonical_id=d["canonical_id"],
                        duplicate_id=d["duplicate_id"],
                        reason=d.get("reason", ""),
                        evidence=d.get("evidence", ""))
                    return self._json(w)
                if method == "GET" and path == "/api/admin/merge-groups":
                    rows = conn.execute(
                        "SELECT * FROM merge_groups ORDER BY id DESC").fetchall()
                    out = []
                    for g in rows:
                        gd = dict(g)
                        gd["members"] = [dict(m) for m in conn.execute(
                            "SELECT * FROM merge_members WHERE group_id=?",
                            (g["id"],))]
                        out.append(gd)
                    return self._json({"groups": out})
                if method == "POST" and path.startswith(
                        "/api/admin/merge-groups/") and path.endswith("/split"):
                    gid = int(path.split("/")[4])  # '',api,admin,merge-groups,<id>,split
                    d = self._read_json()
                    return self._json(repo.split_work(
                        conn, group_id=gid,
                        keep_work_ids=d.get("keep_work_ids")))
                if method == "POST" and path == "/api/admin/reassign/version":
                    d = self._read_json()
                    repo.reassign_version(conn, d["version_id"], d["to_work_id"])
                    return self._json({"ok": True})
                if method == "POST" and path == "/api/admin/reassign/event":
                    d = self._read_json()
                    repo.reassign_event(conn, d["event_id"], d["work_id"],
                                        d.get("version_id"))
                    return self._json({"ok": True})
                if method == "POST" and path == "/api/admin/reassign/note":
                    d = self._read_json()
                    repo.reassign_note(conn, d["note_id"], d["work_id"])
                    return self._json({"ok": True})

                # pre-merge check used by the manual-entry form
                if method == "POST" and path == "/api/admin/match-check":
                    from .identifiers import parse_external_ids
                    d = self._read_json()
                    ids = parse_external_ids(d.get("external_ids", ""))
                    work_id, vid, conflict = importer.match_row(
                        conn, title=d.get("title", ""), kind=d.get("kind", ""),
                        creator=d.get("creator"),
                        origin_year=d.get("origin_year"), ids=ids,
                        evidence_url=d.get("evidence_url"))
                    return self._json({"work_id": work_id, "version_id": vid,
                                       "conflict": conflict})

                # import
                if method == "POST" and path == "/api/admin/import":
                    filename = unquote(self.headers.get("X-Import-Filename",
                                                         "upload.csv"))
                    length = int(self.headers.get("Content-Length") or 0)
                    content = self.rfile.read(length) if length else b""
                    res = importer.import_csv(conn, content, filename)
                    return self._json(res, 200 if res.get("idempotent") else 201)
                if method == "GET" and path == "/api/admin/import-batches":
                    rows = conn.execute(
                        "SELECT * FROM import_batches ORDER BY id DESC").fetchall()
                    return self._json({"batches": [dict(r) for r in rows]})
                if method == "GET" and path.startswith(
                        "/api/admin/import-batches/"):
                    bid = int(path.rsplit("/", 1)[-1])
                    rows = conn.execute(
                        "SELECT * FROM import_rows WHERE batch_id=? ORDER BY line_no",
                        (bid,)).fetchall()
                    return self._json({"rows": [dict(r) for r in rows]})

                # search / stats / export (admin breadth)
                if method == "GET" and path == "/api/admin/search":
                    return self._json(q.admin_search(
                        conn, qs.get("q", [""])[0]))
                if method == "GET" and path == "/api/admin/stats":
                    return self._json(q.stats(conn, public_only=False))
                if method == "GET" and path == "/api/admin/export":
                    return self._json(q.export_admin(conn))

                return self._json({"error": f"未找到路径 {path}",
                                   "code": "not_found"}, 404)
            finally:
                conn.close()

        # ---------- static & card
        def _serve_static(self, name: str, ctype: str | None):
            fp = os.path.normpath(os.path.join(WEB_DIR, name))
            if not fp.startswith(WEB_DIR) or not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            guess = ctype or ("text/html" if fp.endswith(".html") else
                              "text/css" if fp.endswith(".css") else
                              "application/javascript" if fp.endswith(".js")
                              else "application/octet-stream")
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{guess}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _render_share(self, conn, note_id: int):
            import html
            card = q.share_card(conn, note_id)
            esc = html.escape
            body = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>分享卡 · {esc(card['title'])}</title>
<style>
body{{margin:0;background:#f4f1ea;font-family:-apple-system,'Noto Serif SC',serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
.card{{background:#fff;max-width:560px;margin:24px;padding:40px;border-radius:16px;
box-shadow:0 12px 40px rgba(80,60,30,.15)}}
.kind{{color:#a9843b;font-size:13px;letter-spacing:.3em}}
h1{{font-size:24px;margin:8px 0 4px}} .creator{{color:#888;margin-bottom:20px}}
.quote{{border-left:3px solid #d8c38a;padding-left:14px;color:#444;white-space:
pre-wrap}} .gain{{margin-top:16px;line-height:1.8;white-space:pre-wrap}}
.meta{{margin-top:28px;color:#aaa;font-size:12px;text-align:right}}
</style></head><body><div class="card">
<div class="kind">{esc({'book':'书','film':'影','music':'音'}.get(card['kind'],''))}</div>
<h1>《{esc(card['title'])}》</h1>
<div class="creator">{esc(card['creator'] or '')}</div>
{('<div class="quote">'+esc(card['excerpt'])+(' <small>('+esc(card['excerpt_from'])+')</small>' if card['excerpt_from'] else '')+'</div>') if card['excerpt'] else ''}
{('<div class="gain">'+esc(card['body'])+'</div>') if card['body'] else ''}
<div class="meta">成长站书影音档案 · {esc((card['published_at'] or '')[:10])}</div>
</div></body></html>"""
            self._html(body)

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("ARCHIVE_DB", "data/archive.db"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("ARCHIVE_PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("ARCHIVE_HOST", "127.0.0.1"))
    ap.add_argument("--token", default=os.environ.get("ARCHIVE_ADMIN_TOKEN",
                                                      "dev-token"))
    args = ap.parse_args(argv)
    app = App(args.db, args.token)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"成长站档案: http://{args.host}:{args.port}/  (admin /admin, "
          f"token={'default dev-token' if args.token=='dev-token' else 'set'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
