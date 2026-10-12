"""站内通知业务逻辑。

## 为什么是「惰性生成」而不是定时推送

本系统没有短信 / 邮件网关，也没有定时任务框架。真正的「主动推送」需要其中至少
一样，硬造一个进程内调度器只会带来多进程重复触发、进程重启丢任务等一堆新问题。
所以这里的口径是：

* **读者打开个人中心（或打开消息中心）时按需扫描**，把「应还快到期的」「已经逾期的」
  补写成通知——语义是「只要你打开就一定看得到该看到的」；
* 触发点在 `library/notifications/service.sweep_member`，由 `library.reader.service.me`
  与通知列表接口调用；
* 代价是**不打开就不会被推送到**。这一点在 README 的「已知限制」里明确写出，
  不把「站内通知」包装成「主动提醒」。

到书 / 预约过期由预约模块在状态流转处直接调用本模块写入（那里本来就有确定的触发点，
不必等扫描）。

所有写入都走 `dedupe_key` 幂等键，因此扫描可以放心地每次请求都跑。

## 失败只告警

通知是增强功能。任何写入失败都只记日志，绝不向上抛——不能因为通知表出问题就让
一次正常的借书 / 还书 / 改密失败。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from library import db
from library.errors import NotFoundError, ValidationError
from library.notifications import repository as repo
from library.validation import pagination, require_str

logger = logging.getLogger(__name__)

KIND_LABELS = {
    "reservation_ready": "预约到书",
    "reservation_expired": "预约过期",
    "due_soon": "即将到期",
    "overdue": "逾期提醒",
    "fine": "滞纳金",
    "profile": "资料变更",
    "book_request": "荐购处理",
    "system": "馆员通知",
}
# 读者侧可筛选的类型（system 之外都是系统按业务事件生成的）
MEMBER_KINDS = tuple(KIND_LABELS)
# 前端点通知要跳到的锚点
LINK_VIEWS = ("loans", "reservations", "fines", "bookshelf", "reviews", "requests", "profile")


def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    kind = str(item.get("kind") or "")
    item["kind_label"] = KIND_LABELS.get(kind, kind)
    item["read"] = item.get("read_at") is not None
    return item


def notify(
    member_id: int,
    kind: str,
    title: str,
    *,
    body: str = "",
    link_view: str = "",
    ref_type: str = "",
    ref_id: int | None = None,
    dedupe_key: str = "",
) -> int:
    """写一条通知（幂等）。返回新记录 id，跳过或失败时为 0。"""
    if kind not in KIND_LABELS:
        logger.warning("未知通知类型，已忽略", extra={"extra_fields": {"kind": kind}})
        return 0
    try:
        new_id = repo.insert_if_absent(
            {
                "member_id": int(member_id),
                "kind": kind,
                "title": title,
                "body": body,
                "link_view": link_view if link_view in LINK_VIEWS else "",
                "ref_type": ref_type,
                "ref_id": ref_id,
                "dedupe_key": dedupe_key,
            }
        )
    except Exception:  # noqa: BLE001 - 通知是增强功能，绝不拖垮主业务
        logger.warning("写入通知失败（已忽略）", exc_info=True)
        return 0
    if new_id:
        # 邮件出站钩子：读者留了邮箱且未退订时，同一事件同步入队一封邮件。
        # 入队与发送分离且全程吞异常——它失败不影响站内通知已经落地的结果。
        try:
            from library.notifications import outbound  # noqa: PLC0415

            outbound.enqueue_from_notification(
                {"member_id": member_id, "title": title, "body": body, "dedupe_key": dedupe_key, "id": new_id}
            )
        except Exception:  # noqa: BLE001
            logger.warning("邮件出站入队失败（已忽略）", exc_info=True)
    return new_id


# ---------------------------------------------------------------- 惰性扫描


def sweep_member(member_id: int) -> dict[str, int]:
    """扫描该读者的在借记录，补写「即将到期」与「逾期」通知。

    幂等：同一笔借阅的同一个应还日期只发一次（续借后应还日期变了才会再发一条）。
    通知正文刻意不写「已逾期 N 天」这类会随时间失真的数字，只写应还日期。

    逾期判定复用 ``library.fine`` 的**整日口径**（当前日期 > 应还日期才算逾期），
    否则会出现「页面上说逾期、罚金却算 0」这种两套口径打架的情况。
    """
    from library import timeutil  # noqa: PLC0415

    member_id = int(member_id)
    created = {"due_soon": 0, "overdue": 0}
    try:
        cfg = _config()
        ahead = int(cfg.notify_due_soon_days)
        rows = db.query_all(
            """
            SELECT l.id, l.due_at, b.title AS book_title
              FROM loans l
              JOIN books b ON b.id = l.book_id
             WHERE l.member_id = ? AND l.returned_at IS NULL
            """,
            [member_id],
        )
        today = timeutil.now().date()
        for row in rows:
            loan_id = int(row["id"])
            due_day = str(row.get("due_at") or "")[:10]
            due_date = _as_date(due_day)
            if due_date is None:
                continue
            overdue_days = (today - due_date).days
            title = str(row.get("book_title") or "")
            if overdue_days >= 1:
                new_id = notify(
                    member_id,
                    "overdue",
                    f"《{title}》已超过应还日期",
                    body=f"应还日期 {due_day}。逾期期间会持续产生滞纳金，请尽快归还。",
                    link_view="loans",
                    ref_type="loan",
                    ref_id=loan_id,
                    dedupe_key=f"overdue:loan:{loan_id}:{due_day}",
                )
                if new_id:
                    created["overdue"] += 1
            elif ahead > 0 and (due_date - today).days <= ahead:
                new_id = notify(
                    member_id,
                    "due_soon",
                    f"《{title}》即将到期",
                    body=f"应还日期 {due_day}。如需继续阅读，可在「我的借阅」里自助续借。",
                    link_view="loans",
                    ref_type="loan",
                    ref_id=loan_id,
                    dedupe_key=f"due_soon:loan:{loan_id}:{due_day}",
                )
                if new_id:
                    created["due_soon"] += 1
    except Exception:  # noqa: BLE001 - 见模块 docstring
        logger.warning("通知惰性扫描失败（已忽略）", exc_info=True)
    return created


def sweep_all_members() -> dict[str, int]:
    """对**所有有在借记录的读者**跑一轮扫描（后台定时线程用）。

    只遍历「当前有未归还借阅」的读者：没有在借的人不可能产生到期 / 逾期提醒，
    遍历他们纯属浪费。每一轮都是幂等的（``dedupe_key``），因此定时器重复触发
    不会给读者刷出重复消息。
    """
    totals = {"members": 0, "due_soon": 0, "overdue": 0}
    try:
        rows = db.query_all("SELECT DISTINCT member_id FROM loans WHERE returned_at IS NULL")
    except Exception:  # noqa: BLE001 - 增强功能，失败只告警
        logger.warning("通知全量扫描读取读者失败（已忽略）", exc_info=True)
        return totals
    for row in rows:
        created = sweep_member(int(row["member_id"]))
        totals["members"] += 1
        totals["due_soon"] += created.get("due_soon", 0)
        totals["overdue"] += created.get("overdue", 0)
    return totals


def _as_date(value: str):
    """把 ``YYYY-MM-DD`` 解析成 date；解析不了返回 None（历史脏数据直接跳过）。"""
    from datetime import date as _date  # noqa: PLC0415
    from library import timeutil  # noqa: PLC0415

    if not value:
        return None
    try:
        moment = timeutil.parse(value)
    except Exception:  # noqa: BLE001
        return None
    if moment is None:
        return None
    return moment.date() if hasattr(moment, "date") else _date(moment.year, moment.month, moment.day)


def _config():
    from config import get_config  # noqa: PLC0415

    return get_config()


# ---------------------------------------------------------------- 读者侧


def list_my_notifications(member_id: int, query: Mapping[str, Any]) -> dict[str, Any]:
    """我的通知：先惰性扫描一次，保证「打开就能看到该看到的」。"""
    member_id = int(member_id)
    sweep_member(member_id)

    kind = (query.get("kind") or "all").strip()
    if kind not in {"all", *MEMBER_KINDS}:
        raise ValidationError("通知类型不合法", details={"field": "kind"})
    unread_only = str(query.get("unread") or "").strip() in {"1", "true", "yes"}
    page, size = pagination(query)

    total = repo.count_for_member(member_id, unread_only=unread_only, kind=kind)
    rows = repo.list_for_member(
        member_id,
        unread_only=unread_only,
        kind=kind,
        limit=size,
        offset=(page - 1) * size,
    )
    return {
        "items": [_decorate(row) for row in rows],
        "total": total,
        "unread": repo.count_for_member(member_id, unread_only=True),
        "kind_counts": repo.kind_counts(member_id),
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def mark_read(member_id: int, notification_id: int) -> dict[str, Any]:
    member_id = int(member_id)
    row = repo.get(notification_id)
    if row is None or int(row["member_id"]) != member_id:
        # 不区分「不存在」与「不是你的」，避免用 id 探测他人消息是否存在
        raise NotFoundError(f"通知 #{notification_id} 不存在")
    repo.mark_read(member_id, notification_id)
    return {"id": notification_id, "unread": repo.count_for_member(member_id, unread_only=True)}


def mark_all_read(member_id: int) -> dict[str, Any]:
    member_id = int(member_id)
    changed = repo.mark_all_read(member_id)
    return {"marked": changed, "unread": 0}


def unread_count(member_id: int) -> int:
    return repo.count_for_member(int(member_id), unread_only=True)


def send_system(member_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """馆员给某位读者发一条站内通知（催还、预约取书提醒等场景）。"""
    title = require_str(payload, "title", label="通知标题", max_length=80)
    body = str(payload.get("body") or "").strip()[:500]
    link_view = str(payload.get("link_view") or "").strip()
    if link_view and link_view not in LINK_VIEWS:
        raise ValidationError("跳转目标不合法", details={"field": "link_view"})

    new_id = repo.insert_if_absent(
        {
            "member_id": int(member_id),
            "kind": "system",
            "title": title,
            "body": body,
            "link_view": link_view,
            "ref_type": "",
            "ref_id": None,
            # 馆员手动发的消息不设幂等键：同样的话再说一遍是有意义的
            "dedupe_key": "",
        }
    )
    logger.info(
        "馆员发送站内通知",
        extra={"extra_fields": {"member_id": member_id, "operator": operator, "notification_id": new_id}},
    )
    try:
        from library.notifications import outbound  # noqa: PLC0415

        outbound.enqueue_from_notification({"member_id": member_id, "title": title, "body": body, "id": new_id})
    except Exception:  # noqa: BLE001 - 邮件渠道失败不影响站内通知
        logger.warning("邮件出站入队失败（已忽略）", exc_info=True)
    return _decorate(repo.get(new_id) or {})
