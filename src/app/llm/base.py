"""LLM Provider 抽象（可替换供应商的关键接缝）。

两个能力：
- parse：自由文本口述单 → 结构化条目 + 置信度
- ask：知识库问答（给检索到的上下文段落）
"""
from abc import ABC, abstractmethod


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def parse(self, text: str, dish_names: list[str]) -> dict:
        """返回 {table_no, source_hint, items:[{dish_name, qty, notes}], confidence}"""

    @abstractmethod
    def ask(self, question: str, context: str) -> str:
        """知识库问答，context 为检索到的文档片段"""


def get_provider():
    """按配置选择供应商；未配 Key 返回 stub（离线模式）。

    生产替换点：此处按注册表扩展更多供应商（通义/DeepSeek/本地模型）。
    """
    from ..core import config as cfg
    if cfg.LLM_API_KEY:
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(cfg.LLM_API_KEY, cfg.LLM_BASE_URL, cfg.LLM_MODEL)
    from .stub_provider import StubProvider
    return StubProvider()
