"""巡检 Agent：真正的自主循环（感知 → 决策 → 行动）。

- 感知：定时扫描修复队列、档口积压、滞留订单；
- 决策：超时/积压/滞留判定 + 去重（同一问题不反复报警）；
- 行动：只发布预警事件（patrol.*），不修改任何业务数据——只读红线。
"""
import time

from ..core import config, events


_last_warned: dict[str, float] = {}


def _should_warn(key: str, now: float) -> bool:
    """同一问题在去重窗口内只报一次。"""
    last = _last_warned.get(key, 0.0)
    if now - last < config.PATROL_DEDUPE_SECONDS:
        return False
    _last_warned[key] = now
    return True


def run_once(conn, now: float | None = None) -> list[dict]:
    """执行一轮巡检，返回本次发出的预警列表（并发布为 patrol.* 事件）。"""
    now = now or time.time()
    findings: list[dict] = []

    # 1) 修复队列超时未处理
    for row in conn.execute(
        "SELECT * FROM repair_queue WHERE status='open' AND deadline_at IS NOT NULL AND deadline_at < ?",
        (now,),
    ).fetchall():
        minutes = int((now - row["deadline_at"]) / 60)
        if not _should_warn(f"repair:{row['id']}", now):
            continue
        findings.append({
            "type": "patrol.repair_overdue", "biz_no": row["biz_no"] or "",
            "message": f"修复单 #{row['id']} 超时 {minutes} 分钟未处理（责任人：{row['assigned_to']}）",
            "payload": {"message": f"修复单 #{row['id']} 超时 {minutes} 分钟未处理（责任人：{row['assigned_to']}）",
                        "repair_id": row["id"], "overdue_minutes": minutes,
                        "assigned_to": row["assigned_to"], "reason": row["reason"]},
        })

    # 2) 档口积压（排队+制作中 ≥ 阈值）
    for row in conn.execute(
        "SELECT station, COUNT(*) c FROM board_tasks WHERE status IN ('queued','cooking') "
        "GROUP BY station HAVING c >= ?",
        (config.PATROL_BACKLOG_THRESHOLD,),
    ).fetchall():
        level = row["c"] // config.PATROL_BACKLOG_THRESHOLD
        if not _should_warn(f"backlog:{row['station']}:{level}", now):
            continue
        findings.append({
            "type": "patrol.station_backlog", "biz_no": "",
            "message": f"档口「{row['station']}」积压 {row['c']} 单，超过阈值 {config.PATROL_BACKLOG_THRESHOLD}",
            "payload": {"message": f"档口「{row['station']}」积压 {row['c']} 单，超过阈值 {config.PATROL_BACKLOG_THRESHOLD}",
                        "station": row["station"], "count": row["c"], "threshold": config.PATROL_BACKLOG_THRESHOLD},
        })

    # 3) 订单滞留（blocked / needs_repair 停留过久）
    for row in conn.execute(
        "SELECT biz_no, status, created_at FROM orders WHERE status IN ('blocked','needs_repair')",
    ).fetchall():
        stuck = int((now - row["created_at"]) / 60)
        if stuck < config.PATROL_BLOCKED_MINUTES:
            continue
        if not _should_warn(f"stuck:{row['biz_no']}:{stuck // config.PATROL_BLOCKED_MINUTES}", now):
            continue
        label = "被拦截待签核" if row["status"] == "blocked" else "待人工处理"
        findings.append({
            "type": "patrol.order_stuck", "biz_no": row["biz_no"],
            "message": f"订单 {row['biz_no']} {label}已 {stuck} 分钟，请尽快跟进",
            "payload": {"message": f"订单 {row['biz_no']} {label}已 {stuck} 分钟，请尽快跟进",
                        "status": row["status"], "stuck_minutes": stuck},
        })

    for f in findings:
        events.publish(conn, f["biz_no"], f["type"], f["payload"], actor="patrol")
        conn.commit()
    return findings


def start_loop(conn, interval: int | None = None):
    """后台自主巡检线程（守护线程，进程退出即停）。"""
    import threading
    seconds = interval or config.PATROL_INTERVAL_SECONDS

    def loop():
        while True:
            time.sleep(seconds)
            try:
                for f in run_once(conn):
                    print(f"  [patrol] {f['message']}")
            except Exception as exc:  # 巡检自身异常不拖垮主服务
                print(f"[patrol] 巡检异常（下一轮重试）: {exc}")

    thread = threading.Thread(target=loop, daemon=True, name="patrol-agent")
    thread.start()
    return thread
