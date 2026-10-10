"""馆藏条码：生成、规整与校验。

条码与 ISBN 解耦：同一 ISBN 的多册图书共用 ISBN，但每本书（书目）拥有
独立的馆藏条码号，形如 ``BK0000007``，用于贴标、扫码借还与上架归位。
本模块只做纯字符串处理，不依赖数据库，便于被数据层、业务层与脚本复用。
"""

from __future__ import annotations

import re

PREFIX = "BK"
SEQ_WIDTH = 7
MAX_LENGTH = 32

# 允许字母、数字与连字符；馆内自定义编号（如自编号码）也能录入
_VALID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,31}$")


def generate(seq: int) -> str:
    """按序号生成条码号：1 → BK0000001。"""
    number = int(seq)
    if number <= 0:
        raise ValueError("条码序号必须为正整数")
    return f"{PREFIX}{number:0{SEQ_WIDTH}d}"


def normalize(raw: object) -> str:
    """转大写并折叠空格 / 下划线，便于唯一性判定。

    连字符保留：馆内自定义编号（如 ``A-2026-001``）允许带连字符。
    """
    text = str(raw or "").strip().upper()
    return re.sub(r"[\s_]+", "", text)


def is_valid(code: str) -> bool:
    """4–32 位，以字母或数字开头，可含数字、字母与连字符。"""
    if not code or not 4 <= len(code) <= MAX_LENGTH:
        return False
    return bool(_VALID_RE.match(code))
