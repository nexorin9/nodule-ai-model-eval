"""部位与密度等价表的断言（任务 18）。

**这张表是金标准侧与模型侧共用的唯一真源** —— 所以这里既测「写法归一」，
也测「认不出来就认不出来」与「加别名不用改代码」。
"""

from __future__ import annotations

import pytest

from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd import labels as L


# ---------------------------------------------------------------- 部位等价

#: 同一处部位的不同写法 → 同一个标准标签。
LOCATION_SYNONYM_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("右肺上叶", ("右肺上叶", "右上叶", "右上肺", "右上肺野", "右上", "右肺上", "右肺尖", "RUL")),
    ("右肺中叶", ("右肺中叶", "右中叶", "右中肺", "右中肺野", "右中", "右肺中", "RML")),
    ("右肺下叶", ("右肺下叶", "右下叶", "右下肺", "右下肺野", "右下", "右肺下", "RLL")),
    ("左肺上叶", ("左肺上叶", "左上叶", "左上肺", "左上肺野", "左上", "左肺上", "左肺尖", "LUL")),
    ("左肺下叶", ("左肺下叶", "左下叶", "左下肺", "左下肺野", "左下", "左肺下", "LLL")),
    ("右肺", ("右肺", "右肺野", "右侧肺野")),
    ("左肺", ("左肺", "左肺野", "左侧肺野")),
    ("双肺", ("双肺", "双肺野", "双侧肺野", "双侧肺", "两侧肺野", "两侧肺", "两肺")),
]


@pytest.mark.parametrize("canonical,writings", LOCATION_SYNONYM_GROUPS)
def test_location_synonyms_fold_to_one_label(canonical: str, writings: tuple[str, ...]) -> None:
    """同义写法归一到同一个标准标签。"""

    for writing in writings:
        assert L.normalize_location(writing) == canonical, writing


def test_location_tolerates_spacing_and_punctuation() -> None:
    """空格、句号、全角字母不影响归一（写法不会一字不差）。"""

    assert L.normalize_location("右肺 上叶") == "右肺上叶"
    assert L.normalize_location("右肺上叶。") == "右肺上叶"
    assert L.normalize_location("ＲＵＬ") == "右肺上叶"
    assert L.normalize_location("  右上肺野  ") == "右肺上叶"


def test_segment_folds_into_owning_lobe() -> None:
    """肺段归入所属肺叶（判定按肺叶比，段号另存供核对）。"""

    # 右肺下叶背段（S6）的几种写法
    for writing in ("右下肺S6", "右下叶背段", "右下肺背段", "右肺下叶背段"):
        assert L.normalize_location(writing) == "右肺下叶", writing

    # 左肺同一段归左肺下叶
    for writing in ("左下肺S6", "左下叶背段", "左肺下叶背段"):
        assert L.normalize_location(writing) == "左肺下叶", writing

    # 舌段：右肺归中叶，左肺归上叶 —— 两侧不混
    assert L.normalize_location("右肺舌段") == "右肺中叶"
    assert L.normalize_location("左肺舌段") == "左肺上叶"

    # 基底段都在下叶
    for writing in ("右肺下叶前基底段", "右下叶前基底段", "前基底段"):
        assert L.normalize_location(writing) in {"右肺下叶", "下叶"}, writing


def test_segment_detail_is_kept_for_audit() -> None:
    """归入肺叶的同时保留「原文写的是哪个肺段」。"""

    match = L.resolve_location("右下肺S6")
    assert match is not None
    assert match.canonical == "右肺下叶"
    assert match.segment == "下叶背段"
    assert match.segment_code == "S6"
    assert match.side == "右"
    assert match.lobe == "下叶"

    plain = L.resolve_location("右下肺")
    assert plain is not None
    assert plain.segment == ""
    assert plain.segment_code == ""


def test_side_less_writing_keeps_no_side() -> None:
    """只写肺叶不写侧别时，标准标签也**不带侧别**（不替医生补一个）。"""

    assert L.normalize_location("上叶") == "上叶"
    assert L.normalize_location("下叶") == "下叶"
    assert L.normalize_location("S6") == "下叶"
    assert L.normalize_location("S1+2") == "上叶"

    assert L.location_side("下叶") == ""
    assert L.location_lobe("下叶") == "下叶"
    assert L.location_side("右肺下叶") == "右"
    assert L.location_lobe("右肺下叶") == "下叶"
    assert L.location_side("双肺") == "双"
    assert L.location_lobe("双肺") == ""


def test_unknown_writing_returns_none() -> None:
    """认不出来就是认不出来 —— 不猜一个近似标签。"""

    assert L.normalize_location("未知部位") is None
    assert L.normalize_location("胃底部") is None
    assert L.normalize_location("") is None
    assert L.normalize_location("   ") is None
    assert L.normalize_location(None) is None
    assert L.resolve_location("未知部位") is None


