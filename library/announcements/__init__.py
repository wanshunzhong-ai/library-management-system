"""公告与开馆时间领域。

* 馆员接口在本模块（``/api/announcements*``，需 ``announcement:write``）；
* 门户侧只读接口在 ``library.public.routes``（``GET /api/public/announcements``），
  复用本模块 `service.public_list()`，保证「什么算当前生效」只有一处口径。
"""

from library.announcements.routes import bp

__all__ = ["bp"]
