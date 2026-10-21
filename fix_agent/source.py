"""
源码编辑原语：把 AST 节点的位置精确映射回文本区间。

为什么不用现成的代码改写库（libcst / rope）：

- 作业要求"零第三方依赖"能跑，而这两个库都不在标准库里；
- 本次要做的都是**局部、单点**改写（一个表达式、一个语句、一个导入），
  AST + 文本区间足够，不需要完整的格式保留式重写；
- 手写这 150 行换来的是"完全知道自己改了哪几个字符"，出问题时能精确解释，
  这对一个会给别人代码打补丁的 Agent 来说是必需的。

**关键陷阱：``col_offset`` 是 UTF-8 字节偏移，不是字符偏移。**
源码里有中文时（注释、docstring、字符串字面量），直接用 ``line[col_offset:]``
会切在错误的位置。本模块统一做字节 → 字符的换算，这是踩过的坑。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterable, Sequence


class SpliceError(ValueError):
    """区间无法安全改写（越界、重叠、位置缺失）。"""


@dataclass(frozen=True)
class Edit:
    """一处文本替换。``start`` / ``end`` 是**字符**偏移（左闭右开）。"""

    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise SpliceError(f"非法区间 [{self.start}, {self.end})")


class SourceMap:
    """源码 → (行, UTF-8 字节列) 的位置换算器。"""

    def __init__(self, source: str) -> None:
        self.source = source
        self._lines = source.splitlines(keepends=True)
        self._starts: list[int] = []
        cursor = 0
        for line in self._lines:
            self._starts.append(cursor)
            cursor += len(line)
        # 最后补一个"文件末尾"位置，方便 end_lineno 刚好落在最后一行时取值
        self._starts.append(cursor)

    @property
    def total_length(self) -> int:
        return len(self.source)

    def line_text(self, lineno: int) -> str:
        if lineno < 1 or lineno > len(self._lines):
            return ""
        return self._lines[lineno - 1].rstrip("\r\n")

    def offset(self, lineno: int, col_offset: int) -> int:
        """把 (1-based 行号, UTF-8 字节列) 换算成整篇源码的字符偏移。"""
        if lineno < 1 or lineno > len(self._lines):
            raise SpliceError(f"行号越界：{lineno}（文件共 {len(self._lines)} 行）")
        line = self._lines[lineno - 1]
        char_col = byte_col_to_char_col(line, col_offset)
        return self._starts[lineno - 1] + char_col

    def node_span(self, node: ast.AST) -> tuple[int, int]:
        """取节点在源码中的 [start, end) 字符区间。"""
        start_line = getattr(node, "lineno", None)
        start_col = getattr(node, "col_offset", None)
        end_line = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if start_line is None or start_col is None:
            raise SpliceError(f"节点缺少起始位置信息：{type(node).__name__}")
        start = self.offset(start_line, start_col)
        if end_line is None or end_col is None:
            # 兼容旧版本 Python：退化为整行
            start_of_line = self._starts[start_line - 1]
            end = start_of_line + len(self.line_text(start_line))
            return start, end
        return start, self.offset(end_line, end_col)

    def line_span(self, lineno: int, *, include_newline: bool = True) -> tuple[int, int]:
        """取整行的字符区间（默认包含行尾换行）。"""
        if lineno < 1 or lineno > len(self._lines):
            raise SpliceError(f"行号越界：{lineno}")
        start = self._starts[lineno - 1]
        raw = self._lines[lineno - 1]
        end = start + (len(raw) if include_newline else len(raw.rstrip("\r\n")))
        return start, end

    def indent_of(self, lineno: int) -> str:
        """取某一行的缩进（空格串）。"""
        return self.line_text(lineno)[: len(self.line_text(lineno)) - len(self.line_text(lineno).lstrip())]


def byte_col_to_char_col(line: str, byte_col: int) -> int:
    """把 UTF-8 字节偏移换算为该行的字符下标。"""
    if byte_col <= 0:
        return 0
    encoded = line.encode("utf-8")
    if byte_col >= len(encoded):
        return len(line)
    prefix = encoded[:byte_col]
    try:
        return len(prefix.decode("utf-8"))
    except UnicodeDecodeError:
        # 切在多字节字符中间：退回到最近的合法边界
        return len(prefix.decode("utf-8", errors="ignore"))


def apply_edits(source: str, edits: Iterable[Edit]) -> str:
    """
    一次性应用多处编辑。

    实现要点：**按起点倒序**应用。这样前面区间的位置不会因为后面区间被替换而失效，
    不需要维护偏移增量，也就不容易算错。
    """
    ordered = sorted(edits, key=lambda e: (e.start, e.end), reverse=True)
    result = source
    last_start = len(source) + 1
    for edit in ordered:
        if edit.end > last_start:
            raise SpliceError(
                f"编辑区间重叠：[{edit.start}, {edit.end}) 与更靠后的编辑冲突"
            )
        result = result[: edit.start] + edit.text + result[edit.end :]
        last_start = edit.start
    return result


# AST 查找辅助
def parse(source: str, *, filename: str = "<string>") -> ast.AST:
    """解析源码；语法错误抛 :class:`SyntaxError`（调用方决定怎么处理）。"""
    return ast.parse(source, filename=filename)


def node_at_line(tree: ast.AST, lineno: int, types: tuple[type, ...] = (ast.AST,)) -> list[ast.AST]:
    """找出**起始行**落在指定行的所有节点。"""
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, types) and getattr(node, "lineno", None) == lineno
    ]
    found.sort(key=lambda n: (getattr(n, "col_offset", 0), -getattr(n, "end_lineno", 0)))
    return found


def nodes_covering_line(tree: ast.AST, lineno: int, types: tuple[type, ...] = (ast.AST,)) -> list[ast.AST]:
    """找出区间覆盖指定行的所有节点（用于断言某问题位于某个函数内）。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, types):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        if start is None or end is None:
            continue
        if start <= lineno <= end:
            found.append(node)
    found.sort(key=lambda n: (getattr(n, "end_lineno", 0) - getattr(n, "lineno", 0)))
    return found


