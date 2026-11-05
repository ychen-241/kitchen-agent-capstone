"""API 路由表（入口层：只做协议转换，业务在服务层）。

FastAPI 替换点：每行对应一个 @app.post/get 装饰器路由，处理器签名一致。
"""
import re
import time

from ..core import events, security
from ..agents import notify_agent
from ..models import db
from .. import services

ROUTES: list[tuple] = []  # (method, compiled_regex, handler, need_auth, roles)


def route(method: str, pattern: str, need_auth: bool = True, roles: tuple = ()):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn, need_auth, roles))
        return fn
    return deco


# ---------- 认证 ----------

@route("POST", r"/api/login", need_auth=False)
def login(conn, user, match, body):
    row = conn.execute("SELECT * FROM users WHERE username=? AND is_active=1", (body.get("username", ""),)).fetchone()
    if not row or not security.verify_password(body.get("password", ""), row["password_hash"]):
        return 401, {"error": "用户名或口令错误"}
    token = security.jwt_encode({"sub": row["username"], "uid": row["id"], "role": row["role"]})
    return 200, {"token": token, "username": row["username"], "role": row["role"], "station": row["station"]}


@route("GET", r"/api/me")
def me(conn, user, match, body):
    return 200, user


# ---------- 订单（员工/管理员） ----------

@route("POST", r"/api/orders")
def create_order(conn, user, match, body):
    source = body.get("source", "system")
    if source not in ("system", "paper", "voice", "takeout"):
        return 400, {"error": "source 须为 system/paper/voice/takeout"}
    result = services.create_order(conn, body.get("raw_text", ""), source, body.get("table_no", ""), user["username"])
    return 200, result


@route("GET", r"/api/orders")
def list_orders(conn, user, match, body):
    rows = conn.execute(
        "SELECT biz_no, source, table_no, status, confidence, provider, created_by, created_at "
        "FROM orders ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return 200, {"orders": [dict(r) for r in rows]}


def _order_action(action):
    def handler(conn, user, match, body):
        biz_no = match.group(1)
        return 200, getattr(services, action)(conn, biz_no, user["username"])
    return handler


ROUTES.extend([
    ("POST", re.compile(r"^/api/orders/([^/]+)/urge$"), _order_action("urge"), True, ()),
    ("POST", re.compile(r"^/api/orders/([^/]+)/cook$"), _order_action("cook"), True, ()),
    ("POST", re.compile(r"^/api/orders/([^/]+)/complete$"), _order_action("complete"), True, ()),
    ("POST", re.compile(r"^/api/orders/([^/]+)/serve$"), _order_action("serve"), True, ()),
    ("POST", re.compile(r"^/api/orders/([^/]+)/signoff$"), _order_action("signoff"), True, ("admin",)),
])


# ---------- 看板 ----------

@route("GET", r"/api/board")
def get_board(conn, user, match, body):
    return 200, {"board": services.board(conn), "ts": time.time()}


@route("GET", r"/api/state")
def get_state(conn, user, match, body):
    orders = conn.execute(
        "SELECT biz_no, source, table_no, status, confidence, provider, created_by FROM orders ORDER BY id DESC LIMIT 15"
    ).fetchall()
    patrol_rows = conn.execute(
        "SELECT event_type, payload, created_at FROM events WHERE event_type LIKE 'patrol.%' ORDER BY id DESC LIMIT 10"
    ).fetchall()
    import json as _json
    warnings = []
    for r in patrol_rows:
        try:
            payload = _json.loads(r["payload"] or "{}")
        except _json.JSONDecodeError:
            payload = {}
        warnings.append({
            "type": r["event_type"],
            "message": payload.get("message") or payload.get("reason") or r["event_type"],
            "time": time.strftime("%H:%M", time.localtime(r["created_at"])),
        })
    return 200, {"board": services.board(conn), "orders": [dict(r) for r in orders],
                 "repairs": services.repair_list(conn), "warnings": warnings}


# ---------- 知识库 ----------

@route("GET", r"/api/kb/documents")
def kb_list(conn, user, match, body):
    return 200, {"documents": services.list_documents(conn)}


@route("POST", r"/api/kb/documents", roles=("admin",))
def kb_add(conn, user, match, body):
    if not body.get("title") or not body.get("content"):
        return 400, {"error": "title 与 content 必填"}
    result = services.add_document(conn, body["title"], body.get("doc_type", "工艺卡"), body["content"], user["username"])
    return 200, result


@route("POST", r"/api/kb/qa")
def kb_qa(conn, user, match, body):
    question = body.get("question", "").strip()
    if not question:
        return 400, {"error": "question 必填"}
    return 200, services.qa(conn, question)


# ---------- 审计 / 修复队列（管理员） ----------

@route("GET", r"/api/audit", roles=("admin",))
def audit(conn, user, match, body):
    return 200, {"events": services.audit_query(conn, body.get("biz_no", "") if body else "")}


@route("GET", r"/api/repairs", roles=("admin",))
def repairs(conn, user, match, body):
    return 200, {"repairs": services.repair_list(conn)}


# ---------- 通知订阅装配（演示时打印到服务端控制台） ----------

def install_notify() -> None:
    def printer(evt):
        text = notify_agent.on_event(evt)
        if text:
            print(f"  [notify] {text}")
    for t in ("order.blocked", "order.needs_repair", "order.scheduled", "order.urge", "order.signoff",
              "patrol.repair_overdue", "patrol.station_backlog", "patrol.order_stuck"):
        events.subscribe(t, printer)


def dispatch(conn, user, method: str, path: str, body: dict):
    """匹配路由并执行；返回 (status, payload, need_auth_failed)。"""
    for m, pattern, handler, need_auth, roles in ROUTES:
        if m != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        if need_auth and user is None:
            return 401, {"error": "未登录或登录已过期"}, True
        if roles and (user is None or user.get("role") not in roles):
            return 403, {"error": f"需要 {'/'.join(roles)} 权限"}, user is not None
        status, payload = handler(conn, user, match, body)
        return status, payload, None
    return 404, {"error": f"未知接口 {method} {path}"}, None


def open_conn():
    return db.get_conn()
