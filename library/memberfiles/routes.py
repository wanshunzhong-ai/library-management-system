"""读者附件 REST 接口（需 `member:write`）。

上传走 multipart，下载走鉴权后的 ``send_file``（不生成公开直链）。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, make_response, request, send_file

from library import audit
from library.auth.permissions import require_permission
from library.auth.service import current_admin
from library.memberfiles import service

bp = Blueprint("member_files", __name__, url_prefix="/api/member-files")


def _operator() -> str:
    admin = current_admin()
    if not admin:
        return ""
    return str(admin.get("display_name") or admin.get("username") or "")


def _member_id() -> int:
    raw = request.values.get("member_id")
    if raw in (None, ""):
        from library.errors import ValidationError

        raise ValidationError("缺少读者 ID", details={"field": "member_id"})
    return int(raw)


@bp.get("")
@require_permission("member:read")
def list_files():
    return jsonify(service.list_files(_member_id()))


@bp.post("")
@require_permission("member:write")
@audit.audit_action(
    "member_file.upload",
    entity="member",
    summary_of=lambda _kw, p: f"上传读者附件「{(p or {}).get('kind_label') or ''}」",
)
def upload():
    result = service.save(
        _member_id(), request.form, request.files.get("file"), operator=_operator()
    )
    return jsonify(result), 201


@bp.get("/<int:file_id>")
@require_permission("member:read")
def download(file_id: int):
    row = service.get_file(file_id)
    path = service.path_of(row)
    response = make_response(
        send_file(
            path,
            mimetype=str(row.get("mime") or "application/octet-stream"),
            as_attachment=False,
            download_name=str(row.get("filename") or "attachment"),
            conditional=True,
        )
    )
    # 附件是个人信息，禁止任何中间层缓存；图片在页面里仍可正常内联显示
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.delete("/<int:file_id>")
@require_permission("member:write")
@audit.audit_action("member_file.delete", entity="member", summary_of=lambda _kw, _p: "删除读者附件")
def delete_file(file_id: int):
    return jsonify(service.delete_file(file_id))
