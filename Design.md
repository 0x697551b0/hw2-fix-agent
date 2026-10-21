# FixAgent 设计文档

> 软件工程实践 Homework 2 · 修复 Agent（`fix_agent`）

这份文档记的是架构决策、补齐机制、失败处理，以及开发时真正踩到的坑。文中所有数字
（测试数、策略数、行号）都能在仓库里核对，所有"为什么"在代码注释里也有一份对应的说明。

## 1. 需求与目标

作业要求搭一个 2–3 个 Agent 协作的 Workflow，本仓库负责其中"修复"这一环：根据审查
Agent 给出的结构化发现改代码，并且证明每一处改写是安全的。

### 1.1 做什么

每条发现都要有结论：`applied` / `rejected` / `deferred` / `skipped`，并附理由，不允许
静默丢弃。每次改写后立刻验证三件事（能编译、目标规则命中数下降、没引入新的阻断级问题），
三项全过才落地。

修补器都写成纯函数，同样输入必得同样输出，所以报告和 diff 是可以复现的；这条不是洁癖，
联调和验收都要靠它。整个仓库零第三方依赖、不联网、不需要 API Key，既能单独跑，也能被
编排器当节点调用。

### 1.2 不做什么

下面这几条"不做"比"做什么"更能说明这个 Agent 的边界。

不重构。拆函数、提取常量、合并重复代码都是设计决策，会动模块结构，所以 `CG301` /
`CG302` / `CG305` 全部转人工。

不改接口签名。改了会波及调用方，而调用方不在本次工作区内，没法验证（`CG303`）。

不猜业务意图。裸 `except` 该吞什么异常、随机数用在哪儿、反序列化格式能不能变，只有作者
知道（`CG201` / `CG107` / `CG103`）。

不生成注释和类型注解。`CG304` / `CG307` 的评语写得很直白：机器生成的注释没有价值，猜错
注解比没有注解更糟。

不把 LLM 当主路径。规则级修补确定、快、可复现，LLM 只是可选增强，而且必须过同一套自检。

不做回滚。多文件场景下没有事务语义，见第 9 节。

## 2. 总体架构

### 2.1 分层

```
┌──────────────────────────────────────────────────────────────────────┐
│ 接口层   cli.py            plan / apply / strategies / info           │
│          worker.py         python -m fix_agent.worker（JSON 往返）     │
│          protocol.py       AgentRequest / AgentResponse 信封           │
└───────────────────────────────┬──────────────────────────────────────┘
┌───────────────────────────────▼──────────────────────────────────────┐
│ 编排层   agent.py :: FixAgent                                         │
│    · 选发现（only/skip/max_fixes）· 按文件分组 · 按行号倒序            │
│    · 逐条改写 → 自检 → 落盘 · 行号重算 · 汇总 FixOutcome               │
└──────────┬─────────────────────────────────────┬─────────────────────┘
┌──────────▼──────────────────┐   ┌──────────────▼─────────────────────┐
│ 改写层  patchers.py         │   │ 校验层  verifier.py                │
│  9 个 Patcher + 18 条拒绝理由 │   │  两级引擎：codeguard / 内置计数器    │
└──────────┬──────────────────┘   └──────────────┬─────────────────────┘
┌──────────▼─────────────────────────────────────▼─────────────────────┐
│ 原语层   source.py：AST 位置 → 字符区间、行级对齐、区间替换            │
└──────────────────────────────────────────────────────────────────────┘
   横切：models.py（记录）· config.py（配置）· errors.py（异常）· llm.py/prompts.py（可选 LLM）
```

依赖是单向往下的：`agent.py` 依赖 `patchers` / `verifier` / `source`，这三者互不依赖；
`source.py` 只用标准库 `ast`。`verifier` 对 codeguard 是可选依赖，通过 `locate.py` 尽力
去找，找不到就降级，算是依赖倒置的一个轻量用法。

### 2.2 各模块的分工

