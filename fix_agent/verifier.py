"""补丁自检：每次改写都要说明"问题真的少了，也没引入更严重的问题"。

修复 Agent 动的是别人的源码，没有自检的话，一次写错的改写就可能把"只有风格问题"的代码
变成"跑不起来"的代码。所以校验放在每次改写之后，不过就把这次改写变成拒绝改写。

``compile()`` 那条最简单。有 codeguard（Homework 1 的规则引擎）时用它做权威扫描，要求
目标规则命中数下降、阻断级（critical/high）命中数不上升；没有就降级成内置 AST 计数器，
只覆盖本 Agent 会改的 9 条规则，报告里会标明是降级模式。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

from fix_agent.protocol import SEVERITY_ORDER, FindingContract, is_blocking
from fix_agent.source import dotted_name

#: 内置计数器覆盖的规则（与本 Agent 的 patcher 集合一致）
BUILTIN_RULES: tuple[str, ...] = (
    "CG105", "CG106", "CG202", "CG203", "CG204", "CG206", "CG207", "CG402", "CG403",
)

#: 密钥类变量名关键词
_SECRET_HINTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "auth")

#: 会执行 SQL 的方法名
_SQL_CALLERS = frozenset({"execute", "executemany", "executescript", "executedict", "raw", "query"})

#: 各规则的默认级别（用于判断"新增了阻断级问题"）
_RULE_SEVERITY: dict[str, str] = {
    "CG105": "high",
    "CG106": "high",
    "CG202": "medium",
    "CG203": "high",
    "CG204": "low",
    "CG206": "medium",
    "CG207": "medium",
    "CG402": "info",
    "CG403": "low",
    "CG405": "critical",
}


@dataclass
class VerifyResult:
    """一次自检的结论。"""

    verified: bool
    reason: str = ""
    #: 改写前后的规则命中数（规则 ID → 次数）
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    #: 新增的阻断级规则
    new_blocking: list[str] = field(default_factory=list)
    #: ``codeguard`` 或 ``builtin``
    engine: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "engine": self.engine,
            "target_before": None,
            "target_after": None,
            "new_blocking": list(self.new_blocking),
        }


class Verifier:
    """补丁校验器。优先用 codeguard；找不到就降级到内置计数器。"""

    def __init__(
        self,
        codeguard: ModuleType | None = None,
        *,
        codeguard_path: str | Path | None = None,
    ) -> None:
        self.codeguard = codeguard
        if codeguard is None:
            # 有 codeguard 就用它做权威自检；没有就降级为内置计数器，绝不报错
            from fix_agent.locate import ensure_codeguard

            self.codeguard = ensure_codeguard(explicit=codeguard_path)

    @property
    def engine(self) -> str:
        return "codeguard" if self.codeguard is not None else "builtin"

    # 对外
    def counts(self, source: str, *, path: str = "<candidate>") -> dict[str, int]:
        """统计源码中各规则的命中次数。"""
        if self.codeguard is not None:
            return self._codeguard_counts(source, path)
        return self._builtin_counts(source)

    def check(
        self,
        candidate: str,
        *,
        path: str,
        rule_id: str,
        before: dict[str, int],
        blocking: Sequence[str] = ("critical", "high"),
    ) -> VerifyResult:
        """
        校验一处改写。

        判定标准（三条全部满足才算通过）：
        1. 候选源码能通过 ``compile()``；
        2. 目标规则的命中数严格下降；
        3. 阻断级规则命中数没有上升。
        """
        try:
            compile(candidate, path, "exec")
        except SyntaxError as exc:
            return VerifyResult(
                verified=False,
                reason=f"改写后源码无法编译：第 {exc.lineno} 行 {exc.msg}",
                before=before,
                engine=self.engine,
            )

        after = self.counts(candidate, path=path)
        normalized = (rule_id or "").strip().upper()

        if after.get(normalized, 0) >= before.get(normalized, 0):
            return VerifyResult(
                verified=False,
                reason=f"{normalized} 的命中数没有下降（{before.get(normalized, 0)} → {after.get(normalized, 0)}）",
                before=before,
                after=after,
                engine=self.engine,
            )

        new_blocking = [
            rule
            for rule, count in after.items()
            if count > before.get(rule, 0) and is_blocking(_RULE_SEVERITY.get(rule, "medium"), blocking)
        ]
        if new_blocking:
            return VerifyResult(
                verified=False,
                reason="改写引入了新的阻断级问题：" + "、".join(sorted(new_blocking)),
                before=before,
                after=after,
                new_blocking=sorted(new_blocking),
                engine=self.engine,
            )

        return VerifyResult(verified=True, before=before, after=after, engine=self.engine)

    def summarize(
        self,
        findings: Sequence[FindingContract],
        *,
        blocking: Sequence[str] = ("critical", "high"),
    ) -> dict[str, Any]:
        """对整批发现的处理结果做汇总（写进 payload）。"""
        by_severity = {sev: 0 for sev in SEVERITY_ORDER}
        for finding in findings:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        return {
            "engine": self.engine,
            "degraded": self.codeguard is None,
            "degraded_note": (
                "未找到 codeguard，规则级自检降级为内置计数器（仅覆盖本 Agent 会改写的 9 条规则）"
                if self.codeguard is None
                else ""
            ),
            "blocking_severities": list(blocking),
            "findings": len(findings),
            "by_severity": by_severity,
            "rules_covered": list(BUILTIN_RULES),
        }

    # codeguard 实现
    def _codeguard_counts(self, source: str, path: str) -> dict[str, int]:
        analyzer = self.codeguard.static_analysis.CodeAnalyzer()
        result = analyzer.analyze_source(source, path=str(Path(path).name))
        counts: dict[str, int] = {}
        for finding in result.findings:
            rule = str(getattr(finding, "rule_id", "") or "").upper()
            if rule:
                counts[rule] = counts.get(rule, 0) + 1
        return counts

    # 内置实现
    def _builtin_counts(self, source: str) -> dict[str, int]:
        counts = {rule: 0 for rule in BUILTIN_RULES}
        lines = source.split("\n")
        counts["CG402"] = sum(1 for line in lines if line != line.rstrip())

        try:
            tree = ast.parse(source)
        except SyntaxError:
            counts["CG405"] = 1
            return counts

        imported: dict[str, int] = {}
        referenced: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported[alias.asname or alias.name.split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        imported[alias.asname or alias.name] = node.lineno
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                name = dotted_name(node)
                if name:
                    referenced.add(name.split(".")[0])

        for name, lineno in imported.items():
            if name.startswith("_"):
                continue
            if name in referenced:
                continue
            # 名字出现在字符串里（注解、__all__）时保守跳过
            if any(name in line for index, line in enumerate(lines, start=1) if index != lineno and ("\"" in line or "'" in line)):
                continue
            counts["CG403"] += 1

        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = getattr(node, "value", None)
                target = ""
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    target = node.targets[0].id
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    target = node.target.id
                if (
                    target
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and any(hint in target.lower() for hint in _SECRET_HINTS)
                ):
                    counts["CG105"] += 1

            elif isinstance(node, ast.Call) and node.args:
                name = dotted_name(node.func).split(".")[-1].lower()
                if name in _SQL_CALLERS and len(node.args) == 1 and isinstance(node.args[0], ast.JoinedStr):
                    counts["CG106"] += 1
                if dotted_name(node.func) == "open" and not _inside_with(tree, node):
                    counts["CG206"] += 1

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = list(getattr(node.args, "defaults", []))
                for default in defaults:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        counts["CG203"] += 1

            elif isinstance(node, ast.ExceptHandler):
                if node.type is not None:
                    type_name = dotted_name(node.type)
                    if type_name in {"Exception", "BaseException"}:
                        has_logging = any(
                            isinstance(child, ast.Call)
                            and ("exception" in dotted_name(child.func) or "error" in dotted_name(child.func))
                            for child in ast.walk(node)
                        )
                        has_raise = any(isinstance(child, ast.Raise) for child in ast.walk(node))
                        if not has_logging and not has_raise:
                            counts["CG202"] += 1

            elif isinstance(node, ast.Compare):
                for op, comparator in zip(node.ops, node.comparators):
                    if not isinstance(op, (ast.Eq, ast.NotEq)):
                        continue
                    for side in (node.left, comparator):
                        if isinstance(side, ast.Constant) and (side.value is None or isinstance(side.value, bool)):
                            counts["CG204"] += 1

            elif isinstance(node, ast.Assert):
                # 只统计"校验外部输入"的 assert，跟 codeguard 的口径保持一致
                # （表达式里出现 input/request/args/json/param/user 这些特征词）。
                # 口径不一致会出现"修复前 2 条、修复后 1 条"的假象，触发无谓的重试。
                text = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
                if any(
                    keyword in text
                    for keyword in ("input", "request", "args", "json", "param", "user")
                ):
                    counts["CG207"] += 1

        return counts


def _inside_with(tree: ast.AST, call: ast.Call) -> bool:
    """判断某个 ``open()`` 调用是否处于 with 上下文中。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                if item.context_expr is call:
                    return True
    return False


__all__ = ["BUILTIN_RULES", "Verifier", "VerifyResult"]
