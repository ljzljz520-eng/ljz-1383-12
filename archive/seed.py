"""Seed a demo dataset: different translations, a re-read, private/pending/
published notes and a topic rename. Safe to run only on an empty DB."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.db import connect, init_db, now
from app import repository as repo
from app import importer


CSV = """title,kind,event_kind,event_date,creator,origin_year,external_ids,version_label,publisher,medium,version_year,topics,rating,personal_gain,excerpt,excerpt_from,note
1984,book,read,2022-03-10,George Orwell,1949,goodgrid:12000;isbn:9787532777532,董乐山 译本,上海译文出版社,纸质,2014,反乌托邦|自由,5,第一次系统理解语言如何塑造思想,自由即奴役,第一部第一章,语言的边界就是思想的边界？
1984,book,read,2024-08-01,George Orwell,1949,goodgrid:12000;isbn:9789866723407,徐立妍 新译本,麦田,电子书,2016,反乌托邦,4,重读时更关注温斯顿的心理变化,,
东邪西毒,film,watch,2021-11-20,王家卫,1994,imdb:tt0109688,1994 院线修复版,,影院,1994,时间|孤独,5,时间的灰烬这个名字更贴合主题,,
东邪西毒：终极版,film,watch,2023-05-02,王家卫,2008,imdb:tt1010048,2008 终极版,,流媒体,2008,时间,4,重剪版的叙事更直白，少了余味,,
月之暗面,music,listen,2020-06-15,Pink Floyd,1973,mb_release:01,1973 英国首版,Harvest,黑胶,1973,成长|时间,5,循环结构让整轨像一场梦,,
"""


def seed(db_path: str) -> None:
    conn = connect(db_path)
    init_db(conn)
    n = conn.execute("SELECT COUNT(*) c FROM works").fetchone()["c"]
    if n:
        print(f"数据库已有 {n} 部作品，跳过 seed。")
        return
    res = importer.import_csv(conn, CSV.encode("utf-8"), "seed.csv")
    print("导入：", res)

    # topic rename demo: 自由 -> 自由与禁锢（slug 不变）
    t = repo.resolve_topic(conn, "自由")
    if t:
        repo.rename_topic(conn, t["id"], "自由与禁锢")

    # make some notes public, keep the imported excerpt private by default
    notes = repo.list_notes(conn)
    for n in notes:
        if n["from_import"]:
            # imported excerpt stays private (合规演示: 不自动公开)
            continue
    # publish the body-only note of 1984 first read if present
    for n in notes:
        if n["body"] and not n["excerpt"]:
            repo.set_note_visibility(conn, n["id"], "published")

    # a manual published note
    wid = conn.execute("SELECT id FROM works WHERE title='东邪西毒'").fetchone()["id"]
    repo.create_note(conn, work_id=wid, visibility="published",
                     body="每年都要重看一次，每次看到的东西都不一样。")
    conn.commit()
    print("seed 完成。")


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "data/archive.db")
