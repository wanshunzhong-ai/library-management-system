"""批量导入 REST 接口。

* ``POST /api/import/books``   —— 图书批量导入（需 ``book:write``）
* ``POST /api/import/members`` —— 读者批量导入（需 ``member:write``）
* ``GET  /api/import/*/template`` —— 下载对应模板（带 BOM 的 CSV，Excel 直接可开）

请求体支持两种来源，**校验与报错逻辑完全共用**：

* ``{"csv": "书名,ISBN,...\\n..."}`` —— 粘贴的文本，或前端按文本读出来的 .csv 文件；
* ``{"xlsx": "<base64>"}`` —— 上传的 Excel（.xlsx），服务端用标准库解开后转成 CSV 文本，
  再交给同一条解析链路（见 ``library.importer.xlsx``）。

模板由**服务端**生成：导入解析器认哪些表头，模板就写哪些表头，两处不会各说各话。
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.importer import service

bp = Blueprint("imports", __name__, url_prefix="/api/import")


def _csv_response(text: str, filename: str) -> Response:
    # 带 BOM：Excel / WPS 双击打开不乱码
    body = "\ufeff" + text
    response = Response(body, mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@bp.post("/books")
@require_permission("book:write")
@audit.audit_action(
    "import.books",
    entity="book",
    summary_of=lambda _kw, p: (
        f"{'预览' if (p or {}).get('dry_run') else '导入'}图书 "
        f"{int((p or {}).get('created') or 0)} 条，失败 {int((p or {}).get('failed') or 0)} 条"
    ),
)
def import_books():
    payload = request.get_json(silent=True) or {}
    csv_text = service.resolve_table_text(payload)
    return jsonify(service.import_books(csv_text, dry_run=bool(payload.get("dry_run"))))


@bp.post("/members")
@require_permission("member:write")
@audit.audit_action(
    "import.members",
    entity="member",
    summary_of=lambda _kw, p: (
        f"{'预览' if (p or {}).get('dry_run') else '导入'}读者 "
        f"{int((p or {}).get('created') or 0)} 条，失败 {int((p or {}).get('failed') or 0)} 条"
    ),
)
def import_members():
    payload = request.get_json(silent=True) or {}
    csv_text = service.resolve_table_text(payload)
    return jsonify(service.import_members(csv_text, dry_run=bool(payload.get("dry_run"))))


@bp.get("/books/template")
@require_permission("book:read")
def book_template():
    return _csv_response(service.book_template(), "books-template.csv")


@bp.get("/members/template")
@require_permission("member:read")
def member_template():
    return _csv_response(service.member_template(), "members-template.csv")
