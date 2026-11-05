"""通知 Agent：把事件翻译成给一线的可读提示（催菜、拦截、低置信度）。"""
import time


def on_event(event: dict) -> str | None:
    """事件总线订阅者：返回要展示的提示文本（推送渠道生产再接）。"""
    etype = event["type"]
    payload = event.get("payload", {})
    if etype == "order.urge":
        return f"[催菜] {event['biz_no']} 第 {payload.get('urge_count')} 次催菜，看板已重排"
    if etype == "order.blocked":
        return f"[拦截] {event['biz_no']} 被阻断：{payload.get('reason')}，等待食安签核"
    if etype == "order.needs_repair":
        return f"[人工] {event['biz_no']} 解析置信度 {payload.get('confidence')}，已进修复队列"
    if etype == "order.scheduled":
        return f"[排菜] {event['biz_no']} 已进看板（{payload.get('count')} 项）"
    if etype.startswith("patrol."):
        return f"[巡检] {payload.get('message', etype)}"
    return None


def install(subscriber) -> list[str]:
    """把通知订阅挂到事件总线上，返回订阅过的事件类型。"""
    types = ["order.urge", "order.blocked", "order.needs_repair", "order.scheduled"]
    for t in types:
        subscriber(t, on_event)
    return types


_last_urge: dict[str, float] = {}


def urge_allowed(biz_no: str, window_seconds: int) -> bool:
    """同单催菜去重（默认 5 分钟窗口）。"""
    now = time.time()
    last = _last_urge.get(biz_no, 0)
    if now - last < window_seconds:
        return False
    _last_urge[biz_no] = now
    return True
