"""预约业务逻辑：读者排队预约 -> 到书通知 -> 借出完成。

设计要点：

* **只对「暂无可借复本」开放预约**。有可借复本时让读者直接到馆借阅，
  否则预约队列会充斥"本来就能马上借到"的无效单，到书通知也失去意义。
* **到书自动唤队**：还书回补库存后，把该书最早的一条 ``waiting`` 预约置为 ``ready``
  —— 这就是"到书通知"的落点（读者下次进入个人中心即可看到"已到书，请到馆借阅"）。
* **借出自动结单**：读者借到同一本书时，把其未结束预约置为 ``fulfilled``。
* 这两个联动都写在 ``library.loans.service`` 的借还路径里，且**失败只告警不抛出**
  —— 预约是增强功能，绝不能因为预约表出问题就让一次正常的借书 / 还书失败。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any, Mapping

from config import get_config
from library import db, timeutil
from library.books import repository as book_repo
from library.errors import BusinessRuleError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from library.loans import repository as loan_repo
from library.members import repository as member_repo
from library.reservations import repository as repo
from library.validation import optional_str, pagination

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "waiting": "排队中",
    "ready": "已到书",
    "fulfilled": "已借出",
    "cancelled": "已取消",
    "expired": "已过期",
}
MEMBER_STATUS_FILTERS = {"all", "open", "waiting", "ready", "fulfilled", "cancelled", "expired"}


def hold_deadline(ready_at: Any, hold_days: int | None = None) -> str | None:
    """到书后的取书截止时间（``ready_at + 保留天数``）。

    保留期是「到书那一刻起算」，不是「当天 23:59」——读者看到的倒计时必须与
    系统真正执行的过期判定用同一个算式，否则会出现「页面说到期了、系统还没过期」。
    """
    if not ready_at:
        return None
    try:
        moment = timeutil.parse(str(ready_at))
    except Exception:  # noqa: BLE001 - 历史脏数据不该让整个列表挂掉
        return None
    if moment is None:
        return None
    days = int(hold_days if hold_days is not None else get_config().reservation_hold_days)
    return timeutil.to_str(moment + timedelta(days=days))


def _decorate(row: Mapping[str, Any], *, member_id: int | None = None) -> dict[str, Any]:
    item = dict(row)
    status = str(item.get("status") or "")
    item["status_label"] = STATUS_LABELS.get(status, status)
    item["book_available"] = int(item.get("available_copies") or 0) > 0
    # 排队位次：仅排队中有意义（排在第几位，1 表示最前）
    if status == "waiting":
        item["queue_position"] = int(repo.waiting_ahead(int(item["book_id"]), int(item["id"]))) + 1
    else:
        item["queue_position"] = None
    owner = member_id is None or int(item.get("member_id") or 0) == int(member_id)
    # 读者端：能否自行取消（未结束即可取消）
    item["can_cancel"] = status in repo.OPEN_STATUSES and owner
    item["can_ready"] = status == "waiting"
    # 读者端：能否改约（把这条预约换到另一本书），只在读者自己的视图里给
    item["can_rebook"] = member_id is not None and owner and status in repo.OPEN_STATUSES

    # 取书保留期：只有「已到书」才有倒计时
    hold_days = int(get_config().reservation_hold_days)
    item["hold_days"] = hold_days
    deadline = hold_deadline(item.get("ready_at"), hold_days) if status == "ready" else None
    item["expires_at"] = deadline
    if deadline:
        try:
            left = (timeutil.parse(deadline).date() - date.today()).days
        except Exception:  # noqa: BLE001
            left = None
        item["hold_left_days"] = left
        item["hold_label"] = (
            f"请在 {deadline[:10]} 前到馆借阅"
            + (f"（剩 {left} 天）" if left is not None and left >= 0 else "（即将到期）")
        )
    else:
        item["hold_left_days"] = None
        item["hold_label"] = ""
    return item


def get_reservation(res_id: int, *, member_id: int | None = None) -> dict[str, Any]:
    row = repo.get(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    return _decorate(row, member_id=member_id)


# ---------------------------------------------------------------- 读者侧


def reserve(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """提交预约。校验与写入放在同一个 IMMEDIATE 事务里，避免并发下重复排队。

    INSERT 直接走事务内的 ``conn.execute``（而不是 ``db.execute``），后者自带事务，
    会在中途提交外层事务、破坏"读-判断-写"的原子性。
    """
    book_id = _require_int(payload, "book_id", label="图书")
    note = optional_str(payload, "note", label="备注", max_length=200)
    member_id = int(member["id"])

    with db.transaction(immediate=True) as conn:
        current = member_repo.get(member_id)
        if current is None:
            raise NotFoundError("读者账号不存在")
        if str(current.get("status") or "active") != "active":
            raise BusinessRuleError("账号已被冻结，暂时无法预约，请联系馆员")
        if int(current.get("card_lost") or 0):
            raise BusinessRuleError("借书证已挂失，请先解除挂失再预约")

        book = book_repo.get(book_id)
        if book is None:
            raise NotFoundError(f"图书 #{book_id} 不存在")
        if int(book.get("total_copies") or 0) <= 0:
            raise BusinessRuleError(f"《{book['title']}》已下架，无法预约")
        if loan_repo.find_open_loan(book_id, member_id) is not None:
            raise ConflictError(f"你已借阅《{book['title']}》，请先归还再预约")

        available = int(book.get("available_copies") or 0)
        if available > 0:
            raise BusinessRuleError(
                f"《{book['title']}》当前有 {available} 册可借，可直接到馆借阅，无需预约"
            )
        if repo.find_open(book_id, member_id) is not None:
            raise ConflictError(f"你已预约《{book['title']}》，请勿重复提交")

        try:
            cur = conn.execute(
                "INSERT INTO reservations (book_id, member_id, status, note) "
                "VALUES (?, ?, 'waiting', ?)",
                [book_id, member_id, note],
            )
            res_id = int(cur.lastrowid or 0)
            cur.close()
        except sqlite3.IntegrityError as exc:
            # 唯一索引 idx_reservations_open_unique 兜底：并发下重复预约
            raise ConflictError(f"你已预约《{book['title']}》，请勿重复提交") from exc

    logger.info(
        "读者提交预约",
        extra={"extra_fields": {"member_id": member_id, "book_id": book_id, "reservation_id": res_id}},
    )
    return get_reservation(res_id, member_id=member_id)


def cancel_my_reservation(member: Mapping[str, Any], res_id: int) -> dict[str, Any]:
    row = repo.raw(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    if int(row["member_id"]) != int(member["id"]):
        logger.warning(
            "读者尝试取消他人预约",
            extra={"extra_fields": {"member_id": member["id"], "reservation_id": res_id}},
        )
        raise ForbiddenError("只能取消本人的预约")
    if str(row["status"]) not in repo.OPEN_STATUSES:
        raise ConflictError("该预约已结束，无法取消")

    repo.close(res_id, "cancelled")
    logger.info("读者取消预约", extra={"extra_fields": {"member_id": member["id"], "reservation_id": res_id}})
    return get_reservation(res_id, member_id=member["id"])


def list_my_reservations(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    status = (query.get("status") or "all").strip()
    if status not in MEMBER_STATUS_FILTERS:
        raise ValidationError(
            f"status 只能是 {'/'.join(sorted(MEMBER_STATUS_FILTERS))}", details={"field": "status"}
        )
    # 惰性清理超期未取（幂等，通常 0 行）——读者看到的队列才是真实的
    sweep_expired()
    page, size = pagination(query)
    member_id = int(member["id"])
    total = repo.count_for_member(member_id, status)
    rows = repo.list_for_member(member_id, status, limit=size, offset=(page - 1) * size)
    items = [_decorate(row, member_id=member_id) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "open_total": repo.count_for_member(member_id, "open"),
    }


# ---------------------------------------------------------------- 馆员侧


def list_reservations(query: Mapping[str, Any]) -> dict[str, Any]:
    # 馆员打开队列时也顺带清理超期未取的预约，免得清单里躺着已经不作数的「已到书」
    sweep_expired()
    page, size = pagination(query)
    filters = {
        "status": (query.get("status") or "").strip(),
        "keyword": query.get("keyword", ""),
    }
    total = repo.count_all(filters)
    rows = repo.list_all(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_decorate(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "waiting_total": repo.count_all({"status": "waiting"}),
    }


def mark_ready(res_id: int) -> dict[str, Any]:
    """馆员手动通知到书（自动唤队之外的补充手段）。"""
    row = repo.raw(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    if str(row["status"]) != "waiting":
        raise BusinessRuleError("只有排队中的预约才能通知到书")
    repo.mark_ready(res_id)
    _notify_ready(res_id)
    return get_reservation(res_id)


def cancel_reservation(res_id: int) -> dict[str, Any]:
    row = repo.raw(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    if str(row["status"]) not in repo.OPEN_STATUSES:
        raise ConflictError("该预约已结束，无法取消")
    repo.close(res_id, "cancelled")
    return get_reservation(res_id)


# ---------------------------------------------------------------- 借还联动（供 loans.service 调用）


def notify(member_id: int, kind: str, title: str, **kwargs: Any) -> int:
    """转发到通知模块（懒加载，避免预约 ↔ 通知互相 import）。失败只告警。"""
    try:
        from library.notifications import service as notification_service  # noqa: PLC0415

        return notification_service.notify(member_id, kind, title, **kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("预约通知写入失败（已忽略）", exc_info=True)
        return 0


def _notify_ready(res_id: int) -> None:
    """给「已到书」的读者写一条站内通知（含取书截止时间）。"""
    row = repo.get(res_id)
    if row is None or str(row["status"]) != "ready":
        return
    cfg = get_config()
    deadline = hold_deadline(row.get("ready_at"), cfg.reservation_hold_days)
    tail = f"请在 {deadline[:10]} 前到馆办理借出，逾期未取将自动让给下一位。" if deadline else ""
    notify(
        int(row["member_id"]),
        "reservation_ready",
        f"预约到书：《{row['book_title']}》",
        body=f"你预约的图书已到馆，可到流通台办理借出。{tail}",
        link_view="reservations",
        ref_type="reservation",
        ref_id=res_id,
        dedupe_key=f"reservation_ready:res:{res_id}",
    )


def refill_ready(book_id: int) -> int:
    """把「已到书」的预约数量补到与当前可借复本数一致，返回新通知的条数。

    口径是 **ready 条数 ≤ 可借复本数**，而不是「每还一册就唤醒一个」。
    差别在取消 / 过期 / 重复调用这些场景：按次数唤醒会虚报「到书」（明明只有
    一册在架，却通知了两个人）。按现有条数补齐就天然幂等，重复调用不会多发。
    """
    book = book_repo.get(book_id)
    if book is None:
        return 0
    available = int(book.get("available_copies") or 0)
    promoted = 0
    while repo.count_status(book_id, "ready") < available:
        nxt = repo.next_waiting(book_id)
        if nxt is None or not repo.mark_ready(int(nxt["id"])):
            break
        logger.info(
            "预约到书已通知",
            extra={"extra_fields": {"reservation_id": nxt["id"], "book_id": book_id}},
        )
        _notify_ready(int(nxt["id"]))
        promoted += 1
    return promoted


def on_book_returned(book_id: int) -> None:
    """还书回补库存后唤队：把该书最早的排队预约置为「已到书」并通知读者。

    不占用库存——到书读者仍需自己到馆办理借出。整个流程包在 try/except 里：
    预约是增强功能，任何异常都只告警，绝不能影响还书本身是否成功。
    """
    try:
        refill_ready(book_id)
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("预约唤队失败（已忽略）", exc_info=True)


def on_book_borrowed(book_id: int, member_id: int) -> None:
    """借出成功后结单：把该读者对该书的未结束预约置为「已借出」。"""
    try:
        row = repo.find_open(book_id, member_id)
        if row is not None and repo.mark_fulfilled(int(row["id"])):
            logger.info(
                "预约已转为已借出",
                extra={"extra_fields": {"reservation_id": row["id"], "book_id": book_id, "member_id": member_id}},
            )
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("预约结单失败（已忽略）", exc_info=True)


# ---------------------------------------------------------------- 取书保留期


def sweep_expired() -> int:
    """把超期未取的「已到书」预约置为过期、通知读者，并把名额让给下一位。

    惰性触发：读者打开个人中心、或馆员打开预约队列时跑一次。系统没有定时任务，
    所以「过期」是在下一次有人查看时落地的——这不影响正确性（库存与队列都不会
    被超期预约卡住，因为借出时按实际在架册数判定），只是页面上不会在半夜自动翻状态。
    """
    cfg = get_config()
    expired = repo.list_expired_ready(int(cfg.reservation_hold_days))
    handled = 0
    for row in expired:
        res_id = int(row["id"])
        if not repo.close(res_id, "expired"):
            continue  # 并发下已被处理
        handled += 1
        logger.info("预约取书超期已释放", extra={"extra_fields": {"reservation_id": res_id}})
        notify(
            int(row["member_id"]),
            "reservation_expired",
            f"预约已过期：《{row['book_title']}》",
            body=(
                f"你预约的图书到馆后未在 {int(cfg.reservation_hold_days)} 天内办理借出，"
                "预约已自动取消，名额已让给下一位读者。如仍需借阅可重新预约。"
            ),
            link_view="reservations",
            ref_type="reservation",
            ref_id=res_id,
            dedupe_key=f"reservation_expired:res:{res_id}",
        )
        # 让出来的名额立刻补位（refill 按 ready 条数补齐，天然幂等）
        try:
            refill_ready(int(row["book_id"]))
        except Exception:  # noqa: BLE001
            logger.warning("过期让位后的补位失败（已忽略）", exc_info=True)
    return handled


def rebook(member: Mapping[str, Any], res_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """读者改约：把一条未结束的预约换到另一本书。

    「取消再重排」对读者是两步且中间可能被人插队，所以这里做成一个动作，
    并且**关旧单 + 开新单在同一个 IMMEDIATE 事务内**完成——否则中途失败会留下
    「旧的没了、新的没建成」的空档。
    """
    new_book_id = _require_int(payload, "book_id", label="图书")
    member_id = int(member["id"])
    note = optional_str(payload, "note", label="备注", max_length=200)

    with db.transaction(immediate=True) as conn:
        current = member_repo.get(member_id)
        if current is None:
            raise NotFoundError("读者账号不存在")
        if str(current.get("status") or "active") != "active":
            raise BusinessRuleError("账号已被冻结，暂时无法改约，请联系馆员")
        if int(current.get("card_lost") or 0):
            raise BusinessRuleError("借书证已挂失，请先解除挂失再改约")

        old = repo.raw(res_id)
        if old is None:
            raise NotFoundError(f"预约 #{res_id} 不存在")
        if int(old["member_id"]) != member_id:
            logger.warning(
                "读者尝试改约他人预约",
                extra={"extra_fields": {"member_id": member_id, "reservation_id": res_id}},
            )
            raise ForbiddenError("只能修改本人的预约")
        if str(old["status"]) not in repo.OPEN_STATUSES:
            raise ConflictError("该预约已结束，无法改约")
        if int(old["book_id"]) == new_book_id:
            raise ConflictError("新预约的书与当前是同一本，无需改约")

        book = book_repo.get(new_book_id)
        if book is None:
            raise NotFoundError(f"图书 #{new_book_id} 不存在")
        if int(book.get("total_copies") or 0) <= 0:
            raise BusinessRuleError(f"《{book['title']}》已下架，无法预约")
        if loan_repo.find_open_loan(new_book_id, member_id) is not None:
            raise ConflictError(f"你已借阅《{book['title']}》，请先归还再预约")
        available = int(book.get("available_copies") or 0)
        if available > 0:
            raise BusinessRuleError(
                f"《{book['title']}》当前有 {available} 册可借，可直接到馆借阅，无需预约"
            )
        if repo.find_open(new_book_id, member_id) is not None:
            raise ConflictError(f"你已预约《{book['title']}》，请勿重复提交")

        if not repo.close_with(conn, res_id, "cancelled"):
            raise ConflictError("该预约刚被处理，请刷新后重试")
        try:
            new_id = repo.insert_with(conn, book_id=new_book_id, member_id=member_id, note=note)
        except sqlite3.IntegrityError as exc:  # 唯一索引兜底
            raise ConflictError(f"你已预约《{book['title']}》，请勿重复提交") from exc

    logger.info(
        "读者改约",
        extra={"extra_fields": {"member_id": member_id, "from": res_id, "to": new_id, "book_id": new_book_id}},
    )
    return {"closed_id": res_id, "reservation": get_reservation(new_id, member_id=member_id)}


def _require_int(payload: Mapping[str, Any], key: str, *, label: str) -> int:
    value = payload.get(key)
    if value in (None, ""):
        raise ValidationError(f"{label}不能为空", details={"field": key})
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} ID 必须是整数", details={"field": key}) from exc
    if number <= 0:
        raise ValidationError(f"{label} ID 非法", details={"field": key})
    return number
