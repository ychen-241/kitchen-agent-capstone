"""编排器：解析 → 校验 → 调度 → 通知。

编排层只消费/产生事件，不依赖具体 Agent 的实现细节；
Agent 之间不互相调用，全部经由本管线与事件总线衔接。
"""
import time
import uuid

from ..core import config, events
from ..core.config import REPAIR_DEADLINE_MINUTES
from . import notify_agent, parser_agent, scheduler_agent, validator_agent


def process_new_order(conn, raw_text: str, source: str, table_no: str, actor: str) -> dict:
    """下单主链路。返回可直接作为 API 响应的结果。"""
    biz_no = f"R{time.strftime('%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"
    parsed = parser_agent.parse(conn, raw_text, source)

    # 桌号：显式传入 > 解析所得
    final_table = table_no or parsed["table_no"]

    conn.execute(
        "INSERT INTO orders (biz_no, source, table_no, status, raw_text, confidence, provider, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (biz_no, source, final_table, "created", raw_text, parsed["confidence"], parsed["provider"], actor, time.time()),
    )
    order_id = conn.execute("SELECT id FROM orders WHERE biz_no=?", (biz_no,)).fetchone()["id"]
    conn.commit()

    notices: list[str] = []

    # 1) 低置信度 → 人工修复队列（不阻断流程本身，但标需人工确认）
    if parsed["needs_repair"]:
        _enqueue_repair(conn, biz_no, "低置信度解析，需人工确认条目",
                        f"置信度 {parsed['confidence']}（阈值 {config.LLM_MIN_CONFIDENCE}），"
                        f"原因：{parsed['degraded_reason'] or '解析不确定'}")
        events.publish(conn, biz_no, "order.needs_repair", {"confidence": parsed["confidence"]}, actor)
        notices.append(f"解析置信度 {parsed['confidence']} 低于阈值，已转人工修复队列")

    # 2) 校验（过敏 BLOCK / 未知菜品）
    validated = validator_agent.validate(conn, parsed)
    for name, cand in validated["candidates"].items():
        notices.append(f"未知菜品「{name}」？候选：{'、'.join(cand) if cand else '无相似项'}")

    item_ids = []
    for item in validated["items"]:
        cur = conn.execute(
            "INSERT INTO order_items (order_id, dish_id, dish_name, qty, notes, allergy_flag, status) "
            "VALUES (?,?,?,?,?,?,'validated')",
            (order_id, item.get("dish_id"), item["dish_name"], item["qty"], item["notes"], item.get("allergy_flag", 0)),
        )
        item_ids.append({**item, "item_id": cur.lastrowid})

    if validated["blocked"]:
        for item in validated["blocked_items"]:
            conn.execute(
                "INSERT INTO order_items (order_id, dish_id, dish_name, qty, notes, allergy_flag, status) "
                "VALUES (?,?,?,?,?,?,'blocked')",
                (order_id, item.get("dish_id"), item["dish_name"], item["qty"], item["notes"],
                 1 if parser_agent.has_allergy_note(item.get("notes", "")) else 0),
            )
        reason = validated["blocked_items"][0]["block_reason"]
        conn.execute("UPDATE orders SET status='blocked' WHERE id=?", (order_id,))
        _enqueue_repair(conn, biz_no, reason, "整单暂停，等待食品安全主管签核放行",
                        assigned_to="食安主管")
        events.publish(conn, biz_no, "order.blocked", {"reason": reason}, actor)
        conn.commit()
        return {"biz_no": biz_no, "status": "blocked", "reason": reason,
                "blocked_items": validated["blocked_items"], "scheduled": 0, "notices": notices}

    # 3) 排菜
    if item_ids:
        scheduler_agent.upsert_tasks(conn, order_id, item_ids)
        conn.execute("UPDATE orders SET status='scheduled' WHERE id=?", (order_id,))
        events.publish(conn, biz_no, "order.scheduled", {"count": len(item_ids)}, actor)
        notices.append(f"已进看板 {len(item_ids)} 项")
    else:
        conn.execute("UPDATE orders SET status='needs_repair' WHERE id=?", (order_id,))
        notices.append("没有可识别的菜品条目，整单转人工")
        _enqueue_repair(conn, biz_no, "无可识别菜品条目", raw_text)
    conn.commit()
    return {"biz_no": biz_no, "status": "scheduled", "scheduled": len(item_ids),
            "confidence": parsed["confidence"], "provider": parsed["provider"], "notices": notices}


