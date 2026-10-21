# FixAgent —— 修复 Agent

> 软件工程实践 Homework 2 ·「编码 → 审查 → 修复」工作流的第三个节点
>
> 源码仓库：<https://github.com/0x697551b0/hw2-fix-agent>　作者：刘浩天 / 2412190219

FixAgent 读入审查 Agent 给出的结构化发现清单（`FindingContract`），逐条改写工作区里的
源码。每一处改写都要过三级自检：能 `compile()`、目标规则命中数确实下降、没有引入新的
阻断级问题。三条全过才写盘，否则记为"拒绝改写"并写清理由。

之所以卡得这么严，是因为改错代码的代价比不改大得多。拿不准的时候一律抛 `PatchRefused`
转人工，报告里逐条写明为什么不动它。

| 项 | 值 |
| --- | --- |
| 版本 / Python | `1.0.0` / `>=3.10`（开发与验证环境 3.14.7）· 第三方依赖 0 个 |
| 自动修复策略 | 9 条（`python -m fix_agent strategies`） |
| 明确转人工的规则 | 18 条（`patchers.py::NOT_SUPPORTED_REASONS`） |
| 测试 | 59 个用例，全部离线（`python run_tests.py`） |
| 主路径 | 规则级修补器 `engine=rules`：不访问网络、不需要 API Key |

## 在工作流里的位置

```
 ┌────────────────┐  FindingContract[]  ┌───────────────┐  PatchRecord[]   ┌────────────────┐
 │  审查 Agent    │ ──────────────────▶ │  修复 Agent   │ ───────────────▶ │  审查 Agent    │
 │ (review_agent) │  文件:行号/规则/级别 │  (本仓库)     │  已修/拒绝/暂缓  │  复审（第 2 轮）│
 └────────────────┘  fixable=true/false └───────┬───────┘  + unified diff  └────────────────┘
                        自检 Verifier：① compile() 能过　② 目标规则命中数下降　③ 阻断级命中数不上升
```

编排器不关心修复细节，只做三件事：把 `review.completed` 消息里的发现交给本 Agent、
收下 `fix.applied`、把下一轮审查交给审查 Agent。本仓库也能单独用，`plan` 看计划、
`apply` 真改代码。

## 快速开始

Python 3.10+，不装任何第三方包，不联网，不需要 API Key。

```bash
cd fix-agent
python -m fix_agent strategies   # 看 9 条策略（纯打印，不碰文件）
python -m fix_agent info         # 版本、环境、当前生效配置
python run_tests.py              # 跑 59 个用例
```

要让 Agent 真改代码，还得有一份审查结果 JSON。仓库自带的测试夹具就能造出一份可复现的
输入（只写进被 `.gitignore` 忽略的 `.demo/`）：

```bash
python -c "from tests.support import buggy_source; import pathlib; p=pathlib.Path('.demo/worktree'); p.mkdir(parents=True, exist_ok=True); (p/'mod.py').write_text(buggy_source(), encoding='utf-8', newline='\n')"
python -c "from tests.test_agent import _findings; from fix_agent.protocol import dumps; import pathlib; pathlib.Path('.demo/review.json').write_text(dumps({'findings':[f.to_dict() for f in _findings()]}), encoding='utf-8')"

python -m fix_agent plan  .demo/worktree --findings .demo/review.json   # 只读，绝对安全
python -m fix_agent apply .demo/worktree --findings .demo/review.json --report-dir .demo/reports
```

`--findings` 认审查 Agent 原样产出的报告（`{"findings":[...]}`），也认裸 JSON 数组。
`plan` 只读、`apply` 才写文件，两个都能加 `--dry-run` / `--json` 改行为。`.demo/` 用完
直接删掉就行。

本仓库会自动去同级目录找 Homework 1 的 codeguard（`D:\软件工程实践\codeguard`），
找到就用它做权威自检，找不到就降级，细节见下面"自检机制"。

## 效果预览

`plan` 什么都不动，只告诉你这批发现会怎么处理：

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
  ⏸  LOW      CG304   `mod.py:9` 转人工：docstring 得写作者的真实意图，机器编出来的注释没意义
