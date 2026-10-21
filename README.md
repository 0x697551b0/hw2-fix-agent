# FixAgent —— 修复 Agent

> 按审查发现改写代码，**每一处改写都要过自检**才允许落地 —— 软件工程实践 Homework 2
>
> **源码仓库**：<https://github.com/0x697551b0/hw2-fix-agent>　**作者**：刘浩天 / 2412190219

FixAgent 是「编码 → 审查 → 修复」工作流的**第三个节点**。它读入审查 Agent 给出的结构化发现清单
（`FindingContract`），逐条改写工作区里的源码，然后对**每一次改写**做三级自检：能编译、目标规则
命中数确实下降、没有引入新的阻断级问题。三项全过才写盘，否则记为"拒绝改写"并写清理由 ——
一个会瞎改代码的修复 Agent 比一个不改的修复 Agent 危险得多。

| 项 | 值 |
| --- | --- |
| 版本 / Python | `1.0.0` / `>=3.10`（开发与验证环境 3.14.7）· 第三方依赖 **0 个** |
| 自动修复策略 | **9 条**（`python -m fix_agent strategies`）；明确转人工 **18 条** |
| 测试 | **59 个用例，全部离线**（`python run_tests.py`） |
| 主路径 | 规则级修补器 `engine=rules`：不访问网络、不需要 API Key |

## 在工作流里的位置

```
 ┌────────────────┐  FindingContract[]  ┌───────────────┐  PatchRecord[]   ┌────────────────┐
 │  审查 Agent    │ ──────────────────▶ │  修复 Agent   │ ───────────────▶ │  审查 Agent    │
 │ (review_agent) │  文件:行号/规则/级别 │  (本仓库)     │  已修/拒绝/暂缓  │  复审（第 2 轮）│
 └────────────────┘  fixable=true/false └───────┬───────┘  + unified diff  └────────────────┘
                       自检 Verifier：① compile() 能过　② 目标规则命中数下降　③ 阻断级命中数不上升
```

编排器不关心修复细节，只做三件事：把 `review.completed` 消息里的发现交给本 Agent、收下 `fix.applied`、
把下一轮审查交给审查 Agent。本仓库也能单独使用：`plan` 看计划、`apply` 真改代码。

## 目录

