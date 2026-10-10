"""输入校验工具：所有外部输入在边界处收敛成干净的类型。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from library.errors import ValidationError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _get(payload: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = payload.get(key, default)
    if isinstance(value, str):
        return value.strip()
    return value


def require_str(payload: Mapping[str, Any], key: str, *, label: str, max_length: int = 200) -> str:
    value = _get(payload, key)
    if value is None or value == "":
        raise ValidationError(f"{label}不能为空", details={"field": key})
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是文本", details={"field": key})
    if len(value) > max_length:
        raise ValidationError(f"{label}长度不能超过 {max_length} 个字符", details={"field": key})
    return value


def optional_str(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    max_length: int = 200,
    default: str = "",
) -> str:
    value = _get(payload, key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是文本", details={"field": key})
    if len(value) > max_length:
        raise ValidationError(f"{label}长度不能超过 {max_length} 个字符", details={"field": key})
    return value


def require_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _get(payload, key)
    if value is None or value == "":
        raise ValidationError(f"{label}不能为空", details={"field": key})
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是整数", details={"field": key}) from exc
    if minimum is not None and number < minimum:
        raise ValidationError(f"{label}不能小于 {minimum}", details={"field": key})
    if maximum is not None and number > maximum:
        raise ValidationError(f"{label}不能大于 {maximum}", details={"field": key})
    return number


def optional_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
    default: int | None = None,
) -> int | None:
    value = _get(payload, key)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是整数", details={"field": key}) from exc
    if minimum is not None and number < minimum:
        raise ValidationError(f"{label}不能小于 {minimum}", details={"field": key})
    if maximum is not None and number > maximum:
        raise ValidationError(f"{label}不能大于 {maximum}", details={"field": key})
    return number


def optional_email(payload: Mapping[str, Any], key: str, *, label: str = "邮箱") -> str:
    value = _get(payload, key, "")
    if not value:
        return ""
    if not isinstance(value, str) or not _EMAIL_RE.match(value):
        raise ValidationError(f"{label}格式不正确", details={"field": key})
    return value


def require_choice(payload: Mapping[str, Any], key: str, *, label: str, choices: set[str]) -> str:
    value = require_str(payload, key, label=label, max_length=50)
    if value not in choices:
        raise ValidationError(
            f"{label}只能是 {'/'.join(sorted(choices))} 之一",
            details={"field": key, "choices": sorted(choices)},
        )
    return value


def pagination(payload: Mapping[str, Any], *, default_size: int = 10, max_size: int = 100) -> tuple[int, int]:
    page = optional_int(payload, "page", label="页码", minimum=1, default=1) or 1
    size = optional_int(payload, "page_size", label="每页条数", minimum=1, maximum=max_size, default=default_size) or default_size
    return page, size


def normalize_isbn(raw: str) -> str:
    """去掉分隔符并转大写，做唯一性判定用。"""
    return re.sub(r"[^0-9Xx]", "", raw).upper()