| 模块 | 职责 | 不负责什么 |
| --- | --- | --- |
| `agent.py` | 选发现、按文件分组、倒序处理、行号重算、落盘、协议入口 `handle()` | 不实现具体改写策略 |
| `patchers.py` | 9 个修补器 + 18 条"不能自动修"的理由 | 不判断改写是否安全（那是 verifier 的事） |
| `source.py` | AST 位置 → 字符区间、`apply_edits`、`line_mapping` | 不知道任何规则 |
| `verifier.py` | 统计规则命中、判定三条标准、两级引擎切换 | 不改写代码 |
| `models.py` | `PatchRecord` / `FileFix` / `FixOutcome` 与 payload 视图 | 无业务逻辑 |
| `protocol.py` | 契约信封（四个仓库各一份等价副本） | 不含 Agent 行为 |
| `locate.py` | 定位 codeguard（找不到返回 `None`，不抛异常） | 不负责自检本身 |
| `config.py` | `FixConfig`（`FIX_*` 环境变量、校验） | 不含策略开关的语义 |
| `cli.py` | 参数解析、三种输出模式、退出码 | 不含改写逻辑 |
| `worker.py` | 子进程模式：请求文件 → 响应文件 | 不含改写逻辑 |
| `llm.py` / `prompts.py` | `engine=llm` 的 stdlib HTTP 客户端与 Prompt | 默认路径不用 |

## 3. 核心机制详解

### 3.1 拿不准就不改

修复 Agent 的失败代价是不对称的。拒绝改写的代价是"这行代码还是老样子，人来处理五分钟"；
改错代码的代价是"引入一个线上故障，而且看起来一切正常"。所以每条策略都得能回答"凭什么
说这次改写是等价变换"，答不上来就抛 `PatchRefused`。

三条判据，在每个 `Patcher.rewrite` 里都能找到对应的分支。

第一，形态要能静态判定，AST 里能唯一确定要改的节点。比如 `CG105` 只处理
`名字 = 字符串字面量`，遇到 `TOKEN = "sk-" + "-abc"` 直接拒绝
（`test_cg105_refuses_non_literal`）。

第二，改写在语义上等价或者严格更安全。`CG204` 的 `== None → is None`、`CG402` 的删行尾
空白属于"严格更正确"；`CG202` 只加日志不动控制流；`CG207` 把 `assert` 换成显式 `raise`
只会更严格，因为 `assert` 在 `python -O` 下会被优化掉。

第三，改动范围局部且能闭合：不跨函数边界、不改签名、不动控制流结构。`CG206` 是最好的
例子，只有在同一语句块内找到配对的 `close()`、并且 `close()` 之后不再使用该变量时才改写，
否则拒绝（"没有在后续语句中找到 `fh.close()`，无法确定 with 块的作用范围"）。

`errors.py` 里专门写了注释解释为什么 `PatchRefused` 不算错误。把"拒绝修改"抛成异常再
吞掉，会让人误以为出了故障；而"这条问题需要人来判断"本身就是这套系统最有价值的输出之一。
所以它继承 `FixAgentError` 但走 `rejected` 这条正常路径，理由原样写进 `PatchRecord.note`。

### 3.2 AST 定位加字符区间替换

改写的实现只有四步：`ast.parse` 找到目标节点 → `SourceMap.node_span()` 换算成整篇源码的
字符区间 → 用 `Edit(start, end, text)` 替换这一段 → 可选地再插一条模块级语句。

没上 `libcst` / `rope` 这类保留格式的改写库（`source.py` 的模块注释里也写了）：

| 维度 | AST + 区间替换（本实现，约 250 行） | libcst / rope |
| --- | --- | --- |
| 依赖 | 0（标准库 `ast`） | 各引入一个第三方包，跟"clone 下来就能跑"冲突 |
| 本次任务形态 | 都是局部单点改写（一个表达式、一条语句、一行 import） | 为"整文件格式化重写"设计，能力远超所需 |
| 可解释性 | 清楚自己改了哪几个字符，出问题能精确解释 | 抽象层更厚，定位偏差要读库源码 |

