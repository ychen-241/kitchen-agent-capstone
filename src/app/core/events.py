"""进程内事件总线 + 只追加事件留痕（对应表 events）。

事件是编排层唯一的通信方式：Agent 之间不互相调用。
"""
import json
import sqlite3
import threading
import time

from ..models import db

_lock = threading.Lock()
_subscribers: dict[str, list] = {}


def subscribe(event_type: str, handler) -> None:
    _subscribers.setdefault(event_type, []).append(handler)


def publish(conn: sqlite3.Connection, biz_no: str, event_type: str, payload: dict, actor: str = "system") -> None:
    """落库（append-only）并通知内存订阅者。"""
    with _lock:
        conn.execute(
            "INSERT INTO events (biz_no, event_type, payload, actor_id, created_at) VALUES (?,?,?,?,?)",
            (biz_no, event_type, json.dumps(payload, ensure_ascii=False), actor, time.time()),
        )
        conn.commit()
    for handler in _subscribers.get(event_type, []):
        try:
            handler(dict(biz_no=biz_no, type=event_type, payload=payload, actor=actor))
        except Exception as exc:  # 订阅者异常不阻断主流程
            print(f"[eventbus] subscriber error on {event_type}: {exc}")
