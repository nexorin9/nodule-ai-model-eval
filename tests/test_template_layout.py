"""评估表模板**核心界面**的版面测试（任务 44）。

这张表是产品的界面本体 —— 放射科填金标准、信息科看判定、评审会上打印
逐行核对，全都在这一张表里。所以这里断言的不是「文件能打开」，而是
**四个区块在不在、区块之间的界线清不清、回填的人该往哪写**：

1. **四个区块都存在** —— 表头汇总区 / 逐例明细区 / 漏诊明细区 / 底部状态栏；
2. **汇总区单元格位置固定** —— 引擎回填按地址写，位置不能随预置行数变；
3. **既有列与新增列分区** —— 两层的归属与底色分得开（原始件 / 展示件）；
4. **表头冻结与打印设置** —— 两百例的表打印出来仍能逐行核对；
5. **读层只读明细区** —— 漏诊区里的检查号不会被当成新的病例读进来。

**为什么第 5 条要紧**：漏诊明细区里也有检查号（那是引擎回填的结论）。
读层若一路读到表尾，同一例会被读两遍，还会拿空的金标准原文报错 ——
一次评估的口径当场就错了。模板与读层靠区块标记（``sheets/sections.py``）
把这条界线写成约定。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nodule_eval.sheets import tokens  # noqa: E402
from nodule_eval.sheets.columns import ALL_COLUMNS, EXISTING_COLUMNS, NEW_COLUMNS  # noqa: E402
from nodule_eval.sheets.read import read_sheet  # noqa: E402
from nodule_eval.sheets.sections import (  # noqa: E402
    SECTION_MARKER_MISSED,
    SECTION_MARKER_STATUS,
    is_section_marker,
)
from nodule_eval.sheets.template import (  # noqa: E402
    FIRST_DATA_ROW,
    GROUP_HEADER_ROW,
    HEADER_ROW,
    MISSED_SPANS,
    SHEET_DETAIL,
    SUMMARY_CELLS,
    SUMMARY_COUNT_KEYS,
    SUMMARY_LABELS,
    SUMMARY_METRIC_KEYS,
    TITLE_ROW,
    find_sections,
    layout_for,
    write_template,
)

#: 汇总区必须有的指标 —— 少一个，评审会上就少一个可核对的数。
REQUIRED_SUMMARY_KEYS = (
    "sample_size",
    "tp",
    "fp",
    "fn",
    "tn",
    "sensitivity",
    "specificity",
    "ppv",
    "npv",
    "failed_count",
    "rule_degraded_count",
    "needs_review_count",
)


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    return write_template(tmp_path / "评估表.xlsx", prefilled_rows=5)


@pytest.fixture
def workbook(template_path: Path):
    return load_workbook(template_path)


# --------------------------------------------------------------------------
# 一、四个区块
# --------------------------------------------------------------------------


def test_four_blocks_exist(workbook) -> None:
    """四个区块都在：汇总区 / 明细区 / 漏诊区 / 状态栏。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    # 区块一：表头汇总区（小标题带 + 两排指标）
    assert sheet.cell(row=layout.summary_section_row, column=1).value == "评估汇总（引擎回填）"
    assert sheet.cell(row=layout.summary_label_rows[0], column=1).value == SUMMARY_LABELS[
        "sample_size"
    ]

    # 区块二：逐例明细区（分组表头 + 列头 + 数据行）
    assert sheet.cell(row=layout.group_header_row, column=1).value
    assert sheet.cell(row=layout.header_row, column=1).value == ALL_COLUMNS[0]
    assert sheet.max_row >= layout.last_data_row

    # 区块三：漏诊明细区（区块标题 + 自己的表头）
    assert sheet.cell(row=layout.missed_section_row, column=1).value.startswith(
        SECTION_MARKER_MISSED
    )
    assert sheet.cell(row=layout.missed_header_row, column=1).value == MISSED_SPANS[0][0]

    # 区块四：底部状态栏（最后一行）
    assert sheet.cell(row=layout.status_row, column=1).value.startswith(SECTION_MARKER_STATUS)
    assert layout.status_row == sheet.max_row


def test_blocks_are_stacked_in_order() -> None:
    """四个区块自上而下排开，位置互不重叠。"""

    layout = layout_for(5)

    assert layout.title_row < layout.summary_section_row
    assert layout.summary_section_row < layout.group_header_row
    assert layout.group_header_row < layout.header_row
    assert layout.header_row < layout.first_data_row
    assert layout.last_data_row < layout.missed_section_row
    assert layout.missed_section_row < layout.missed_header_row
    assert layout.missed_header_row < layout.missed_first_data_row
    assert layout.missed_last_data_row < layout.status_row


