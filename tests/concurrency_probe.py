"""并发写路径对照实验：在「修复前 / 修复后」两套代码上跑同样的并发场景。

用法：
  git stash push -- library/db.py library/loans/service.py   # 回到修复前
  python .preview/concurrency_probe.py
  git stash pop                                              # 回到修复后
  python .preview/concurrency_probe.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, set_config  # noqa: E402
from library import db  # noqa: E402
from library.loans import service as loan_service  # noqa: E402


def run_concurrently(fn, times):
    barrier = threading.Barrier(times)
    results = [None] * times

    def worker(slot):
        db.get_conn()
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass
        try:
            results[slot] = ("ok", fn(slot))
        except Exception as exc:  # noqa: BLE001
            results[slot] = ("err", exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(times)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def ok_count(results):
    return sum(1 for status, _ in results if status == "ok")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="concurrency-probe-")
    set_config(
        Config(
            db_path=os.path.join(tmp, "probe.db"),
            host="127.0.0.1",
            port=8000,
            debug=False,
            loan_days=30,
            max_renew=2,
            fine_per_day=0.5,
            default_max_borrow=5,
            log_level="ERROR",
            secret_key="probe",
        )
    )
    db.init_db()

    def add_member(card, max_borrow):
        return db.execute(
            "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
            "VALUES (?, ?, '学生', 'active', ?)",
            [card, f"探针{card}", max_borrow],
        )

    def add_book(isbn, copies):
        return db.execute(
            "INSERT INTO books (isbn, title, category, total_copies, available_copies) "
            "VALUES (?, ?, '探针', ?, ?)",
            [isbn, f"探针书{isbn[-3:]}", copies, copies],
        )

    verdicts = []

    # 场景 1：同一读者（上限 1 册）并发借两本不同的书
    m1 = add_member("P0001", 1)
    bx, by = add_book("880000000001", 1), add_book("880000000002", 1)
    results = run_concurrently(
        lambda slot: loan_service.borrow({"book_id": [bx, by][slot], "member_id": m1}), 2
    )
    open_a = int(
        db.query_scalar(
            "SELECT COUNT(*) FROM loans WHERE member_id = ? AND returned_at IS NULL", [m1]
        )
    )
    good = ok_count(results) == 1 and open_a == 1
    verdicts.append(good)
    print("[场景1] 同一读者并发借两本不同的书（可借上限 1 册）")
    print(f"        成功次数={ok_count(results)}  在借记录数={open_a}   期望：成功 1 次 / 在借 1 条")
    print(f"        {'✅ 正确' if good else '❌ 竞态：突破可借上限，借出了 ' + str(open_a) + ' 册'}")

    # 场景 2：同一读者并发借同一本书（库存 3 册，6 线程）
    m2 = add_member("P0002", 5)
    bz = add_book("880000000003", 3)
    results = run_concurrently(
        lambda slot: loan_service.borrow({"book_id": bz, "member_id": m2}), 6
    )
    dup = int(
        db.query_scalar(
            "SELECT COUNT(*) FROM loans WHERE book_id = ? AND member_id = ? AND returned_at IS NULL",
            [bz, m2],
        )
    )
    good = ok_count(results) == 1 and dup == 1
    verdicts.append(good)
    print("[场景2] 同一读者并发借同一本书（6 线程）")
    print(f"        成功次数={ok_count(results)}  同书在借记录数={dup}   期望：成功 1 次 / 记录 1 条")
    print(f"        {'✅ 正确' if good else '❌ 竞态：同一本书产生 ' + str(dup) + ' 条重复在借记录'}")

    # 场景 3：并发还同一笔借阅（6 线程）
    m3 = add_member("P0003", 5)
    b4 = add_book("880000000004", 2)
    loan = loan_service.borrow({"book_id": b4, "member_id": m3})
    before = db.query_one("SELECT total_copies, available_copies FROM books WHERE id = ?", [b4])
    results = run_concurrently(lambda slot: loan_service.return_book(loan["id"]), 6)
    after = db.query_one("SELECT total_copies, available_copies FROM books WHERE id = ?", [b4])
    good = (
        ok_count(results) == 1
        and after["available_copies"] == before["available_copies"] + 1
        and after["available_copies"] <= after["total_copies"]
    )
    verdicts.append(good)
    print("[场景3] 并发还同一笔借阅（6 线程）")
    print(
        f"        成功次数={ok_count(results)}  可借数 {before['available_copies']} -> "
        f"{after['available_copies']}（总册数 {after['total_copies']}）   期望：成功 1 次 / 库存只 +1"
    )
    print(f"        {'✅ 正确' if good else '❌ 竞态：库存被重复回补，可借数超过总册数'}")

    # 场景 4：并发续借同一笔借阅（8 线程，上限 2 次）
    m4 = add_member("P0004", 5)
    b5 = add_book("880000000005", 1)
    loan = loan_service.borrow({"book_id": b5, "member_id": m4})
    results = run_concurrently(lambda slot: loan_service.renew(loan["id"]), 8)
    renew_count = int(db.query_scalar("SELECT renew_count FROM loans WHERE id = ?", [loan["id"]]))
    good = renew_count <= 2
    verdicts.append(good)
    print("[场景4] 并发续借同一笔借阅（8 线程，上限 2 次）")
    print(f"        成功次数={ok_count(results)}  续借次数={renew_count}   期望：续借次数 <= 2")
    print(f"        {'✅ 正确' if good else '❌ 竞态：续借次数突破上限'}")

    print(f"\n小结：{sum(verdicts)}/4 个场景通过")
    return 0 if all(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
