"""评估表读取层的边界测试。

这一层是**表格到引擎的第一道接缝**，出错的代价全落在结论上：

- **少读一行** —— 那一例不进评估，敏感度按更小的样本算，看起来还挺好看；
- **改写一个字** —— 「未见异常」被归一成「见异常」，阳性读成阴性，敏感度直接崩；
- **猜一个值** —— 检查号缺失就填个空的，模型拿不到影像，回来的「未检出」
  混进指标分母，把「没跑成」算成「模型没看见」。

所以这里断言的**不是**「能读出几行」，而是四类边界各自的处置：

1. 空白行 / 缺检查号的行 —— **计数并报行号**，不静默丢；
2. 必填缺失 —— **报错并指到第几行**；
3. 超长文本 —— **截断并标记**，原文不静默丢弃；
4. 真实数据形态 —— 全角数字、中文标点、GBK/UTF-8 混编、跨行叙述。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nodule_eval.contracts import parse_request  # noqa: E402
from nodule_eval.errors import ErrorCode, NoduleEvalError  # noqa: E402
from nodule_eval.sheets.columns import ALL_COLUMNS  # noqa: E402
from nodule_eval.sheets.read import (  # noqa: E402
    MAX_GOLD_TEXT_CHARS,
    TRUNCATION_MARKER,
    decode_bytes,
    fragment_spans,
    normalize_gold_standard_text,
    normalize_identifier,
    read_sheet,
)
from nodule_eval.sheets.template import FIRST_DATA_ROW, SHEET_DETAIL, write_template  # noqa: E402

HEADER = "accession_no,gold_standard_text,image_ref"


def _write_csv(tmp_path: Path, name: str, text: str) -> Path:
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# 基本读取
# --------------------------------------------------------------------------


def test_reads_csv_rows_into_case_inputs(tmp_path: Path) -> None:
    """一张正常的表读成两例，字段与行号都对。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,右下肺见磨玻璃结节，约 8mm。,study://uid-1\n"
        "ACC-002,双肺未见明显结节影。,\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 2
    first, second = result.cases
    assert first.accession_no == "ACC-001"
    assert first.gold_standard_text == "右下肺见磨玻璃结节，约 8mm。"
    assert first.image_ref == "study://uid-1"
    assert first.row_no == 2
    assert second.accession_no == "ACC-002"
    assert second.image_ref is None
    assert second.row_no == 3
    assert result.header_row == 1


def test_row_number_matches_the_row_a_clerk_sees(tmp_path: Path) -> None:
    """行号是**工作表里的行号**（含表头）—— 回填要靠它定位到那一行。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n" + "ACC-001,右下肺见结节。,\n" + "ACC-002,双肺未见异常。,\n",
    )

    result = read_sheet(path)

    assert [case.row_no for case in result.cases] == [2, 3]
    assert [row.row_no for row in result.rows] == [2, 3]


def test_reads_xlsm_workbook(tmp_path: Path) -> None:
    """xlsm 也能读 —— 这是产品的主入口（表格内插件）。"""

    from openpyxl import load_workbook

    target = write_template(tmp_path / "评估表.xlsm", prefilled_rows=3)
    workbook = load_workbook(target)
    sheet = workbook[SHEET_DETAIL]
    sheet.cell(row=FIRST_DATA_ROW, column=1, value="ACC-001")
    sheet.cell(row=FIRST_DATA_ROW, column=2, value="右下肺见磨玻璃结节。")
    sheet.cell(row=FIRST_DATA_ROW + 1, column=1, value="ACC-002")
    sheet.cell(row=FIRST_DATA_ROW + 1, column=2, value="双肺未见明显结节影。")
    workbook.save(target)

    result = read_sheet(target)

    assert [case.accession_no for case in result.cases] == ["ACC-001", "ACC-002"]
    assert result.cases[0].row_no == FIRST_DATA_ROW
    assert result.sheet_name == SHEET_DETAIL


# --------------------------------------------------------------------------
# 边界一：空白行与缺检查号的行 —— 计数并报行号
# --------------------------------------------------------------------------


def test_blank_rows_are_counted_not_dropped(tmp_path: Path) -> None:
    """模板预留的空白行跳过但**计数** —— 加起来要等于表里的数据行数。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,右下肺见结节。,\n"
        "\n"
        ",\n"
        "ACC-002,双肺未见异常。,\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 2
    assert result.blank_rows == 2
    counts = result.counts()
    assert counts["cases"] + counts["blank_rows"] + counts["skipped_rows"] == 4


