"""规则基线归并的断言（任务 19）。

**这一层是整条主干的确定性地基**：模型不可达时接管归并（任务 24），
又是任务 25「规则基线 vs 语义归并」对照实验的对照组。所以这里测三件事：

1. **规则读得准** —— 量纲换算、部位密度归一、否定命中；
2. **规则读不出来时说实话** —— 属性留空 + 原词记进台账，**不猜**；
3. **规则与语义归并同构** —— 输出契约逐字段对得上，两边才能直接比对。
"""

from __future__ import annotations

import pytest

from nodule_eval.contracts import NORMALIZE_SOURCES
from nodule_eval.goldstd import normalize_gold_standard
from nodule_eval.goldstd import rules as R
from nodule_eval.goldstd.schema import (
    NORMALIZE_SOURCE_MODEL,
    NORMALIZE_SOURCE_RULE,
    GoldLabels,
)


# ---------------------------------------------------------------- 量纲换算

#: 写法 → 毫米。
SIZE_CASES: list[tuple[str, float]] = [
    ("8mm", 8.0),
    ("8 mm", 8.0),
    ("8MM", 8.0),
    ("8毫米", 8.0),
    ("8 毫米", 8.0),
    ("0.8cm", 8.0),
    ("0.8 cm", 8.0),
    ("0.8CM", 8.0),
    ("0.8厘米", 8.0),
    ("0.8公分", 8.0),
    ("1.2cm", 12.0),
    ("18mm", 18.0),
    ("最大径约8mm", 8.0),
    ("结节直径约 8mm", 8.0),
]


@pytest.mark.parametrize("text,expected_mm", SIZE_CASES)
def test_size_is_converted_to_millimetres(text: str, expected_mm: float) -> None:
    """尺寸统一换算成毫米（cm 乘 10）。"""

    match = R.parse_size(text)
    assert match is not None, text
    assert match.value_mm == expected_mm, text


def test_size_keeps_one_decimal() -> None:
    """小数尺寸保留一位（四舍五入，不做银行家舍入）。"""

    assert R.parse_size("8.33mm").value_mm == 8.3
    assert R.parse_size("8.55mm").value_mm == 8.6
    assert R.parse_size("0.85cm").value_mm == 8.5
    assert R.parse_size("3.05mm").value_mm == 3.1


def test_size_of_second_dimension_is_not_taken() -> None:
    """「5×3mm」里的 3 是短径 —— 认成 3mm 比认不出来更糟。

    拿 3mm 去比模型的 5mm，一次命中会被算成漏诊。所以宁可不认。
    """

    assert R.parse_size("5×3mm") is None
    # 先写最大径时取最大径（8mm），后一个 6mm 是横径，不取。
    assert R.parse_size("8mm×6mm").value_mm == 8.0


def test_no_size_leaves_none() -> None:
    """没有尺寸就留空 —— 不填 0（0mm 会被判成「有个 0 毫米的结节」）。"""

    assert R.parse_size("双肺未见明显异常") is None
    assert R.normalize_by_rules("双肺未见明显异常").size_mm is None


def test_unknown_length_unit_is_recorded_not_guessed() -> None:
    """换算不了的长度单位：尺寸留空 + 原词记进台账。"""

    merge = R.merge_by_rules("右下肺见结节，结节直径约 0.5 吋")
    assert merge.labels.size_mm is None
    assert any("吋" in term for term in merge.diagnostics.unknown_size_terms)


# ---------------------------------------------------------------- 部位与密度


def test_location_and_density_fold_to_canonical_labels() -> None:
    """部位与密度走等价表，落到标准标签。"""

    labels = R.normalize_by_rules("右下肺见磨玻璃结节")
    assert labels.location == "右肺下叶"
    assert labels.density == "磨玻璃"

    labels = R.normalize_by_rules("右上肺野见 8mm 实性结节")
    assert labels.location == "右肺上叶"
    assert labels.density == "实性"
    assert labels.size_mm == 8.0


