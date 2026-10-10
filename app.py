"""图书管理系统入口。

用法：
    python app.py                 # 启动 Web 服务
    python app.py init            # 仅初始化数据库
    python app.py seed [--force]  # 写入演示数据
    python app.py reset           # 重置数据库并重建演示数据
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from config import load_config
from library import db, seed as seed_module
from library.logging_conf import init_logging
from library.web import create_app

logger = logging.getLogger(__name__)


def _bootstrap():
    cfg = load_config()  # 配置非法立即抛错，fail fast
    init_logging(cfg.log_level)
    return cfg


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    app = create_app(cfg)
    if args.seed or args.reset:
        with app.app_context():
            seed_module.seed(force=args.reset)

    _install_signal_handlers()
    logger.info(
        "服务启动",
        extra={"extra_fields": {"url": f"http://{cfg.host}:{cfg.port}", "db": cfg.db_path, "debug": cfg.debug}},
    )
    print(f"\n  图书管理系统已启动 →  http://{cfg.host}:{cfg.port}\n")
    app.run(host=cfg.host, port=cfg.port, debug=cfg.debug, use_reloader=False, threaded=True)
    return 0


def cmd_init(_args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    app = create_app(cfg)
    with app.app_context():
        db.init_db()
    print(f"数据库已初始化：{cfg.db_path}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    app = create_app(cfg)
    with app.app_context():
        written = seed_module.seed(force=args.force)
    print("演示数据已写入。" if written else "数据库非空，未写入（加 --force 可强制覆盖）。")
    return 0


def cmd_reset(_args: argparse.Namespace) -> int:
    cfg = _bootstrap()
    app = create_app(cfg)
    with app.app_context():
        seed_module.seed(force=True)
    print(f"数据库已重置并写入演示数据：{cfg.db_path}")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    """数据体检：库存越界、重复在借、重复手机号、登录标识互相遮蔽等。"""
    import json

    cfg = _bootstrap()
    app = create_app(cfg)
    with app.app_context():
        report = db.check_consistency()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["ok"]:
        print("\n数据体检通过。")
        return 0
    print(f"\n发现 {len(report['issues'])} 处问题，可尝试 `python app.py repair`。")
    return 1


def cmd_repair(_args: argparse.Namespace) -> int:
    """修复有明确答案的问题（目前只处理「同一读者同一本书的重复在借」）。"""
    cfg = _bootstrap()
    app = create_app(cfg)
    with app.app_context():
        result = db.repair_consistency()
    if not result["closed_loans"]:
        print("没有需要修复的重复在借记录。")
        return 0 if result["after"]["ok"] else 1
    print(f"已把 {result['closed_loans']} 条重复在借记录标记为已归还，库存同步修正。")
    if not result["after"]["ok"]:
        print(f"仍有 {len(result['after']['issues'])} 处问题需要人工处理。")
    return 0 if result["after"]["ok"] else 1


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        logger.info("收到退出信号，正在关闭服务", extra={"extra_fields": {"signal": signum}})
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, AttributeError):  # 非主线程 / 平台不支持
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="library", description="图书管理系统")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="启动 Web 服务（默认）")
    run.add_argument("--seed", action="store_true", help="启动前写入演示数据（库非空则跳过）")
    run.add_argument("--reset", action="store_true", help="启动前重置数据库并写入演示数据")
    run.set_defaults(func=cmd_run)

    sub.add_parser("init", help="初始化数据库").set_defaults(func=cmd_init)

    seed_cmd = sub.add_parser("seed", help="写入演示数据")
    seed_cmd.add_argument("--force", action="store_true", help="清空现有数据后重新写入")
    seed_cmd.set_defaults(func=cmd_seed)

    sub.add_parser("reset", help="重置数据库并重建演示数据").set_defaults(func=cmd_reset)

    sub.add_parser("check", help="数据体检（库存 / 重复在借 / 登录标识冲突）").set_defaults(func=cmd_check)

    sub.add_parser("repair", help="修复重复在借记录并补建唯一索引").set_defaults(func=cmd_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["run"])
    try:
        return args.func(args)
    except Exception as exc:  # 启动期错误：打印清晰原因，不抛栈给用户
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
