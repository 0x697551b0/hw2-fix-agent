"""自检器（verifier.py）的测试。

自检是修复 Agent 的"刹车"：改错了必须能被拦下。这组用例同时覆盖两种引擎
（codeguard / 内置计数器），因为降级路径同样会被真实用户走到。
"""

from __future__ import annotations

from fix_agent.verifier import BUILTIN_RULES, Verifier

from tests.support import buggy_source, clean_source, trailing_whitespace_source


def test_codeguard_engine_available() -> None:
    verifier = Verifier()
    assert verifier.engine in {"codeguard", "builtin"}
    if verifier.engine == "builtin":  # pragma: no cover - 环境缺 codeguard 时
        print("（提示：未找到 codeguard，本次自检走内置降级模式）")


def test_counts_detect_seeded_rules() -> None:
    verifier = Verifier()
    counts = verifier.counts(buggy_source(), path="x.py")
    for rule in ("CG105", "CG106", "CG203", "CG206", "CG207", "CG403"):
        assert counts.get(rule, 0) >= 1, f"{rule} 应被检出，实际：{counts}"


def test_clean_code_has_no_blocking_rules() -> None:
    verifier = Verifier()
    counts = verifier.counts(clean_source(), path="x.py")
    for rule in ("CG105", "CG106", "CG202", "CG203", "CG206", "CG207"):
        assert counts.get(rule, 0) == 0, f"{rule} 不应命中干净代码"


def test_syntax_error_detected() -> None:
    verifier = Verifier()
    counts = verifier.counts("def broken(:\n", path="x.py")
    assert counts.get("CG405", 0) == 1


def test_check_rejects_uncompilable_candidate() -> None:
    verifier = Verifier()
    before = verifier.counts(buggy_source(), path="x.py")
    result = verifier.check(
        "def broken(:\n", path="x.py", rule_id="CG106", before=before
    )
    assert not result.verified
    assert "编译" in result.reason


def test_check_rejects_when_rule_not_reduced() -> None:
    """候选源码和原来一样 → 自检必须拒绝（说明改写没生效）。"""
    verifier = Verifier()
    source = buggy_source()
    before = verifier.counts(source, path="x.py")
    result = verifier.check(source, path="x.py", rule_id="CG106", before=before)
    assert not result.verified
    assert "没有下降" in result.reason


def test_check_accepts_real_improvement() -> None:
    verifier = Verifier()
    source = buggy_source()
    before = verifier.counts(source, path="x.py")
    improved = source.replace("    fh = open(path)\n", "    fh = open(path, encoding='utf-8')\n")
    # 这处替换不消除 CG206（仍然没用 with），所以应当被拒绝
    rejected = verifier.check(improved, path="x.py", rule_id="CG206", before=before)
    assert not rejected.verified


def test_check_detects_new_blocking_issue() -> None:
    """改写引入新的阻断级问题时要拒绝。"""
    verifier = Verifier()
    source = clean_source()
    before = verifier.counts(source, path="x.py")
    worse = source.replace(
        "import json", 'import json\n\n\ndef leak(cursor, user_id):\n    cursor.execute(f"SELECT {user_id}")\n'
    )
    result = verifier.check(worse, path="x.py", rule_id="CG304", before=before)
    assert not result.verified
    assert result.new_blocking or "没有下降" in result.reason


def test_summarize_reports_degradation() -> None:
    verifier = Verifier()
    summary = verifier.summarize([])
    assert summary["engine"] in {"codeguard", "builtin"}
    assert summary["degraded"] is (verifier.engine != "codeguard")
    assert set(BUILTIN_RULES) >= {"CG105", "CG106", "CG207"}


def test_builtin_counter_matches_codeguard_on_fixture() -> None:
    """内置计数器与 codeguard 在同一份样例上的结论应一致（契约测试）。"""
    with_codeguard = Verifier()
    if with_codeguard.codeguard is None:  # pragma: no cover
        return
    builtin = Verifier(codeguard=None)
    builtin.codeguard = None
    source = buggy_source()
    a = with_codeguard.counts(source, path="x.py")
    b = builtin.counts(source, path="x.py")
    for rule in BUILTIN_RULES:
        assert a.get(rule, 0) == b.get(rule, 0), f"{rule} 两种引擎结论不一致：{a.get(rule)} vs {b.get(rule)}"


def test_trailing_whitespace_counted() -> None:
    verifier = Verifier()
    counts = verifier.counts(trailing_whitespace_source(), path="x.py")
    assert counts.get("CG402", 0) >= 1