代价是位置信息得自己处理（多行节点和编码问题见 3.5），还得在文本层面自己保证"改动不
重叠"。`apply_edits` 的解法是按起点倒序应用：

```python
ordered = sorted(edits, key=lambda e: (e.start, e.end), reverse=True)
for edit in ordered:
    if edit.end > last_start:                  # 与更靠后的编辑区间冲突
        raise SpliceError("编辑区间重叠 ...")
    result = result[: edit.start] + edit.text + result[edit.end :]
    last_start = edit.start
```

倒序之后，前面区间的下标不会因为后面被替换而失效，不用维护偏移增量，也就少一处能算错的
地方（`test_apply_edits_descending_order` 和 `test_overlapping_edits_rejected` 守着它）。

模块级插入点的选择也有讲究。补 `import logging` / `logger = ...` 时，插入位置定在最后
一个顶层 import 语句的末尾，不往后跳空行：空行属于原有排版，插在 import
之后第一行能让新 import 紧挨着 import 组，原来的两行空行还留在组的末尾
（`patchers.py::_prelude_offset` 的注释）。

### 3.3 行号漂移：本项目踩到最贵的 bug

发现清单里的行号是针对原始源码的，可任何一次改写都可能插行删行：`CG202` 会在文件顶部
插入 `import logging` 和 `logger = logging.getLogger(__name__)`；`CG105` 可能插入
`import os`；`CG206` 把 `fh = open(p); ...; fh.close()` 三行合成一个 `with` 块，净减两行。

第一层修正是把同一文件内的发现按行号从大到小处理，这样"尚未处理的行号"受影响的可能性
最小。但这只减少概率，问题还在：模块级 import 插在最顶部，无论什么顺序都会把后面所有
行号整体顶下去；`CG206` 这类删行的改写又会让后面的行号整体上移。

第二层修正才是真解决：每完成一次改写，立刻用 `difflib` 做行级对齐，把剩余待处理发现的
行号换算到新源码上（`agent.py::_remap_pending` + `source.py::line_mapping`）。

```python
mapping = line_mapping(before, after)      # {旧行号: 新行号}，对不上的行不出现
for finding in pending:
    if not finding.line: continue
    mapped = mapping.get(int(finding.line))
    finding.line = None if mapped is None else mapped     # 位置消失 → 交给"转人工"
```

用对齐，不自己算增量，是因为一个补丁可能同时删行和插行（`open/read/close` 变成 `with`），
增量法在这类地方必然算错，对齐算法不需要特判。`line_mapping` 只保留 `equal` 和 `replace`
块里能一一对应的行；被删掉的行不在映射里，调用方据此把该发现标成"位置失效，转人工"
（`test_line_mapping_deletion_drops_lines`）。

这个 bug 贵在修补器不报错。拿着过期的行号去定位，`node_at_line` 很可能在那一行找到另一个
同类节点，然后"成功地"改掉一段无关代码：编译能过，自检里目标规则命中数也确实下降了
（它改的是别处的同类问题），一切看起来都正常。`tests/test_agent.py::test_line_shift_does_not_break_later_findings`
就是这个 bug 的回归测试，它断言 `CG202`（会插 import）和 `CG105`、`CG106` 都能被正确改写，
而且没有任何拒绝。

还有一个报告上的细节：`PatchRecord.line` 一律用审查报告里的原始行号，不用"处理时的新行
号"（`agent.py` 里那个 `original_lines` 字典就是为这个存在的）。读者会拿报告里的行号去
当前文件里看，换成新行号就错位了。代价是同一文件里后处理的条目行号看着不连续，这个能接受。

### 3.4 补丁自证：三条标准加两级引擎

自检的目的，是让"改错了"变成一次看得见的拒绝，不留下一次静默的污染。

| # | 标准 | 实现位置 | 失败信息（真实文案） |
| --- | --- | --- | --- |
| ① | `compile()` 能过 | `Verifier.check` | `改写后源码无法编译：第 N 行 ...` |
| ② | 目标规则命中数严格下降 | `Verifier.check` | `CG106 的命中数没有下降（1 → 1）` |
| ③ | 阻断级命中数不上升 | `Verifier.check` | `改写引入了新的阻断级问题：CG105` |

