"""评估表的**视觉 token 与 openpyxl 样式工厂**（任务 43）。

本模块是表格界面「长什么样」的唯一真源：色板、字体、字号、行高、列宽、
数字格式全在这里定一次，模板生成（任务 44）与逐例回填（任务 46）都从这里取。
两处各写一份色值，改一次配色就会漏改一处 —— 一半新版一半旧版，
印出来颜色深浅不一，没人说得清哪一行是哪一档。

**设计基调：对标放射科评估表，不是网页后台。**

- **低饱和、可打印**：三档底色（命中 / 漏判 / 失败）都用低饱和浅色，
  黑白激光打印机打出来层次还在，彩打也不刺眼；底色与正文的对比度按
  WCAG AA（≥ 4.5:1）守着，保证黑白打印时字也读得清。
  :func:`saturation` 与 :func:`relative_luminance` 让这条约束**可测**，
  不是嘴上说说（``tests/test_tokens.py`` 逐档断言）。
- **表头深蓝、白字、冻结首行**：两百例的表往下滚，表头必须一直在。
- **数字列等宽右对齐**：敏感度 / 特异度 / 阳性预测值 / 阴性预测值四位小数，
  原始计数整数；等宽字体让上下两行的数字对齐，扫一眼就能比大小。
- **列宽固定**：列宽一变，打印出来就错位；模板与回填共用同一份列宽常量。

**为什么只有三档底色**：底色在这张表里的含义是「这一行需要人看一眼」。
命中（hit）不需要动，漏判（miss）是安全事件必须复核，失败（failed）压根没结论。
误报与真阴性保持白底 —— 误报是特异度问题，由汇总指标与漏诊清单承担，
把满屏标红反而稀释了真正要看的漏诊行。
"""

from __future__ import annotations

from typing import Final, Mapping, Sequence

from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..contracts import VERDICT_FAILED, VERDICT_HIT, VERDICT_MISS

# --------------------------------------------------------------------------
# 色板（全部大写六位十六进制，不带 # —— openpyxl 的 ARGB 写法）
# --------------------------------------------------------------------------

#: 表头底色 —— 深蓝（放射科评估表的惯例：深色表头 + 白字，投影与打印都清楚）。
COLOR_HEADER_FILL: Final[str] = "1F3A5F"

#: 表头文字色 —— 纯白。
COLOR_HEADER_TEXT: Final[str] = "FFFFFF"

#: 区块标题（汇总区 / 漏诊区的小标题）底色 —— 极浅蓝灰。
COLOR_SECTION_FILL: Final[str] = "E8EEF5"

#: 正文文字色 —— 近黑，打印最稳。
COLOR_TEXT: Final[str] = "1F2933"

#: 次级文字色（说明、备注）。
COLOR_TEXT_SECONDARY: Final[str] = "52616F"

#: 三级文字色（占位提示、列头旁的小字）。
COLOR_TEXT_MUTED: Final[str] = "8A97A3"

#: 网格线色 —— 极浅灰，打印几乎看不见但仍有分隔。
COLOR_BORDER: Final[str] = "D0D7E2"

#: 命中档底色 —— 淡绿。
COLOR_HIT_FILL: Final[str] = "E6F0E2"

#: 漏判档底色 —— 淡橙（安全事件，暖色提醒）。
COLOR_MISS_FILL: Final[str] = "FBE8DC"

#: 失败档底色 —— 淡红。
COLOR_FAILED_FILL: Final[str] = "F5DFDF"

#: 告警色 —— 失败原因、挂起提示的文字色（不用作底色）。
COLOR_ALERT: Final[str] = "B54708"

#: 状态栏底色 —— 极浅灰，比区块标题更轻：状态栏是「附注」，不该抢眼。
COLOR_STATUS_FILL: Final[str] = "F1F3F6"

#: 分组表头（既有列组）的分隔线色 —— 比正文行分隔线重一档，
#: 让「科室填的」与「引擎填的」两段一眼分得开。
COLOR_GROUP_BORDER: Final[str] = "9AA7B4"

#: 三档判定底色：**判定值 → 底色**的唯一真源。
#:
#: 判定值取自 :mod:`nodule_eval.contracts` 的五值枚举；
#: 只有 hit / miss / failed 有底色，false_positive 与 true_negative 保持白底
#: （理由见模块文档）。
VERDICT_FILLS: Final[Mapping[str, str]] = {
    VERDICT_HIT: COLOR_HIT_FILL,
    VERDICT_MISS: COLOR_MISS_FILL,
    VERDICT_FAILED: COLOR_FAILED_FILL,
}

