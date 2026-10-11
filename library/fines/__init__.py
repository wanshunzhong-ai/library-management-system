"""滞纳金领域：读者自助缴纳、馆员代收 / 减免。

* 读者接口挂在 ``library.reader.routes``（``/api/reader/fines*``）；
* 馆员接口在本模块（``/api/fines*``，需 ``loan:write``）。
"""

from library.fines.routes import bp

__all__ = ["bp"]