两级引擎的意义不一样。codeguard 是权威：Homework 1 那套 27 条规则的引擎，口径和审查 Agent
完全一致，`Verifier._codeguard_counts` 把 `analyze_source()` 的 `findings` 按 `rule_id`
计数。内置计数器是降级路径，只覆盖本 Agent 会改写的 9 条规则（`BUILTIN_RULES`），用 AST
现算；它的价值是"没有 codeguard 也能工作"，代价是覆盖面窄，所以 `verification.degraded`
和 CLI 的那行警告都是显式的，不含糊。

降级路径最大的风险是两边口径不一致，于是有了契约测试
`test_builtin_counter_matches_codeguard_on_fixture`：在同一份夹具上，两个引擎对 9 条规则的
计数必须逐条相等。写这个测试时踩到过一个真实的细节：`CG207` 在 codeguard 里只统计"校验
外部输入"的 `assert`（表达式里出现 `input` / `request` / `args` / `json` / `param` / `user`
这些特征词），内置计数器不跟着这么写，就会出现"修复前报 2 条、修复后报 1 条"的假象，进而
触发无谓的重试。这条对齐写在了 `verifier.py::_builtin_counts` 的注释里。

自检还有一处设计：校验对象是整份候选源码，不是"改写的那几行"。局部片段既没法独立编译，
也没法独立扫描。这也是 `engine=llm` 的 Prompt 强制模型返回完整文件的原因。

### 3.5 `col_offset` 是 UTF-8 字节偏移

CPython 的 `ast` 节点位置里，`col_offset` / `end_col_offset` 是该行的 UTF-8 字节数，不是
字符下标。源码里有中文时（注释、docstring、字符串字面量），直接 `line[col_offset:]` 会切
在多字节字符中间。后果同样是"不报错，只是改错地方"：切出来的片段仍能拼成合法 Python，
只是内容错位。

所以 `SourceMap.offset()` 统一走 `byte_col_to_char_col()`：把该行编码成 UTF-8，取前
`byte_col` 字节再解码回字符数，切在字符中间时退回最近的合法边界（`errors="ignore"`），
不抛异常。回归测试是 `test_column_offset_handles_chinese`（`# 中文注释` 后面那一行）和
`test_chinese_line_mapping`（中文变量名加中文字符串）。

这条跟 3.3 的行号漂移是同一类问题的两个维度：位置信息一旦对不上，修补器不会报错，它只
会安静地改错地方。这也是本仓库把所有位置换算集中到 `source.py` 一个类里的原因，只有一处
能算位置，出问题只用查一个文件。

### 3.6 可选的 LLM 引擎

`engine=llm` 让模型整文件重写，但通过标准完全一样（`agent.py::_fix_with_llm`）。模型必须
返回完整文件内容（`prompts.py` 的 System Prompt 里写死了"不是 diff、不是片段"），因为自检
要编译整份源码、要整份扫描规则命中；然后先 `compile()`，失败就整批 `rejected`（"模型产出
无法编译：第 N 行 ..."）；再比较"目标规则命中数是否真的下降"，一条都没降就整批拒绝；最后
只有确实被消掉的那几条规则对应的发现标 `applied`，其余标 `rejected`。

不管改写来自规则还是模型，判据是同一套，模型说得再漂亮也得过自检。代价是更费 token（要求
返回整个文件），换来的是每一份模型产出都能被机器验证，我觉得这个交换值。

`llm.py` 只用标准库 `urllib.request`，没引 `httpx`：规则引擎才是主路径，为一个可选能力
引入第三方依赖会让"clone 下来就能跑"打折。代价是超时、错误码分类和退避重试得自己写
（`401/403` 不重试，`408/429/5xx` 指数退避加抖动，最多 3 次），但这些都是显式代码，反而
好排查。