#: 有底色的判定值（按「需要人看一眼」的严重度排序）。
TIED_VERDICTS: Final[tuple[str, ...]] = (VERDICT_HIT, VERDICT_MISS, VERDICT_FAILED)

# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------

#: 正文字体族 —— 中文系统栈里在院内 Windows 机器上最稳的一个。
#: （``Microsoft YaHei`` 随 Windows 与 WPS 一起装，不需要额外字体包。）
FONT_FAMILY: Final[str] = "Microsoft YaHei"

#: 数字列等宽字体族 —— 让上下两行的数字对齐。
FONT_FAMILY_MONO: Final[str] = "Consolas"

#: 页面标题字号。
FONT_SIZE_TITLE: Final[int] = 15

#: 区块标题字号。
FONT_SIZE_SECTION: Final[int] = 11

#: 表头字号。
FONT_SIZE_HEADER: Final[int] = 10

#: 正文字号。
FONT_SIZE_BODY: Final[int] = 10

#: 辅助文字字号。
FONT_SIZE_SMALL: Final[int] = 9

# --------------------------------------------------------------------------
# 行高与列宽（Excel 字符宽 / 磅）
# --------------------------------------------------------------------------

#: 页面标题行高。
ROW_HEIGHT_TITLE: Final[int] = 28

#: 区块标题行高。
ROW_HEIGHT_SECTION: Final[int] = 24

#: 表头行高。
ROW_HEIGHT_HEADER: Final[int] = 22

#: 明细行行高。
ROW_HEIGHT_DATA: Final[int] = 18

#: 明细区各列的固定列宽。**列宽固定是打印不错位的前提**，
#: 模板生成与逐例回填共用这一份，改一处两处生效。
COLUMN_WIDTHS: Final[Mapping[str, int]] = {
    "accession_no": 18,
    "gold_standard_text": 46,
    "image_ref": 26,
    "verdict": 12,
    "model_findings": 40,
    "failure_reason": 34,
    "tool_call_id": 26,
    "needs_review": 14,
    "normalize_source": 14,
}

#: 汇总区标签列宽（任务 44 的汇总区用）。
SUMMARY_LABEL_WIDTH: Final[int] = 22

#: 汇总区数值列宽（任务 44 的汇总区用）。
SUMMARY_VALUE_WIDTH: Final[int] = 16

#: 未登记列名时的兜底列宽。
DEFAULT_COLUMN_WIDTH: Final[int] = 18

# --------------------------------------------------------------------------
# 数字格式
# --------------------------------------------------------------------------

#: 四项指标的统一数字格式 —— **四位小数**。
#: 汇总行要给人手算复核，位数必须固定；用 ``0.0000`` 而不是 ``General``。
NUMBER_FORMAT_METRIC: Final[str] = "0.0000"

#: 原始计数的统一数字格式 —— 整数。
NUMBER_FORMAT_COUNT: Final[str] = "0"

#: 尺寸（毫米）的数字格式 —— 一位小数。
NUMBER_FORMAT_SIZE: Final[str] = "0.0"

#: 数字格式按用途分派。
NUMBER_FORMATS: Final[Mapping[str, str]] = {
    "metric": NUMBER_FORMAT_METRIC,
    "count": NUMBER_FORMAT_COUNT,
    "size": NUMBER_FORMAT_SIZE,
}

# --------------------------------------------------------------------------
# 颜色工具（可打印性可测）
# --------------------------------------------------------------------------


def _rgb(color: str) -> tuple[int, int, int]:
    """把 ``RRGGBB``（可带 ``#``）解析成三通道整数。"""

    text = color.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"颜色须为六位十六进制，收到：{color!r}")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        raise ValueError(f"颜色须为六位十六进制，收到：{color!r}") from None


def saturation(color: str) -> float:
    """颜色的饱和度（HSV 口径，0.0–1.0）。

    浅色底色的饱和度天然低；这个数用来把「低饱和」这条设计约束**变成可断言的数字**
    —— 低饱和的底色在彩打与复印时不会糊成一片，层次仍在。
    """

    r, g, b = _rgb(color)
    high = max(r, g, b)
    if high == 0:
        return 0.0
    return (high - min(r, g, b)) / high


