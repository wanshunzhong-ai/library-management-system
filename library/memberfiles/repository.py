"""读者附件（照片 / 证件）数据访问层。

只存**随机存储名**与展示用原始文件名，绝不把磁盘绝对路径写进库：
路径由「上传根目录 + 读者 id + 存储名」拼出来，因此数据库被带走也不会泄露部署路径，
也不存在从库里读到 `../../etc/passwd` 这种路径穿越的入口。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_COLUMNS = (
    "id, member_id, kind, label, filename, stored_name, mime, size, operator, created_at"
)

KIND_LABELS = {"photo": "读者照片", "id_card": "证件影像"}


def list_for_member(member_id: int, *, kind: str = "") -> list[dict[str, Any]]:
    if kind:
        return db.query_all(
            f"SELECT {_COLUMNS} FROM member_files WHERE member_id = ? AND kind = ? ORDER BY id DESC",
            [member_id, kind],
        )
    return db.query_all(
        f"SELECT {_COLUMNS} FROM member_files WHERE member_id = ? ORDER BY kind, id DESC",
        [member_id],
    )


def get(file_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM member_files WHERE id = ?", [file_id])


def count_for_member(member_id: int, *, kind: str) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM member_files WHERE member_id = ? AND kind = ?",
            [member_id, kind],
        )
        or 0
    )


def insert(payload: Mapping[str, Any]) -> int:
    return db.execute(
        "INSERT INTO member_files "
        "(member_id, kind, label, filename, stored_name, mime, size, operator) "
        "VALUES (:member_id, :kind, :label, :filename, :stored_name, :mime, :size, :operator)",
        dict(payload),
    )


def delete(file_id: int) -> None:
    db.execute("DELETE FROM member_files WHERE id = ?", [file_id])


def photo_of(member_id: int) -> dict[str, Any] | None:
    """取该读者最新的一张照片（列表页头像用）。"""
    return db.query_one(
        f"SELECT {_COLUMNS} FROM member_files WHERE member_id = ? AND kind = 'photo' "
        "ORDER BY id DESC LIMIT 1",
        [member_id],
    )