def test_template_blank_rows_are_counted(tmp_path: Path) -> None:
    """模板自带 200 行预留空白 —— 读模板不该凭空造出 200 例。"""

    target = write_template(tmp_path / "评估表.xlsm", prefilled_rows=5)

    result = read_sheet(target)

    assert result.cases == ()
    assert result.blank_rows == 5


def test_row_without_accession_no_is_skipped_and_row_number_reported(tmp_path: Path) -> None:
    """有内容却没有检查号的行 —— 跳过、**记行号**、给提示。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,右下肺见结节。,\n"
        ",右下肺另见结节。,\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 1
    assert result.skipped_rows == (3,)
    assert any("第 3 行" in warning for warning in result.warnings)


def test_row_without_accession_no_is_never_merged_into_the_previous_case(tmp_path: Path) -> None:
    """没有检查号的行**不会**被并进上一行 —— 那一例的金标准不能被污染。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,双肺未见异常。,\n"
        ",右下肺另见结节。,\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 1
    assert result.cases[0].gold_standard_text == "双肺未见异常。"
    assert result.joins == ()
    assert result.skipped_rows == (3,)


# --------------------------------------------------------------------------
# 边界二：必填缺失 —— 报错并指到第几行
# --------------------------------------------------------------------------


def test_blank_gold_standard_reports_the_row_number(tmp_path: Path) -> None:
    """金标准是空白的行 —— 报错并点名第几行，不静默丢。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n" + "ACC-001,右下肺见结节。,\n" + "ACC-002,   ,\n",
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        read_sheet(path)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "第 3 行" in excinfo.value.message
    assert "gold_standard_text" in excinfo.value.message


def test_missing_existing_column_names_the_column(tmp_path: Path) -> None:
    """表里没有既有列 —— 报错并**点名缺哪一列**，不按空表处理。"""

    path = _write_csv(tmp_path, "表.csv", "检查号,备注\nACC-001,空\n")

    with pytest.raises(NoduleEvalError) as excinfo:
        read_sheet(path)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "缺少既有列：gold_standard_text" in excinfo.value.message


def test_missing_accession_column_names_the_column(tmp_path: Path) -> None:
    """缺哪一列就点哪一列 —— 两列都缺时两个都点名。"""

    path = _write_csv(tmp_path, "表.csv", "金标准原文,备注\n右下肺见结节。,空\n")

    with pytest.raises(NoduleEvalError) as excinfo:
        read_sheet(path)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "缺少既有列：accession_no" in excinfo.value.message


def test_missing_file_is_reported(tmp_path: Path) -> None:
    """文件不存在 —— 指名报错，不返回空表。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        read_sheet(tmp_path / "没有这张表.csv")

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "没有这张表.csv" in excinfo.value.message


def test_unsupported_format_is_reported(tmp_path: Path) -> None:
    """不支持的格式 —— 报错说清支持什么。"""

    target = tmp_path / "表.pdf"
    target.write_bytes(b"%PDF-1.4")

    with pytest.raises(NoduleEvalError) as excinfo:
        read_sheet(target)

    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert ".pdf" in excinfo.value.message


# --------------------------------------------------------------------------
# 边界三：超长文本 —— 截断并标记
# --------------------------------------------------------------------------


def test_overlong_gold_standard_is_truncated_and_marked(tmp_path: Path) -> None:
    """超长原文 —— 截断**并标记**，原文长度留痕，不静默丢弃。"""

    long_text = "右下肺见磨玻璃结节。" * 600
    path = _write_csv(tmp_path, "表.csv", HEADER + "\n" + f"ACC-001,{long_text},\n")

    result = read_sheet(path)

    case = result.cases[0]
    assert case.gold_standard_text != long_text
    assert TRUNCATION_MARKER in case.gold_standard_text
    assert len(case.gold_standard_text) > MAX_GOLD_TEXT_CHARS
    row = result.rows[0]
    assert row.truncated is True
    assert row.original_length == len(long_text)
    assert "truncated" in row.flags
    assert result.counts()["truncated_rows"] == 1


