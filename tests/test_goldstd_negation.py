"""否定作用域、多病灶口径与跨行拼接的断言（任务 20）。

**这一层决定敏感度与特异度对不对**：

- **否定误召**（阴性被判成阳性）拉低**特异度**；
- **否定漏召**（阳性被判成阴性）拉低**敏感度**；
- 属性串味（部位 / 密度取错）会让一次真命中被算成漏诊 —— 与漏召同罪。

所以本文件把三类东西钉死：

1. **回归集逐条命中**（``regression.yaml`` 里的真实写法，期望标签全写死）；
2. **否定误召 / 漏召两侧都被纠正**（拿「朴素口径」对照：先出现标记说了算、
   整段扫一遍取第一个部位 —— 这两种朴素做法都会错）；
3. **跨行拼接与多病灶**的边界（上一行的否定不外溢；一处病灶一处标签）。
"""

from __future__ import annotations

import pytest

from nodule_eval.goldstd import normalize_gold_standard
from nodule_eval.goldstd import regression as REG
from nodule_eval.goldstd import rules as R
from nodule_eval.goldstd.schema import GoldLabels


# ---------------------------------------------------------------- 回归集本身


def test_regression_set_has_at_least_twelve_cases() -> None:
    """回归集**不少于 12 条** —— 少于这个数，一处改动就盖不住。"""

    cases = REG.regression_cases()
    assert len(cases) >= 12, f"回归集只有 {len(cases)} 条"


def test_regression_set_covers_both_sides_of_the_error() -> None:
    """误召与漏召两侧都要有 —— 只测一侧等于没测。"""

    categories = REG.load_regression_set().by_category()
    assert "否定误召" in categories
    assert "否定漏召" in categories
    assert len(categories["否定误召"]) >= 3
    assert len(categories["否定漏召"]) >= 3


def test_regression_set_loads_without_error() -> None:
    """数据文件本身能载入（字段缺失即报错，不静默取默认值）。"""

    payload = REG.load_regression_set().to_dict()
    assert payload["version"] == 1
    assert len(payload["cases"]) >= 12


def test_missing_regression_file_is_reported() -> None:
    """文件不存在 → 报错，**不回落到内置兜底集**（兜底集会让回归看着是绿的）。"""

    from nodule_eval.errors import ErrorCode, NoduleEvalError

    with pytest.raises(NoduleEvalError) as excinfo:
        REG.load_regression_set("/tmp/不存在的回归集.yaml")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID


# ------------------------------------------------------------ 回归集逐条命中


@pytest.mark.parametrize(
    "case",
    REG.regression_cases(),
    ids=[case.id for case in REG.regression_cases()],
)
def test_regression_case_hits_expected_labels(case: REG.RegressionCase) -> None:
    """回归集**逐条**命中期望标签。"""

    merge = R.merge_by_rules(case.text)
    labels = merge.labels
    expect = case.expect

    assert labels.presence == expect["presence"], case.note
    assert labels.location == expect["location"], case.note
    assert labels.size_mm == expect["size_mm"], case.note
    assert labels.density == expect["density"], case.note
    assert len(labels.lesions) == expect["lesions"], case.note
    assert labels.needs_human_review == expect["needs_human_review"], case.note

    if case.lesion_expect:
        for index, wanted in enumerate(case.lesion_expect):
            lesion = labels.lesions[index]
            assert lesion.location == wanted["location"], case.note
            assert lesion.size_mm == wanted["size_mm"], case.note
            assert lesion.density == wanted["density"], case.note

    if case.unknown_density_terms:
        for term in case.unknown_density_terms:
            assert term in merge.diagnostics.unknown_density_terms, case.note


# -------------------------------------------------------- 否定误召（阴性判阳性）


@pytest.mark.parametrize(
    "text",
    [
        "双肺无钙化灶。",
        "双肺未见明显结节及钙化灶。",
        "双肺纹理清晰，未见明显异常。",
    ],
)
def test_negative_reports_are_not_read_as_positive(text: str) -> None:
    """阴性报告**不许**读成阳性 —— 一次误召就拉低特异度。"""

    assert R.normalize_by_rules(text).presence is False


