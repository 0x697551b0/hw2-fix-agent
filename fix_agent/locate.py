"""
定位 codeguard（**可选**依赖）。

与审查 Agent 不同，修复 Agent 对 codeguard 是"有则更好"：有它就能做权威的规则级自检，
没有就降级为内置计数器。因此这里的查找全程"尽力而为"，找不到也不抛异常。

之所以在本仓库再写一份而不是 import 审查 Agent 的 ``locate``：三个 Agent 仓库必须能
各自单独 clone、单独测试，不能因为"复用 60 行找包代码"而让仓库 3 依赖仓库 2。
重复的部分刻意保持在最小（找包 + 加 sys.path），真正复杂的逻辑各自不同。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

#: 环境变量：额外的包搜索根目录（用 os.pathsep 分隔）
ENV_PACKAGES_HOME = "HW2_PACKAGES_HOME"

#: 向上搜索的最大层数
_MAX_ANCESTORS = 5


def ensure_codeguard(*, explicit: str | Path | None = None) -> ModuleType | None:
    """返回 codeguard 模块；找不到返回 ``None``（不抛异常）。"""
    return ensure_package("codeguard", explicit=explicit)


def ensure_package(
    name: str, *, explicit: str | Path | None = None
) -> ModuleType | None:
    """确保 ``name`` 包可导入并返回模块；找不到返回 ``None``。"""
    module = _try_import(name)
    if module is not None:
        return module
    for root in _candidate_roots(explicit):
        if _has_package(root, name):
            _prepend_sys_path(root)
            module = _try_import(name)
            if module is not None:
                return module
    return None


def find_package_root(name: str, *, explicit: str | Path | None = None) -> Path | None:
    """只做定位，不 import。"""
    for root in _candidate_roots(explicit):
        if _has_package(root, name):
            return root
    return None


def describe() -> dict[str, object]:
    """诊断信息。"""
    module = ensure_codeguard()
    if module is None:
        return {"available": False, "package": "codeguard"}
    return {
        "available": True,
        "package": "codeguard",
        "version": getattr(module, "__version__", "unknown"),
        "file": getattr(module, "__file__", ""),
    }


# 内部实现
def _try_import(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - 导入失败统一当作"没找到"
        return None


def _has_package(root: Path, name: str) -> bool:
    try:
        return (root / name / "__init__.py").is_file()
    except OSError:  # pragma: no cover
        return False


def _prepend_sys_path(root: Path) -> None:
    entry = str(root.resolve())
    sys.modules.pop("codeguard", None)
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
    importlib.invalidate_caches()


def _candidate_roots(explicit: str | Path | None) -> list[Path]:
    roots: list[Path] = []

    def add(candidate: Path | str | None) -> None:
        if candidate is None:
            return
        try:
            resolved = Path(candidate).expanduser().resolve()
        except OSError:  # pragma: no cover
            return
        if resolved not in roots:
            roots.append(resolved)

    if explicit:
        add(Path(explicit))
        add(Path(explicit).parent)

    for entry in (os.environ.get(ENV_PACKAGES_HOME) or "").split(os.pathsep):
        if entry.strip():
            add(entry.strip())

    _walk_up(Path.cwd(), add)
    _walk_up(Path(__file__).resolve().parents[1], add)
    return roots


def _walk_up(start: Path, add) -> None:
    node = start
    for _ in range(_MAX_ANCESTORS):
        add(node)
        try:
            for child in node.iterdir():
                if child.is_dir():
                    add(child)
        except OSError:  # pragma: no cover
            pass
        if node.parent == node:
            break
        node = node.parent


__all__ = [
    "ENV_PACKAGES_HOME",
    "describe",
    "ensure_codeguard",
    "ensure_package",
    "find_package_root",
]
