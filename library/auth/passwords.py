"""密码哈希与初始密码规则（管理员、读者共用）。

单独成模块是为了让 auth（管理员）与 members（读者）都能引用，
避免业务模块之间互相导入。
"""

from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash

# 初始密码取证号后 N 位；证号较短时退化为整串
INITIAL_PASSWORD_SUFFIX = 6


def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method="pbkdf2:sha256", salt_length=16)


def verify_password(raw: str, hashed: str) -> bool:
    if not hashed:
        return False
    return check_password_hash(hashed, raw)


def initial_password(card_no: str) -> str:
    """读者账号的初始密码规则：证号后 6 位（不足 6 位则用整串）。"""
    card = (card_no or "").strip()
    if not card:
        return ""
    return card[-INITIAL_PASSWORD_SUFFIX:]