════════════════════════════════════════════════════════════════════════
可自动修复 7 条，转人工 1 条。
（转人工的条目不会被静默丢弃，会写进修复报告。）
```

`apply` 的真实输出（自检引擎用的是 Homework 1 的 codeguard）：

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
 """示例：用户存储服务（故意保留典型问题）。"""
 
-import hashlib
 import json
+import logging
+import os
 
-AUTH_TOKEN = "sk-live-9f8a7b6c5d4e3f2a1b0c"
+logger = logging.getLogger(__name__)
+
+AUTH_TOKEN = os.environ["AUTH_TOKEN"]
 
 
 def create_user(cursor, name, email, age):
     assert name and email, "name 与 email 不能为空"
-    cursor.execute(f"INSERT INTO users (name, email, age) VALUES ('{name}', '{email}', {age})")
+    cursor.execute("INSERT INTO users (name, email, age) VALUES (?, ?, ?)", (name, email, age,))
     return cursor.lastrowid
 
 
 def get_user(cursor, user_id):
-    assert user_id, "user_id 不能为空"
+    if not (user_id):
+        raise ValueError('user_id 不能为空')
     try:
         cursor.execute("SELECT id, name FROM users WHERE id = ?", (user_id,))
         return cursor.fetchone()
     except Exception:
+        logger.exception("get_user 中捕获未预期异常，已按原有降级逻辑继续", exc_info=True)
         return None
 
 
-def find_users(cursor, keyword, limit_collector=[]):
+def find_users(cursor, keyword, limit_collector=None):
+    if limit_collector is None:
+        limit_collector = []
     sql = "SELECT id, name FROM users WHERE name LIKE ?"
     cursor.execute(sql, ("%" + keyword + "%",))
     limit_collector.extend(cursor.fetchall())
@@ -29,7 +36,6 @@
 
 
 def load_config(path):
-    fh = open(path)
-    data = fh.read()
-    fh.close()
+    with open(path) as fh:
+        data = fh.read()
     return json.loads(data)
```

上面条目的顺序是 CG206 → CG203 → … → CG403，跟发现清单的顺序不一样，因为同一文件内的
发现是按行号从大到小处理的（原因见 `Design.md` 第 3 节）。另外注意 CG202 只补日志，没把
`return None` 改成 `raise`：那个返回值可能是有意留的降级路径。

## 命令行用法

| 子命令 | 作用 | 改文件 | 需要 API Key |
| --- | --- | --- | --- |
| `plan <目录>` | 只打印处理计划：每条发现走哪条策略、为什么转人工 | 绝不 | 不需要 |
| `apply <目录>` | 执行修复，逐条自检后写回 | 会改（`--dry-run` 除外） | 不需要（`--engine llm` 除外） |
| `strategies` / `info` | 打印 9 条策略对照表 / 版本、环境、当前配置 | 不改 | 不需要 |

`plan` / `apply` 共用这些参数，其中 `--findings` 必填：

| 参数 | 说明 |
| --- | --- |
| `<target>` / `--findings <path>` | 要修复的目录（发现里的 `file` 相对它）/ 审查结果 JSON |
| `--engine rules\|llm` | 修复引擎，默认 `rules` |
| `--only-rule <ID>` / `--skip-rule <ID>` / `--max-fixes <N>` | 只修 / 跳过指定规则（可重复）/ 最多处理多少条（默认 80） |
| `--codeguard-path <dir>` | codeguard 源码根目录（自检用），一般不用管 |
| `--json` / `--quiet` / `--verbose` | 输出格式与日志级别 |

`--findings` 支持 `{"findings":[...]}`、`{"payload":{"findings":[...]}}` 和裸数组三种形态。
`apply` 还多几个开关：`--dry-run` 全流程跑一遍、自检照做、就是不写盘（`note` 后面会追加
"（试运行，未写盘）"）；`--report-dir <dir>` 把 `fix.json` / `fix.diff` 落到指定目录；
`--keep-backup` 在写回前把原文件备份到同目录的 `.fix_backup/<文件名>.orig`。`--engine llm`
还要 `--api-key`（或环境变量 `FIX_API_KEY`）。

退出码只有两个值：

| 退出码 | 含义 |
| --- | --- |
| `0` | 任务完成。有发现被拒绝改写也算完成，结果在报告里逐条交代 |
| `2` | 用法、配置或输入错误：找不到 findings 文件、JSON 里没有 `findings`、目标目录不存在、`engine=llm` 缺 Key |

子进程模式 `python -m fix_agent.worker` 在 Agent 返回 `status=failed` 时退出码为 `1`。

