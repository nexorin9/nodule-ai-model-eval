"""逐例回填 —— 把引擎的判定写回评估表的**新增六列**（任务 46）。

**这张表有两层**（源产品「原始件 / 展示件分离」原则的落法，见
:mod:`nodule_eval.sheets.template` 模块文档）：

- **既有三列**（``accession_no`` / ``gold_standard_text`` / ``image_ref``）
  是科室维护的**原始件** —— 本模块**一个字都不改**，回填前后逐单元格比对断言。
- **新增六列**（``verdict`` / ``model_findings`` / ``failure_reason`` /
  ``tool_call_id`` / ``needs_review`` / ``normalize_source``）
  是引擎回填的**展示件** —— 只有这一层被写。

**为什么既有列必须逐格比对而不是「我们没写那儿」**：回填是唯一一次
「程序往人写的表里写东西」的动作，一次列错位就会把金标准原文覆盖成判定值 ——
原文一旦被改，事后没法证明当初是按什么判的。断言写死在
:func:`assert_existing_columns_unchanged`，**比对不过就不落盘**。

**中断不留半截表**（``按行原子回填``）：整轮回填先在内存里改完、
逐格核对既有列，再**一次性原子替换**文件（临时文件 → ``os.replace``）。
中途崩了 / 被 Ctrl-C 了 / 磁盘写满了 —— 原表**一个字节都没变**，
不会出现「前 80 例填好了、第 81 例填到一半」的半截状态。

**三态**（文案在 :mod:`nodule_eval.sheets.status`）：

- **加载态** —— 引擎跑的过程中写状态文件，宏轮询刷新状态栏；
- **空态** —— 本表没圈定病例 → 状态栏写「本表未圈定病例」；
  本次没漏诊 → 漏诊区写「本次未发现漏判」（不是一片空白）；
- **错误态** —— 失败例在明细区按失败底色标出，状态栏给**错误码 + 处置提示**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from ..errors import ErrorCode, NoduleEvalError
from . import tokens
from .columns import ALL_COLUMNS, COLUMN_LABELS_ZH, EXISTING_COLUMNS
from .macro_contract import (
    BACKFILL_COLUMNS,
    NEEDS_REVIEW_NO,
    NEEDS_REVIEW_YES,
    SUMMARY_CELL_ADDRESSES,
    summary_values,
)
from .read import HEADER_SEARCH_ROWS, _normalize_key, cell_text
from .sections import SECTION_MARKER_MISSED, SECTION_MARKER_STATUS
from .status import (
    EMPTY_MISSED_TEXT,
    STATUS_PREFIX,
    RunStatus,
    STATE_COMPLETED,
    status_bar_text,
)
from .template import DETAIL_SHEET_NAMES, find_sections

#: 空态取值。
EMPTY_STATE_NONE = ""
EMPTY_STATE_NO_CASES = "no_cases"
EMPTY_STATE_NO_MISSES = "no_misses"

#: 漏诊区各列（契约列名 → 漏诊区的列号）。
MISSED_AREA_COLUMNS: tuple[tuple[str, int], ...] = (
    ("accession_no", 1),
    ("gold_standard_summary", 2),
    ("model_findings", 3),
    ("missed_reason", 4),
    ("tool_call_id", 6),
    ("rule_degraded", 8),
)


@dataclass(frozen=True)
class BackfillResult:
    """回填结果 —— 调用方（CLI / 宏）据此汇报与自检。"""

    path: Path
    sheet_name: str
    written_rows: int = 0
    failed_rows: int = 0
    updated_cells: int = 0
    summary_cells: int = 0
    missed_rows: int = 0
    empty_state: str = EMPTY_STATE_NONE
    status_text: str = ""
    existing_columns_unchanged: bool = True
    checked_cells: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sheet_name": self.sheet_name,
            "written_rows": self.written_rows,
            "failed_rows": self.failed_rows,
            "updated_cells": self.updated_cells,
            "summary_cells": self.summary_cells,
            "missed_rows": self.missed_rows,
            "empty_state": self.empty_state,
            "status_text": self.status_text,
            "existing_columns_unchanged": self.existing_columns_unchanged,
            "checked_cells": self.checked_cells,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# 定位
# --------------------------------------------------------------------------


def _resolve_detail_sheet(workbook) -> Worksheet:
    """找明细页（``评估表`` / ``逐例明细`` 都认，都没有就取第一页）。"""

    for name in DETAIL_SHEET_NAMES:
        if name in workbook.sheetnames:
            return workbook[name]
    return workbook[workbook.sheetnames[0]]


#: 契约列名 → 归一后的键（**既有列 + 新增六列**）。
_ALL_COLUMN_KEYS: dict[str, str] = {
    name: _normalize_key(name) for name in ALL_COLUMNS
}

#: 归一后的键 → 契约列名。
_ALL_COLUMN_BY_KEY: dict[str, str] = {
    key: name for name, key in _ALL_COLUMN_KEYS.items()
}

#: 中文列名（「字段说明」页那一套）归一后的键 → 契约列名。
_ALL_ALIAS_KEYS: dict[str, str] = {
    _normalize_key(label): name
    for name, label in COLUMN_LABELS_ZH.items()
}


def _map_all_columns(row: Sequence[Any]) -> dict[str, int]:
    """把一行表头映射成「契约列名 → 列号（0 基）」—— **九列全认**。

    读层（:mod:`nodule_eval.sheets.read`）只认**既有三列**（它只读输入侧）；
    回填要写**新增六列**，所以这里另有一份全列映射 ——
    但**归一规则与优先级跟读层完全一致**（契约名逐字命中 → 中文别名 → 前缀命中），
    两处不各写一套规则：读层认得的表头，回填也认得；
    读层认不得的，回填会**指名报错**而不是猜一列。
    """

    exact: dict[str, int] = {}
    alias: dict[str, int] = {}

    for index, value in enumerate(row):
        if value is None:
            continue
        key = _normalize_key(cell_text(value))
        if not key:
            continue

        name = _ALL_COLUMN_BY_KEY.get(key)
        if name is not None:
            exact.setdefault(name, index)
            continue

        name = _ALL_ALIAS_KEYS.get(key)
        if name is not None:
            alias.setdefault(name, index)
            continue

        for candidate in ALL_COLUMNS:
            if key.startswith(_ALL_COLUMN_KEYS[candidate]):
                alias.setdefault(candidate, index)
                break
        else:
            for label, candidate in _ALL_ALIAS_KEYS.items():
                if key.startswith(label):
                    alias.setdefault(candidate, index)
                    break

    merged = dict(alias)
    merged.update(exact)
    return merged


def _find_full_header_row(rows: Sequence[Sequence[Any]]) -> tuple[int, dict[str, int]]:
    """找表头行（**九列命中最多**的那一行）。

    读层的 :func:`~nodule_eval.sheets.read._find_header_row` 只数既有列；
    回填要按**新增列**定位，所以这里数的是全列 —— 但仍是**同一份归一规则**。
    """

    best_row = 0
    best_map: dict[str, int] = {}
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS], start=1):
        mapping = _map_all_columns(row)
        if len(mapping) > len(best_map):
            best_row = index
            best_map = mapping
        if len(best_map) >= len(ALL_COLUMNS):
            break
    return best_row, best_map


def _locate_columns(worksheet: Worksheet) -> tuple[int, dict[str, int]]:
    """定位表头行与列号映射（既有三列 + 新增六列）。"""

    rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
    header_row, mapping = _find_full_header_row(rows)
    if not mapping:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                "评估表找不到表头行：前若干行里没有既有列"
                "（accession_no / gold_standard_text / image_ref）"
            ),
        )
    missing = [name for name in ("accession_no", "gold_standard_text") if name not in mapping]
    if missing:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=(
                f"评估表表头行（第 {header_row} 行）缺少既有列：{'、'.join(missing)}"
            ),
        )
    return header_row, mapping


def _column_index(mapping: Mapping[str, int], name: str) -> int:
    """契约列名 → 1 基列号；表里没有这一列就报错并点名。"""

    if name not in mapping:
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"评估表里没有「{name}」这一列，无法回填",
        )
    return int(mapping[name]) + 1


# --------------------------------------------------------------------------
# 既有列快照与比对（**回填的红线**）
# --------------------------------------------------------------------------


def snapshot_existing_columns(
    worksheet: Worksheet,
    mapping: Mapping[str, int],
    *,
    first_row: int,
    last_row: int,
) -> dict[tuple[int, int], Any]:
    """既有三列的**逐格快照**（回填前）。

    快照按 ``(行号, 列号)`` 存值 —— 空格子也要存（``None``），
    否则「原来有值、现在被清空」这种改动就发现不了。
    """

    snapshot: dict[tuple[int, int], Any] = {}
    for name in EXISTING_COLUMNS:
        if name not in mapping:
            continue
        column = int(mapping[name]) + 1
        for row in range(first_row, last_row + 1):
            snapshot[(row, column)] = worksheet.cell(row=row, column=column).value
    return snapshot


def assert_existing_columns_unchanged(
    before: Mapping[tuple[int, int], Any],
    after: Mapping[tuple[int, int], Any],
) -> None:
    """断言既有列逐格未变 —— **对不上就报错，不落盘**。

    既有列是科室写的原始件，金标准原文尤其不能被改。
    这一条断言是「回填只写新增六列」的**可核对证据**，不是口头保证。
    """

    changed = [
        (row, column)
        for (row, column), value in before.items()
        if after.get((row, column)) != value
    ]
    if not changed:
        return
    row, column = changed[0]
    raise NoduleEvalError(
        ErrorCode.CONTRACT_INVALID,
        detail=(
            f"回填改动了既有列（第 {row} 行第 {column} 列），已放弃落盘："
            f"原值 {before[(row, column)]!r} → 现值 {after.get((row, column))!r}；"
            f"既有列（金标准原文等）只读，产品只写新增六列"
        ),
    )


# --------------------------------------------------------------------------
# 取值辅助
# --------------------------------------------------------------------------


def _row_value(row: Any, key: str) -> Any:
    """从逐例行（字典或 dataclass）里取一个字段。"""

    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _text(value: Any) -> str:
    """落表用的文本（``None`` 落空串，不留 ``None`` 字面量）。"""

    if value is None:
        return ""
    return str(value)


def _needs_review_text(value: Any) -> str:
    """``needs_review`` 的表内写法（是 / 否）。"""

    if isinstance(value, str):
        return NEEDS_REVIEW_YES if value.strip() in ("是", "true", "True", "1") else NEEDS_REVIEW_NO
    return NEEDS_REVIEW_YES if bool(value) else NEEDS_REVIEW_NO


# --------------------------------------------------------------------------
# 回填
# --------------------------------------------------------------------------


def backfill_workbook(
    path: Path | str,
    case_rows: Sequence[Any],
    *,
    summary: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    missed_list: Sequence[Any] | None = None,
    status: RunStatus | None = None,
    sheet_name: str | None = None,
) -> BackfillResult:
    """把一轮评估的结果回填进评估表。

    **只写新增六列**（+ 汇总区 + 漏诊区 + 状态栏），既有三列一个字都不动；
    落盘走**原子替换**，中断不会留下半截表。

    参数
    ----
    path:
        评估表路径（``.xlsm`` / ``.xlsx``）。``.xlsm`` 会保留宏工程
        （``keep_vba=True``）—— 回填不能把表里的宏弄丢。
    case_rows:
        逐例判定行（``result.json`` 的 ``case_rows``；字典或 dataclass 都行）。
        每行带 ``row_no``（它对应表里的第几行）。
    summary / metrics:
        run 级结论与指标 —— 写进表头汇总区的**固定地址**。
    missed_list:
        漏诊清单 —— 写进漏诊明细区；为空时写「本次未发现漏判」。
    status:
        运行状态（:class:`~nodule_eval.sheets.status.RunStatus`）。
        不给就按本轮回填的实际情况合成一条。
    """

    file_path = Path(path)
    if not file_path.is_file():
        raise NoduleEvalError(
            ErrorCode.CONTRACT_INVALID,
            detail=f"评估表文件不存在：{file_path}",
        )

    workbook = load_workbook(file_path, keep_vba=file_path.suffix.lower() == ".xlsm")
    try:
        worksheet = (
            workbook[sheet_name]
            if sheet_name and sheet_name in workbook.sheetnames
            else _resolve_detail_sheet(workbook)
        )
        header_row, mapping = _locate_columns(worksheet)
        sections = find_sections(worksheet)
        first_row = header_row + 1
        # **明细区的最后一行** —— 到漏诊区之前为止。
        # 漏诊区与状态栏也在第一列上，但它们不是「科室填的既有列」，
        # 把它们算进「既有列零改动」的核对范围，回填自己写漏诊清单就会把自己拦下来。
        detail_last_row = sections.get("missed_section_row", worksheet.max_row + 1) - 1
        last_row = max(detail_last_row, first_row)

        before = snapshot_existing_columns(
            worksheet, mapping, first_row=first_row, last_row=last_row
        )

        warnings: list[str] = []
        written_rows = 0
        failed_rows = 0
        updated_cells = 0
        empty_state = EMPTY_STATE_NONE

        if not case_rows:
            # **空态 · 本表未圈定病例** —— 一行都没写，只在状态栏说明。
            empty_state = EMPTY_STATE_NO_CASES
        else:
            written_rows, failed_rows, updated_cells = _write_case_rows(
                worksheet, mapping, case_rows, header_row, warnings, last_row=last_row
            )

        summary_cells = _write_summary_cells(worksheet, summary, metrics)

        missed_rows = 0
        if missed_list is not None:
            missed_rows = _write_missed_area(worksheet, sections, missed_list)
            if not missed_list and empty_state == EMPTY_STATE_NONE:
                empty_state = EMPTY_STATE_NO_MISSES

        status_obj = status or _synthesize_status(
            run_id="",
            total=len(case_rows),
            written_rows=written_rows,
            failed_rows=failed_rows,
            empty_state=empty_state,
        )
        status_text = status_bar_text(status_obj)
        _write_status_bar(worksheet, sections, status_text)

        # 既有列必须逐格未变 —— 对不上就不落盘（原表保持原样）。
        after = snapshot_existing_columns(
            worksheet, mapping, first_row=first_row, last_row=last_row
        )
        assert_existing_columns_unchanged(before, after)

        _save_atomic(workbook, file_path)

        return BackfillResult(
            path=file_path,
            sheet_name=worksheet.title,
            written_rows=written_rows,
            failed_rows=failed_rows,
            updated_cells=updated_cells,
            summary_cells=summary_cells,
            missed_rows=missed_rows,
            empty_state=empty_state,
            status_text=status_text,
            existing_columns_unchanged=True,
            checked_cells=len(before),
            warnings=tuple(warnings),
        )
    finally:
        workbook.close()


def _write_case_rows(
    worksheet: Worksheet,
    mapping: Mapping[str, int],
    case_rows: Sequence[Any],
    header_row: int,
    warnings: list[str],
    *,
    last_row: int = 0,
) -> tuple[int, int, int]:
    """逐例回填（**只写新增六列**）—— 返回（写入行数, 失败例数, 写入格数）。

    行号取逐例行的 ``row_no``；缺 ``row_no`` 时按检查号在表里找那一行
    （找不到就跳过并记一条告警，**不猜行号** —— 猜错就是写到别人那行上）。

    ``last_row`` 是**明细区的最后一行**（到漏诊区之前为止）——
    按检查号找行时不许越过它，否则会命中漏诊区里的那一行。
    """

    columns = {name: _column_index(mapping, name) for name in BACKFILL_COLUMNS}
    written = 0
    failed = 0
    cells = 0

    for row in case_rows:
        accession_no = _text(_row_value(row, "accession_no"))
        verdict = _text(_row_value(row, "verdict"))
        row_no = _row_value(row, "row_no")
        try:
            target_row = int(row_no) if row_no else 0
        except (TypeError, ValueError):
            target_row = 0
        if target_row <= header_row:
            target_row = _find_row_by_accession(
                worksheet, mapping, accession_no, header_row, last_row
            )
        if target_row <= header_row:
            warnings.append(
                f"检查号 {accession_no or '（空）'} 在表里找不到对应行，已跳过（不猜行号）"
            )
            continue

        values = {
            "verdict": verdict,
            "model_findings": _text(_row_value(row, "model_findings")),
            "failure_reason": _text(_row_value(row, "failure_reason")),
            "tool_call_id": _text(_row_value(row, "tool_call_id")),
            "needs_review": _needs_review_text(_row_value(row, "needs_review")),
            "normalize_source": _text(_row_value(row, "normalize_source")),
        }
        for name, value in values.items():
            cell = worksheet.cell(row=target_row, column=columns[name])
            cell.value = value
            cell.font = tokens.body_font()
            cell.alignment = tokens.body_alignment("left", wrap_text=True)
            cell.border = tokens.data_border()
            cells += 1

        if verdict == "failed":
            # **错误态**：失败例的失败原因格按失败底色标出 + 告警字色 ——
            # 打开表就能一眼看出哪几例没跑成，不必去读一列文字。
            failed += 1
            reason_cell = worksheet.cell(row=target_row, column=columns["failure_reason"])
            fill = tokens.verdict_fill("failed")
            if fill is not None:
                reason_cell.fill = fill
            reason_cell.font = tokens.body_font(color=tokens.COLOR_ALERT)

        written += 1

    return written, failed, cells


def _find_row_by_accession(
    worksheet: Worksheet,
    mapping: Mapping[str, int],
    accession_no: str,
    header_row: int,
    last_row: int = 0,
) -> int:
    """按检查号在表里找行号（找不到返回 0）。

    逐例行自带 ``row_no`` 时根本走不到这里；这一条是**兜底**
    （比如从 CSV 直跑、没有行号的场合）。
    搜索范围只到**明细区末行** —— 漏诊区里也有检查号，不能命中那儿。
    """

    if not accession_no or "accession_no" not in mapping:
        return 0
    column = int(mapping["accession_no"]) + 1
    limit = last_row if last_row else worksheet.max_row
    for row in range(header_row + 1, limit + 1):
        value = worksheet.cell(row=row, column=column).value
        if value is None:
            continue
        if cell_text(value).strip() == accession_no.strip():
            return row
    return 0


def _write_summary_cells(
    worksheet: Worksheet,
    summary: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | None,
) -> int:
    """写表头汇总区的**固定地址**（15 格）。

    汇总区没数据时写占位符「—」—— 留空会让人以为漏算了。
    """

    if not summary and not metrics:
        return 0
    values = summary_values(summary or {}, metrics or {})
    written = 0
    for key, address in SUMMARY_CELL_ADDRESSES.items():
        if key not in values:
            continue
        cell = worksheet[address]
        cell.value = values[key]
        if key in ("sensitivity", "specificity", "ppv", "npv"):
            tokens.style_metric_cell(cell)
        elif key == "degrade_note":
            tokens.style_summary_note_cell(cell)
        else:
            tokens.style_count_cell(cell)
        written += 1
    return written


def _write_missed_area(
    worksheet: Worksheet,
    sections: Mapping[str, int],
    missed_list: Sequence[Any],
) -> int:
    """写漏诊明细区。

    **条数须与漏判数 FN 相等** —— 复核的人数一遍清单就能手算敏感度。
    没有漏诊时写一句「本次未发现漏判」，不是一片空白。
    """

    missed_section_row = sections.get("missed_section_row")
    if missed_section_row is None:
        return 0
    first_data_row = missed_section_row + 2

    if not missed_list:
        cell = worksheet.cell(row=first_data_row, column=1, value=EMPTY_MISSED_TEXT)
        tokens.style_alert_cell(cell)
        for column in range(2, 10):
            worksheet.cell(row=first_data_row, column=column).fill = tokens.section_fill()
        return 0

    written = 0
    for offset, item in enumerate(missed_list):
        row = first_data_row + offset
        for name, column in MISSED_AREA_COLUMNS:
            value = _row_value(item, name)
            if name == "rule_degraded":
                text = NEEDS_REVIEW_YES if bool(value) else NEEDS_REVIEW_NO
            else:
                text = _text(value)
            cell = worksheet.cell(row=row, column=column, value=text)
            cell.font = tokens.body_font()
            cell.alignment = tokens.body_alignment("left", wrap_text=True)
            cell.border = tokens.data_border()
        written += 1
    return written


def _write_status_bar(
    worksheet: Worksheet,
    sections: Mapping[str, int],
    text: str,
) -> bool:
    """写状态栏那一行。

    **必须保留区块标记**（``【运行状态】``）—— 读取层见到标记即停，
    标记被抹掉，状态栏会被当成普通数据行读进来。
    """

    status_row = sections.get("status_row")
    if status_row is None:
        return False
    cell = worksheet.cell(row=status_row, column=1)
    cell.value = text if text.startswith(STATUS_PREFIX) else f"{STATUS_PREFIX}{text}"
    tokens.style_status_cell(cell)
    return True


def _synthesize_status(
    *,
    run_id: str,
    total: int,
    written_rows: int,
    failed_rows: int,
    empty_state: str,
) -> RunStatus:
    """没给状态文件时，按本轮回填的实际情况合成一条状态。"""

    if empty_state == EMPTY_STATE_NO_CASES:
        return RunStatus(
            run_id=run_id,
            state="idle",
            processed=0,
            total=0,
            message="本表未圈定病例",
        )
    return RunStatus(
        run_id=run_id,
        state=STATE_COMPLETED,
        processed=written_rows,
        total=total,
        failed_cases=failed_rows,
    )


def _save_atomic(workbook, file_path: Path) -> None:
    """**原子落盘**：先写临时文件，再 ``os.replace`` 换上去。

    中途崩了 / 磁盘满了 —— 原表一个字节都没变，不会留下半截回填的表。
    临时文件与目标同目录，``os.replace`` 才是同一文件系统上的原子操作。
    """

    tmp_path = file_path.with_name(file_path.name + ".filling.tmp")
    try:
        workbook.save(tmp_path)
        os.replace(tmp_path, file_path)
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


# --------------------------------------------------------------------------
# 只读侧的小工具（CLI / 测试用）
# --------------------------------------------------------------------------


def read_status_bar(path: Path | str, *, sheet_name: str | None = None) -> str:
    """读一张表的状态栏文本（用来核对回填结果，不改表）。"""

    workbook = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        worksheet = (
            workbook[sheet_name]
            if sheet_name and sheet_name in workbook.sheetnames
            else _resolve_detail_sheet(workbook)
        )
        for row in range(1, worksheet.max_row + 1):
            value = worksheet.cell(row=row, column=1).value
            if value and str(value).strip().startswith(SECTION_MARKER_STATUS):
                return str(value)
        return ""
    finally:
        workbook.close()


def read_missed_area_texts(path: Path | str, *, sheet_name: str | None = None) -> list[str]:
    """读漏诊区的文本（空态那句「本次未发现漏判」也要读得到）。

    只读到**下一个区块标记**为止 —— 漏诊区下面就是状态栏，
    状态栏那句「评估完成」不属于漏诊区。
    """

    workbook = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        worksheet = (
            workbook[sheet_name]
            if sheet_name and sheet_name in workbook.sheetnames
            else _resolve_detail_sheet(workbook)
        )
        texts: list[str] = []
        for row in range(1, worksheet.max_row + 1):
            value = worksheet.cell(row=row, column=1).value
            if value and str(value).strip().startswith(SECTION_MARKER_MISSED):
                # +1 是漏诊区的表头行，+2 才是数据行（与模板版面一致）。
                for offset in range(2, 40):
                    cell_value = worksheet.cell(row=row + offset, column=1).value
                    if cell_value is None:
                        continue
                    text = str(cell_value).strip()
                    if text.startswith("【"):
                        break  # 下一个区块（状态栏）—— 不是漏诊区内容
                    texts.append(text)
                break
        return texts
    finally:
        workbook.close()