def test_missed_area_lists_the_six_missed_columns(workbook) -> None:
    """漏诊区的列与漏诊清单一致：检查号 / 金标准摘要 / 模型返回 / 漏诊原因 /
    调用记录ID / 规则降级。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)
    headers = [
        sheet.cell(row=layout.missed_header_row, column=start).value
        for _label, start, _end in MISSED_SPANS
    ]
    assert headers == ["检查号", "金标准摘要", "模型返回", "漏诊原因", "调用记录ID", "规则降级"]


# --------------------------------------------------------------------------
# 二、汇总区：固定单元格位置
# --------------------------------------------------------------------------


def test_summary_cells_are_at_fixed_addresses(workbook) -> None:
    """汇总区每个指标都落在约定好的格子里 —— 引擎按地址回填。"""

    sheet = workbook[SHEET_DETAIL]
    for key, address in SUMMARY_CELLS.items():
        assert sheet[address].value is not None, f"{key} 没写在 {address}"
        assert sheet[address].value == "—", f"{key} 的占位符不对"


def test_summary_addresses_do_not_depend_on_prefilled_rows() -> None:
    """预置行数变了，汇总区的地址不能跟着变 —— 宏与引擎都按死地址写。"""

    small = layout_for(5)
    large = layout_for(200)

    assert small.summary_section_row == large.summary_section_row
    assert small.summary_label_rows == large.summary_label_rows
    assert small.summary_value_rows == large.summary_value_rows
    assert small.summary_value_rows == (6, 8)


def test_summary_contains_every_required_metric() -> None:
    """该有的指标一个不少 —— 少一个，评审会上就少一个可核对的数。"""

    for key in REQUIRED_SUMMARY_KEYS:
        assert key in SUMMARY_CELLS, key
        assert key in SUMMARY_LABELS, key


def test_summary_labels_sit_above_their_values(workbook) -> None:
    """标签在值**上方** —— 一排标签一排数值，扫一遍就知道每个数是什么。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    # 第一排：A5=样本量 → A6=值；F5=敏感度 → F6=值
    assert sheet.cell(row=layout.summary_label_rows[0], column=1).value == "样本量"
    assert sheet.cell(row=layout.summary_label_rows[0], column=6).value == "敏感度"
    # 第二排：A7=失败数 → A8=值
    assert sheet.cell(row=layout.summary_label_rows[1], column=1).value == "失败数"
    assert sheet.cell(row=layout.summary_label_rows[1], column=6).value == "降级说明"


def test_summary_numbers_use_fixed_formats(workbook) -> None:
    """计数是整数、指标是四位小数 —— 汇总行要给人拿计算器手算复核。"""

    sheet = workbook[SHEET_DETAIL]

    for key in SUMMARY_COUNT_KEYS:
        assert sheet[SUMMARY_CELLS[key]].number_format == tokens.NUMBER_FORMAT_COUNT, key
    for key in SUMMARY_METRIC_KEYS:
        assert sheet[SUMMARY_CELLS[key]].number_format == tokens.NUMBER_FORMAT_METRIC, key


def test_summary_area_never_uses_existing_column_aliases(workbook) -> None:
    """汇总区不能出现「检查号 / 金标准原文 / 影像引用」。

    读取层靠这三个中文别名定位表头行 —— 汇总区里出现会把表头认错，
    整张表的输入侧就错位了。
    """

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)
    forbidden = ("检查号", "金标准原文", "影像引用")

    for row in range(1, layout.header_row):
        for column in range(1, len(ALL_COLUMNS) + 1):
            value = sheet.cell(row=row, column=column).value
            if not isinstance(value, str):
                continue
            for alias in forbidden:
                assert alias not in value, f"第 {row} 行第 {column} 列出现「{alias}」"


# --------------------------------------------------------------------------
# 三、既有列与新增列分区
# --------------------------------------------------------------------------


def test_existing_and_new_columns_are_two_groups(workbook) -> None:
    """既有列组在左、新增列组在右，两组各有自己的合并范围。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)
    row = layout.group_header_row

    assert sheet.cell(row=row, column=1).value.startswith("科室填写")
    assert sheet.cell(row=row, column=len(EXISTING_COLUMNS) + 1).value.startswith("引擎回填")

    merged = {str(rng) for rng in sheet.merged_cells.ranges}
    left = f"A{row}:C{row}"
    right = f"D{row}:I{row}"
    assert left in merged
    assert right in merged


def test_new_column_group_is_filled_and_existing_group_is_not(workbook) -> None:
    """新增列组带底色（展示件）、既有列组白底（原始件）—— 两层一眼分得开。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)
    row = layout.group_header_row

    existing = sheet.cell(row=row, column=1)
    new = sheet.cell(row=row, column=len(EXISTING_COLUMNS) + 1)

    assert existing.fill.patternType is None, "既有列组不该有底色 —— 那是科室的原始件"
    assert new.fill.patternType == "solid", "新增列组要有底色"
    assert new.fill.start_color.rgb.endswith(tokens.COLOR_SECTION_FILL)


def test_column_header_keeps_contract_order(workbook) -> None:
    """分区只是**视觉**上的，既有列与新增列的契约顺序一点没动。"""

    sheet = workbook[SHEET_DETAIL]
    headers = [
        sheet.cell(row=HEADER_ROW, column=index).value
        for index in range(1, len(ALL_COLUMNS) + 1)
    ]
    assert headers == list(ALL_COLUMNS)
    assert headers[: len(EXISTING_COLUMNS)] == list(EXISTING_COLUMNS)
    assert headers[len(EXISTING_COLUMNS) :] == list(NEW_COLUMNS)