顺便说 `parse_fix_payload` 的容错：模型经常在 JSON 外面裹一层 Markdown 围栏或者废话，所以
依次尝试"围栏内的内容 → 原文 → 首个 `{` 到末个 `}` 的片段"，哪个能解析成 dict 就用哪个。
这跟 Homework 1 里"协议解析要写得宽松"是同一条经验。

## 4. 数据结构

三个模型都是 `dataclass`，零依赖，字段少，容错解析手写反而更可控：

```python
PatchRecord(finding_id, rule_id, file, line, status, strategy, note, before, after, verified)
FileFix(path, original, patched, records=[PatchRecord])     # .changed / .unified_diff
FixOutcome(files=[FileFix], engine, elapsed_seconds, diagnostics, verification)
```

### 4.1 每条发现都要逐条交代状态

`status` 只有四个值，边界很清楚：

| status | 含义 | 谁产生 |
| --- | --- | --- |
| `applied` | 已改写并通过自检（`--dry-run` 时也统计，`note` 里标"未写盘"） | 修补器 + 自检通过 |
| `rejected` | 尝试过但不能安全改写：自检未过、形态拿不准、文件不存在、路径越界 | `PatchRefused` / `PatchFailed` / 自检失败 |
| `deferred` | 压根不尝试：规则没有策略、文件后缀不支持、发现没有行号 | 注册表查询阶段 |
| `skipped` | 在这条路径上没有处理（`engine=llm` 时的中间态） | LLM 引擎 |

"拒绝"和"暂缓"分开、不合并成一个"失败"，是因为它们指向的下一步动作不同：`rejected`
要人看一眼具体代码（某条策略对这个形态失效），`deferred` 要人做设计决策（要不要拆函数、
要不要补注释）。报告里两者分节列出，`FixOutcome.summary()` 也分别计数。

`FixOutcome.payload()` 同时给三种视图：`applied` / `rejected` / `deferred`（按状态分组，
给人看也给编排器看）和 `records`（全量，给追溯用）。这点冗余是有意的，下游不用再自己过滤。

### 4.2 diff 从哪来

`FileFix.unified_diff` 用 `difflib.unified_diff(..., fromfile=f"a/{path}", tofile=f"b/{path}")`
现场生成，不依赖外部 `git`。所以报告里的补丁和磁盘上的文件是同源的，不会出现"报告说改了、
文件没改"；生成成本随文件大小线性增长，`max_fixes` 默认 80 也顺带限住了单次 diff 的规模。

## 5. 失败处理与降级

异常体系按"谁该负责"划分（`errors.py`）：`PatchRefused`（这是决定）、`PatchFailed`（尝试
失败）、`VerificationError`（自检不过）、`TargetError`（目标不可用）、`ConfigError`（配置
不合法）。

| 失败点 | 谁兜住 | 用户看到什么 |
| --- | --- | --- |
| 源码本身语法错误 | `Patcher.apply` 捕获 `SyntaxError` → `PatchFailed` | 该条 `rejected`，`note`："源码本身无法解析（第 N 行 ...）" |
| 定位不到节点 / 区间非法 | 捕获 `SpliceError` → `PatchFailed` | 该条 `rejected`，`note`："定位失败：..." |
| 改写后不可编译 | `Verifier.check` 第 ① 条 | `rejected`，`note`："自检未通过：改写后源码无法编译..." |
| 规则数没下降 / 引入新阻断 | `Verifier.check` 第 ②③ 条 | `rejected`，附 `之前 → 之后` 或新增规则名 |
| 找不到 codeguard | `locate.ensure_codeguard` 返回 `None` | 不报错，降级为内置计数器并在报告里标注 |
| 文件不存在 / 越界 | `_safe_path` / `is_file()` | 全部 `rejected`，整批不写盘 |
| 非 `.py` 文件 | `EDITABLE_SUFFIXES` 检查 | 全部 `deferred`："暂不支持自动改写 .xxx 文件" |
| 目标目录不存在 | `_resolve_root` 抛 `TargetError` | CLI 打印 `❌ TargetError: ...`，退出码 2 |
| `engine=llm` 缺 Key | `FixConfig.validate` 抛 `ConfigError` | 报错信息列出三种配置方式的选项 |
| 模型输出不是 JSON | `parse_fix_payload` 三重尝试后抛 `ValueError` | 全部 `rejected`："模型修复失败：..." |
| 模型重写后目标规则没减少 | `_fix_with_llm` 显式检查 | `rejected`："模型重写后目标规则命中数没有下降，自检拒绝落地" |
| 报告目录写不进去 | `_persist` 捕获 `OSError` | 静默返回，报告写不进去不该让修复结果丢失 |
| 协议层任何未预期异常 | `handle()` 最后一层 `except Exception` | `AgentResponse.failure`，`status=failed`，退出码 1 |

