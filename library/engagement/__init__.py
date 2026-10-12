"""读者互动领域：我的书架（收藏）、图书评价、荐购登记、资料变更申请。

* 读者侧接口在 ``library.reader.routes``（前缀 ``/api/reader/``，走读者会话）；
* 馆员侧审核接口在本模块（``/api/engagement*``，需 ``member:write``）；
* 门户展示的评分聚合与评价列表在 ``library.public.routes``，复用本模块 service，
  保证「什么算一条可见评价」只有一处口径。
"""

from library.engagement.routes import bp

__all__ = ["bp"]
