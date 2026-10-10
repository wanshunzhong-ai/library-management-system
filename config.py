"""集中式配置。

所有配置项均来自环境变量，并在进程启动时一次性校验（fail fast），
避免运行到一半才发现配置非法。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _env_str(key: str, default: str) -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _env_int(key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前值 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} 不能小于 {minimum}，当前值 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} 不能大于 {maximum}，当前值 {value}")
    return value


def _env_float(key: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是数字，当前值 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} 不能小于 {minimum}，当前值 {value}")
    return value


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ConfigError(f"{key} 必须是布尔值（true/false），当前值 {raw!r}")


@dataclass(frozen=True)
class Config:
    """运行时配置快照。"""

    db_path: str
    host: str
    port: int
    debug: bool
    loan_days: int
    max_renew: int
    fine_per_day: float
    default_max_borrow: int
    log_level: str
    secret_key: str

    # ---- 站点与鉴权（带默认值，便于测试直接构造） ----
    library_name: str = "星海图书馆"
    library_slogan: str = "让每一本书都找到它的读者"
    admin_user: str = "admin"
    admin_password: str = "admin123"
    session_hours: int = 8
    login_max_attempts: int = 5
    login_lock_minutes: int = 5
    cookie_secure: bool = False


def load_config() -> Config:
    db_path = _env_str("LIBRARY_DB_PATH", str(BASE_DIR / "data" / "library.db"))
    return Config(
        db_path=db_path,
        host=_env_str("LIBRARY_HOST", "127.0.0.1"),
        port=_env_int("LIBRARY_PORT", 8000, minimum=1, maximum=65535),
        debug=_env_bool("LIBRARY_DEBUG", False),
        # 业务参数
        loan_days=_env_int("LIBRARY_LOAN_DAYS", 30, minimum=1, maximum=365),
        max_renew=_env_int("LIBRARY_MAX_RENEW", 2, minimum=0, maximum=10),
        fine_per_day=_env_float("LIBRARY_FINE_PER_DAY", 0.50, minimum=0),
        default_max_borrow=_env_int("LIBRARY_DEFAULT_MAX_BORROW", 5, minimum=1, maximum=50),
        # 可观测性 / 安全
        log_level=_env_str("LIBRARY_LOG_LEVEL", "INFO").upper(),
        secret_key=_env_str("LIBRARY_SECRET_KEY", "dev-only-insecure-key"),
        # 站点信息
        library_name=_env_str("LIBRARY_NAME", "星海图书馆"),
        library_slogan=_env_str("LIBRARY_SLOGAN", "让每一本书都找到它的读者"),
        # 后台管理员（首次启动时写入数据库，之后以数据库为准）
        admin_user=_env_str("LIBRARY_ADMIN_USER", "admin"),
        admin_password=_env_str("LIBRARY_ADMIN_PASSWORD", "admin123"),
        session_hours=_env_int("LIBRARY_SESSION_HOURS", 8, minimum=1, maximum=720),
        login_max_attempts=_env_int("LIBRARY_LOGIN_MAX_ATTEMPTS", 5, minimum=1, maximum=100),
        login_lock_minutes=_env_int("LIBRARY_LOGIN_LOCK_MINUTES", 5, minimum=1, maximum=1440),
        cookie_secure=_env_bool("LIBRARY_COOKIE_SECURE", False),
    )


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    """显式注入配置（测试 / 嵌入式使用）。"""
    global _config
    _config = cfg