一条贯穿始终的做法：自检失败不是异常，是结果。它走 `rejected` 这条正常返回路径，所以一次
"全军拒绝"的运行仍然是 `status=ok`、退出码 0，因为拒绝本身就是这套系统的合法输出。

## 6. 安全设计

这个 Agent 会直接改别人的源码，安全边界得写死。

路径沙箱在 `_safe_path`：发现里的 `file` 是相对路径时用 `(root / file).resolve()` 解析，
再用 `target.relative_to(root)` 校验是否还在工作区内；绝对路径同样解析后校验。不满足就直接
返回 `None`，该文件的所有发现记 `rejected`（"目标路径不在工作区内，已拒绝改写"）。`resolve()`
会展开符号链接，所以 `..` 和软链跳转都拦得住（`test_write_outside_root_rejected`）。

只改 `.py`，`EDITABLE_SUFFIXES = frozenset({".py"})` 就一个后缀，因为所有策略都是 `ast`
驱动的，多开一个等于重写一套。

不创建文件。目标文件不存在时记 `rejected`（"文件不存在"），绝不 `touch` 一个空文件再去改
（`test_missing_file_is_rejected_not_created`）。

先内存后磁盘。单文件内所有改写都在内存里完成，最后 `current != original` 才写一次盘，所以
"中途某个策略崩了"不会留下半改的文件。

备份是可选的：`--keep-backup` 或 `FIX_KEEP_BACKUP=1` 时，写回前把原文复制到
`<文件所在目录>/.fix_backup/<文件名>.orig`。默认关闭，因为工作流里的 `worktree` 本身就是
可丢弃的副本，强行备份只会在仓库里堆垃圾。

写入统一 `encoding="utf-8", newline="\n"`，免得 Windows 上写出 CRLF 导致整份文件在 git diff
里"全变"。另外 `config.describe()` 对 API Key 做掩码，`FixConfig` 不打印完整 Key。

## 7. 测试策略

59 个用例分四个文件，关注点有两类：正例和负例。

| 类别 | 代表用例 | 守住什么 |
| --- | --- | --- |
| 正例（改对了） | `test_cg106_parameterizes_inline_sql`、`test_cg206_uses_context_manager` | 改写后目标规则命中数为 0，且 `ast.parse` 能过 |
| 负例（该拒绝时拒绝） | `test_cg105_refuses_non_literal`、`test_cg207_refuses_negated_assert`、`test_cg106_refuses_when_already_parameterized` | 拿不准的形态必须抛 `PatchRefused`，不许瞎改 |
| 纯函数性 | `test_all_patchers_are_pure` | 同样输入必得同样输出（否则报告不可复现） |
| 契约一致性 | `test_builtin_counter_matches_codeguard_on_fixture` | 降级引擎和权威引擎在 9 条规则上结论一致 |
| 回归 | `test_line_shift_does_not_break_later_findings`、`test_column_offset_handles_chinese` | 3.3 和 3.5 那两个"静默改错"的坑 |
| 边界 | `test_write_outside_root_rejected`、`test_missing_file_is_rejected_not_created`、`test_max_fixes_caps_work` | 越界、缺文件、上限 |
| 契约往返 | `test_handle_roundtrip`、`test_handle_reports_bad_payload` | `AgentRequest → handle → AgentResponse` 的 payload 形状与错误路径 |

