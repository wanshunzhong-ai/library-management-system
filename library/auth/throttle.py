"""登录失败限流（进程内计数）。

计数 key 为 "账号|IP"，连续失败达到阈值后锁定一段时间。
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


class LoginThrottle:
    """按 key 统计连续失败次数，超限后临时锁定。"""

    def __init__(
        self,
        namespace: str,
        *,
        max_attempts: Callable[[], int],
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
        return f"{(account or '').strip().lower()}|{ip or '-'}"

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
            triggered = record[0] >= self._max_attempts()
            if triggered:
                record[1] = time.time() + self._lock_seconds()
                record[0] = 0
        if triggered:
            logger.warning(
                "登录失败次数过多，已临时锁定",
                extra={"extra_fields": {"scope": self.namespace, "account": key.split("|")[0]}},
            )
        return triggered

    def clear(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._records.clear()


def _login_max_attempts() -> int:
    from config import get_config

    return get_config().login_max_attempts


def _login_lock_seconds() -> int:
    from config import get_config

    return get_config().login_lock_minutes * 60


# 全站唯一的登录限流器：管理员与读者共用，按 "账号|IP" 计数。
login_throttle = LoginThrottle(
    "login",
    max_attempts=_login_max_attempts,
    lock_seconds=_login_lock_seconds,
)
