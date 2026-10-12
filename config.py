"""集中式配置。

所有配置项均来自环境变量，并在进程启动时一次性校验（fail fast），
避免运行到一半才发现配置非法。
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}

# 会话密钥里出现这些值说明是「照抄示例 / 随手写的」，生产模式下一律拒绝启动。
# 默认密钥可预测意味着任何人都能伪造管理员会话，比默认口令更危险。
_PLACEHOLDER_SECRETS = {
    "dev-only-insecure-key",
    "please-change-me",
    "change-me",
    "changeme",
    "secret",
    "secret-key",
    "test-secret",
}
_SECRET_MIN_LENGTH = 16
_SECRET_FILE_NAME = ".secret_key"


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


def _env_list(key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """逗号分隔的列表型环境变量（如代理白名单）。"""
    raw = os.getenv(key)
    if raw is None:
        return default
    items = tuple(item.strip() for item in raw.split(",") if item.strip())
    return items or default


# ---------------------------------------------------------------- 会话密钥


def secret_file_path(db_path: str) -> Path:
    """密钥的落盘位置：与数据库同目录，便于随数据一起迁移/备份。"""
    return Path(db_path).expanduser().parent / _SECRET_FILE_NAME


def _load_or_create_secret_key(db_path: str) -> str:
    """未显式配置密钥时：生成一个随机密钥并持久化。

    这样既保留「开箱即用」，又让每套部署的会话密钥互不相同且不可预测。
    文件权限收紧到仅所有者可读写（Windows 上 chmod 语义有限，但不影响功能）。
    """
    path = secret_file_path(db_path)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise ConfigError(f"无法读取会话密钥文件 {path}：{exc}") from exc
    if existing:
        return existing

    key = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ConfigError(f"无法写入会话密钥文件 {path}：{exc}") from exc
    logger.warning(
        "未配置 LIBRARY_SECRET_KEY，已自动生成会话密钥并保存到 %s（请勿提交到版本库）",
        path,
    )
    return key


def _resolve_secret_key(db_path: str, *, debug: bool) -> str:
    """决定会话密钥：显式配置优先，否则自动生成并落盘。

    生产模式（LIBRARY_DEBUG=false）下拒绝示例值 / 过短的值 —— 这类密钥
    可被用来伪造会话直接冒充管理员，绕过口令校验。
    """
    raw = (os.getenv("LIBRARY_SECRET_KEY") or "").strip()
    if not raw:
        return _load_or_create_secret_key(db_path)

    placeholder = raw.lower() in _PLACEHOLDER_SECRETS
    too_short = len(raw) < _SECRET_MIN_LENGTH
    if debug:
        if placeholder or too_short:
            logger.warning("LIBRARY_SECRET_KEY 是示例值或过短，仅开发模式（debug）允许使用")
        return raw
    if placeholder:
        raise ConfigError(
            "LIBRARY_SECRET_KEY 仍是示例值，生产模式下禁止使用（该密钥可被用来伪造管理员会话）；"
            "请改成随机字符串，或清空该变量以自动生成并保存到 .secret_key"
        )
    if too_short:
        raise ConfigError(f"LIBRARY_SECRET_KEY 至少需要 {_SECRET_MIN_LENGTH} 个字符，当前 {len(raw)} 个")
    return raw


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
    # 罚金封顶（每笔借阅）：0 表示不限制
    # fine_max_days 限制最长计息天数，fine_cap 限制单笔罚金金额，两者取先到者
    fine_max_days: int = 365
    fine_cap: float = 50.0
    # 受信反向代理（IP 或 CIDR）：只有这些来源的 X-Forwarded-For 才被采信
    trusted_proxies: tuple[str, ...] = ()
    # 在线办证申请：同一来源在窗口期内的提交 / 查询次数上限（防刷）
    apply_max_attempts: int = 10
    apply_lock_minutes: int = 10
    # 预约到书后的取书保留天数：超期未取自动让位给下一位排队者
    reservation_hold_days: int = 3
    # 到期前提醒的提前天数：距应还日期不足这个天数时给读者发一条站内通知
    notify_due_soon_days: int = 3
    # 预约过期释放的后台扫描间隔（秒）。0 表示不启动后台线程，只保留
    # 「有人打开页面时惰性扫描」的行为——单元测试即用 0，避免每个用例都起线程。
    sweep_interval_seconds: int = 600
    # 读者附件（照片 / 证件）上传目录与单文件大小上限
    upload_dir: str = ""
    upload_max_mb: int = 4
    # 邮件出站渠道（SMTP）。host 留空 = 未配置邮件渠道：出站队列照常入队，
    # 但停在 pending 不投递；配置后由后台扫描线程 / 手动触发补发。
    smtp_host: str = ""
    smtp_port: int = 0            # 0 = 按 secure 自动选择（SSL 465 / TLS 587）
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""           # 发件人地址，留空取 smtp_user
    smtp_secure: str = "auto"     # auto / ssl / tls / none
    # 罚金减免审批阈值（元）。0 = 不启用审批（减免直接生效，维持原有流程）；
    # >0 后单笔减免金额 ≥ 阈值自动转「待审批」，需系统管理员批准才生效。
    approval_waiver_threshold: float = 0.0
    # 备份目录：留空取数据库同级的 backups/
    backup_dir: str = ""


def load_config() -> Config:
    db_path = _env_str("LIBRARY_DB_PATH", str(BASE_DIR / "data" / "library.db"))
    debug = _env_bool("LIBRARY_DEBUG", False)
    smtp_secure = _env_str("LIBRARY_SMTP_SECURE", "auto").strip().lower()
    if smtp_secure not in ("auto", "ssl", "tls", "none"):
        raise ConfigError(f"LIBRARY_SMTP_SECURE 只支持 auto/ssl/tls/none，当前值 {smtp_secure!r}")
    return Config(
        db_path=db_path,
        host=_env_str("LIBRARY_HOST", "127.0.0.1"),
        port=_env_int("LIBRARY_PORT", 8000, minimum=1, maximum=65535),
        debug=debug,
        # 业务参数
        loan_days=_env_int("LIBRARY_LOAN_DAYS", 30, minimum=1, maximum=365),
        max_renew=_env_int("LIBRARY_MAX_RENEW", 2, minimum=0, maximum=10),
        fine_per_day=_env_float("LIBRARY_FINE_PER_DAY", 0.50, minimum=0),
        # 罚金封顶：max_days = 0 / cap = 0 均表示该维度不限制
        fine_max_days=_env_int("LIBRARY_FINE_MAX_DAYS", 365, minimum=0, maximum=3650),
        fine_cap=_env_float("LIBRARY_FINE_CAP", 50.0, minimum=0),
        default_max_borrow=_env_int("LIBRARY_DEFAULT_MAX_BORROW", 5, minimum=1, maximum=50),
        # 可观测性 / 安全
        log_level=_env_str("LIBRARY_LOG_LEVEL", "INFO").upper(),
        secret_key=_resolve_secret_key(db_path, debug=debug),
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
        trusted_proxies=_env_list("LIBRARY_TRUSTED_PROXIES"),
        apply_max_attempts=_env_int("LIBRARY_APPLY_MAX_ATTEMPTS", 10, minimum=1, maximum=1000),
        apply_lock_minutes=_env_int("LIBRARY_APPLY_LOCK_MINUTES", 10, minimum=1, maximum=1440),
        reservation_hold_days=_env_int("LIBRARY_RESERVATION_HOLD_DAYS", 3, minimum=1, maximum=90),
        notify_due_soon_days=_env_int("LIBRARY_NOTIFY_DUE_SOON_DAYS", 3, minimum=0, maximum=90),
        sweep_interval_seconds=_env_int(
            "LIBRARY_SWEEP_INTERVAL_SECONDS", 600, minimum=0, maximum=86400
        ),
        # 默认与数据库同目录，便于随数据一起备份 / 迁移
        upload_dir=_env_str(
            "LIBRARY_UPLOAD_DIR", str(Path(db_path).expanduser().parent / "uploads")
        ),
        upload_max_mb=_env_int("LIBRARY_UPLOAD_MAX_MB", 4, minimum=1, maximum=50),
        smtp_host=_env_str("LIBRARY_SMTP_HOST", ""),
        smtp_port=_env_int("LIBRARY_SMTP_PORT", 0, minimum=0, maximum=65535),
        smtp_user=_env_str("LIBRARY_SMTP_USER", ""),
        smtp_password=_env_str("LIBRARY_SMTP_PASSWORD", ""),
        smtp_from=_env_str("LIBRARY_SMTP_FROM", ""),
        smtp_secure=_env_str("LIBRARY_SMTP_SECURE", "auto").strip().lower(),
        approval_waiver_threshold=_env_float("LIBRARY_APPROVAL_WAIVER_THRESHOLD", 0.0, minimum=0),
        backup_dir=_env_str("LIBRARY_BACKUP_DIR", str(Path(db_path).expanduser().parent / "backups")),
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
