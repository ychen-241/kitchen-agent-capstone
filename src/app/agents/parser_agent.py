"""解析 Agent：自由文本 → 结构化条目。

纪律：
- LLM 优先，异常自动降级规则解析（降级是设计路径，不是故障处理）；
- 降级样本置信度封顶 0.5；
- 低于阈值 → needs_repair=True，由编排器送人工修复队列。
"""
import re

from ..core import config
from ..llm import base as llm_base
from ..llm.stub_provider import StubProvider


def parse(conn, raw_text: str, source: str) -> dict:
    dish_rows = conn.execute("SELECT name FROM dishes WHERE is_active=1").fetchall()
    dish_names = [r["name"] for r in dish_rows]

    provider = llm_base.get_provider()
    degraded = False
    try:
        result = provider.parse(raw_text, dish_names)
    except Exception as exc:
        # 降级路径：任何 LLM 异常 → 规则解析，置信度封顶 0.5
        degraded = True
        result = StubProvider().parse(raw_text, dish_names)
        result["confidence"] = min(float(result.get("confidence", 0.5)), 0.5)
        result["degraded_reason"] = f"LLM 异常降级: {type(exc).__name__}"
        result["provider"] = StubProvider.name

    # 规则兜底来源提示：口述/纸质单未识别桌号 → 降低置信度
    if source in ("paper", "voice") and not result.get("table_no"):
        result["confidence"] = min(float(result.get("confidence", 0.5)), 0.5)

    items = []
    for item in result.get("items", []):
        name = str(item.get("dish_name", "")).strip()
        if not name:
            continue
        notes = str(item.get("notes") or "").strip()
        qty = item.get("qty") or 1
        try:
            qty = max(1, int(qty))
        except (TypeError, ValueError):
            qty = 1
        items.append({"dish_name": name, "qty": qty, "notes": notes})

    confidence = float(result.get("confidence", 0.5))
    needs_repair = confidence < config.LLM_MIN_CONFIDENCE
    return {
        "items": items,
        "table_no": str(result.get("table_no") or ""),
        "confidence": round(confidence, 2),
        "provider": result.get("provider", getattr(provider, "name", "unknown")),
        "degraded": degraded,
        "degraded_reason": result.get("degraded_reason", ""),
        "needs_repair": needs_repair,
    }


ALLERGY_PATTERN = re.compile(r"过敏|忌口|不能吃")


def has_allergy_note(notes: str) -> bool:
    return bool(ALLERGY_PATTERN.search(notes or ""))
