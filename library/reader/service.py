"""读者账号：登录、会话、个人借阅查询与自助服务。

读者与管理员使用不同的会话键（session["member_id"] 与 session["admin_id"]）和
各自独立的 CSRF token；登录入口已统一到首页，由鉴权层识别身份后调用本模块
的 start_session 建立读者会话。一次登录只保留一种身份，不会同时存在两套会话。
读者只能访问自己的数据，任何跨读者的读写都按越权处理。
"""

from __future__ import annotations

import functools
import hmac
import logging
import secrets
from typing import Any, Callable, Mapping

from flask import g, jsonify, redirect, request, session, url_for

from config import get_config
from library import db, fine
from library.auth.passwords import hash_password, verify_password
from library.auth.throttle import (
    clear_login_locks,
    login_throttle,  # noqa: F401  对外沿用旧入口
    record_login_failure,
    remaining_login_lock,
)
from library.errors import (
    BusinessRuleError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from library.loans import repository as loan_repo
from library.loans import service as loan_service
from library.members import repository as member_repo
from library.members import service as member_service
from library.validation import optional_email, optional_str

logger = logging.getLogger(__name__)

SESSION_MEMBER_ID = "member_id"
SESSION_READER_CSRF = "reader_csrf"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 读者端免登录接口（登录页与登录动作本身）
PUBLIC_ENDPOINTS = {"reader.login_page", "reader.login"}

# 「首次登录必须改密」闸门放行的最小集合。少一个就会把读者锁死在自己的账号外
# （拿不到 CSRF token、看不到提示、连改密接口都进不去），多一个就等于闸门失效。
PASSWORD_CHANGE_ALLOWED = {
    ("GET", "/api/reader/me"),
    ("GET", "/api/reader/notifications"),
    ("POST", "/api/reader/password"),
    ("POST", "/api/reader/logout"),
}

# 与管理员共用同一个限流器：登录入口统一，两套计数会给出绕过锁定的空子
reader_throttle = login_throttle


# ---------------------------------------------------------------- 资料视图


def public_member(row: Mapping[str, Any]) -> dict[str, Any]:
    """读者本人可见的资料（不含密码哈希）。"""
    return {
        "id": int(row["id"]),
        "card_no": row["card_no"],
        "name": row["name"],
        "phone": row["phone"],
        "email": row["email"],
        "email_subscribed": bool(row.get("email_subscribed", 1)),
        "member_type": row["member_type"],
        "status": row["status"],
        "max_borrow": int(row["max_borrow"]),
        "card_lost": bool(row.get("card_lost")),
        "lost_at": row.get("lost_at"),
        "must_change_password": bool(row.get("must_change_password")),
        "last_login_at": row.get("last_login_at"),
    }


# ---------------------------------------------------------------- 会话


def find_for_auth(account: str) -> Mapping[str, Any] | None:
    """按借书证号或手机号取鉴权行（供统一登录入口识别身份用）。"""
    key = str(account or "").strip()
    if not key:
        return None
    return member_repo.get_for_auth_by_account(key)


def start_session(row: Mapping[str, Any]) -> str:
    """建立读者会话并返回 CSRF token。

    供统一登录入口与读者专用登录接口共用：先清空既有会话，
    保证同一浏览器一次只持有一种身份（读者或管理员）。
    传入的是带 password_hash 的鉴权行，此处只取 id 并刷新登录时间。
    """
    member_id = int(row["id"])
    member_repo.touch_login(member_id)
    session.clear()
    session.permanent = True
    session[SESSION_MEMBER_ID] = member_id
    session[SESSION_READER_CSRF] = secrets.token_urlsafe(24)
    return session[SESSION_READER_CSRF]


def login(payload: Mapping[str, Any], ip: str) -> dict[str, Any]:
    """读者专用登录接口（保留）：账号可以是借书证号或手机号。"""
    account = str(payload.get("account") or payload.get("card_no") or payload.get("identifier") or "").strip()
    password = str(payload.get("password") or "")
    if not account:
        raise ValidationError("请输入借书证号或手机号", details={"field": "account"})
    if not password:
        raise ValidationError("请输入密码", details={"field": "password"})

    remaining = remaining_login_lock(account, ip)
    if remaining > 0:
        raise BusinessRuleError(f"登录失败次数过多，请 {remaining} 秒后重试")

    row = member_repo.get_for_auth_by_account(account)
    if row is None or not verify_password(password, str(row.get("password_hash") or "")):
        record_login_failure(account, ip)
        logger.warning("读者登录失败", extra={"extra_fields": {"account": account, "ip": ip}})
        raise UnauthorizedError("账号或密码不正确", details={"field": "password"})

    clear_login_locks(account, ip)
    csrf = start_session(row)
    row = member_repo.get(int(row["id"])) or row

    logger.info(
        "读者登录成功",
        extra={"extra_fields": {"card_no": row["card_no"], "ip": ip}},
    )
    return {"member": public_member(row), "csrf_token": csrf}


def logout() -> None:
    session.pop(SESSION_MEMBER_ID, None)
    session.pop(SESSION_READER_CSRF, None)


def current_reader() -> dict[str, Any] | None:
    """取当前登录读者（请求内缓存）。"""
    if getattr(g, "_current_reader_loaded", False):
        return getattr(g, "_current_reader", None)
    g._current_reader_loaded = True
    g._current_reader = None

    member_id = session.get(SESSION_MEMBER_ID)
    if not member_id:
        return None
    row = member_repo.get(int(member_id))
    if row is None:
        session.pop(SESSION_MEMBER_ID, None)
        return None
    g._current_reader = public_member(row)
    return g._current_reader


def csrf_token() -> str:
    token = session.get(SESSION_READER_CSRF)
    if not token:
        token = secrets.token_urlsafe(24)
        session[SESSION_READER_CSRF] = token
    return token


def csrf_protect() -> tuple | None:
    """读者端写操作 CSRF 校验。返回响应表示校验失败。"""
    if request.method in SAFE_METHODS:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    expected = session.get(SESSION_READER_CSRF)
    provided = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("_csrf")
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        logger.warning("读者端 CSRF 校验失败", extra={"extra_fields": {"path": request.path}})
        return (
            jsonify({"error": {"code": "csrf_failed", "message": "请求校验失败，请刷新页面后重试"}}),
            403,
        )
    return None


def reader_required(view: Callable) -> Callable:
    """个人中心页面：未登录跳统一登录页（首页）；接口：返回 401 JSON。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_reader() is not None:
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return (
                jsonify({"error": {"code": "unauthorized", "message": "登录状态已失效，请重新登录"}}),
                401,
            )
        return redirect(url_for("public.home", next=request.path))

    return wrapper


def password_change_gate():
    """首次登录强制改密的闸门。返回响应表示拦截。

    馆员建号 / 重置密码 / 办证发证后，口令是「证号后 6 位」——证号是顺序编号时
    这几乎等于公开信息，所以必须让读者在真正使用账号前先换掉它。
    拦截期间只放行 ``PASSWORD_CHANGE_ALLOWED`` 里的最小集合（页面初始化 + 改密本身）。
    """
    member = current_reader()
    if member is None or not member.get("must_change_password"):
        return None
    if (request.method, request.path) in PASSWORD_CHANGE_ALLOWED:
        return None
    logger.info(
        "截断未改初始密码的读者请求",
        extra={"extra_fields": {"member_id": member["id"], "path": request.path}},
    )
    return (
        jsonify(
            {
                "error": {
                    "code": "password_change_required",
                    "message": "这是初始密码，请先修改密码后再使用其他功能",
                }
            }
        ),
        403,
    )


# ---------------------------------------------------------------- 个人中心数据


def stats(member: Mapping[str, Any]) -> dict[str, Any]:
    """在借 / 逾期 / 历史册数与累计罚金。

    逾期判定、天数与罚金全部走 ``library.fine`` 的统一口径（整日 + 封顶），
    因此与借阅列表逐行显示、概览估算三者完全一致。
    """
    member_id = int(member["id"])
    row = db.query_one(
        """
        SELECT
            SUM(CASE WHEN returned_at IS NULL THEN 1 ELSE 0 END) AS active_loans,
            SUM(CASE WHEN returned_at IS NULL AND date(due_at) < date('now', 'localtime')
                     THEN 1 ELSE 0 END) AS overdue_loans,
            SUM(CASE WHEN returned_at IS NOT NULL THEN 1 ELSE 0 END) AS returned_total,
            COUNT(*) AS loan_total
          FROM loans
         WHERE member_id = ?
        """,
        [member_id],
    ) or {}

    # 逐笔计算后汇总（含封顶），口径与借阅列表一致
    settlements = db.query_all(
        "SELECT due_at, returned_at FROM loans WHERE member_id = ?", [member_id]
    )
    active = int(row.get("active_loans") or 0)
    return {
        "active_loans": active,
        "overdue_loans": int(row.get("overdue_loans") or 0),
        "returned_total": int(row.get("returned_total") or 0),
        "loan_total": int(row.get("loan_total") or 0),
        "quota_left": max(0, int(member["max_borrow"]) - active),
        "fine_total": fine.total_fine(settlements),
        "overdue_days_total": fine.total_overdue_days(settlements),
    }


def me() -> dict[str, Any]:
    member = current_reader()
    if member is None:
        raise UnauthorizedError("请先登录读者账号")
    cfg = get_config()
    return {
        "member": member,
        "stats": stats(member),
        "reservations": _reservation_summary(member),
        "fines": _fine_summary(member),
        "notifications": _notification_summary(member),
        "rules": {
            "loan_days": cfg.loan_days,
            "max_renew": cfg.max_renew,
            "fine_per_day": cfg.fine_per_day,
            "fine_max_days": cfg.fine_max_days,
            "fine_cap": cfg.fine_cap,
            "reservation_hold_days": cfg.reservation_hold_days,
            "notify_due_soon_days": cfg.notify_due_soon_days,
        },
        "library": {"name": cfg.library_name, "slogan": cfg.library_slogan},
        "csrf_token": csrf_token(),
    }


def _reservation_summary(member: Mapping[str, Any]) -> dict[str, Any]:
    from library.reservations import repository as reservation_repo  # noqa: PLC0415

    member_id = int(member["id"])
    return {
        "open_total": reservation_repo.count_for_member(member_id, "open"),
        "ready_total": reservation_repo.count_for_member(member_id, "ready"),
    }


def _fine_summary(member: Mapping[str, Any]) -> dict[str, Any]:
    from library.fines import service as fine_service  # noqa: PLC0415

    overview = fine_service.overview(member)
    return {
        "total_outstanding": overview["total_outstanding"],
        "total_paid": overview["total_paid"],
        "total_waived": overview["total_waived"],
        "item_count": len(overview["items"]),
    }


def _notification_summary(member: Mapping[str, Any]) -> dict[str, Any]:
    """未读通知数（顺带把该补的通知补上）。

    系统没有定时任务，通知是在这里**惰性生成**的：读者每打开一次个人中心，
    就复查一遍「有没有书马上要还、有没有已经逾期、有没有预约该取了」。
    见 `library.notifications.service` 的模块注释。
    """
    from library.notifications import service as notification_service  # noqa: PLC0415
    from library.reservations import service as reservation_service  # noqa: PLC0415

    try:
        reservation_service.sweep_expired()
    except Exception:  # noqa: BLE001 - 概览页不该因为清理失败而打不开
        logger.warning("预约超期清理失败（已忽略）", exc_info=True)

    member_id = int(member["id"])
    notification_service.sweep_member(member_id)
    return {"unread": notification_service.unread_count(member_id)}


def list_my_loans(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    """只看自己的借阅记录：member_id 强制取自会话，不接受前端传入。"""
    status = (query.get("status") or "all").strip()
    if status not in {"all", "open", "borrowed", "overdue", "returned"}:
        raise ValidationError(
            "status 只能是 all/open/borrowed/overdue/returned", details={"field": "status"}
        )
    return loan_service.list_loans(
        {
            "status": status,
            "member_id": int(member["id"]),
            "page": query.get("page", 1),
            "page_size": query.get("page_size", 10),
        }
    )


def renew_my_loan(member: Mapping[str, Any], loan_id: int) -> dict[str, Any]:
    """自助续借：先校验记录归属，再复用管理端的续借规则。"""
    raw = loan_repo.raw(loan_id)
    if raw is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    if int(raw["member_id"]) != int(member["id"]):
        logger.warning(
            "读者尝试续借他人记录",
            extra={"extra_fields": {"member_id": member["id"], "loan_id": loan_id}},
        )
        raise ForbiddenError("只能续借本人的借阅记录")
    if member["status"] != "active":
        raise BusinessRuleError("账号已被冻结，暂时无法自助续借，请联系管理员")
    if member.get("card_lost"):
        raise BusinessRuleError("借书证已挂失，请先解除挂失再续借")

    result = loan_service.renew(loan_id)
    logger.info(
        "读者自助续借",
        extra={"extra_fields": {"member_id": member["id"], "loan_id": loan_id}},
    )
    return result


def update_profile(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """读者自助维护联系方式与邮件订阅开关；姓名 / 证号 / 可借上限仍由管理员掌握。"""
    phone = optional_str(payload, "phone", label="手机号", max_length=20)
    email = optional_email(payload, "email")
    subscribed = payload.get("email_subscribed")
    subscribed_flag = None if subscribed is None or subscribed == "" else str(subscribed).strip().lower() in ("1", "true", "on", "yes")

    # 手机号可用于登录，必须与其它读者的证号 / 手机号以及管理员工号全局唯一
    member_service.ensure_identifier_available(
        phone, field="phone", label="手机号", exclude_member_id=int(member["id"])
    )

    member_repo.update_contact(int(member["id"]), phone, email, email_subscribed=subscribed_flag)
    row = member_repo.get(int(member["id"])) or {}
    return public_member(row)


def change_password(member: Mapping[str, Any], old_password: str, new_password: str) -> None:
    row = member_repo.get_for_auth_by_account(str(member["card_no"]))
    if row is None or not verify_password(old_password, str(row.get("password_hash") or "")):
        raise ValidationError("当前密码不正确", details={"field": "old_password"})
    if len(new_password) < 6:
        raise ValidationError("新密码至少 6 位", details={"field": "new_password"})
    if new_password == old_password:
        raise ValidationError("新密码不能与当前密码相同", details={"field": "new_password"})

    member_repo.update_password_hash(int(member["id"]), hash_password(new_password))
    # 改密成功即解除「首次登录必须改密」——这是这道闸门唯一的出口
    member_repo.set_must_change_password(int(member["id"]), False)
    logout()  # 改密后强制重新登录
    logger.info("读者修改密码", extra={"extra_fields": {"member_id": member["id"]}})


# ---------------------------------------------------------------- 预约（读者侧）


def list_my_reservations(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    from library.reservations import service as reservation_service  # noqa: PLC0415

    return reservation_service.list_my_reservations(member, query)


def create_reservation(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.reservations import service as reservation_service  # noqa: PLC0415

    return reservation_service.reserve(member, payload)


def cancel_reservation(member: Mapping[str, Any], res_id: int) -> dict[str, Any]:
    from library.reservations import service as reservation_service  # noqa: PLC0415

    return reservation_service.cancel_my_reservation(member, res_id)


def rebook_reservation(
    member: Mapping[str, Any], res_id: int, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """改约：把一条未结束的预约换到另一本书（关旧单 + 开新单在一个事务内）。"""
    from library.reservations import service as reservation_service  # noqa: PLC0415

    return reservation_service.rebook(member, res_id, payload)


def search_books_for_reserve(query: Mapping[str, Any]) -> dict[str, Any]:
    """预约前的图书检索：复用门户检索口径，只返回可预约相关的馆藏信息。"""
    from library.books import service as book_service  # noqa: PLC0415

    return book_service.list_books(
        {
            "keyword": query.get("keyword", ""),
            "category": query.get("category", ""),
            "page": query.get("page", 1),
            "page_size": query.get("page_size", 8),
        }
    )


# ---------------------------------------------------------------- 读者互动
# 四个领域（收藏 / 评价 / 荐购 / 资料变更）的业务在 library.engagement，
# 读者端只做「取会话中的 member_id → 转发」，与预约、滞纳金保持同一套写法。


def list_my_favorites(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.list_my_favorites(member, query)


def add_favorite(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.add_favorite(member, payload)


def update_favorite(member: Mapping[str, Any], favorite_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.update_favorite(member, favorite_id, payload)


def remove_favorite(member: Mapping[str, Any], favorite_id: int) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.remove_favorite(member, favorite_id)


def list_my_reviews(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.list_my_reviews(member, query)


def list_my_reviewable(member: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.list_reviewable(member)


def submit_review(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.submit_review(member, payload)


def delete_my_review(member: Mapping[str, Any], review_id: int) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.delete_my_review(member, review_id)


def book_reviews(member: Mapping[str, Any], book_id: int, query: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.book_reviews(book_id, query, member_id=int(member["id"]))


def list_my_book_requests(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.list_my_book_requests(member, query)


def submit_book_request(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.submit_book_request(member, payload)


def list_my_profile_requests(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.list_my_profile_requests(member, query)


def submit_profile_request(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.engagement import service as engagement  # noqa: PLC0415

    return engagement.submit_profile_request(member, payload)


# ---------------------------------------------------------------- 滞纳金（读者侧）


def fines_overview(member: Mapping[str, Any]) -> dict[str, Any]:
    from library.fines import service as fine_service  # noqa: PLC0415

    return fine_service.overview(member)


def pay_fines(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    from library.fines import service as fine_service  # noqa: PLC0415

    result = fine_service.pay(member, payload)
    logger.info(
        "读者自助缴纳滞纳金",
        extra={"extra_fields": {"member_id": member["id"], "amount": result.get("paid_total")}},
    )
    try:
        from library.notifications import service as notification_service  # noqa: PLC0415

        notification_service.notify(
            int(member["id"]),
            "fine",
            f"已登记缴纳滞纳金 ¥{float(result.get('paid_total') or 0):.2f}",
            body=f"本次共 {int(result.get('count') or 0)} 笔。系统内为自助登记，如需纸质凭证请联系馆员。",
            link_view="fines",
        )
    except Exception:  # noqa: BLE001 - 通知是增强功能
        logger.warning("缴纳通知写入失败（已忽略）", exc_info=True)
    return result


# ---------------------------------------------------------------- 借书证挂失


def set_card_lost(member: Mapping[str, Any], lost: bool) -> dict[str, Any]:
    """读者自助挂失 / 解挂。

    挂失是读者的自我保护动作，独立于馆方的 ``status``（冻结）——因此即便读者
    仍有未还图书也能挂失（挂失只是锁住"用这张证借书"的能力，不影响已借记录）。
    """
    member_id = int(member["id"])
    row = member_repo.get(member_id)
    if row is None:
        raise NotFoundError("读者账号不存在")
    currently = bool(row.get("card_lost"))
    if currently == bool(lost):
        return public_member(member_repo.get(member_id) or row)

    member_repo.set_card_lost(member_id, bool(lost))
    logger.warning(
        "读者借书证状态变更",
        extra={"extra_fields": {"member_id": member_id, "card_lost": bool(lost)}},
    )
    return public_member(member_repo.get(member_id) or row)