def test_ambiguous_segment_code_returns_none() -> None:
    """S4 / S5 两侧归属不同（右中叶 vs 上叶），不带侧别时交人工复核。"""

    assert L.normalize_location("S4") is None
    assert L.normalize_location("S5") is None
    # 写成肺叶就没歧义了
    assert L.normalize_location("右肺中叶") == "右肺中叶"
    assert L.normalize_location("左肺上叶") == "左肺上叶"


def test_left_lung_has_no_middle_lobe() -> None:
    """左肺没有中叶 —— 不存在的解剖结构不替医生编一个。"""

    assert L.normalize_location("左肺中叶") is None
    assert L.normalize_location("左中叶") is None


# ---------------------------------------------------------------- 密度等价

DENSITY_SYNONYM_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("磨玻璃", ("磨玻璃", "磨玻璃影", "磨玻璃结节", "毛玻璃影", "GGN", "GGO",
                "ground-glass", "ground-glass-nodule", "ground-glass-opacity")),
    ("实性", ("实性", "实性影", "实性结节", "solid", "solid-nodule")),
    ("部分实性", ("部分实性", "部分实性结节", "部分磨玻璃", "混合密度", "半实性",
                  "part-solid", "partially-solid", "PSN", "mixed-ground-glass")),
    ("钙化", ("钙化", "钙化灶", "钙化结节", "calcified", "calcification")),
    ("空洞", ("空洞", "空洞影", "空洞灶", "cavity", "cavitary", "cavitation")),
    ("囊性", ("囊性", "囊性影", "囊性结节", "cystic", "cyst")),
]


@pytest.mark.parametrize("canonical,writings", DENSITY_SYNONYM_GROUPS)
def test_density_synonyms_fold_to_one_label(canonical: str, writings: tuple[str, ...]) -> None:
    """同义写法归一到同一个密度标签。"""

    for writing in writings:
        assert L.normalize_density(writing) == canonical, writing


def test_unknown_density_returns_none() -> None:
    assert L.normalize_density("未知密度") is None
    assert L.normalize_density("") is None
    assert L.normalize_density(None) is None
    assert L.resolve_density("未知密度") is None


def test_cavity_and_cystic_stay_separate() -> None:
    """空洞与囊性不合并 —— 合并会让最严口径的比对变松。"""

    assert L.normalize_density("空洞") != L.normalize_density("囊性")
    assert L.normalize_density("cavity") == "空洞"
    assert L.normalize_density("cystic") == "囊性"


# ------------------------------------------------------------ 多个写法与冲突


def test_multiple_writings_return_same_label() -> None:
    """给多个写法，返回同一个标准标签。"""

    assert L.normalize_locations(["右肺上叶", "RUL", "右上叶"]) == "右肺上叶"
    assert L.normalize_locations(["右下肺", "右下肺S6", "右下叶背段"]) == "右肺下叶"
    assert L.normalize_densities(["磨玻璃", "GGN", "ground-glass"]) == "磨玻璃"


def test_conflicting_or_unknown_writings_return_none() -> None:
    """写法打架或认不出来 → 不挑一个当答案。"""

    assert L.normalize_locations(["右肺上叶", "左肺上叶"]) is None
    assert L.normalize_locations(["右肺上叶", "未知部位"]) is None
    assert L.normalize_locations(["右肺上叶", ""]) is None
    assert L.normalize_locations([]) is None
    assert L.normalize_densities(["磨玻璃", "实性"]) is None


# ------------------------------------------------------------------ 文本扫描


def test_find_location_picks_leftmost_longest() -> None:
    """文本里取部位：先取最靠左的，同起点取最长的。"""

    match = L.find_location("右下肺见磨玻璃结节")
    assert match is not None
    assert match.canonical == "右肺下叶"
    assert match.matched_text == "右下肺"

    match = L.find_location("右肺下叶背段见结节")
    assert match is not None
    assert match.canonical == "右肺下叶"
    assert match.segment_code == "S6"

    match = L.find_location("双肺未见明显结节影")
    assert match is not None
    assert match.canonical == "双肺"

    assert L.find_location("心影不大，未见明显异常") is None


def test_find_density_prefers_the_longer_term() -> None:
    """「部分实性」不能被切成「实性」。"""

    match = L.find_density("右肺上叶见部分实性结节")
    assert match is not None
    assert match.canonical == "部分实性"

    match = L.find_density("右肺上叶见实性结节")
    assert match is not None
    assert match.canonical == "实性"

    assert L.find_density("双肺纹理清晰") is None


def test_find_returns_evidence_span_in_original_text() -> None:
    """命中片段指回原文（供 evidence_span 用）。"""

    text = "右下肺见磨玻璃结节，双肺其余清晰"
    match = L.find_location(text)
    assert match is not None
    assert match.canonical == "右肺下叶"
    assert match.start == 0
    assert match.matched_text == "右下肺"


