"""错误码落地矩阵 —— 任务 42。

一个错误码在产品里要落到**五个**地方，缺一处就会「同一个错误在不同层
显示三种说法」：

==================  ==========================================================
落地维度            含义
==================  ==========================================================
触发点              在哪儿会走到这个错误码（代码里真的写着 ``ErrorCode.X``）
中文文案            错误码表里的模板 —— 三处可见面的**唯一**来源
可重试性            4xx 参数类一律不可重试：不消耗退避预算
是否进指标分母      失败例永远不进分母；降级类仍进但单列
用户可见位置        表格 ``failure_reason`` 列 / 终端输出 / ``summary.json``
==================  ==========================================================

本模块提供三件事：

1. :func:`build_matrix` —— 落地矩阵（声明 + 代码里实际触发点）；
2. :func:`failure_breakdown` —— **按错误码汇总失败例**，``summary.json``
   与终端输出共用同一份，三处文案因此必然一致；
3. :func:`scan_hardcoded_error_text` —— 静态扫描，断言业务代码里
   **没有**把错误文案硬编码一份（文案只从 :mod:`nodule_eval.errors` 取）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import (
    ERROR_CODES,
    ERROR_TABLE,
    SURFACE_SUMMARY,
    SURFACE_TABLE,
    SURFACE_TERMINAL,
    USER_VISIBLE_SURFACES,
    ErrorCode,
    format_message,
    spec,
)

#: 本包根目录（``src/nodule_eval``）。
PACKAGE_ROOT = Path(__file__).resolve().parent
#: 源码根目录（``src``）。
SRC_ROOT = PACKAGE_ROOT.parent

#: 中日韩统一表意文字（用于识别中文文案）。
_CJK = re.compile(r"[㐀-䶿一-鿿]")

#: 触发点扫描要跳过的文件（本模块自己与错误码表本身）。
_SKIP_SCAN_FILES = frozenset({"errors.py", "error_matrix.py"})


# --------------------------------------------------------------------------
# 落地矩阵
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ErrorMatrixRow:
    """一个错误码的落地矩阵行。"""

    error_code: str
    trigger_points: tuple[str, ...]
    message_template: str
    retryable: bool
    counts_toward_metrics: bool
    visible_in: tuple[str, ...]
    code_sites: tuple[str, ...]

    @property
    def reachable(self) -> bool:
        """代码里真的写着这个错误码（不是登记了却没人用）。"""

        return bool(self.code_sites)

    @property
    def in_table(self) -> bool:
        return any(SURFACE_TABLE in item for item in self.visible_in)

    @property
    def in_terminal(self) -> bool:
        return any(SURFACE_TERMINAL in item for item in self.visible_in)

    @property
    def in_summary(self) -> bool:
        return any(SURFACE_SUMMARY in item for item in self.visible_in)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "trigger_points": list(self.trigger_points),
            "message_template": self.message_template,
            "retryable": self.retryable,
            "counts_toward_metrics": self.counts_toward_metrics,
            "visible_in": list(self.visible_in),
            "code_sites": list(self.code_sites),
        }


def _code_sites(root: Path | None = None) -> dict[str, list[str]]:
    """扫源码找出 ``ErrorCode.X`` 的真实出现位置（``文件:行``）。

    这是落地矩阵的「触发点」一列的**代码证据** —— 声明的触发点与代码里
    实际写着的对不上，说明有一处是登记了却没人用（或用了却没登记）。
    """

    base = Path(root) if root is not None else SRC_ROOT
    found: dict[str, list[str]] = {}
    for path in sorted(base.rglob("*.py")):
        if path.name in _SKIP_SCAN_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 源码坏了另说
            continue
        rel = path.relative_to(base).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ErrorCode"
            ):
                found.setdefault(node.attr, []).append(f"{rel}:{node.lineno}")
    return found


def build_matrix(root: Path | None = None) -> tuple[ErrorMatrixRow, ...]:
    """落地矩阵 —— 声明（错误码表）+ 代码证据（源码扫描）合成一张表。"""

    sites = _code_sites(root)
    rows: list[ErrorMatrixRow] = []
    for code in ERROR_CODES:
        s = spec(code)
        rows.append(
            ErrorMatrixRow(
                error_code=code,
                trigger_points=s.trigger_points,
                message_template=s.message,
                retryable=s.retryable,
                counts_toward_metrics=s.counts_toward_metrics,
                visible_in=s.visible_in,
                code_sites=tuple(sites.get(code, ())),
            )
        )
    return tuple(rows)


def render_matrix_markdown(root: Path | None = None) -> str:
    """把落地矩阵渲染成 Markdown（``docs/error_matrix.md``）。"""

    rows = build_matrix(root)
    lines: list[str] = [
        "# 错误码落地矩阵",
        "",
        "本文件由 `python scripts/gen_error_matrix.py` 生成，请勿手工编辑。",
        "",
        "一个错误码在产品里要落到五个地方：**在哪儿触发**、**中文文案**、"
        "**能不能重试**、**进不进指标分母**、**用户在哪儿看到**。"
        "缺一处就会出现「同一个错误在不同层显示三种说法」。",
        "",
        "文案模板的**唯一来源**是 `src/nodule_eval/errors.py`；"
        "表格 `failure_reason` 列、终端输出、`summary.json` 三处"
        "共用 `errors.user_text()`，因此同一错误码必然是同一句话。",
        "",
        "## 落地矩阵",
        "",
        "| 错误码 | 触发点 | 中文文案模板 | 可重试 | 进指标分母 | 用户可见位置 | 代码位置 |",
        "|--------|--------|--------------|--------|------------|--------------|----------|",
    ]
    for row in rows:
        triggers = "<br>".join(t for t in row.trigger_points) or "—"
        sites = "<br>".join(row.code_sites[:4]) or "—"
        if len(row.code_sites) > 4:
            sites += f"<br>…共 {len(row.code_sites)} 处"
        visible = "<br>".join(row.visible_in) or "—"
        lines.append(
            "| `{code}` | {triggers} | `{message}` | {retry} | {metrics} | {visible} | {sites} |".format(
                code=row.error_code,
                triggers=triggers,
                message=row.message_template.replace("|", "\\|"),
                retry="是" if row.retryable else "否",
                metrics="是" if row.counts_toward_metrics else "否",
                visible=visible,
                sites=sites,
            )
        )
    lines += [
        "",
        "## 用户可见的三个面",
        "",
        "| 面 | 落点 | 谁产出文案 |",
        "|----|------|------------|",
        "| 表格 | `failure_reason` 列（评估表新增列） | `errors.user_text()` |",
        "| 终端 | `run` 子命令输出里的「失败分布」 | `errors.user_text()` |",
        "| 结论 | `summary.json` 的 `failures.by_code` | `errors.user_text()` |",
        "",
        "三个面读的是**同一个函数**的返回值 —— 所以文案一致不是靠人工核对，"
        "是结构上不可能不一致。`tests/test_error_matrix.py` 每次都真跑一遍"
        "并逐字比对。",
        "",
        "## 重试口径",
        "",
        "4xx 参数类**不重试**：重试必然还是被拒，把退避预算浪费掉只会让"
        "这一例拖慢整轮。5xx / 429 与超时才退避重试（最多 3 次）。"
        "重试口径由 `errors.py` 的 `retryable` 字段声明，"
        "`adapters/http.py` 的实际行为由 `tests/test_error_matrix.py` 核对。",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 按错误码汇总失败例 —— summary.json 与终端输出共用
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FailureCodeEntry:
    """一个错误码在本轮里的汇总。"""

    error_code: str
    count: int
    message: str
    template: str
    samples: tuple[str, ...]
    retryable: bool
    counts_toward_metrics: bool
    visible_in: tuple[str, ...]
    trigger_points: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "count": self.count,
            "message": self.message,
            "template": self.template,
            "samples": list(self.samples),
            "retryable": self.retryable,
            "counts_toward_metrics": self.counts_toward_metrics,
            "visible_in": list(self.visible_in),
            "trigger_points": list(self.trigger_points),
        }


def _row_fields(row: Any) -> tuple[str, str]:
    """从逐例行取 ``(error_code, failure_reason)``。"""

    code = getattr(row, "error_code", "") or ""
    reason = getattr(row, "failure_reason", "") or ""
    return str(code), str(reason)


def failure_breakdown(rows: Iterable[Any]) -> dict[str, Any]:
    """按错误码汇总失败例 —— ``summary.json`` 的 ``failures`` 段。

    ``message`` 取该码**首个失败例**的 ``failure_reason`` —— 与表格那一格
    逐字相同；``samples`` 列出该码在本轮出现的全部不同文案。
    """

    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for row in rows:
        code, reason = _row_fields(row)
        if not code:
            continue
        if code not in buckets:
            buckets[code] = []
            order.append(code)
        buckets[code].append(reason)

    by_code: list[dict[str, Any]] = []
    for code in sorted(order, key=lambda c: (-len(buckets[c]), c)):
        samples = tuple(dict.fromkeys(reason for reason in buckets[code] if reason))
        s = spec(code)
        by_code.append(
            FailureCodeEntry(
                error_code=code,
                count=len(buckets[code]),
                message=samples[0] if samples else "",
                template=s.message,
                samples=samples,
                retryable=s.retryable,
                counts_toward_metrics=s.counts_toward_metrics,
                visible_in=s.visible_in,
                trigger_points=s.trigger_points,
            ).to_dict()
        )
    return {
        "total": sum(len(v) for v in buckets.values()),
        "by_code": by_code,
    }


def render_failure_lines(
    payload: Mapping[str, Any],
    *,
    label: str = "失败分布",
    indent: str = " " * 16,
) -> list[str]:
    """把失败汇总渲染成终端行 —— 与 ``summary.json`` 同源。

    第一个错误码带 ``label``，其余缩进对齐；一个错误码都没有时返回空列表
    （终端不该多出一行「失败分布：」的空话）。
    """

    entries = list(payload.get("by_code") or ())
    if not entries:
        return []
    lines: list[str] = []
    for index, entry in enumerate(entries):
        message = entry.get("message") or entry.get("template") or ""
        body = f"{entry.get('error_code')} × {entry.get('count')} —— {message}"
        if index == 0:
            # 首行带标签（标签 + 冒号 + 空格 = 16 列），续行缩进对齐到同一列。
            lines.append(f"{label}      : {body}")
        else:
            lines.append(f"{indent}{body}")
    return lines


def sample_matches_template(code: str, text: str) -> bool:
    """一段失败文案是否与该错误码的模板对得上。

    判定口径：文案的**静态前缀**必须与模板的静态前缀一致 ——
    同一错误码换个说法（模板前缀变了）在这里就会被抓住。
    """

    template = spec(code).message
    prefix = template.split("{", 1)[0].strip()
    if not prefix:
        return True
    return text.startswith(prefix)


# --------------------------------------------------------------------------
# 静态扫描：错误文案不许硬编码
# --------------------------------------------------------------------------
def _template_fragments(template: str) -> tuple[str, ...]:
    """拆出模板里的**静态片段**（占位符之间的固定文字）。

    只取含中文且不少于 4 个汉字的片段 —— 「（」「）」这种标点片段
    没有判别力，硬编码了也说明不了问题。
    """

    fragments: list[str] = []
    for part in template.split("{"):
        fragment = part.split("}", 1)[0] if "}" in part else part
        fragment = fragment.strip()
        if len(_CJK.findall(fragment)) >= 4:
            fragments.append(fragment)
    return tuple(fragments)


def _code_string_literals(tree: ast.AST) -> list[str]:
    """取一棵语法树里**代码用到的**字符串常量（排除文档字符串）。

    注释不在语法树里；文档字符串是「解释这段代码在干什么」，
    不是「给用户看的文案」，两者都不该算硬编码。
    """

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    literals: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            literals.append(node.value)
    return literals


def scan_hardcoded_error_text(root: Path | None = None) -> list[dict[str, Any]]:
    """扫源码：**错误文案不许在业务代码里再写一份**。

    两条红线：

    1. 错误码表里模板的静态片段，不得出现在 ``errors.py`` 之外的源码里
       —— 出现即说明有人把规范文案抄了一份，改模板时就会漏改。
    2. ``failure_reason`` / ``error_message`` 不得直接赋中文常量
       —— 用户可见文案必须走 ``errors.user_text()``。

    返回违例清单（空清单 = 通过）。
    """

    base = Path(root) if root is not None else SRC_ROOT
    fragments: list[tuple[str, str]] = []
    for code in ERROR_CODES:
        for fragment in _template_fragments(ERROR_TABLE[code].message):
            fragments.append((code, fragment))

    violations: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*.py")):
        if path.name in _SKIP_SCAN_FILES:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - 源码坏了另说
            continue
        rel = path.relative_to(base).as_posix()

        # 只看**代码里的字符串常量** —— 注释与文档字符串里提到规范文案
        # 是正常的（那是在解释规则），抄一份当文案用才是问题。
        literals = _code_string_literals(tree)

        # 红线 1：模板静态片段被抄了一份。
        for code, fragment in fragments:
            for literal in literals:
                if fragment in literal:
                    violations.append(
                        {
                            "rule": "template_fragment_duplicated",
                            "file": rel,
                            "error_code": code,
                            "fragment": fragment,
                            "detail": "错误码表的规范文案被硬编码在业务代码里",
                        }
                    )
                    break

        # 红线 2：用户可见文案直接赋中文常量。
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        targets.append(target.id)
                    elif isinstance(target, ast.Attribute):
                        targets.append(target.attr)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets.append(node.target.id)
            for name in targets:
                if name not in ("failure_reason", "error_message"):
                    continue
                value = node.value
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and _CJK.search(value.value)
                ):
                    violations.append(
                        {
                            "rule": "user_visible_text_literal",
                            "file": rel,
                            "line": getattr(node, "lineno", 0),
                            "detail": f"{name} 直接赋了中文常量，应走 errors.user_text()",
                        }
                    )
    return violations


def retry_policy_by_status() -> dict[int, bool]:
    """按错误码表推出每个 HTTP 状态该不该重试。

    4xx（含 429 之外的参数类）一律不可重试；5xx 与 429 可重试。
    这是**声明**出来的口径，``adapters/http.py`` 的实际行为由测试核对
    （两边对不上就是有人改了一边）。
    """

    policy: dict[int, bool] = {}
    for status in range(400, 500):
        policy[status] = spec(ErrorCode.INFERENCE_4XX).retryable
    for status in (429, 500, 502, 503, 504):
        policy[status] = spec(ErrorCode.INFERENCE_5XX).retryable
    return policy


def visible_surface_matrix() -> list[dict[str, Any]]:
    """每个错误码落在用户可见三个面的哪几个（供矩阵文档与测试核对）。"""

    rows: list[dict[str, Any]] = []
    for code in ERROR_CODES:
        s = spec(code)
        rows.append(
            {
                "error_code": code,
                "in_table": any(SURFACE_TABLE in item for item in s.visible_in),
                "in_terminal": any(SURFACE_TERMINAL in item for item in s.visible_in),
                "in_summary": any(SURFACE_SUMMARY in item for item in s.visible_in),
            }
        )
    return rows


__all__ = [
    "ErrorMatrixRow",
    "FailureCodeEntry",
    "USER_VISIBLE_SURFACES",
    "build_matrix",
    "failure_breakdown",
    "render_failure_lines",
    "render_matrix_markdown",
    "retry_policy_by_status",
    "sample_matches_template",
    "scan_hardcoded_error_text",
    "visible_surface_matrix",
]