def relative_luminance(color: str) -> float:
    """颜色的相对亮度（WCAG 口径，0.0–1.0）。

    底色越亮，这张表在黑白打印时越省墨、字越清楚；
    这个数用来守住「底色必须浅」这条约束。
    """

    def channel(value: int) -> float:
        srgb = value / 255
        if srgb <= 0.03928:
            return srgb / 12.92
        return ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """两个颜色之间的对比度（WCAG 口径，1.0–21.0）。

    AA 级正文要求 ≥ 4.5:1。底色浅 + 文字近黑，这一栏轻松过；
    把它写成函数而不是口头保证，是为了让「可打印」这条要求可核对。
    """

    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


#: 底色的饱和度上限（超过就不是「低饱和」，彩打会跳）。
MAX_FILL_SATURATION: Final[float] = 0.25

#: 底色的亮度下限（低于这个值就压不住黑色正文，打印发灰）。
MIN_FILL_LUMINANCE: Final[float] = 0.75

#: 底色与正文的对比度下限（WCAG AA）。
MIN_FILL_CONTRAST: Final[float] = 4.5


# --------------------------------------------------------------------------
# 样式工厂
# --------------------------------------------------------------------------


def solid_fill(color: str) -> PatternFill:
    """实心底色。"""

    return PatternFill(start_color=color, end_color=color, fill_type="solid")


def header_fill() -> PatternFill:
    """表头底色（深蓝）。"""

    return solid_fill(COLOR_HEADER_FILL)


def section_fill() -> PatternFill:
    """区块标题底色（浅蓝灰）。"""

    return solid_fill(COLOR_SECTION_FILL)


def verdict_fill(verdict: str) -> PatternFill | None:
    """判定值 → 底色；没有底色的判定值返回 ``None``（保持白底）。"""

    color = VERDICT_FILLS.get(verdict)
    if color is None:
        return None
    return solid_fill(color)


def header_font() -> Font:
    """表头字体：中文系统字体、加粗、白字。"""

    return Font(name=FONT_FAMILY, size=FONT_SIZE_HEADER, bold=True, color=COLOR_HEADER_TEXT)


def body_font(
    *,
    bold: bool = False,
    color: str = COLOR_TEXT,
    size: int = FONT_SIZE_BODY,
    mono: bool = False,
) -> Font:
    """正文字体。``mono=True`` 用于数字列（等宽对齐）。"""

    return Font(
        name=FONT_FAMILY_MONO if mono else FONT_FAMILY,
        size=size,
        bold=bold,
        color=color,
    )


def header_alignment() -> Alignment:
    """表头对齐：水平居中、垂直居中。"""

    return Alignment(horizontal="center", vertical="center")


def body_alignment(horizontal: str = "left", *, wrap_text: bool = False) -> Alignment:
    """正文对齐：默认左对齐，``wrap_text`` 用于长文本列。"""

    return Alignment(horizontal=horizontal, vertical="center", wrap_text=wrap_text)


def data_border() -> Border:
    """明细行的下边框（极浅灰）。"""

    return Border(bottom=Side(style="thin", color=COLOR_BORDER))


def group_border() -> Border:
    """分组表头的分隔线（比正文行重一档 —— 两段要分得开）。"""

    return Border(bottom=Side(style="medium", color=COLOR_GROUP_BORDER))


def status_fill() -> PatternFill:
    """状态栏底色（极浅灰）。"""

    return solid_fill(COLOR_STATUS_FILL)


def style_metric_cell(cell, *, size: int = FONT_SIZE_BODY) -> None:
    """把一个单元格排成「指标」样式：等宽字体、右对齐、四位小数。"""

    cell.font = body_font(mono=True, size=size)
    cell.alignment = body_alignment("right")
    cell.number_format = NUMBER_FORMAT_METRIC


def style_count_cell(cell, *, size: int = FONT_SIZE_BODY) -> None:
    """把一个单元格排成「原始计数」样式：等宽字体、右对齐、整数。"""

    cell.font = body_font(mono=True, size=size)
    cell.alignment = body_alignment("right")
    cell.number_format = NUMBER_FORMAT_COUNT