def test_truncation_prefers_a_sentence_boundary(tmp_path: Path) -> None:
    """截断尽量切在句子上 —— 从中间切可能把否定词切坏。"""

    body = "右下肺见磨玻璃结节。" * 400
    tail = "双肺未见异常。"
    long_text = body + tail
    path = _write_csv(tmp_path, "表.csv", HEADER + "\n" + f"ACC-001,{long_text},\n")

    result = read_sheet(path)

    text = result.cases[0].gold_standard_text
    assert text.endswith(TRUNCATION_MARKER)
    kept = text[: -len(TRUNCATION_MARKER)]
    assert kept.endswith("。")


# --------------------------------------------------------------------------
# 边界四：真实数据形态
# --------------------------------------------------------------------------


def test_fullwidth_digits_and_punctuation_are_normalized_but_original_kept(tmp_path: Path) -> None:
    """全角数字与中文标点 —— 归一出一份分析用副本，**原文逐字不动**。"""

    original = "右下肺见结节，约８mm。"
    path = _write_csv(tmp_path, "表.csv", HEADER + "\n" + f"ＡＣＣ－００１,{original},\n")

    result = read_sheet(path)

    case = result.cases[0]
    # 检查号归一（全角 → 半角），否则对不上 RIS。
    assert case.accession_no == "ACC-001"
    # 原文逐字保留，含全角数字与中文逗号。
    assert case.gold_standard_text == original
    # 分析用副本另存一份。
    assert case.gold_standard_normalized == "右下肺见结节,约8mm."
    assert "normalized" in result.rows[0].flags


def test_identifier_normalization_strips_prefix_and_spaces() -> None:
    """标识符归一：去空白、去前缀符号；**大小写不动**。"""

    assert normalize_identifier("  #ACC-001 ") == "ACC-001"
    assert normalize_identifier("ACC  001") == "ACC 001"
    assert normalize_identifier("acc-001") == "acc-001"


def test_gold_standard_normalization_keeps_line_breaks() -> None:
    """归一不能动换行 —— 换行是分句边界。"""

    text = "双肺未见异常。\n右下肺见结节，约８mm。"

    normalized = normalize_gold_standard_text(text)

    assert "\n" in normalized
    assert normalized.split("\n")[1] == "右下肺见结节,约8mm."


def test_gbk_and_utf8_mixed_csv_is_decoded(tmp_path: Path) -> None:
    """UTF-8 与 GBK 混编 —— 逐行回退，两行都要读对，**混编要被报出来**。"""

    utf8_line = "ACC-001,右下肺见磨玻璃结节。,study://uid-1".encode("utf-8")
    gbk_line = "ACC-002,双肺未见明显结节影。,study://uid-2".encode("gb18030")
    target = tmp_path / "混编.csv"
    target.write_bytes(HEADER.encode("utf-8") + b"\n" + utf8_line + b"\n" + gbk_line + b"\n")

    result = read_sheet(target)

    assert [case.accession_no for case in result.cases] == ["ACC-001", "ACC-002"]
    assert result.cases[0].gold_standard_text == "右下肺见磨玻璃结节。"
    assert result.cases[1].gold_standard_text == "双肺未见明显结节影。"
    assert result.mixed_encoding is True
    assert len(result.encodings) >= 2


def test_pure_gbk_csv_is_decoded_without_false_mixed_flag(tmp_path: Path) -> None:
    """整份 GBK —— 解得对，且**不**误报成混编。"""

    body = "ACC-001,右下肺见磨玻璃结节。,study://uid-1\nACC-002,双肺未见异常。,\n"
    target = tmp_path / "gbk.csv"
    target.write_bytes(HEADER.encode("gb18030") + b"\n" + body.encode("gb18030"))

    result = read_sheet(target)

    assert [case.accession_no for case in result.cases] == ["ACC-001", "ACC-002"]
    assert result.cases[0].gold_standard_text == "右下肺见磨玻璃结节。"
    assert result.mixed_encoding is False


