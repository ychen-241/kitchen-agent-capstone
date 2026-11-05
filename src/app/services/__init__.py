"""服务层：订单状态机（口径唯一判定点）+ 看板 + 知识库 + 审计查询。"""
import json
import time

from ..agents import orchestrator, scheduler_agent


# ---- 订单服务 ----

def create_order(conn, raw_text: str, source: str, table_no: str, actor: str) -> dict:
    return orchestrator.process_new_order(conn, raw_text, source, table_no, actor)


def urge(conn, biz_no: str, actor: str) -> dict:
    return orchestrator.urge_order(conn, biz_no, actor)


def cook(conn, biz_no: str, actor: str) -> dict:
    order = _get_order(conn, biz_no)
    if not order or order["status"] != "scheduled":
        return {"error": f"订单状态 {order['status'] if order else '不存在'} 不可开工"}
    conn.execute("UPDATE orders SET status='cooking' WHERE id=?", (order["id"],))
    conn.execute("UPDATE board_tasks SET status='cooking' WHERE order_id=? AND status='queued'", (order["id"],))
    _event(conn, biz_no, "order.cooking", {"by": actor}, actor)
    conn.commit()
    return {"biz_no": biz_no, "status": "cooking"}


def complete(conn, biz_no: str, actor: str) -> dict:
    order = _get_order(conn, biz_no)
    if not order or order["status"] != "cooking":
        return {"error": f"订单状态 {order['status'] if order else '不存在'} 不可完工"}
    conn.execute("UPDATE orders SET status='ready' WHERE id=?", (order["id"],))
    conn.execute("UPDATE board_tasks SET status='done', snapshot_at=? WHERE order_id=? AND status='cooking'",
                 (time.time(), order["id"]))
    _event(conn, biz_no, "order.ready", {"by": actor}, actor)
    conn.commit()
    return {"biz_no": biz_no, "status": "ready"}


def serve(conn, biz_no: str, actor: str) -> dict:
    """上桌回写：ready → closed（结账归零），统计漏菜口径。"""
    order = _get_order(conn, biz_no)
    if not order or order["status"] != "ready":
        return {"error": f"订单状态 {order['status'] if order else '不存在'} 不可上桌"}
    missing = conn.execute(
        "SELECT COUNT(*) c FROM order_items WHERE order_id=? AND status NOT IN ('validated','removed_by_signoff')",
        (order["id"],),
    ).fetchone()["c"]
    conn.execute("UPDATE orders SET status='closed', closed_at=? WHERE id=?", (time.time(), order["id"]))
    _event(conn, biz_no, "order.closed", {"missing_items": missing, "by": actor}, actor)
    conn.commit()
    return {"biz_no": biz_no, "status": "closed", "missing_items": missing}


def signoff(conn, biz_no: str, actor: str) -> dict:
    return orchestrator.release_order(conn, biz_no, actor)


# ---- 看板服务 ----

def board(conn) -> list[dict]:
    rows = scheduler_agent.board_rows(conn)
    return [_task_dict(r) for r in rows]


def _task_dict(r) -> dict:
    return {
        "id": r["id"], "biz_no": r["biz_no"], "dish_name": r["dish_name"],
        "station": r["station"], "table_no": r["table_no"], "score": r["score"],
        "reason": r["reason"], "status": r["status"], "urge_count": r["urge_count"],
    }


# ---- 知识库服务 ----

def add_document(conn, title: str, doc_type: str, content: str, actor: str) -> dict:
    from ..kb import ingest
    cur = conn.execute(
        "INSERT INTO kb_documents (title, doc_type, status, updated_by, updated_at) VALUES (?,?,?,?,?)",
        (title, doc_type, "draft", actor, time.time()),
    )
    doc_id = cur.lastrowid
    chunks = ingest.chunk_and_embed(conn, doc_id, content)
    conn.execute("UPDATE kb_documents SET status='published' WHERE id=?", (doc_id,))
    conn.commit()
    return {"doc_id": doc_id, "title": title, "chunks": chunks, "status": "published"}


def qa(conn, question: str) -> dict:
    from ..kb import retriever
    from ..llm import base as llm_base
    hits = retriever.search(conn, question, top_k=3)
    if not hits:
        return {"answer": "知识库中没有相关内容，请联系管理员上传对应工艺卡。", "citations": []}
    context = "\n".join(f"[{h['doc_title']}#{h['chunk_index']}] {h['content']}" for h in hits)
    provider = llm_base.get_provider()
    try:
        answer = provider.ask(question, context)
    except Exception:
        from ..llm.stub_provider import StubProvider
        answer = StubProvider().ask(question, context)
    return {
        "answer": answer,
        "citations": [{"doc": h["doc_title"], "score": h["score"], "excerpt": h["content"][:60]} for h in hits],
    }


def list_documents(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, doc_type, version, status, updated_by, updated_at FROM kb_documents ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---- 审计/修复队列 ----

def audit_query(conn, biz_no: str = "") -> list[dict]:
    if biz_no:
        rows = conn.execute("SELECT * FROM events WHERE biz_no=? ORDER BY id", (biz_no,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["payload"] = json.loads(item["payload"] or "{}")
        except json.JSONDecodeError:
            pass
        item["time"] = time.strftime("%H:%M:%S", time.localtime(item["created_at"]))
        result.append(item)
    return result


def repair_list(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM repair_queue ORDER BY id DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["deadline"] = time.strftime("%H:%M", time.localtime(item["deadline_at"])) if item["deadline_at"] else ""
        out.append(item)
    return out


# ---- helpers ----

def _get_order(conn, biz_no: str):
    return conn.execute("SELECT * FROM orders WHERE biz_no=?", (biz_no,)).fetchone()


def _event(conn, biz_no: str, etype: str, payload: dict, actor: str) -> None:
    from ..core import events
    events.publish(conn, biz_no, etype, payload, actor)
