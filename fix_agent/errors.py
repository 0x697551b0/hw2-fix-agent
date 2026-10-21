"""修复 Agent 的异常体系。

PatchRefused 有点特殊。它不表示出故障，只是修补器主动做了个"这块我不改"的决定，
所以走 rejected 这条正常路径，理由原样写进报告。要是当成异常抛出去再吞掉，
读代码的人会以为出了故障，实际上"这条得人来判断"本身就是一种结论。
"""

from __future__ import annotations


class FixAgentError(Exception):
    """本 Agent 所有异常的基类。"""


class PatchRefused(FixAgentError):
    """修补器判断该问题不适合自动改写。``reason`` 必须是人能读懂的理由。"""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class PatchFailed(FixAgentError):
    """修补器尝试改写但失败了（定位不到节点、区间非法等）。"""


class VerificationError(FixAgentError):
    """改写后的源码未通过自检（语法错误 / 问题未消失 / 引入新问题）。"""


class TargetError(FixAgentError):
    """目标目录或文件不可用。"""


class ConfigError(FixAgentError):
    """配置不合法。"""
