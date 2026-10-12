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
from pathlib import Path

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
    # 服务进程里开启后台定时扫描（预约过期释放 + 到期提醒）；
    # 其余子命令（init/seed/check/repair）不启线程，避免一次性任务被定时器拖住。
    app = create_app(cfg, with_scheduler=True)
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


def cmd_backup(args: argparse.Namespace) -> int:
    """数据库备份：默认做一份一致性快照，`--with-uploads` 连附件一起打 zip。"""
    from library import backup as backup_module

    cfg = _bootstrap()
    if getattr(args, "list", False):
        items = backup_module.list_backups()
        if not items:
            print(f"还没有任何备份。备份目录：{backup_module.backup_dir()}")
            return 0
        print(f"备份目录：{backup_module.backup_dir()}\n")
        print(f"{'文件名':<38}{'大小':>10}  {'类型':<6}{'附件':<6}创建时间")
        for item in items:
            print(
                f"{item['name']:<38}{item['size_label']:>10}  "
                f"{item['kind']:<6}{'是' if item['with_uploads'] else '否':<6}{item['created_at']}"
            )
        return 0

    app = create_app(cfg)
    with app.app_context():
        result = backup_module.create_backup(with_uploads=bool(args.with_uploads))
    print(f"备份完成：{result['path']}")
    print(
        f"  大小 {result['size_label']}　书目 {result['counts'].get('books', 0)} 种　"
        f"读者 {result['counts'].get('members', 0)} 人　借阅 {result['counts'].get('loans', 0)} 条　"
        f"附件 {result['uploaded_files']} 个"
    )
    if not result["with_uploads"]:
        print("  提示：附件目录未打包，需要整站迁移请加 --with-uploads")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """从备份恢复数据库。**必须在服务停止后执行**，覆盖前会自动留一份安全副本。"""
    import sys as _sys

    from library import backup as backup_module

    _bootstrap()
    source = Path(args.from_path).expanduser()
    if not source.exists():
        print(f"备份文件不存在：{source}", file=_sys.stderr)
        return 1
    if not args.yes:
        if not _sys.stdin.isatty():
            print("非交互环境下恢复必须显式加 --yes（该操作会覆盖当前数据库）", file=_sys.stderr)
            return 1
        print(f"即将用 {source} 覆盖当前数据库，覆盖前会自动留一份安全副本。")
        if input("确认继续？输入 yes 回车：").strip().lower() != "yes":
            print("已取消。")
            return 1
    result = backup_module.restore_backup(source)
    print(f"已从备份恢复：{result['restored_from']}")
    print(f"  当前数据库：{result['database']}")
    if result["safety_copy"]:
        print(f"  覆盖前的副本：{result['safety_copy']}")
    counts = result["counts"]
    print(
        f"  书目 {counts.get('books', 0)} 种　读者 {counts.get('members', 0)} 人　"
        f"借阅 {counts.get('loans', 0)} 条　后台账号 {counts.get('admins', 0)} 个"
    )
    print("  提示：请重新启动服务，并用原有账号登录验证数据。")
    return 0


def create_app_for_serve():
    """生产 WSGI 入口（给 waitress / gunicorn 用）。

    ``create_app`` 需要调用方传配置，WSGI 服务器没法传——这个零参工厂负责
    「读配置 + 初始化日志 + 起后台扫描」，等价于 ``python app.py run``：

        waitress-serve --call --host=0.0.0.0 --port=8000 app:create_app_for_serve
        gunicorn -w 2 -b 0.0.0.0:8000 "app:create_app_for_serve()"

    注意多进程下每个进程都会起自己的扫描线程；扫描是幂等的（重复只是几次空查询），
    若要严格只跑一次，把 ``LIBRARY_SWEEP_INTERVAL_SECONDS`` 设为 0，改用外部调度。
    """
    cfg = _bootstrap()
    return create_app(cfg, with_scheduler=True)


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

    backup_cmd = sub.add_parser("backup", help="备份数据库（默认快照到 backups/）")
    backup_cmd.add_argument("--with-uploads", action="store_true", help="连附件目录一起打成 zip")
    backup_cmd.add_argument("--list", action="store_true", help="只列出已有备份，不做备份")
    backup_cmd.set_defaults(func=cmd_backup)

    restore_cmd = sub.add_parser("restore", help="从备份恢复数据库（须停止服务后执行）")
    restore_cmd.add_argument("--from", dest="from_path", required=True, help="备份文件路径（.db 或 .zip）")
    restore_cmd.add_argument("--yes", action="store_true", help="跳过交互确认")
    restore_cmd.set_defaults(func=cmd_restore)
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
