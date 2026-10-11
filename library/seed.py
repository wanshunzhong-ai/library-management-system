"""演示数据：仅在库为空时写入，便于首次启动即可看到完整效果。"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from library import barcode, db, timeutil
from library.auth.passwords import hash_password, initial_password
from library.demo_catalog import BOOKS, DESCRIPTIONS_BY_ISBN

logger = logging.getLogger(__name__)

MEMBERS = [
    ("2024001", "李思远", "13800001111", "lisiyuan@example.com", "学生"),
    ("2024002", "王梦琪", "13800002222", "wangmengqi@example.com", "学生"),
    ("2024003", "张浩然", "13800003333", "zhanghaoran@example.com", "学生"),
    ("2024004", "陈雨欣", "13800004444", "chenyuxin@example.com", "学生"),
    ("2024005", "刘子墨", "13800005555", "liuzimo@example.com", "学生"),
    ("2024006", "沈知言", "13800006666", "shenzhiyan@example.com", "学生"),
    ("2024007", "苏婉清", "13800007777", "suwanqing@example.com", "学生"),
    ("2024008", "顾青山", "13800008888", "guqingshan@example.com", "学生"),
    ("2024009", "韩雪松", "13800009999", "hanxuesong@example.com", "学生"),
    ("2024010", "林晚秋", "13800001011", "linwanqiu@example.com", "学生"),
    ("T202001", "赵启明", "13900006666", "zhaoqiming@example.com", "教师"),
    ("T202002", "孙丽华", "13900007777", "sunlihua@example.com", "教师"),
    ("T202003", "周建国", "13900008888", "zhoujianguo@example.com", "教师"),
    ("T202004", "马文韬", "13900001012", "mawentao@example.com", "教师"),
    ("S2023001", "吴晓蓉", "13700009999", "wuxiaorong@example.com", "社会"),
    ("S2023002", "郑一鸣", "13700001010", "zhengyiming@example.com", "社会"),
    ("S2023003", "方雨桐", "13700001314", "fangyutong@example.com", "社会"),
    ("V2025001", "黄嘉怡", "13600001122", "huangjiayi@example.com", "VIP"),
    ("V2025002", "何俊杰", "13600003344", "hejunjie@example.com", "VIP"),
    ("V2025003", "秦朗", "13600001516", "qinlang@example.com", "VIP"),
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
        for isbn, title, author, publisher, category, year, location, total, description in BOOKS:
            cur = conn.execute(
                "INSERT INTO books (isbn, title, author, publisher, category, publish_year, location, "
                "                   description, total_copies, available_copies) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [isbn, title, author, publisher, category, year, location, description, total, total],
            )
            book_id = int(cur.lastrowid)
            # 馆藏条码：默认 BK + 7 位序号，与图书 id 对齐
            conn.execute("UPDATE books SET barcode = ? WHERE id = ?", [barcode.generate(book_id), book_id])
            book_ids.append((book_id, total))

        member_ids: list[int] = []
        for card_no, name, phone, email, member_type in MEMBERS:
            # 演示数据同步开通门户账号，初始密码 = 证号后 6 位
            cur = conn.execute(
                "INSERT INTO members (card_no, name, phone, email, member_type, status, max_borrow, password_hash) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                [
                    card_no,
                    name,
                    phone,
                    email,
                    member_type,
                    5 if member_type == "学生" else 10,
                    hash_password(initial_password(card_no)),
                ],
            )
            member_ids.append(int(cur.lastrowid))

        # 构造借阅历史：约 85 条，覆盖「已归还 / 在借 / 逾期」三种状态
        open_counter: dict[int, int] = {}
        # 同一读者不能同时借同一本书两次（业务规则 + 唯一索引都禁止），生成时先避开
        open_pairs: set[tuple[int, int]] = set()
        plans: list[tuple[int, int, int, int | None]] = []
        for _ in range(85):
            book_id, total = rng.choice(book_ids)
            if open_counter.get(book_id, 0) >= total:
                continue
            member_id = rng.choice(member_ids)
            borrowed_days_ago = rng.randint(1, 150)
            returned_after = None if rng.random() < 0.42 else rng.randint(1, min(30, borrowed_days_ago))
            if returned_after is None:
                if (book_id, member_id) in open_pairs:
                    continue
                open_pairs.add((book_id, member_id))
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

    logger.info(
        "演示数据写入完成",
        extra={"extra_fields": {"books": len(BOOKS), "members": len(MEMBERS), "loans": len(plans)}},
    )
    return True


def ensure_content() -> int:
    """为演示书目回填简介（仅补空值，幂等）。

    老库升级后已有书目没有简介，这里按 ISBN 与演示目录比对补齐，
    只影响简介为空的记录，不会覆盖馆员已录入的内容。返回回填条数。
    """
    updated = 0
    with db.transaction() as conn:
        for isbn, description in DESCRIPTIONS_BY_ISBN.items():
            cur = conn.execute(
                "UPDATE books SET description = ?, updated_at = datetime('now', 'localtime') "
                " WHERE isbn = ? AND (description IS NULL OR description = '')",
                [description, isbn],
            )
            updated += max(0, int(cur.rowcount or 0))

    if updated:
        logger.info("演示简介回填完成", extra={"extra_fields": {"books": updated}})
    return updated
