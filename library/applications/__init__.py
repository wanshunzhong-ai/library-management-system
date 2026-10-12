"""在线办证申请领域。

* 访客接口在 ``library.public.routes``（``POST /api/public/member-applications*``）；
* 馆员审核接口在本模块（``/api/applications*``，需 ``member:write``——审核通过的
  动作就是"建读者档案"，复用同一能力比新造一个更贴合语义）。

隐私取舍：**只存证件号后 4 位**，完整证件号由馆员现场核验，不在系统里留第二份。
"""

from library.applications.routes import bp

__all__ = ["bp"]
