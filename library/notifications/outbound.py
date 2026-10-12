"""邮件出站渠道：站内通知之外的第二条触达路径。

## 设计取舍

* **入队与投递分离**。``enqueue`` 只写 ``outbound_messages`` 一行，永远轻快且不碰网络；
  真正的 SMTP 交互由 ``deliver_pending`` 完成（后台扫描线程每轮带一次，管理员也可手动触发）。
  SMTP 未配置时消息停在 ``pending``——配置好之后自然会补发，不丢数据。
* **幂等**。沿用站内通知的 ``dedupe_key`` 思路：同一业务事件的邮件只入队一次
  （部分唯一索引 ``idx_outbound_dedupe`` 兜底），扫描重复触发不会轰炸读者。
* **失败退避**。发送失败记 ``attempts`` 与 ``last_error``，重试到上限（5 次）转
  ``failed`` 不再自动重试；管理员可在系统维护里一键把失败重置回队列。
* **尊重退订**。读者没有邮箱或关闭了邮件订阅（``members.email_subscribed``）时
  直接不入队——业务必需的站内通知不受影响，邮件只是锦上添花的渠道。
* **零新依赖**。用标准库 ``smtplib`` / ``email.message``；发信全程 try/except，
  任何邮件异常都不允许拖垮主业务（与站内通知同一纪律）。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Mapping

from library import db, timeutil
from library.errors import BusinessRuleError, ValidationError
from library.validation import optional_email

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BATCH_LIMIT = 20

STATUS_LABELS: dict[str, str] = {"pending": "待发送", "sent": "已发送", "failed": "发送失败"}


def _config():
    from config import get_config  # noqa: PLC0415

    return get_config()


def smtp_configured(cfg=None) -> bool:
    cfg = cfg or _config()
    return bool(str(cfg.smtp_host or "").strip())


def _connect():
    """按配置建立 SMTP 连接（不登录不发送，供测试邮件与批量投递共用）。"""
    cfg = _config()
    host = str(cfg.smtp_host or "").strip()
    if not host:
        raise BusinessRuleError("尚未配置邮件渠道（LIBRARY_SMTP_HOST），无法发送邮件")
    port = int(cfg.smtp_port or 0)
    secure = str(cfg.smtp_secure or "auto").strip().lower()
    if secure == "auto":
        secure = "ssl" if port == 465 else "tls"
    if port <= 0:
        port = 465 if secure == "ssl" else 587

    if secure == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(host, port, timeout=10)
    try:
        if secure == "tls":
            client.starttls(context=ssl.create_default_context())
        user = str(cfg.smtp_user or "").strip()
        if user:
            client.login(user, str(cfg.smtp_password or ""))
    except Exception:
        try:
            client.quit()
        except Exception:  # noqa: BLE001 - 清理失败不影响主流程
            pass
        raise
    return client, cfg


def _sender(cfg) -> str:
    address = str(cfg.smtp_from or "").strip() or str(cfg.smtp_user or "").strip()
    return address or f"library@{str(cfg.smtp_host or '').strip()}"


# ---------------------------------------------------------------- 入队


def enqueue(
    member_id: int,
    subject: str,
    body: str,
    *,
    dedupe_key: str = "",
) -> str:
    """给读者入队一封业务邮件。返回 queued / skipped（无邮箱 / 已退订 / 重复入队）。

    任何异常都吞掉只告警——邮件是增强渠道，绝不能影响主业务事务。
    """
    try:
        member = db.query_one(
            "SELECT email, email_subscribed FROM members WHERE id = ?", [int(member_id)]
        )
        if member is None:
            return "skipped"
        address = str(member.get("email") or "").strip()
        if not address or not int(member.get("email_subscribed") or 0):
            return "skipped"
        db.execute(
            "INSERT OR IGNORE INTO outbound_messages "
            "(member_id, channel, to_address, subject, body, status, dedupe_key, created_at) "
            "VALUES (?, 'email', ?, ?, ?, 'pending', ?, ?)",
            [int(member_id), address, subject[:200], body[:2000], dedupe_key,
             timeutil.now().isoformat(timespec="seconds")],
        )
        return "queued"
    except Exception:  # noqa: BLE001 - 见模块 docstring
        logger.warning("邮件入队失败（已忽略）", exc_info=True)
        return "skipped"


def enqueue_from_notification(row: Mapping[str, Any]) -> str:
    """站内通知写入成功后顺手入队同名邮件（同一 dedupe 体系，天然幂等）。"""
    key = str(row.get("dedupe_key") or f"note:{row.get('id')}")
    return enqueue(
        int(row["member_id"]),
        str(row.get("title") or ""),
        str(row.get("body") or ""),
        dedupe_key=f"mail:{key}",
    )


# ---------------------------------------------------------------- 投递


def deliver_pending(limit: int = BATCH_LIMIT) -> dict[str, int]:
    """投递待发送邮件（含未达重试上限的失败件）。SMTP 未配置时原样返回。"""
    result = {"delivered": 0, "failed": 0, "remaining": 0, "configured": 1 if smtp_configured() else 0}
    if not smtp_configured():
        result["remaining"] = int(
            (db.query_one("SELECT COUNT(*) AS n FROM outbound_messages WHERE status = 'pending'") or {"n": 0})["n"]
        )
        return result

    rows = db.query_all(
        "SELECT * FROM outbound_messages WHERE status = 'pending' ORDER BY id LIMIT ?",
        [max(1, int(limit))],
    )
    if not rows:
        return result

    try:
        client, cfg = _connect()
    except Exception as exc:  # noqa: BLE001 - 连接失败：这批全部保留 pending，下轮再试
        logger.warning("SMTP 连接失败（本轮跳过投递）", extra={"extra_fields": {"error": str(exc)}})
        result["remaining"] = len(rows)
        return result

    try:
        for row in rows:
            try:
                msg = EmailMessage()
                msg["Subject"] = str(row["subject"])
                msg["From"] = formataddr((str(_config().library_name or "图书馆"), _sender(cfg)))
                msg["To"] = str(row["to_address"])
                msg.set_content(
                    str(row["body"] or "") + "\n\n—— 本邮件由图书馆管理系统自动发送，请勿直接回复。"
                )
                client.send_message(msg)
            except Exception as exc:  # noqa: BLE001 - 单件失败不影响其余
                _record_failure(row, str(exc))
                result["failed"] += 1
                continue
            db.execute(
                "UPDATE outbound_messages SET status = 'sent', sent_at = ?, attempts = attempts + 1, "
                "last_error = '' WHERE id = ?",
                [timeutil.now().isoformat(timespec="seconds"), int(row["id"])],
            )
            result["delivered"] += 1
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass

    result["remaining"] = int(
        (db.query_one("SELECT COUNT(*) AS n FROM outbound_messages WHERE status = 'pending'") or {"n": 0})["n"]
    )
    return result


def _record_failure(row: Mapping[str, Any], error: str) -> None:
    attempts = int(row.get("attempts") or 0) + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    try:
        db.execute(
            "UPDATE outbound_messages SET attempts = ?, last_error = ?, status = ? WHERE id = ?",
            [attempts, error[:500], status, int(row["id"])],
        )
    except Exception:  # noqa: BLE001
        logger.warning("记录邮件失败状态出错（已忽略）", exc_info=True)


def retry_failed() -> int:
    """把发送失败（已达上限）的邮件重置回队列重新投递。返回重置条数。"""
    # 注意：``db.execute`` 只返回 lastrowid，拿影响行数必须自己开事务拿 cursor
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE outbound_messages SET status = 'pending', attempts = 0 WHERE status = 'failed'"
        )
        affected = int(cur.rowcount or 0)
        cur.close()
    return affected


# ---------------------------------------------------------------- 状态与测试


def channel_status() -> dict[str, Any]:
    """系统维护页的渠道卡片：配置摘要 + 队列统计。密码永不回传。"""
    cfg = _config()
    configured = smtp_configured(cfg)
    secure = str(cfg.smtp_secure or "auto").strip().lower()
    if secure == "auto":
        secure = "ssl" if int(cfg.smtp_port or 0) == 465 else "tls"
    stats = db.query_all("SELECT status, COUNT(*) AS n FROM outbound_messages GROUP BY status")
    counts = {str(r["status"]): int(r["n"]) for r in stats}
    recent = db.query_all(
        "SELECT o.id, o.subject, o.to_address, o.status, o.attempts, o.last_error, o.created_at, o.sent_at, "
        "m.name AS member_name, m.card_no "
        "FROM outbound_messages o LEFT JOIN members m ON m.id = o.member_id "
        "ORDER BY o.id DESC LIMIT 15"
    )
    return {
        "configured": configured,
        "host": str(cfg.smtp_host or ""),
        "port": int(cfg.smtp_port or 0),
        "secure": secure,
        "username": str(cfg.smtp_user or ""),
        "from_address": _sender(cfg) if configured else "",
        "counts": {
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
        },
        "status_labels": STATUS_LABELS,
        "recent": [dict(r) for r in recent],
    }


def send_test(to_address: str) -> dict[str, Any]:
    """给管理员指定的邮箱发一封测试邮件（不进队列，直接同步发送）。"""
    address = optional_email({"email": to_address}, "email", label="收件邮箱")
    if not address:
        raise ValidationError("收件邮箱不能为空", details={"field": "email"})
    client, cfg = _connect()
    try:
        msg = EmailMessage()
        msg["Subject"] = "【图书馆管理系统】邮件渠道测试"
        msg["From"] = formataddr((str(cfg.library_name or "图书馆"), _sender(cfg)))
        msg["To"] = address
        msg.set_content(
            "这是一封测试邮件。\n\n"
            "如果你收到了它，说明系统的邮件出站渠道（SMTP）配置正确，\n"
            "读者的催还 / 到期 / 预约到书等业务通知可以同步发往邮箱。\n"
        )
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass
    return {"to": address, "ok": True}
