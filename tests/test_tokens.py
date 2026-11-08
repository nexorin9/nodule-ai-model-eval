"""评估表视觉 token 与样式工厂的测试。

这张表是科室打印出来贴在讨论会上的 —— 底色糊了、字看不清、
列宽跑偏导致打印错位，都是「结论没毛病但表不能用」的事故。

所以这里断言的**不是**「样式对象存在」，而是三件可核对的事：

1. **三档底色互不相同、且低饱和可打印** —— 饱和度、亮度、对比度都有数；
2. **表头冻结与列宽真的写进了工作簿** —— 不是「应该会写」；
3. **样式只有一处定义** —— 模板生成与回填共用同一份 token，改一处两处生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for path in (SRC,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from nodule_eval.contracts import (  # noqa: E402
    VERDICT_FAILED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
)
from nodule_eval.sheets import template as template_module  # noqa: E402
from nodule_eval.sheets import tokens  # noqa: E402
from nodule_eval.sheets.columns import ALL_COLUMNS  # noqa: E402
from nodule_eval.sheets.template import (  # noqa: E402
    FIRST_DATA_ROW,
    HEADER_ROW,
    SHEET_DETAIL,
    write_template,
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
# 色板：三档底色互不相同、且低饱和可打印
# --------------------------------------------------------------------------


def test_three_tied_verdicts_have_distinct_fills() -> None:
    """命中 / 漏判 / 失败三档底色必须互不相同。

    两档撞色 = 表上分不出「漏判」和「失败」，
    而这两件事的处置完全不同（一个是安全事件，一个是没跑成）。
    """

    fills = [tokens.VERDICT_FILLS[verdict] for verdict in tokens.TIED_VERDICTS]
    assert len(fills) == 3
    assert len(set(fills)) == 3


def test_tied_fills_differ_from_header_fill() -> None:
    """三档底色不能与表头深蓝撞色 —— 否则表头和明细糊成一片。"""

    for verdict in tokens.TIED_VERDICTS:
        assert tokens.VERDICT_FILLS[verdict] != tokens.COLOR_HEADER_FILL


def test_only_three_verdicts_are_tinted() -> None:
    """只有三档有底色；误报与真阴性保持白底。

    底色在这张表里的含义是「这一行需要人看一眼」。
    把误报也标红，满屏红色反而稀释了真正要看的漏诊行。
    """

    assert tokens.TIED_VERDICTS == (VERDICT_HIT, VERDICT_MISS, VERDICT_FAILED)
    assert VERDICT_FALSE_POSITIVE not in tokens.VERDICT_FILLS
    assert VERDICT_TRUE_NEGATIVE not in tokens.VERDICT_FILLS
    assert tokens.verdict_fill(VERDICT_FALSE_POSITIVE) is None
    assert tokens.verdict_fill(VERDICT_TRUE_NEGATIVE) is None


@pytest.mark.parametrize("verdict", [VERDICT_HIT, VERDICT_MISS, VERDICT_FAILED])
def test_fills_are_low_saturation(verdict: str) -> None:
    """底色必须低饱和 —— 彩打不跳色、复印不糊成一片。"""

    color = tokens.VERDICT_FILLS[verdict]
    assert tokens.saturation(color) <= tokens.MAX_FILL_SATURATION, (verdict, color)


@pytest.mark.parametrize("verdict", [VERDICT_HIT, VERDICT_MISS, VERDICT_FAILED])
def test_fills_are_light_enough_to_print(verdict: str) -> None:
    """底色必须够浅 —— 压不住黑色正文，打印发灰。"""

    color = tokens.VERDICT_FILLS[verdict]
    assert tokens.relative_luminance(color) >= tokens.MIN_FILL_LUMINANCE, (verdict, color)


@pytest.mark.parametrize("verdict", [VERDICT_HIT, VERDICT_MISS, VERDICT_FAILED])
def test_fills_keep_text_readable(verdict: str) -> None:
    """底色与正文的对比度达 WCAG AA —— 黑白打印也读得清。"""

    color = tokens.VERDICT_FILLS[verdict]
    ratio = tokens.contrast_ratio(tokens.COLOR_TEXT, color)
    assert ratio >= tokens.MIN_FILL_CONTRAST, (verdict, color, ratio)


def test_header_fill_keeps_white_text_readable() -> None:
    """表头深蓝底 + 白字，对比度也要够 —— 表头是表里唯一的深色块。"""

    ratio = tokens.contrast_ratio(tokens.COLOR_HEADER_TEXT, tokens.COLOR_HEADER_FILL)
    assert ratio >= tokens.MIN_FILL_CONTRAST, ratio


# --------------------------------------------------------------------------
# 表头与列宽：真的写进了工作簿
# --------------------------------------------------------------------------


def test_header_is_frozen_in_workbook(workbook) -> None:
    """表头必须冻结 —— 两百例的表往下滚，表头不在就认不出列了。

    冻结锚点 = **第一条数据行**（表头行及其以上全部冻住）。任务 44 把
    汇总区放到表头之上，冻结锚点随之从 A2 挪到 A12 —— 断言按版面推导，
    不写死字符串，表头行一挪这里就会跟着指出差在哪。
    """

    sheet = workbook[SHEET_DETAIL]
    assert FIRST_DATA_ROW > 1
    assert sheet.freeze_panes == f"A{FIRST_DATA_ROW}"


def test_header_row_height_is_fixed(workbook) -> None:
    """表头行高固定，打印出来层次不因行数变化而塌掉。"""

    sheet = workbook[SHEET_DETAIL]
    assert sheet.row_dimensions[HEADER_ROW].height == tokens.ROW_HEIGHT_HEADER


def test_header_style_is_deep_blue_with_white_text(workbook) -> None:
    """表头：深蓝底、白字、加粗、居中。"""

    sheet = workbook[SHEET_DETAIL]
    cell = sheet.cell(row=HEADER_ROW, column=1)
    assert cell.fill.start_color.rgb.endswith(tokens.COLOR_HEADER_FILL)
    assert cell.font.color.rgb.endswith(tokens.COLOR_HEADER_TEXT)
    assert cell.font.bold is True
    assert cell.font.name == tokens.FONT_FAMILY
    assert cell.alignment.horizontal == "center"


def test_column_widths_are_written_from_tokens(workbook) -> None:
    """各列列宽按 token 写入 —— 列宽一改，模板与回填两处一起变。"""

    sheet = workbook[SHEET_DETAIL]
    from openpyxl.utils import get_column_letter

    for index, name in enumerate(ALL_COLUMNS, start=1):
        letter = get_column_letter(index)
        assert sheet.column_dimensions[letter].width == tokens.COLUMN_WIDTHS[name], name


def test_template_column_widths_come_from_token_module() -> None:
    """模板不再自己留一份列宽字面量 —— 改一处两处生效。"""

    assert template_module.COLUMN_WIDTHS == dict(tokens.COLUMN_WIDTHS)


def test_data_rows_have_fixed_height_and_border(workbook) -> None:
    """明细行行高与分隔线固定，两百行与二十行看起来一样整齐。"""

    sheet = workbook[SHEET_DETAIL]
    row = FIRST_DATA_ROW + 1
    assert sheet.row_dimensions[row].height == tokens.ROW_HEIGHT_DATA
    cell = sheet.cell(row=row, column=1)
    assert cell.border.bottom is not None
    assert cell.border.bottom.color.rgb.endswith(tokens.COLOR_BORDER)


# --------------------------------------------------------------------------
# 三档底色的条件格式
# --------------------------------------------------------------------------


def test_conditional_formatting_covers_three_verdicts(workbook) -> None:
    """判定列要挂三档条件格式 —— 回填只写值，底色由表格自己算。"""

    sheet = workbook[SHEET_DETAIL]
    rules = [
        rule
        for formatting in sheet.conditional_formatting
        for rule in formatting.rules
    ]
    assert len(rules) == 3

    formulas = " ".join(rule.formula[0] for rule in rules)
    for verdict in tokens.TIED_VERDICTS:
        assert f'"{verdict}"' in formulas, verdict


def test_conditional_formatting_targets_the_verdict_column(workbook) -> None:
    """条件格式挂在判定列上，不能挂到别的列去。"""

    sheet = workbook[SHEET_DETAIL]
    from openpyxl.utils import get_column_letter

    verdict_index = ALL_COLUMNS.index("verdict") + 1
    letter = get_column_letter(verdict_index)
    targets = {
        str(formatting.sqref)
        for formatting in sheet.conditional_formatting
    }
    assert targets, "条件格式一条都没挂上"
    for target in targets:
        assert target.startswith(letter), target


def test_conditional_formatting_uses_token_colors(workbook) -> None:
    """规则里的底色就是 token 里那三档 —— 配色只有一处定义。"""

    sheet = workbook[SHEET_DETAIL]
    found: list[str] = []
    for formatting in sheet.conditional_formatting:
        for rule in formatting.rules:
            fill = rule.dxf.fill
            if fill is None or fill.bgColor is None:
                continue
            found.append(str(fill.bgColor.rgb)[-6:])
            found.append(str(fill.start_color.rgb)[-6:])

    for verdict in tokens.TIED_VERDICTS:
        assert tokens.VERDICT_FILLS[verdict] in found, verdict


def test_apply_verdict_rules_requires_the_column() -> None:
    """判定列不在表里就报错 —— 挂错列的规则会静默不生效。"""

    workbook = Workbook()
    sheet = workbook.active
    with pytest.raises(KeyError):
        tokens.apply_verdict_conditional_formatting(sheet, ("accession_no",), 2, 5)


# --------------------------------------------------------------------------
# 数字格式：指标四位小数、计数整数、列宽固定
# --------------------------------------------------------------------------


def test_metric_cells_use_four_decimal_format() -> None:
    """四项指标统一四位小数 —— 汇总行要给人手算复核，位数必须固定。"""

    assert tokens.NUMBER_FORMAT_METRIC == "0.0000"
    workbook = Workbook()
    cell = workbook.active.cell(row=1, column=1, value=0.5)
    tokens.style_metric_cell(cell)
    assert cell.number_format == "0.0000"
    assert cell.alignment.horizontal == "right"
    assert cell.font.name == tokens.FONT_FAMILY_MONO


def test_count_cells_use_integer_format() -> None:
    """原始计数是整数，不带小数尾巴。"""

    assert tokens.NUMBER_FORMAT_COUNT == "0"
    workbook = Workbook()
    cell = workbook.active.cell(row=1, column=1, value=18)
    tokens.style_count_cell(cell)
    assert cell.number_format == "0"
    assert cell.font.name == tokens.FONT_FAMILY_MONO


def test_numeric_cells_are_right_aligned_and_monospaced() -> None:
    """数字列等宽右对齐 —— 上下两行的数字才能对齐着比大小。"""

    workbook = Workbook()
    sheet = workbook.active
    tokens.apply_data_row_style(sheet, 1, ("accession_no", "size"), numeric_columns=("size",))
    numeric = sheet.cell(row=1, column=2)
    text = sheet.cell(row=1, column=1)
    assert numeric.alignment.horizontal == "right"
    assert numeric.font.name == tokens.FONT_FAMILY_MONO
    assert text.alignment.horizontal == "left"


# --------------------------------------------------------------------------
# 样式只有一处定义（模板生成与回填共用）
# --------------------------------------------------------------------------


def test_tokens_manifest_exports_every_family() -> None:
    """token 可导出 —— 模板生成、回填、文档三处读同一份。"""

    manifest = tokens.tokens_manifest()
    assert set(manifest) == {
        "colors",
        "verdict_fills",
        "fonts",
        "row_heights",
        "column_widths",
        "number_formats",
    }
    assert manifest["colors"]["header_fill"] == tokens.COLOR_HEADER_FILL
    assert manifest["verdict_fills"] == dict(tokens.VERDICT_FILLS)
    assert manifest["column_widths"] == dict(tokens.COLUMN_WIDTHS)
    assert manifest["number_formats"]["metric"] == "0.0000"


def test_manifest_is_serialisable() -> None:
    """导出的 token 得能落成 JSON（文档与自检脚本要用）。"""

    import json

    payload = json.dumps(tokens.tokens_manifest(), ensure_ascii=False)
    assert "header_fill" in payload


def test_style_helpers_share_the_same_font_family() -> None:
    """字体族只有一处定义 —— 表格里出现第二种字体就是漏改。"""

    assert tokens.FONT_FAMILY == "Microsoft YaHei"
    assert tokens.FONT_FAMILY_MONO == "Consolas"
    workbook = Workbook()
    sheet = workbook.active
    tokens.style_label_cell(sheet.cell(row=1, column=1))
    tokens.style_title_cell(sheet.cell(row=2, column=1))
    tokens.style_section_cell(sheet.cell(row=3, column=1))
    tokens.style_alert_cell(sheet.cell(row=4, column=1))
    for row in range(1, 5):
        assert sheet.cell(row=row, column=1).font.name == tokens.FONT_FAMILY


def test_alert_text_uses_alert_color() -> None:
    """失败原因与挂起提示用告警色 —— 跟底色分开，别混成一片红。"""

    workbook = Workbook()
    cell = workbook.active.cell(row=1, column=1, value="推理服务暂时不可用")
    tokens.style_alert_cell(cell)
    assert cell.font.color.rgb.endswith(tokens.COLOR_ALERT)