```bash
python -m fix_agent apply .demo/worktree --findings .demo/review.json --only-rule CG106   # 只修 SQL 注入
python -m fix_agent apply .demo/worktree --findings .demo/review.json --dry-run           # 先看 diff
python -m fix_agent apply .demo/worktree --findings .demo/review.json --json              # JSON 打到终端
```

> 要落机器可读的结果请用 `--report-dir <目录>`。Windows PowerShell 的 `>` 默认写 UTF-16，
> 接 CI 时容易被误判成编码问题。

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
| `CG105` | `move_secret_to_env` | 字面量密钥 → `os.environ["TOKEN"]`，并补 `import os` | 搬到环境变量语义等价，也明确更安全；只处理"右边是字符串字面量"的简单赋值 |
| `CG106` | `parameterize_sql` | f-string / `%` 拼的 SQL → 占位符 + 参数元组 | 值没变，只是把字符串拼接换成驱动占位符 |
| `CG202` | `log_broad_except` | 宽泛 `except` 静默忽略 → 先 `logger.exception(...)` 再维持原逻辑 | 只增加一行日志、不动控制流；故意不加 `raise` |
| `CG203` | `sentinel_default` | `def f(x=[])` → `x=None` + 函数体内初始化 | Python 官方推荐写法，对外签名兼容，只在函数体补两行 |
| `CG204` | `identity_comparison` | `x == None` → `x is None` | 单例比较语义确定，改写后避免重载 `__eq__` 的歧义 |
| `CG206` | `context_manager` | `open(); ...; close()` → `with open(p) as fh:` | 同一语句块内找到配对 `close()`、且之后不再用该变量时，改写是等价的 |
| `CG207` | `explicit_validation` | `assert cond, msg` → `if not (cond): raise ValueError(msg)` | `assert` 在 `python -O` 下会被优化掉，改写后语义只会更严格 |
| `CG402` | `strip_trailing_whitespace` | 删行尾空白 | 纯空白字符，不改变语义也不改变缩进深度 |
| `CG403` | `drop_unused_import` | 未使用的 import → 删行或从多名称导入中移除 | "未被引用"能在 AST 层面判定，删掉不会改变运行时行为 |

这张表可以用 `python -m fix_agent strategies` 直接打出来。每条改写都带 `note`（改了什么、
人还要做什么）和 `before`/`after` 片段，写进 `PatchRecord`，加 `--report-dir` 时落到
`fix.json` 里。

## 哪些问题我们明确不修

这张表大概是本仓库最想给人看的：哪些问题我们拒绝自动修，以及为什么。`plan` 会把理由
原文打印出来。

| 规则 | 为什么不自动修 |
| --- | --- |
| `CG101` | 动态执行是不是业务上真需要，只能人判断 |
| `CG102` | 修命令注入得改调用方的参数约定，等于重新设计 |
| `CG103` | 反序列化格式一改，存量数据能不能读得另外评估 |
| `CG104` | 关掉 TLS 校验有时是内网自签证书的临时办法，得先确认 CA 方案 |
| `CG107` | 随机数用在哪儿（令牌/盐/采样）只有作者知道，不能瞎换 |
| `CG108` | 临时文件竞态要看并发模型和清理策略，代码里看不出来 |
| `CG201` | 裸 `except` 想吞掉什么异常只有作者清楚，改了就把错误语义变了 |
| `CG205` | 除数为 0 一般是上游数据脏，应该从源头修 |
| `CG208` | 在 `finally` 里 `return` 属控制流结构问题，不是改一行能解决的 |
| `CG301` | 函数太长要重新划分职责，这是设计决策 |
| `CG302` | 嵌套太深要抽函数，模块结构会跟着变 |
| `CG303` | 参数太多要引入聚合对象，对外签名就变了 |
| `CG304` | docstring 得写作者的真实意图，机器编出来的注释没意义 |
| `CG305` | 常量提出来叫什么名字、放哪儿，需要领域知识 |
| `CG307` | 返回类型得作者确认，注解猜错比不写更糟 |
| `CG401` | 折行位置影响可读性，得人来定 |
| `CG405` | 语法错误只能作者自己修，机器猜不出原本想写什么 |
| `CG000` | 元信息提示，不是代码问题 |

这些发现的状态是 `deferred`（暂缓，转人工），不是被忽略：`test_unfixable_finding_is_deferred_not_dropped`
断言"暂缓必须给出理由"，`test_unsupported_reasons_documented` 断言每个理由都非空。

