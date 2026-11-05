"""OpenAI 兼容供应商：通过 HTTP API 调用（可换任何兼容服务）。

任何异常向上抛出，由解析 Agent 捕获后降级到规则解析——降级路径是设计出来的。
"""
import json
import re
import urllib.request

from .base import LLMProvider
from ..core import config as cfg


class OpenAIProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _chat(self, messages: list[dict], force_json: bool = False) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if force_json:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=cfg.LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]

    def parse(self, text: str, dish_names: list[str]) -> dict:
        prompt = (
            "你是餐厅点单解析器。把口述/纸质单文本解析为 JSON：\n"
            '{"table_no": "桌号字符串，识别不到为空", "items": [{"dish_name": "菜名(必须从候选中选)", '
            '"qty": 整数份数, "notes": "备注原文"}], "confidence": 0到1的数}\n'
            f"候选菜名：{json.dumps(dish_names, ensure_ascii=False)}\n"
            f"单据文本：{text}\n"
            "只输出 JSON。菜名不在候选中也要如实输出原文并降低 confidence。"
        )
        raw = self._chat(
            [{"role": "user", "content": prompt}],
            force_json=True,
        )
        match = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(match.group(0) if match else raw)
        result.setdefault("items", [])
        result.setdefault("confidence", 0.5)
        result.setdefault("table_no", "")
        return result

    def ask(self, question: str, context: str) -> str:
        prompt = (
            "根据以下知识库片段回答问题，只依据片段内容，答不出就如实说不知道。\n"
            f"[知识库片段]\n{context}\n\n[问题]\n{question}"
        )
        return self._chat([{"role": "user", "content": prompt}])
