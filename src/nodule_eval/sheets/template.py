"""评估表模板的**核心界面**（``openpyxl``）。

**这张表就是产品的界面本体** —— 用户全程不离开它：放射科在明细区填金标准，
信息科点「开始评估」，引擎把判定写回新增六列，评审会上打印出来逐行核对。

**四个区块（自上而下）**：

1. **表头汇总区**（第 4–8 行）—— 样本量 / TP·FP·FN·TN / 四项指标 / 失败数 /
   规则降级数 / 挂起数 / 不可比数 / 模型归并例数 / 降级说明。
   **单元格位置固定**（:data:`SUMMARY_CELLS`）—— 引擎按地址回填，
   评审会上不用往下翻两页就能看到「这轮敏感度多少、漏了几例」。
2. **逐例明细区**（分组表头 + 列头 + 数据行）—— 既有三列与新增六列
   **左右分区**：既有列是科室维护的**原始件**（输入层），新增列是引擎回填的
   **展示件**（输出层）。两层用一行分组表头 + 不同底色区分。
3. **漏诊明细区**（明细区之下）—— 引擎回填，条数须与漏判数一致。
4. **底部状态栏**（最后一行）—— 运行状态 / 最近更新 / 调用底账路径。

**为什么「既有列 / 新增列」不是上下两张表**：一列 = 一例是这张表的地基
（逐例明细 CSV 与评估表新增列逐字对齐，回填按列定位）。上下两张表会让
「同一例的两半」分处两行，回填与导出都得再做一次行号映射 ——
一次映射错位就是一次静默错案。所以两层用**左右分区**表达：
既有列在左（原始件，输入层），新增列在右（展示件，输出层），
分组表头把两层的底色与归属写清楚。

**样式只有一份**：配色、字体、行高、列宽、数字格式全在
:mod:`nodule_eval.sheets.tokens`（任务 43）；模板生成（任务 44）与
逐例回填（任务 46）都从那里取，改一处两处生效。

**列名用契约名而不是中文**：宏（VBA / WPS JSA）与 ``openpyxl`` 回填都按
:data:`nodule_eval.sheets.columns.ALL_COLUMNS` 定位列，表头一旦换成中文，
定位规则就要多一套映射；中文名放在「字段说明」页里给科室看。

**读层只读明细区**：明细区之后的两个区块由
:mod:`nodule_eval.sheets.sections` 的区块标记隔开 —— 读层见到标记即停，
不会把漏诊清单里的检查号当成新的病例读进来。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from . import tokens
from .columns import (
    ALL_COLUMNS,
    COLUMN_HINTS_ZH,
    COLUMN_LABELS_ZH,
    EXISTING_COLUMNS,
    NEW_COLUMNS,
    SHEET_COLUMNS,
    assert_alignment_with_case_rows,
)
from .sections import SECTION_MARKER_MISSED, SECTION_MARKER_STATUS

# --------------------------------------------------------------------------
# 工作表名
# --------------------------------------------------------------------------

#: 明细区所在页的名字。**读取层认这两个名字**：
#: 「评估表」是我们生成的模板页，「逐例明细」是科室自己的表常见的页名。
DETAIL_SHEET_NAMES: tuple[str, ...] = ("评估表", "逐例明细")

#: 明细区所在的工作表名（= :data:`DETAIL_SHEET_NAMES` 的第一项）。
SHEET_DETAIL = DETAIL_SHEET_NAMES[0]

#: 漏诊明细区所在的工作表名。
SHEET_MISSED = "漏诊明细"

#: 页面标题文案。
TITLE_TEXT = "肺结节 AI 模型评估表"

#: 标题下方的元信息行（模板态是占位说明，引擎回填后写实值）。
META_TEXT = "被评模型：{model_id} {model_version}　｜　数据集：{dataset_ref}　｜　运行ID：{run_id}　｜　生成时间：{generated_at}"

#: 模板态的元信息占位（发给科室时看得懂「哪些格子等着引擎填」）。
META_PLACEHOLDER = "被评模型：（引擎回填）　｜　数据集：（引擎回填）　｜　运行ID：（引擎回填）　｜　生成时间：（引擎回填）　｜　汇总区与判定列由引擎回填，本表既有列由科室维护"

#: 分组表头 —— 既有列组（原始件 / 输入层）。
GROUP_LABEL_EXISTING = "科室填写 · 原始件（产品只读，不改写）"

#: 分组表头 —— 新增列组（展示件 / 输出层）。
GROUP_LABEL_NEW = "引擎回填 · 展示件（判定与解释）"

#: 漏诊明细区的区块标题（首格同时是读层的区块标记）。
MISSED_SECTION_TEXT = (
    f"{SECTION_MARKER_MISSED}引擎回填 · 条数须与漏判数 FN 相等（放射科逐例回看原调用）"
)

#: 状态栏的区块标题（首格同时是读层的区块标记）。
STATUS_TEXT = f"{SECTION_MARKER_STATUS}尚未评估（模板初始状态）"

#: 状态栏第二格 —— 最近更新。
STATUS_UPDATED_TEXT = "最近更新：（引擎回填）"

#: 状态栏第三格 —— 调用底账路径。
STATUS_LEDGER_TEXT = "调用底账：runs/<run_id>/tool_calls.jsonl"

# --------------------------------------------------------------------------
# 行位置（前 11 行是固定的，与预置行数无关）
# --------------------------------------------------------------------------

#: 页面标题行（1 基）。
TITLE_ROW = 1

#: 元信息行（1 基）。
META_ROW = 2

#: 汇总区上方的留白行。
SPACER_ROW_TOP = 3

#: 汇总区的小标题行。
SUMMARY_SECTION_ROW = 4

#: 汇总区第一排的标签行 / 数值行。
SUMMARY_LABEL_ROW_1 = 5
SUMMARY_VALUE_ROW_1 = 6

#: 汇总区第二排的标签行 / 数值行。
SUMMARY_LABEL_ROW_2 = 7
SUMMARY_VALUE_ROW_2 = 8

#: 汇总区与明细区之间的留白行。
SPACER_ROW_BOTTOM = 9

#: 分组表头行（既有列组 / 新增列组）。
GROUP_HEADER_ROW = 10

#: 列头所在行（1 基）。
HEADER_ROW = 11

#: 逐例明细区第一条数据行（1 基）—— 数据从这行往下写。
FIRST_DATA_ROW = HEADER_ROW + 1

#: 预置的空白数据行数：放射科打开就能直接往里填，不必先拉格式。
PREFILLED_ROWS = 200

#: 漏诊明细区预置的空白行数。
MISSED_PREFILLED_ROWS = 20

# --------------------------------------------------------------------------
# 汇总区：固定单元格地址（引擎回填按这里写）
# --------------------------------------------------------------------------

#: 汇总区各指标的中文标签。
SUMMARY_LABELS: Mapping[str, str] = {
    "sample_size": "样本量",
    "tp": "命中 TP",
    "fp": "误报 FP",
    "fn": "漏判 FN",
    "tn": "真阴性 TN",
    "sensitivity": "敏感度",
    "specificity": "特异度",
    "ppv": "阳性预测值",
    "npv": "阴性预测值",
    "failed_count": "失败数",
    "rule_degraded_count": "规则降级数",
    "needs_review_count": "挂起数",
    "incomparable_count": "不可比数",
    "model_normalized_count": "模型归并例数",
    "degrade_note": "降级说明",
}

#: 汇总区各指标的**固定单元格地址**。
#:
#: 这些地址写在模板的最上面几行，**与预置行数无关** —— 引擎（任务 46 回填）
#: 与宏（任务 45）都按这张表写值，不用先知道表里有多少行。
SUMMARY_CELLS: Mapping[str, str] = {
    "sample_size": "A6",
    "tp": "B6",
    "fp": "C6",
    "fn": "D6",
    "tn": "E6",
    "sensitivity": "F6",
    "specificity": "G6",
    "ppv": "H6",
    "npv": "I6",
    "failed_count": "A8",
    "rule_degraded_count": "B8",
    "needs_review_count": "C8",
    "incomparable_count": "D8",
    "model_normalized_count": "E8",
    "degrade_note": "F8",
}

#: 汇总区里**按四位小数**显示的指标（百分比类）。
SUMMARY_METRIC_KEYS: tuple[str, ...] = ("sensitivity", "specificity", "ppv", "npv")

#: 汇总区里**按整数**显示的计数类指标。
SUMMARY_COUNT_KEYS: tuple[str, ...] = (
    "sample_size",
    "tp",
    "fp",
    "fn",
    "tn",
    "failed_count",
    "rule_degraded_count",
    "needs_review_count",
    "incomparable_count",
    "model_normalized_count",
)

#: 汇总区数值的占位符 —— 引擎回填前显示「—」，不留空让人以为漏算了。
SUMMARY_PLACEHOLDER = "—"

#: 降级说明所在单元格的合并范围（F8:I8）。
DEGRADE_NOTE_SPAN = ("F", "I")

# --------------------------------------------------------------------------
# 漏诊明细区的列（标签 + 合并跨度）
# --------------------------------------------------------------------------

#: 漏诊明细区的列：（表头文字, 起列, 止列）—— 止列 > 起列表示该列合并。
MISSED_SPANS: tuple[tuple[str, int, int], ...] = (
    ("检查号", 1, 1),
    ("金标准摘要", 2, 2),
    ("模型返回", 3, 3),
    ("漏诊原因", 4, 5),
    ("调用记录ID", 6, 7),
    ("规则降级", 8, 9),
)

# --------------------------------------------------------------------------
# 状态栏（底部的三格 + 合并跨度）
# --------------------------------------------------------------------------

#: 状态栏三格：（文字, 起列, 止列）。
STATUS_SPANS: tuple[tuple[str, int, int], ...] = (
    (STATUS_TEXT, 1, 4),
    (STATUS_UPDATED_TEXT, 5, 6),
    (STATUS_LEDGER_TEXT, 7, 9),
)

# --------------------------------------------------------------------------
# 列宽（真源在 tokens）
# --------------------------------------------------------------------------

#: 各列的建议列宽（Excel 字符宽）。
#: **真源在** :mod:`nodule_eval.sheets.tokens` —— 任务 43 起配色、字体、行高、
#: 列宽、数字格式统一由 token 定，模板生成（任务 44）与逐例回填（任务 46）
#: 都从那里取，改一处两处生效。这里保留这个名字只是为了不让既有调用方断掉。
COLUMN_WIDTHS: dict[str, int] = dict(tokens.COLUMN_WIDTHS)


# --------------------------------------------------------------------------
# 布局
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateLayout:
    """模板的**版面地图** —— 每个区块在第几行、引擎该往哪写。

    任务 46 的回填与任务 45 的宏都按这张地图定位，不再各自写一份行号。
    """

    sheet_name: str
    missed_sheet_name: str
    title_row: int
    meta_row: int
    summary_section_row: int
    summary_label_rows: tuple[int, int]
    summary_value_rows: tuple[int, int]
    group_header_row: int
    header_row: int
    first_data_row: int
    last_data_row: int
    missed_section_row: int
    missed_header_row: int
    missed_first_data_row: int
    missed_last_data_row: int
    status_row: int
    freeze_cell: str
    print_area: str
    print_title_rows: str
    last_row: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sheet_name": self.sheet_name,
            "missed_sheet_name": self.missed_sheet_name,
            "title_row": self.title_row,
            "meta_row": self.meta_row,
            "summary_section_row": self.summary_section_row,
            "summary_label_rows": list(self.summary_label_rows),
            "summary_value_rows": list(self.summary_value_rows),
            "group_header_row": self.group_header_row,
            "header_row": self.header_row,
            "first_data_row": self.first_data_row,
            "last_data_row": self.last_data_row,
            "missed_section_row": self.missed_section_row,
            "missed_header_row": self.missed_header_row,
            "missed_first_data_row": self.missed_first_data_row,
            "missed_last_data_row": self.missed_last_data_row,
            "status_row": self.status_row,
            "freeze_cell": self.freeze_cell,
            "print_area": self.print_area,
            "print_title_rows": self.print_title_rows,
            "last_row": self.last_row,
        }


def layout_for(prefilled_rows: int = PREFILLED_ROWS) -> TemplateLayout:
    """按预置行数算出整张表的版面地图。

    **前 11 行是固定的**（标题 / 元信息 / 汇总区 / 分组表头 / 列头），
    因此 :data:`SUMMARY_CELLS` 里的地址与预置行数无关 ——
    引擎回填时不用先知道表里有多少行。
    """

    if prefilled_rows < 1:
        raise ValueError(f"预置行数至少为 1（实得 {prefilled_rows}）")

    last_data_row = FIRST_DATA_ROW + prefilled_rows - 1
    # 漏诊区紧接明细区（不留空行）：中间那一行若留空，读层会把它算成
    # 「模板预留的空白行」—— 空白行计数是给人对账用的，不该多一行。
    missed_section_row = last_data_row + 1
    missed_header_row = missed_section_row + 1
    missed_first_data_row = missed_header_row + 1
    missed_last_data_row = missed_first_data_row + MISSED_PREFILLED_ROWS - 1
    status_row = missed_last_data_row + 2
    last_row = status_row

    return TemplateLayout(
        sheet_name=SHEET_DETAIL,
        missed_sheet_name=SHEET_MISSED,
        title_row=TITLE_ROW,
        meta_row=META_ROW,
        summary_section_row=SUMMARY_SECTION_ROW,
        summary_label_rows=(SUMMARY_LABEL_ROW_1, SUMMARY_LABEL_ROW_2),
        summary_value_rows=(SUMMARY_VALUE_ROW_1, SUMMARY_VALUE_ROW_2),
        group_header_row=GROUP_HEADER_ROW,
        header_row=HEADER_ROW,
        first_data_row=FIRST_DATA_ROW,
        last_data_row=last_data_row,
        missed_section_row=missed_section_row,
        missed_header_row=missed_header_row,
        missed_first_data_row=missed_first_data_row,
        missed_last_data_row=missed_last_data_row,
        status_row=status_row,
        freeze_cell=f"A{FIRST_DATA_ROW}",
        print_area=f"A1:{get_column_letter(len(ALL_COLUMNS))}{last_row}",
        print_title_rows=f"{GROUP_HEADER_ROW}:{HEADER_ROW}",
        last_row=last_row,
    )


# --------------------------------------------------------------------------
# 区块写法
# --------------------------------------------------------------------------


def _merge(worksheet: Worksheet, row: int, start_column: int, end_column: int) -> None:
    """合并一行里的一段列（合并前先写值，合并后只填样式）。"""

    if end_column > start_column:
        worksheet.merge_cells(
            start_row=row,
            start_column=start_column,
            end_row=row,
            end_column=end_column,
        )


def _band(worksheet: Worksheet, row: int, text: str, *, height: int | None = None) -> None:
    """整行区块标题带（浅蓝灰底 + 加粗）。"""

    anchor = worksheet.cell(row=row, column=1, value=text)
    tokens.style_section_cell(anchor)
    for index in range(2, len(ALL_COLUMNS) + 1):
        cell = worksheet.cell(row=row, column=index)
        cell.fill = tokens.section_fill()
    _merge(worksheet, row, 1, len(ALL_COLUMNS))
    worksheet.row_dimensions[row].height = height or tokens.ROW_HEIGHT_SECTION


def _write_title(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """页面标题 + 元信息行。"""

    anchor = worksheet.cell(row=layout.title_row, column=1, value=TITLE_TEXT)
    tokens.style_title_cell(anchor)
    _merge(worksheet, layout.title_row, 1, len(ALL_COLUMNS))
    worksheet.row_dimensions[layout.title_row].height = tokens.ROW_HEIGHT_TITLE

    meta = worksheet.cell(row=layout.meta_row, column=1, value=META_PLACEHOLDER)
    tokens.style_meta_cell(meta)
    _merge(worksheet, layout.meta_row, 1, len(ALL_COLUMNS))
    worksheet.row_dimensions[layout.meta_row].height = tokens.ROW_HEIGHT_DATA

    worksheet.row_dimensions[SPACER_ROW_TOP].height = 6


def _write_summary(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """表头汇总区 —— 两排指标，**位置固定**，引擎按 :data:`SUMMARY_CELLS` 回填。

    第一排：样本量 / TP·FP·FN·TN / 四项指标（9 格）。
    第二排：失败数 / 规则降级数 / 挂起数 / 不可比数 / 模型归并例数 / 降级说明。

    **这一区不能出现「检查号 / 金标准原文 / 影像引用」这三个中文别名** ——
    读取层靠它们定位表头行，汇总区里出现会把表头行认错。
    """

    _band(worksheet, layout.summary_section_row, "评估汇总（引擎回填）")

    first_labels = (
        "sample_size",
        "tp",
        "fp",
        "fn",
        "tn",
        "sensitivity",
        "specificity",
        "ppv",
        "npv",
    )
    second_labels = (
        "failed_count",
        "rule_degraded_count",
        "needs_review_count",
        "incomparable_count",
        "model_normalized_count",
    )

    for index, key in enumerate(first_labels, start=1):
        cell = worksheet.cell(row=SUMMARY_LABEL_ROW_1, column=index, value=SUMMARY_LABELS[key])
        tokens.style_summary_label_cell(cell)
    for index, key in enumerate(second_labels, start=1):
        cell = worksheet.cell(row=SUMMARY_LABEL_ROW_2, column=index, value=SUMMARY_LABELS[key])
        tokens.style_summary_label_cell(cell)

    worksheet.row_dimensions[SUMMARY_LABEL_ROW_1].height = tokens.ROW_HEIGHT_DATA
    worksheet.row_dimensions[SUMMARY_LABEL_ROW_2].height = tokens.ROW_HEIGHT_DATA

    # 降级说明占 F:I（第一排没排满的格子并过来），标题与内容各占一行。
    note_label = worksheet.cell(
        row=SUMMARY_LABEL_ROW_2, column=6, value=SUMMARY_LABELS["degrade_note"]
    )
    tokens.style_summary_label_cell(note_label)
    for index in range(7, len(ALL_COLUMNS) + 1):
        worksheet.cell(row=SUMMARY_LABEL_ROW_2, column=index).fill = tokens.section_fill()
    _merge(worksheet, SUMMARY_LABEL_ROW_2, 6, len(ALL_COLUMNS))

    # 数值行 —— 计数整数、指标四位小数、降级说明是文字。
    for key, address in SUMMARY_CELLS.items():
        cell = worksheet[address]
        cell.value = SUMMARY_PLACEHOLDER
        if key in SUMMARY_METRIC_KEYS:
            tokens.style_metric_cell(cell)
        elif key in SUMMARY_COUNT_KEYS:
            tokens.style_count_cell(cell)
        else:
            tokens.style_summary_note_cell(cell)
    worksheet.row_dimensions[SUMMARY_VALUE_ROW_1].height = tokens.ROW_HEIGHT_DATA
    worksheet.row_dimensions[SUMMARY_VALUE_ROW_2].height = tokens.ROW_HEIGHT_DATA

    note_cell = worksheet[SUMMARY_CELLS["degrade_note"]]
    for index in range(7, len(ALL_COLUMNS) + 1):
        worksheet.cell(row=SUMMARY_VALUE_ROW_2, column=index).fill = tokens.section_fill()
    _merge(worksheet, SUMMARY_VALUE_ROW_2, 6, len(ALL_COLUMNS))

    worksheet.row_dimensions[SPACER_ROW_BOTTOM].height = 6


def _write_group_header(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """分组表头 —— 既有列组（左，原始件）与新增列组（右，展示件）。

    **两层用底色区分**：既有列组保持白底（输入层，科室的东西不该被装饰），
    新增列组用浅蓝灰底（输出层，引擎写的东西）。这一行是整张表的**分区说明**，
    也是「原始件 / 展示件」这条源产品原则在表格上的落点。
    """

    split = len(EXISTING_COLUMNS)
    last = len(ALL_COLUMNS)

    existing = worksheet.cell(
        row=layout.group_header_row, column=1, value=GROUP_LABEL_EXISTING
    )
    tokens.style_group_label_cell(existing, filled=False)
    for index in range(2, split + 1):
        worksheet.cell(row=layout.group_header_row, column=index).border = tokens.group_border()
    _merge(worksheet, layout.group_header_row, 1, split)

    new = worksheet.cell(row=layout.group_header_row, column=split + 1, value=GROUP_LABEL_NEW)
    tokens.style_group_label_cell(new, filled=True)
    for index in range(split + 2, last + 1):
        cell = worksheet.cell(row=layout.group_header_row, column=index)
        cell.fill = tokens.section_fill()
        cell.border = tokens.group_border()
    _merge(worksheet, layout.group_header_row, split + 1, last)

    worksheet.row_dimensions[layout.group_header_row].height = tokens.ROW_HEIGHT_SECTION


def _write_detail_area(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """逐例明细区：列头 + 预置空白行 + 判定列的三档条件格式。"""

    tokens.apply_header_row(
        worksheet,
        ALL_COLUMNS,
        row=layout.header_row,
        freeze=True,
        first_data_row=layout.first_data_row,
    )

    # 预置的空白行要**真的写进工作簿**（带底边框的空单元格），
    # 否则 openpyxl 不会保存它们 —— 放射科打开时看到的是一张光秃秃的表，
    # 还得自己拉行高、补边框才像一张评估表。
    for offset in range(layout.last_data_row - layout.first_data_row + 1):
        row = layout.first_data_row + offset
        tokens.apply_data_row_style(worksheet, row, ALL_COLUMNS)

    # 三档判定底色用**条件格式**挂上：回填只写值不碰样式，
    # 底色由 Excel/WPS 自己算 —— 人工改了判定值，底色跟着变，
    # 不会出现「值是漏判、底色还是绿的」这种自相矛盾。
    tokens.apply_verdict_conditional_formatting(
        worksheet,
        ALL_COLUMNS,
        layout.first_data_row,
        layout.last_data_row,
    )


def _write_missed_area(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """漏诊明细区 —— 引擎回填，条数须与漏判数 FN 相等。

    这一区在明细区**之下**，首格写区块标记（:mod:`nodule_eval.sheets.sections`）——
    读取层见到标记即停，不会把这里的检查号当成新的病例读进来。
    """

    _band(worksheet, layout.missed_section_row, MISSED_SECTION_TEXT)

    for label, start_column, end_column in MISSED_SPANS:
        cell = worksheet.cell(row=layout.missed_header_row, column=start_column, value=label)
        tokens.style_missed_header_cell(cell)
        for index in range(start_column + 1, end_column + 1):
            worksheet.cell(row=layout.missed_header_row, column=index).fill = tokens.header_fill()
        _merge(worksheet, layout.missed_header_row, start_column, end_column)
    worksheet.row_dimensions[layout.missed_header_row].height = tokens.ROW_HEIGHT_HEADER

    for offset in range(layout.missed_last_data_row - layout.missed_first_data_row + 1):
        row = layout.missed_first_data_row + offset
        worksheet.row_dimensions[row].height = tokens.ROW_HEIGHT_DATA
        for _label, start_column, end_column in MISSED_SPANS:
            for index in range(start_column, end_column + 1):
                cell = worksheet.cell(row=row, column=index)
                cell.border = tokens.data_border()
                cell.font = tokens.body_font()
                cell.alignment = tokens.body_alignment("left")
            _merge(worksheet, row, start_column, end_column)


def _write_status_bar(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """底部状态栏 —— 运行状态 / 最近更新 / 调用底账路径。

    首格写区块标记，与漏诊区同一个约定：读层见到即停。
    """

    for text, start_column, end_column in STATUS_SPANS:
        cell = worksheet.cell(row=layout.status_row, column=start_column, value=text)
        tokens.style_status_cell(cell)
        for index in range(start_column + 1, end_column + 1):
            other = worksheet.cell(row=layout.status_row, column=index)
            other.fill = tokens.status_fill()
        _merge(worksheet, layout.status_row, start_column, end_column)
    worksheet.row_dimensions[layout.status_row].height = tokens.ROW_HEIGHT_DATA


def _setup_print(worksheet: Worksheet, layout: TemplateLayout) -> None:
    """打印设置：横向、单页宽、每页重复表头。

    **两百例的表打印出来要能逐行核对** —— 横向 + 缩放到一页宽，
    九列不跨页断开；分组表头与列头在每一页顶部重复，
    翻到第几页都知道自己在看哪一段。
    """

    worksheet.page_setup.orientation = "landscape"
    worksheet.page_setup.paperSize = worksheet.PAPERSIZE_A4
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    worksheet.page_margins.left = 0.4
    worksheet.page_margins.right = 0.4
    worksheet.page_margins.top = 0.5
    worksheet.page_margins.bottom = 0.5
    worksheet.print_area = layout.print_area
    worksheet.print_title_rows = layout.print_title_rows


def _build_detail_sheet(workbook: Workbook, prefilled_rows: int) -> tuple[Worksheet, TemplateLayout]:
    """主界面页：汇总区 + 明细区 + 漏诊区 + 状态栏（四个区块）。"""

    layout = layout_for(prefilled_rows)
    worksheet = workbook.active
    worksheet.title = layout.sheet_name

    _write_title(worksheet, layout)
    _write_summary(worksheet, layout)
    _write_group_header(worksheet, layout)
    _write_detail_area(worksheet, layout)
    _write_missed_area(worksheet, layout)
    _write_status_bar(worksheet, layout)
    _setup_print(worksheet, layout)

    # 网格线关掉：表里的分隔线是我们自己画的（行高 + 底边框 + 分区底色），
    # 再叠一层 Excel 灰网格会显脏 —— 打印出来也只是一片噪点。
    worksheet.sheet_view.showGridLines = False
    worksheet.sheet_view.zoomScale = 90

    return worksheet, layout


def _build_columns_sheet(workbook: Workbook) -> Worksheet:
    """字段说明页：中文列名 + 谁填 + 怎么填。

    表头是契约名（机器读），这一页是人读的 —— 放射科打开就知道哪列该写什么。
    """

    worksheet = workbook.create_sheet(SHEET_COLUMNS)
    headers = ("列名（契约）", "中文名", "由谁填写", "填写说明")
    for index, title in enumerate(headers, start=1):
        cell = worksheet.cell(row=1, column=index, value=title)
        cell.font = tokens.body_font(bold=True)
        cell.fill = tokens.section_fill()
    worksheet.row_dimensions[1].height = tokens.ROW_HEIGHT_HEADER

    for offset, name in enumerate(ALL_COLUMNS):
        row = 2 + offset
        owner = "科室填写" if name in EXISTING_COLUMNS else "引擎回填"
        worksheet.cell(row=row, column=1, value=name)
        worksheet.cell(row=row, column=2, value=COLUMN_LABELS_ZH.get(name, name))
        worksheet.cell(row=row, column=3, value=owner)
        worksheet.cell(row=row, column=4, value=COLUMN_HINTS_ZH.get(name, ""))

    for index, width in enumerate((18, 14, 12, 62), start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width
    worksheet.freeze_panes = worksheet["A2"]

    return worksheet


def build_workbook(prefilled_rows: int = PREFILLED_ROWS) -> Workbook:
    """构造评估表工作簿（四个区块 + 字段说明页）。"""

    assert_alignment_with_case_rows()

    workbook = Workbook()
    _build_detail_sheet(workbook, prefilled_rows)
    _build_columns_sheet(workbook)
    return workbook


def write_template(
    path: Path | str,
    prefilled_rows: int = PREFILLED_ROWS,
    columns: Sequence[str] = ALL_COLUMNS,
) -> Path:
    """把评估表模板写到 ``path``，返回落盘路径。

    ``.xlsm`` 与 ``.xlsx`` 都支持：模板本身不含宏（宏以源码交付，见
    ``docs/macro_install.md``），``openpyxl`` 写不出 VBA 工程。
    """

    if tuple(columns) != ALL_COLUMNS:
        raise ValueError(f"列集合必须与契约一致（实得 {tuple(columns)}）")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = build_workbook(prefilled_rows=prefilled_rows)
    workbook.save(target)
    return target


def new_columns() -> tuple[str, ...]:
    """产品新增的六列（回填时只碰这些）。"""

    return NEW_COLUMNS


def find_sections(worksheet: Worksheet) -> dict[str, int]:
    """在表里找出漏诊区与状态栏的行号（按区块标记扫第一列）。

    任务 46 的回填按这两个行号写值 —— 模板的预置行数可以变，
    标记不会变，所以行号用扫的办法拿，不必把预置行数当成已知条件。
    """

    found: dict[str, int] = {}
    for row in range(1, worksheet.max_row + 1):
        text = worksheet.cell(row=row, column=1).value
        if not text:
            continue
        stripped = str(text).strip()
        if stripped.startswith(SECTION_MARKER_MISSED) and "missed_section_row" not in found:
            found["missed_section_row"] = row
        elif stripped.startswith(SECTION_MARKER_STATUS) and "status_row" not in found:
            found["status_row"] = row
    return found