def test_detail_rows_still_align_one_case_per_row(workbook) -> None:
    """分区没有把「一列 = 一例」拆坏 —— 明细区仍是一行一例。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    assert layout.last_data_row - layout.first_data_row + 1 == 5
    for row in range(layout.first_data_row, layout.last_data_row + 1):
        assert sheet.row_dimensions[row].height == tokens.ROW_HEIGHT_DATA


# --------------------------------------------------------------------------
# 四、表头冻结与打印设置
# --------------------------------------------------------------------------


def test_header_is_frozen(workbook) -> None:
    """表头（含汇总区）冻住 —— 往下翻两百行，列名与指标一直在。"""

    sheet = workbook[SHEET_DETAIL]
    assert sheet.freeze_panes == f"A{FIRST_DATA_ROW}"


def test_print_setup_is_landscape_single_page_wide(workbook) -> None:
    """横向 + 缩到一页宽 + 每页重复表头。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    assert sheet.page_setup.orientation == "landscape"
    assert sheet.page_setup.fitToWidth == 1
    assert sheet.page_setup.fitToHeight == 0
    assert sheet.sheet_properties.pageSetUpPr.fitToPage is True
    assert layout.print_title_rows == f"{GROUP_HEADER_ROW}:{HEADER_ROW}"
    # openpyxl 把重复标题行规范化成 $10:$11 的形态 —— 按绝对行号断言。
    assert f"${GROUP_HEADER_ROW}:${HEADER_ROW}" in sheet.print_title_rows
    # openpyxl 会把打印区域规范化成「页名」!$A$1:$I$40 的形态 —— 按绝对区间断言。
    assert f"$A$1:$I${layout.status_row}" in sheet.print_area


def test_print_area_covers_the_whole_sheet(workbook) -> None:
    """打印区域含四个区块 —— 状态栏与漏诊区也要能打出来。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    assert f"$A$1:$I${layout.status_row}" in sheet.print_area
    assert layout.status_row == sheet.max_row


# --------------------------------------------------------------------------
# 五、读层只读明细区
# --------------------------------------------------------------------------


def test_reader_stops_at_the_missed_block(tmp_path: Path) -> None:
    """漏诊区里的检查号是**结论**，不是输入 —— 读层不能把它读成新病例。"""

    target = write_template(tmp_path / "评估表.xlsx", prefilled_rows=2)
    book = load_workbook(target)
    sheet = book[SHEET_DETAIL]
    layout = layout_for(2)

    sheet.cell(row=layout.first_data_row, column=1, value="ACC-001")
    sheet.cell(row=layout.first_data_row, column=2, value="右下肺见磨玻璃结节。")
    sheet.cell(row=layout.first_data_row + 1, column=1, value="ACC-002")
    sheet.cell(row=layout.first_data_row + 1, column=2, value="双肺未见明显结节影。")
    # 漏诊区里放一行结论：检查号 + 金标准摘要（**没有**金标准原文）
    sheet.cell(row=layout.missed_first_data_row, column=1, value="ACC-001")
    sheet.cell(row=layout.missed_first_data_row, column=2, value="右肺下叶 | 8.0mm | 磨玻璃")
    book.save(target)

    result = read_sheet(target)

    assert [case.accession_no for case in result.cases] == ["ACC-001", "ACC-002"]
    # 两行都填了 → 没有预留空白行；漏诊区那一行**没有**被算成空白行，
    # 也没有被当成第三例（它落在区块标记之后，读层已经停了）。
    assert result.blank_rows == 0
    assert result.skipped_rows == ()
    assert result.warnings == (), result.warnings


def test_section_markers_are_recognized() -> None:
    """区块标记的识别 —— 模板写标记，读层认标记，两边一份约定。"""

    assert is_section_marker(f"{SECTION_MARKER_MISSED}漏诊明细（引擎回填）")
    assert is_section_marker(f"{SECTION_MARKER_STATUS}尚未评估")
    assert not is_section_marker("ACC-001")
    assert not is_section_marker("")
    assert not is_section_marker(None)


def test_find_sections_locates_both_blocks(workbook) -> None:
    """按标记扫出漏诊区与状态栏的行号 —— 回填按行号写，不靠猜。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    found = find_sections(sheet)

    assert found["missed_section_row"] == layout.missed_section_row
    assert found["status_row"] == layout.status_row


def test_title_and_meta_are_above_the_summary(workbook) -> None:
    """标题与元信息在最上面 —— 打开表先知道这是哪一轮、哪款模型。"""

    sheet = workbook[SHEET_DETAIL]
    layout = layout_for(5)

    assert sheet.cell(row=TITLE_ROW, column=1).value == "肺结节 AI 模型评估表"
    assert sheet.cell(row=layout.meta_row, column=1).value
    assert layout.meta_row < layout.summary_section_row