def test_decode_bytes_reports_mixed_encodings() -> None:
    """解码结果本身要说清用了哪些编码。"""

    raw = "ACC-001,右下肺。\n".encode("utf-8") + "ACC-002,双肺未见。\n".encode("gb18030")

    decoded = decode_bytes(raw)

    assert decoded.mixed is True
    assert "utf-8" in decoded.encodings
    assert "gb18030" in decoded.encodings


# --------------------------------------------------------------------------
# 跨行叙述拼接
# --------------------------------------------------------------------------


def test_cross_row_continuation_is_joined_with_a_hard_boundary(tmp_path: Path) -> None:
    """同一个检查号写在相邻两行 —— 并成一例，段与段之间用换行隔开。

    「未见异常」在第一段，不能因为拼接就把第二段的「见结节」一起否定掉 ——
    所以分隔符必须是硬边界（换行），不是空格。
    """

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,双肺未见异常。,\n"
        "ACC-001,但右下肺见磨玻璃结节，约 8mm。,\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 1
    case = result.cases[0]
    assert case.gold_standard_text == "双肺未见异常。\n但右下肺见磨玻璃结节，约 8mm。"
    assert case.row_no == 2
    assert result.rows[0].source_rows == (2, 3)
    assert "joined" in result.rows[0].flags
    assert len(result.joins) == 1
    assert result.joins[0].target_row_no == 2
    assert result.joins[0].source_row_no == 3


def test_negation_stays_inside_its_own_fragment_after_joining(tmp_path: Path) -> None:
    """拼接后否定词仍落在自己那一段 —— 位置可定位，作用域不外溢。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,双肺未见异常。,\n"
        "ACC-001,但右下肺见磨玻璃结节。,\n",
    )

    result = read_sheet(path)

    text = result.cases[0].gold_standard_text
    spans = fragment_spans(text)
    assert len(spans) == 2
    negation_position = text.index("未见")
    assert spans[0][0] <= negation_position < spans[0][1]
    # 第二段里没有否定词 —— 第一段的「未见」不覆盖到第二段。
    assert "未见" not in text[spans[1][0]: spans[1][1]]


def test_non_adjacent_duplicate_is_reported_not_merged(tmp_path: Path) -> None:
    """同一个检查号**隔行**重复 —— 不并（并了会把两处叙述混成一段），
    但要**说出来**：行号报出来，留给绑定层按「检查号重复」挂起。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,双肺未见异常。,\n"
        "ACC-002,右下肺见结节。,\n"
        "ACC-001,但右下肺见磨玻璃结节。,\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 3
    assert result.joins == ()
    assert any("重复出现" in warning and "ACC-001" in warning for warning in result.warnings)


def test_continuation_is_never_silent(tmp_path: Path) -> None:
    """拼接要**说出来** —— 提示里有行号。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n"
        "ACC-001,双肺未见异常。,\n"
        "ACC-001,但右下肺见结节。,\n",
    )

    result = read_sheet(path)

    assert any("第 3 行" in warning and "并作一例" in warning for warning in result.warnings)


# --------------------------------------------------------------------------
# 表头容错
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "accession_no,gold_standard_text,image_ref",
        "ACCESSION_NO,GOLD_STANDARD_TEXT,IMAGE_REF",
        "Accession No,Gold Standard Text,Image Ref",
        "accession-no,gold-standard-text,image-ref",
        "ａｃｃｅｓｓｉｏｎ＿ｎｏ,ｇｏｌｄ＿ｓｔａｎｄａｒｄ＿ｔｅｘｔ,ｉｍａｇｅ＿ｒｅｆ",
        "检查号,金标准原文,影像引用",
    ],
)
def test_header_case_and_fullwidth_tolerance(tmp_path: Path, header: str) -> None:
    """表头大小写、全半角、连字符、中文别名 —— 都要认。"""

    path = _write_csv(tmp_path, "表.csv", header + "\n" + "ACC-001,右下肺见结节。,\n")

    result = read_sheet(path)

    assert len(result.cases) == 1
    assert result.cases[0].accession_no == "ACC-001"
    assert result.cases[0].gold_standard_text == "右下肺见结节。"


def test_contract_column_name_wins_over_chinese_alias(tmp_path: Path) -> None:
    """一张表里两套写法都有 —— 按契约列名定位。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        "检查号,accession_no,gold_standard_text\n"
        "ACC-OLD,ACC-001,右下肺见结节。\n",
    )

    result = read_sheet(path)

    assert result.cases[0].accession_no == "ACC-001"


