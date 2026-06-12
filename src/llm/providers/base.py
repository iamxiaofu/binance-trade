"""Provider 抽象：只负责「构造请求 + 调 SDK + 解析 TradeDecision」。

重试 / 超时 / 失败降级 HOLD / LLMTrace 审计等安全逻辑全部留在 ``LLMClient`` 单点，
provider 之间只差「请求结构 + 解析方式」，因此用策略模式抽这三个方法。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from src.llm.schema import TradeDecision


@runtime_checkable
class LLMProvider(Protocol):
    """一个 LLM 对接源（anthropic / openai_compatible / ...）。"""

    name: str
    model: str

    def request_payload(self, *, system: str, user_prompt: str, max_tokens: int) -> dict:
        """可 json 序列化的请求快照，供 LLMTrace.request_json（不含 api_key）。"""
        ...

    async def create(self, *, system: str, user_prompt: str, max_tokens: int) -> Any:
        """调 SDK，强制 submit_decision 工具，返回原始 response。"""
        ...

    def parse(self, resp: Any, expected_symbol: str) -> TradeDecision | None:
        """从原始 response 解析 TradeDecision；失败 / 无工具调用返回 None。"""
        ...

    async def ping(self, *, max_tokens: int = 16) -> None:
        """test 端点用的最小连通性探测；失败抛异常。"""
        ...

    async def close(self) -> None:
        ...
