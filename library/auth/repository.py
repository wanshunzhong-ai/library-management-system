"""管理员账号数据访问层。"""

from __future__ import annotations

from typing import Any

from library import db

_COLUMNS = "id, username, password_hash, display_name, role, is_active, last_login_at, created_at"

# 对外（账号管理界面 / 日志）用的字段：不含口令散列。
_PUBLIC_COLUMNS = "id, username, display_name, role, is_active, last_login_at, created_at"


def count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM admins") or 0)


def list_all() -> list[dict[str, Any]]:
    """全部管理员（启动自检默认口令用）。"""
    return db.query_all(f"SELECT {_COLUMNS} FROM admins ORDER BY id")


def list_public() -> list[dict[str, Any]]:
    """账号管理列表：不含 password_hash，避免散列被顺带带出到前端。"""
    return db.query_all(
        f"SELECT {_PUBLIC_COLUMNS} FROM admins ORDER BY (role = 'admin') DESC, id"
    )


def count_active_admins(exclude_id: int | None = None) -> int:
    """在职的「系统管理员」数量（可用排除某个 id 来模拟"如果把他停用/删掉还剩几个"）。"""
    if exclude_id is None:
        return int(
            db.query_scalar(
                "SELECT COUNT(*) FROM admins WHERE role = 'admin' AND is_active = 1"
            )
            or 0
        )
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM admins WHERE role = 'admin' AND is_active = 1 AND id <> ?",
            [exclude_id],
        )
        or 0
    )


def member_uses_identifier(username: str) -> bool:
    """该用户名是否已被某个读者的证号或手机号占用。

    管理员用户名与读者登录标识共用同一个登录入口（先匹配管理员），一旦重名，
    读者会被永久遮蔽在门外——建档时挡过一遍，建后台账号时也必须挡。
    """
    return bool(
        db.query_scalar(
            "SELECT 1 FROM members WHERE card_no = ? OR (phone <> '' AND phone = ?) LIMIT 1",
            [username, username],
        )
    )


def get_by_username(username: str) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM admins WHERE username = ?", [username])


def exists_username(username: str) -> bool:
    """该用户名是否已被管理员工号占用（读者证号/手机号不能与之重名，否则登录会互相遮蔽）。"""
    return bool(db.query_scalar("SELECT 1 FROM admins WHERE username = ? LIMIT 1", [username]))


def get(admin_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM admins WHERE id = ?", [admin_id])


def create(username: str, password_hash: str, display_name: str, role: str = "admin") -> int:
    return db.execute(
        "INSERT INTO admins (username, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        [username, password_hash, display_name, role],
    )


def update_password(admin_id: int, password_hash: str) -> None:
    db.execute("UPDATE admins SET password_hash = ? WHERE id = ?", [password_hash, admin_id])


def update_profile(admin_id: int, *, display_name: str, role: str, is_active: bool) -> None:
    db.execute(
        "UPDATE admins SET display_name = ?, role = ?, is_active = ? WHERE id = ?",
        [display_name, role, 1 if is_active else 0, admin_id],
    )


def delete(admin_id: int) -> None:
    db.execute("DELETE FROM admins WHERE id = ?", [admin_id])


def touch_login(admin_id: int) -> None:
    db.execute("UPDATE admins SET last_login_at = datetime('now', 'localtime') WHERE id = ?", [admin_id])