def test_negated_attribute_does_not_leak_into_the_label_set() -> None:
    """被否掉的属性不进标签 ——「未见…钙化灶」的密度不是钙化。

    朴素做法（整段扫一遍取第一个命中）会把密度算成「钙化」，
    比对时一次真命中会被算成密度不一致 —— 与漏诊同罪。
    """

    labels = R.normalize_by_rules("双肺未见明显结节及钙化灶。")
    assert labels.presence is False
    assert labels.density is None
    assert labels.lesions == ()


def test_location_comes_from_the_asserted_clause_not_an_earlier_one() -> None:
    """部位取**断言句**的部位，不取前一句的。

    「双肺未见异常，但右下肺见磨玻璃结节」—— 部位是右下肺，不是双肺。
    取错了，比对时一次真命中会被算成部位不一致 = 漏诊。
    """

    labels = R.normalize_by_rules("双肺未见异常，但右下肺见磨玻璃结节。")
    assert labels.presence is True
    assert labels.location == "右肺下叶"
    assert labels.density == "磨玻璃"


def test_naive_whole_text_scan_would_have_been_wrong() -> None:
    """把「朴素口径」摆出来对照 —— 它确实会错，这层才有意义。

    朴素口径 = 整段扫一遍取第一个部位命中 + 先出现标记说了算。
    两处都会让「双肺未见异常，但右下肺见磨玻璃结节」判错。
    """

    text = "双肺未见异常，但右下肺见磨玻璃结节。"
    naive_location = R.find_location(text)
    assert naive_location is not None
    assert naive_location.canonical == "双肺", "朴素口径取到的是前一句的部位"
    # 本层取的是断言句的部位：
    assert R.normalize_by_rules(text).location == "右肺下叶"


# -------------------------------------------------------- 否定漏召（阳性判阴性）


@pytest.mark.parametrize(
    "text",
    [
        "未见异常，可见磨玻璃影。",
        "双肺未见明显结节，可见磨玻璃结节影。",
        "除右下肺小结节外未见异常。",
    ],
)
def test_positive_reports_are_not_read_as_negative(text: str) -> None:
    """阳性报告**不许**读成阴性 —— 一次漏召就拉低敏感度。"""

    assert R.normalize_by_rules(text).presence is True


def test_negation_does_not_reach_across_a_contrast_marker() -> None:
    """否定只管所在分句 —— 转折词后面是另一句结论。"""

    labels = R.normalize_by_rules("双肺未见异常，但右下肺见磨玻璃结节。")
    merge = R.merge_by_rules("双肺未见异常，但右下肺见磨玻璃结节。")
    # 逐句结论：第一句否定、第二句肯定 —— 整体取并存事实的阳性一侧
    assert merge.diagnostics.clause_presence == (False, None, True)
    assert labels.presence is True


def test_negation_governs_only_the_following_finding() -> None:
    """否定词管的是**紧跟着**的那处病灶，后面的病灶词不受它管。"""

    merge = R.merge_by_rules("双肺未见异常可见磨玻璃影。")
    assert merge.labels.presence is True
    assert merge.labels.density == "磨玻璃"


def test_negation_with_modifier_between_still_governs() -> None:
    """否定与病灶词之间夹着修饰字（「未发现**明显**结节」）—— 否定仍然管得到。"""

    assert R.normalize_by_rules("双肺未发现明显结节影。").presence is False


def test_coordinated_descriptions_share_the_negation() -> None:
    """并列描述同一处的病灶词一并被否定（「未见明显结节**及**钙化灶」）。"""

    merge = R.merge_by_rules("双肺未见明显结节及钙化灶。")
    assert merge.labels.presence is False
    assert merge.labels.lesions == ()


# -------------------------------------------------------------- 多病灶口径


def test_each_lesion_gets_its_own_label_set() -> None:
    """一句里两处病灶 → 两组标签，逐处对得上。"""

    labels = R.normalize_by_rules("右下肺见一 8mm 磨玻璃结节，左上肺见一 5mm 实性结节。")
    assert labels.presence is True
    assert len(labels.lesions) == 2

    first, second = labels.lesions
    assert (first.location, first.size_mm, first.density) == ("右肺下叶", 8.0, "磨玻璃")
    assert (second.location, second.size_mm, second.density) == ("左肺上叶", 5.0, "实性")


