"""Core acceptance tests (no network). Run: python -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db
from app import repository as repo
from app import importer
from app import query as q
from app.identifiers import parse_external_ids, norm_value, norm_namespace


CSV_A = """title,kind,event_kind,event_date,creator,origin_year,external_ids,version_label,publisher,medium,version_year,topics,rating,personal_gain,excerpt,excerpt_from,note
1984,book,read,2022-03-10,George Orwell,1949,goodgrid:12000;isbn:9787532777532,董乐山译本,上海译文,纸质,2014,自由|反乌托邦,5,第一次的收获,自由即奴役,p9,语言塑造思想
1984,book,read,2024-08-01,George Orwell,1949,goodgrid:12000;isbn:9789866723407,徐立妍译本,麦田,电子书,2016,自由,4,重读新收获,,,
同名书,book,read,2024-01-01,张三,2020,isbn:9787000000001,A版,甲社,纸质,2020,主题X,3,,,
同名书,book,read,2024-02-01,李四,2021,isbn:9787000000018,B版,乙社,纸质,2021,主题X,3,,,
"""

CSV_TITLE_ONLY = """title,kind,event_kind,event_date,creator,origin_year,external_ids,version_label,publisher,medium,version_year,topics,personal_gain,excerpt
同名书,book,read,2024-03-01,张三,2022,,C版,丙社,纸质,2022,主题X,只有标题没有标识,某摘录
"""


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = connect(self.tmp.name)
        init_db(self.conn)
        self.r = importer.import_csv(self.conn, CSV_A.encode(), "a.csv")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    # 1. 同一本书不同译本：作品合并为一，版本为二（ISBN 不同 => 版本不同）
    def test_translations_same_work_two_versions(self):
        self.assertEqual(self.r["conflict_rows"], 0)
        works = {r["title"]: dict(r) for r in
                 self.conn.execute("SELECT * FROM works")}
        w1984 = works["1984"]
        versions = repo.list_versions(self.conn, w1984["id"])
        self.assertEqual(len(versions), 2, "两个 ISBN 不同的译本应是两个版本")
        labels = {v["label"] for v in versions}
        self.assertEqual(labels, {"董乐山译本", "徐立妍译本"})

    # 2. 同名但不同 ISBN、不同作者 => 不同作品，不能凭标题合并
    def test_same_title_different_isbn_not_merged(self):
        rows = self.conn.execute(
            "SELECT id,creator FROM works WHERE title='同名书'").fetchall()
        self.assertEqual(len(rows), 2, "同名不同作品必须分开")
        self.assertNotEqual(rows[0]["creator"], rows[1]["creator"])

    # 3. 仅有标题、无外部标识 => title_only 冲突，等待人工
    def test_title_only_creates_conflict(self):
        r = importer.import_csv(self.conn, CSV_TITLE_ONLY.encode(), "b.csv")
        self.assertEqual(r["conflict_rows"], 1)
        conf = importer.list_conflicts(self.conn)[0]
        self.assertEqual(conf["kind"], "title_only")
        self.assertGreaterEqual(len(conf["payload"]["candidates"]), 2)
        # raw line preserved
        raw = self.conn.execute(
            "SELECT raw_line FROM import_rows WHERE conflict_id=?",
            (conf["id"],)).fetchone()
        self.assertIn("张三", raw["raw_line"])

    # 4. 冲突审阅：保持独立 => 新建第三个作品
    def test_conflict_keep_separate(self):
        importer.import_csv(self.conn, CSV_TITLE_ONLY.encode(), "b.csv")
        cid = importer.list_conflicts(self.conn)[0]["id"]
        out = importer.resolve_conflict(self.conn, cid, "keep_separate")
        wid = out["new_work_id"]
        w = repo.get_work(self.conn, wid)
        self.assertEqual(w["title"], "同名书")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM works WHERE title='同名书'")
            .fetchone()["c"], 3)

    # 5. 冲突审阅：人工确认合并 => 写合并来历
    def test_conflict_manual_merge_requires_reason_and_logs(self):
        importer.import_csv(self.conn, CSV_TITLE_ONLY.encode(), "b.csv")
        cid = importer.list_conflicts(self.conn)[0]["id"]
        target = self.conn.execute(
            "SELECT id FROM works WHERE title='同名书' AND creator='张三'") \
            .fetchone()["id"]
        with self.assertRaises(repo.DomainError):
            importer.resolve_conflict(self.conn, cid, "merge")
        out = importer.resolve_conflict(
            self.conn, cid, "merge", target_work_id=target,
            reason="人工核对版权页确认为同一书")
        self.assertEqual(out["status"], "resolved_merge")
        log = self.conn.execute(
            "SELECT payload FROM merge_log WHERE action='manual_match'") \
            .fetchone()
        self.assertIn("人工核对", log["payload"])

    # 6. 离线导入重传幂等
    def test_reimport_same_bytes_is_idempotent(self):
        works_before = self.conn.execute("SELECT COUNT(*) c FROM works") \
            .fetchone()["c"]
        events_before = self.conn.execute("SELECT COUNT(*) c FROM events") \
            .fetchone()["c"]
        r2 = importer.import_csv(self.conn, CSV_A.encode(), "a-copy.csv")
        self.assertTrue(r2["idempotent"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM works")
                         .fetchone()["c"], works_before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM events")
                         .fetchone()["c"], events_before)
        batches = self.conn.execute("SELECT COUNT(*) c FROM import_batches") \
            .fetchone()["c"]
        self.assertEqual(batches, 1, "同内容只保留一个批次")

    # 7. 重读 = 新事件；统计区分 events / works
    def test_reread_is_new_event_and_stats_basis(self):
        wid = self.conn.execute(
            "SELECT id FROM works WHERE title='1984'").fetchone()["id"]
        evs = self.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE work_id=?", (wid,)) \
            .fetchone()["c"]
        self.assertEqual(evs, 2, "重读必须是独立的第二条事件")
        st = q.stats(self.conn, public_only=False)
        row2022 = next(x for x in st["by_year"] if x["year"] == 2022)
        row2024 = next(x for x in st["by_year"] if x["year"] == 2024)
        self.assertEqual(row2024["events"], 3)   # 重读 + 2 同名
        self.assertEqual(row2024["works"], 3)    # 1984 + 两本同名
        self.assertIn("重读", st["counting_basis"]["events"])

    # 8. 错误合并后拆分
    def test_wrong_merge_split_restores_work(self):
        a = self.conn.execute(
            "SELECT id FROM works WHERE title='同名书' AND creator='张三'") \
            .fetchone()["id"]
        b = self.conn.execute(
            "SELECT id FROM works WHERE title='同名书' AND creator='李四'") \
            .fetchone()["id"]
        repo.merge_works(self.conn, canonical_id=a, duplicate_id=b,
                         reason="误操作")
        self.assertEqual(repo.canonical_work_id(self.conn, b), a)
        gid = self.conn.execute(
            "SELECT id FROM merge_groups WHERE canonical_work_id=?",
            (a,)).fetchone()["id"]
        out = repo.split_work(self.conn, group_id=gid)
        self.assertIn(b, out["restored_work_ids"])
        restored = repo.get_work(self.conn, b)
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["creator"], "李四")
        # provenance records both merge and split
        prov = repo.merge_provenance(self.conn, a)
        actions = {h["action"] for h in prov["log"]}
        self.assertEqual(actions, {"merge", "split"})

    # 9. 主题重命名：slug 不变，旧名仍可解析
    def test_topic_rename_keeps_links(self):
        t = repo.resolve_topic(self.conn, "自由")
        slug = t["slug"]
        repo.rename_topic(self.conn, t["id"], "自由与禁锢")
        self.assertEqual(repo.resolve_topic(self.conn, slug)["name"],
                         "自由与禁锢")
        self.assertEqual(repo.resolve_topic(self.conn, "自由")["id"], t["id"],
                         "旧名应仍能解析")
        page = q.public_events_by_topic(self.conn, slug)
        self.assertEqual(page["previous_names"][0]["old_name"], "自由")

    # 10. 来源链接变更留痕
    def test_source_url_change_history(self):
        s = importer.get_or_create_source(self.conn, "x", "https://old")
        importer.change_source_url(self.conn, s["id"], "https://new",
                                   "站点迁移")
        ch = self.conn.execute(
            "SELECT * FROM source_link_changes WHERE source_id=?",
            (s["id"],)).fetchone()
        self.assertEqual(ch["old_url"], "https://old")
        self.assertEqual(ch["new_url"], "https://new")

    # 11. 笔记可见性：导入摘录不自动公开，统一过滤
    def test_visibility_consistency(self):
        # all imported notes are private regardless of CSV content
        n = self.conn.execute(
            "SELECT * FROM notes WHERE excerpt IS NOT NULL").fetchone()
        self.assertEqual(n["visibility"], "private")
        self.assertEqual(n["from_import"], 1)
        # publish one owner-written note
        nid = n["id"]
        # private/pending/archived never appear publicly
        feed = q.public_feed(self.conn)
        self.assertEqual(feed["items"], [])
        with self.assertRaises(repo.DomainError):
            q.share_card(self.conn, nid)
        repo.set_note_visibility(self.conn, nid, "pending")
        with self.assertRaises(repo.DomainError):
            q.share_card(self.conn, nid)
        # owner explicitly publishes -> now consistent everywhere
        repo.set_note_visibility(self.conn, nid, "published")
        card = q.share_card(self.conn, nid)
        self.assertEqual(card["note_id"], nid)
        feed = q.public_feed(self.conn)
        self.assertEqual(feed["items"][0]["id"], nid)
        s = q.public_search(self.conn, "语言")
        self.assertTrue(any(r["note_id"] == nid for r in s["results"]),
                        "已发布的正文可搜到")
        # 第三方摘录即使在已发布笔记上也不作为搜索命中内容，除非站主显式公开摘录
        s_ex = q.public_search(self.conn, "自由即奴役")
        self.assertFalse(any(r["note_id"] == nid for r in s_ex["results"]))
        exported = q.export_public(self.conn)
        self.assertTrue(any(x["id"] == nid for x in exported["notes"]))
        # archiving removes from all public surfaces
        repo.set_note_visibility(self.conn, nid, "archived")
        self.assertEqual(q.public_feed(self.conn)["items"], [])
        self.assertEqual(q.public_search(self.conn, "语言")["results"], [])

    # 12. 分页中发布新条目：游标稳定，不重不漏
    def test_keyset_pagination_stable_while_publishing(self):
        wid = self.conn.execute(
            "SELECT id FROM works WHERE title='1984'").fetchone()["id"]
        ids = []
        for i in range(5):
            ids.append(repo.create_note(
                self.conn, work_id=wid, visibility="published",
                body=f"笔记 {i}"))
        page1 = q.public_feed(self.conn, limit=2)
        self.assertEqual([x["id"] for x in page1["items"]],
                         [ids[4], ids[3]])
        # publish a NEW note while reader is paging
        new_id = repo.create_note(self.conn, work_id=wid,
                                  visibility="published", body="新发布")
        page2 = q.public_feed(self.conn, limit=2,
                              cursor=page1["next_cursor"])
        seen = [x["id"] for x in page1["items"]] + \
               [x["id"] for x in page2["items"]]
        self.assertNotIn(new_id, seen[:4],
                         "新条目不应插入到读者已浏览到的位置")
        self.assertEqual(len(seen), len(set(seen)), "无重复")

    # 13. 标识归一化
    def test_identifier_normalization(self):
        self.assertEqual(norm_value("isbn", "978-7-5327-7753-2"),
                         "9787532777532")
        self.assertEqual(norm_value("isbn", "0-14-017753-7"),
                         "9780140177534", "ISBN10 应转为 ISBN13")
        self.assertEqual(norm_value("imdb", "87803"), "tt0087803")
        parsed = parse_external_ids("isbn:978-7-5327-7753-2; imdb:tt0109688")
        self.assertEqual(len(parsed), 2)
        self.assertTrue(norm_namespace("isbn13") == "isbn")

    # 14. 外部标识冲突 => id_mismatch 冲突
    def test_id_mismatch_conflict(self):
        # same ISBN pointing into a row whose strong work-id differs:
        # simulate a row with a work-level id that already exists elsewhere
        w = repo.create_work(self.conn, kind="book", title="书甲",
                             work_ids=[("goodgrid", "100")])
        w2 = repo.create_work(self.conn, kind="book", title="书乙")
        repo.add_work_identifier(self.conn, w2["id"], "goodgrid", "200")
        csv = ("title,kind,event_kind,event_date,external_ids\n"
               "某书,book,read,2024-09-01,goodgrid:100\n")
        # single consistent id => merges to w
        r = importer.import_csv(self.conn, csv.encode(), "c.csv")
        self.assertEqual(r["conflict_rows"], 0)
        csv2 = ("title,kind,event_kind,event_date,external_ids\n"
                "某书,book,read,2024-09-02,goodgrid:200\n")
        # same title import under the other id => the row matches w2 cleanly
        # (titles differ here). For true mismatch, claim both ids:
        csv3 = ("title,kind,event_kind,event_date,external_ids\n"
                "某书,book,read,2024-09-03,goodgrid:100;goodgrid:200\n")
        r3 = importer.import_csv(self.conn, csv3.encode(), "d.csv")
        self.assertEqual(r3["conflict_rows"], 1)
        conf = importer.list_conflicts(self.conn)
        self.assertTrue(any(c["kind"] == "id_mismatch" for c in conf))


if __name__ == "__main__":
    unittest.main(verbosity=2)
