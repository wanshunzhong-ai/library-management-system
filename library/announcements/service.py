"""公告与开馆时间业务逻辑。

两个设计取舍：

1. **开馆时间做成一条特殊公告**（``kind='hours'``），而不是单开一张配置表。
   馆员只需要在一个地方维护"对外要说的话"，门户按 kind 取用：公告栏渲染
   ``notice``，关于本馆的开馆时间取最新一条生效的 ``hours``。
2. **时间窗口按自然日归一**：只填日期时，生效时间取当天 00:00:00、
   失效时间取当天 23:59:59——否则"设置到 10 月 11 日结束"会在当天凌晨就失效。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import timeutil
from library.announcements import repository as repo
from library.errors import NotFoundError, ValidationError
from library.validation import optional_str, pagination, require_choice, require_str

KINDS = {"notice", "hours"}
LEVELS = {"info", "important"}
STATUSES = {"published", "draft", "archived"}

KIND_LABELS = {"notice": "公告", "hours": "开馆时间"}
LEVEL_LABELS = {"info": "普通", "important": "重要"}
STATUS_LABELS = {"published": "已发布", "draft": "草稿", "archived": "已下线"}


def _normalize_dt(value: str, *, end_of_day: bool, field: str, label: str) -> str | None:
    """把日期/时间输入统一成 ``YYYY-MM-DD HH:MM:SS``；空值表示不限。"""
    text = (value or "").strip()
    if not text:
        return None
    parsed = timeutil.parse(text)
    if parsed is None:
        raise ValidationError(
            f"{label}格式不正确，请用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM",
            details={"field": field},
        )
    if end_of_day and len(text) <= 10:  # 只给了日期：失效时间顺延到当天结束
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return timeutil.to_str(parsed)


def _pinned(payload: Mapping[str, Any]) -> int:
    raw = payload.get("pinned")
    if isinstance(raw, bool):
        return 1 if raw else 0
    if raw in (None, ""):
        return 0
    return 1 if int(raw) else 0


def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
    publish_at = _normalize_dt(
        optional_str(payload, "publish_at", label="生效时间", max_length=32),
        end_of_day=False,
        field="publish_at",
        label="生效时间",
    )
    expire_at = _normalize_dt(
        optional_str(payload, "expire_at", label="失效时间", max_length=32),
        end_of_day=True,
        field="expire_at",
        label="失效时间",
    )
    if publish_at and expire_at and expire_at < publish_at:
        raise ValidationError("失效时间不能早于生效时间", details={"field": "expire_at"})
    return {
        "title": require_str(payload, "title", label="标题", max_length=100),
        "body": optional_str(payload, "body", label="正文", max_length=2000),
        "kind": require_choice(payload, "kind", label="类型", choices=KINDS)
        if payload.get("kind")
        else "notice",
        "level": require_choice(payload, "level", label="重要程度", choices=LEVELS)
        if payload.get("level")
        else "info",
        "pinned": _pinned(payload),
        "status": require_choice(payload, "status", label="状态", choices=STATUSES)
        if payload.get("status")
        else "published",
        "publish_at": publish_at,
        "expire_at": expire_at,
    }


def _is_effective(item: Mapping[str, Any]) -> bool:
    if str(item.get("status")) != "published":
        return False
    now = timeutil.to_str(timeutil.now())
    publish_at = item.get("publish_at")
    expire_at = item.get("expire_at")
    if publish_at and str(publish_at) > now:
        return False
    if expire_at and str(expire_at) < now:
        return False
    return True


def _decorate(item: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row["pinned"] = bool(row.get("pinned"))
    row["kind_label"] = KIND_LABELS.get(str(row.get("kind")), str(row.get("kind") or ""))
    row["level_label"] = LEVEL_LABELS.get(str(row.get("level")), str(row.get("level") or ""))
    row["status_label"] = STATUS_LABELS.get(str(row.get("status")), str(row.get("status") or ""))
    row["effective"] = _is_effective(row)
    if row.get("publish_at") and row.get("expire_at"):
        row["window_label"] = f"{str(row['publish_at'])[:10]} 至 {str(row['expire_at'])[:10]}"
    elif row.get("publish_at"):
        row["window_label"] = f"{str(row['publish_at'])[:10]} 起长期有效"
    elif row.get("expire_at"):
        row["window_label"] = f"即日起至 {str(row['expire_at'])[:10]}"
    else:
        row["window_label"] = "长期有效"
    return row


# ---------------------------------------------------------------- 后台


def list_announcements(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "keyword": query.get("keyword", ""),
        "status": query.get("status", ""),
        "kind": query.get("kind", ""),
        "sort": query.get("sort", ""),
        "order": query.get("order", ""),
    }
    total = repo.count(filters)
    items = [_decorate(r) for r in repo.list_all(filters, limit=size, offset=(page - 1) * size)]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "published_total": repo.count({"status": "published"}),
        "draft_total": repo.count({"status": "draft"}),
        "kinds": [{"value": k, "label": v} for k, v in KIND_LABELS.items()],
    }


def get_announcement(announcement_id: int) -> dict[str, Any]:
    row = repo.get(announcement_id)
    if row is None:
        raise NotFoundError(f"公告 #{announcement_id} 不存在")
    return _decorate(row)


def create_announcement(payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    data = _normalize(payload)
    data["operator"] = operator
    new_id = repo.insert(data)
    return get_announcement(new_id)


def update_announcement(
    announcement_id: int, payload: Mapping[str, Any], *, operator: str = ""
) -> dict[str, Any]:
    if repo.get(announcement_id) is None:
        raise NotFoundError(f"公告 #{announcement_id} 不存在")
    data = _normalize(payload)
    data["operator"] = operator
    repo.update(announcement_id, data)
    return get_announcement(announcement_id)


def set_status(announcement_id: int, status: str, *, operator: str = "") -> dict[str, Any]:
    if status not in STATUSES:
        raise ValidationError(
            f"状态只能是 {'/'.join(sorted(STATUSES))} 之一", details={"field": "status"}
        )
    if repo.get(announcement_id) is None:
        raise NotFoundError(f"公告 #{announcement_id} 不存在")
    repo.set_status(announcement_id, status)
    return get_announcement(announcement_id)


def delete_announcement(announcement_id: int) -> dict[str, Any]:
    row = repo.get(announcement_id)
    if row is None:
        raise NotFoundError(f"公告 #{announcement_id} 不存在")
    repo.delete(announcement_id)
    return {"deleted": announcement_id, "title": row["title"]}


# ---------------------------------------------------------------- 门户（免登录）


def public_list() -> dict[str, Any]:
    """门户公告栏数据：当前生效的公告（不含开馆时间，它单独渲染）。"""
    items = [_decorate(r) for r in repo.active(kind="notice", limit=20)]
    return {"items": items, "total": len(items)}


def public_hours() -> dict[str, Any] | None:
    """门户「关于本馆」里的开馆时间：取最新一条生效的 hours 公告。"""
    rows = repo.active(kind="hours", limit=1)
    return _decorate(rows[0]) if rows else None
