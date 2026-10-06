"""End-to-end HTTP tests against the stdlib server (random port)."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.server import make_handler, App

TOKEN = "test-token"


def req(server, method, path, body=None, token=None, raw=None,
        content_type="application/json", extra_headers=None):
    from urllib.parse import urlsplit, urlunsplit, quote, quote_plus, parse_qsl
    sp = urlsplit(path)
    enc_query = "&".join(
        quote_plus(k) + ("=" + quote_plus(v) if v else "")
        for k, v in parse_qsl(sp.query, keep_blank_values=True))
    path = urlunsplit(("", "", quote(sp.path, safe="/%"), enc_query,
                       sp.fragment))
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": content_type}
    if token:
        headers["X-Admin-Token"] = token
    from urllib.parse import quote
    for k, v in (extra_headers or {}):
        try:
            v.encode("latin-1")
        except UnicodeEncodeError:
            v = quote(v)
        headers[k] = v
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            ct = resp.headers.get("Content-Type", "")
            payload = resp.read()
            return resp.status, (json.loads(payload) if "json" in ct else payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


CSV = """title,kind,event_kind,event_date,creator,origin_year,external_ids,version_label,publisher,medium,version_year,topics,rating,personal_gain,excerpt,excerpt_from,note
1984,book,read,2022-03-10,George Orwell,1949,goodgrid:12000;isbn:9787532777532,董乐山译本,译文,纸质,2014,自由,5,收获一,自由即奴役,p9,感想一
1984,book,read,2024-08-01,George Orwell,1949,goodgrid:12000;isbn:9789866723407,徐立妍译本,麦田,电子书,2016,自由,4,收获二,,,
城堡,book,read,2024-01-05,卡夫卡,1926,isbn:9787532740771,韩瑞祥译本,译文,纸质,2007,孤独,4,,,
"""

CSV_TITLE = """title,kind,event_kind,event_date,creator,origin_year,external_ids,version_label,topics,personal_gain,excerpt
城堡,book,read,2025-03-01,卡夫卡,1926,,试译本,孤独,标题撞车的新事件,私有摘录
"""


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        cls.app = App(cls.tmp.name, TOKEN)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.thr = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.unlink(cls.tmp.name)

    def call(self, *a, **k):
        return req(self.server, *a, **k)

    def test_01_unauthenticated_admin_blocked(self):
        s, b = self.call("GET", "/api/admin/works")
        self.assertEqual(s, 401)
        s, b = self.call("POST", "/api/admin/session", {"token": "wrong"})
        self.assertEqual(s, 401)
        s, b = self.call("POST", "/api/admin/session", {"token": TOKEN})
        self.assertEqual(s, 200)

    def test_02_import_and_idempotent_reupload(self):
        s, b = self.call("POST", "/api/admin/import", raw=CSV.encode(),
                         token=TOKEN, content_type="text/csv",
                         extra_headers=[("X-Import-Filename", "c.csv")])
        self.assertEqual(s, 201)
        self.assertEqual(b["total_rows"], 3)
        self.assertEqual(b["conflict_rows"], 0)
        # re-upload identical bytes
        s, b2 = self.call("POST", "/api/admin/import", raw=CSV.encode(),
                          token=TOKEN, content_type="text/csv",
                          extra_headers=[("X-Import-Filename", "c-again.csv")])
        self.assertTrue(b2["idempotent"])
        self.assertEqual(b2["batch_id"], b["batch_id"])
        # works: 1984 single work with 2 versions
        s, works = self.call("GET", "/api/admin/works", token=TOKEN)
        self.assertEqual(len(works["works"]), 2)
        wid = works["works"][-1]["id"] if False else None
        s, w = self.call("GET", "/api/admin/works/1", token=TOKEN)
        self.assertEqual(len(w["versions"]), 2)

    def test_03_raw_rows_preserved(self):
        s, b = self.call("GET", "/api/admin/import-batches/1", token=TOKEN)
        self.assertTrue(all(r["raw_line"] for r in b["rows"]))
        self.assertIn("董乐山", b["rows"][0]["raw_line"])

    def test_04_title_conflict_review_flow(self):
        s, b = self.call("POST", "/api/admin/import", raw=CSV_TITLE.encode(),
                         token=TOKEN, content_type="text/csv",
                         extra_headers=[("X-Import-Filename", "t.csv")])
        self.assertEqual(b["conflict_rows"], 1)
        s, conf = self.call("GET", "/api/admin/conflicts?status=open",
                            token=TOKEN)
        cid = conf["conflicts"][0]["id"]
        # merge without reason is refused
        s, b = self.call("POST", f"/api/admin/conflicts/{cid}/resolve",
                         {"action": "merge", "target_work_id": 3}, token=TOKEN)
        self.assertEqual(s, 400)
        # resolve: keep separate creates a new work and replays the event
        s, b = self.call("POST", f"/api/admin/conflicts/{cid}/resolve",
                         {"action": "keep_separate", "reason": "人工核对不同书"},
                         token=TOKEN)
        self.assertEqual(s, 200)
        self.assertIn("new_work_id", b)
        self.assertEqual(len(b["replayed"]), 1)
        # the replayed note must be private
        s, notes = self.call(
            "GET", f"/api/admin/notes?work_id={b['new_work_id']}", token=TOKEN)
        self.assertEqual(notes["notes"][0]["visibility"], "private")
        # feed still empty (nothing published)
        s, feed = self.call("GET", "/api/public/feed")
        self.assertEqual(feed["items"], [])

    def test_05_publish_share_search_export_consistency(self):
        s, notes = self.call("GET", "/api/admin/notes", token=TOKEN)
        target = next(n for n in notes["notes"] if n["body"] == "感想一")
        # card 404 before publish
        s, b = self.call("GET", f"/share/{target['id']}")
        self.assertEqual(s, 404)
        s, b = self.call("PATCH", f"/api/admin/notes/{target['id']}",
                         {"visibility": "published"}, token=TOKEN)
        self.assertEqual(s, 200)
        # card now renders HTML
        s, html = self.call("GET", f"/share/{target['id']}")
        self.assertEqual(s, 200)
        self.assertIn("感想一".encode(), html)
        # public search summary appears
        s, b = self.call("GET", "/api/public/search?q=感想")
        self.assertTrue(any(r["note_id"] == target["id"]
                            for r in b["results"]))
        # the imported excerpt (private) must NOT be searchable publicly
        s, b = self.call("GET", "/api/public/search?q=自由即奴役")
        self.assertFalse(any(r["note_id"] == target["id"]
                             for r in b["results"]))
        # export contains only the published note
        s, b = self.call("GET", "/api/public/export")
        ids = [n["id"] for n in b["notes"]]
        self.assertEqual(ids, [target["id"]])
        # admin export contains everything incl. private + raw rows
        s, b = self.call("GET", "/api/admin/export", token=TOKEN)
        self.assertGreaterEqual(len(b["notes"]), 2)
        self.assertTrue(b["import_rows"])

    def test_06_pagination_publish_midway(self):
        s, notes = self.call("GET", "/api/admin/notes", token=TOKEN)
        wid = 1  # 1984
        created = []
        for i in range(4):
            s, b = self.call("POST", "/api/admin/notes", {
                "work_id": wid, "visibility": "published",
                "body": f"连续笔记{i}"}, token=TOKEN)
            created.append(b["note_id"])
        s, p1 = self.call("GET", "/api/public/feed?limit=2")
        self.assertIn("next_cursor", p1)
        first_ids = [x["id"] for x in p1["items"]]
        # publish a brand-new note while paging
        s, b = self.call("POST", "/api/admin/notes", {
            "work_id": wid, "visibility": "published",
            "body": "分页中途新发"}, token=TOKEN)
        new_id = b["note_id"]
        s, p2 = self.call("GET", "/api/public/feed?limit=2&cursor="
                          + p1["next_cursor"])
        second_ids = [x["id"] for x in p2["items"]]
        self.assertFalse(set(first_ids) & set(second_ids), "不重复")
        self.assertNotIn(new_id, second_ids, "新条目不插页")

    def test_07_topic_rename_link_stable(self):
        s, topics = self.call("GET", "/api/admin/topics", token=TOKEN)
        t = next(x for x in topics["topics"] if x["name"] == "自由")
        slug = t["slug"]
        s, b = self.call("POST", f"/api/admin/topics/{t['id']}/rename",
                         {"name": "自由与边界"}, token=TOKEN)
        s, page = self.call("GET", f"/api/public/topics/{slug}")
        self.assertEqual(page["topic"]["name"], "自由与边界")
        self.assertEqual(page["previous_names"][0]["old_name"], "自由")
        s, page2 = self.call("GET", "/api/public/topics/自由")
        self.assertEqual(page2["topic"]["id"], t["id"])

    def test_08_source_link_change(self):
        s, b = self.call("POST", "/api/admin/sources",
                         {"name": "src", "url": "https://old"}, token=TOKEN)
        sid = b["id"]
        s, b = self.call("POST", f"/api/admin/sources/{sid}/url",
                         {"url": "https://new", "reason": "域名迁移"},
                         token=TOKEN)
        self.assertEqual(b["url"], "https://new")
        s, ch = self.call("GET", "/api/admin/source-changes", token=TOKEN)
        self.assertTrue(any(x["old_url"] == "https://old"
                            and x["new_url"] == "https://new"
                            for x in ch["changes"]))

    def test_09_wrong_merge_then_split(self):
        s, works = self.call("GET", "/api/admin/works", token=TOKEN)
        # merge work 2 into 1 wrongly
        s, b = self.call("POST", "/api/admin/merge",
                         {"canonical_id": 1, "duplicate_id": 2,
                          "reason": "误并"}, token=TOKEN)
        s, b = self.call("GET", "/api/admin/merge-groups", token=TOKEN)
        gid = b["groups"][0]["id"]
        s, b = self.call("POST", f"/api/admin/merge-groups/{gid}/split",
                         {}, token=TOKEN)
        self.assertIn(2, b["restored_work_ids"])
        s, w = self.call("GET", "/api/admin/works/2", token=TOKEN)
        self.assertEqual(w["status"], "active")

    def test_10_stats_basis_and_pages(self):
        s, b = self.call("GET", "/api/public/stats")
        self.assertIn("events", b["counting_basis"])
        self.assertIn("works", b["counting_basis"])
        self.assertTrue(any(y["year"] == 2022 for y in b["by_year"]))
        s, y = self.call("GET", "/api/public/years/2024")
        self.assertIn("counting_basis", y)
        s, years = self.call("GET", "/api/public/years")
        self.assertTrue(years["years"])

    def test_11_work_page_shows_merge_provenance(self):
        # work 1 has merge/split log for work 2
        s, b = self.call("GET", "/api/public/works/1")
        self.assertEqual(s, 200)
        actions = {x["action"] for x in b["merge_provenance"]["log"]}
        self.assertIn("split", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
