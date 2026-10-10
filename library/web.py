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


def create_app(config: Config | None = None) -> Flask:
    if config is not None:
        set_config(config)
    cfg = config or get_config()
    init_logging(cfg.log_level)

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        JSON_SORT_KEYS=False,
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
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
        # 老库升级后补上演示书目简介（仅补空值，失败不阻塞启动）
        try:
            from library import seed as seed_module

            seed_module.ensure_content()
        except Exception:  # noqa: BLE001 - 回填属增强逻辑，不应拖垮启动
            logger.warning("演示简介回填失败，已跳过", exc_info=True)

    return app


def _register_blueprints(app: Flask) -> None:
    from library.auth import bp as auth_bp
    from library.books import bp as books_bp
    from library.dashboard import bp as dashboard_bp
    from library.loans import bp as loans_bp
    from library.members import bp as members_bp
    from library.public import bp as public_bp
    from library.reader import bp as reader_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(reader_bp)
    app.register_blueprint(books_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(loans_bp)
    app.register_blueprint(dashboard_bp)


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
        logger.warning("integrity error: %s", exc)
        return (
            jsonify({"error": {"code": "conflict", "message": "数据约束冲突，请检查唯一字段是否重复"}, "request_id": _request_id()}),
            409,
        )

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
