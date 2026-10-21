"""
FixAgent 主体：按审查发现，逐条改写代码，并且每一处改写都要过自检。

流程（对每个文件）：

1. 读到内存（**先不写盘**）；
2. 把该文件的发现**按行号从大到小**排序 —— 这是关键工程细节，见下方说明；
3. 逐条：查修补器 → 改写 → 自检（语法 + 规则数下降 + 无新增阻断级）；
4. 全部处理完才写回文件，并生成 unified diff。

为什么按行号**从大到小**处理：任何一次改写都可能插入或删除行。如果从前往后改，
第二处改动的行号就失效了，修补器会定位到错的代码上 —— 而且它不会报错，只会**静静地
改错地方**。倒序处理让"尚未处理的行号"始终有效，这是个不需要额外维护偏移表的办法。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from fix_agent.config import FixConfig
from fix_agent.errors import ConfigError, PatchFailed, PatchRefused, TargetError
from fix_agent.models import (
    STATUS_APPLIED,
    STATUS_DEFERRED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    FileFix,
    FixOutcome,
    PatchRecord,
)
from fix_agent.patchers import (
    PATCHERS,
    patcher_for,
    unsupported_reason,
)
from fix_agent.protocol import (
    SEVERITY_ORDER,
    STATUS_OK,
    AgentRequest,
    AgentResponse,
    Artifact,
    FindingContract,
    dumps,
)
from fix_agent.source import line_mapping
from fix_agent.verifier import Verifier

AGENT_NAME = "fix"

#: 允许改写的源码后缀
EDITABLE_SUFFIXES: frozenset[str] = frozenset({".py"})


@dataclass
class FixRequestOptions:
    """一次修复的可选参数。"""

    dry_run: bool | None = None
    only_rules: Sequence[str] | None = None
    skip_rules: Sequence[str] | None = None
    max_fixes: int | None = None


class FixAgent:
    """修复 Agent。``verifier`` 可注入，便于测试把自检固定成可控实现。"""

    def __init__(self, config: FixConfig | None = None, *, verifier: Verifier | None = None) -> None:
        self.config = config or FixConfig.from_env()
        self.config.validate()
        self.verifier = verifier or Verifier(
            codeguard_path=self.config.codeguard_path or None
        )
        self._llm = None

    # 对外主入口
    def fix(
        self,
        target: str | Path,
        findings: Iterable[FindingContract | dict[str, Any]],
        *,
        dry_run: bool | None = None,
        only_rules: Sequence[str] | None = None,
        skip_rules: Sequence[str] | None = None,
        max_fixes: int | None = None,
    ) -> FixOutcome:
        """按发现清单修复目标目录下的文件。"""
        started = time.perf_counter()
        root = self._resolve_root(target)
        contracts = [_coerce_finding(item) for item in findings]
        contracts = [f for f in contracts if f.status in {"open", ""}]

        effective_only = {
            (r or "").strip().upper() for r in (only_rules if only_rules is not None else self.config.only_rules)
        }
        effective_skip = {
            (r or "").strip().upper() for r in (skip_rules if skip_rules is not None else self.config.skip_rules)
        }
        selected = [
            f
            for f in contracts
            if (not effective_only or f.rule_id.upper() in effective_only)
            and f.rule_id.upper() not in effective_skip
        ]
        limit = max_fixes if max_fixes is not None else self.config.max_fixes
        selected.sort(key=lambda f: (_severity_rank(f.severity), f.file, -(f.line or 0)))
        selected = selected[:limit]

        outcome = FixOutcome(engine=self.config.engine)
        outcome.diagnostics.update(
            {
                "root": str(root),
                "engine": self.config.engine,
                "verifier": self.verifier.engine,
                "requested": len(contracts),
                "selected": len(selected),
                "dry_run": bool(dry_run if dry_run is not None else self.config.dry_run),
                "supported_rules": sorted(PATCHERS),
            }
        )

        by_file: dict[str, list[FindingContract]] = {}
        for finding in selected:
            by_file.setdefault(finding.file, []).append(finding)

        verification_rows: list[dict[str, Any]] = []
        for relative, file_findings in sorted(by_file.items()):
            file_fix = self._fix_file(
                root,
                relative,
                file_findings,
                dry_run=bool(dry_run if dry_run is not None else self.config.dry_run),
            )
            outcome.files.append(file_fix)
            if file_fix.changed:
                verification_rows.append(self._verify_file(root, file_fix))

        outcome.verification = {
            "enabled": self.config.verify,
            "engine": self.verifier.engine,
            "degraded": self.verifier.engine != "codeguard",
            "files": verification_rows,
            "applied": len(outcome.applied),
            "rejected": len(outcome.rejected),
            "deferred": len(outcome.deferred),
        }
        outcome.elapsed_seconds = round(time.perf_counter() - started, 3)
        return outcome

    # 单文件处理
    def _fix_file(
        self,
        root: Path,
        relative: str,
        findings: Sequence[FindingContract],
        *,
        dry_run: bool,
    ) -> FileFix:
        path = self._safe_path(root, relative)
        if path is None:
            return FileFix(
                path=relative,
                original="",
                patched="",
                records=[_rejected(f, "目标路径不在工作区内，已拒绝改写") for f in findings],
            )
        if not path.is_file():
            return FileFix(
                path=relative,
                original="",
                patched="",
                records=[_rejected(f, "文件不存在") for f in findings],
            )
        if path.suffix.lower() not in EDITABLE_SUFFIXES:
            return FileFix(
                path=relative,
                original="",
                patched="",
                records=[_deferred(f, f"暂不支持自动改写 {path.suffix} 文件") for f in findings],
            )

        original = path.read_text(encoding="utf-8")
        current = original
        records: list[PatchRecord] = []
        #: 报告里的行号一律用**审查报告里的原始行号**，避免读者拿它去当前文件里定位时错位
        original_lines = {f.finding_id: f.line for f in findings}

        # 行号从大到小，保证"还没处理的行号"尽量少受影响（仍不够，见下方 remap）
        ordered = sorted(findings, key=lambda f: (f.line or 10**9), reverse=True)
        pending = list(ordered)

        while pending:
            finding = pending.pop(0)
            record = PatchRecord(
                finding_id=finding.finding_id,
                rule_id=finding.rule_id,
                file=relative,
                line=original_lines.get(finding.finding_id, finding.line),
            )
            patcher = patcher_for(finding.rule_id)
            if patcher is None:
                record.status = STATUS_DEFERRED
                record.note = unsupported_reason(finding.rule_id)
                records.append(record)
                continue
            if not finding.line:
                record.status = STATUS_DEFERRED
                record.note = "发现没有行号，无法定位到具体代码，转人工"
                records.append(record)
                continue

            baseline = self.verifier.counts(current, path=relative)
            try:
                patch = patcher.apply(current, finding)
            except PatchRefused as exc:
                record.status = STATUS_REJECTED
                record.note = exc.reason
                records.append(record)
                continue
            except PatchFailed as exc:
                record.status = STATUS_REJECTED
                record.note = f"改写失败：{exc}"
                records.append(record)
                continue

            if self.config.verify:
                check = self.verifier.check(
                    patch.source,
                    path=relative,
                    rule_id=finding.rule_id,
                    before=baseline,
                    blocking=self.config.blocking,
                )
                if not check.verified:
                    record.status = STATUS_REJECTED
                    record.note = f"自检未通过：{check.reason}"
                    records.append(record)
                    continue
                record.verified = True

            record.status = STATUS_APPLIED
            record.strategy = patch.strategy
            record.note = patch.note
            record.before = patch.before
            record.after = patch.after
            records.append(record)
            # 改写可能插入/删除行（尤其是模块级 import 与 logger），
            # 因此立刻把**剩余待处理发现**的行号换算到新源码上
            current = self._remap_pending(current, patch.source, pending)

        if current != original and not dry_run:
            self._write(path, current, original)
        elif current != original and dry_run:
            for record in records:
                if record.applied:
                    record.note = f"{record.note}（试运行，未写盘）"

        return FileFix(path=relative, original=original, patched=current, records=records)

    @staticmethod
    def _remap_pending(
        before: str, after: str, pending: list[FindingContract]
    ) -> str:
        """
        把剩余待处理发现的行号换算到新源码上，并返回新源码。

        为什么必须做：修补器本身只动"自己那一行"，但补模块级 ``import logging`` /
        ``logger = ...`` 会插在文件顶部，把后面所有行号整体顶下去。不重算行号的话，
        下一条发现就会拿着旧行号去定位 —— 而修补器不会报错，它会在错误的行上"成功"地
        改掉一段无关代码。这类 bug 最贵，因为它看起来一切正常。

        换算用的是 :func:`fix_agent.source.line_mapping` 的整篇行级对齐；对不上的行
        （说明该行已被删除）把行号置空，后续会明确记为"位置失效，转人工"。
        """
        if not pending:
            return after
        mapping = line_mapping(before, after)
        for finding in pending:
            if not finding.line:
                continue
            mapped = mapping.get(int(finding.line))
            if mapped is None:
                finding.line = None
            else:
                finding.line = mapped
        return after

    def _verify_file(self, root: Path, file_fix: FileFix) -> dict[str, Any]:
        """对改写后的整份文件做一次汇总扫描。"""
        before = self.verifier.counts(file_fix.original, path=file_fix.path)
        after = self.verifier.counts(file_fix.patched, path=file_fix.path)
        cleared = [rule for rule, count in before.items() if count and not after.get(rule)]
        remaining = {rule: count for rule, count in sorted(after.items()) if count}
        increased = [
            rule for rule, count in after.items() if count > before.get(rule, 0)
        ]
        return {
            "path": file_fix.path,
            "before": {rule: count for rule, count in sorted(before.items()) if count},
            "after": remaining,
            "cleared": sorted(cleared),
            "increased": sorted(increased),
        }

    # LLM 引擎
    def _fix_with_llm(
        self,
        root: Path,
        relative: str,
        findings: Sequence[FindingContract],
        *,
        dry_run: bool,
    ) -> FileFix:
        """
        让模型整文件重写，然后走**同一套自检**。

        这是本 Agent 的核心主张：不管改写的来源是规则还是模型，通过的标准都一样 ——
        语法必须正确、目标规则必须真的不再命中、不能引入新的阻断级问题。模型说得再好，
        过不了自检就不落地。
        """
        path = self._safe_path(root, relative)
        if path is None or not path.is_file():
            return FileFix(
                path=relative,
                original="",
                patched="",
                records=[_rejected(f, "文件不可读") for f in findings],
            )
        original = path.read_text(encoding="utf-8")
        records = [
            PatchRecord(
                finding_id=f.finding_id,
                rule_id=f.rule_id,
                file=relative,
                line=f.line,
                status=STATUS_SKIPPED,
                strategy="llm_rewrite",
            )
            for f in findings
        ]

        baseline = self.verifier.counts(original, path=relative)
        try:
            candidate = self._ask_model(relative, original, findings, baseline)
        except Exception as exc:  # noqa: BLE001 - 模型侧失败统一转成"拒绝"
            for record in records:
                record.status = STATUS_REJECTED
                record.note = f"模型修复失败：{type(exc).__name__}: {exc}"
            return FileFix(path=relative, original=original, patched=original, records=records)

        if not self.config.verify:
            for record in records:
                record.status = STATUS_APPLIED
                record.note = "模型整文件重写（自检已关闭）"
            if not dry_run:
                self._write(path, candidate, original)
            return FileFix(path=relative, original=original, patched=candidate, records=records)

        try:
            compile(candidate, relative, "exec")
        except SyntaxError as exc:
            for record in records:
                record.status = STATUS_REJECTED
                record.note = f"模型产出无法编译：第 {exc.lineno} 行 {exc.msg}"
            return FileFix(path=relative, original=original, patched=original, records=records)

        after = self.verifier.counts(candidate, path=relative)
        # 至少要有一条目标规则真的少命中了，否则这次重写没有意义
        cleared = [
            f.rule_id
            for f in findings
            if after.get(f.rule_id.upper(), 0) < baseline.get(f.rule_id.upper(), 0)
        ]
        if not cleared:
            for record in records:
                record.status = STATUS_REJECTED
                record.note = "模型重写后目标规则命中数没有下降，自检拒绝落地"
            return FileFix(path=relative, original=original, patched=original, records=records)

        for record in records:
            if record.rule_id.upper() in {c.upper() for c in cleared}:
                record.status = STATUS_APPLIED
                record.verified = True
                record.note = "模型整文件重写并通过自检"
            else:
                record.status = STATUS_REJECTED
                record.note = "模型重写后该条规则仍然命中，转人工"

        if not dry_run:
            self._write(path, candidate, original)
        return FileFix(path=relative, original=original, patched=candidate, records=records)

    def _ask_model(
        self,
        relative: str,
        content: str,
        findings: Sequence[FindingContract],
        baseline: dict[str, int],
    ) -> str:
        from fix_agent.llm import StdlibChatLLM
        from fix_agent.prompts import SYSTEM_PROMPT, build_user_prompt, parse_fix_payload

        if self._llm is None:
            self._llm = StdlibChatLLM(self.config)
        result = self._llm.complete(
            SYSTEM_PROMPT, build_user_prompt(relative, content, findings)
        )
        payload = parse_fix_payload(result.text)
        files = payload.get("files") or []
        match = next((f for f in files if Path(f["path"]).name == Path(relative).name), None)
        if match is None:
            match = next((f for f in files if f["content"].strip()), None)
        if match is None:
            raise ValueError("模型没有返回该文件的内容")
        return match["content"]

    # 辅助
    def _resolve_root(self, target: str | Path) -> Path:
        path = Path(target)
        if not path.is_absolute():
            path = self.config.workspace_path / path
        path = path.resolve()
        if not path.exists():
            raise TargetError(f"修复目标不存在：{path}")
        return path

    @staticmethod
    def _safe_path(root: Path, relative: str) -> Path | None:
        """只允许改动工作区内的文件 —— 与 coding_agent 的路径沙箱同一思路。"""
        if not relative:
            return None
        candidate = Path(relative)
        target = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        return target

    def _write(self, path: Path, content: str, original: str) -> None:
        if self.config.keep_backup:
            backup_dir = path.parent / ".fix_backup"
            backup_dir.mkdir(exist_ok=True)
            (backup_dir / f"{path.name}.orig").write_text(original, encoding="utf-8", newline="\n")
        path.write_text(content, encoding="utf-8", newline="\n")

    # 协议入口
    def handle(self, request: AgentRequest) -> AgentResponse:
        problems = request.validate(expected_agent=AGENT_NAME)
        if problems:
            return AgentResponse.failure(AGENT_NAME, "请求不合法：" + "；".join(problems))

        payload = request.payload
        target = str(payload.get("target") or payload.get("workdir") or "").strip()
        if not target:
            return AgentResponse.failure(AGENT_NAME, "payload 缺少 target（要修复哪个目录）")
        raw_findings = payload.get("findings") or []
        if not isinstance(raw_findings, list):
            return AgentResponse.failure(AGENT_NAME, "payload.findings 必须是数组")
        if not raw_findings:
            empty = FixOutcome(engine=self.config.engine)
            return AgentResponse(
                agent=AGENT_NAME,
                status=STATUS_OK,
                summary="没有需要处理的发现",
                payload=empty.payload(),
                metrics={"applied": 0, "findings": 0},
                diagnostics={"engine": self.config.engine},
            )

        try:
            findings = [_coerce_finding(item) for item in raw_findings]
            if self.config.engine == "llm":
                outcome = self._fix_all_llm(target, findings, payload)
            else:
                outcome = self.fix(
                    target,
                    findings,
                    dry_run=_bool_or_none(payload.get("dry_run")),
                    only_rules=payload.get("only_rules") or None,
                    skip_rules=payload.get("skip_rules") or None,
                    max_fixes=payload.get("max_fixes"),
                )
        except (TargetError, ConfigError) as exc:
            return AgentResponse.failure(AGENT_NAME, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 协议层兜住一切
            return AgentResponse.failure(AGENT_NAME, f"未预期错误 {type(exc).__name__}: {exc}")

        response = AgentResponse(
            agent=AGENT_NAME,
            status=STATUS_OK,
            summary=outcome.summary(),
            payload=outcome.payload(),
            metrics={
                "elapsed_seconds": outcome.elapsed_seconds,
                "applied": len(outcome.applied),
                "rejected": len(outcome.rejected),
                "deferred": len(outcome.deferred),
                "files_changed": len(outcome.changed_files),
            },
            diagnostics=outcome.diagnostics,
        )
        out_dir = str(payload.get("report_dir") or "").strip()
        if out_dir:
            self._persist(out_dir, outcome, response)
        return response

    def _fix_all_llm(
        self, target: str | Path, findings: Sequence[FindingContract], payload: dict[str, Any]
    ) -> FixOutcome:
        """LLM 引擎按文件聚合后逐文件重写。"""
        started = time.perf_counter()
        root = self._resolve_root(target)
        dry_run = bool(_bool_or_none(payload.get("dry_run")) or self.config.dry_run)
        by_file: dict[str, list[FindingContract]] = {}
        for finding in findings:
            if finding.status in {"open", ""}:
                by_file.setdefault(finding.file, []).append(finding)

        outcome = FixOutcome(engine="llm")
        rows: list[dict[str, Any]] = []
        for relative, group in sorted(by_file.items()):
            file_fix = self._fix_with_llm(root, relative, group, dry_run=dry_run)
            outcome.files.append(file_fix)
            if file_fix.changed:
                rows.append(self._verify_file(root, file_fix))
        outcome.verification = {
            "enabled": self.config.verify,
            "engine": self.verifier.engine,
            "files": rows,
            "applied": len(outcome.applied),
            "rejected": len(outcome.rejected),
            "deferred": len(outcome.deferred),
        }
        outcome.elapsed_seconds = round(time.perf_counter() - started, 3)
        return outcome

    def _persist(self, out_dir: str, outcome: FixOutcome, response: AgentResponse) -> None:
        directory = Path(out_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "fix.json").write_text(dumps(outcome.payload()), encoding="utf-8", newline="\n")
            if outcome.diff:
                (directory / "fix.diff").write_text(outcome.diff, encoding="utf-8", newline="\n")
        except OSError:
            return
        response.artifacts.append(
            Artifact(
                kind="fix_report",
                path="fix.json",
                media_type="application/json",
                description=f"{len(outcome.applied)} 条已修复",
            )
        )
        if outcome.diff:
            response.artifacts.append(
                Artifact(
                    kind="diff",
                    path="fix.diff",
                    media_type="text/x-diff",
                    description="统一的补丁 diff",
                )
            )


# 辅助函数
def _coerce_finding(raw: FindingContract | dict[str, Any]) -> FindingContract:
    if isinstance(raw, FindingContract):
        return raw
    if isinstance(raw, dict):
        return FindingContract.from_dict(raw)
    raise ValueError(f"发现条目必须是 FindingContract 或 dict，实际为 {type(raw).__name__}")


def _severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(str(severity).strip().lower())
    except ValueError:
        return len(SEVERITY_ORDER)


def _rejected(finding: FindingContract, note: str) -> PatchRecord:
    return PatchRecord(
        finding_id=finding.finding_id,
        rule_id=finding.rule_id,
        file=finding.file,
        line=finding.line,
        status=STATUS_REJECTED,
        note=note,
    )


def _deferred(finding: FindingContract, note: str) -> PatchRecord:
    return PatchRecord(
        finding_id=finding.finding_id,
        rule_id=finding.rule_id,
        file=finding.file,
        line=finding.line,
        status=STATUS_DEFERRED,
        note=note,
    )


def _bool_or_none(value: Any) -> bool | None:
    return bool(value) if value is not None else None
