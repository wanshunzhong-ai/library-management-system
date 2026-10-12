"""后台定时扫描线程：让「预约取书过期释放」与「逾期 / 到期提醒」真正到点生效。

## 为什么之前是惰性的

第 10 批把预约过期释放与到期 / 逾期提醒做成「读者打开页面时补算」，原因是**没有
调度载体**——凭空写一个只在单进程有效的调度器，会带来多进程重复触发、重启丢任务、
测试用例每次建 app 都起线程等一堆麻烦。但惰性的代价很实在：**读者不打开页面就永远
收不到逾期提醒**，预约过期也要等下次有人访问才让位。

本模块用最朴素的方式补上这一环：一个 daemon 线程，按固定间隔跑一轮扫描。

## 取舍与边界

* **只在 `app.py run` 启动**（``create_app(with_scheduler=True)``）。测试与其它
  嵌入式用法默认不起线程——否则 700 多个用例会各起一个后台线程，既慢又难排查。
* **失败只告警**：扫描逻辑本身是增强功能（幂等），任何异常都吞掉，绝不能让线程死掉
  或影响请求。
* **多进程下会重复跑**：但两条扫描（``sweep_expired`` / ``sweep_all_members``）都是
  幂等的（条件 UPDATE + ``dedupe_key`` 唯一索引兜底），重复执行的代价只是几次空查询。
  真要上多实例，正确做法是换成外部调度（cron / Celery beat）只跑一个实例。
* **间隔可配**：``LIBRARY_SWEEP_INTERVAL_SECONDS``（默认 600 秒，0 = 关闭线程，
  退回纯惰性模式）。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from library import db, timeutil

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def run_once(*, record: bool = True, kind: str = "auto") -> dict[str, int]:
    """跑一轮扫描（幂等）。返回本轮处理计数，供日志与测试直接调用。

    ``record=True`` 时把本轮结果写入 ``job_runs`` 留痕（必须在应用上下文内调用）。
    留痕失败只告警——它本身也是增强功能，不能反过来拖垮扫描。
    """
    started_at = timeutil.now().isoformat(timespec="seconds")
    t0 = time.monotonic()
    result = {"expired_reservations": 0, "members": 0, "due_soon": 0, "overdue": 0, "mail": 0}
    ok = True
    error = ""
    try:
        from library.reservations import service as reservation_service  # noqa: PLC0415

        result["expired_reservations"] = int(reservation_service.sweep_expired())
    except Exception:  # noqa: BLE001 - 见模块 docstring
        ok = False
        error = "预约过期释放扫描失败"
        logger.warning(error, exc_info=True)

    try:
        from library.notifications import service as notify_service  # noqa: PLC0415

        totals = notify_service.sweep_all_members()
        result["members"] = int(totals.get("members") or 0)
        result["due_soon"] = int(totals.get("due_soon") or 0)
        result["overdue"] = int(totals.get("overdue") or 0)
    except Exception:  # noqa: BLE001
        ok = False
        error = (error + "；" if error else "") + "通知全量扫描失败"
        logger.warning("通知全量扫描失败（已忽略）", exc_info=True)

    try:
        from library.notifications import outbound  # noqa: PLC0415

        result["mail"] = int(outbound.deliver_pending().get("delivered") or 0)
    except Exception:  # noqa: BLE001
        ok = False
        error = (error + "；" if error else "") + "邮件出站投递失败"
        logger.warning("邮件出站投递失败（已忽略）", exc_info=True)

    if record:
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO job_runs (job, kind, started_at, finished_at, duration_ms, ok, result, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        "sweep",
                        kind,
                        started_at,
                        timeutil.now().isoformat(timespec="seconds"),
                        int((time.monotonic() - t0) * 1000),
                        1 if ok else 0,
                        json.dumps(result, ensure_ascii=False),
                        error,
                    ],
                )
        except Exception:  # noqa: BLE001 - 留痕失败不影响扫描本身
            logger.warning("扫描留痕写入失败（已忽略）", exc_info=True)
    return result


def start(app, interval: int | None = None) -> bool:
    """启动后台扫描线程。``interval <= 0`` 或已在运行时直接返回 False。"""
    global _thread
    from config import get_config  # noqa: PLC0415

    seconds = int(get_config().sweep_interval_seconds if interval is None else interval)
    if seconds <= 0:
        logger.info("未启用后台扫描线程（LIBRARY_SWEEP_INTERVAL_SECONDS=0）")
        return False

    _stop_event.clear()
    with threading.Lock():
        if _thread is not None and _thread.is_alive():
            return False
        _thread = threading.Thread(
            target=_loop,
            args=(app, seconds),
            name="library-sweeper",
            daemon=True,
        )
        _thread.start()
    logger.info("后台扫描线程已启动", extra={"extra_fields": {"interval_seconds": seconds}})
    return True


def stop(timeout: float = 2.0) -> None:
    """停止后台扫描线程（进程退出 / 测试收尾）。"""
    global _thread
    _stop_event.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _thread = None


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def _loop(app, seconds: int) -> None:
    while not _stop_event.wait(seconds):
        try:
            with app.app_context():
                counts = run_once()
        except Exception:  # noqa: BLE001 - 线程绝不能因为一次异常退出
            logger.warning("后台扫描轮次异常（已忽略）", exc_info=True)
            continue
        if any(counts.values()):
            logger.info(
                "后台扫描完成",
                extra={"extra_fields": counts},
            )
