"""修复 Agent 配置。环境变量前缀 ``FIX_``。

``blocking`` 和审查 Agent 同名同义，必须跟审查侧保持一致：本 Agent 拿它判断
"这次改写有没有引入新的阻断级问题"。两边口径不一样就会出现"修复觉得安全、
审查觉得不合格"的来回拉锯，所以编排器把同一个值同时发给两个 Agent。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from fix_agent.errors import ConfigError
from fix_agent.protocol import SEVERITY_ORDER

ENV_PREFIX = "FIX_"

#: 修复引擎：``rules`` 规则级修补器；``llm`` 模型整文件重写（仍要过自检）
ENGINES: tuple[str, ...] = ("rules", "llm")


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """极简 ``.env`` 解析（四个仓库共用同一实现，便于互相阅读）。"""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key:
            continue
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


@dataclass
class FixConfig:
    """修复 Agent 运行期配置。"""

    # ---- 策略 ----
    engine: str = "rules"
    blocking: tuple[str, ...] = ("critical", "high")
    #: 单次最多处理多少条发现（防止一个文件里上百条低级别问题刷屏）
    max_fixes: int = 80
    #: 只处理这些规则（空表示全部）
    only_rules: tuple[str, ...] = ()
    #: 跳过这些规则
    skip_rules: tuple[str, ...] = ()
    #: 只试运行，不写文件
    dry_run: bool = False
    #: 是否做规则级自检（False 时只做语法检查，不建议关闭）
    verify: bool = True
    #: 写文件前是否备份原文件到 ``.fix_backup/``
    keep_backup: bool = False

    # ---- 输出 ----
    workspace_root: str = ""
    codeguard_path: str = ""

    # ---- LLM（engine=llm 时使用）----
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.1
    max_tokens: int = 8000
    timeout: float = 120.0

    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = ".env", **overrides: Any) -> "FixConfig":
        if dotenv_path is not None:
            load_dotenv(dotenv_path)
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            env_name = f"{ENV_PREFIX}{f.name.upper()}"
            if env_name not in os.environ:
                continue
            kwargs[f.name] = _coerce(os.environ[env_name], f.type, f.name)
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)

    def validate(self) -> None:
        if self.engine not in ENGINES:
            raise ConfigError(f"engine 只能取 {ENGINES}，当前为 {self.engine!r}")
        unknown = [s for s in self.blocking if s not in SEVERITY_ORDER]
        if unknown:
            raise ConfigError(f"blocking 含有未知级别 {unknown}")
        if self.max_fixes < 1:
            raise ConfigError("max_fixes 至少为 1")
        if self.engine == "llm" and not self.api_key.strip():
            raise ConfigError(
                "engine=llm 需要 API Key。任选一种方式配置：\n"
                f"  1) 环境变量 {ENV_PREFIX}API_KEY=sk-xxxx\n"
                "  2) 命令行 --api-key sk-xxxx\n"
                "  3) 没有 Key：用默认的 --engine rules 规则级修补器"
            )

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace_root).resolve() if self.workspace_root else Path.cwd().resolve()

    def describe(self) -> str:
        masked = "未配置"
        if self.engine == "llm":
            masked = (
                f"{self.api_key[:8]}...{self.api_key[-4:]}" if len(self.api_key) > 16 else "***"
            )
        else:
            masked = "规则级修补器（不访问网络）"
        return (
            f"engine={self.engine}  blocking={','.join(self.blocking)}  "
            f"max_fixes={self.max_fixes}  verify={self.verify}  dry_run={self.dry_run}  "
            f"api_key={masked}"
        )


def _coerce(raw: str, type_hint: Any, name: str) -> Any:
    hint = type_hint if isinstance(type_hint, str) else getattr(type_hint, "__name__", "str")
    text = raw.strip()
    if hint == "bool":
        return text.lower() in {"1", "true", "yes", "y", "on"}
    if hint == "int":
        try:
            return int(text)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {ENV_PREFIX}{name.upper()} 应为整数，实际为 {raw!r}") from exc
    if hint == "float":
        try:
            return float(text)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {ENV_PREFIX}{name.upper()} 应为小数，实际为 {raw!r}") from exc
    if hint.startswith("tuple"):
        return tuple(part.strip() for part in text.split(",") if part.strip())
    return text
