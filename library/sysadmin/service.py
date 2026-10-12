"""系统维护服务：把「后台线程 / 邮件渠道 / 备份」三块运维信息拼成可读的卡片数据。"""

from __future__ import annotations

import json
from typing import Any, Mapping

from library import backup as backup_module
from library import db

RECENT_LIMIT = 20
JOB_LABELS = {"sweep": "后台扫描（预约释放 + 到期提醒 + 邮件出站）"}


def _job_config():
    from config import get_config  # noqa: PLC0415

    return get_config()


def jobs_overview() -> dict[str, Any]:
    """调度卡片：线程是否在跑、间隔多久、最近若干轮的结果。"""
    from library import scheduler  # noqa: PLC0415

    cfg = _job_config()
    rows = db.query_all(
        "SELECT id, job, kind, started_at, finished_at, duration_ms, ok, result, error "
        "FROM job_runs ORDER BY id DESC LIMIT ?",
        [RECENT_LIMIT],
    )
    recent = []
    for row in rows:
        item = dict(row)
        try:
            item["result_data"] = json.loads(item.get("result") or "") if item.get("result") else {}
        except (ValueError, TypeError):
            item["result_data"] = {}
        item["kind_label"] = "手动触发" if item.get("kind") == "manual" else "定时"
        item["job_label"] = JOB_LABELS.get(str(item.get("job") or ""), str(item.get("job") or ""))
        recent.append(item)

    last = recent[0] if recent else None
    return {
        "interval_seconds": int(cfg.sweep_interval_seconds),
        "enabled": int(cfg.sweep_interval_seconds) > 0,
        "thread_running": scheduler.is_running(),
        "total_runs": int((db.query_one("SELECT COUNT(*) AS n FROM job_runs") or {"n": 0})["n"]),
        "last_run": last,
        "recent": recent,
        "jobs": [{"key": "sweep", "label": JOB_LABELS["sweep"]}],
    }


def run_job_now(job: str = "sweep") -> dict[str, Any]:
    """手动跑一轮（管理员在系统维护页点「立即执行」）。

    幂等：扫描逻辑本身按条件更新 + 唯一幂等键兜底，重复点击不会重复发消息。
    """
    from library import scheduler  # noqa: PLC0415

    job = str(job or "sweep").strip() or "sweep"
    if job not in JOB_LABELS:
        raise ValueError(f"未知的后台任务：{job}")
    result = scheduler.run_once(kind="manual")
    return {"job": job, "job_label": JOB_LABELS[job], "result": result}


def mail_overview() -> dict[str, Any]:
    from library.notifications import outbound  # noqa: PLC0415

    return outbound.channel_status()


def send_test_mail(to_address: str) -> dict[str, Any]:
    from library.notifications import outbound  # noqa: PLC0415

    return outbound.send_test(to_address)


def retry_failed_mail() -> dict[str, Any]:
    from library.notifications import outbound  # noqa: PLC0415

    count = outbound.retry_failed()
    return {"reset": count, "status": outbound.channel_status()}


def backups_overview() -> dict[str, Any]:
    items = backup_module.list_backups()
    return {
        "directory": str(backup_module.backup_dir()),
        "items": items,
        "total": len(items),
    }


def create_backup(*, with_uploads: bool = False) -> dict[str, Any]:
    return backup_module.create_backup(with_uploads=bool(with_uploads))


def backup_path(name: str):
    return backup_module.resolve_backup(name)


def wants_uploads(payload: Mapping[str, Any] | None) -> bool:
    return bool((payload or {}).get("with_uploads"))
