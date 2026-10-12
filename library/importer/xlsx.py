"""零依赖的 xlsx 读取：把 Excel 表格转成 CSV 文本，复用现成的导入解析器。

## 为什么自己写

导入功能已经用 CSV 打通（含模板、逐行报错、dry_run 预览）。再引入 ``openpyxl``
只为把 xlsx 转成同样的行列数据，性价比太低——项目至今只有 Flask 一个依赖，
而 **xlsx 本质就是一个 zip**：``xl/sharedStrings.xml`` 存字符串表，
``xl/worksheets/sheet1.xml`` 存单元格，标准库的 ``zipfile`` + ``xml.etree`` 足够。

## 覆盖范围与边界

支持：共享字符串（含富文本 runs）、内联字符串、数字、布尔、公式缓存值、
稀疏单元格（按 ``r="B2"`` 回填空列）、多工作表取第一张。

不支持（也**有意不做**）：样式与日期格式（日期在 xlsx 里是「数字 + 样式」，
本系统的导入字段都是文本与整数，不做日期推断）、宏与图表、超过 1000 行的表
（导入本身有行数上限，交给上层统一报错）。

读出来的行列数据用 ``csv.writer`` 转成文本后交给现有的 CSV 解析链路，
这样「粘贴 CSV」「上传 CSV」「上传 Excel」三种入口**走完全相同的校验与报错逻辑**，
不会出现两套口径。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.etree import ElementTree as ET

_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")

__all__ = ["looks_like_xlsx", "parse_xlsx", "to_csv_text", "XlsxError"]


class XlsxError(ValueError):
    """xlsx 结构不可读（损坏、加密、非表格）。"""


def looks_like_xlsx(data: bytes) -> bool:
    """zip 魔数 —— xlsx / docx / pptx 都是 PK 开头。"""
    return len(data) >= 2 and data[:2] == b"PK"


def parse_xlsx(data: bytes) -> list[list[str]]:
    """解析第一张工作表为二维文本数组（不含表头判断，交给上层）。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxError("文件不是有效的 Excel（.xlsx）文档") from exc

    with archive:
        shared = _shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        if sheet_path is None:
            raise XlsxError("工作簿里没有找到工作表")
        try:
            root = ET.fromstring(archive.read(sheet_path))
        except (KeyError, ET.ParseError) as exc:
            raise XlsxError("工作表内容无法解析") from exc
        return _read_rows(root, shared)


def to_csv_text(rows: list[list[str]]) -> str:
    """把二维数组写成 CSV 文本，交给 ``importer.service`` 的 CSV 解析链路。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return buffer.getvalue()


# ---------------------------------------------------------------- 内部


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    values: list[str] = []
    for si in root.findall(f"{_MAIN_NS}si"):
        # 富文本：<si><r><t>前半</t></r><r><t>后半</t></r></si> 要拼起来
        values.append("".join(t.text or "" for t in si.iter(f"{_MAIN_NS}t")))
    return values


def _first_sheet_path(archive: zipfile.ZipFile) -> str | None:
    """按 workbook 关系文件取第一张表的实际路径（不硬编码 sheet1.xml）。"""
    try:
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except (KeyError, ET.ParseError):
        return "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in archive.namelist() else None

    first_rid = None
    for sheet in workbook.iter(f"{_MAIN_NS}sheet"):
        first_rid = sheet.get(f"{{http://schemas.openxmlformats.org/officeDocument/2006/relationships}}id")
        if first_rid:
            break
    if first_rid:
        for rel in rels.iter(f"{_REL_NS}Relationship"):
            if rel.get("Id") == first_rid:
                target = str(rel.get("Target") or "")
                path = target.lstrip("/")
                if not path.startswith("xl/"):
                    path = f"xl/{path}"
                if path in archive.namelist():
                    return path
    for name in archive.namelist():
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            return name
    return None


def _column_index(reference: str) -> int:
    match = _CELL_RE.match(reference.upper())
    if not match:
        return 0
    letters = match.group(1)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = str(cell.get("t") or "")
    if kind == "inlineStr":
        node = cell.find(f"{_MAIN_NS}is")
        if node is None:
            return ""
        return "".join(t.text or "" for t in node.iter(f"{_MAIN_NS}t"))
    value_node = cell.find(f"{_MAIN_NS}v")
    raw = "" if value_node is None or value_node.text is None else value_node.text
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "1" if raw == "1" else "0"
    return _clean_number(raw)


def _clean_number(raw: str) -> str:
    """整数型浮点去掉尾巴：``2020.0`` → ``2020``（Excel 里的年份常长这样）。"""
    text = raw.strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return text


def _read_rows(root: ET.Element, shared: list[str]) -> list[list[str]]:
    sheet_data = root.find(f"{_MAIN_NS}sheetData")
    if sheet_data is None:
        return []
    rows: list[list[str]] = []
    for row in sheet_data.findall(f"{_MAIN_NS}row"):
        cells: dict[int, str] = {}
        cursor = 0
        for cell in row.findall(f"{_MAIN_NS}c"):
            reference = str(cell.get("r") or "")
            index = _column_index(reference) if reference else cursor
            cells[index] = _cell_text(cell, shared)
            cursor = index + 1
        if not cells:
            rows.append([])
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, "") for i in range(width)])
    while rows and all(not str(cell).strip() for cell in rows[-1]):
        rows.pop()
    return rows
