"""修复 Agent 的异常体系。

注意 :class:`PatchRefused` 的定位：它**不是错误**，而是修补器主动做出的"这块我不改"
决定。两者必须分开——把"拒绝修改"抛成异常再吞掉，会让人误以为出了故障；而实际上
"这条问题需要人来判断"是这套系统最重要的输出之一。
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
