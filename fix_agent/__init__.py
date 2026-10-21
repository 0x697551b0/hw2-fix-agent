"""
FixAgent —— 修复 Agent。

定位：Homework 2「代码审查流程」工作流的**第三个节点**。
职责：按审查 Agent 给的结构化发现改写代码，并**自证**每一处改写是安全的。

    from fix_agent import FixAgent, FixConfig

    agent = FixAgent(FixConfig(engine="rules"))
    outcome = agent.fix("./worktree", findings)
    print(outcome.summary(), len(outcome.applied))
"""

from fix_agent.agent import AGENT_NAME, FixAgent, FixRequestOptions
from fix_agent.config import FixConfig
from fix_agent.errors import (
    ConfigError,
    FixAgentError,
    PatchFailed,
    PatchRefused,
    TargetError,
    VerificationError,
)
from fix_agent.models import FixOutcome, FileFix, PatchRecord
from fix_agent.patchers import PATCHERS, NOT_SUPPORTED_REASONS, patcher_for, supported_rule_ids
from fix_agent.protocol import AgentRequest, AgentResponse, Artifact, FindingContract
from fix_agent.verifier import Verifier, VerifyResult

__version__ = "1.0.0"
__agent_name__ = AGENT_NAME

__all__ = [
    "AGENT_NAME",
    "AgentRequest",
    "AgentResponse",
    "Artifact",
    "ConfigError",
    "FileFix",
    "FindingContract",
    "FixAgent",
    "FixAgentError",
    "FixConfig",
    "FixOutcome",
    "FixRequestOptions",
    "NOT_SUPPORTED_REASONS",
    "PATCHERS",
    "PatchFailed",
    "PatchRecord",
    "PatchRefused",
    "TargetError",
    "VerificationError",
    "Verifier",
    "VerifyResult",
    "__agent_name__",
    "__version__",
    "patcher_for",
    "supported_rule_ids",
]
