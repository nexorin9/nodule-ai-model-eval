"""属性三层的比对规则（任务 27）：**尺寸容差 · 部位等价 · 密度等价 · 候选选择**。

这一组断言回答四个问题 —— 每一个都会**直接改写敏感度与特异度**：

1. **容差边界在哪儿** —— 恰好等于阈值算不算一致？差 0.1mm 呢？差 2.1mm 呢？
2. **部位别名能不能对上** —— 放射科写「右下肺」、厂商回「右肺下叶」，
   认得出是同一处；认不出（不在等价表里）时**不能**当成一致。
3. **认不出的写法算什么** —— **不可比**：既不当一致、也不当不一致，
   单独计数（不混进 hit / miss）。
4. **多候选时拿哪个去比** —— 选择依据必须落在判定依据里，说得清「凭什么选它」。

**为什么这些边界值得逐条钉死**：容差差 0.1mm、别名差一个字，
在报告上都是「命中 / 漏诊」两栏之间的一次搬家 ——
敏感度与特异度直接跟着动，而报告上看不出是口径问题还是模型问题。
"""

from __future__ import annotations

import json

import pytest
from stub_inference import InferenceStub

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config, load_model_spec
from nodule_eval.contracts import (
    VERDICT_FAILED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_TRUE_NEGATIVE,
    CaseInput,
    EvalRequest,
    RunOptions,
)
from nodule_eval.engine import Engine
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd import GoldLabels
from nodule_eval.metrics import (
    ATTRIBUTE_WEIGHTS,
    LAYER_INCOMPARABLE,
    LAYER_MATCH,
    LAYER_MISMATCH,
    LAYER_NOT_COMPARED,
    compare_density,
    compare_location,
    compare_size,
    judge_case,
    normalize_tolerance,
    select_candidate,
)
from nodule_eval.store import RunStore


class _Candidate:
    """一个候选病灶（只带比对要用的三个属性）。"""

    def __init__(self, position: str, size_mm: float | None, density: str, confidence: float = 0.9):
        self.position = position
        self.size_mm = size_mm
        self.density = density
        self.confidence = confidence


#: 金标准：右下肺见一磨玻璃结节，大小约 8mm。
GOLD = GoldLabels(
    presence=True,
    location="右肺下叶",
    size_mm=8.0,
    density="磨玻璃",
)


# --------------------------------------------------------------------------
# 1. 尺寸容差：闭区间，边界逐格钉死
# --------------------------------------------------------------------------
def test_size_exactly_at_tolerance_counts_as_match():
    """恰好等于阈值算一致（闭区间）—— 边界上不能摇摆。"""

    outcome = compare_size(8.0, 10.0, tolerance_mm=2.0)
    assert outcome.status == LAYER_MATCH
    assert outcome.delta_mm == 2.0
    assert "容差 2mm 内" in outcome.reason


def test_size_just_inside_tolerance_counts_as_match():
    """差 0.1mm（阈值 − 0.1）算一致。"""

    outcome = compare_size(8.0, 9.9, tolerance_mm=2.0)
    assert outcome.status == LAYER_MATCH
    assert outcome.delta_mm == pytest.approx(1.9)


def test_size_tiny_difference_counts_as_match():
    """差 0.1mm 算一致（远在容差内）。"""

    outcome = compare_size(8.0, 8.1, tolerance_mm=2.0)
    assert outcome.status == LAYER_MATCH


def test_size_just_outside_tolerance_counts_as_mismatch():
    """差 2.1mm（阈值 + 0.1）算不一致。"""

    outcome = compare_size(8.0, 10.1, tolerance_mm=2.0)
    assert outcome.status == LAYER_MISMATCH
    assert outcome.delta_mm == pytest.approx(2.1)
    assert "超出容差 2mm" in outcome.reason


def test_size_boundary_survives_float_representation():
    """浮点表示误差不得把「恰好等于阈值」判成超出。

    ``8.1 - 10.1`` 在双精度下是 ``-2.0000000000000018`` ——
    不加余量，数学上恰好等于阈值的尺寸会被判成不一致。
    """

    outcome = compare_size(8.1, 10.1, tolerance_mm=2.0)
    assert outcome.status == LAYER_MATCH


def test_size_tolerance_is_configurable():
    """容差可配：改容差，边界跟着走。"""

    assert compare_size(8.0, 8.5, tolerance_mm=0.5).status == LAYER_MATCH
    assert compare_size(8.0, 8.6, tolerance_mm=0.5).status == LAYER_MISMATCH
    # 换回默认容差，同一对尺寸又一致了
    assert compare_size(8.0, 8.6, tolerance_mm=2.0).status == LAYER_MATCH