def release_order(conn, biz_no: str, actor: str) -> dict:
    """食安签核放行：剔除被拦截条目，其余进看板。"""
    order = conn.execute("SELECT * FROM orders WHERE biz_no=?", (biz_no,)).fetchone()
    if not order:
        return {"error": "订单不存在"}
    if order["status"] != "blocked":
        return {"error": f"订单状态为 {order['status']}，无需放行"}
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? AND status='blocked'", (order["id"],)
    ).fetchall()
    removed = [r["dish_name"] for r in items]
    conn.execute("UPDATE order_items SET status='removed_by_signoff' WHERE order_id=? AND status='blocked'", (order["id"],))
    ok_items = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? AND status='validated'", (order["id"],)
    ).fetchall()
    schedulable = [
        {"item_id": r["id"], "dish_id": r["dish_id"], "dish_name": r["dish_name"],
         "qty": r["qty"], "station": conn.execute("SELECT station FROM dishes WHERE id=?", (r["dish_id"],)).fetchone()["station"]}
        for r in ok_items if r["dish_id"]
    ]
    if schedulable:
        scheduler_agent.upsert_tasks(conn, order["id"], schedulable)
        conn.execute("UPDATE orders SET status='scheduled' WHERE id=?", (order["id"],))
        events.publish(conn, biz_no, "order.scheduled", {"count": len(schedulable), "after_signoff": True}, actor)
    else:
        conn.execute("UPDATE orders SET status='void' WHERE id=?", (order["id"],))
    _resolve_repair(conn, biz_no, actor)
    events.publish(conn, biz_no, "order.signoff", {"removed": removed, "by": actor}, actor)
    conn.commit()
    return {"biz_no": biz_no, "status": "signed_off", "removed": removed,
            "scheduled": len(schedulable), "notice": f"食安签核完成，剔除 {removed}，其余已进看板"}


def urge_order(conn, biz_no: str, actor: str) -> dict:
    """催菜：同单去重 → 加权 → 全看板重排。"""
    order = conn.execute("SELECT * FROM orders WHERE biz_no=?", (biz_no,)).fetchone()
    if not order:
        return {"error": "订单不存在"}
    if order["status"] not in ("scheduled", "cooking"):
        return {"error": f"订单状态 {order['status']} 不可催菜"}
    if not notify_agent.urge_allowed(biz_no, config.URGE_DEDUPE_SECONDS):
        return {"notice": f"同单 {config.URGE_DEDUPE_SECONDS // 60} 分钟内已催过，已去重"}
    conn.execute(
        "UPDATE board_tasks SET urge_count=urge_count+1, urge_at=? WHERE order_id=? AND status IN ('queued','cooking')",
        (time.time(), order["id"]),
    )
    scheduler_agent.resort_station(conn)
    count = conn.execute(
        "SELECT urge_count FROM board_tasks WHERE order_id=? LIMIT 1", (order["id"],)
    ).fetchone()
    events.publish(conn, biz_no, "order.urge", {"urge_count": count["urge_count"] if count else 1}, actor)
    conn.commit()
    return {"biz_no": biz_no, "notice": "催菜生效，看板已重排（每条排序理由已更新）"}


def _enqueue_repair(conn, biz_no: str, reason: str, detail: str, assigned_to: str = "值班经理") -> None:
    conn.execute(
        "INSERT INTO repair_queue (biz_no, reason, detail, assigned_to, deadline_at) VALUES (?,?,?,?,?)",
        (biz_no, reason, detail, assigned_to, time.time() + REPAIR_DEADLINE_MINUTES * 60),
    )
    conn.commit()


def _resolve_repair(conn, biz_no: str, actor: str) -> None:
    conn.execute(
        "UPDATE repair_queue SET status='resolved', resolved_at=?, resolved_by=? WHERE biz_no=? AND status='open'",
        (time.time(), actor, biz_no),
    )
    conn.commit()
