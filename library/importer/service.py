"""批量导入业务逻辑（图书 / 读者）。

## 设计要点

* **逐行独立**：每一行都是一次独立的创建调用，单行失败不影响其他行，最终返回
  「第 N 行：失败原因」的清单。柜面导入 200 条数据，不该因为第 7 条 ISBN 写错
  就整批不回滚也不该整批失败。
* **先预览后写入**：``dry_run=True`` 时只做校验（字段、唯一性、文件内重复），
  不落库，前端可以先把错误清单摆给馆员看，确认无误再正式导入。
* **文件内重复也要拦**：同一份 CSV 里出现两条相同 ISBN / 证号，如果只依赖数据库
  唯一索引，第二行会报「已存在」——提示是对的，但馆员会以为是库里已有。
  这里单独把「文件内重复」拎出来说清楚。
* **不吞异常**：只有业务错误（``AppError``）被记为该行失败；编程错误（500）
  直接向上抛，避免把 bug 伪装成「数据有问题」。
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
from typing import Any, Mapping

from library.books import service as book_service
from library.errors import AppError, ValidationError
from library.importer import xlsx as xlsx_reader
from library.members import service as member_service

logger = logging.getLogger(__name__)

MAX_ROWS = 1000
MAX_TEXT_CHARS = 1_000_000  # 单次导入的 CSV 体量上限（约 1MB 文本）


def resolve_table_text(payload: Mapping[str, Any]) -> str:
    """从请求体取表格文本：``xlsx``（base64 Excel）优先，其次 ``csv``（粘贴 / 文本文件）。

    Excel 走「解开 → 转 CSV 文本」这一条路，于是**三种入口共用同一套表头识别、
    逐行校验与报错文案**——不会像两套解析器那样各说各话。
    """
    raw_xlsx = str(payload.get("xlsx") or "").strip()
    if raw_xlsx:
        try:
            data = base64.b64decode(raw_xlsx, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("Excel 文件内容无法识别，请重新选择文件", details={"field": "xlsx"}) from exc
        if not xlsx_reader.looks_like_xlsx(data):
            raise ValidationError(
                "这不是有效的 .xlsx 文件（旧版 .xls 请先用 Excel 另存为 .xlsx）",
                details={"field": "xlsx"},
            )
        try:
            rows = xlsx_reader.parse_xlsx(data)
        except xlsx_reader.XlsxError as exc:
            raise ValidationError(str(exc), details={"field": "xlsx"}) from exc
        if not rows:
            raise ValidationError("这个 Excel 里没有可导入的内容", details={"field": "xlsx"})
        if len(rows) - 1 > MAX_ROWS:
            raise ValidationError(
                f"单次最多导入 {MAX_ROWS} 行，当前文件有 {len(rows) - 1} 行（含表头共 {len(rows)} 行）",
                details={"field": "xlsx"},
            )
        logger.info("Excel 已转换为导入文本", extra={"extra_fields": {"rows": len(rows) - 1}})
        return xlsx_reader.to_csv_text(rows)
    return str(payload.get("csv") or "")

# 表头别名：中英文都能认，避免馆员因为表头写法被卡住
BOOK_HEADERS = {
    "title": "书名",
    "isbn": "isbn",
    "author": "作者",
    "publisher": "出版社",
    "category": "分类",
    "publish_year": "publish_year",
    "location": "馆藏位置",
    "total_copies": "总册数",
    "barcode": "馆藏条码",
    "description": "图书简介",
}
_MEMBER_HEADERS = {
    "name": "姓名",
    "card_no": "读者证号",
    "member_type": "读者类型",
    "phone": "手机号",
    "email": "邮箱",
    "max_borrow": "可借上限",
    "status": "状态",
}

# 行首的 BOM、全角空格、零宽字符都可能在从 Excel 复制时混进来，统一清掉
_STRIP_CHARS = "\ufeff\u200b\u3000 \t\r\n"


def import_books(csv_text: str, *, dry_run: bool = False) -> dict[str, Any]:
    rows, header_map = _parse(csv_text, BOOK_HEADERS, required=("title", "isbn"))
    result = _new_result("books", dry_run, len(rows))
    seen_isbn: set[str] = set()
    seen_barcode: set[str] = set()

    for index, raw in enumerate(rows, start=2):  # 第 1 行是表头
        title = _cell(raw, header_map, "title")
        try:
            if not title:
                raise ValidationError("书名不能为空", details={"field": "title"})
            payload = {
                "title": title,
                "isbn": _cell(raw, header_map, "isbn"),
                "author": _cell(raw, header_map, "author"),
                "publisher": _cell(raw, header_map, "publisher"),
                "category": _cell(raw, header_map, "category"),
                "publish_year": _cell(raw, header_map, "publish_year"),
                "location": _cell(raw, header_map, "location"),
                "total_copies": _cell(raw, header_map, "total_copies") or 1,
                "barcode": _cell(raw, header_map, "barcode"),
                "description": _cell(raw, header_map, "description"),
            }
            normalized = book_service.validate_book(payload)
            isbn = str(normalized["isbn"])
            barcode = str(normalized.get("barcode") or "")
            if isbn in seen_isbn:
                raise ValidationError(f"文件内重复：ISBN {isbn} 在前面已出现过")
            if barcode and barcode in seen_barcode:
                raise ValidationError(f"文件内重复：馆藏条码 {barcode} 在前面已出现过")
            seen_isbn.add(isbn)
            if barcode:
                seen_barcode.add(barcode)

            if dry_run:
                result["rows"].append(
                    {"line": index, "status": "ok", "message": "校验通过", "label": title}
                )
            else:
                created = book_service.create_book(payload)
                result["created"] += 1
                result["rows"].append(
                    {
                        "line": index,
                        "status": "ok",
                        "message": f"已新增（#{created['id']}）",
                        "label": title,
                    }
                )
        except AppError as exc:
            result["failed"] += 1
            result["rows"].append(
                {"line": index, "status": "failed", "message": exc.message, "label": title or "（空书名）"}
            )

    return _finish(result)


def import_members(csv_text: str, *, dry_run: bool = False) -> dict[str, Any]:
    rows, header_map = _parse(csv_text, _MEMBER_HEADERS, required=("name", "card_no"))
    result = _new_result("members", dry_run, len(rows))
    seen_card: set[str] = set()
    seen_phone: set[str] = set()

    for index, raw in enumerate(rows, start=2):
        name = _cell(raw, header_map, "name")
        card_no = _cell(raw, header_map, "card_no")
        try:
            if not name:
                raise ValidationError("姓名不能为空", details={"field": "name"})
            if not card_no:
                raise ValidationError("读者证号不能为空", details={"field": "card_no"})
            phone = _cell(raw, header_map, "phone")
            if card_no in seen_card:
                raise ValidationError(f"文件内重复：读者证号 {card_no} 在前面已出现过")
            if phone and phone in seen_phone:
                raise ValidationError(f"文件内重复：手机号 {phone} 在前面已出现过")
            seen_card.add(card_no)
            if phone:
                seen_phone.add(phone)

            payload = {
                "name": name,
                "card_no": card_no,
                "member_type": _cell(raw, header_map, "member_type") or "社会",
                "phone": phone,
                "email": _cell(raw, header_map, "email"),
                "max_borrow": _cell(raw, header_map, "max_borrow"),
                "status": _cell(raw, header_map, "status") or "active",
            }
            member_service.validate_member(payload)

            if dry_run:
                result["rows"].append(
                    {"line": index, "status": "ok", "message": "校验通过", "label": f"{name}（{card_no}）"}
                )
            else:
                created = member_service.create_member(payload)
                result["created"] += 1
                result["rows"].append(
                    {
                        "line": index,
                        "status": "ok",
                        "message": f"已新增（初始密码 {created.get('initial_password') or '—'}）",
                        "label": f"{name}（{card_no}）",
                    }
                )
        except AppError as exc:
            result["failed"] += 1
            result["rows"].append(
                {
                    "line": index,
                    "status": "failed",
                    "message": exc.message,
                    "label": f"{name or '（空姓名）'}（{card_no or '—'}）",
                }
            )

    return _finish(result)


def book_template() -> str:
    return _template(
        ["书名", "ISBN", "作者", "出版社", "分类", "出版年份", "馆藏位置", "总册数", "馆藏条码", "图书简介"],
        [
            ["示例：人类简史", "9787508647357", "尤瓦尔·赫拉利", "中信出版社", "历史", "2014", "A区-3排", "3", "", "简介可留空"],
            ["示例：三体", "9787536692930", "刘慈欣", "重庆出版社", "科幻", "2008", "A区-1排", "5", "", ""],
        ],
    )


def member_template() -> str:
    return _template(
        ["姓名", "读者证号", "读者类型", "手机号", "邮箱", "可借上限", "状态"],
        [
            ["示例：张三", "2026001", "学生", "13800000001", "zhangsan@example.com", "5", "正常"],
            ["示例：李四", "2026002", "教师", "13800000002", "", "10", "正常"],
        ],
    )


# ---------------------------------------------------------------- 内部工具


def _parse(
    csv_text: str, headers: Mapping[str, str], *, required: tuple[str, ...]
) -> tuple[list[dict[str, str]], dict[str, int]]:
    text = str(csv_text or "")
    if not text.strip():
        raise ValidationError("请先粘贴或上传 CSV 内容", details={"field": "csv"})
    if len(text) > MAX_TEXT_CHARS:
        raise ValidationError(
            f"单次导入内容不能超过 {MAX_TEXT_CHARS // 1000}KB，请拆分后分批导入",
            details={"field": "csv"},
        )
    # utf-8-sig 兼容 Excel 导出的 BOM
    stream = io.StringIO(text.lstrip("\ufeff"), newline="")
    reader = csv.reader(stream)
    try:
        header_row = next(reader)
    except StopIteration as exc:
        raise ValidationError("CSV 内容为空", details={"field": "csv"}) from exc

    index_of: dict[str, int] = {}
    aliases = _aliases(headers)
    for position, cell in enumerate(header_row):
        key = str(cell or "").strip().strip(_STRIP_CHARS).lower()
        field = aliases.get(key)
        if field and field not in index_of:
            index_of[field] = position

    missing = [headers[name] for name in required if name not in index_of]
    if missing:
        raise ValidationError(
            f"CSV 缺少必需的列：{'、'.join(missing)}（可下载模板参照表头）",
            details={"field": "csv", "columns": missing},
        )

    rows: list[dict[str, str]] = []
    for row in reader:
        if not any(str(cell or "").strip().strip(_STRIP_CHARS) for cell in row):
            continue  # 跳过空行（Excel 常见尾部空行）
        item: dict[str, str] = {}
        for field, position in index_of.items():
            item[field] = (
                str(row[position] or "").strip().strip(_STRIP_CHARS) if position < len(row) else ""
            )
        rows.append(item)
        if len(rows) > MAX_ROWS:
            raise ValidationError(
                f"单次最多导入 {MAX_ROWS} 行，请拆分后分批导入", details={"field": "csv"}
            )
    if not rows:
        raise ValidationError("CSV 里没有可导入的数据行", details={"field": "csv"})
    return rows, index_of


def _aliases(headers: Mapping[str, str]) -> dict[str, str]:
    """正反两份别名：既认英文字段名，也认中文表头。"""
    table: dict[str, str] = {}
    for field, label in headers.items():
        table[field.lower()] = field
        table[label.lower()] = field
    # 额外补充一些常见写法
    table.update(
        {
            "书号": "isbn",
            "isbn号": "isbn",
            "出版年份": "publish_year",
            "年份": "publish_year",
            "year": "publish_year",
            "位置": "location",
            "册数": "total_copies",
            "copies": "total_copies",
            "条码": "barcode",
            "简介": "description",
            "证号": "card_no",
            "借书证号": "card_no",
            "card": "card_no",
            "类型": "member_type",
            "电话": "phone",
            "手机": "phone",
            "状态": "status",
            "active": "status",
            "上限": "max_borrow",
        }
    )
    return table


def _cell(row: Mapping[str, str], header_map: Mapping[str, int], field: str) -> str:
    if field not in header_map:
        return ""
    return str(row.get(field) or "").strip().strip(_STRIP_CHARS)


def _new_result(kind: str, dry_run: bool, total: int) -> dict[str, Any]:
    return {
        "kind": kind,
        "dry_run": bool(dry_run),
        "total": total,
        "created": 0,
        "failed": 0,
        "rows": [],
    }


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    result["ok"] = result["failed"] == 0
    verb = "校验通过" if result["dry_run"] else "导入成功"
    result["message"] = (
        f"共 {result['total']} 行：{verb} {result['total'] - result['failed']} 行，"
        f"失败 {result['failed']} 行"
    )
    logger.info(
        "批量导入完成",
        extra={
            "extra_fields": {
                "kind": result["kind"],
                "dry_run": result["dry_run"],
                "total": result["total"],
                "created": result["created"],
                "failed": result["failed"],
            }
        },
    )
    return result


def _template(headers: list[str], examples: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(headers)
    for example in examples:
        writer.writerow(example)
    return buffer.getvalue()
