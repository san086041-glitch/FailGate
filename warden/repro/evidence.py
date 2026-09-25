"""证据等级（技术方案 8.1 节）：如实说明做到了哪一步。"""

from __future__ import annotations

from enum import StrEnum


class EvidenceLevel(StrEnum):
    NONE = "NONE"  # 没拿到任何证据（环境装不上、没复现出来……）
    L0 = "L0"  # 代码分析定位到可疑路径，但没有实际执行
    L1 = "L1"  # 在报告的版本上用独立脚本复现，失败特征一致
    L2 = "L2"  # 仓库内的失败测试
    L3 = "L3"  # 定位到引入版本或提交
    DELEGATED = "DELEGATED"  # 生成脚本，委托报告者或维护者运行
