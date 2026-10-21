"""
修复 Agent 命令行。

``plan`` 命令值得单独说明：它只打印"这批发现里哪些会怎么处理"，不动任何文件。
在让 Agent 改代码之前先看一眼计划，是使用这类工具时的最低自我保护。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from fix_agent import __version__
from fix_agent.agent import FixAgent
from fix_agent.config import FixConfig
from fix_agent.errors import FixAgentError
from fix_agent.patchers import patcher_for, strategies_table, unsupported_reason
from fix_agent.protocol import FindingContract, dumps

BANNER = r"""
  _____ _      __
 |  ___(_)_  __\ \
 | |_  | \ \/ / > >
 |  _| | |>  < / /
 |_|   |_/_/\_\_|    修复 Agent · Homework 2
""".strip("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fix-agent",
        description="修复 Agent：按审查发现改写代码，每一处改写都要过自检",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m fix_agent plan ./worktree --findings ./reports/review.json\n"
            "  python -m fix_agent apply ./worktree --findings ./reports/review.json\n"
            "  python -m fix_agent apply ./worktree --findings ./reports/review.json --dry-run\n"
            "  python -m fix_agent strategies\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"fix-agent {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("target", help="要修复的目录")
    common.add_argument("--findings", required=True, help="审查结果 JSON（含 findings 数组）")
    common.add_argument("--engine", choices=("rules", "llm"), help="修复引擎（默认 rules）")
    common.add_argument("--only-rule", action="append", dest="only_rules", help="只修指定规则")
    common.add_argument("--skip-rule", action="append", dest="skip_rules", help="跳过指定规则")
    common.add_argument("--max-fixes", type=int, help="最多处理多少条")
    common.add_argument("--api-key", help="engine=llm 的 API Key")
    common.add_argument("--model", help="engine=llm 的模型名")
    common.add_argument("--codeguard-path", help="codeguard 源码根目录（自检用）")
    common.add_argument("--json", action="store_true", help="以 JSON 输出")
    common.add_argument("--quiet", action="store_true", help="只输出一行结论")
    common.add_argument("--verbose", action="store_true")

    plan_cmd = sub.add_parser("plan", parents=[common], help="只打印处理计划，不改文件")

    apply_cmd = sub.add_parser("apply", parents=[common], help="执行修复")
    apply_cmd.add_argument("--dry-run", action="store_true", help="只试运行，不写文件")
    apply_cmd.add_argument("--report-dir", help="把 fix.json / fix.diff 写到该目录")
    apply_cmd.add_argument("--keep-backup", action="store_true", help="写回前备份原文件")

    sub.add_parser("strategies", help="打印支持的改写策略")
    sub.add_parser("info", help="打印版本与配置")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args)

    if not args.command or args.command == "info":
        print(BANNER)
        print(f"\n版本：{__version__}    环境：Python {sys.version.split()[0]}")
        print(f"配置：{FixConfig.from_env().describe()}")
        print("\n可用命令：plan / apply / strategies / info")
        return 0

    if args.command == "strategies":
        return _cmd_strategies()

    try:
        findings = _load_findings(args.findings)
        return _cmd_plan_or_apply(args, findings)
    except FixAgentError as exc:
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"❌ 找不到文件：{exc}", file=sys.stderr)
        return 2


def _cmd_plan_or_apply(args: argparse.Namespace, findings: list[FindingContract]) -> int:
    config = _build_config(args)
    if args.command == "plan":
        print(_render_plan(findings))
        return 0

    agent = FixAgent(config)
    dry_run = bool(getattr(args, "dry_run", False))
    outcome = agent.fix(
        args.target,
        findings,
        dry_run=dry_run or None,
        only_rules=args.only_rules or None,
        skip_rules=args.skip_rules or None,
        max_fixes=args.max_fixes,
    )

    report_dir = getattr(args, "report_dir", None)
    if report_dir:
        directory = Path(report_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fix.json").write_text(dumps(outcome.payload()), encoding="utf-8", newline="\n")
        if outcome.diff:
            (directory / "fix.diff").write_text(outcome.diff, encoding="utf-8", newline="\n")

    if args.json:
        print(dumps(outcome.payload()))
    elif args.quiet:
        print(outcome.summary())
    else:
        print(_render_outcome(outcome, dry_run=dry_run))
    return 0


def _render_plan(findings: Sequence[FindingContract]) -> str:
    """处理计划：每条发现会走哪条路。"""
    lines = [
        f"处理计划（共 {len(findings)} 条发现）",
        "═" * 72,
    ]
    for finding in findings:
        patcher = patcher_for(finding.rule_id)
        if patcher is None:
            lines.append(
                f"  ⏸  {finding.severity.upper():<8} {finding.rule_id or '-':<7} "
                f"`{finding.location}` 转人工：{unsupported_reason(finding.rule_id)}"
            )
        else:
            lines.append(
                f"  🔧 {finding.severity.upper():<8} {finding.rule_id:<7} "
                f"`{finding.location}` → {patcher.strategy}"
            )
    auto = sum(1 for f in findings if patcher_for(f.rule_id))
    lines += [
        "═" * 72,
        f"可自动修复 {auto} 条，转人工 {len(findings) - auto} 条。",
        "（转人工的条目不会被静默丢弃，会写进修复报告。）",
    ]
    return "\n".join(lines)


def _render_outcome(outcome, *, dry_run: bool) -> str:
    lines = [
        ("试运行结果" if dry_run else "修复结果"),
        "═" * 72,
        f"  {outcome.summary()}",
        f"  自检引擎：{outcome.verification.get('engine', '-')}",
    ]
    if outcome.verification.get("degraded"):
        lines.append("  ⚠️  未找到 codeguard，规则级自检已降级为内置计数器")
    applied = outcome.applied
    if applied:
        lines += ["", "已修复："]
        for record in applied:
            lines.append(f"  ✅ {record.rule_id:<7} `{record.file}:{record.line}` {record.note}")
    rejected = outcome.rejected
    if rejected:
        lines += ["", "转人工："]
        for record in rejected:
            lines.append(f"  ⏸  {record.rule_id:<7} `{record.file}:{record.line}` {record.note}")
    if outcome.diff:
        lines += ["", "补丁 diff：", "─" * 72, outcome.diff]
    lines.append("═" * 72)
    return "\n".join(lines)


def _cmd_strategies() -> int:
    rows = strategies_table()
    print(f"{'规则':<8}{'策略':<26}说明")
    print("─" * 100)
    for row in rows:
        print(f"{row['rule_id']:<8}{row['strategy']:<26}{row['description']}")
    print(f"\n共支持 {len(rows)} 条规则的自动修复。其余规则会明确转人工，不做静默跳过。")
    return 0


def _load_findings(path: str) -> list[FindingContract]:
    """从审查结果 JSON 里读出 findings 数组；也接受裸数组。"""
    raw = Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("findings") or (data.get("payload") or {}).get("findings") or []
    else:
        raise FixAgentError("findings 文件应为 JSON 对象或数组")
    findings = [FindingContract.from_dict(item) for item in items if isinstance(item, dict)]
    if not findings:
        raise FixAgentError(f"{path} 里没有 findings 数组")
    return findings


def _build_config(args: argparse.Namespace) -> FixConfig:
    overrides: dict[str, object] = {}
    for key in ("engine", "api_key", "model", "codeguard_path", "max_fixes"):
        value = getattr(args, key, None)
        if value:
            overrides[key] = value
    if getattr(args, "only_rules", None):
        overrides["only_rules"] = tuple(args.only_rules)
    if getattr(args, "skip_rules", None):
        overrides["skip_rules"] = tuple(args.skip_rules)
    if getattr(args, "keep_backup", False):
        overrides["keep_backup"] = True
    config = FixConfig.from_env(**overrides)
    config.validate()
    return config


def _setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    _ensure_utf8_stdout()


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
