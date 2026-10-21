"""修复 Agent 的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 单条发现的处理状态
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_DEFERRED = "deferred"
STATUS_SKIPPED = "skipped"

#: 文件级改写状态
FILE_CHANGED = "changed"
FILE_UNCHANGED = "unchanged"


@dataclass
class PatchRecord:
    """一条发现的处理记录。每条发现都要有结论和理由，不允许静默丢弃。"""

    finding_id: str
    rule_id: str
    file: str = ""
    line: int | None = None
    status: str = STATUS_SKIPPED
    strategy: str = ""
    #: 为什么这么处理：成功时说明改了什么，拒绝时说明为什么不能自动改
    note: str = ""
    before: str = ""
    after: str = ""
    verified: bool = False

    @property
    def applied(self) -> bool:
        return self.status == STATUS_APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "file": self.file,
            "line": self.line,
            "status": self.status,
            "strategy": self.strategy,
            "note": self.note,
            "before": self.before,
            "after": self.after,
            "verified": self.verified,
        }


@dataclass
class FileFix:
    """单个文件的改写结果。"""

    path: str
    original: str
    patched: str
    records: list[PatchRecord] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.patched != self.original

    @property
    def unified_diff(self) -> str:
        import difflib

        diff = difflib.unified_diff(
            self.original.splitlines(keepends=True),
            self.patched.splitlines(keepends=True),
            fromfile=f"a/{self.path}",
            tofile=f"b/{self.path}",
            n=3,
        )
        return "".join(diff)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "changed": self.changed,
            "records": [r.to_dict() for r in self.records],
            "added_lines": _line_delta(self.original, self.patched),
        }


@dataclass
class FixOutcome:
    """一次修复任务的完整结果（工作流 payload 的来源）。"""

    files: list[FileFix] = field(default_factory=list)
    engine: str = "rules"
    elapsed_seconds: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)

    # 聚合视图
    @property
    def records(self) -> list[PatchRecord]:
        return [record for file in self.files for record in file.records]

    @property
    def applied(self) -> list[PatchRecord]:
        return [r for r in self.records if r.status == STATUS_APPLIED]

    @property
    def rejected(self) -> list[PatchRecord]:
        return [r for r in self.records if r.status == STATUS_REJECTED]

    @property
    def deferred(self) -> list[PatchRecord]:
        return [r for r in self.records if r.status == STATUS_DEFERRED]

    @property
    def changed_files(self) -> list[str]:
        return [f.path for f in self.files if f.changed]

    @property
    def diff(self) -> str:
        return "".join(f.unified_diff for f in self.files if f.changed)

    def summary(self) -> str:
        """给终端看的一行结论。"""
        if not self.changed_files:
            reasons = len(self.rejected) + len(self.deferred)
            return (
                f"未改动任何文件（尝试 {len(self.records)} 条，全部转人工/暂缓）"
                if reasons
                else "没有需要处理的发现"
            )
        return (
            f"修复 {len(self.applied)} 条问题，涉及 {len(self.changed_files)} 个文件；"
            f"转人工 {len(self.rejected)} 条，暂缓 {len(self.deferred)} 条"
        )

    def payload(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "summary": self.summary(),
            "applied": [r.to_dict() for r in self.applied],
            "rejected": [r.to_dict() for r in self.rejected],
            "deferred": [r.to_dict() for r in self.deferred],
            "records": [r.to_dict() for r in self.records],
            "files": [f.to_dict() for f in self.files],
            "files_changed": self.changed_files,
            "diff": self.diff,
            "verification": self.verification,
            "diagnostics": dict(self.diagnostics),
        }


def _line_delta(before: str, after: str) -> int:
    return len(after.splitlines()) - len(before.splitlines())
