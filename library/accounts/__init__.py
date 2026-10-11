"""后台账号与审计领域（系统管理）。

* ``/api/accounts`` —— 后台账号的增删改、启停用、重置密码（需 ``account:manage``）
* ``/api/logs``     —— 操作日志查询（需 ``log:read``）

两者都只对系统管理员开放。流通馆员即使直接构造请求也拿不到这些接口，
见 ``library/auth/permissions.py`` 的能力表。
"""

from library.accounts.routes import bp

__all__ = ["bp"]
