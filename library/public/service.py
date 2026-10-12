"""读者门户业务逻辑（含免登录的公告、办证申请与借阅速查）。"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from config import get_config
from library import db, fine, timeutil
from library.auth.passwords import verify_password
from library.auth.throttle import (
    clear_login_locks,
    record_login_failure,
    remaining_login_lock,
)
from library.errors import BusinessRuleError, UnauthorizedError, ValidationError
from library.members import repository as member_repo
from library.public import repository as repo
from library.validation import pagination

logger = logging.getLogger(__name__)


def _decorate(book: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(book)
    available = int(item.get("available_copies") or 0)
    total = int(item.get("total_copies") or 0)
    item["available"] = available > 0
    if available <= 0:
        item["status_label"] = "全部借出"
    elif available == 1:
        item["status_label"] = "仅剩 1 册"
    else:
        item["status_label"] = f"可借 {available} 册"
    item["availability_ratio"] = round(available / total, 3) if total else 0.0
    # 评分先给默认值，由 _attach_ratings 批量回填（单本书走 _decorate 时就是 0 条）
    item.setdefault("rating_avg", 0.0)
    item.setdefault("rating_count", 0)
    return item


def _attach_ratings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给一批书目补上评分聚合。

    门户首页与检索页一次要渲染几十张卡，逐本查评分就是典型的 N+1；
    这里一次性把 book_id 收集起来批量取，再回填到每一条上。
    评价口径只有一处（``library.engagement.repository.rating_map``，只统计已发布），
    门户 / 读者端 / 后台看到的是同一个数字。
    """
    if not items:
        return items
    from library.engagement import repository as engagement_repo  # noqa: PLC0415

    ratings = engagement_repo.rating_map([int(item["id"]) for item in items if item.get("id")])
    for item in items:
        stats = ratings.get(int(item["id"])) if item.get("id") else None
        item["rating_avg"] = float(stats["avg"]) if stats else 0.0
        item["rating_count"] = int(stats["total"]) if stats else 0
    return items


def overview() -> dict[str, Any]:
    cfg = get_config()
    from library.announcements import service as announcement_service  # noqa: PLC0415

    hours = announcement_service.public_hours()
    return {
        "library": {
            "name": cfg.library_name,
            "slogan": cfg.library_slogan,
        },
        "stats": {
            **repo.stats(),
            "reader_count": repo.reader_count(),
            "active_reader_count": repo.active_reader_count(),
            "loan_total": repo.loan_total(),
        },
        "rules": {
            "loan_days": cfg.loan_days,
            "max_renew": cfg.max_renew,
            "fine_per_day": cfg.fine_per_day,
            "fine_max_days": cfg.fine_max_days,
            "fine_cap": cfg.fine_cap,
            "max_borrow": cfg.default_max_borrow,
        },
        "categories": repo.categories(),
        # 一次性把门户要展示的三块附加信息带回，避免首屏多次往返
        "announcements": announcement_service.public_list()["items"],
        "hours": hours,
        "locations": repo.locations(),
        "location_names": repo.location_names(),
        "application_options": application_form_options(),
        "recommended": _attach_ratings([_decorate(b) for b in repo.recommended(6)]),
        "new_arrivals": _attach_ratings([_decorate(b) for b in repo.new_arrivals(6)]),
    }


def search(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query, default_size=8)
    filters = {
        "keyword": query.get("keyword", ""),
        "category": query.get("category", ""),
        "location": query.get("location", ""),
        "available_only": str(query.get("available_only", "")).lower() in {"1", "true", "yes"},
    }
    total = repo.count(filters)
    items = _attach_ratings([_decorate(b) for b in repo.search(filters, limit=size, offset=(page - 1) * size)])
    keyword = str(filters["keyword"] or "").strip()
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "keyword": keyword,
        "location": str(filters["location"] or "").strip(),
        "available_only": filters["available_only"],
    }


# ---------------------------------------------------------------- 免登录借阅速查


def book_reviews(book_id: int, query: Mapping[str, Any]) -> dict[str, Any]:
    """门户图书详情弹窗里的读者评价（免登录、只读、只含已发布）。

    直接复用 ``library.engagement.service.book_reviews``——「什么算一条可见评价」
    以及评价者的脱敏口径只有那一处，门户不去自己拼 SQL，免得两边不一致。
    """
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.book_reviews(book_id, query)


def _mask_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return "读者"
    return text[0] + "*" * (len(text) - 1)


def _mask_card(card_no: str) -> str:
    text = (card_no or "").strip()
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * 3}{text[-2:]}"


