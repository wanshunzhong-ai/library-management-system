"""演示数据：仅在库为空时写入，便于首次启动即可看到完整效果。"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from library import db, timeutil

logger = logging.getLogger(__name__)

BOOKS = [
    ("9787115428028", "Python 编程：从入门到实践", "Eric Matthes", "人民邮电出版社", "计算机", 2016, "A-01-03", 5),
    ("9787111544937", "深入理解计算机系统", "Randal E. Bryant", "机械工业出版社", "计算机", 2016, "A-01-05", 3),
    ("9787111213826", "算法导论", "Thomas H. Cormen", "机械工业出版社", "计算机", 2013, "A-02-01", 2),
    ("9787121362217", "重构：改善既有代码的设计", "Martin Fowler", "电子工业出版社", "计算机", 2019, "A-02-04", 4),
    ("9787115546081", "流畅的 Python", "Luciano Ramalho", "人民邮电出版社", "计算机", 2020, "A-02-07", 3),
    ("9787506365437", "活着", "余华", "作家出版社", "文学", 2012, "B-01-01", 6),
    ("9787020002207", "红楼梦", "曹雪芹", "人民文学出版社", "文学", 1996, "B-01-04", 4),
    ("9787532776634", "百年孤独", "加西亚·马尔克斯", "南海出版公司", "文学", 2017, "B-01-08", 3),
    ("9787544270878", "解忧杂货店", "东野圭吾", "南海出版公司", "文学", 2014, "B-02-02", 5),
    ("9787208061644", "万历十五年", "黄仁宇", "上海人民出版社", "历史", 2007, "C-01-02", 3),
    ("9787508647357", "人类简史", "尤瓦尔·赫拉利", "中信出版社", "历史", 2014, "C-01-05", 4),
    ("9787521701067", "枪炮、病菌与钢铁", "贾雷德·戴蒙德", "中信出版社", "历史", 2022, "C-01-09", 2),
    ("9787508660752", "国富论", "亚当·斯密", "中信出版社", "经济", 2016, "D-01-01", 2),
    ("9787521717716", "穷查理宝典", "彼得·考夫曼", "中信出版社", "经济", 2021, "D-01-03", 3),
    ("9787115479440", "经济学原理", "曼昆", "人民邮电出版社", "经济", 2018, "D-01-06", 4),
    ("9787544253994", "挪威的森林", "村上春树", "南海出版公司", "文学", 2011, "B-02-05", 3),
    ("9787540488069", "月亮与六便士", "毛姆", "湖南文艺出版社", "文学", 2018, "B-02-08", 4),
    ("9787115473899", "数学之美", "吴军", "人民邮电出版社", "科普", 2020, "E-01-02", 3),
    ("9787535732309", "时间简史", "史蒂芬·霍金", "湖南科学技术出版社", "科普", 2010, "E-01-04", 2),
    ("9787521708462", "宇宙的琴弦", "布莱恩·格林", "中信出版社", "科普", 2019, "E-01-07", 2),
    ("9787115373991", "代码整洁之道", "Robert C. Martin", "人民邮电出版社", "计算机", 2015, "A-03-01", 3),
    ("9787020104345", "平凡的世界", "路遥", "人民文学出版社", "文学", 2017, "B-03-01", 5),
    ("9787544291170", "白夜行", "东野圭吾", "南海出版公司", "文学", 2015, "B-03-04", 4),
    ("9787521701883", "原则", "瑞·达利欧", "中信出版社", "经济", 2018, "D-02-02", 3),
]

MEMBERS = [
    ("2024001", "李思远", "13800001111", "lisiyuan@example.com", "学生"),
    ("2024002", "王梦琪", "13800002222", "wangmengqi@example.com", "学生"),
    ("2024003", "张浩然", "13800003333", "zhanghaoran@example.com", "学生"),
    ("2024004", "陈雨欣", "13800004444", "chenyuxin@example.com", "学生"),
    ("2024005", "刘子墨", "13800005555", "liuzimo@example.com", "学生"),
    ("T202001", "赵启明", "13900006666", "zhaoqiming@example.com", "教师"),
    ("T202002", "孙丽华", "13900007777", "sunlihua@example.com", "教师"),
    ("T202003", "周建国", "13900008888", "zhoujianguo@example.com", "教师"),
    ("S2023001", "吴晓蓉", "13700009999", "wuxiaorong@example.com", "社会"),
    ("S2023002", "郑一鸣", "13700001010", "zhengyiming@example.com", "社会"),
    ("V2025001", "黄嘉怡", "13600001122", "huangjiayi@example.com", "VIP"),
    ("V2025002", "何俊杰", "13600003344", "hejunjie@example.com", "VIP"),
]


def is_empty() -> bool:
    return int(db.query_scalar("SELECT COUNT(*) FROM books") or 0) == 0


def seed(force: bool = False) -> bool:
    """写入演示数据；库非空时跳过。返回是否实际写入。"""
    if not force and not is_empty():
        logger.info("数据库已有数据，跳过演示数据初始化")
        return False

    rng = random.Random(20261010)
    now = timeutil.now()

    with db.transaction() as conn:
        if force:
            conn.execute("DELETE FROM loans")
            conn.execute("DELETE FROM books")
            conn.execute("DELETE FROM members")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('loans','books','members')")

        book_ids: list[tuple[int, int]] = []
        for isbn, title, author, publisher, category, year, location, total in BOOKS:
            cur = conn.execute(
                "INSERT INTO books (isbn, title, author, publisher, category, publish_year, location, "
                "                   total_copies, available_copies) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [isbn, title, author, publisher, category, year, location, total, total],
            )
            book_ids.append((int(cur.lastrowid), total))

        member_ids: list[int] = []
        for card_no, name, phone, email, member_type in MEMBERS:
            cur = conn.execute(
                "INSERT INTO members (card_no, name, phone, email, member_type, status, max_borrow) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                [card_no, name, phone, email, member_type, 5 if member_type == "学生" else 10],
            )
            member_ids.append(int(cur.lastrowid))

        # 构造借阅历史：约 45 条，覆盖「已归还 / 在借 / 逾期」三种状态
        open_counter: dict[int, int] = {}
        plans: list[tuple[int, int, int, int | None]] = []
        for _ in range(45):
            book_id, total = rng.choice(book_ids)
            if open_counter.get(book_id, 0) >= total:
                continue
            member_id = rng.choice(member_ids)
            borrowed_days_ago = rng.randint(1, 120)
            returned_after = None if rng.random() < 0.42 else rng.randint(1, min(30, borrowed_days_ago))
            if returned_after is None:
                open_counter[book_id] = open_counter.get(book_id, 0) + 1
            plans.append((book_id, member_id, borrowed_days_ago, returned_after))

        for book_id, member_id, borrowed_days_ago, returned_after in plans:
            borrowed_at = now - timedelta(days=borrowed_days_ago, hours=rng.randint(0, 8))
            due_at = timeutil.plus_days(borrowed_at, 30)
            if returned_after is None:
                returned_at = None
            else:
                returned_at = borrowed_at + timedelta(days=returned_after, hours=rng.randint(0, 6))
            conn.execute(
                "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, returned_at, renew_count, note) "
                "VALUES (?, ?, ?, ?, ?, ?, '')",
                [
                    book_id,
                    member_id,
                    timeutil.to_str(borrowed_at),
                    timeutil.to_str(due_at),
                    timeutil.to_str(returned_at) if returned_at else None,
                    rng.choice([0, 0, 0, 1]),
                ],
            )

        for book_id, open_count in open_counter.items():
            conn.execute(
                "UPDATE books SET available_copies = total_copies - ? WHERE id = ?",
                [open_count, book_id],
            )

    logger.info("演示数据写入完成", extra={"extra_fields": {"books": len(BOOKS), "members": len(MEMBERS), "loans": len(plans)}})
    return True