另有三类走的是 `rejected`（拒绝）：文件不存在、路径不在工作区内；目标文件不是 `.py`
（`EDITABLE_SUFFIXES = {".py"}` 之外的后缀其实记 `deferred`）；以及修补器自己判断形态
拿不准，每个修补器都有显式的 `PatchRefused` 分支，比如 `CG106` 遇到"已经传了参"、
`CG207` 遇到 `assert not x` 容易把逻辑写反、`CG206` 找不到配对的 `close()`。

## 自检机制

一次改写要同时满足三条才算通过（`verifier.py::Verifier.check`）：改写后的源码 `compile()`
能过；目标规则命中数严格下降；阻断级（默认 `critical` + `high`）命中数没有上升。任何一条
不满足就拒绝，`note` 里写明是哪条、前后对比是多少。

```
发现 ─▶ patcher.apply() ─▶ 候选源码 ─▶ ① compile() ─▶ ② 目标规则↓ ─▶ ③ 阻断级不↑ ─▶ 写盘
             └── PatchRefused / 任一条不过 ───────────────────────────────▶ rejected（附理由）
```

自检有两级引擎。找得到 codeguard 就用它做权威判定，报告里写 `自检引擎：codeguard`；找不到
就退化成内置 AST 计数器，只覆盖本 Agent 会改写的 9 条规则，同时显式标注降级、`fix.json`
的 `verification.degraded` 记为 `true`。两种引擎在同一份夹具上的结论必须一致，这条由契约
测试守着。

校验发生在每次改写之后，不是攒到最后统一查。某条策略一旦写错，错误会立刻被拦下并降级成
"拒绝改写"，不会污染到后面几步。

## 与上下游的契约

本仓库跟编排器、审查 Agent 只共享一个 JSON 信封（`fix_agent/protocol.py`）。四个仓库各存
一份等价副本，是故意留的重复：每个仓库都要能单独 clone、单独跑通。

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

想单独验一遍跨进程路径，就把上面的请求存成文件，再执行
`python -m fix_agent.worker --request-file req.json --response-file resp.json`。`status`
只会是 `ok` 或 `failed`，`failed` 时 `error` 一定非空、退出码为 1。`payload` 里每条发现
都有且只有一条 `PatchRecord`（`applied` / `rejected` / `deferred` / `skipped`），所以哪条
处理了、哪条没处理、为什么，都能逐条追溯。

## 测试

```bash
python run_tests.py           # 59 个用例（-v 列出用例名，patchers 只跑该文件）；pytest -q 也行
```

真实输出的结尾一行是：`结果：59/59 通过，全部通过`（Python 3.14.7）。全部测试离线跑：
不联网、不要 API Key、也不依赖另外三个仓库。`Verifier` 会尽力去找 codeguard，找不到就走
内置计数器，用例对两种引擎都成立。

| 文件 | 用例 | 守住什么 |
| --- | --- | --- |
| `test_agent.py` / `test_patchers.py` | 15 / 19 | 端到端闭环：7 条全修干净、不可修的转人工且不丢弃、`--dry-run` 不碰磁盘、越界路径被拒、只修/跳过/上限过滤、diff 生成、协议往返、行号漂移回归 / 每个策略"改对了"（用 `Verifier` 判定）和"该拒绝时拒绝"，以及修补器是纯函数 |
| `test_source.py` / `test_verifier.py` | 14 / 11 | 倒序应用、区间重叠、中文源码的字节列偏移、行级对齐 / 三种拒绝路径、降级说明、两引擎一致性 |

## 配置项

优先级是命令行参数 > 环境变量 > `.env` > 默认值。前缀 `FIX_`，完整示例见 `.env.example`，
当前生效值用 `python -m fix_agent info` 看。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FIX_ENGINE` / `FIX_BLOCKING` | `rules` / `critical,high` | 引擎（`rules` / `llm`）/ 阻断级别（必须与编排器、审查 Agent 一致） |
| `FIX_MAX_FIXES` / `FIX_ONLY_RULES` / `FIX_SKIP_RULES` / `FIX_VERIFY` / `FIX_DRY_RUN` / `FIX_KEEP_BACKUP` | `80` / 空 / 空 / `1` / `0` / `0` | 单次处理上限 / 只处理规则 / 跳过规则 / 规则级自检 / 试运行 / 写回前备份到 `.fix_backup/` |
| `FIX_WORKSPACE_ROOT` / `FIX_CODEGUARD_PATH` / `HW2_PACKAGES_HOME` | 空 | 相对 `target` 的基准目录 / codeguard 源码根目录 / 额外包搜索根（`os.pathsep` 分隔） |
| `FIX_API_KEY` / `FIX_MODEL` / `FIX_BASE_URL` | 空 / `deepseek-chat` / `https://api.deepseek.com` | `engine=llm` 用；另有 `FIX_TEMPERATURE`(0.1) / `FIX_MAX_TOKENS`(8000) / `FIX_TIMEOUT`(120s) |