def style_label_cell(cell, *, bold: bool = True, size: int = FONT_SIZE_BODY) -> None:
    """把一个单元格排成「标签」样式：中文正文字体、左对齐。"""

    cell.font = body_font(bold=bold, size=size)
    cell.alignment = body_alignment("left")


def style_title_cell(cell) -> None:
    """把一个单元格排成「页面标题」样式。"""

    cell.font = body_font(bold=True, size=FONT_SIZE_TITLE)
    cell.alignment = body_alignment("left")


def style_section_cell(cell) -> None:
    """把一个单元格排成「区块标题」样式（浅蓝灰底 + 加粗）。"""

    cell.font = body_font(bold=True, size=FONT_SIZE_SECTION)
    cell.alignment = body_alignment("left")
    cell.fill = section_fill()


def style_alert_cell(cell, *, size: int = FONT_SIZE_SMALL) -> None:
    """把一个单元格排成「告警」样式（失败原因、挂起提示）。"""

    cell.font = body_font(color=COLOR_ALERT, size=size)
    cell.alignment = body_alignment("left", wrap_text=True)


def style_meta_cell(cell) -> None:
    """元信息行（模型 / 数据集 / 运行ID / 生成时间）—— 次级文字。"""

    cell.font = body_font(color=COLOR_TEXT_SECONDARY, size=FONT_SIZE_SMALL)
    cell.alignment = body_alignment("left")


def style_summary_label_cell(cell) -> None:
    """汇总区的指标标签 —— 加粗、右对齐、浅蓝灰底。

    标签在数值**上方**：一排标签一排数值，读的人从上往下扫一遍
    就知道每个数是什么，不必回头对照图例。
    """

    cell.font = body_font(bold=True, color=COLOR_TEXT_SECONDARY, size=FONT_SIZE_SMALL)
    cell.alignment = body_alignment("right")
    cell.fill = section_fill()


def style_summary_note_cell(cell) -> None:
    """汇总区里的文字格（降级说明）—— 左对齐、次级文字。"""

    cell.font = body_font(color=COLOR_TEXT_SECONDARY, size=FONT_SIZE_SMALL)
    cell.alignment = body_alignment("left", wrap_text=True)
    cell.fill = section_fill()


def style_group_label_cell(cell, *, filled: bool) -> None:
    """分组表头 —— 既有列组（白底）与新增列组（浅蓝灰底）。

    ``filled=False`` 是**既有列组**：科室维护的原始件，不该被装饰。
    ``filled=True`` 是**新增列组**：引擎回填的展示件，底色标出来，
    打开表就知道哪六列是机器写的。
    """

    cell.font = body_font(bold=True, size=FONT_SIZE_SECTION)
    cell.alignment = body_alignment("left")
    cell.border = group_border()
    if filled:
        cell.fill = section_fill()


def style_missed_header_cell(cell) -> None:
    """漏诊明细区的表头 —— 深蓝底、白字（与明细区表头同一套）。"""

    cell.fill = header_fill()
    cell.font = header_font()
    cell.alignment = header_alignment()


def style_status_cell(cell) -> None:
    """底部状态栏 —— 极浅灰底、次级文字。"""

    cell.font = body_font(color=COLOR_TEXT_SECONDARY, size=FONT_SIZE_SMALL)
    cell.alignment = body_alignment("left")
    cell.fill = status_fill()


# --------------------------------------------------------------------------
# 工作簿级应用器（模板生成与回填共用）
# --------------------------------------------------------------------------