def test_unknown_columns_are_ignored(tmp_path: Path) -> None:
    """多出来的列不报错 —— 科室表里常有别的列。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + ",备注\n" + "ACC-001,右下肺见结节。,study://uid-1,随手写的\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 1
    assert result.cases[0].accession_no == "ACC-001"


# --------------------------------------------------------------------------
# 与契约、与既有列的一致性
# --------------------------------------------------------------------------


def test_read_cases_can_be_fed_into_the_request_contract(tmp_path: Path) -> None:
    """读出来的病例能直接进请求契约 —— 这一层是主路径的一段，不是旁路。"""

    path = _write_csv(
        tmp_path,
        "表.csv",
        HEADER + "\n" + "ACC-001,右下肺见结节。,study://uid-1\n",
    )

    cases = read_sheet(path).cases
    payload = {
        "run_id": "run-1",
        "model_id": "demo-model",
        "model_version": "1.0.0",
        "dataset_ref": "本院-2026-11",
        "cases": [case.to_dict() for case in cases],
    }

    request = parse_request(payload)

    assert request.cases[0].accession_no == "ACC-001"
    assert request.cases[0].gold_standard_text == "右下肺见结节。"
    assert request.cases[0].gold_standard_normalized == "右下肺见结节."


def test_gold_standard_original_is_byte_identical_to_the_cell(tmp_path: Path) -> None:
    """原文逐字保留 —— 含换行与全角标点，一个字都不动。"""

    original = "双肺未见异常。\n右下肺见结节，约８mm。"
    # 单元格里的换行在 CSV 里要加引号，否则会断成两行。
    path = _write_csv(tmp_path, "表.csv", HEADER + "\n" + f'ACC-001,"{original}",\n')

    result = read_sheet(path)

    assert result.cases[0].gold_standard_text == original
    assert result.cases[0].gold_standard_normalized != original
    assert "\n" in result.cases[0].gold_standard_normalized


def test_numeric_accession_no_becomes_a_string(tmp_path: Path) -> None:
    """Excel 把检查号存成数字 —— 要还原成字符串，不能带 ``.0``。"""

    from openpyxl import load_workbook

    target = write_template(tmp_path / "评估表.xlsm", prefilled_rows=2)
    workbook = load_workbook(target)
    sheet = workbook[SHEET_DETAIL]
    sheet.cell(row=FIRST_DATA_ROW, column=1, value=20240115001)
    sheet.cell(row=FIRST_DATA_ROW, column=2, value="右下肺见结节。")
    workbook.save(target)

    result = read_sheet(target)

    assert result.cases[0].accession_no == "20240115001"


def test_existing_columns_are_never_read_back_as_input(tmp_path: Path) -> None:
    """表里留着上一轮回填的新增列 —— 读取层**不看**它们。

    上一轮跑过的表，判定列里还留着旧的「命中 / 漏判」。读取层要是把它
    当输入，结论就会被上一轮的结果带着走 —— 这一层只认既有三列。
    """

    path = _write_csv(
        tmp_path,
        "表.csv",
        "accession_no,gold_standard_text,image_ref,verdict,tool_call_id\n"
        "ACC-001,右下肺见结节。,study://uid-1,hit,tc-old-0001\n",
    )

    result = read_sheet(path)

    assert len(result.cases) == 1
    case = result.cases[0]
    assert case.accession_no == "ACC-001"
    assert case.gold_standard_text == "右下肺见结节。"
    assert not hasattr(case, "verdict")
    assert not hasattr(case, "tool_call_id")
    # 新增列不属于读取层的输入契约。
    assert set(ALL_COLUMNS) - {"accession_no", "gold_standard_text", "image_ref"}
