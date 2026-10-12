"""数据库备份与恢复：把「运维动作」从「改代码」里拿出来。

## 做法与取舍

* **用 SQLite 的在线备份 API**（``sqlite3.Connection.backup``）而不是直接拷文件：
  拷贝一个正在被写入的库有拿到半截事务的风险；在线备份由 SQLite 自己保证快照一致性，
  而且**不阻塞正在服务的读写**（可以随时热备，不必停服）。
* **备份名带时间戳**（``library-YYYYMMDD-HHMMSS.db``），旁边落一份 ``.json`` 摘要
  （版本、表行数、附件文件数）——恢复前先看摘要，比看文件名猜要靠得住。
* **``--with-uploads`` 打 zip**：库和附件目录是一体的（附件只在库里存文件名），
  要迁移整站就一起打包，免得恢复后附件全成死链。
* **恢复默认先留退路**：覆盖前把当前库另存为 ``library-pre-restore-<ts>.db``，
  万一恢复错了还能回头；``--yes`` 只是跳过交互确认，不是跳过安全副本。
* **只做冷恢复**：恢复必须停服后执行（CLI 命令），后台只提供「备份 + 下载」，
  不提供在线恢复——把正在运行的库原地换掉是自找麻烦。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CORE_TABLES = ("books", "members", "loans", "admins")
_SQLITE_MAGIC = b"SQLite format 3\x00"


def backup_dir() -> Path:
    from config import get_config  # noqa: PLC0415

    raw = str(get_config().backup_dir or "").strip()
    base = Path(raw).expanduser() if raw else Path(get_config().db_path).expanduser().parent / "backups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _db_path() -> Path:
    from config import get_config  # noqa: PLC0415

    return Path(get_config().db_path).expanduser()


def _counts(path: Path) -> dict[str, int]:
    """统计核心表行数（写进摘要，恢复前先看它比看文件名靠谱）。

    必须**显式关闭 cursor**：Windows 上未关闭的游标会拖住文件句柄，
    紧接着删除临时库文件就会报 WinError 32。
    """
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(path))
        try:
            for table in _CORE_TABLES:
                cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                try:
                    counts[table] = int(cur.fetchone()[0])
                except (sqlite3.Error, TypeError):
                    counts[table] = 0
                finally:
                    cur.close()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("统计备份表行数失败（已忽略）", exc_info=True)
    return counts


def create_backup(*, with_uploads: bool = False) -> dict[str, Any]:
    """做一份一致性快照。返回 {name, path, size_bytes, size_label, counts, uploaded_files, with_uploads}。"""
    from library import timeutil  # noqa: PLC0415

    source = _db_path()
    if not source.exists():
        raise FileNotFoundError(f"数据库不存在：{source}")
    stamp = timeutil.now().strftime("%Y%m%d-%H%M%S")
    target_dir = backup_dir()
    db_target = target_dir / f"library-{stamp}.db"

    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(db_target))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    manifest = {
        "created_at": timeutil.now().isoformat(timespec="seconds"),
        "source": str(source),
        "database": db_target.name,
        "counts": _counts(db_target),
        "uploaded_files": 0,
        "with_uploads": bool(with_uploads),
    }

    if with_uploads:
        upload_files = _upload_files()
        manifest["uploaded_files"] = len(upload_files)
        zip_target = target_dir / f"library-{stamp}.zip"
        with zipfile.ZipFile(zip_target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_target, arcname=db_target.name)
            for path in upload_files:
                root = _upload_root()
                zf.write(path, arcname=str(Path("uploads") / path.relative_to(root)))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        # zip 已经收好数据库了，中间产物删掉即可；Windows 上若句柄尚未释放删不掉，
        # 留着一个同名的 .db 也无害（内容一致），不能因此让整次备份失败。
        try:
            db_target.unlink(missing_ok=True)
        except OSError:
            logger.info("备份中间文件未能删除（Windows 句柄延迟），已保留")
        result_path = zip_target
    else:
        result_path = db_target
        manifest["uploaded_files"] = len(_upload_files())

    # 两种形态都写一份摘要 sidecar：备份列表靠它显示数据量与创建时间，
    # 恢复前也该先看摘要——比盯着文件名猜可靠得多。
    (target_dir / f"library-{stamp}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    size = result_path.stat().st_size
    logger.info(
        "数据库已备份",
        extra={"extra_fields": {"path": str(result_path), "size": size, "with_uploads": with_uploads}},
    )
    return {
        "name": result_path.name,
        "path": str(result_path),
        "size_bytes": size,
        "size_label": _human_size(size),
        "counts": manifest["counts"],
        "uploaded_files": manifest["uploaded_files"],
        "with_uploads": bool(with_uploads),
        "created_at": manifest["created_at"],
    }


def _upload_root() -> Path:
    from config import get_config  # noqa: PLC0415

    raw = str(get_config().upload_dir or "").strip()
    root = Path(raw).expanduser() if raw else Path(get_config().db_path).expanduser().parent / "uploads"
    return root


def _upload_files() -> list[Path]:
    root = _upload_root()
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def list_backups() -> list[dict[str, Any]]:
    """按时间倒序列出备份（含 .db 与 .zip，附带可读的摘要信息）。"""
    directory = backup_dir()
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("library-*"), key=lambda p: p.name, reverse=True):
        if path.is_dir() or path.suffix == ".json":
            continue
        summary: dict[str, Any] = {}
        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                summary = json.loads(sidecar.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                summary = {}
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "size_label": _human_size(stat.st_size),
                "kind": "zip" if path.suffix == ".zip" else "db",
                "created_at": (summary.get("created_at") or "").replace("T", " "),
                "counts": summary.get("counts") or {},
                "with_uploads": bool(summary.get("with_uploads")),
                "safe_to_restore": _is_restorable(path),
            }
        )
    return items


def resolve_backup(name: str) -> Path:
    """把备份名解析成路径，**只允许备份目录下的普通文件**（防路径穿越）。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("备份文件名不合法")
    candidate = (backup_dir() / name).resolve()
    root = backup_dir().resolve()
    if candidate.parent != root or not candidate.is_file():
        raise ValueError("备份文件不存在")
    return candidate


