"""类型化错误层级。

服务层只抛这些错误，由全局错误处理器统一转成结构化 JSON，
避免 `raise Exception("oops")` 这种无法区分语义的写法。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有业务错误的基类。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            payload["error"]["details"] = self.details
        return payload


class ValidationError(AppError):
    """请求参数不合法。"""

    status_code = 400
    code = "validation_error"


class UnauthorizedError(AppError):
    """未登录或凭证错误。"""

    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    """已登录但无权限。"""

    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    """资源不存在。"""

    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """资源冲突（唯一键重复、被引用无法删除等）。"""

    status_code = 409
    code = "conflict"


class BusinessRuleError(AppError):
    """违反业务规则（超额、逾期、库存不足等）。"""

    status_code = 422
    code = "business_rule_violation"
