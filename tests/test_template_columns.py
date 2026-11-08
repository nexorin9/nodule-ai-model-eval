"""评估表模板的列契约测试。

模板是**科室每天要看的那张表**：既有列由放射科维护，产品只新增六列。
两类事故都会毁掉一次评估：

- **既有列被改写** —— 金标准原文被覆盖，评估结论就失去了参照物；
- **新增列错位** —— 「失败原因」写进「模型返回」，表看起来正常但内容全错。

所以这里断言的**不是**「模板能打开」，而是「列名、列数、列顺序」三件事。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
SCRIPTS = PROJECT_ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from nodule_eval.contracts import CASE_ROW_COLUMNS  # noqa: E402
from nodule_eval.sheets.columns import (  # noqa: E402
    ALL_COLUMNS,
    EXISTING_COLUMNS,
    NEW_COLUMNS,
    assert_alignment_with_case_rows,
)
from nodule_eval.sheets.template import (  # noqa: E402
    FIRST_DATA_ROW,
    HEADER_ROW,
    SHEET_COLUMNS,
    SHEET_DETAIL,
    write_template,
)
import build_xlsm  # noqa: E402  —— scripts/ 下的命令行入口

#: 既有列 —— 科室维护，产品**绝不**覆盖。
EXPECTED_EXISTING = ("accession_no", "gold_standard_text", "image_ref")

#: 产品新增的六列 —— 引擎回填。
EXPECTED_NEW = (
    "verdict",
    "model_findings",
    "failure_reason",
    "tool_call_id",
    "needs_review",
    "normalize_source",
)


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    """生成一份模板供断言。"""

    return write_template(tmp_path / "评估表.xlsx", prefilled_rows=5)


@pytest.fixture
def workbook(template_path: Path):
    """读回生成的工作簿。"""

    return load_workbook(template_path)


# --------------------------------------------------------------------------
# 列契约
# --------------------------------------------------------------------------


def test_existing_columns_are_complete() -> None:
    """既有三列必须齐备 —— 少一列就读不出金标准原文。"""

    assert EXISTING_COLUMNS == EXPECTED_EXISTING
    for name in EXPECTED_EXISTING:
        assert name in ALL_COLUMNS


def test_new_columns_are_exactly_six_and_ordered() -> None:
    """新增列**恰好六列**，顺序固定 —— 多了少了都会让回填错位。"""

    assert len(NEW_COLUMNS) == 6
    assert NEW_COLUMNS == EXPECTED_NEW


def test_all_columns_puts_existing_before_new() -> None:
    """既有列在前、新增列在后，两段不交错。"""

    assert ALL_COLUMNS == EXISTING_COLUMNS + NEW_COLUMNS
    assert ALL_COLUMNS.index("gold_standard_text") < ALL_COLUMNS.index("verdict")


def test_new_columns_align_with_case_rows_csv() -> None:
    """新增列与逐例明细 CSV 的列必须逐字一致。

    两边各写一份字面量的话，改一次列名就会漏改一处 ——
    回填与导出按不同列序写，读出来的东西看着正常但全错位。

    注意 CSV 里还带 ``accession_no``（既有列）与 ``row_no``（行号），
    比对的是**两边共有的新增六列**这一段。
    """

    assert_alignment_with_case_rows()
    csv_new = tuple(
        key for key in CASE_ROW_COLUMNS if key not in EXISTING_COLUMNS and key != "row_no"
    )
    assert csv_new == NEW_COLUMNS


# --------------------------------------------------------------------------
# 模板本身
# --------------------------------------------------------------------------


def test_template_header_row_has_all_columns(workbook) -> None:
    """表头行的列名与列序必须与契约一致。"""

    sheet = workbook[SHEET_DETAIL]
    headers = [
        sheet.cell(row=HEADER_ROW, column=index).value
        for index in range(1, len(ALL_COLUMNS) + 1)
    ]
    assert headers == list(ALL_COLUMNS)


def test_template_has_no_extra_columns(workbook) -> None:
    """表头之后不得有多余列 —— 多一列就意味着回填要处理一个不存在的列。"""

    sheet = workbook[SHEET_DETAIL]
    headers = [
        sheet.cell(row=HEADER_ROW, column=index).value
        for index in range(1, len(ALL_COLUMNS) + 3)
    ]
    assert headers[len(ALL_COLUMNS):] == [None, None]


def test_template_has_column_explanation_sheet(workbook) -> None:
    """「字段说明」页必须存在，且每列都有一行说明。"""

    assert SHEET_COLUMNS in workbook.sheetnames
    sheet = workbook[SHEET_COLUMNS]
    documented = [sheet.cell(row=row, column=1).value for row in range(2, 2 + len(ALL_COLUMNS))]
    assert documented == list(ALL_COLUMNS)


def test_template_existing_cells_are_empty(workbook) -> None:
    """模板不预填既有列的内容 —— 金标准原文只能由科室写。"""

    sheet = workbook[SHEET_DETAIL]
    for offset in range(5):
        row = FIRST_DATA_ROW + offset
        for index, name in enumerate(ALL_COLUMNS, start=1):
            assert sheet.cell(row=row, column=index).value is None, name


def test_template_has_prefilled_blank_rows(workbook) -> None:
    """模板要预置空白行 —— 放射科打开就能填，不必先拉格式。"""

    sheet = workbook[SHEET_DETAIL]
    assert sheet.max_row >= FIRST_DATA_ROW + 4


def test_write_template_rejects_foreign_column_set(tmp_path: Path) -> None:
    """传入别的列集合必须报错 —— 模板列不许由调用方临时拼。"""

    with pytest.raises(ValueError):
        write_template(tmp_path / "bad.xlsx", columns=("a", "b"))


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------


def test_cli_generates_template(tmp_path: Path, capsys) -> None:
    """``python scripts/build_xlsm.py`` 能生成模板并打印列名。"""

    target = tmp_path / "评估表.xlsx"
    code = build_xlsm.main(["--out", str(target), "--rows", "3"])

    captured = capsys.readouterr()
    assert code == 0
    assert target.is_file()
    assert "评估表模板已生成" in captured.out
    for name in ALL_COLUMNS:
        assert name in captured.out

    sheet = load_workbook(target)[SHEET_DETAIL]
    headers = [
        sheet.cell(row=HEADER_ROW, column=index).value
        for index in range(1, len(ALL_COLUMNS) + 1)
    ]
    assert headers == list(ALL_COLUMNS)
