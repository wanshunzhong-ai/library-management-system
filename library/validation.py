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


def require_float(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _get(payload, key)
    if value is None or value == "":
        raise ValidationError(f"{label}不能为空", details={"field": key})
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是数字", details={"field": key}) from exc
    if minimum is not None and number < minimum:
        raise ValidationError(f"{label}不能小于 {minimum}", details={"field": key})
    if maximum is not None and number > maximum:
        raise ValidationError(f"{label}不能大于 {maximum}", details={"field": key})
    return number


def optional_float(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    minimum: float | None = None,
    maximum: float | None = None,
    default: float = 0.0,
) -> float:
    value = _get(payload, key)
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}必须是数字", details={"field": key}) from exc
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


def isbn_check(raw: str) -> str:
    """按校验位判断 ISBN 是否规范，仅用于**提示**而不阻断保存。

    返回 ``ok`` / ``invalid_checksum``（长度合法但校验位不对）/ ``unknown_format``
    （长度不是 10 或 13 位，无从判断）。不少馆藏沿用内部编码或旧书号，
    因此这里只做提示，避免把历史数据挡在门外。
    """
    code = normalize_isbn(raw)
    if len(code) == 13:
        if not code.isdigit():
            return "unknown_format"
        total = sum((1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(code[:12]))
        expected = (10 - total % 10) % 10
        return "ok" if expected == int(code[12]) else "invalid_checksum"

    if len(code) == 10:
        if not (code[:9].isdigit() and (code[9].isdigit() or code[9] == "X")):
            return "unknown_format"
        # 权重 10..1，X 计作 10；合法 ISBN-10 的加权和应能被 11 整除
        total = sum((10 - i) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(code))
        return "ok" if total % 11 == 0 else "invalid_checksum"

    return "unknown_format"
