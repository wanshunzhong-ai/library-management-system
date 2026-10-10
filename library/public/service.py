"""读者门户业务逻辑。"""

from __future__ import annotations

from typing import Any, Mapping

from config import get_config
from library.public import repository as repo
from library.validation import pagination


def _decorate(book: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(book)
    available = int(item.get("available_copies") or 0)
    total = int(item.get("total_copies") or 0)
    item["available"] = available > 0
    if available <= 0:
        item["status_label"] = "全部借出"
    elif available == 1:
        item["status_label"] = "仅剩 1 册"
    else:
        item["status_label"] = f"可借 {available} 册"
    item["availability_ratio"] = round(available / total, 3) if total else 0.0
    return item


def overview() -> dict[str, Any]:
    cfg = get_config()
    return {
        "library": {
            "name": cfg.library_name,
            "slogan": cfg.library_slogan,
        },
        "stats": {
            **repo.stats(),
            "reader_count": repo.reader_count(),
            "active_reader_count": repo.active_reader_count(),
            "loan_total": repo.loan_total(),
        },
        "rules": {
            "loan_days": cfg.loan_days,
            "max_renew": cfg.max_renew,
            "fine_per_day": cfg.fine_per_day,
            "max_borrow": cfg.default_max_borrow,
        },
        "categories": repo.categories(),
        "recommended": [_decorate(b) for b in repo.recommended(6)],
        "new_arrivals": [_decorate(b) for b in repo.new_arrivals(6)],
    }


def search(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query, default_size=8)
    filters = {
        "keyword": query.get("keyword", ""),
        "category": query.get("category", ""),
        "available_only": str(query.get("available_only", "")).lower() in {"1", "true", "yes"},
    }
    total = repo.count(filters)
    items = [_decorate(b) for b in repo.search(filters, limit=size, offset=(page - 1) * size)]
    keyword = str(filters["keyword"] or "").strip()
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "keyword": keyword,
        "available_only": filters["available_only"],
    }
