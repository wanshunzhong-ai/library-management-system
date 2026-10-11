"""后台账号与操作日志接口（仅系统管理员）。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.accounts import service
from library.auth.permissions import require_permission
from library.auth.service import current_admin

bp = Blueprint("accounts", __name__)


def _actor_id() -> int | None:
    admin = current_admin()
    return int(admin["id"]) if admin else None


# ---------------------------------------------------------------- 账号管理


@bp.get("/api/accounts")
@require_permission("account:manage")
def list_accounts():
    return jsonify(service.list_accounts())


@bp.get("/api/accounts/<int:account_id>")
@require_permission("account:manage")
def get_account(account_id: int):
    return jsonify(service.get_account(account_id))


@bp.post("/api/accounts")
@require_permission("account:manage")
def create_account():
    account = service.create_account(request.get_json(silent=True) or {})
    audit.log(
        "account.create",
        entity="account",
        entity_id=account.get("id"),
        summary=f"新建后台账号「{account.get('username')}」（{account.get('role_label')}）",
        detail={"role": account.get("role")},
    )
    return jsonify(account), 201


@bp.put("/api/accounts/<int:account_id>")
@require_permission("account:manage")
def update_account(account_id: int):
    account, changes = service.update_account(
        account_id, request.get_json(silent=True) or {}, actor_id=_actor_id()
    )
    if changes:
        audit.log(
            "account.update",
            entity="account",
            entity_id=account_id,
            summary=f"修改后台账号「{account.get('username')}」：" + "；".join(changes),
            detail={"changes": changes},
        )
    return jsonify(account)


@bp.post("/api/accounts/<int:account_id>/password")
@require_permission("account:manage")
def reset_account_password(account_id: int):
    account = service.set_password(account_id, request.get_json(silent=True) or {})
    audit.log(
        "account.password",
        entity="account",
        entity_id=account_id,
        summary=f"重置后台账号「{account.get('username')}」的密码",
    )
    return jsonify({**account, "message": "密码已重置，请通知对方使用新密码登录"})


@bp.delete("/api/accounts/<int:account_id>")
@require_permission("account:manage")
def delete_account(account_id: int):
    account = service.delete_account(account_id, actor_id=_actor_id())
    audit.log(
        "account.delete",
        entity="account",
        entity_id=account_id,
        summary=f"删除后台账号「{account.get('username')}」（原角色 {account.get('role_label')}）",
        detail={"role": account.get("role")},
    )
    return jsonify({"deleted": account_id})


# ---------------------------------------------------------------- 操作日志


@bp.get("/api/logs")
@require_permission("log:read")
def list_logs():
    return jsonify(service.list_logs(request.args.to_dict(flat=True)))


@bp.get("/api/logs/overview")
@require_permission("log:read")
def log_overview():
    return jsonify(service.log_overview())
