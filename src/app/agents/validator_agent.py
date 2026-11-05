"""校验 Agent：过敏冲突 BLOCK 级拦截 + 未知菜品转人工（候选建议）。"""
import difflib
import json

from . import parser_agent


def _allergen_conflict(notes: str, allergens: list[str]) -> str | None:
    """备注提到某过敏原且菜品含该过敏原 → 返回冲突的过敏原。"""
    for allergen in allergens:
        if allergen and allergen in (notes or ""):
            return allergen
    return None


def validate(conn, parse_result: dict) -> dict:
    """逐项校验。返回 {blocked: bool, blocked_items: [...], candidates: {...}, items: [...]}"""
    blocked_items: list[dict] = []
    ok_items: list[dict] = []
    candidates: dict[str, list[str]] = {}
    all_names = [r["name"] for r in conn.execute("SELECT name FROM dishes WHERE is_active=1").fetchall()]

    for item in parse_result["items"]:
        name = item["dish_name"]
        row = conn.execute("SELECT * FROM dishes WHERE name=? AND is_active=1", (name,)).fetchone()
        if row is None:
            # 未知菜品：给出候选，转人工
            candidates[name] = difflib.get_close_matches(name, all_names, n=3, cutoff=0.4)
            blocked_items.append({**item, "block_reason": "未知菜品，需人工确认"})
            continue
        allergens = json.loads(row["allergens"] or "[]")
        conflict = _allergen_conflict(item["notes"], allergens)
        if conflict or (parser_agent.has_allergy_note(item["notes"]) and allergens and
                        any(a in item["notes"] for a in allergens)):
            blocked_items.append({
                **item,
                "dish_id": row["id"],
                "block_reason": f"过敏冲突 BLOCK：菜品「{name}」含过敏原「{conflict or '见备注'}」，需食安签核",
            })
            continue
        ok_items.append({**item, "dish_id": row["id"], "station": row["station"],
                         "allergy_flag": 1 if parser_agent.has_allergy_note(item["notes"]) else 0})

    return {
        "blocked": len(blocked_items) > 0,
        "blocked_items": blocked_items,
        "items": ok_items,
        "candidates": candidates,
    }
