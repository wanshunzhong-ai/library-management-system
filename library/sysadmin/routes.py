"""系统维护接口（仅系统管理员）：调度 / 邮件渠道 / 备份。

全部落在 ``/api/`` 前缀（非 reader / public），因此自动吃管理员会话与管理员 CSRF。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file

from library import audit
from library.auth.permissions import require_permission
from library.errors import NotFoundError, ValidationError
from library.sysadmin import service

bp = Blueprint("sysadmin", __name__, url_prefix="/api/system")


# ---------------------------------------------------------------- 调度


@bp.get("/jobs")
@require_permission("account:manage")
def jobs():
    return jsonify(service.jobs_overview())


@bp.post("/jobs/<job>/run")
@require_permission("account:manage")
@audit.audit_action("system.job_run", entity="system", summary_of=lambda kw, _p: f"手动执行后台任务 {kw.get('job')}")
def run_job(job: str):
    try:
        return jsonify(service.run_job_now(job))
    except ValueError as exc:
        raise ValidationError(str(exc), details={"field": "job"}) from exc


# ---------------------------------------------------------------- 邮件渠道


@bp.get("/mail")
@require_permission("account:manage")
def mail_status():
    return jsonify(service.mail_overview())


@bp.post("/mail/test")
@require_permission("account:manage")
@audit.audit_action("system.mail_test", entity="system", summary_of=lambda _kw, p: f"发送邮件渠道测试到 {(p or {}).get('to') or ''}")
def mail_test():
    payload = request.get_json(silent=True) or {}
    to_address = str(payload.get("to") or "").strip()
    if not to_address:
        raise ValidationError("请填写收件邮箱", details={"field": "to"})
    return jsonify(service.send_test_mail(to_address))


@bp.post("/mail/retry")
@require_permission("account:manage")
@audit.audit_action("system.mail_retry", entity="system", summary_of=lambda _kw, _p: "重试发送失败的邮件")
def mail_retry():
    return jsonify(service.retry_failed_mail())


# ---------------------------------------------------------------- 备份


@bp.get("/backups")
@require_permission("account:manage")
def backups():
    return jsonify(service.backups_overview())


@bp.post("/backups")
@require_permission("account:manage")
@audit.audit_action("system.backup", entity="system", summary_of=lambda _kw, p: f"创建数据库备份（含附件：{'是' if (p or {}).get('with_uploads') else '否'}）")
def create_backup():
    payload = request.get_json(silent=True) or {}
    return jsonify(service.create_backup(with_uploads=service.wants_uploads(payload)))


@bp.get("/backups/<name>")
@require_permission("account:manage")
def download_backup(name: str):
    try:
        path = service.backup_path(name)  # 内部做路径穿越校验
    except ValueError as exc:
        raise NotFoundError("备份文件不存在") from exc
    return send_file(path, as_attachment=True, download_name=path.name)