def test_lesion_count_matches_the_number_of_asserted_findings() -> None:
    """病灶处数 = **未被否定**的病灶词数（被否掉的不算病灶）。"""

    assert len(R.normalize_by_rules("双肺见多发结节及钙化灶。").lesions) == 2
    assert len(R.normalize_by_rules("双肺未见明显结节及钙化灶。").lesions) == 0


def test_later_lesion_attribute_does_not_flow_backwards() -> None:
    """后面病灶的密度不倒着认领 ——「钙化」是第二处的。"""

    lesions = R.normalize_by_rules("双肺见多发结节及钙化灶。").lesions
    assert lesions[0].density is None
    assert lesions[1].density == "钙化"


def test_top_level_attributes_are_the_first_lesion_projection() -> None:
    """顶层属性是**首个病灶**的投影（只有一处时两者一致）。"""

    labels = R.normalize_by_rules("右下肺见一 8mm 磨玻璃结节，左上肺见一 5mm 实性结节。")
    assert labels.location == labels.lesions[0].location
    assert labels.size_mm == labels.lesions[0].size_mm
    assert labels.density == labels.lesions[0].density


# ------------------------------------------------------------ 跨行叙述拼接


def test_negation_on_an_earlier_line_does_not_reach_the_next_line() -> None:
    """跨行拼接后仍按分句判 —— 上一行的「未见」不外溢到下一行。"""

    labels = R.normalize_by_rules("双肺未见明显异常。\n右下肺见磨玻璃结节影。")
    assert labels.presence is True
    assert labels.location == "右肺下叶"
    assert labels.density == "磨玻璃"


def test_cross_line_join_keeps_the_size_of_the_same_sentence() -> None:
    """同一句话折行（逗号收尾）—— 尺寸不丢。"""

    labels = R.normalize_by_rules("右下肺见一磨玻璃结节，\n大小约 8mm。")
    assert labels.presence is True
    assert labels.size_mm == 8.0
    assert labels.location == "右肺下叶"


def test_cross_line_join_keeps_both_lines_negative() -> None:
    """两行都是否定 → 整体阴性。"""

    labels = R.normalize_by_rules("双肺未见明显异常。\n未见明显结节影。")
    assert labels.presence is False
    assert labels.lesions == ()


def test_diagnostics_expose_the_line_fragments() -> None:
    """台账给出**按行切开的片段** —— 否定词落在哪一行，一眼可见。"""

    merge = R.merge_by_rules("双肺未见明显异常。\n右下肺见磨玻璃结节影。")
    assert merge.diagnostics.fragments == (
        "双肺未见明显异常。",
        "右下肺见磨玻璃结节影。",
    )
    # 否定词落在第 0 段，部位与密度落在第 1 段
    negation = merge.diagnostics.negations[0]
    assert merge.diagnostics.fragments[0][negation.start : negation.end] == negation.marker


# ------------------------------------------------------------------ 契约不变


def test_original_text_is_never_rewritten_by_the_scope_layer() -> None:
    """作用域判定只读不写 —— 原文一个字都不动。"""

    text = "双肺未见明显异常。\n右下肺见一磨玻璃结节，大小约 8mm。"
    before = text
    R.merge_by_rules(text)
    assert text == before


def test_contract_shape_is_unchanged_by_the_scope_layer() -> None:
    """作用域与多病灶落定后，输出契约仍是**同一份** dataclass。"""

    labels = R.normalize_by_rules("右下肺见 8mm 磨玻璃结节")
    assert isinstance(labels, GoldLabels)
    assert GoldLabels.__module__ == "nodule_eval.goldstd.schema"
    assert set(labels.to_dict()) == {
        "presence",
        "location",
        "size_mm",
        "density",
        "lesions",
        "evidence_span",
        "confidence",
        "needs_human_review",
        "source",
        "degraded",
        "degrade_reason",
        "degrade_error_code",
    }


def test_facade_entry_and_rule_merge_agree() -> None:
    """包级入口与规则基线是同一个结论（引擎走的是包级入口）。"""

    text = "双肺未见异常，但右下肺见一 8mm 磨玻璃结节。"
    assert normalize_gold_standard(text).to_dict() == R.normalize_by_rules(text).to_dict()
