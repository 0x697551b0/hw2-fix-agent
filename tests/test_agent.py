"""FixAgent 的端到端测试。

跑的是整条链路：发现清单 → 逐条改写 → 自检 → 落盘 → 复验。
其中 ``test_all_blocking_findings_cleared`` 是最要紧的一条，它要求修完之后文件里
不能再有任何可自动修复的规则命中，我拿这个当"闭环"的定义。
"""

from __future__ import annotations

from fix_agent.agent import FixAgent
from fix_agent.config import FixConfig
from fix_agent.errors import TargetError
from fix_agent.protocol import AgentRequest, FindingContract
from fix_agent.verifier import Verifier

from tests.support import buggy_source, expect_raises, read, tempdir, write


def _findings() -> list[FindingContract]:
    """与样例源码行号对应的发现清单（结构等同于审查 Agent 的输出）。"""
    return [
        FindingContract(finding_id="F1", rule_id="CG403", severity="low", file="mod.py", line=3,
                        title="未使用的导入：hashlib", fixable=True),
        FindingContract(finding_id="F2", rule_id="CG105", severity="critical", file="mod.py", line=6,
                        title="疑似硬编码密钥：AUTH_TOKEN", fixable=True),
        FindingContract(finding_id="F3", rule_id="CG106", severity="high", file="mod.py", line=11,
                        title="SQL 语句由拼接/格式化构造", fixable=True),
        FindingContract(finding_id="F4", rule_id="CG207", severity="medium", file="mod.py", line=16,
                        title="使用 assert 校验外部输入", fixable=True),
        FindingContract(finding_id="F5", rule_id="CG202", severity="medium", file="mod.py", line=20,
                        title="捕获 Exception 后静默忽略", fixable=True),
        FindingContract(finding_id="F6", rule_id="CG203", severity="high", file="mod.py", line=24,
                        title="可变默认参数：find_users()", fixable=True),
        FindingContract(finding_id="F7", rule_id="CG206", severity="medium", file="mod.py", line=32,
                        title="open() 未使用 with 上下文管理器", fixable=True),
        FindingContract(finding_id="F8", rule_id="CG304", severity="low", file="mod.py", line=9,
                        title="缺少文档字符串：create_user", fixable=False),
    ]


def _agent(**overrides) -> FixAgent:
    config = FixConfig(engine="rules")
    for key, value in overrides.items():
        setattr(config, key, value)
    return FixAgent(config)


def test_all_blocking_findings_cleared() -> None:
    with tempdir() as tmp:
        target = write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings())
        patched = read(target)
    assert len(outcome.applied) == 7, [r.rule_id for r in outcome.applied]
    remaining = Verifier().counts(patched, path="mod.py")
    for rule in ("CG105", "CG106", "CG202", "CG203", "CG206", "CG207", "CG403"):
        assert remaining.get(rule, 0) == 0, f"{rule} 仍命中：{remaining}"
    compile(patched, "mod.py", "exec")


def test_unfixable_finding_is_deferred_not_dropped() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings())
    deferred = {r.rule_id for r in outcome.deferred}
    assert "CG304" in deferred, deferred
    assert all(r.note for r in outcome.deferred), "暂缓必须给出理由"


def test_dry_run_does_not_touch_disk() -> None:
    with tempdir() as tmp:
        target = write(tmp / "mod.py", buggy_source())
        before = read(target)
        outcome = _agent().fix(tmp, _findings(), dry_run=True)
        assert read(target) == before, "试运行不应写盘"
    assert outcome.applied, "试运行仍应报告'将会修什么'"
    assert "试运行" in outcome.applied[0].note


def test_missing_file_is_rejected_not_created() -> None:
    findings = [
        FindingContract(finding_id="Fx", rule_id="CG106", severity="high", file="nope.py", line=1)
    ]
    with tempdir() as tmp:
        outcome = _agent().fix(tmp, findings)
    assert not outcome.applied
    assert outcome.rejected and "不存在" in outcome.rejected[0].note


def test_write_outside_root_rejected() -> None:
    findings = [
        FindingContract(finding_id="Fx", rule_id="CG106", severity="high", file="../escape.py", line=1)
    ]
    with tempdir() as tmp:
        write(tmp.parent / "escape.py", "x = 1\n")
        outcome = _agent().fix(tmp / ".", findings)
    assert not outcome.applied


def test_only_rules_filter() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings(), only_rules=["CG207"])
    assert {r.rule_id for r in outcome.applied} == {"CG207"}
    assert len(outcome.records) == 1


def test_skip_rules_filter() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings(), skip_rules=["CG105"])
    assert "CG105" not in {r.rule_id for r in outcome.applied}


def test_max_fixes_caps_work() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings(), max_fixes=2)
    assert len(outcome.records) == 2


def test_diff_is_generated() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings())
    assert outcome.diff.startswith("--- a/mod.py"), outcome.diff[:80]
    assert "+++ b/mod.py" in outcome.diff


def test_verification_summary_present() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings())
    verification = outcome.verification
    assert verification["enabled"] is True
    assert verification["engine"] in {"codeguard", "builtin"}
    assert verification["files"], verification


def test_handle_roundtrip() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        request = AgentRequest(
            agent="fix",
            task_id="fix-r1",
            payload={"target": str(tmp), "findings": [f.to_dict() for f in _findings()]},
        )
        response = _agent().handle(request)
    assert response.ok, response.error
    assert response.payload["applied"]
    assert response.payload["files_changed"] == ["mod.py"]


def test_handle_with_no_findings_is_ok() -> None:
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        request = AgentRequest(agent="fix", task_id="t", payload={"target": str(tmp), "findings": []})
        response = _agent().handle(request)
    assert response.ok
    assert response.metrics["applied"] == 0


def test_handle_reports_bad_payload() -> None:
    with tempdir() as tmp:
        request = AgentRequest(agent="fix", task_id="t", payload={})
        response = _agent().handle(request)
    assert not response.ok
    assert "target" in response.error


def test_missing_target_raises() -> None:
    expect_raises(TargetError, _agent().fix, "/definitely/not/here", [])


def test_line_shift_does_not_break_later_findings() -> None:
    """回归测试：前面的补丁插入行后，后续发现的行号必须被换算。

    这是本项目踩到过最贵的一个 bug：修补器不会报错，它会在错误的行上"成功"地改掉
    无关代码。CG202 会在文件顶部插入 import logging，把后面所有行号顶下去；如果
    没重算，CG105（原本第 6 行）就会对不上。
    """
    with tempdir() as tmp:
        write(tmp / "mod.py", buggy_source())
        outcome = _agent().fix(tmp, _findings())
    applied_rules = {r.rule_id for r in outcome.applied}
    assert {"CG202", "CG105", "CG106"} <= applied_rules, applied_rules
    assert not outcome.rejected, [r.note for r in outcome.rejected]