def apply_column_widths(
    worksheet: Worksheet,
    columns: Sequence[str],
    widths: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """按 token 里的列宽表写入各列宽度，返回实际写进去的列宽。"""

    table = COLUMN_WIDTHS if widths is None else widths
    applied: dict[str, int] = {}
    for index, name in enumerate(columns, start=1):
        width = table.get(name, DEFAULT_COLUMN_WIDTH)
        worksheet.column_dimensions[get_column_letter(index)].width = width
        applied[name] = width
    return applied


def apply_header_row(
    worksheet: Worksheet,
    columns: Sequence[str],
    row: int = 1,
    *,
    freeze: bool = True,
    first_data_row: int | None = None,
) -> None:
    """写表头行样式：深蓝底、白字、加粗、居中，并冻结首行。

    列宽也一并落上 —— 表头与列宽是同一件事的两面，改一次配色不该只改一半。
    """

    fill = header_fill()
    font = header_font()
    alignment = header_alignment()

    for index, name in enumerate(columns, start=1):
        cell = worksheet.cell(row=row, column=index, value=name)
        cell.fill = fill
        cell.font = font
        cell.alignment = alignment

    worksheet.row_dimensions[row].height = ROW_HEIGHT_HEADER
    apply_column_widths(worksheet, columns)

    if freeze:
        anchor = first_data_row if first_data_row is not None else row + 1
        worksheet.freeze_panes = worksheet.cell(row=anchor, column=1)


def apply_verdict_conditional_formatting(
    worksheet: Worksheet,
    columns: Sequence[str],
    first_row: int,
    last_row: int | None = None,
    *,
    verdict_column: str = "verdict",
) -> list[str]:
    """给判定列挂三档底色的条件格式规则。

    用**条件格式**而不是逐格刷底色：回填（任务 46）只写值、不碰样式，
    底色由 Excel/WPS 自己算 —— 表格被人工改了判定值，底色跟着变，
    不会出现「值是漏判、底色还是绿的」这种自相矛盾。
    """

    if verdict_column not in columns:
        raise KeyError(f"评估表里没有这一列：{verdict_column}")

    index = columns.index(verdict_column) + 1
    letter = get_column_letter(index)
    end = last_row if last_row is not None else worksheet.max_row
    if end < first_row:
        return []

    target = f"{letter}{first_row}:{letter}{end}"
    applied: list[str] = []
    for verdict in TIED_VERDICTS:
        color = VERDICT_FILLS[verdict]
        rule = FormulaRule(
            formula=[f'${letter}{first_row}="{verdict}"'],
            fill=solid_fill(color),
            stopIfTrue=False,
        )
        worksheet.conditional_formatting.add(target, rule)
        applied.append(verdict)
    return applied


def apply_data_row_style(
    worksheet: Worksheet,
    row: int,
    columns: Sequence[str],
    *,
    numeric_columns: Sequence[str] = (),
) -> None:
    """给一行明细落基础样式：行高 + 下边框；数字列用等宽右对齐。"""

    worksheet.row_dimensions[row].height = ROW_HEIGHT_DATA
    border = data_border()
    for index, name in enumerate(columns, start=1):
        cell = worksheet.cell(row=row, column=index)
        cell.border = border
        if name in numeric_columns:
            cell.font = body_font(mono=True)
            cell.alignment = body_alignment("right")
        else:
            cell.font = body_font()
            cell.alignment = body_alignment("left")


# --------------------------------------------------------------------------
# 导出（供文档 / 任务 44 与 46 复用同一份 token）
# --------------------------------------------------------------------------


def tokens_manifest() -> dict:
    """把整份 token 导成可序列化的字典。

    用途有二：模板生成与回填都从这一份取（改一处两处生效），
    以及文档 / 自检脚本可以把配色与数字格式打印出来核对。
    """

    return {
        "colors": {
            "header_fill": COLOR_HEADER_FILL,
            "header_text": COLOR_HEADER_TEXT,
            "section_fill": COLOR_SECTION_FILL,
            "text": COLOR_TEXT,
            "text_secondary": COLOR_TEXT_SECONDARY,
            "text_muted": COLOR_TEXT_MUTED,
            "border": COLOR_BORDER,
            "hit_fill": COLOR_HIT_FILL,
            "miss_fill": COLOR_MISS_FILL,
            "failed_fill": COLOR_FAILED_FILL,
            "alert": COLOR_ALERT,
            "status_fill": COLOR_STATUS_FILL,
            "group_border": COLOR_GROUP_BORDER,
        },
        "verdict_fills": dict(VERDICT_FILLS),
        "fonts": {
            "family": FONT_FAMILY,
            "mono_family": FONT_FAMILY_MONO,
            "title": FONT_SIZE_TITLE,
            "section": FONT_SIZE_SECTION,
            "header": FONT_SIZE_HEADER,
            "body": FONT_SIZE_BODY,
            "small": FONT_SIZE_SMALL,
        },
        "row_heights": {
            "title": ROW_HEIGHT_TITLE,
            "section": ROW_HEIGHT_SECTION,
            "header": ROW_HEIGHT_HEADER,
            "data": ROW_HEIGHT_DATA,
        },
        "column_widths": dict(COLUMN_WIDTHS),
        "number_formats": dict(NUMBER_FORMATS),
    }
