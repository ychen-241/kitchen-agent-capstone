"""规则解析兜底供应商（离线可用 / LLM 故障降级目标）。

降级纪律：降级样本置信度封顶 0.5（由调用方强制），并进入人工修复队列。
"""
import re

from .base import LLMProvider

CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


class StubProvider(LLMProvider):
    name = "rule-stub"

    def parse(self, text: str, dish_names: list[str]) -> dict:
        items: list[dict] = []
        table_no = ""
        m = re.search(r"(\d+)\s*号桌", text) or re.search(r"桌号[:：]?\s*(\d+)", text)
        if m:
            table_no = m.group(1)
        elif re.search(r"外卖|带走|打包", text):
            table_no = "外卖"

        matched_spans: list[tuple[int, int]] = []
        for name in sorted(dish_names, key=len, reverse=True):
            for match in re.finditer(re.escape(name), text):
                start = match.start()
                if any(s <= start <= e for s, e in matched_spans):
                    continue
                matched_spans.append((start, match.end()))
                # 向前找份数
                prefix = text[max(0, start - 4):start]
                qty = 1
                qm = re.search(r"(\d+)\s*份?([个份碗碟])?$", prefix)
                if qm:
                    qty = int(qm.group(1))
                elif prefix.endswith("份") and len(prefix) >= 2 and prefix[-2] in CN_NUM:
                    qty = CN_NUM[prefix[-2]]
                else:
                    for cn, val in CN_NUM.items():
                        if prefix.endswith(cn):
                            qty = val
                            break
                # 向后找备注（同句内）
                seg = text[match.end():text.find("\n", match.end()) if "\n" in text[match.end():] else len(text)]
                seg = seg.split("，")[0].split(",")[0].split("。")[0] if seg else ""
                notes = seg.strip() if seg.strip() and not re.search(r"\d+\s*份", seg) else ""
                items.append({"dish_name": name, "qty": qty, "notes": notes})

        confidence = 0.9 if items else 0.3
        if not table_no:
            confidence = min(confidence, 0.5)
        return {"table_no": table_no, "items": items, "confidence": round(confidence, 2)}

    def ask(self, question: str, context: str) -> str:
        """无 LLM 时做抽取式回答：按问题字符二元组匹配最相关句子。"""
        sentences = [s.strip() for s in re.split(r"[。\n]", context) if s.strip()]
        q = re.sub(r"[\s，。？?.,：:的了吗呢吧啊嘛]", "", question)
        grams = {q[i:i + 2] for i in range(len(q) - 1)} or {q}
        scored = []
        for s in sentences:
            hits = sum(1 for g in grams if g in s)
            if hits:
                scored.append((hits, s))
        scored.sort(reverse=True)
        if not scored:
            return "知识库中没有找到与该问题直接相关的内容，请联系管理员补充文档。"
        top = [s for _, s in scored[:3]]
        return "依据工艺卡：" + "；".join(top) + "。"
