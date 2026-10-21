"""修复 Agent 的 Prompt（``engine=llm`` 时用）。

核心要求就一条：模型得返回完整文件，不能只给"改这几行"的片段。

原因是自检要编译整份源码、要扫全文件的规则命中，光给片段既没法编译也没法扫描。代价是
费 token，换来每份模型产出都能被机器验证，我觉得值。
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from fix_agent.protocol import FindingContract

SYSTEM_PROMPT = """\
你是一个严谨的代码修复工程师。用户会给你一份源码和一份静态审查发现清单，请逐条修复。

硬性要求：
1. 只输出一个 JSON 对象，不要输出解释文字，不要使用 Markdown 代码围栏。
2. JSON 结构：{"files": [{"path": "相对路径", "content": "修复后的完整文件内容"}],
                "notes": [{"finding_id": "...", "action": "fixed|skipped", "reason": "..."}]}
3. `content` 必须是**完整文件**，不是 diff、不是片段。
4. 只修清单里列出的问题。不要顺手重构、不要改接口签名、不要调整代码风格。
5. 拿不准的条目请标 `"action": "skipped"` 并说明原因。**乱改比不改危险得多。**
6. 修复后代码必须语法正确，且仍然实现原有功能。

修复口径（与规则一一对应）：
- 硬编码密钥 → 改从环境变量读取（os.environ），并在 notes 里提醒轮换泄露的凭据；
- SQL 注入 → 改成占位符 + 参数元组，绝不把变量拼进 SQL 字符串；
- 宽泛 except → 补日志（logger.exception），**保留原有的降级返回值**，不要擅自加 raise；
- 可变默认参数 → 改成 None 哨兵，函数体内初始化；
- open() 未用 with → 改成 with 语句；
- assert 做校验 → 改成 if not ...: raise ValueError(...)；
- 未使用的导入 → 删除；
- 行尾空白 → 删除。
"""


def build_user_prompt(
    path: str,
    content: str,
    findings: Sequence[FindingContract],
    *,
    extra_rules: str = "",
) -> str:
    """构造修复请求。"""
    lines = [
        f"# 文件：{path}",
        "",
        "## 待修复的审查发现",
        "",
    ]
    for finding in findings:
        lines.append(
            f"- [{finding.finding_id}] {finding.severity.upper()} {finding.rule_id} "
            f"第 {finding.line} 行：{finding.title}"
        )
        if finding.suggestion:
            lines.append(f"  - 建议：{finding.suggestion}")
    if extra_rules.strip():
        lines += ["", "## 额外约束", extra_rules.strip()]
    lines += [
        "",
        "## 源码",
        "```python",
        content.rstrip("\n"),
        "```",
        "",
        "请按要求只返回 JSON 对象。",
    ]
    return "\n".join(lines)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_fix_payload(text: str) -> dict[str, Any]:
    """解析模型输出，容忍 Markdown 围栏与前后废话。"""
    if not text or not text.strip():
        raise ValueError("模型输出为空")
    candidates = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return _normalize(parsed)
    raise ValueError(f"无法从模型输出中解析出 JSON：{text[:300]}")


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    files_raw = data.get("files") or {}
    if isinstance(files_raw, dict):
        files_raw = [{"path": k, "content": v} for k, v in files_raw.items()]
    files = [
        {"path": str(item.get("path") or ""), "content": str(item.get("content") or "")}
        for item in files_raw
        if isinstance(item, dict) and item.get("path") and item.get("content")
    ]
    notes = [
        {
            "finding_id": str(note.get("finding_id") or ""),
            "action": str(note.get("action") or "skipped"),
            "reason": str(note.get("reason") or ""),
        }
        for note in (data.get("notes") or [])
        if isinstance(note, dict)
    ]
    return {"files": files, "notes": notes}
