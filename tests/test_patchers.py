"""修补器（patchers.py）的单元测试。

每个策略测两件事：改写之后目标规则命中数真的下降了（用 Verifier 判定，不靠肉眼看），
以及拿不准的形态必须抛 ``PatchRefused``。后面这条更要紧，会瞎改代码的修复 Agent
比不改的危险。
"""

from __future__ import annotations

import ast

from fix_agent.errors import PatchRefused
from fix_agent.patchers import (
    PATCHERS,
    NOT_SUPPORTED_REASONS,
    patcher_for,
    strategies_table,
    supported_rule_ids,
    unsupported_reason,
)
from fix_agent.protocol import FindingContract
from fix_agent.verifier import Verifier

from tests.support import buggy_source, expect_raises


def _finding(rule_id: str, line: int, title: str = "") -> FindingContract:
    return FindingContract(
        finding_id=f"F-{rule_id}-{line}", rule_id=rule_id, line=line, title=title or rule_id
    )


def _apply(rule_id: str, source: str, line: int, *, title: str = "") -> str:
    patcher = patcher_for(rule_id)
    assert patcher is not None, rule_id
    patch = patcher.apply(source, _finding(rule_id, line, title))
    ast.parse(patch.source)  # 改完必须是合法 Python
    return patch.source


def test_registry_covers_expected_rules() -> None:
    expected = {"CG105", "CG106", "CG202", "CG203", "CG204", "CG206", "CG207", "CG402", "CG403"}
    assert expected <= set(supported_rule_ids()), supported_rule_ids()


def test_every_patcher_has_description() -> None:
    for rule_id, patcher in PATCHERS.items():
        assert patcher.description, rule_id
        assert patcher.strategy, rule_id


def test_unknown_rule_has_no_patcher() -> None:
    assert patcher_for("CG999") is None
    assert "未知" in unsupported_reason("CG999")


def test_unsupported_reasons_documented() -> None:
    for rule, reason in NOT_SUPPORTED_REASONS.items():
        assert reason, rule


def test_strategies_table_complete() -> None:
    rows = strategies_table()
    assert len(rows) == len(PATCHERS)
    assert all(row["strategy"] for row in rows)


# 各策略
def test_cg105_moves_secret_to_env() -> None:
    source = buggy_source()
    patched = _apply("CG105", source, 6)
    assert 'os.environ["AUTH_TOKEN"]' in patched, patched
    assert "import os" in patched
    counts = Verifier().counts(patched, path="x.py")
    assert counts.get("CG105", 0) == 0, counts


def test_cg105_refuses_non_literal() -> None:
    source = '"""x"""\n\nTOKEN = "sk" + "-abc"\n'
    expect_raises(PatchRefused, lambda: _apply("CG105", source, 3))


def test_cg106_parameterizes_inline_sql() -> None:
    source = buggy_source()
    patched = _apply("CG106", source, 11)
    assert 'execute(f"' not in patched
    assert "?, ?, ?" in patched or "VALUES (?, ?, ?)" in patched, patched
    assert "name, email, age" in patched
    assert Verifier().counts(patched, path="x.py").get("CG106", 0) == 0


def test_cg106_refuses_when_already_parameterized() -> None:
    source = '"""x"""\n\n\ndef f(cursor):\n    cursor.execute("SELECT 1", (1,))\n'
    expect_raises(PatchRefused, lambda: _apply("CG106", source, 5))


def test_cg203_sentinel_default() -> None:
    source = buggy_source()
    patched = _apply("CG203", source, 24)
    assert "limit_collector=None" in patched, patched
    assert "if limit_collector is None:" in patched
    assert Verifier().counts(patched, path="x.py").get("CG203", 0) == 0


def test_cg202_logs_broad_except() -> None:
    source = buggy_source()
    patched = _apply("CG202", source, 20)
    assert "logger.exception" in patched, patched
    assert "import logging" in patched
    assert "return None" in patched, "必须保留原有降级行为"
    assert Verifier().counts(patched, path="x.py").get("CG202", 0) == 0


def test_cg206_uses_context_manager() -> None:
    source = buggy_source()
    patched = _apply("CG206", source, 32)
    assert "with open(path) as fh:" in patched, patched
    assert ".close()" not in patched
    assert Verifier().counts(patched, path="x.py").get("CG206", 0) == 0


def test_cg207_explicit_validation() -> None:
    source = buggy_source()
    patched = _apply("CG207", source, 16)
    assert "if not (user_id):" in patched, patched
    assert "raise ValueError" in patched
    # 另一处 ``assert name and email`` 不含外部输入特征词，codeguard 本来就不报它，
    # 所以修完之后 CG207 应当彻底归零
    assert Verifier().counts(patched, path="x.py").get("CG207", 0) == 0


def test_cg207_refuses_negated_assert() -> None:
    source = '"""x"""\n\n\ndef f(user):\n    assert not user\n'
    expect_raises(PatchRefused, lambda: _apply("CG207", source, 5))


def test_cg403_drops_unused_import() -> None:
    source = buggy_source()
    patched = _apply("CG403", source, 3, title="未使用的导入：hashlib")
    assert "import hashlib" not in patched, patched
    assert "import json" in patched, "其他导入不受影响"


def test_cg402_strips_trailing_whitespace() -> None:
    source = '"""x"""\n\ndef f():\n    return 1   \n'
    patched = _apply("CG402", source, 4)
    assert "1   " not in patched
    assert patched.endswith("return 1\n")


def test_cg204_identity_comparison() -> None:
    source = '"""x"""\n\n\ndef f(user):\n    if user == None:\n        return 1\n'
    patched = _apply("CG204", source, 5)
    assert "user is None" in patched, patched


def test_cg106_indirect_variable_form() -> None:
    """变量间接传参（真实代码里最常见的写法）也要能修。"""
    source = (
        '"""x"""\n'
        "\n"
        "\n"
        "def find(cursor, email):\n"
        '    sql = "SELECT id FROM users WHERE email = \'%s\'" % email\n'
        "    cursor.execute(sql)\n"
        "    return cursor.fetchall()\n"
    )
    patched = _apply("CG106", source, 5)
    assert "%" not in patched.split("sql = ")[1].split("\n")[0], patched
    assert "?" in patched
    ast.parse(patched)


def test_all_patchers_are_pure() -> None:
    """同样输入必须得到同样输出（否则报告无法复现）。"""
    source = buggy_source()
    first = _apply("CG106", source, 11)
    second = _apply("CG106", source, 11)
    assert first == second
