"""概览统计的聚合查询。"""

from __future__ import annotations

from typing import Any

from library import db


def book_stats() -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT COUNT(*)                          AS total_titles,
               COALESCE(SUM(total_copies), 0)    AS total_copies,
               COALESCE(SUM(available_copies), 0) AS available_copies,
               COALESCE(SUM(total_copies - available_copies), 0) AS borrowed_copies,
               COALESCE(SUM(CASE WHEN total_copies > 0 THEN 1 ELSE 0 END), 0) AS on_shelf_titles,
               COALESCE(SUM(CASE WHEN total_copies = 0 THEN 1 ELSE 0 END), 0) AS off_shelf_titles
          FROM books
        """
    )
    return row or {}


def member_stats() -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT COUNT(*) AS total_members,
               COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active_members,
               COALESCE(SUM(CASE WHEN status = 'frozen' THEN 1 ELSE 0 END), 0) AS frozen_members
          FROM members
        """
    )
    return row or {}


def loan_stats() -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(CASE WHEN returned_at IS NULL THEN 1 ELSE 0 END), 0) AS open_loans,
               COALESCE(SUM(CASE WHEN returned_at IS NULL
                                  AND date(due_at) < date('now', 'localtime') THEN 1 ELSE 0 END), 0) AS overdue_loans,
               COALESCE(SUM(CASE WHEN returned_at IS NULL
                                  AND date(due_at) >= date('now', 'localtime')
                                  AND date(due_at) <= date('now', 'localtime', '+7 days') THEN 1 ELSE 0 END), 0) AS due_soon_loans,
               COALESCE(SUM(CASE WHEN date(borrowed_at) = date('now', 'localtime') THEN 1 ELSE 0 END), 0) AS borrowed_today,
               COALESCE(SUM(CASE WHEN date(returned_at) = date('now', 'localtime') THEN 1 ELSE 0 END), 0) AS returned_today,
               COUNT(*) AS total_loans
          FROM loans
        """
    )
    return row or {}


