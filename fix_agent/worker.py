"""``python -m fix_agent.worker``：子进程模式的一次 JSON 往返。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from fix_agent.agent import AGENT_NAME, FixAgent
from fix_agent.config import FixConfig
from fix_agent.protocol import AgentRequest, AgentResponse, dumps


def main(argv: Sequence[str] | None = None) -> int:
    # stdout 模式必须先把编码切到 UTF-8：Windows 控制台默认 GBK，
    # 修复摘要里带中文与 ✅/⏸ 符号，直接 print 会抛 UnicodeEncodeError
    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="python -m fix_agent.worker",
        description="以子进程方式执行一次修复任务（stdin → stdout 单次 JSON 往返）",
    )
    parser.add_argument("--request-file", help="请求 JSON 文件；省略则从 stdin 读取")
    parser.add_argument("--response-file", help="响应写到文件；省略则写到 stdout")
    parser.add_argument("--engine", choices=("rules", "llm"), help="覆盖请求中的引擎")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        raw = Path(args.request_file).read_text(encoding="utf-8-sig") if args.request_file else sys.stdin.read()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        return _emit(AgentResponse.failure(AGENT_NAME, f"无法读取请求 JSON：{exc}"), args.response_file)

    try:
        request = AgentRequest.from_dict(payload)
    except ValueError as exc:
        return _emit(AgentResponse.failure(AGENT_NAME, f"请求格式错误：{exc}"), args.response_file)

    overrides: dict[str, object] = {}
    if args.engine:
        overrides["engine"] = args.engine
    # 只传配置类认识的键：子进程里的 TypeError 对调用方只表现为"没有产出响应"
    from dataclasses import fields

    known = {f.name for f in fields(FixConfig)}
    overrides = {key: value for key, value in overrides.items() if key in known}

    try:
        config = FixConfig.from_env(**overrides)
        config.validate()
        agent = FixAgent(config)
    except Exception as exc:  # noqa: BLE001 - 初始化失败也要变成合法响应
        return _emit(AgentResponse.failure(AGENT_NAME, f"初始化失败：{exc}"), args.response_file)

    response = agent.handle(request)
    return _emit(response, args.response_file)


def _emit(response: AgentResponse, response_file: str | None) -> int:
    text = dumps(response.to_dict())
    if response_file:
        try:
            Path(response_file).write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:  # pragma: no cover
            print(f"无法写入响应文件：{exc}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    if not response.ok:
        print(f"修复 Agent 执行失败：{response.error}", file=sys.stderr)
        return 1
    return 0


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，输出中文/emoji 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
