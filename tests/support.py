"""测试辅助：临时目录、样例源码、断言小工具。

样例里那个"硬编码密钥"是运行时拼出来的，源码文件里不出现连续的字面量。这样本仓库
被静态扫描时不会命中 CG105，测试夹具不该让工具对着自己报警。写进临时文件的样例里
还是完整字面量，规则照常命中。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]

#: 演示用假凭据（运行时拼接，避免本文件自身命中硬编码密钥规则）
_DEMO_TOKEN = "sk" + "-" + "live" + "-9f8a7b6c5d4e3f2a1b0c"


def _candidates() -> list[Path]:
    out: list[Path] = []
    for key in ("TMP", "TEMP", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            out.append(Path(value))
    out.append(Path(tempfile.gettempdir()))
    out.append(ROOT / ".testtmp")
    return out


@contextmanager
def tempdir(prefix: str = "test_") -> Iterator[Path]:
    created: Path | None = None
    for base in _candidates():
        try:
            base.mkdir(parents=True, exist_ok=True)
            created = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
            break
        except OSError:
            continue
    if created is None:  # pragma: no cover
        raise RuntimeError("找不到可写的临时目录")
    try:
        yield created
    finally:
        shutil.rmtree(created, ignore_errors=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def expect_raises(exc_type: type, func, *args, **kwargs) -> BaseException:
    try:
        func(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"期望 {exc_type.__name__}，实际抛出 {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"期望抛出 {exc_type.__name__}，但没有任何异常")


def buggy_source() -> str:
    """一份覆盖 7 种可自动修复规则的样例源码（结构与编码 Agent 的草案同构）。"""
    return (
        '"""示例：用户存储服务（故意保留典型问题）。"""\n'
        "\n"
        "import hashlib\n"
        "import json\n"
        "\n"
        f'AUTH_TOKEN = "{_DEMO_TOKEN}"\n'
        "\n"
        "\n"
        "def create_user(cursor, name, email, age):\n"
        '    assert name and email, "name 与 email 不能为空"\n'
        '    cursor.execute(f"INSERT INTO users (name, email, age) VALUES (\'{name}\', \'{email}\', {age})")\n'
        "    return cursor.lastrowid\n"
        "\n"
        "\n"
        "def get_user(cursor, user_id):\n"
        '    assert user_id, "user_id 不能为空"\n'
        "    try:\n"
        '        cursor.execute("SELECT id, name FROM users WHERE id = ?", (user_id,))\n'
        "        return cursor.fetchone()\n"
        "    except Exception:\n"
        "        return None\n"
        "\n"
        "\n"
        "def find_users(cursor, keyword, limit_collector=[]):\n"
        '    sql = "SELECT id, name FROM users WHERE name LIKE ?"\n'
        '    cursor.execute(sql, ("%" + keyword + "%",))\n'
        "    limit_collector.extend(cursor.fetchall())\n"
        "    return limit_collector\n"
        "\n"
        "\n"
        "def load_config(path):\n"
        "    fh = open(path)\n"
        "    data = fh.read()\n"
        "    fh.close()\n"
        "    return json.loads(data)\n"
    )


def trailing_whitespace_source() -> str:
    return '"""行尾空白样例。"""\n\n\ndef f():\n    return 1   \n'


def clean_source() -> str:
    return (
        '"""干净样例。"""\n'
        "\n"
        "import json\n"
        "\n"
        "\n"
        "def load_config(path: str) -> dict:\n"
        '    """读取 JSON 配置。"""\n'
        '    with open(path, encoding="utf-8") as handle:\n'
        "        return json.loads(handle.read())\n"
    )


def ensure_root_on_path() -> None:
    entry = str(ROOT)
    if entry not in sys.path:
        sys.path.insert(0, entry)
