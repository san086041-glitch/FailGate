"""LLM 接入层：OpenAI 兼容客户端、计价、结构化输出（技术方案第 13 节）。"""

from .client import LLMClient, LLMError, LLMResponse, ToolCall, Usage

__all__ = ["LLMClient", "LLMError", "LLMResponse", "ToolCall", "Usage"]
