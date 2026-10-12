"""站内通知的读者侧接口。

前缀刻意落在 ``/api/reader/`` 之下——全局守卫是按**路径前缀**区分身份的，
放在这里才能自动落到读者会话与读者 CSRF 上，不必另开一套鉴权分支。
（馆员给读者发通知的接口挂在 ``library.members.routes``，因为那是对读者的操作。）
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library.errors import UnauthorizedError
from library.notifications import service
from library.reader import service as reader_service

bp = Blueprint("notifications", __name__, url_prefix="/api/reader/notifications")


def _require_reader() -> dict:
    member = reader_service.current_reader()
    if member is None:
        raise UnauthorizedError("请先登录读者账号")
    return member


@bp.get("")
def list_notifications():
    member = _require_reader()
    return jsonify(service.list_my_notifications(int(member["id"]), request.args.to_dict(flat=True)))


@bp.post("/read-all")
def mark_all_read():
    member = _require_reader()
    return jsonify(service.mark_all_read(int(member["id"])))


@bp.post("/<int:notification_id>/read")
def mark_read(notification_id: int):
    member = _require_reader()
    return jsonify(service.mark_read(int(member["id"]), notification_id))