def _is_restorable(path: Path) -> bool:
    try:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                return any(n.endswith(".db") for n in zf.namelist())
        with path.open("rb") as fh:
            return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except (OSError, zipfile.BadZipFile):
        return False


def restore_backup(source: Path, *, make_safety_copy: bool = True) -> dict[str, Any]:
    """用备份覆盖当前数据库。**必须在服务停止后执行。**

    返回 {restored_from, safety_copy, counts}；``safety_copy`` 是覆盖前的当前库副本路径。
    """
    from library import timeutil  # noqa: PLC0415

    source = Path(source).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"备份文件不存在：{source}")

    tmp_db: Path | None = None
    if source.suffix == ".zip":
        with zipfile.ZipFile(source) as zf:
            names = [n for n in zf.namelist() if n.endswith(".db")]
            if not names:
                raise ValueError("压缩包里没有数据库文件")
            tmp = backup_dir() / f"_restore-tmp-{timeutil.now().strftime('%Y%m%d-%H%M%S')}.db"
            with zf.open(names[0]) as src_fh, tmp.open("wb") as dst_fh:
                shutil.copyfileobj(src_fh, dst_fh)
            tmp_db = tmp
    else:
        with source.open("rb") as fh:
            if fh.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                raise ValueError("这不是一个合法的 SQLite 数据库文件")

    incoming = tmp_db or source
    target = _db_path()
    safety: Path | None = None
    if make_safety_copy and target.exists():
        safety = target.parent / f"library-pre-restore-{timeutil.now().strftime('%Y%m%d-%H%M%S')}.db"
        shutil.copy2(target, safety)

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(incoming, target)
    if tmp_db is not None:
        tmp_db.unlink(missing_ok=True)

    counts = _counts(target)
    logger.warning(
        "数据库已从备份恢复",
        extra={"extra_fields": {"source": str(source), "safety_copy": str(safety) if safety else ""}},
    )
    return {
        "restored_from": str(source),
        "database": str(target),
        "safety_copy": str(safety) if safety else "",
        "counts": counts,
    }


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