def test_size_missing_on_either_side_is_not_a_match():
    """金标准没写尺寸 → 未比对；模型没给尺寸 → 不可比。都不是一致。"""

    assert compare_size(None, 8.0, tolerance_mm=2.0).status == LAYER_NOT_COMPARED
    assert compare_size(8.0, None, tolerance_mm=2.0).status == LAYER_INCOMPARABLE


def test_negative_tolerance_is_rejected():
    """容差配错当场报错 —— 容差错了等于判定口径被悄悄改了。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        compare_size(8.0, 9.0, tolerance_mm=-1.0)
    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "不得为负数" in excinfo.value.detail

    with pytest.raises(NoduleEvalError):
        normalize_tolerance(float("nan"))


# --------------------------------------------------------------------------
# 2. 部位等价：别名命中 / 不一致 / 认不出
# --------------------------------------------------------------------------
def test_location_alias_hits_same_canonical_label():
    """放射科写「右下肺」、厂商回「右肺下叶」—— 同一处，算一致。"""

    outcome = compare_location("右肺下叶", "右下肺")
    assert outcome.status == LAYER_MATCH
    assert outcome.gold_value == "右肺下叶"
    assert outcome.candidate_value == "右肺下叶"


def test_location_english_alias_hits_same_canonical_label():
    """厂商回英文缩写（RUL）也要认得出。"""

    outcome = compare_location("右肺上叶", "RUL")
    assert outcome.status == LAYER_MATCH
    assert outcome.candidate_value == "右肺上叶"


def test_location_written_as_a_sentence_still_resolves():
    """厂商回的是一句话时，扫文本把部位捞出来。"""

    outcome = compare_location("右肺下叶", "右下肺见一结节")
    assert outcome.status == LAYER_MATCH
    assert outcome.candidate_value == "右肺下叶"


def test_location_mismatch_is_reported_with_both_labels():
    """部位不一致时两侧的标准标签都留出来，便于核对。"""

    outcome = compare_location("右肺下叶", "左肺下叶")
    assert outcome.status == LAYER_MISMATCH
    assert outcome.gold_value == "右肺下叶"
    assert outcome.candidate_value == "左肺下叶"


def test_location_unknown_writing_is_incomparable_not_mismatch():
    """写法不在等价表里 → **不可比**：不当一致，也不当不一致。"""

    outcome = compare_location("右肺下叶", "肺门区")
    assert outcome.status == LAYER_INCOMPARABLE
    assert outcome.incomparable is True
    assert "肺门区" in outcome.reason
    assert "不在部位等价表里" in outcome.reason


def test_location_unknown_gold_writing_is_incomparable():
    """金标准侧写法认不出来（解剖上不存在的结构）→ 不可比。"""

    outcome = compare_location("左肺中叶", "右肺下叶")
    assert outcome.status == LAYER_INCOMPARABLE
    assert "金标准部位写法" in outcome.reason


def test_location_missing_is_not_compared():
    """金标准没写部位 → 未比对（没有可比的对象）。"""

    assert compare_location(None, "右下肺").status == LAYER_NOT_COMPARED
    assert compare_location("", "右下肺").status == LAYER_NOT_COMPARED


# --------------------------------------------------------------------------
# 3. 密度等价：同一套口径
# --------------------------------------------------------------------------
def test_density_alias_hits_same_canonical_label():
    """磨玻璃 ↔ GGN ↔ GGO 认作同一密度。"""

    for writing in ("GGN", "GGO", "磨玻璃影"):
        outcome = compare_density("磨玻璃", writing)
        assert outcome.status == LAYER_MATCH, writing
        assert outcome.candidate_value == "磨玻璃"


def test_density_mismatch_is_reported_with_both_labels():
    outcome = compare_density("磨玻璃", "实性")
    assert outcome.status == LAYER_MISMATCH
    assert outcome.gold_value == "磨玻璃"
    assert outcome.candidate_value == "实性"


def test_density_unknown_writing_is_incomparable():
    """密度写法认不出来 → 不可比。"""

    outcome = compare_density("磨玻璃", "密度不均")
    assert outcome.status == LAYER_INCOMPARABLE
    assert "密度不均" in outcome.reason


def test_density_missing_is_not_compared():
    assert compare_density(None, "磨玻璃").status == LAYER_NOT_COMPARED


# --------------------------------------------------------------------------
# 4. 候选选择：拿最接近的那个去比，依据要说得清
# --------------------------------------------------------------------------
def test_selection_picks_the_candidate_closest_to_the_gold():
    """三个候选里挑部位对得上的那一个。"""

    candidates = (
        _Candidate("左肺上叶", 8.0, "磨玻璃"),
        _Candidate("右下肺", 8.0, "磨玻璃"),
        _Candidate("右下肺", 20.0, "钙化"),
    )
    selection = select_candidate(candidates, GOLD, tolerance_mm=2.0)
    assert selection is not None
    assert selection.index == 1
    assert selection.outcomes["location"].status == LAYER_MATCH
    assert selection.outcomes["density"].status == LAYER_MATCH
    assert "最接近" in selection.basis


def test_selection_scores_location_above_density_above_size():
    """权重口径：部位 > 密度 > 尺寸（写在 ATTRIBUTE_WEIGHTS 里，可核对）。"""

    assert ATTRIBUTE_WEIGHTS["location"] > ATTRIBUTE_WEIGHTS["density"]
    assert ATTRIBUTE_WEIGHTS["density"] > ATTRIBUTE_WEIGHTS["size"]

    # 部位对但密度错 vs 部位错但密度对 —— 前者得分更高
    location_right = select_candidate(
        (_Candidate("右下肺", 8.0, "实性"),), GOLD, tolerance_mm=2.0
    )
    density_right = select_candidate(
        (_Candidate("左肺下叶", 8.0, "磨玻璃"),), GOLD, tolerance_mm=2.0
    )
    assert location_right is not None and density_right is not None
    assert location_right.score > density_right.score


def test_selection_tie_takes_the_first_and_says_so():
    """同分取厂商返回顺序里的第一个，并在依据里写明同分个数。"""

    candidates = (
        _Candidate("右下肺", 8.0, "磨玻璃"),
        _Candidate("右下肺", 8.0, "磨玻璃"),
    )
    selection = select_candidate(candidates, GOLD, tolerance_mm=2.0)
    assert selection is not None
    assert selection.index == 0
    assert selection.tie_count == 2
    assert "同分" in selection.basis


def test_selection_is_none_when_no_candidate():
    assert select_candidate((), GOLD, tolerance_mm=2.0) is None


# --------------------------------------------------------------------------
# 5. 判定层接上属性三层
# --------------------------------------------------------------------------
def test_hit_judgement_carries_attribute_layers_and_selection():
    """命中时逐层结论、选中候选与选择依据一并落进判定依据。"""

    judgement = judge_case(GOLD, (_Candidate("右下肺", 8.0, "磨玻璃"),))
    assert judgement.verdict == VERDICT_HIT
    assert judgement.size_layer == LAYER_MATCH
    assert judgement.location_layer == LAYER_MATCH
    assert judgement.density_layer == LAYER_MATCH
    assert judgement.incomparable_layers == ()
    assert judgement.selected_candidate == "右下肺 | 8.0mm | 磨玻璃"
    assert "最接近" in judgement.selection_basis or "只返回 1 个候选" in judgement.selection_basis
    assert judgement.attribute_notes and len(judgement.attribute_notes) == 3


def test_incomparable_attribute_is_counted_separately_and_not_as_a_match():
    """部位写法认不出来 → 记「不可比」，不当一致也不当不一致。"""

    judgement = judge_case(GOLD, (_Candidate("肺门区", 8.0, "磨玻璃"),))
    assert judgement.incomparable_layers == ("location",)
    assert judgement.incomparable_reason
    assert "肺门区" in judgement.incomparable_reason
    # 部位层不是「一致」，也不是「不一致」
    assert judgement.location_layer == LAYER_INCOMPARABLE
    assert judgement.location_layer not in (LAYER_MATCH, LAYER_MISMATCH)
    # 其余两层照常给结论
    assert judgement.size_layer == LAYER_MATCH
    assert judgement.density_layer == LAYER_MATCH


def test_size_tolerance_reaches_the_judgement():
    """容差由判定入口可配 —— 容差决定这一层是���致还是不��致。"""

    candidate = (_Candidate("右下肺", 9.0, "磨玻璃"),)
    loose = judge_case(GOLD, candidate, size_tolerance_mm=2.0)
    tight = judge_case(GOLD, candidate, size_tolerance_mm=0.5)
    assert loose.size_layer == LAYER_MATCH
    assert tight.size_layer == LAYER_MISMATCH


def test_non_hit_verdicts_leave_attribute_layers_uncompared():
    """存在性不一致时直接定案，属性三层是「未比对」。"""

    miss = judge_case(GOLD, ())
    assert miss.verdict == VERDICT_MISS
    assert miss.size_layer == LAYER_NOT_COMPARED
    assert miss.location_layer == LAYER_NOT_COMPARED

    negative_gold = GoldLabels(presence=False)
    false_positive = judge_case(negative_gold, (_Candidate("右下肺", 8.0, "磨玻璃"),))
    assert false_positive.verdict == VERDICT_FALSE_POSITIVE
    assert false_positive.size_layer == LAYER_NOT_COMPARED

    true_negative = judge_case(negative_gold, ())
    assert true_negative.verdict == VERDICT_TRUE_NEGATIVE
    assert true_negative.density_layer == LAYER_NOT_COMPARED


def test_failure_still_wins_over_attribute_comparison():
    """失败优先：调用失败时不做属性比对，判 failed。"""

    judgement = judge_case(
        GOLD,
        (_Candidate("右下肺", 8.0, "磨玻璃"),),
        error="推理服务暂时不可用（HTTP 500）：服务端错误",
        error_code=ErrorCode.INFERENCE_5XX.value,
    )
    assert judgement.verdict == VERDICT_FAILED
    assert judgement.size_layer == LAYER_NOT_COMPARED


# --------------------------------------------------------------------------
# 6. 端到端：本轮的尺寸容差一路传到逐例产物
# --------------------------------------------------------------------------
def _engine(tmp_path):
    config = load_config()
    spec = load_model_spec("demo-model")
    store = RunStore.open(tmp_path / "store.sqlite3", runs_dir=tmp_path / "runs")
    engine = Engine(
        config,
        spec,
        client=HttpClient(sleep=lambda _seconds: None),
        runs_dir=tmp_path / "runs",
        store=store,
    )
    engine.test_store = store  # type: ignore[attr-defined]
    return engine


def test_engine_passes_run_options_tolerance_into_the_judgement(tmp_path, full_env, monkeypatch):
    """``options.size_tolerance_mm`` 一路传到判定层，并落进逐例产物。"""

    stub = InferenceStub(
        candidates=[{"position": "右下肺", "size_mm": 9.0, "density": "磨玻璃", "confidence": 0.9}]
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        request = EvalRequest(
            run_id="attr-tolerance",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="属性层端到端样例",
            cases=(
                CaseInput(
                    accession_no="ATTR-0001",
                    gold_standard_text="右下肺见一磨玻璃结节，大小约 8mm。",
                    image_ref="dx://local/0001",
                    study_uid="STUDY-0001",
                    row_no=1,
                ),
            ),
            options=RunOptions(size_tolerance_mm=0.5),
        )
        response = engine.run(request)
    finally:
        stub.stop()

    row = response.case_rows[0]
    assert row.verdict == VERDICT_HIT

    artifact = (
        tmp_path / "runs" / "attr-tolerance" / "cases" / "ATTR-0001" / "attempt-0001" / "case.json"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    judgement = payload["judgement"]
    assert judgement["layers"]["size"] == LAYER_MISMATCH
    assert judgement["layers"]["location"] == LAYER_MATCH
    assert judgement["selected_candidate"].startswith("右下肺")
    assert judgement["selection_basis"]


def test_engine_default_tolerance_treats_one_millimetre_as_match(tmp_path, full_env, monkeypatch):
    """缺省容差 2mm：差 1mm 算一致（端到端跑一次真实出站链路）。"""

    stub = InferenceStub(
        candidates=[{"position": "右下肺", "size_mm": 9.0, "density": "磨玻璃", "confidence": 0.9}]
    )
    stub.start()
    monkeypatch.setenv("INFERENCE_BASE_URL", stub.base_url)
    try:
        engine = _engine(tmp_path)
        request = EvalRequest(
            run_id="attr-tolerance-default",
            model_id="demo-model",
            model_version="v1.0.0",
            dataset_ref="属性层端到端样例",
            cases=(
                CaseInput(
                    accession_no="ATTR-0002",
                    gold_standard_text="右下肺见一磨玻璃结节，大小约 8mm。",
                    image_ref="dx://local/0002",
                    study_uid="STUDY-0002",
                    row_no=1,
                ),
            ),
        )
        response = engine.run(request)
    finally:
        stub.stop()

    artifact = (
        tmp_path
        / "runs"
        / "attr-tolerance-default"
        / "cases"
        / "ATTR-0002"
        / "attempt-0001"
        / "case.json"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["judgement"]["layers"]["size"] == LAYER_MATCH
    assert response.case_rows[0].verdict == VERDICT_HIT
