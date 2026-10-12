"""读者附件业务逻辑：照片与证件影像的上传、读取与删除。

## 安全约束（每一条都是必要的）

1. **类型白名单**：只收 JPEG / PNG / WebP / PDF。不看扩展名就存，等于给上传者
   一个在服务器上放任意文件的机会。
2. **魔数校验**：扩展名可以随便改，所以还要看文件头字节是否与声明的类型相符。
3. **随机存储名**：磁盘文件名由服务端生成（``secrets.token_hex``），完全不用
   用户提供的名字——否则 ``../../app.py`` 这种文件名就是一个写文件漏洞。
4. **路径归属校验**：读取时把拼出的绝对路径 resolve 后确认仍在上传根目录内，
   作为最后一道防线。
5. **鉴权下载**：附件不属于 ``/api/public/*``，必须登录后台才能取，
   不生成任何长期有效的公开直链。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Mapping

from config import get_config
from library.errors import BusinessRuleError, NotFoundError, ValidationError
from library.memberfiles import repository as repo
from library.members import repository as member_repo
from library.validation import optional_str

logger = logging.getLogger(__name__)

KINDS = set(repo.KIND_LABELS)
# 扩展名 → 允许的 MIME；同时作为「该扩展名能否上传」的白名单
EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
# 魔数前缀（文件头字节）用于确认内容与声明类型一致；WebP 还要额外核对 RIFF 容器里的标记
MAX_FILES_PER_KIND = 10


def upload_root() -> Path:
    """附件根目录。

    配置缺省（``upload_dir`` 为空，例如测试里直接构造 ``Config``）时退回「数据库同级的
    ``uploads/``」——不能退化成 ``Path("")``，那等于把附件写进进程当前目录。
    """
    configured = str(get_config().upload_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(get_config().db_path).expanduser().parent / "uploads"


def _safe_ext(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in EXT_MIME:
        raise ValidationError(
            "只支持 JPG / PNG / WebP / PDF 四种格式", details={"field": "file"}
        )
    return ext


def _sniff(ext: str, head: bytes) -> str:
    """按文件头确认内容类型，返回 MIME；不匹配则拒绝。"""
    expected = EXT_MIME[ext]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        actual = "image/webp"
    elif head.startswith(b"%PDF-"):
        actual = "application/pdf"
    else:
        raise ValidationError(
            "文件内容与扩展名不符，已拒绝保存", details={"field": "file"}
        )
    if actual != expected:
        raise ValidationError(
            f"文件声明为 {expected}，实际内容是 {actual}，已拒绝保存",
            details={"field": "file"},
        )
    return actual


def path_of(record: Mapping[str, Any]) -> Path:
    """由记录拼出磁盘绝对路径，并确认它确实落在上传根目录内。"""
    root = upload_root().resolve()
    target = (root / str(int(record["member_id"])) / str(record["stored_name"])).resolve()
    if root not in target.parents and target.parent != root:
        # 正常情况永远不会走到这里；留着是为了让「拼接路径」这件事有硬边界
        raise BusinessRuleError("附件路径不合法")
    return target


def list_files(member_id: int) -> dict[str, Any]:
    member_id = int(member_id)
    if member_repo.get(member_id) is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    rows = repo.list_for_member(member_id)
    items = [_decorate(row) for row in rows]
    return {
        "items": items,
        "photos": [item for item in items if item["kind"] == "photo"],
        "id_cards": [item for item in items if item["kind"] == "id_card"],
        "kind_labels": repo.KIND_LABELS,
        "max_files_per_kind": MAX_FILES_PER_KIND,
        "max_size_mb": int(get_config().upload_max_mb),
    }


def save(member_id: int, form: Mapping[str, Any], file_storage, *, operator: str = "") -> dict[str, Any]:
    """保存一个上传文件。返回新记录（含可供 <img> 直接引用的 url）。"""
    member_id = int(member_id)
    if member_repo.get(member_id) is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    if file_storage is None or not getattr(file_storage, "filename", ""):
        raise ValidationError("请选择要上传的文件", details={"field": "file"})

    kind = str(form.get("kind") or "photo").strip() or "photo"
    if kind not in KINDS:
        raise ValidationError(f"附件类型只能是 {'/'.join(sorted(KINDS))} 之一", details={"field": "kind"})
    if repo.count_for_member(member_id, kind=kind) >= MAX_FILES_PER_KIND:
        raise BusinessRuleError(
            f"每位读者的「{repo.KIND_LABELS[kind]}」最多保留 {MAX_FILES_PER_KIND} 个，请先删除旧文件"
        )

    filename = str(file_storage.filename)
    ext = _safe_ext(filename)
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    mime = _sniff(ext, head)

    max_bytes = int(get_config().upload_max_mb) * 1024 * 1024
    data = file_storage.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValidationError(
            f"文件超过 {int(get_config().upload_max_mb)}MB 上限", details={"field": "file"}
        )
    if not data:
        raise ValidationError("文件内容为空", details={"field": "file"})

    stored_name = secrets.token_hex(16) + ext
    folder = upload_root() / str(member_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / stored_name).write_bytes(data)

    file_id = repo.insert(
        {
            "member_id": member_id,
            "kind": kind,
            "label": optional_str(form, "label", label="说明", max_length=50),
            "filename": filename[:200],
            "stored_name": stored_name,
            "mime": mime,
            "size": len(data),
            "operator": operator,
        }
    )
    logger.info(
        "保存读者附件",
        extra={"extra_fields": {"member_id": member_id, "kind": kind, "size": len(data)}},
    )
    return _decorate(repo.get(file_id) or {})


def get_file(file_id: int) -> dict[str, Any]:
    row = repo.get(int(file_id))
    if row is None:
        raise NotFoundError(f"附件 #{file_id} 不存在")
    path = path_of(row)
    if not path.exists():
        raise NotFoundError("附件文件已丢失（可能被手工清理过）")
    return row


def delete_file(file_id: int) -> dict[str, Any]:
    row = get_file(int(file_id))
    try:
        path_of(row).unlink()
    except OSError:  # 文件已不在也允许删记录，否则会留下永远删不掉的脏行
        logger.warning("附件文件删除失败（仅删记录）", exc_info=True)
    repo.delete(int(file_id))
    logger.info("删除读者附件", extra={"extra_fields": {"file_id": file_id}})
    return {"deleted": int(file_id), "member_id": int(row["member_id"]), "kind": row["kind"]}


def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["kind_label"] = repo.KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))
    item["url"] = f"/api/member-files/{int(item['id'])}"
    item["size_label"] = _size_label(int(item.get("size") or 0))
    item["is_image"] = str(item.get("mime") or "").startswith("image/")
    return item


def _size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"
