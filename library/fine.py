"""逾期天数与罚金：**全系统唯一**的计算口径。

历史问题（评审 P1-4 / P1-5）：

1. **口径漂移**：借阅列表（``loans/service._decorate``）、读者中心（``reader/service`` 的 SQL）、
   概览（``dashboard``）三处各写一遍公式，改一处漏两处；
2. **罚金无上限**：罚金按天线性累加，一本书逾期数年可以罚出天价；
3. **自相矛盾**：「是否逾期」用时间点比较（``due < now``），「逾期天数」用整日差
   （``date`` 相减），于是应还当天时刻刚过时会出现「状态=逾期、天数 0、罚金 ¥0」。

本模块规定：

- **一律按整日口径**：``date(参考日) > date(应还日)`` 才算逾期，逾期天数 = 日期差。
  这样「状态为逾期」⇔「逾期天数 ≥ 1」恒成立，不会再出现逾期 0 天的记录。
- **两道封顶**（均为每笔借阅，0 表示不限制）：
  ``LIBRARY_FINE_MAX_DAYS`` 限制最长计息天数，``LIBRARY_FINE_CAP`` 限制单笔罚金金额。
- **唯一入口**：列表展示、读者中心汇总、概览估算都必须调用这里，不得各算各的。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

from config import get_config
from library import timeutil


def _as_date(value: Any) -> date | None:
    """把数据库时间串 / date / datetime 归一为 ``date``；无法解析返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    parsed = timeutil.parse(str(value))
    return parsed.date() if parsed else None


def overdue_days(due_at: Any, reference_at: Any = None) -> int:
    """整日口径的逾期天数。

    ``reference_at`` 缺省取当前时间；已归还的记录应显式传入 ``returned_at``。
    未逾期（含应还当天）一律返回 0，因此返回值 ≥ 1 时必然对应「逾期」状态。
    """
    due = _as_date(due_at)
    if due is None:
        return 0
    reference = _as_date(reference_at) if reference_at is not None else timeutil.now().date()
    if reference is None:
        return 0
    days = (reference - due).days
    return days if days > 0 else 0


def is_overdue(due_at: Any, reference_at: Any = None) -> bool:
    """是否逾期：与 :func:`overdue_days` 同口径（天数 ≥ 1）。"""
    return overdue_days(due_at, reference_at) > 0


def fine_for_days(
    days: int,
    *,
    fine_per_day: float | None = None,
    max_days: int | None = None,
    cap: float | None = None,
) -> float:
    """把逾期天数换算成罚金，并施加封顶。参数缺省时读取当前配置。"""
    cfg = get_config()
    per_day = cfg.fine_per_day if fine_per_day is None else fine_per_day
    limit_days = cfg.fine_max_days if max_days is None else max_days
    limit_money = cfg.fine_cap if cap is None else cap

    effective = max(0, int(days))
    if limit_days and limit_days > 0:
        effective = min(effective, int(limit_days))

    amount = round(effective * float(per_day), 2)
    if limit_money and limit_money > 0:
        amount = min(amount, round(float(limit_money), 2))
    return round(amount, 2)


def loan_settlement(due_at: Any, reference_at: Any = None) -> dict[str, Any]:
    """单笔借阅的结算结果：``{"days", "fine", "capped"}``。

    ``days`` 为真实逾期天数（不封顶，便于展示「逾期 N 天」）；
    ``capped`` 表示这笔罚金被 ``MAX_DAYS`` 或 ``CAP`` 截断。
    """
    cfg = get_config()
    days = overdue_days(due_at, reference_at)
    fine = fine_for_days(days)

    raw_amount = round(days * float(cfg.fine_per_day), 2)
    capped = fine < raw_amount
    return {"days": days, "fine": fine, "capped": capped}


def total_fine(loans: Iterable[Mapping[str, Any]]) -> float:
    """一批借阅记录的罚金合计（逐笔封顶后求和）。

    传入的每条记录需含 ``due_at``，已归还的另需含 ``returned_at``。
    汇总口径与列表逐行展示的罚金完全一致。
    """
    total = 0.0
    for row in loans:
        total += loan_settlement(row.get("due_at"), row.get("returned_at"))["fine"]
    return round(total, 2)


def total_overdue_days(loans: Iterable[Mapping[str, Any]]) -> int:
    """一批借阅记录的逾期天数合计（真实天数，不封顶）。"""
    return sum(overdue_days(row.get("due_at"), row.get("returned_at")) for row in loans)