def test_density_aliases_fold_to_one_label() -> None:
    """英文缩写与中文写法归到同一标签。"""

    assert R.normalize_by_rules("GGO 病灶").density == "磨玻璃"
    assert R.normalize_by_rules("GGN 病灶").density == "磨玻璃"
    assert R.normalize_by_rules("部分实性结节").density == "部分实性"


def test_lung_segment_folds_to_its_lobe() -> None:
    """肺段归入所属肺叶（段号由等价表另存）。"""

    assert R.normalize_by_rules("右下肺S6见磨玻璃结节").location == "右肺下叶"


def test_unknown_attribute_terms_are_recorded_not_guessed() -> None:
    """认不出来的原词记进台账 —— 属性留空是「没读出来」，不是「没问题」。"""

    merge = R.merge_by_rules("右下肺见结节，肺门区见淋巴结肿大")
    assert merge.labels.location == "右肺下叶"
    assert "肺门区" in merge.diagnostics.unknown_location_terms

    merge = R.merge_by_rules("右下肺见密度不均的结节")
    assert merge.labels.density is None
    assert "密度不均" in merge.diagnostics.unknown_density_terms


def test_recognised_terms_are_not_reported_as_unknown() -> None:
    """认得出的部位 / 密度不进「未知」台账（否则台账全是噪声）。"""

    merge = R.merge_by_rules("右下肺见磨玻璃结节")
    assert merge.diagnostics.unknown_location_terms == ()
    assert merge.diagnostics.unknown_density_terms == ()


# ---------------------------------------------------------------- 否定词表


@pytest.mark.parametrize(
    "text",
    [
        "未见明显结节",
        "双肺未见异常",
        "未发现结节影",
        "无明显结节",
        "未见异常",
    ],
)
def test_negation_markers_fold_to_absence(text: str) -> None:
    """否定词命中即「没有」。"""

    assert R.normalize_by_rules(text).presence is False


def test_except_clause_keeps_presence() -> None:
    """「除外右下肺小结节」= 除了它以外没有 → **仍然有病灶**。

    「除外」既是否定标记又是转折词：单独成句时它那句说「没有」，
    后半句说「有」，并存事实取阳性那一侧 —— 这与任务 14 的存在性层同口径。
    否定词的**作用域**细化留给任务 20。
    """

    labels = R.normalize_by_rules("除外右下肺小结节")
    assert labels.presence is True


def test_affirmative_marker_folds_to_presence() -> None:
    """肯定词命中即「有」。"""

    assert R.normalize_by_rules("右下肺见磨玻璃结节").presence is True


def test_negation_positions_are_recorded_for_scope_judging() -> None:
    """否定词的**位置**要记下来 —— 任务 20 的作用域判定靠它。"""

    hits = R.find_negations("双肺未见异常，但右下肺见结节")
    assert hits, "否定词必须有命中"
    first = hits[0]
    assert "未见" in first.marker
    assert "双肺未见异常"[: first.end].endswith(first.marker)
    assert first.clause_index >= 0, "命中要落在某一句里（任务 20 按句判作用域）"


def test_negation_scope_is_not_yet_resolved() -> None:
    """并存事实取阳性那一侧（否定作用域的细化留给任务 20）。"""

    labels = R.normalize_by_rules("双肺未见异常，但右下肺见结节")
    assert labels.presence is True


def test_unreadable_text_reports_none_not_negative() -> None:
    """既无肯定也无否定 → 读不出来（``None``），**不落成阴性**。"""

    labels = R.normalize_by_rules("胸片已摄，图像清晰")
    assert labels.presence is None
    assert labels.needs_human_review is True


# ---------------------------------------------------------------- 输出契约


