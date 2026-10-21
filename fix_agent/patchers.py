"""修补器注册表：每条可自动修的规则配一个保守的改写策略。

拿不准就 ``PatchRefused``，把问题留给人工，一个会瞎改代码的修复 Agent 比一个不改的危险
得多。所以只做单点改写：不动签名、不动控制流，原意能保住就保住（宽泛 except 只补日志，
不加 ``raise``）。所有策略都基于 ``ast`` 定位加字符区间替换（见 ``source.py``），不引
第三方依赖。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import ClassVar, Sequence

from fix_agent.errors import PatchFailed, PatchRefused
from fix_agent.protocol import FindingContract
from fix_agent.source import (
    Edit,
    SourceMap,
    SpliceError,
    apply_edits,
    dotted_name,
    enclosing_block,
    enclosing_function,
    indent_text,
    node_at_line,
    nodes_covering_line,
    parse,
    statement_index,
)

#: 可能执行 SQL 的方法名（与 codeguard 的识别口径一致）
SQL_CALLERS: frozenset[str] = frozenset({"execute", "executemany", "executescript", "executedict", "raw", "query"})

#: 判定"密钥类变量名"的关键词
SECRET_NAME_HINTS: tuple[str, ...] = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "auth")

#: 不可自动修复的规则 → 理由（与审查 Agent 的 fixability 表保持一致，有契约测试比对）
NOT_SUPPORTED_REASONS: dict[str, str] = {
    "CG101": "动态执行是不是业务上真需要，只能人判断",
    "CG102": "修命令注入得改调用方的参数约定，等于重新设计",
    "CG103": "反序列化格式一改，存量数据能不能读得另外评估",
    "CG104": "关掉 TLS 校验有时是内网自签证书的临时办法，得先确认 CA 方案",
    "CG107": "随机数用在哪儿（令牌/盐/采样）只有作者知道，不能瞎换",
    "CG108": "临时文件竞态要看并发模型和清理策略，代码里看不出来",
    "CG201": "裸 except 想吞掉什么异常只有作者清楚，改了就把错误语义变了",
    "CG205": "除数为 0 一般是上游数据脏，应该从源头修",
    "CG208": "在 finally 里 return 属控制流结构问题，不是改一行能解决的",
    "CG301": "函数太长要重新划分职责，这是设计决策",
    "CG302": "嵌套太深要抽函数，模块结构会跟着变",
    "CG303": "参数太多要引入聚合对象，对外签名就变了",
    "CG304": "docstring 得写作者的真实意图，机器编出来的注释没意义",
    "CG305": "常量提出来叫什么名字、放哪儿，需要领域知识",
    "CG307": "返回类型得作者确认，注解猜错比不写更糟",
    "CG401": "折行位置影响可读性，得人来定",
    "CG405": "语法错误只能作者自己修，机器猜不出原本想写什么",
    "CG000": "元信息提示，不是代码问题",
}


@dataclass
class Patch:
    """一次成功改写的结果。"""

    source: str
    strategy: str
    note: str
    before: str = ""
    after: str = ""
    detail: dict[str, object] = field(default_factory=dict)


class Patcher:
    """修补器基类。子类实现 ``rewrite``。"""

    strategy: ClassVar[str] = ""
    rule_ids: ClassVar[tuple[str, ...]] = ()
    description: ClassVar[str] = ""

    def apply(self, source: str, finding: FindingContract) -> Patch:
        """统一的入口：解析 → 改写 → 组装结果。任何内部异常都转成可读的失败。"""
        try:
            patch = self.rewrite(source, finding)
        except PatchRefused:
            raise
        except SpliceError as exc:
            raise PatchFailed(f"定位失败：{exc}") from exc
        except SyntaxError as exc:
            raise PatchFailed(f"源码本身无法解析（第 {exc.lineno} 行：{exc.msg}）") from exc
        except (ValueError, KeyError, IndexError, AttributeError) as exc:
            raise PatchFailed(f"改写失败（{type(exc).__name__}: {exc}）") from exc
        patch.strategy = self.strategy
        return patch

    def rewrite(self, source: str, finding: FindingContract) -> Patch:  # pragma: no cover - 抽象
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} {self.strategy}>"


# 公共工具
def _lineno(finding: FindingContract) -> int:
    if not finding.line:
        raise PatchRefused("该发现没有行号，无法定位到具体代码")
    return int(finding.line)


def _excerpt(source: str, lineno: int) -> str:
    return SourceMap(source).line_text(lineno)


def _is_sql_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = dotted_name(node.func).split(".")[-1].lower()
    return name in SQL_CALLERS


def _top_level_imports(tree: ast.AST) -> set[str]:
    """模块级 import 的模块名集合（用于判断"还需不需要补 import"）。"""
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _logger_name(tree: ast.AST) -> str:
    """找出模块里已有的 logger 变量名；没有则返回约定名 ``logger``。"""
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            text = ast.unparse(node.value) if hasattr(ast, "unparse") else ""
            if "getLogger" in text:
                return target.id
    return "logger"


def _has_any_logger(tree: ast.AST) -> bool:
    """模块里是否已经定义了 logger 对象（变量名随意）。"""
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and "getLogger" in (ast.unparse(node.value) if hasattr(ast, "unparse") else ""):
                return True
    return False


def _prelude_offset(sm: SourceMap, tree: ast.AST) -> int:
    """
    计算模块级 import 与 logger 的插入位置：最后一个顶层 import 语句的末尾。

    插在这里，不往后找空行：空行属于原有排版，插在 import 之后第一行能让新 import
    紧挨着 import 组，原有的两行空行还留在组的末尾。
    """
    body = list(getattr(tree, "body", []))
    last_import: ast.stmt | None = None
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node

    if last_import is not None:
        end_line = getattr(last_import, "end_lineno", last_import.lineno)
        _, offset = sm.line_span(end_line, include_newline=True)
        return offset

    # 没有 import：放在 docstring 之后（若有）
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        end_line = getattr(body[0], "end_lineno", body[0].lineno)
        _, offset = sm.line_span(end_line, include_newline=True)
        return offset
    return 0


def _module_edits(
    sm: SourceMap,
    tree: ast.AST,
    *,
    want_imports: Sequence[str] = (),
    want_logger: bool = False,
) -> list[Edit]:
    """补上需要的 import 与 ``logger = logging.getLogger(__name__)``。"""
    existing = _top_level_imports(tree)
    to_add = [name for name in want_imports if name not in existing]
    need_logger = want_logger and not _has_any_logger(tree)
    if not to_add and not need_logger:
        return []

    offset = _prelude_offset(sm, tree)
    lines: list[str] = [f"import {name}" for name in to_add]
    if need_logger:
        if lines:
            lines.append("")
        lines.append("logger = logging.getLogger(__name__)")
    text = "\n".join(lines) + "\n"
    if need_logger and not to_add:
        # 只补 logger 时给一个前导空行，避免贴着上一行 import
        text = "\n" + text
    return [Edit(offset, offset, text)]



def _body_insert_point(sm: SourceMap, func: ast.AST) -> tuple[int, str, bool]:
    """
    计算函数体插入点。

    返回 (字符偏移, 缩进, 是否在 docstring 之后)。

    - 若首条语句是 docstring，插到 docstring 之后（缩进取第二条语句或首条+4）；
    - 否则插到首条语句之前（此时偏移正好在缩进之后，插入文本无需再带前导缩进）。
    """
    body = list(getattr(func, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        end_line = getattr(body[0], "end_lineno", body[0].lineno)
        _, offset = sm.line_span(end_line, include_newline=True)
        indent = sm.indent_of(body[1].lineno) if len(body) > 1 else sm.indent_of(body[0].lineno) + "    "
        return offset, indent, True

    first = body[0]
    offset = sm.offset(first.lineno, first.col_offset)
    return offset, sm.indent_of(first.lineno), False


def _collapse_blank_runs(source: str, *, limit: int = 2) -> str:
    """把 3 个以上的连续空行压到 ``limit`` 个（删 import 后可能留下多余空行）。"""
    lines = source.split("\n")
    out: list[str] = []
    run = 0
    for line in lines:
        if line.strip():
            run = 0
            out.append(line)
        else:
            run += 1
            if run > limit:
                continue
            out.append(line)
    return "\n".join(out)


# 具体修补器
class MoveSecretToEnv(Patcher):
    """CG105：把源码里的字符串字面量密钥改为从环境变量读取。"""

    strategy = "move_secret_to_env"
    rule_ids = ("CG105",)
    description = "字符串字面量密钥 → os.environ[<变量名>]，并补 import os"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)

        stmt = _find_literal_secret_assign(tree, lineno)
        if stmt is None:
            raise PatchRefused(
                "该处的密钥不是「简单赋值给字符串字面量」的形态（可能是拼接或函数参数），"
                "自动改写会破坏原意，需人工迁移到配置中心"
            )
        name = _assign_target_name(stmt)
        value = stmt.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            raise PatchRefused("赋值右侧不是字符串字面量")

        env_name = _env_var_name(name)
        start, end = sm.node_span(value)
        edits = [Edit(start, end, f'os.environ["{env_name}"]')]
        edits += _module_edits(sm, tree, want_imports=["os"])
        new_source = apply_edits(source, edits)

        return Patch(
            source=new_source,
            strategy=self.strategy,
            note=f"密钥改为从环境变量 {env_name} 读取；**请同步轮换已写入版本库的旧凭据**",
            before=sm.line_text(lineno),
            after=SourceMap(new_source).line_text(lineno),
            detail={"env_var": env_name},
        )


class ParameterizeSql(Patcher):
    """CG106：把拼接/格式化的 SQL 改写成参数化查询。"""

    strategy = "parameterize_sql"
    rule_ids = ("CG106",)
    description = "f-string/%/.format 拼出的 SQL → 占位符 + 参数元组"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)

        # 情形一：execute(f"... {x} ...") 直接内联插值
        inline = [c for c in node_at_line(tree, lineno, (ast.Call,)) if _is_sql_call(c)]
        if inline:
            call = inline[-1]
            patch = self._inline(tree, sm, call, source)
            if patch is not None:
                return patch

        # 情形二：sql = f"..."; execute(sql) 变量间接传参
        patch = self._indirect(tree, sm, source, lineno)
        if patch is not None:
            return patch

        raise PatchRefused(
            "这条 SQL 的构造方式不是「内联 f-string」或「同函数内变量 + execute」两种可安全改写的形态"
            "（可能是跨函数传递、动态拼接表名等），需人工确认改写边界"
        )

    def _inline(self, tree: ast.AST, sm: SourceMap, call: ast.Call, source: str) -> Patch | None:
        if len(call.args) >= 2:
            raise PatchRefused("该调用已经传入了参数（已参数化），规则可能是误报，需人工确认")
        if call.keywords:
            raise PatchRefused("该调用带有具名参数，改写会丢失语义")
        if not call.args:
            raise PatchRefused("该调用没有位置参数")
        first = call.args[0]
        if not _is_interpolated(first):
            return None

        template, params = _sqlize(first)
        func_src = ast.unparse(call.func)
        replacement = f"{func_src}({_literal(template)}, ({', '.join(params)},))"
        start, end = sm.node_span(call)
        new_source = apply_edits(source, [Edit(start, end, replacement)])
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note=f"改写为参数化查询，{len(params)} 个值经驱动占位符传入",
            before=sm.line_text(call.lineno),
            after=SourceMap(new_source).line_text(call.lineno),
            detail={"template": template, "params": params},
        )

    def _indirect(self, tree: ast.AST, sm: SourceMap, source: str, lineno: int) -> Patch | None:
        assign = None
        for node in node_at_line(tree, lineno, (ast.Assign, ast.AnnAssign)):
            if node.value is not None and _is_interpolated(node.value):
                assign = node
                break
        if assign is None:
            return None
        if len(getattr(assign, "targets", [None])) != 1 or not isinstance(assign.targets[0], ast.Name):
            return None
        variable = assign.targets[0].id
        func = enclosing_function(tree, lineno)
        if func is None:
            return None

        # 在同一个函数里找出用该变量执行 SQL 的调用，且没有传参数
        call = None
        for node in ast.walk(func):
            if not _is_sql_call(node) or not node.args:
                continue
            if isinstance(node.args[0], ast.Name) and node.args[0].id == variable and len(node.args) == 1:
                call = node
                break
        if call is None:
            return None

        template, params = _sqlize(assign.value)
        value_start, value_end = sm.node_span(assign.value)
        call_start, call_end = sm.node_span(call)
        func_src = ast.unparse(call.func)
        edits = [
            Edit(value_start, value_end, _literal(template)),
            Edit(call_start, call_end, f"{func_src}({variable}, ({', '.join(params)},))"),
        ]
        new_source = apply_edits(source, edits)
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note=f"SQL 模板与参数分离，{len(params)} 个值经驱动占位符传入",
            before=sm.line_text(assign.lineno),
            after=SourceMap(new_source).line_text(assign.lineno),
            detail={"template": template, "params": params},
        )


class LogBroadExcept(Patcher):
    """CG202：给宽泛 except 补日志（保留原有的降级行为）。"""

    strategy = "log_broad_except"
    rule_ids = ("CG202",)
    description = "except Exception 静默忽略 → 先 logger.exception(...) 再维持原逻辑"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)
        handlers = nodes_covering_line(tree, lineno, (ast.ExceptHandler,))
        if not handlers:
            raise PatchRefused("定位不到 except 处理块")
        handler = handlers[0]
        if not handler.body:
            raise PatchRefused("except 块为空，无法插入日志语句")

        func = enclosing_function(tree, lineno)
        label = func.name if func is not None else "模块级代码"
        logger_var = _logger_name(tree)
        first = handler.body[0]
        indent = sm.indent_of(first.lineno)
        insert_at = sm.offset(first.lineno, first.col_offset)
        log_line = (
            f'{logger_var}.exception("{label} 中捕获未预期异常，已按原有降级逻辑继续", exc_info=True)'
        )
        edits = [Edit(insert_at, insert_at, log_line + "\n" + indent)]
        edits += _module_edits(sm, tree, want_imports=["logging"], want_logger=True)
        new_source = apply_edits(source, edits)
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note="补充异常日志；**保留原降级返回值**（未擅自改为向上抛出，避免改变线上行为）",
            before=sm.line_text(handler.lineno),
            after=f'{SourceMap(new_source).line_text(handler.lineno)} / {log_line.strip()}',
            detail={"logger": logger_var, "function": label},
        )


class SentinelDefault(Patcher):
    """CG203：可变默认参数改为 None 哨兵。"""

    strategy = "sentinel_default"
    rule_ids = ("CG203",)
    description = "def f(x=[]) → def f(x=None) + 函数体内 if x is None: x = []"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)
        func = enclosing_function(tree, lineno)
        if func is None:
            raise PatchRefused("定位不到包含该默认参数的函数")

        name, default_node = _find_mutable_default(func, lineno)
        if name is None or default_node is None:
            raise PatchRefused("该行没有找到可变对象默认参数（规则可能已失效）")

        default_src = ast.unparse(default_node)
        start, end = sm.node_span(default_node)
        offset, indent, after_docstring = _body_insert_point(sm, func)
        if after_docstring:
            body_text = f"{indent}if {name} is None:\n{indent}    {name} = {default_src}\n"
        else:
            body_text = f"if {name} is None:\n{indent}    {name} = {default_src}\n{indent}"

        new_source = apply_edits(source, [Edit(start, end, "None"), Edit(offset, offset, body_text)])
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note=f"默认值 {default_src} 改为 None 哨兵，函数体内按需初始化，避免跨调用状态污染",
            before=sm.line_text(func.lineno),
            after=SourceMap(new_source).line_text(func.lineno),
            detail={"param": name, "original_default": default_src},
        )


class IdentityComparison(Patcher):
    """CG204：``== None`` → ``is None``。"""

    strategy = "identity_comparison"
    rule_ids = ("CG204",)
    description = "x == None / x != True → x is None / x is not True"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)
        candidates = [
            node
            for node in node_at_line(tree, lineno, (ast.Compare,))
            if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
        ]
        for node in candidates:
            if not _is_identity_target(node):
                continue
            op = "is" if isinstance(node.ops[0], ast.Eq) else "is not"
            replacement = f"{ast.unparse(node.left)} {op} {ast.unparse(node.comparators[0])}"
            start, end = sm.node_span(node)
            new_source = apply_edits(source, [Edit(start, end, replacement)])
            return Patch(
                source=new_source,
                strategy=self.strategy,
                note=f"改用 `{op}` 做单例比较，避免重载 __eq__ 带来的歧义",
                before=sm.line_text(lineno),
                after=SourceMap(new_source).line_text(lineno),
            )
        raise PatchRefused("该行没有找到与 None/True/False 的等值比较")


class ContextManager(Patcher):
    """CG206：``open()`` 未用 with → 改写为 with 语句。"""

    strategy = "context_manager"
    rule_ids = ("CG206",)
    description = "fh = open(p); ...; fh.close() → with open(p) as fh: ..."

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)

        call = next(
            (node for node in node_at_line(tree, lineno, (ast.Call,)) if dotted_name(node.func) == "open"),
            None,
        )
        if call is None:
            raise PatchRefused("该行没有找到直接调用 open() 的表达式")

        holder = _statement_of(tree, call)
        if holder is None:
            raise PatchRefused("open() 调用不是独立语句的一部分，无法安全改写")
        block = enclosing_block(tree, holder)
        index = statement_index(block, holder)
        if index < 0:
            raise PatchRefused("定位不到 open() 所在的语句块")

        target_name = _assign_target_name(holder) if isinstance(holder, ast.Assign) else ""
        if not target_name:
            raise PatchRefused("open() 的返回值没有赋给单个简单变量，无法构造 with ... as")

        close_index = _find_close(block, index, target_name)
        if close_index < 0:
            raise PatchRefused(
                f"没有在后续语句中找到 `{target_name}.close()`，无法确定 with 块的作用范围，"
                "需人工改写"
            )
        if _used_after(block, close_index, target_name):
            raise PatchRefused(f"`{target_name}` 在 close() 之后仍被使用，自动改写会破坏语义")

        open_src = sm.source[slice(*sm.node_span(call))]
        if close_index == index + 1:
            middle = "pass"
        else:
            first = block[index + 1]
            last = block[close_index - 1]
            start = sm.offset(first.lineno, first.col_offset)
            _, end = sm.line_span(getattr(last, "end_lineno", last.lineno), include_newline=False)
            middle = sm.source[start:end]

        base_indent = sm.indent_of(holder.lineno)
        region_start = sm.offset(holder.lineno, holder.col_offset)
        _, region_end = sm.line_span(
            getattr(block[close_index], "end_lineno", block[close_index].lineno), include_newline=False
        )
        body = indent_text(middle, " " * 4)
        replacement = f"with {open_src} as {target_name}:\n{indent_text(body, base_indent)}"
        new_source = apply_edits(source, [Edit(region_start, region_end, replacement)])
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note=f"改写为 `with ... as {target_name}`，异常路径下也能及时释放句柄",
            before=sm.line_text(holder.lineno),
            after=SourceMap(new_source).line_text(holder.lineno),
        )


class ExplicitValidation(Patcher):
    """CG207：``assert`` 运行时校验 → 显式判断并抛 ValueError。"""

    strategy = "explicit_validation"
    rule_ids = ("CG207",)
    description = "assert cond, msg → if not (cond): raise ValueError(msg)"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)
        asserts = node_at_line(tree, lineno, (ast.Assert,))
        if not asserts:
            raise PatchRefused("该行没有找到 assert 语句")
        node = asserts[0]
        if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
            raise PatchRefused("assert 表达式本身带 not，自动改写容易把逻辑写反，需人工确认")

        cond = ast.unparse(node.test)
        message = ast.unparse(node.msg) if node.msg is not None else '"参数校验失败"'
        indent = sm.indent_of(node.lineno)
        replacement = f"if not ({cond}):\n{indent}    raise ValueError({message})"
        start, end = sm.node_span(node)
        new_source = apply_edits(source, [Edit(start, end, replacement)])
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note="改用显式判断 + ValueError；`python -O` 下不再失效",
            before=sm.line_text(lineno),
            after=SourceMap(new_source).line_text(lineno),
        )


class StripTrailingWhitespace(Patcher):
    """CG402：去掉行尾空白。"""

    strategy = "strip_trailing_whitespace"
    rule_ids = ("CG402",)
    description = "行尾空白 → 删除"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lines = source.split("\n")
        changed = False
        for index, line in enumerate(lines):
            stripped = line.rstrip()
            if stripped != line:
                lines[index] = stripped
                changed = True
        if not changed:
            raise PatchRefused("该行已经没有行尾空白（规则可能已失效）")
        new_source = "\n".join(lines)
        return Patch(
            source=new_source,
            strategy=self.strategy,
            note="删除行尾空白",
            before="(行尾含空白)",
            after="(已清理)",
        )


class DropUnusedImport(Patcher):
    """CG403：删除未使用的导入。"""

    strategy = "drop_unused_import"
    rule_ids = ("CG403",)
    description = "未使用的 import → 整行删除或从多名称导入中移除"

    def rewrite(self, source: str, finding: FindingContract) -> Patch:
        lineno = _lineno(finding)
        sm = SourceMap(source)
        tree = parse(source)
        name = _unused_name_from_title(finding.title)
        if not name:
            raise PatchRefused("无法从问题标题中识别出要删除的导入名")

        candidates = node_at_line(tree, lineno, (ast.Import, ast.ImportFrom))
        node = next(
            (n for n in candidates if any(_alias_name(a) == name for a in n.names)),
            None,
        )
        if node is None:
            raise PatchRefused(f"该行没有找到名为 {name!r} 的导入")
        if isinstance(node, ast.ImportFrom) and node.names and node.names[0].name == "*":
            raise PatchRefused("`import *` 无法安全地局部删除")

        remaining = [a for a in node.names if _alias_name(a) != name]
        if remaining:
            replacement = _render_import(node, remaining)
            start, end = sm.node_span(node)
            new_source = apply_edits(source, [Edit(start, end, replacement)])
            note = f"从导入语句中移除未使用的 {name}"
        else:
            start, _ = sm.line_span(node.lineno, include_newline=False)
            _, end = sm.line_span(getattr(node, "end_lineno", node.lineno), include_newline=True)
            new_source = apply_edits(source, [Edit(start, end, "")])
            new_source = _collapse_blank_runs(new_source)
            note = f"删除未使用的导入行 `{name}`"

        return Patch(
            source=new_source,
            strategy=self.strategy,
            note=note,
            before=sm.line_text(lineno),
            after=SourceMap(new_source).line_text(lineno),
            detail={"import": name},
        )


# 注册表
def _build_registry() -> dict[str, Patcher]:
    registry: dict[str, Patcher] = {}
    for patcher_cls in (
        MoveSecretToEnv,
        ParameterizeSql,
        LogBroadExcept,
        SentinelDefault,
        IdentityComparison,
        ContextManager,
        ExplicitValidation,
        StripTrailingWhitespace,
        DropUnusedImport,
    ):
        patcher = patcher_cls()
        for rule_id in patcher.rule_ids:
            registry[rule_id] = patcher
    return registry


PATCHERS: dict[str, Patcher] = _build_registry()


def patcher_for(rule_id: str) -> Patcher | None:
    return PATCHERS.get((rule_id or "").strip().upper())


def supported_rule_ids() -> list[str]:
    return sorted(PATCHERS)


def strategies_table() -> list[dict[str, object]]:
    """供 CLI 与文档生成的策略对照表。"""
    return [
        {
            "rule_id": rule_id,
            "strategy": patcher.strategy,
            "description": patcher.description,
        }
        for rule_id, patcher in sorted(PATCHERS.items())
    ]


def unsupported_reason(rule_id: str) -> str:
    return NOT_SUPPORTED_REASONS.get(
        (rule_id or "").strip().upper(),
        "未知规则，没有对应的自动改写策略",
    )


# 私有辅助
def _find_literal_secret_assign(tree: ast.AST, lineno: int) -> ast.Assign | ast.AnnAssign | None:
    for node in node_at_line(tree, lineno, (ast.Assign, ast.AnnAssign)):
        if node.value is None:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node
    # 赋值语句可能跨行，退一步找覆盖该行的赋值
    for node in nodes_covering_line(tree, lineno, (ast.Assign, ast.AnnAssign)):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node
    return None


def _assign_target_name(node: ast.AST) -> str:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            return target.id
    return ""


def _env_var_name(name: str) -> str:
    """把变量名转成稳定的环境变量名。"""
    candidate = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    for hint in SECRET_NAME_HINTS:
        if hint.upper() in candidate:
            return candidate
    return f"{candidate}_SECRET" if candidate else "APP_SECRET"


def _is_interpolated(node: ast.AST | None) -> bool:
    """判断表达式是不是"把值插进字符串"的写法（f-string 或 % 格式化）。

    codeguard 的 CG106 两种形态都认，修补器也得都认。只认 f-string 就会出现
    "审查报了、修复说不会修"的尴尬。
    """
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
    return False


def _sqlize(node: ast.AST) -> tuple[str, list[str]]:
    """把"字符串插值表达式"拆成 (占位符模板, 参数表达式列表)。"""
    if isinstance(node, ast.JoinedStr):
        return _sqlize_joined(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _sqlize_percent(node)
    raise PatchRefused("无法识别的 SQL 拼接写法")


def _sqlize_joined(node: ast.JoinedStr) -> tuple[str, list[str]]:
    """把 f-string 拆成 (占位符模板, 参数表达式列表)。"""
    parts: list[str] = []
    params: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
        elif isinstance(value, ast.FormattedValue):
            if value.format_spec is not None or value.conversion not in (-1, 115):
                raise PatchRefused(
                    "f-string 带有格式说明符或 !r / !s 转换，转成参数化查询会改变值，需人工处理"
                )
            params.append(ast.unparse(value.value))
            parts.append("?")
        else:  # pragma: no cover - 理论上不会出现
            raise PatchRefused("f-string 含有无法静态解析的片段")
    template = "".join(parts)
    # 原来的写法常带引号包裹占位符（VALUES ('{name}')），参数化后必须去掉这层引号
    template = template.replace("'?'", "?").replace('"?"', "?")
    if not params:
        raise PatchRefused("这条 SQL 里没有需要参数化的值")
    return template, params


def _sqlize_percent(node: ast.BinOp) -> tuple[str, list[str]]:
    """把 ``"… %s" % value`` 拆成 (占位符模板, 参数表达式列表)。"""
    left = node.left
    if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):  # pragma: no cover
        raise PatchRefused("`%` 左侧不是字符串字面量，无法静态改写")
    template = str(left.value)

    right = node.right
    if isinstance(right, ast.Tuple):
        params = [ast.unparse(element) for element in right.elts]
    else:
        params = [ast.unparse(right)]

    for quoted in ("'%s'", '"%s"', "'%d'", '"%d"'):
        template = template.replace(quoted, "?")
    template = template.replace("%s", "?").replace("%d", "?")

    if "?" not in template:
        raise PatchRefused("模板里没有 %s / %d 占位符，不是可参数化的形态")
    if template.count("?") != len(params):
        raise PatchRefused(
            f"占位符数量（{template.count('?')}）与参数个数（{len(params)}）不一致，"
            "自动改写会改变语义，需人工处理"
        )
    return template, params


def _literal(text: str) -> str:
    """生成安全的 Python 字符串字面量（保留中文、正确转义）。"""
    return json.dumps(text, ensure_ascii=False)


def _find_mutable_default(
    func: ast.AST, lineno: int
) -> tuple[str | None, ast.AST | None]:
    args = getattr(func, "args", None)
    if args is None:
        return None, None
    pairs: list[tuple[str, ast.AST]] = []
    positional = list(args.args)
    defaults = list(args.defaults)
    if defaults:
        for arg, default in zip(positional[-len(defaults) :], defaults):
            pairs.append((arg.arg, default))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            pairs.append((arg.arg, default))

    for name, default in pairs:
        if not _is_mutable_literal(default):
            continue
        line = getattr(default, "lineno", getattr(arg, "lineno", 0))
        if line == lineno or lineno == getattr(func, "lineno", 0):
            return name, default
    for name, default in pairs:
        if _is_mutable_literal(default):
            return name, default
    return None, None


def _is_mutable_literal(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"list", "dict", "set"}:
        return not node.args
    return False


def _is_identity_target(node: ast.Compare) -> bool:
    for side in (node.left, node.comparators[0]):
        if isinstance(side, ast.Constant) and (side.value is None or isinstance(side.value, bool)):
            return True
    return False


def _statement_of(tree: ast.AST, node: ast.AST) -> ast.stmt | None:
    """找出"返回值就是这个 node"的那条语句。"""
    source_line = getattr(node, "lineno", None)
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.stmt):
            continue
        value = getattr(candidate, "value", None)
        if value is node:
            return candidate
        if isinstance(candidate, ast.With):
            continue
        if source_line and getattr(candidate, "lineno", None) == source_line and isinstance(
            candidate, (ast.Assign, ast.AnnAssign, ast.Expr)
        ):
            inner = getattr(candidate, "value", None)
            if inner is node or (isinstance(inner, ast.Call) and inner is node):
                return candidate
    return None


def _find_close(block: Sequence[ast.stmt], start: int, name: str) -> int:
    for index in range(start + 1, len(block)):
        stmt = block[index]
        value = getattr(stmt, "value", None)
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "close"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == name
        ):
            return index
    return -1


def _used_after(block: Sequence[ast.stmt], index: int, name: str) -> bool:
    for stmt in block[index + 1 :]:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == name:
                return True
    return False


def _alias_name(alias: ast.alias) -> str:
    return alias.asname or alias.name.split(".")[0]


def _unused_name_from_title(title: str) -> str:
    match = re.search(r"[：:]\s*([A-Za-z_][\w.]*)", title or "")
    return match.group(1) if match else ""


def _render_import(node: ast.Import | ast.ImportFrom, aliases: Sequence[ast.alias]) -> str:
    names = ", ".join(
        f"{a.name} as {a.asname}" if a.asname else a.name for a in aliases
    )
    if isinstance(node, ast.Import):
        return f"import {names}"
    dots = "." * (getattr(node, "level", 0) or 0)
    module = node.module or ""
    return f"from {dots}{module} import {names}"


__all__ = [
    "NOT_SUPPORTED_REASONS",
    "PATCHERS",
    "Patch",
    "Patcher",
    "patcher_for",
    "strategies_table",
    "supported_rule_ids",
    "unsupported_reason",
]