判据统一用 `Verifier`，不去比字符串。比如 `test_cg106_parameterizes_inline_sql` 断言的是
"CG106 命中数归零 + 参数元组出现"，不去比"某几个字符出现在某处"，后者会让任何无关的排版
调整都变成红灯。

测试全部离线：不联网、不需要 API Key、不依赖另外三个仓库。`Verifier` 尽力找 codeguard，
找不到就用内置计数器，两种情况下用例都该通过（`test_codeguard_engine_available` 会打印
提示但不失败）。

还有一个小心思：`tests/support.py` 里那个"演示用假凭据"是运行时拼出来的
（`"sk" + "-" + "live" + ...`），源码文件里不出现连续的字面量，这样本仓库被自己的规则引擎
或 codeguard 扫描时不会命中 `CG105`。测试夹具不该让工具对着自己报警。写进临时文件的样例里
是完整字面量，规则照常命中。

## 8. 开发中遇到的工程问题

### 8.1 行号漂移：修补器"成功地"改错了地方

见 3.3。只做"按行号倒序处理"时，单条改写的测试全绿；一跑多规则混合的样例就悄悄出错：
`CG202` 在顶部插入两行 import，后面的 `CG105` 拿着旧行号去找第 6 行，找到的是别的语句，
改写"成功"、自检也"通过"（它确实消掉了一个 `CG105` 命中），但改的是错的代码。修复就是
引入 `line_mapping` 重算剩余行号，并补上 `test_line_shift_does_not_break_later_findings`。

教训是位置类 bug 的可怕之处在于它不报错。所以自检不仅要问"问题消失了没"，还得（在测试
里）问"是不是在正确的地方消失的"，这也是为什么回归测试断言的是"`CG105`、`CG106`、`CG202`
都 applied 且没有任何 rejected"，不是只看最终计数。

### 8.2 `col_offset` 的字节/字符错位

见 3.5。第一版直接 `line[col_offset:end_col_offset]`，纯英文样例全过；加入中文注释后切片
落在多字节字符中间，产出的是"看起来像代码但不是原代码"的片段。修复是统一做
`byte_col_to_char_col` 换算。这个坑只在源码含非 ASCII 字符时出现，而中文注释在实际项目里
太常见了，等于说它会在最不该出问题的地方出问题。

### 8.3 审查报了、修复却说"不会修"

codeguard 的 `CG106` 同时识别 f-string 和 `%` 格式化两种拼接。第一版修补器只认
`ast.JoinedStr`（f-string），于是出现"审查报了一条 SQL 注入，修复 Agent 回一句'无法识别
的写法'"的尴尬。修复是让 `_is_interpolated` 同时认 `JoinedStr` 和 `Constant % ...`，再给
`%` 形态单独写 `_sqlize_percent`，其中还要处理 `VALUES ('%s')` 这种"占位符被引号包着"的
写法：参数化之后那层引号必须去掉，`template.replace("'?'", "?")` 就是干这个的。

教训是上下游口径必须显式对齐。同类的第二处是 `CG207`（3.4）：内置计数器必须跟 codeguard
一样只统计"校验外部输入"的 `assert`，否则会出现"修完了数字没变"的假象。

### 8.4 子进程模式里"没有产出响应"

`worker.py` 有个容易忽略的坑，注释里写得很直白：

> 只传配置类认识的键：子进程里的 `TypeError` 对调用方只表现为"没有产出响应"

编排器会把一些选项透传给 Agent 的配置类。要是 `worker.py` 直接把任意键塞进
`FixConfig(**overrides)`，一个不认识的键就会让子进程在初始化阶段抛 `TypeError` 退出，而
调用方（编排器的 `subprocess` 传输）看到的只是"响应文件不存在"，错误信息全丢了。所以这里
先用 `dataclasses.fields(FixConfig)` 过滤一遍已知键，并把初始化失败也包装成一个合法的
`AgentResponse.failure`。同一思路贯穿整个 `handle()`：协议层兜住一切异常，不让调用方看到
裸 traceback。