def test_rule_output_matches_semantic_merge_contract() -> None:
    """规则归并与语义归并**同一份契约** —— 两边才能逐字段直接比。"""

    expected_keys = {
        "presence",
        "location",
        "size_mm",
        "density",
        "lesions",
        "evidence_span",
        "confidence",
        "needs_human_review",
        "source",
        # 降级三件套（任务 24）：规则基线与语义归并**共用**同一份契约 ——
        # 降级与否都落在这三个字段上，两边逐字段可直接比。
        "degraded",
        "degrade_reason",
        "degrade_error_code",
    }
    labels = R.normalize_by_rules("右下肺见 8mm 磨玻璃结节")
    assert isinstance(labels, GoldLabels)
    assert set(labels.to_dict()) == expected_keys
    # 语义归并（任务 21）返回的是同一个 dataclass —— 契约不会各写一份。
    assert GoldLabels.__module__ == "nodule_eval.goldstd.schema"


def test_contract_carries_the_multi_lesion_label_set() -> None:
    """契约里带**多病灶标签集合**（任务 20）—— 判定层按最严口径逐处比对。

    顶层三个属性是它的投影：只有一处病灶时两者一致。
    """

    labels = R.normalize_by_rules("右下肺见 8mm 磨玻璃结节")
    assert len(labels.lesions) == 1
    lesion = labels.lesions[0]
    assert lesion.location == labels.location == "右肺下叶"
    assert lesion.size_mm == labels.size_mm == 8.0
    assert lesion.density == labels.density == "磨玻璃"
    assert lesion.finding == "磨玻璃结节"
    assert lesion.evidence_span in "右下肺见 8mm 磨玻璃结节"


def test_rule_source_is_marked_as_rule() -> None:
    """归并来源写 ``rule`` —— 评估表 normalize_source 列据此统计降级例数。"""

    labels = R.normalize_by_rules("右下肺见结节")
    assert labels.source == NORMALIZE_SOURCE_RULE
    assert labels.source in NORMALIZE_SOURCES
    assert NORMALIZE_SOURCE_MODEL in NORMALIZE_SOURCES


def test_facade_entry_matches_rule_merge() -> None:
    """包级入口与规则基线是同一个结论（引擎走的是包级入口）。"""

    text = "右下肺见 8mm 磨玻璃结节"
    assert normalize_gold_standard(text).to_dict() == R.normalize_by_rules(text).to_dict()


def test_evidence_span_points_back_to_original_text() -> None:
    """证据片段是**原文里的一段**（复核的人靠它核对）。"""

    text = "双肺未见异常。右下肺见磨玻璃结节，较大者约 8mm。"
    labels = R.normalize_by_rules(text)
    assert labels.evidence_span
    assert labels.evidence_span in text


def test_original_text_is_never_rewritten() -> None:
    """原文零改写：归并只读不写。"""

    text = "双肺未见异常。右下肺见磨玻璃结节，较大者约 8mm。"
    before = text
    R.merge_by_rules(text)
    assert text == before


def test_normalized_copy_is_used_for_matching_when_given() -> None:
    """给了分析用副本就按副本匹配（原文不动）。"""

    labels = R.normalize_by_rules(
        "双肺未见异常。",
        normalized_text="双肺未见异常。",
    )
    assert labels.presence is False


def test_diagnostics_expose_clause_split_and_hits() -> None:
    """台账给出分句与标记命中位置（任务 20 的作用域判定在它上面做）。"""

    merge = R.merge_by_rules("双肺未见异常，但右下肺见结节")
    assert len(merge.diagnostics.clauses) >= 2
    assert merge.diagnostics.negations
    assert merge.diagnostics.affirmatives
    assert merge.to_dict()["labels"]["presence"] is True


def test_convert_to_mm_refuses_unknown_unit() -> None:
    """换算不了的长度单位不猜。"""

    assert R.convert_to_mm("8", "寸") is None
    assert R.convert_to_mm("abc", "mm") is None
    assert R.convert_to_mm("0.8", "cm") == 8.0