def _open_loan_items(member_id: int, limit: int = 8) -> list[dict[str, Any]]:
    rows = db.query_all(
        """
        SELECT l.id, l.borrowed_at, l.due_at, l.renew_count,
               b.title, b.author, b.location
          FROM loans l
          JOIN books b ON b.id = l.book_id
         WHERE l.member_id = ? AND l.returned_at IS NULL
         ORDER BY date(l.due_at) ASC, l.id ASC
         LIMIT ?
        """,
        [member_id, limit],
    )
    for row in rows:
        days = fine.overdue_days(row["due_at"])
        row["overdue_days"] = days
        row["overdue"] = days > 0
        row["fine"] = fine.fine_for_days(days)
        row["state_label"] = f"逾期 {days} 天" if days > 0 else "在借"
    return rows


def quick_check(payload: Mapping[str, Any], *, ip: str = "") -> dict[str, Any]:
    """免登录的只读概览：凭「证号 / 手机号 + 密码」看自己在借与欠费情况。

    刻意做到三点：

    * **不建会话**：全程不写 session，查完即走，避免把"速查"变成隐形的登录；
    * **脱敏**：只回姓名首字、证号首尾与汇总数字，不返回联系方式、不返回借阅明细的书名以外信息；
    * **共用登录限流桶**：失败计数与正式登录同键，否则攻击者可以绕过登录锁定，
      改用这个免登录接口继续爆破密码。
    """
    account = str(payload.get("account") or payload.get("identifier") or "").strip()
    password = str(payload.get("password") or "")
    if not account:
        raise ValidationError("请输入借书证号或手机号", details={"field": "account"})
    if not password:
        raise ValidationError("请输入密码", details={"field": "password"})

    remaining = remaining_login_lock(account, ip)
    if remaining > 0:
        raise BusinessRuleError(f"失败次数过多，请 {remaining} 秒后再试")

    row = member_repo.get_for_auth_by_account(account)
    if row is None or not verify_password(password, str(row.get("password_hash") or "")):
        record_login_failure(account, ip)  # 与登录共用桶：不能借速查绕过锁定
        logger.warning("借阅速查失败", extra={"extra_fields": {"account": account, "ip": ip}})
        raise UnauthorizedError("借书证号 / 手机号或密码不正确", details={"field": "password"})

    clear_login_locks(account, ip)
    member_id = int(row["id"])

    from library.fines import service as fine_service  # noqa: PLC0415
    from library.reader import service as reader_service  # noqa: PLC0415
    from library.reservations import repository as reservation_repo  # noqa: PLC0415

    public_member = reader_service.public_member(row)
    stats = reader_service.stats(public_member)
    fines = fine_service.overview(public_member)
    items = _open_loan_items(member_id)

    logger.info("免登录借阅速查", extra={"extra_fields": {"member_id": member_id, "ip": ip}})
    return {
        "member": {
            "name": _mask_name(str(row["name"])),
            "card_no": _mask_card(str(row["card_no"])),
            "member_type": row["member_type"],
            "status": row["status"],
            "status_label": "正常" if row["status"] == "active" else "已冻结",
            "card_lost": bool(row.get("card_lost")),
        },
        "summary": {
            "active_loans": stats["active_loans"],
            "overdue_loans": stats["overdue_loans"],
            "quota_left": stats["quota_left"],
            "max_borrow": int(row["max_borrow"]),
            "fine_outstanding": fines["total_outstanding"],
            "reservation_open": reservation_repo.count_for_member(member_id, "open"),
            "reservation_ready": reservation_repo.count_for_member(member_id, "ready"),
        },
        "next_due_at": min((i["due_at"] for i in items), default=None),
        "items": items,
        "checked_at": timeutil.to_str(timeutil.now()),
    }


# ---------------------------------------------------------------- 在线办证申请


def submit_application(payload: Mapping[str, Any], *, ip: str = "") -> dict[str, Any]:
    from library.applications import service as application_service  # noqa: PLC0415

    return application_service.submit(payload, ip=ip)


def lookup_application(payload: Mapping[str, Any], *, ip: str = "") -> dict[str, Any]:
    from library.applications import service as application_service  # noqa: PLC0415

    return application_service.lookup(payload, ip=ip)


def application_form_options() -> dict[str, Any]:
    """办证表单的可选项：让前端与后端校验共用同一份取值。"""
    from library.applications import service as application_service  # noqa: PLC0415
    from library.members import service as member_service  # noqa: PLC0415

    return {
        "id_types": list(application_service.ID_TYPES),
        "member_types": sorted(member_service.MEMBER_TYPES),
    }


__all__ = [
    "overview",
    "search",
    "book_reviews",
    "quick_check",
    "submit_application",
    "lookup_application",
    "application_form_options",
]
