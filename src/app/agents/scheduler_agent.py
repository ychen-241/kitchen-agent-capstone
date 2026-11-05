"""调度 Agent：确定性计分排菜，每条输出排序理由。

计分：过敏 P0 置顶 +100 > 催菜 +40/次 > 外卖 +10 > 等待 +2/分钟 > 档口压力 +1。
确定性规则，不交给概率模型——可解释是硬要求。
"""
import json
import time

from ..core import config


def compute_score(conn, task_row) -> tuple[float, str]:
    order = conn.execute("SELECT * FROM orders WHERE id=?", (task_row["order_id"],)).fetchone()
    item = conn.execute("SELECT * FROM order_items WHERE id=?", (task_row["order_item_id"],)).fetchone()
    reasons: list[str] = []
    score = 0.0

    if item["allergy_flag"]:
        score += config.SCORE_ALLERGY_P0
        reasons.append(f"含过敏备注，P0 置顶 +{config.SCORE_ALLERGY_P0}")

    if task_row["urge_count"]:
        score += config.SCORE_URGE * task_row["urge_count"]
        reasons.append(f"催菜 {task_row['urge_count']} 次 +{config.SCORE_URGE * task_row['urge_count']}")

    if order["source"] == "takeout":
        score += config.SCORE_TAKEOUT
        reasons.append(f"外卖时限 +{config.SCORE_TAKEOUT}")

    wait_minutes = max(0.0, (time.time() - order["created_at"]) / 60.0)
    wait_score = round(wait_minutes * config.SCORE_WAIT_PER_MIN, 1)
    score += wait_score
    reasons.append(f"等待 {wait_minutes:.0f} 分钟 +{wait_score}")

    same_station = conn.execute(
        "SELECT COUNT(*) c FROM board_tasks WHERE station=? AND status IN ('queued','cooking') AND order_item_id != ?",
        (task_row["station"], task_row["order_item_id"]),
    ).fetchone()["c"]
    if same_station:
        score += same_station * config.SCORE_STATION_PRESSURE
        reasons.append(f"档口压力（{same_station} 单在制） +{same_station}")

    return round(score, 1), "；".join(reasons) if reasons else "常规先到先做"


def upsert_tasks(conn, order_id: int, items: list[dict]) -> None:
    """为订单的可出品条目建/刷看板任务。"""
    for item in items:
        existing = conn.execute(
            "SELECT id FROM board_tasks WHERE order_item_id=?", (item["item_id"],)
        ).fetchone()
        if existing:
            _refresh(conn, existing["id"])
            continue
        conn.execute(
            "INSERT INTO board_tasks (order_item_id, order_id, station, dish_name, status) VALUES (?,?,?,?, 'queued')",
            (item["item_id"], order_id, item["station"], item["dish_name"]),
        )
    conn.commit()
    resort_station(conn)


def _refresh(conn, task_id: int) -> None:
    row = conn.execute("SELECT * FROM board_tasks WHERE id=?", (task_id,)).fetchone()
    score, reason = compute_score(conn, row)
    conn.execute("UPDATE board_tasks SET score=?, reason=? WHERE id=?", (score, reason, task_id))
    conn.commit()


def resort_station(conn, station: str | None = None) -> None:
    """重算分数（催菜/等待/压力变化后调用）。"""
    if station:
        rows = conn.execute(
            "SELECT * FROM board_tasks WHERE station=? AND status IN ('queued','cooking')", (station,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM board_tasks WHERE status IN ('queued','cooking')"
        ).fetchall()
    for row in rows:
        score, reason = compute_score(conn, row)
        conn.execute("UPDATE board_tasks SET score=?, reason=? WHERE id=?", (score, reason, row["id"]))
    conn.commit()


def snapshot_lines(conn) -> list[str]:
    """看板文本快照（用于演示输出/复盘）。"""
    rows = board_rows(conn)
    lines = [f"{'顺':<3}{'菜':<8}{'桌':<5}{'档口':<6}{'分':<7}理由/状态"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i:<3}{r['dish_name']:<8}{r['table_no'] or '-':<5}{r['station'] or '-':<6}"
                     f"{r['score']:<7}{r['reason']} [{r['status']}]")
    return lines


def board_rows(conn) -> list:
    return conn.execute(
        "SELECT b.*, o.table_no, o.biz_no FROM board_tasks b JOIN orders o ON o.id=b.order_id "
        "WHERE b.status IN ('queued','cooking') ORDER BY b.score DESC, b.id ASC"
    ).fetchall()