def test_find_location_returns_the_first_location_mentioned() -> None:
    """同一句里出现多处部位时取**最靠左**的一处。

    同一处病灶到底归哪个部位，由分句与作用域规则决定（任务 20），
    本表只负责「把写法归一」，不替医生判断病灶归属。
    """

    match = L.find_location("双肺肺野清晰，右下肺见结节")
    assert match is not None
    assert match.canonical == "双肺"


# -------------------------------------------------------------- 表是数据驱动的


def test_new_alias_needs_no_code_change(tmp_path) -> None:
    """新增别名只改数据文件 —— 判定逻辑代码一行不动。"""

    custom = tmp_path / "labels.yaml"
    custom.write_text(
        """
locations:
  canonical:
    - 右肺上叶
  aliases:
    右肺上叶: 右肺上叶
    右侧上叶: 右肺上叶
densities:
  canonical:
    - 磨玻璃
  aliases:
    磨玻璃: 磨玻璃
""",
        encoding="utf-8",
    )

    table = L.load_label_table(custom)
    # 新写法在自定义表里认得出来
    assert L.normalize_location("右侧上叶", table=table) == "右肺上叶"
    # 默认表不认它 —— 证明两张表互不干扰
    assert L.normalize_location("右侧上叶") is None
    # 同一个归一函数吃两张表 —— 代码没分叉
    assert L.normalize_location("右肺上叶", table=table) == "右肺上叶"


def test_duplicate_alias_after_normalization_is_rejected(tmp_path) -> None:
    """两个别名归一后撞键 → 载入即报错，不静默取一个。"""

    custom = tmp_path / "labels.yaml"
    custom.write_text(
        """
locations:
  canonical:
    - 右肺上叶
  aliases:
    右肺上叶: 右肺上叶
    右肺 上叶: 右肺上叶
densities:
  canonical:
    - 磨玻璃
  aliases:
    磨玻璃: 磨玻璃
""",
        encoding="utf-8",
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        L.load_label_table(custom)
    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "撞键" in excinfo.value.detail


def test_alias_pointing_to_unknown_canonical_is_rejected(tmp_path) -> None:
    """别名指向没登记的标准标签 → 报错（防手滑打错标签名）。"""

    custom = tmp_path / "labels.yaml"
    custom.write_text(
        """
locations:
  canonical:
    - 右肺上叶
  aliases:
    右上葉: 右肺上葉
densities:
  canonical:
    - 磨玻璃
  aliases:
    磨玻璃: 磨玻璃
""",
        encoding="utf-8",
    )

    with pytest.raises(NoduleEvalError) as excinfo:
        L.load_label_table(custom)
    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "未登记的标准标签" in excinfo.value.detail


def test_missing_table_file_is_rejected(tmp_path) -> None:
    """表文件不在 → 报错，不回落到任何内置兜底表。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        L.load_label_table(tmp_path / "nope.yaml")
    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "不存在" in excinfo.value.detail


# ------------------------------------------------------------ 唯一真源与概览


def test_both_sides_use_the_same_table() -> None:
    """金标准侧与模型侧调同一个函数 → 同一处病灶同一标签。"""

    gold = L.normalize_location("右下肺")
    model = L.normalize_location("右肺下叶")
    assert gold == model == "右肺下叶"

    gold_density = L.normalize_density("磨玻璃")
    model_density = L.normalize_density("GGN")
    assert gold_density == model_density == "磨玻璃"


def test_describe_and_aliases_for() -> None:
    """概览与「我们认哪些写法」可查（放射科能核对口径）。"""

    overview = L.describe_tables()
    assert overview["source_path"].endswith("labels.yaml")
    assert "右肺上叶" in overview["locations"]["canonical"]
    assert "磨玻璃" in overview["densities"]["canonical"]
    assert overview["locations"]["alias_count"] > 50
    assert overview["locations"]["segment_alias_count"] > 0

    aliases = L.aliases_for("右肺上叶")
    assert "rul" in aliases
    assert "右上叶" in aliases
    assert "右上肺野" in aliases

    density_aliases = L.aliases_for("磨玻璃", kind="density")
    assert "ggn" in density_aliases

    with pytest.raises(ValueError):
        L.aliases_for("右肺上叶", kind="不存在的类别")


def test_canonical_lists_are_exposed() -> None:
    assert "右肺上叶" in L.canonical_locations()
    assert "左肺下叶" in L.canonical_locations()
    assert "双肺" in L.canonical_locations()
    assert "磨玻璃" in L.canonical_densities()
    assert "部分实性" in L.canonical_densities()


def test_normalize_key_is_shared_by_both_kinds() -> None:
    """键归一是同一个函数 —— 两边的表不会各归各的。"""

    assert L.normalize_key("右肺 上叶") == L.normalize_key("右肺上叶")
    assert L.normalize_key("ＧＧＮ") == "ggn"
    assert L.normalize_key(None) == ""
