"""预约领域：读者排队预约、到书通知、借出结单。

* 读者接口挂在 ``library.reader.routes``（``/api/reader/reservations*``）；
* 馆员接口在本模块（``/api/reservations*``，需 ``loan:write``）。
"""

from library.reservations.routes import bp

__all__ = ["bp"]