`FIX_BLOCKING` 在审查 Agent 那边同名同义，两边必须一致：本 Agent 拿它判断"这次改写有没有
引入新的阻断级问题"。口径不一致就会出现"修复觉得安全、审查觉得不合格"的来回拉锯，所以
编排器把同一个值同时发给两个 Agent。

## 几个设计取舍

改写方式是 AST 定位加字符区间替换，不做整文件格式化重写。这样不动无关代码，diff 小、
人能复核，也不用引入 `libcst` / `rope`。

拿不准一律 `PatchRefused`，因为自动改错代码的代价远高于留给人。依赖同理，第三方依赖是
0 个，clone 下来就能跑，`engine=llm` 连 HTTP 都用标准库。

自检放在每次改写之后，不攒到最后统一做，错误立刻拦下；判定优先用 codeguard，有 Homework 1
的规则引擎就按它的口径来，没有也能工作但会明说降级了。

行号处理做两层：先倒序处理，再用 `difflib` 行级对齐重算。只做倒序解决不了一半问题，
模块级 import 插入会把后面整体顶下去。报告里的行号统一用审查报告里的原始行号，因为读者
会拿它去当前文件里定位，换成处理时的新行号就错位了。

引擎分工上，规则级是主路径，LLM 只是可选增强：规则级确定、可复现、快，LLM 只在规则覆盖
不到的形态上有价值，而且必须过同一套自检。

## 常见问题

**一定要有 codeguard 才能跑吗？能修 Java / JavaScript 吗？** 不用。找不到 codeguard 时自检
降级为内置 AST 计数器（只覆盖 9 条规则），报告里会明确标注；想和审查侧口径完全一致，就把
codeguard 放同目录，或者用 `--codeguard-path ../codeguard`。其他语言也不行：
`EDITABLE_SUFFIXES` 只有 `.py`，别的后缀记 `deferred` 并说明原因。所有策略都建在 Python
`ast` 上，换语言等于重写。

**为什么不把 `except Exception: return None` 直接改成 `raise`？`fix.json` 里的 `after` 为什么
可能是空的？** 前一个问题：改了会变更线上行为，那个返回值可能是特意留的降级路径，本 Agent
只补日志，"要不要改成抛出"属于设计决策。后一个问题：`before`/`after` 是"同一行号的片段"，
策略在文件顶部插入 `import os` 或 `logger = ...` 时原行号的内容会整体下移，那一行可能变成
空行，这种情况以 `diff` 为准。

**会不会改到工作区之外的文件？`--dry-run` 和 `--json` 能一起用吗？** 不会。`_safe_path` 校验
解析后的路径是否还在 `target` 内，不满足的直接记 `rejected`（`test_write_outside_root_rejected`
守着这条）。`--dry-run` 和 `--json` 也能并用，前者决定写不写盘，后者只决定输出格式。

## 许可与相关仓库

本项目为课程作业，采用 MIT 许可。

| 仓库 | 角色 | 地址 |
| --- | --- | --- |
| Homework 1 | codeguard：代码审查 Agent（本仓库自检的权威规则引擎） | <https://github.com/0x697551b0/codeguard> |
| Homework 2 · 1 | 编码 Agent（`coding_agent`） | <https://github.com/0x697551b0/hw2-coding-agent> |
| Homework 2 · 2 | 审查 Agent（`review_agent`，复用 codeguard） | <https://github.com/0x697551b0/hw2-review-agent> |
| Homework 2 · 3 | 修复 Agent（本仓库，`fix_agent`） | <https://github.com/0x697551b0/hw2-fix-agent> |
| Homework 2 · 4 | 编排器（`workflow`） | <https://github.com/0x697551b0/hw2-code-review-workflow> |

设计文档见 [`Design.md`](Design.md)：架构、自证机制、行号漂移和踩坑记录都在那儿。
