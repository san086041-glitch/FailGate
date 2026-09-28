"""模型计价（美元 / 百万 token）。

价格随厂商调整而变化，这里的数字取自 DeepSeek 官方价格页（2026-09）：
https://api-docs.deepseek.com/quick_start/pricing
未收录的模型按 0 计价并记录警告，不阻塞流程。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from .client import Usage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    cache_hit: float
    cache_miss: float
    output: float


# 高峰价；DeepSeek 非高峰时段半价
PEAK_PRICES: dict[str, Price] = {
    "deepseek-flash": Price(cache_hit=0.006, cache_miss=0.30, output=1.20),
    "deepseek-v4-pro": Price(cache_hit=0.044, cache_miss=1.32, output=3.96),
}
OFF_PEAK_FACTOR = {"deepseek-flash": 0.5, "deepseek-v4-pro": 0.5}


def is_deepseek_peak(at: datetime) -> bool:
    """高峰：UTC 周一至周五 01:00–04:00、06:00–10:00（未计入中国法定节假日）。"""
    at = at.astimezone(UTC)
    return at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)


def cost_usd(model: str, usage: Usage, at: datetime | None = None) -> float:
    price = PEAK_PRICES.get(model)
    if price is None:
        log.warning("未收录模型 %s 的价格，本次按 0 计价", model)
        return 0.0
    factor = 1.0
    if model in OFF_PEAK_FACTOR and not is_deepseek_peak(at or datetime.now(UTC)):
        factor = OFF_PEAK_FACTOR[model]
    miss = max(usage.prompt_tokens - usage.cached_tokens, 0)
    total = (
        usage.cached_tokens * price.cache_hit
        + miss * price.cache_miss
        + usage.completion_tokens * price.output
    )
    return total * factor / 1_000_000
