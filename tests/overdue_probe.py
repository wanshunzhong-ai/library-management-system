"""逾期口径与罚金封顶的对照实验：在「修复前 / 修复后」两套代码上跑同样场景。

用法：
  git stash push -- library/loans/service.py library/loans/repository.py   # 回到修复前
  python tests/overdue_probe.py
  git stash pop                                                            # 回到修复后
  python tests/overdue_probe.py

判据（修复前应全部失败，修复后应全部通过）：
  1. 应还「今天」且时刻已过的记录，不应被判为逾期（旧口径会判逾期但天数为 0）；
  2. 列表里不应存在「状态 = 逾期 但 逾期天数 = 0」的自相矛盾记录；
  3. 逾期 200 天的罚金应被配置上限截断，而不是 200 × 日罚金。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, get_config, set_config  # noqa: E402
from library import db, timeutil  # noqa: E402
from library.loans import service as loan_service  # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="overdue-probe-")
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

    cfg = get_config()
    per_day = cfg.fine_per_day
    cap = cfg.fine_cap
    member_id = db.execute(
        "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
        "VALUES ('OP0001', '口径探针', '学生', 'active', 5)"
    )
    today_midnight = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def add_loan(tag: str, due_at) -> int:
        book_id = db.execute(
            "INSERT INTO books (isbn, title, category, total_copies, available_copies) "
            "VALUES (?, ?, '探针', 1, 0)",
            [f"9799000{len(tag):05d}", f"探针书 {tag}"],
        )
        return db.execute(
            "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, returned_at, renew_count, note) "
            "VALUES (?, ?, ?, ?, NULL, 0, ?)",
            [
                book_id,
                member_id,
                timeutil.to_str(timeutil.now() - timedelta(days=300)),
                timeutil.to_str(due_at),
                f"probe-{tag}",
            ],
        )

    add_loan("today", today_midnight)                     # 应还当天、时刻已过
    add_loan("long", timeutil.now() - timedelta(days=200))  # 逾期 200 天

    items = loan_service.list_loans(
        {"member_id": member_id, "status": "all", "page": 1, "page_size": 100}
    )["items"]
    by_note = {item["note"]: item for item in items}

    verdicts = []

    # 判据 1：应还当天不该被判成逾期
    today_item = by_note.get("probe-today", {})
    good = today_item.get("status") == "borrowed" and int(today_item.get("overdue_days") or 0) == 0
    verdicts.append(good)
    print("[判据1] 应还「今天」（时刻已过）的借阅状态")
    print(f"        实际 status={today_item.get('status')}  overdue_days={today_item.get('overdue_days')}"
          "   期望：borrowed / 0")
    print(f"        {'✅ 正确：整日口径，当天不算逾期' if good else '❌ 矛盾：判为逾期却算出 0 天'}")

    # 判据 2：不存在「逾期但 0 天」的记录
    contradictions = [
        item for item in items
        if item.get("status") == "overdue" and int(item.get("overdue_days") or 0) == 0
    ]
    good = not contradictions
    verdicts.append(good)
    print("[判据2] 列表中「状态 = 逾期 但 天数 = 0」的矛盾记录数")
    print(f"        矛盾记录={len(contradictions)}   期望：0")
    print(f"        {'✅ 正确：状态与天数同口径' if good else '❌ 存在 ' + str(len(contradictions)) + ' 条自相矛盾记录'}")

    # 判据 3：超长逾期罚金被封顶
    long_item = by_note.get("probe-long", {})
    raw = round(200 * per_day, 2)
    fine = float(long_item.get("fine") or 0)
    good = abs(fine - round(min(raw, cap), 2)) < 1e-6
    verdicts.append(good)
    print("[判据3] 逾期 200 天的罚金")
    print(f"        实际罚金=¥{fine:.2f}  未封顶金额=¥{raw:.2f}  配置上限=¥{cap:.2f}"
          "   期望：取较小者")
    print(f"        {'✅ 正确：罚金已被封顶' if good else '❌ 未封顶：罚金随天数无限累加'}")

    passed = sum(1 for v in verdicts if v)
    print(f"\n对照结果：{passed}/{len(verdicts)} 项正确")
    return 0 if passed == len(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
