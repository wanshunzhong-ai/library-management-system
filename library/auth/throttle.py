"""登录失败限流（进程内计数）。

每个失败会同时记进两个桶：

* ``账号|IP`` —— 针对「盯住一个账号猛试密码」，阈值 = ``login_max_attempts``；
* ``IP``      —— 针对「同一来源轮换账号名喷洒」（撞库），阈值更宽松，
  避免同一出口 NAT 后的正常用户被个别人的试错连累。

单进程部署够用；多实例部署需换成 Redis 等共享存储。

管理员与读者共用同一个命名空间的 `login_throttle`：登录入口已合并为首页的
一个输入框，若两类身份各用一套计数，攻击者交替调用不同入口即可绕过锁定。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# IP 维度专用 key 前缀，用于区分「账号|IP」与「纯 IP」两种桶
_IP_KEY_PREFIX = "#ip#"
# IP 桶阈值 = 账号阈值 × 倍数（并有下限），只挡批量喷洒，不挡正常手误
_IP_ATTEMPT_MULTIPLIER = 5
_IP_ATTEMPT_FLOOR = 20


class LoginThrottle:
    """按 key 统计连续失败次数，超限后临时锁定。

    阈值由 ``max_attempts(key)`` 按 key 决定，因此同一个实例可以承载
    「账号|IP」与「纯 IP」两种阈值的桶。
    """

    def __init__(
        self,
        namespace: str,
        *,
        max_attempts: Callable[[str], int],
        lock_seconds: Callable[[], int],
    ) -> None:
        self.namespace = namespace
        self._max_attempts = max_attempts
        self._lock_seconds = lock_seconds
        # key -> [失败次数, 锁定截止时间戳]
        self._records: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key_for(account: str, ip: str) -> str:
        """账号维度：``账号|IP``（账号统一小写去空格，避免换个大小写换桶）。"""
        return f"{(account or '').strip().lower()}|{ip or '-'}"

    @staticmethod
    def ip_key(ip: str) -> str:
        """来源维度：同一 IP 的全部账号失败共享计数。"""
        return f"{_IP_KEY_PREFIX}{ip or '-'}"

    def _limit_for(self, key: str) -> int:
        return max(1, int(self._max_attempts(key) or 1))

    def remaining_lock(self, key: str) -> int:
        """返回剩余锁定秒数，0 表示未被锁定。"""
        with self._lock:
            record = self._records.get(key)
            if not record:
                return 0
            locked_until = record[1]
            if locked_until and locked_until > time.time():
                return int(locked_until - time.time()) + 1
            if locked_until and locked_until <= time.time():
                self._records.pop(key, None)
        return 0

    def record_failure(self, key: str) -> bool:
        """记一次失败；返回本次是否触发锁定。"""
        with self._lock:
            record = self._records.setdefault(key, [0.0, 0.0])
            record[0] += 1
            triggered = record[0] >= self._limit_for(key)
            if triggered:
                record[1] = time.time() + self._lock_seconds()
                record[0] = 0
        if triggered:
            logger.warning(
                "登录失败次数过多，已临时锁定",
                extra={"extra_fields": {"scope": self.namespace, "bucket": key.split("|")[0]}},
            )
        return triggered

    def clear(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._records.clear()


def _login_max_attempts(key: str) -> int:
    from config import get_config

    cfg = get_config()
    if key.startswith(_IP_KEY_PREFIX):
        return max(cfg.login_max_attempts * _IP_ATTEMPT_MULTIPLIER, _IP_ATTEMPT_FLOOR)
    return cfg.login_max_attempts


def _login_lock_seconds() -> int:
    from config import get_config

    return get_config().login_lock_minutes * 60


# 全站唯一的登录限流器：管理员与读者共用。
login_throttle = LoginThrottle(
    "login",
    max_attempts=_login_max_attempts,
    lock_seconds=_login_lock_seconds,
)


# ---------------------------------------------------------------- 组合操作
# 一次登录尝试同时属于「账号|IP」与「IP」两个桶，三个函数把这套规则收在一处，
# 管理员与读者两条登录路径共用，避免口径不一致留下绕过口子。


def lock_keys(identifier: str, ip: str) -> tuple[str, str]:
    return (LoginThrottle.key_for(identifier, ip), LoginThrottle.ip_key(ip))


def remaining_login_lock(identifier: str, ip: str) -> int:
    """两个桶里剩余锁定时间的较大值，0 表示可以继续尝试。"""
    return max(login_throttle.remaining_lock(key) for key in lock_keys(identifier, ip))


def record_login_failure(identifier: str, ip: str) -> None:
    for key in lock_keys(identifier, ip):
        login_throttle.record_failure(key)


def clear_login_locks(identifier: str, ip: str) -> None:
    for key in lock_keys(identifier, ip):
        login_throttle.clear(key)
