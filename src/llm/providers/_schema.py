"""把 TradeDecision 的 pydantic JSON Schema 转成各 provider 的工具/函数定义。

- Anthropic：``input_schema`` 接受 ``$ref/$defs``，直接用原始 schema。
- OpenAI 兼容：``function.parameters`` 对 ``$ref/$defs`` 支持脆弱（尤其第三方网关），
  必须用 ``_inline_defs`` 把 ``$defs`` 内联展开后再喂进去。
"""
from __future__ import annotations

from typing import Any

from src.llm.schema import TradeDecision

# 工具名：强制 LLM 以本工具返回结构化决策（两 provider 共用）。
_TOOL_NAME = "submit_decision"
_TOOL_DESC = "提交本周期对该标的的结构化交易决策。必须调用本工具。"


def build_anthropic_tool() -> dict:
    """Anthropic tool 定义（保留 $defs，Anthropic 接受 $ref）。"""
    schema = TradeDecision.model_json_schema()
    schema.pop("title", None)
    return {
        "name": _TOOL_NAME,
        "description": _TOOL_DESC,
        "input_schema": schema,
    }


def build_openai_function() -> dict:
    """OpenAI 兼容 function 定义（$defs 必须内联，否则部分网关报 schema 错）。"""
    return {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": _TOOL_DESC,
            "parameters": inline_defs(TradeDecision.model_json_schema()),
        },
    }


def inline_defs(schema: dict) -> dict:
    """把 JSON Schema 里的 ``$defs``/``definitions`` 内联到引用点，删除顶层 defs 与 title。

    例：``properties.action = {"$ref": "#/$defs/Action"}`` →
        ``properties.action = {"enum": [...], "type": "string", ...}``
    """
    defs: dict[str, Any] = {}
    defs.update(schema.get("$defs", {}) or {})
    defs.update(schema.get("definitions", {}) or {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = str(node["$ref"]).split("/")[-1]
                target = resolve(defs.get(ref_name, {}))
                merged = dict(target)
                # 引用点上的兄弟键（如 description）覆盖/补充内联结果
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = resolve(v)
                merged.pop("title", None)
                return merged
            return {k: resolve(v) for k, v in node.items() if k not in ("title",)}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    out = resolve({k: v for k, v in schema.items() if k not in ("$defs", "definitions")})
    out.pop("title", None)
    return out
