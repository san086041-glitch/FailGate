"""LLM 接入层：OpenAI 兼容客户端、计价、结构化输出（技术方案第 13 节）。"""

from .client import LLMClient, LLMError, LLMResponse, Thinking, ToolCall, Usage, thinking_params
from .gateway import LLMGateway, parse_spec

__all__ = [
    "LLMClient", "LLMError", "LLMGateway", "LLMResponse", "Thinking", "ToolCall", "Usage",
    "parse_spec", "thinking_params",
]