def enclosing_function(tree: ast.AST, lineno: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """找出包含指定行的最内层函数定义。"""
    candidates = nodes_covering_line(
        tree, lineno, (ast.FunctionDef, ast.AsyncFunctionDef)
    )
    return candidates[0] if candidates else None


def enclosing_handler(tree: ast.AST, lineno: int) -> ast.ExceptHandler | None:
    candidates = nodes_covering_line(tree, lineno, (ast.ExceptHandler,))
    return candidates[0] if candidates else None


def statement_index(block: Sequence[ast.stmt], target: ast.stmt) -> int:
    """在语句列表里定位 target 的下标；找不到返回 -1。"""
    for index, candidate in enumerate(block):
        if candidate is target:
            return index
    for index, candidate in enumerate(block):
        if (
            getattr(candidate, "lineno", None) == getattr(target, "lineno", None)
            and getattr(candidate, "col_offset", None) == getattr(target, "col_offset", None)
        ):
            return index
    return -1


def enclosing_block(tree: ast.AST, stmt: ast.stmt) -> list[ast.stmt]:
    """找出直接包含 stmt 的那一层语句列表。"""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and any(item is stmt for item in block):
                return block
        for handler in getattr(node, "handlers", []) or []:
            if isinstance(handler.body, list) and any(item is stmt for item in handler.body):
                return handler.body
    return []


def indent_text(text: str, prefix: str) -> str:
    """给多行文本整体加缩进（跳过空行）。"""
    return "\n".join(
        (prefix + line if line.strip() else line) for line in text.split("\n")
    )


def dotted_name(node: ast.AST | None) -> str:
    """把 ``a.b.c`` 形式的属性访问还原成点号字符串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def line_mapping(before: str, after: str) -> dict[int, int]:
    """
    计算 ``before`` 每一行在 ``after`` 中的新行号（1-based）。

    存在的理由是一个**真实踩到的 bug**：修补器大多只动自己那一行，但补 ``import
    logging`` / ``logger = ...`` 这类模块级前置语句会插在文件很靠前的位置，把后面
    所有行号整体顶下去。如果还拿旧行号去定位下一条发现，修补器会"静静地改错地方"。

    用 ``difflib`` 做行级对齐而不是自己算增量：一个补丁可能同时删行和插行（例如把
    ``open(); read(); close()`` 变成 ``with: read()``），增量法在这种地方必然算错，
    而对齐算法不需要特判。

    返回的字典只包含"能对上"的行；被删掉的行不在字典里，调用方据此判定"该发现的位置
    已经消失，转人工"。
    """
    import difflib

    old_lines = before.split("\n")
    new_lines = after.split("\n")
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset + 1] = j1 + offset + 1
        elif tag == "replace":
            # 块内长度可能不同，只对齐前面能一一对应的部分
            for offset in range(min(i2 - i1, j2 - j1)):
                mapping[i1 + offset + 1] = j1 + offset + 1
    return mapping
