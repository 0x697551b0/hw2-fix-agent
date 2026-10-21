"""源码编辑原语（source.py）的测试。

其中 ``test_column_offset_handles_chinese`` 是回归测试：``col_offset`` 是 **UTF-8 字节**
偏移，不是字符偏移。源码里有中文时直接用 ``line[col:]`` 会切在多字节字符中间，
改出来的代码是坏的 —— 而且不会报错，只会静静地改错地方。
"""

from __future__ import annotations

import ast

from fix_agent.source import (
    Edit,
    SourceMap,
    SpliceError,
    apply_edits,
    byte_col_to_char_col,
    dotted_name,
    enclosing_function,
    line_mapping,
    node_at_line,
    parse,
)

from tests.support import expect_raises


def test_apply_edits_descending_order() -> None:
    """多处编辑按起点倒序应用，前面的区间不会因为后面的替换而错位。"""
    text = "abcdefghij"
    result = apply_edits(text, [Edit(0, 2, "XY"), Edit(5, 7, "ZZ")])
    assert result == "XYcdeZZhij", result


def test_overlapping_edits_rejected() -> None:
    expect_raises(SpliceError, apply_edits, "abcdef", [Edit(0, 3, "X"), Edit(2, 5, "Y")])


def test_invalid_edit_rejected() -> None:
    expect_raises(SpliceError, Edit, 5, 2, "x")


def test_byte_col_to_char_col_ascii() -> None:
    assert byte_col_to_char_col("abc", 2) == 2
    assert byte_col_to_char_col("abc", 0) == 0
    assert byte_col_to_char_col("abc", 99) == 3


def test_column_offset_handles_chinese() -> None:
    """中文注释后的同一行代码，列偏移必须按字符算。"""
    line = '# 中文注释\nx = 1\n'
    sm = SourceMap(line)
    tree = parse(line)
    target = node_at_line(tree, 2, (ast.Assign,))[0]
    start, end = sm.node_span(target)
    assert line[start:end] == "x = 1", repr(line[start:end])


def test_chinese_line_mapping() -> None:
    line = '"""文档"""\n中文变量 = "值"\n'
    sm = SourceMap(line)
    tree = parse(line)
    constant = node_at_line(tree, 2, (ast.Constant,))[0]
    start, end = sm.node_span(constant)
    assert line[start:end] == '"值"', repr(line[start:end])


def test_node_span_multiline() -> None:
    text = "def f():\n    return (\n        1\n    )\n"
    sm = SourceMap(text)
    tree = parse(text)
    node = node_at_line(tree, 1, (ast.FunctionDef,))[0]
    start, end = sm.node_span(node)
    assert text[start:end].startswith("def f():")
    assert text[start:end].endswith(")")


def test_line_span_includes_newline() -> None:
    sm = SourceMap("a\nb\n")
    start, end = sm.line_span(1)
    assert sm.source[start:end] == "a\n"
    start2, end2 = sm.line_span(1, include_newline=False)
    assert sm.source[start2:end2] == "a"


def test_indent_of() -> None:
    sm = SourceMap("def f():\n    x = 1\n")
    assert sm.indent_of(2) == "    "


def test_line_mapping_insertion_shifts_lines() -> None:
    """前面插入两行，后面的行整体下移 2。"""
    before = "a\nb\nc\n"
    after = "new1\nnew2\na\nb\nc\n"
    mapping = line_mapping(before, after)
    assert mapping[1] == 3
    assert mapping[3] == 5


def test_line_mapping_deletion_drops_lines() -> None:
    before = "a\ndoomed\nb\n"
    after = "a\nb\n"
    mapping = line_mapping(before, after)
    assert mapping[1] == 1
    assert 2 not in mapping, "被删掉的行不应出现在映射里"
    assert mapping[3] == 2


def test_line_mapping_identity() -> None:
    text = "a\nb\nc\n"
    mapping = line_mapping(text, text)
    # split("\n") 会在末尾多出一个空行，所以是 4 行而不是 3 行
    assert mapping == {1: 1, 2: 2, 3: 3, 4: 4}, mapping


def test_enclosing_function() -> None:
    text = "def outer():\n    def inner():\n        return 1\n"
    tree = parse(text)
    func = enclosing_function(tree, 3)
    assert func is not None and func.name == "inner"


def test_dotted_name() -> None:
    tree = parse("cursor.execute(x)\n")
    call = node_at_line(tree, 1, (ast.Call,))[0]
    assert dotted_name(call.func) == "cursor.execute"
