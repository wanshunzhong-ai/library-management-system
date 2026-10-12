"""应用工厂：装配配置、日志、数据库、蓝图、鉴权守卫与全局错误处理。"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from flask import Flask, jsonify, render_template, request

from config import Config, get_config, set_config
from library import db
from library.auth import service as auth_service
from library.errors import AppError
from library.logging_conf import init_logging, register_request_logging
from library.members import service as member_service
from library.reader import service as reader_service

logger = logging.getLogger(__name__)


def create_app(config: Config | None = None, *, with_scheduler: bool = False) -> Flask:
    if config is not None:
        set_config(config)
    cfg = config or get_config()
    init_logging(cfg.log_level)

    # 上传接口要收图片 / PDF，2MB 的默认上限太紧；按配置放宽，并至少保留 2MB。
    max_content = max(2, int(cfg.upload_max_mb) + 1) * 1024 * 1024
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        JSON_SORT_KEYS=False,
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=max_content,
        # ---- 会话 Cookie 安全 ----
        SESSION_COOKIE_NAME="library_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=cfg.session_hours),
    )
    app.json.ensure_ascii = False

    register_request_logging(app)
    _register_blueprints(app)
    _register_guard(app)
    _register_pages(app)
    _register_ops(app)
    _register_error_handlers(app)
    _register_security_headers(app)
    _register_cors(app)

    app.teardown_appcontext(db.close_conn)

    with app.app_context():
        db.init_db()
        auth_service.ensure_default_admin()
        member_service.ensure_reader_accounts()
        _warn_if_default_admin_password()
        # 老库升级后补上演示书目简介（仅补空值，失败不阻塞启动）
        try:
            from library import seed as seed_module

            seed_module.ensure_content()
        except Exception:  # noqa: BLE001 - 回填属增强逻辑，不应拖垮启动
            logger.warning("演示简介回填失败，已跳过", exc_info=True)

    if with_scheduler:
        # 后台定时扫描（预约过期释放 + 到期/逾期提醒）。
        # 只在真正的服务进程里启动：测试与嵌入式用法默认不起线程。
        from library import scheduler  # noqa: PLC0415

        scheduler.start(app)

    return app


def _warn_if_default_admin_password() -> None:
    """启动自检：还有管理员在用配置里的初始口令时，喊一嗓子。

    后台控制台也会显示醒目横幅（``/api/auth/me`` 的 ``security`` 字段）。
    不直接拒绝启动，是为了保留「开箱即用」——但生产部署必须改掉。
    """
    usernames = auth_service.admins_using_default_password()
    if not usernames:
        return
    logger.warning(
        "检测到仍在使用初始管理员口令的账号：%s —— 任何知道默认口令的人都能进入后台，请立即修改密码",
        "、".join(usernames),
    )


def _register_blueprints(app: Flask) -> None:
    from library.accounts import bp as accounts_bp
    from library.acquisitions import bp as acquisitions_bp
    from library.announcements import bp as announcements_bp
    from library.applications import bp as applications_bp
    from library.approvals import bp as approvals_bp
    from library.auth import bp as auth_bp
    from library.books import bp as books_bp
    from library.compensations import bp as compensations_bp
    from library.dashboard import bp as dashboard_bp
    from library.engagement import bp as engagement_bp
    from library.fines import bp as fines_bp
    from library.importer import bp as importer_bp
    from library.loans import bp as loans_bp
    from library.memberfiles import bp as memberfiles_bp
    from library.members import bp as members_bp
    from library.notifications import bp as notifications_bp
    from library.public import bp as public_bp
    from library.reader import bp as reader_bp
    from library.reservations import bp as reservations_bp
    from library.stocktaking import bp as stocktaking_bp
    from library.sysadmin import bp as sysadmin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(reader_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(books_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(loans_bp)
    app.register_blueprint(reservations_bp)
    app.register_blueprint(fines_bp)
    app.register_blueprint(announcements_bp)
    app.register_blueprint(applications_bp)
    app.register_blueprint(engagement_bp)
    app.register_blueprint(compensations_bp)
    app.register_blueprint(stocktaking_bp)
    app.register_blueprint(acquisitions_bp)
    app.register_blueprint(memberfiles_bp)
    app.register_blueprint(importer_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(sysadmin_bp)


def _register_guard(app: Flask) -> None:
    """统一守卫，按路径前缀区分三类接口。

    1. `/api/reader/*`  —— 读者门户：只认读者会话，写操作校验读者 CSRF；
    2. `/api/public/*` 与登录相关 —— 免登录；
    3. 其余 `/api/*`    —— 后台管理：只认管理员会话，写操作校验管理员 CSRF。

    管理员与读者使用互相独立的两套会话键与 CSRF token，可同时登录互不顶掉。
    """
    public_prefixes = auth_service.PUBLIC_API_PREFIXES
    public_endpoints = auth_service.PUBLIC_API_ENDPOINTS

    @app.before_request
    def _guard():
        path = request.path
        if not path.startswith("/api/"):
            return None

        endpoint = request.endpoint or ""

        if path.startswith("/api/reader/"):
            if endpoint in reader_service.PUBLIC_ENDPOINTS:
                return None
            if reader_service.current_reader() is None:
                return (
                    jsonify({"error": {"code": "unauthorized", "message": "请先登录读者账号"}}),
                    401,
                )
            # 仍在使用初始密码时先逼它改掉，否则「证号后 6 位」这种可猜解的口令
            # 会长期有效。改密接口本身与页面初始化所需的几个接口放行。
            blocked = reader_service.password_change_gate()
            if blocked is not None:
                return blocked
            return reader_service.csrf_protect()

        if path.startswith(public_prefixes) or endpoint in public_endpoints:
            return None

        if auth_service.current_admin() is None:
            return (
                jsonify({"error": {"code": "unauthorized", "message": "请先登录后再操作"}}),
                401,
            )
        return auth_service.csrf_protect()


def _register_pages(app: Flask) -> None:
    @app.get("/admin")
    @auth_service.admin_required
    def admin_console():
        cfg = get_config()
        return render_template(
            "index.html",
            library_name=cfg.library_name,
            admin_name=auth_service.current_admin(),
        )

    @app.get("/admin/")
    @auth_service.admin_required
    def admin_console_slash():
        return admin_console()


def _register_ops(app: Flask) -> None:
    @app.get("/health")
    def health():
        """存活探针：不触碰数据库。"""
        return jsonify({"status": "ok"})

    @app.get("/ready")
    def ready():
        """就绪探针：确认数据库可读。"""
        try:
            db.query_scalar("SELECT 1")
        except sqlite3.Error as exc:
            logger.error("ready check failed: %s", exc)
            return jsonify({"status": "degraded", "reason": "database_unavailable"}), 503
        return jsonify({"status": "ready"})


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def _handle_app_error(exc: AppError):
        if exc.status_code >= 500:
            logger.exception("app error: %s", exc.message)
        else:
            logger.info("handled error", extra={"extra_fields": {"code": exc.code, "message": exc.message}})
        payload = exc.to_dict()
        payload["request_id"] = _request_id()
        return jsonify(payload), exc.status_code

    @app.errorhandler(sqlite3.IntegrityError)
    def _handle_integrity(exc: sqlite3.IntegrityError):
        """把 SQLite 约束违规翻译成能指明排查方向的中文提示。

        原先一律返回「唯一字段重复」的 409：NOT NULL / CHECK 违规时提示会把人
        引到错误的方向（去查重，而真正的问题是空值或库存越界）。
        """
        name = str(getattr(exc, "sqlite_errorname", "") or "").upper()
        logger.warning("integrity error: %s", exc)
        if "UNIQUE" in name or "PRIMARYKEY" in name:
            code, status = "conflict", 409
            message = "数据约束冲突：唯一字段（ISBN / 条码 / 借书证号 / 手机号）已存在"
        elif "NOTNULL" in name:
            code, status = "validation_error", 400
            message = "存在必填字段为空，请补全后重试"
        elif "CHECK" in name:
            code, status = "validation_error", 400
            message = "数据不满足一致性约束（例如可借数不能大于总册数、可借数不能为负）"
        elif "FOREIGNKEY" in name:
            code, status = "conflict", 409
            message = "存在关联数据引用，无法完成该操作"
        else:
            code, status = "conflict", 409
            message = "数据约束冲突，请检查输入内容后重试"
        return jsonify({"error": {"code": code, "message": message}, "request_id": _request_id()}), status

    @app.errorhandler(404)
    def _handle_404(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": "not_found", "message": f"接口不存在：{request.path}"}}), 404
        cfg = get_config()
        return render_template(
            "public.html", library_name=cfg.library_name, library_slogan=cfg.library_slogan
        ), 404

    @app.errorhandler(405)
    def _handle_405(_exc):
        return jsonify({"error": {"code": "method_not_allowed", "message": f"{request.method} 不被 {request.path} 支持"}}), 405

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        logger.exception("unhandled exception")
        return (
            jsonify({"error": {"code": "internal_error", "message": "服务器内部错误，请稍后重试"}, "request_id": _request_id()}),
            500,
        )


def _request_id() -> str | None:
    try:
        from flask import g

        return getattr(g, "request_id", None)
    except RuntimeError:
        return None


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def _headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        # 后台页面与接口不允许被中间层缓存
        path = request.path
        if path.startswith("/api/") or path.startswith("/admin"):
            response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
            response.headers.setdefault("Pragma", "no-cache")
        return response


def _register_cors(app: Flask) -> None:
    """显式白名单 CORS，默认不开（前后端同源部署）。"""
    import os

    raw = os.getenv("LIBRARY_CORS_ORIGINS", "").strip()
    if not raw:
        return
    allowed = {origin.strip() for origin in raw.split(",") if origin.strip()}

    @app.after_request
    def _cors(response):
        origin = request.headers.get("Origin")
        if origin and origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Request-ID"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        return response
