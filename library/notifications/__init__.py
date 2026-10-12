"""站内通知领域。

* 读者侧接口在本模块（``/api/reader/notifications*``，走读者会话）；
* 馆员发通知的入口在 ``library.members.routes``（``POST /api/members/:id/notify``）；
* 通知的**生成**散在触发点：到书 / 预约过期由 `library.reservations.service` 直接调用，
  即将到期 / 逾期由 `service.sweep_member` 惰性扫描补写，审核结果由各审核流程写入。
"""

from library.notifications.routes import bp

__all__ = ["bp"]