- [快速开始](#快速开始)　- [效果预览](#效果预览)　- [命令行用法](#命令行用法)　- [项目结构](#项目结构)
- [支持的 9 条修复策略](#支持的-9-条修复策略)　- [明确不支持自动修的规则及理由](#明确不支持自动修的规则及理由)
- [自检机制](#自检机制)　- [与上下游的契约](#与上下游的契约)　- [测试与自检](#测试与自检)　- [配置项](#配置项)　- [设计取舍](#设计取舍)　- [常见问题](#常见问题)　- [许可与相关仓库](#许可与相关仓库)

## 快速开始

Python **3.10+**；**不需要装任何第三方包**，**不需要联网、不需要 API Key**。

```bash
cd fix-agent
python -m fix_agent strategies   # 看 9 条策略（纯打印，不碰文件）
python -m fix_agent info         # 版本、环境、当前生效配置；python run_tests.py 跑 59 个用例
```

要让 Agent 真改代码，还需要一份审查结果 JSON。用仓库自带的测试夹具造一份可复现的输入（只写进被 `.gitignore` 忽略的 `.demo/`）：

```bash
python -c "from tests.support import buggy_source; import pathlib; p=pathlib.Path('.demo/worktree'); p.mkdir(parents=True, exist_ok=True); (p/'mod.py').write_text(buggy_source(), encoding='utf-8', newline='\n')"
python -c "from tests.test_agent import _findings; from fix_agent.protocol import dumps; import pathlib; pathlib.Path('.demo/review.json').write_text(dumps({'findings':[f.to_dict() for f in _findings()]}), encoding='utf-8')"

python -m fix_agent plan  .demo/worktree --findings .demo/review.json   # 只读，绝对安全
python -m fix_agent apply .demo/worktree --findings .demo/review.json --report-dir .demo/reports
```

`--findings` 接受审查 Agent 原样产出的报告（`{"findings":[...]}`），也接受裸 JSON 数组；`plan` 只读、`apply`
才写文件，两者都可用 `--dry-run` / `--json` 改变行为，`.demo/` 用完可直接删掉。

## 效果预览

`plan`（**不动任何文件**，只是一张"这批发现会怎么处理"的清单）：

```
处理计划（共 8 条发现）
════════════════════════════════════════════════════════════════════════
  🔧 LOW      CG403   `mod.py:3` → drop_unused_import
  🔧 CRITICAL CG105   `mod.py:6` → move_secret_to_env
  🔧 HIGH     CG106   `mod.py:11` → parameterize_sql
  🔧 MEDIUM   CG207   `mod.py:16` → explicit_validation
  🔧 MEDIUM   CG202   `mod.py:20` → log_broad_except
  🔧 HIGH     CG203   `mod.py:24` → sentinel_default
  🔧 MEDIUM   CG206   `mod.py:32` → context_manager
  ⏸  LOW      CG304   `mod.py:9` 转人工：文档字符串必须描述真实意图，机器生成注释没有价值
════════════════════════════════════════════════════════════════════════
可自动修复 7 条，转人工 1 条。（转人工的条目不会被静默丢弃，会写进修复报告。）
```

`apply`（真实输出，自检引擎用的是 Homework 1 的 codeguard）：

```
修复结果
════════════════════════════════════════════════════════════════════════
  修复 7 条问题，涉及 1 个文件；转人工 0 条，暂缓 1 条
  自检引擎：codeguard

已修复：
  ✅ CG206   `mod.py:32` 改写为 `with ... as fh`，异常路径下也能及时释放句柄
  ✅ CG203   `mod.py:24` 默认值 [] 改为 None 哨兵，函数体内按需初始化，避免跨调用状态污染
  ✅ CG202   `mod.py:20` 补充异常日志；**保留原降级返回值**（未擅自改为向上抛出，避免改变线上行为）
  ✅ CG207   `mod.py:16` 改用显式判断 + ValueError；`python -O` 下不再失效
  ✅ CG106   `mod.py:11` 改写为参数化查询，3 个值经驱动占位符传入
  ✅ CG105   `mod.py:6` 密钥改为从环境变量 AUTH_TOKEN 读取；**请同步轮换已写入版本库的旧凭据**
  ✅ CG403   `mod.py:3` 删除未使用的导入行 `hashlib`
```

补丁 diff（节选，完整内容在 `--report-dir` 的 `fix.diff` 里）：

```diff
--- a/mod.py
+++ b/mod.py
@@ -1,27 +1,34 @@
-import hashlib
 import json
+import logging
+import os
-AUTH_TOKEN = "sk-live-9f8a7b6c5d4e3f2a1b0c"
+logger = logging.getLogger(__name__)
+
+AUTH_TOKEN = os.environ["AUTH_TOKEN"]
-    cursor.execute(f"INSERT INTO users (name, email, age) VALUES ('{name}', '{email}', {age})")
+    cursor.execute("INSERT INTO users (name, email, age) VALUES (?, ?, ?)", (name, email, age,))
-    assert user_id, "user_id 不能为空"
+    if not (user_id):
+        raise ValueError('user_id 不能为空')
     except Exception:
+        logger.exception("get_user 中捕获未预期异常，已按原有降级逻辑继续", exc_info=True)
         return None
...
-    fh = open(path)
-    data = fh.read()
-    fh.close()
+    with open(path) as fh:
+        data = fh.read()
```

细节值得注意：条目顺序 `CG206 → CG203 → ... → CG403` **不是**发现清单顺序（同一文件内按行号从大到小处理）；`CG202` 只补日志、不擅自把 `return None` 改成 `raise`（那个返回值可能是有意的降级路径）。

## 命令行用法

| 子命令 | 作用 | 改文件 | 需要 API Key |
| --- | --- | --- | --- |
| `plan <目录>` | 只打印处理计划：每条发现走哪条策略 / 为什么转人工 | ❌ 绝不 | 不需要 |
| `apply <目录>` | 执行修复，逐条自检后写回 | ✅（`--dry-run` 除外） | 不需要（`--engine llm` 除外） |
| `strategies` / `info` | 打印 9 条策略对照表 / 版本、环境、当前配置 | ❌ | 不需要 |

`plan` / `apply` 共用参数（`--findings` 必填）：

| 参数 | 说明 |
| --- | --- |
| `<target>` / `--findings <path>` | 要修复的目录（发现里的 `file` 相对它）/ **必填**的审查结果 JSON |
| `--engine rules\|llm` | 修复引擎，默认 `rules` |
| `--only-rule <ID>` / `--skip-rule <ID>` / `--max-fixes <N>` | 只修 / 跳过指定规则（可重复）/ 最多处理多少条（默认 80） |
| `--codeguard-path <dir>` | codeguard 源码根目录（自检用），一般不需要 |
| `--json` / `--quiet` / `--verbose` | 输出格式与日志级别 |

`--findings` 支持 `{"findings":[...]}`、`{"payload":{"findings":[...]}}` 或裸数组。`apply` 额外支持
`--dry-run`（全流程跑一遍、自检照做、**不写盘**，`note` 追加"（试运行，未写盘）"）、`--report-dir <dir>`
（写 `fix.json` / `fix.diff`）、`--keep-backup`（写回前备份到同目录 `.fix_backup/<文件名>.orig`）；
`--engine llm` 还需 `--api-key`（或 `FIX_API_KEY`）。

### 退出码约定

| 退出码 | 含义 |
| --- | --- |
| `0` | 任务完成（**注意**："有发现被拒绝改写"也算完成，结果在报告里逐条交代） |
| `2` | 用法/配置/输入错误：找不到 findings 文件、JSON 无 `findings`、目标目录不存在、`engine=llm` 缺 Key |

`python -m fix_agent.worker`（子进程模式）在 Agent 返回 `status=failed` 时退出码为 `1`。

```bash
python -m fix_agent apply .demo/worktree --findings .demo/review.json --only-rule CG106   # 只修 SQL 注入
python -m fix_agent apply .demo/worktree --findings .demo/review.json --dry-run           # 先看 diff
python -m fix_agent apply .demo/worktree --findings .demo/review.json --json              # JSON 打到终端
```

> 落盘机器可读结果请用 `--report-dir <目录>`：Windows PowerShell 的 `>` 默认写 UTF-16，接 CI 容易被误判成编码问题。

## 项目结构

```
fix-agent/
├── fix_agent/
│   ├── agent.py      ★ FixAgent 主体：逐文件、逐条改写 + 行号重算
│   ├── patchers.py   ★ 9 条修补器 + 18 条"不能自动修"的理由表
│   ├── source.py     ★ 编辑原语：AST 位置 → 字符区间、行级对齐
│   ├── verifier.py   ★ 补丁自检（codeguard / 内置计数器两级引擎）
│   ├── protocol.py   ★ Agent 契约信封（四个仓库各存一份等价副本）
│   ├── models.py · locate.py · config.py · errors.py    模型 / 找 codeguard / 配置 / 异常体系
│   ├── cli.py        plan / apply / strategies / info      ·  worker.py  子进程单次 JSON 往返
│   └── llm.py · prompts.py    engine=llm 的极简 OpenAI 兼容客户端与 Prompt（urllib，零依赖）
├── tests/            59 个用例：test_agent / test_patchers / test_source / test_verifier
└── run_tests.py      零依赖测试运行器（没装 pytest 也能跑）· pyproject.toml · .env.example
```

## 支持的 9 条修复策略

| 规则 | 策略名 | 做什么 | 为什么这件事可以安全地自动做 |
| --- | --- | --- | --- |
| `CG105` | `move_secret_to_env` | 字面量密钥 → `os.environ["TOKEN"]`，并补 `import os` | 密钥搬到环境变量语义等价且明确更安全；只处理"右边是字符串字面量"的简单赋值 |
| `CG106` | `parameterize_sql` | f-string / `%` 拼的 SQL → 占位符 + 参数元组 | 参数化是**等价改写**：值没变，只把字符串拼接换成驱动占位符 |
| `CG202` | `log_broad_except` | 宽泛 `except` 静默忽略 → 先 `logger.exception(...)` 再维持原逻辑 | 只**增加**一行日志、不动控制流；刻意不加 `raise` |
| `CG203` | `sentinel_default` | `def f(x=[])` → `x=None` + 函数体内初始化 | Python 官方推荐写法，对外签名兼容，只在函数体补两行 |
| `CG204` | `identity_comparison` | `x == None` → `x is None` | 单例比较语义确定，改写后严格更正确（避免重载 `__eq__` 的歧义） |
| `CG206` | `context_manager` | `open(); ...; close()` → `with open(p) as fh:` | 同一语句块内找到配对 `close()` 且之后不再用该变量时，改写是等价的 |
| `CG207` | `explicit_validation` | `assert cond, msg` → `if not (cond): raise ValueError(msg)` | `assert` 在 `python -O` 下会被优化掉；改写后语义只会更严格 |
| `CG402` | `strip_trailing_whitespace` | 删行尾空白 | 纯空白字符，不改变语义、不改变缩进深度 |
| `CG403` | `drop_unused_import` | 未使用的 import → 删行或从多名称导入中移除 | "未被引用"可在 AST 层面判定，删掉不可能改变运行时行为 |

查看这张表的命令是 `python -m fix_agent strategies`。每条改写都带 `note`（改了什么、人还要做什么）与
`before`/`after` 片段，写进 `PatchRecord`，`--report-dir` 时落到 `fix.json`。

## 明确不支持自动修的规则及理由

这是本仓库最想让人看见的一张表：**哪些问题我们拒绝自动修，为什么**（`patchers.py::NOT_SUPPORTED_REASONS`，
`plan` 会把每条的原文打印出来）。

| 规则 | 为什么不自动修 | 规则 | 为什么不自动修 |
| --- | --- | --- | --- |
| `CG101` | 动态执行是否必需属业务决策，需人工判断 | `CG301` | 函数过长需要重新划分职责，属设计决策 |
| `CG102` | 命令注入修复涉及调用方参数契约，需重新设计 | `CG302` | 嵌套过深需要提取函数，会改变模块结构 |
| `CG103` | 反序列化格式变更影响存量数据兼容性，需评估迁移 | `CG303` | 参数过多需引入聚合对象，会改变对外签名 |
| `CG104` | 关闭 TLS 校验有时是内网自签证书的权宜之计 | `CG304` | 文档字符串必须描述真实意图，机器生成注释没有价值 |
| `CG107` | 随机数用途（令牌/盐/采样）只有作者知道 | `CG305` | 提取常量的命名与归属需要领域知识 |
| `CG108` | 临时文件竞态需确认并发模型与清理策略 | `CG307` | 返回值类型需作者确认，猜错注解比没有注解更糟 |
| `CG201` | 裸 `except` 想吞掉什么异常只有作者清楚 | `CG401` | 折行位置影响可读性，需人工斟酌 |
| `CG205` | 除数为 0 通常说明上游数据有问题，应在源头修 | `CG405` | 语法错误需作者修复，机器无法推断原本想写什么 |
| `CG208` | `finally` 里的 `return` 改变控制流，属结构性改动 | `CG000` | 元信息提示，不是代码问题 |

这些发现的状态是 **`deferred`（暂缓，转人工）**，不是"被忽略"：
`test_unfixable_finding_is_deferred_not_dropped` 断言"暂缓必须给出理由"，
`test_unsupported_reasons_documented` 断言每个理由都非空。另有三类在运行时被**拒绝**（状态
`rejected`）：文件不存在或路径不在工作区内；目标文件不是 `.py`（`EDITABLE_SUFFIXES = {".py"}`，
其他后缀记 `deferred`）；修补器自己判断"形态拿不准" —— 每个修补器都有显式 `PatchRefused` 分支（例如
`CG106` 遇到"已经传了参"、`CG207` 遇到 `assert not x` 容易把逻辑写反、`CG206` 找不到配对的 `close()`）。

## 自检机制

一次改写要同时满足三条才算通过（`verifier.py::Verifier.check`）：

| # | 标准 | 不满足时 |
| --- | --- | --- |
| ① | 改写后的源码 `compile()` 能过 | 拒绝，`note` 带上语法错误行号 |
| ② | 目标规则命中数**严格下降** | 拒绝，`note` 写明 `之前 → 之后` 的次数 |
| ③ | 阻断级（默认 `critical`+`high`）命中数**没有上升** | 拒绝，`note` 列出新增的阻断级规则 |

```
发现 ─▶ patcher.apply() ─▶ 候选源码 ─▶ ① compile() ─▶ ② 目标规则↓ ─▶ ③ 阻断级不↑ ─▶ 写盘
             └── PatchRefused / 任一条不过 ───────────────────────────────▶ rejected（附理由）
```

两级引擎：找得到 codeguard 时用它做**权威判定**（报告写 `自检引擎：codeguard`）；找不到时退化为内置
AST 计数器（只覆盖本 Agent 会改写的 9 条规则），并显式标注"降级模式"、`fix.json` 的
`verification.degraded` 为 `true`。两种引擎在同一夹具上的结论必须一致（这是契约测试）。自检发生在
**每次改写之后**而不是最后统一检查：某条策略一旦写错，错误会立刻被拦下并降级成"拒绝改写"。

## 与上下游的契约

本仓库与编排器、审查 Agent 只共享一个 JSON 信封（`fix_agent/protocol.py`，四个仓库各存一份**等价副本**
—— 刻意的重复，为了让每个仓库都能单独 clone、单独跑通）。

请求（编排器 → 本 Agent，`AgentRequest`）：

```json
{"contract_version": "1.0", "agent": "fix", "task_id": "fix-r1", "round": 1,
 "objective": "按审查发现逐条改写代码，并在每处改写后自检",
 "payload": {"target": "<运行目录>/worktree", "report_dir": "<运行目录>/reports/fix-r1", "dry_run": false,
   "findings": [{"finding_id": "F2", "rule_id": "CG105", "severity": "critical", "file": "user_store.py",
     "line": 8, "title": "疑似硬编码密钥：AUTH_TOKEN", "fixable": true, "status": "open"}]},
 "shared": {"gate": {"passed": false}, "current_round": 1}, "metadata": {"blocking": ["critical", "high"]}}
```

响应（本 Agent → 编排器，`AgentResponse`，子进程模式跑出来的真实片段）：

```json
{"agent": "fix", "status": "ok", "summary": "修复 1 条问题，涉及 1 个文件；转人工 0 条，暂缓 0 条",
 "payload": {"engine": "rules", "rejected": [], "deferred": [], "files_changed": ["mod.py"],
   "applied": [{"finding_id": "F2", "rule_id": "CG105", "file": "mod.py", "line": 6, "status": "applied",
     "strategy": "move_secret_to_env", "verified": true,
     "note": "密钥改为从环境变量 AUTH_TOKEN 读取；**请同步轮换已写入版本库的旧凭据**"}],
   "diff": "--- a/mod.py\n+++ b/mod.py\n@@ -2,8 +2,9 @@\n ...",
   "verification": {"enabled": true, "engine": "codeguard", "degraded": false,
                    "files": [{"path": "mod.py", "cleared": ["CG105"], "increased": []}]}},
 "metrics": {"elapsed_seconds": 0.009, "applied": 1, "files_changed": 1}, "error": ""}
```
单独验证跨进程路径：先写一个同上的请求文件，再执行
`python -m fix_agent.worker --request-file req.json --response-file resp.json`。`status` 只会是
`ok` / `failed`；`failed` 时 `error` 必非空、退出码为 `1`。`payload` 里**每条发现都有且只有一条
`PatchRecord`**（`applied` / `rejected` / `deferred` / `skipped`），所以"哪条被处理、哪条没有、为什么"可逐条追溯。

## 测试与自检

```bash
python run_tests.py           # 59 个用例（-v 列出用例名，patchers 只跑该文件）；pytest -q 亦可
```

真实输出结尾一行：`结果：59/59 通过，全部通过`（Python 3.14.7）。全部测试**离线**：不联网、不需要
API Key、不依赖另外三个仓库；`Verifier` 尽力去找 codeguard，找不到就走内置计数器 —— 用例对两种引擎
都成立。

| 文件 | 用例 | 守住什么 |
| --- | --- | --- |
| `test_agent.py` / `test_patchers.py` | 15 / 19 | 端到端闭环：7 条全修干净、不可修的转人工而非丢弃、`--dry-run` 不碰磁盘、越界路径被拒、只修/跳过/上限过滤、diff 生成、协议往返、**行号漂移回归** / 每个策略"改对了"（用 `Verifier` 判定）+ "该拒绝时拒绝" + 修补器是纯函数 |
| `test_source.py` / `test_verifier.py` | 14 / 11 | 倒序应用、区间重叠、**中文源码的字节列偏移**、行级对齐 / 三种拒绝路径、降级说明、两引擎一致性 |

## 配置项

优先级：**命令行参数 > 环境变量 > `.env` > 默认值**；前缀 `FIX_`，完整示例见 `.env.example`，当前生效值用 `python -m fix_agent info` 查看。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FIX_ENGINE` / `FIX_BLOCKING` | `rules` / `critical,high` | 引擎（`rules` / `llm`）/ 阻断级别（**必须与编排器、审查 Agent 一致**） |
| `FIX_MAX_FIXES` / `FIX_ONLY_RULES` / `FIX_SKIP_RULES` / `FIX_VERIFY` / `FIX_DRY_RUN` / `FIX_KEEP_BACKUP` | `80` / 空 / 空 / `1` / `0` / `0` | 单次处理上限 / 只处理规则 / 跳过规则 / 规则级自检 / 试运行 / 写回前备份到 `.fix_backup/` |
| `FIX_WORKSPACE_ROOT` / `FIX_CODEGUARD_PATH` / `HW2_PACKAGES_HOME` | 空 | 相对 `target` 的基准目录 / codeguard 源码根目录 / 额外包搜索根（`os.pathsep` 分隔） |
| `FIX_API_KEY` / `FIX_MODEL` / `FIX_BASE_URL` | 空 / `deepseek-chat` / `https://api.deepseek.com` | `engine=llm` 用；配 `FIX_TEMPERATURE`(0.1) / `FIX_MAX_TOKENS`(8000) / `FIX_TIMEOUT`(120s) |

> `FIX_BLOCKING` 与审查 Agent 同名同义且**必须一致**：本 Agent 用它判断"我这次的改写有没有引入新的
> 阻断级问题"。口径不一致会出现"修复觉得安全、审查觉得不合格"的来回拉锯，所以编排器把同一个值
> 同时发给两个 Agent。

## 设计取舍

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 改写方式 | **AST 定位 + 字符区间替换**，不做整文件格式化重写 | 不动无关代码，diff 最小、可人工复核；也不必引入 `libcst` / `rope` |
| 拿不准时 / 依赖 | **`PatchRefused`（拒绝）而不是猜**；**零第三方依赖** | 自动改错代码的代价远高于留给人；`clone` 下来就能跑，`engine=llm` 连 HTTP 都用标准库 |
| 自检位置与权威 | **每次改写之后**立即校验；codeguard 优先、内置计数器降级 | 错误立刻拦下而不是攒到最后；有 Homework 1 的规则引擎就用它的口径，没有也能工作但会明说"降级了" |
| 行号处理 / 报告行号 | 倒序 + `difflib` 行级对齐重算；报告统一用**审查报告里的原始行号** | 倒序只解决一半问题（模块级 import 插入会把后面整体顶下去）；读者会拿报告行号去当前文件定位，用"处理时的新行号"会错位 |
| 引擎分工 | 主路径规则级，LLM 是可选增强 | 规则级确定、可复现、快；LLM 只在规则覆盖不到的形态上有价值，且必须过同一套自检 |

## 常见问题

**Q：一定要有 codeguard 才能跑吗？能修 Java / JavaScript 吗？** 不用 —— 找不到 codeguard 时自检降级为
内置 AST 计数器（只覆盖 9 条规则）并在报告里明确标注；想与审查侧口径完全一致就把 codeguard 放同
目录，或用 `--codeguard-path ../codeguard`。也**不能修其他语言**：`EDITABLE_SUFFIXES` 只有 `.py`，
其他后缀记 `deferred` 并说明原因（所有策略都建在 Python `ast` 上，换语言等于重写）。

**Q：为什么不给我把 `except Exception: return None` 改成 `raise`？`fix.json` 里 `after` 为什么可能是
空的？** 前者会改变线上行为，原来的返回值可能是刻意的降级路径 —— 本 Agent 只补日志，"要不要改成
抛出"是设计决策。后者是因为 `before`/`after` 是"同一行号的片段"：当策略在文件顶部插入 `import os`
或 `logger = ...` 时，原行号的内容会整体下移，那一行可能变成空行，**以 `diff` 为准**。

**Q：会不会改到工作区之外的文件？`--dry-run` 与 `--json` 能一起用吗？** 不会：`_safe_path` 校验解析后
的路径是否仍在 `target` 内，不满足的直接记 `rejected`（`test_write_outside_root_rejected` 守着这条）；
`--dry-run` 与 `--json` 也可并用（前者决定"写不写盘"，后者只决定输出格式）。

## 许可与相关仓库

本项目为课程作业，采用 MIT 许可。

| 仓库 | 角色 | 地址 |
| --- | --- | --- |
| Homework 1 | codeguard：代码审查 Agent（本仓库自检的权威规则引擎） | <https://github.com/0x697551b0/codeguard> |
| Homework 2 · 1 | 编码 Agent（`coding_agent`） | <https://github.com/0x697551b0/hw2-coding-agent> |
| Homework 2 · 2 | 审查 Agent（`review_agent`，复用 codeguard） | <https://github.com/0x697551b0/hw2-review-agent> |
| Homework 2 · 3 | **修复 Agent（本仓库，`fix_agent`）** | <https://github.com/0x697551b0/hw2-fix-agent> |
| Homework 2 · 4 | 编排器（`workflow`） | <https://github.com/0x697551b0/hw2-code-review-workflow> |

相关文档：[`Design.md`](Design.md) —— 架构、自证机制、行号漂移与真实踩坑记录。