### 8.5 删 import 之后留下三行空行

`CG403` 删掉整行 `import hashlib` 之后，原本"import 组 + 两个空行 + 代码"的排版会变成
"import 组 + 三个空行"。于是补了 `_collapse_blank_runs(source, limit=2)`，把连续 3 行以上
的空白压回 2 行。这个小函数也说明补丁本身得干净：不然每次自动修复都在 diff 里带一堆无意义
的空白变更，评审时很难一眼看出真改了什么。

## 9. 已知局限与改进方向

| 局限 | 说明 | 可能的改进 |
| --- | --- | --- |
| 只支持 Python | 所有策略基于 `ast`，`EDITABLE_SUFFIXES = {".py"}` | 换语言基本等于重写；可以先做"检测到非 Python 就整体转人工"的更明确提示 |
| 策略覆盖面窄 | 9 条自动修复、18 条明确转人工，而 codeguard 共 27 条规则 | 逐步补充有确定等价变换的规则（例如 `CG404` 遮蔽内置名能不能改名？会影响调用方，仍不建议） |
| 没有事务语义 | 多文件按文件名顺序处理，前面的先写盘；后续文件抛 `TargetError` 时前面的改动不回滚 | 引入"先全部算完再统一落盘"的两阶段提交 |
| 单线程 | 逐文件、逐发现串行，大仓库会慢 | 文件级并行（每个文件一份独立副本）是安全的，因为改写是纯函数 |
| `before`/`after` 片段可能为空 | 顶部插入 import 后行号下移，原行号那一行可能变成空行 | 记录"改写后的相对锚点"，不用绝对行号；或者干脆只保留 diff |
| LLM 引擎比较朴素 | 整文件重写、单轮、没有 diff 反馈 | 让它返回 unified diff 再应用，token 更省也更容易校验 |
| 自检只看规则计数 | 判断不了"语义是否仍然正确"（例如 `CG106` 改写后 SQL 表名有没有拼对） | 增加"改写前后 AST 等价性检查"（除目标节点外结构不变）作为第 ④ 条标准 |

## 10. 附录：关键文件速查

| 文件 | 行数量级 | 关键类 / 函数 | 干什么的 |
| --- | --- | --- | --- |
| `fix_agent/agent.py` | ~600 | `FixAgent.fix` / `_fix_file` / `_remap_pending` / `handle` | 逐文件、逐条改写；行号重算在这里 |
| `fix_agent/patchers.py` | ~940 | `PATCHERS` / `Patcher` / `NOT_SUPPORTED_REASONS` | 9 条策略 + 18 条拒绝理由 |
| `fix_agent/source.py` | ~240 | `SourceMap` / `apply_edits` / `line_mapping` | 位置换算与区间替换的原语 |
| `fix_agent/verifier.py` | ~290 | `Verifier.check` / `_codeguard_counts` / `_builtin_counts` | 三条自检标准，两级引擎 |
| `fix_agent/protocol.py` | ~320 | `AgentRequest` / `AgentResponse` / `FindingContract` | 四个仓库共享的契约 |
| `fix_agent/models.py` | ~150 | `PatchRecord` / `FileFix` / `FixOutcome` | 逐条状态与 payload 视图 |
| `fix_agent/config.py` | ~145 | `FixConfig.from_env` / `validate` | `FIX_*` 配置与校验 |
| `fix_agent/cli.py` | ~245 | `main` / `_render_plan` / `_render_outcome` | 三个输出模式与退出码 |
| `fix_agent/locate.py` | ~130 | `ensure_codeguard` | 找到就用，找不到返回 `None` |
| `fix_agent/worker.py` | ~90 | `main` / `_emit` | 子进程 JSON 往返 |
| `tests/support.py` | ~135 | `buggy_source` / `tempdir` / `expect_raises` | 夹具与工具 |
