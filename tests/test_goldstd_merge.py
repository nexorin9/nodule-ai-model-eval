"""语义归并（任务 21）的验收测试。

三条主线：

1. **回指校验**：模型给的证据段必须是金标准原文里**连续存在**的一段；
   对不上 → 判解析失败 → **降级到规则基线**（不是判失败）。
2. **输出字段校验**：标签四项缺任一 → 判解析失败；
   ``confidence`` / ``needs_human_review`` 缺省 → 填默认值并**记一次解析降级**。
3. **原文不可变**：归并结果只写 ``model_findings`` / ``normalize_source`` 两列；
   金标准原文**逐单元格零改动**。

**替身纪律**：本文件里的 ``StubGateway`` 是**测试替身**，只存在于 ``tests/`` 层；
产品代码不 import 它，也不含任何本地回环地址。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.goldstd.merge import (
    MERGE_OUTPUT_FIELDS,
    MERGE_WRITABLE_COLUMNS,
    apply_merge_updates,
    build_merge_prompt,
    merge_by_model,
    merge_updates_for_labels,
    parse_merge_output,
    validate_evidence_span,
)
from nodule_eval.goldstd.schema import NORMALIZE_SOURCE_MODEL, NORMALIZE_SOURCE_RULE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 测试替身（只在本文件内）
# --------------------------------------------------------------------------


class StubGateway:
    """按预设文本回话的模型端点替身。

    记录收到的 system / user —— 用来断言「模型读到的就是金标准原文，
    一个字都没被改过」。
    """

    def __init__(self, reply: str, *, raises: Exception | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def complete(self, *, system: str, user: str, json_schema=None) -> str:
        self.calls.append({"system": system, "user": user, "json_schema": json_schema})
        if self.raises is not None:
            raise self.raises
        return self.reply


def _reply(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


#: 一段真实的胸片金标准原文（属性齐全）。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm，边界欠清。"

#: 模型对上面这段的归并结果（全部字段齐全）。
GOOD_PAYLOAD = {
    "presence": True,
    "location": "右肺下叶",
    "size_mm": 8.0,
    "density": "磨玻璃",
    "evidence_span": "右下肺见一磨玻璃结节，最大径约8mm",
    "confidence": 0.92,
    "needs_human_review": False,
}


# --------------------------------------------------------------------------
# 提示词（外置 · 可版本化）
# --------------------------------------------------------------------------


def test_prompt_file_is_external_and_versioned():
    """提示词外置成可版本化文件（沿用源产品 main.py:47-49 的做法）。"""

    prompt_path = PROJECT_ROOT / "prompts" / "goldstd_merge.v1.md"
    assert prompt_path.is_file(), "语义归并提示词必须外置成独立文件"
    text = prompt_path.read_text(encoding="utf-8")
    assert "v1" in prompt_path.name
    assert "金标准" in text


def test_prompt_injects_label_table_from_equivalence_table():
    """标签表在运行时从等价表填进提示词 —— 标签表换了不用改提示词。"""

    prompt = build_merge_prompt()
    from nodule_eval.goldstd.labels import canonical_densities, canonical_locations

    for label in canonical_locations():
        assert label in prompt, f"部位标准标签 {label} 漏进了提示词"
    for label in canonical_densities():
        assert label in prompt, f"密度标准标签 {label} 漏进了提示词"
    assert "{{LOCATIONS}}" not in prompt
    assert "{{DENSITIES}}" not in prompt


def test_prompt_missing_file_raises_instead_of_fallback(tmp_path: Path):
    """提示词文件缺失 → 报 CONTRACT_INVALID，**不回落内置兜底提示词**。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        build_merge_prompt(tmp_path / "no-such-prompt.md")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "提示词文件不存在" in excinfo.value.detail


def test_prompt_missing_placeholder_raises(tmp_path: Path):
    """提示词缺标签表占位符 → 报 CONTRACT_INVALID（口径查不出来就不许跑）。"""

    broken = tmp_path / "broken.md"
    broken.write_text("# 没有占位符的提示词", encoding="utf-8")
    with pytest.raises(NoduleEvalError) as excinfo:
        build_merge_prompt(broken)
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "占位符" in excinfo.value.detail


# --------------------------------------------------------------------------
# 输出字段校验
# --------------------------------------------------------------------------


def test_output_contract_has_seven_fields():
    """输出契约是七字段 —— 少一个都判解析失败。"""

    assert MERGE_OUTPUT_FIELDS == (
        "presence",
        "location",
        "size_mm",
        "density",
        "evidence_span",
        "confidence",
        "needs_human_review",
    )


def test_parse_accepts_well_formed_output():
    """合规输出解析出全部字段。"""

    parsed = parse_merge_output(_reply(GOOD_PAYLOAD))
    assert parsed.presence is True
    assert parsed.location == "右肺下叶"
    assert parsed.size_mm == 8.0
    assert parsed.density == "磨玻璃"
    assert parsed.evidence_span == "右下肺见一磨玻璃结节，最大径约8mm"
    assert parsed.confidence == pytest.approx(0.92)
    assert parsed.needs_human_review is False
    assert parsed.degraded_fields == ()


def test_parse_accepts_json_fenced_reply():
    """模型爱加 ```json 围栏 —— 剥掉是格式规整，不改内容。"""

    raw = "```json\n" + _reply(GOOD_PAYLOAD) + "\n```"
    parsed = parse_merge_output(raw)
    assert parsed.location == "右肺下叶"


@pytest.mark.parametrize("missing", ["presence", "location", "size_mm", "density"])
def test_parse_missing_required_field_fails(missing: str):
    """标签四项缺任一 → 解析失败（报 CONTRACT_INVALID 并点名缺哪个）。"""

    payload = {key: value for key, value in GOOD_PAYLOAD.items() if key != missing}
    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output(_reply(payload))
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert missing in excinfo.value.detail


def test_parse_missing_evidence_span_fails():
    """缺 evidence_span → 解析失败（没有回指锚点就没法复核）。"""

    payload = {key: value for key, value in GOOD_PAYLOAD.items() if key != "evidence_span"}
    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output(_reply(payload))
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "evidence_span" in excinfo.value.detail


def test_parse_missing_optional_fields_defaults_and_records_degradation():
    """confidence / needs_human_review 缺省 → 填默认值并**记一次解析降级**。"""

    payload = {
        key: value
        for key, value in GOOD_PAYLOAD.items()
        if key not in ("confidence", "needs_human_review")
    }
    parsed = parse_merge_output(_reply(payload))
    assert parsed.confidence == 0.0
    assert parsed.needs_human_review is False
    assert set(parsed.degraded_fields) == {"confidence", "needs_human_review"}


def test_parse_rejects_wrong_types():
    """字段类型不对 → 解析失败（不猜、不静默改数据）。"""

    bad_presence = dict(GOOD_PAYLOAD, presence="是")
    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output(_reply(bad_presence))
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "presence" in excinfo.value.detail

    bad_size = dict(GOOD_PAYLOAD, size_mm="约八毫米")
    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output(_reply(bad_size))
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "size_mm" in excinfo.value.detail


def test_parse_rejects_non_json():
    """模型回散文 → 解析失败（不猜它想说什么）。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output("根据影像所见，右下肺可见结节影，建议结合临床。")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "不是合法 JSON" in excinfo.value.detail


def test_parse_rejects_non_object_top_level():
    """顶层不是对象 → 解析失败。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        parse_merge_output("[1, 2, 3]")
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID


# --------------------------------------------------------------------------
# 回指校验（核心）
# --------------------------------------------------------------------------


def test_evidence_span_must_be_contiguous_substring_of_original():
    """证据段必须是原文里**连续存在**的一段。"""

    ok, why = validate_evidence_span("右下肺见一磨玻璃结节，最大径约8mm", GOLD_TEXT)
    assert ok is True
    assert "回指" in why

    ok, why = validate_evidence_span("右下肺见一磨玻璃结节。左肺清晰。", GOLD_TEXT)
    assert ok is False
    assert "不存在" in why


def test_evidence_span_tolerates_whitespace_only_difference():
    """空白差异不算数（原文有换行、模型抄成一行，命中）。"""

    original = "右下肺见一磨玻璃结节，\n最大径约8mm。"
    ok, _ = validate_evidence_span("右下肺见一磨玻璃结节，最大径约8mm", original)
    assert ok is True


def test_evidence_span_empty_fails():
    """空证据段 → 不通过。"""

    ok, why = validate_evidence_span("", GOLD_TEXT)
    assert ok is False
    assert why == "证据片段为空"


def test_fabricated_evidence_span_degrades_to_rules():
    """**模型编一句不存在的话当证据 → 整份输出作废，降级到规则基线。**"""

    payload = dict(GOOD_PAYLOAD, evidence_span="左上肺见一实性结节，最大径约15mm")
    gateway = StubGateway(_reply(payload))
    result = merge_by_model(GOLD_TEXT, gateway=gateway)

    assert result.degraded is True
    assert "回指校验未通过" in result.reason
    assert result.labels.source == NORMALIZE_SOURCE_RULE
    # 降级后仍给出**确定的**结论 —— 不是「这一例失败」
    assert result.labels.presence is True
    assert result.labels.location == "右肺下叶"


def test_fabricated_location_degrades_to_rules():
    """模型编一个原文里没有的部位 → 降级（标签复核挡住）。"""

    payload = dict(GOOD_PAYLOAD, location="左肺上叶")
    result = merge_by_model(GOLD_TEXT, gateway=StubGateway(_reply(payload)))
    assert result.degraded is True
    assert "标签复核未通过" in result.reason
    assert result.labels.source == NORMALIZE_SOURCE_RULE


def test_fabricated_size_degrades_to_rules():
    """模型编一个原文里没有的尺寸 → 降级。"""

    payload = dict(GOOD_PAYLOAD, size_mm=15.0)
    result = merge_by_model(GOLD_TEXT, gateway=StubGateway(_reply(payload)))
    assert result.degraded is True
    assert "标签复核未通过" in result.reason
    assert "尺寸" in result.reason


def test_fabricated_density_degrades_to_rules():
    """模型编一个原文里没有的密度 → 降级。"""

    payload = dict(GOOD_PAYLOAD, density="钙化")
    result = merge_by_model(GOLD_TEXT, gateway=StubGateway(_reply(payload)))
    assert result.degraded is True
    assert "标签复核未通过" in result.reason


def test_unknown_location_value_degrades_to_rules():
    """部位取值不在等价表里（左肺中叶这种不存在的解剖结构）→ 降级。"""

    payload = dict(GOOD_PAYLOAD, location="左肺中叶")
    result = merge_by_model(GOLD_TEXT, gateway=StubGateway(_reply(payload)))
    assert result.degraded is True
    assert "不在部位等价表里" in result.reason


def test_presence_contradicting_rules_degrades_to_rules():
    """模型与规则对**同一段证据**的判读矛盾 → 降级。

    原文写的是「未见明显结节」，模型却说「有病灶」——
    这不是「模型比规则强」，是模型读反了。
    """

    text = "双肺未见明显结节影。"
    payload = {
        "presence": True,
        "location": None,
        "size_mm": None,
        "density": None,
        "evidence_span": "双肺未见明显结节影",
        "confidence": 0.8,
        "needs_human_review": False,
    }
    result = merge_by_model(text, gateway=StubGateway(_reply(payload)))
    assert result.degraded is True
    assert "矛盾" in result.reason
    assert result.labels.source == NORMALIZE_SOURCE_RULE
    assert result.labels.presence is False


# --------------------------------------------------------------------------
# 归并主流程
# --------------------------------------------------------------------------


def test_merge_success_returns_model_source():
    """合规输出 → 走模型口径（``normalize_source=model``）。"""

    gateway = StubGateway(_reply(GOOD_PAYLOAD))
    result = merge_by_model(GOLD_TEXT, gateway=gateway)

    assert result.degraded is False
    assert result.labels.source == NORMALIZE_SOURCE_MODEL
    assert result.labels.presence is True
    assert result.labels.location == "右肺下叶"
    assert result.labels.size_mm == pytest.approx(8.0)
    assert result.labels.density == "磨玻璃"
    assert result.labels.confidence == pytest.approx(0.92)


def test_merge_model_reads_original_text_verbatim():
    """模型读到的 user 消息就是**金标准原文**，一个字都没被改过。"""

    gateway = StubGateway(_reply(GOOD_PAYLOAD))
    merge_by_model(GOLD_TEXT, gateway=gateway)
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["user"] == GOLD_TEXT
    assert "金标准" in str(gateway.calls[0]["system"])


def test_merge_reads_size_in_cm_as_mm():
    """原文写 cm、模型答 mm → 对得上（量纲换算后一致）。"""

    text = "右下肺见一磨玻璃结节，大小约0.8cm。"
    payload = {
        "presence": True,
        "location": "右肺下叶",
        "size_mm": 8.0,
        "density": "磨玻璃",
        "evidence_span": "右下肺见一磨玻璃结节，大小约0.8cm",
        "confidence": 0.9,
        "needs_human_review": False,
    }
    result = merge_by_model(text, gateway=StubGateway(_reply(payload)))
    assert result.degraded is False
    assert result.labels.size_mm == pytest.approx(8.0)


def test_merge_uses_alias_writing_from_original():
    """模型按原文写法答部位（「右下肺」）也能归一到标准标签。"""

    payload = dict(GOOD_PAYLOAD, location="右下肺")
    result = merge_by_model(GOLD_TEXT, gateway=StubGateway(_reply(payload)))
    assert result.degraded is False
    assert result.labels.location == "右肺下叶"


def test_merge_gateway_unreachable_degrades_to_rules():
    """模型端点不可达 → 降级到规则基线（不是判这一例失败）。"""

    gateway = StubGateway("", raises=RuntimeError("connection refused"))
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    assert result.degraded is True
    # 降级原因走**错误码表**的固定文案（任务 24）—— 不散落硬编码。
    assert "已降级为规则归并" in result.reason
    assert "connection refused" in result.reason
    assert result.labels.source == NORMALIZE_SOURCE_RULE
    assert result.labels.presence is True
    # 降级标记随标签走（引擎据此计「降级数」并挂人工复核）。
    assert result.labels.degraded is True
    assert result.labels.degrade_error_code == ErrorCode.MODEL_UNAVAILABLE.value
    assert result.labels.needs_human_review is True


def test_merge_invalid_json_degrades_to_rules():
    """模型回散文 → 降级（不是判失败）。"""

    gateway = StubGateway("根据影像所见，右下肺可见结节影。")
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    assert result.degraded is True
    assert "解析失败" in result.reason
    assert result.labels.source == NORMALIZE_SOURCE_RULE


def test_degraded_result_still_carries_rule_labels():
    """降级后拿到的仍是规则基线的**确定结论**，不是空标签。"""

    gateway = StubGateway("not json at all")
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    assert result.labels.presence is True
    assert result.labels.location == "右肺下叶"
    assert result.labels.size_mm == pytest.approx(8.0)
    assert result.labels.density == "磨玻璃"


def test_merge_result_records_raw_reply():
    """模型返回的原文随结果带出（事后能查模型当时说了什么）。"""

    gateway = StubGateway(_reply(GOOD_PAYLOAD))
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    assert "右下肺见一磨玻璃结节" in result.raw
    assert result.to_dict()["degraded"] is False


def test_merge_produces_lesions_like_rule_baseline():
    """语义归并也要填 ``lesions`` —— 两侧口径同构（任务 20 的契约）。"""

    gateway = StubGateway(_reply(GOOD_PAYLOAD))
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    assert len(result.labels.lesions) == 1
    lesion = result.labels.lesions[0]
    assert lesion.location == "右肺下叶"
    assert lesion.size_mm == pytest.approx(8.0)
    assert lesion.density == "磨玻璃"
    assert lesion.evidence_span == "右下肺见一磨玻璃结节，最大径约8mm"


# --------------------------------------------------------------------------
# 原文不可变：只写两列
# --------------------------------------------------------------------------


def test_merge_updates_only_touch_two_columns():
    """归并结果只写 model_findings 与 normalize_source 两列。"""

    assert MERGE_WRITABLE_COLUMNS == ("model_findings", "normalize_source")

    gateway = StubGateway(_reply(GOOD_PAYLOAD))
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    updates = merge_updates_for_labels(result.labels)
    assert set(updates) == set(MERGE_WRITABLE_COLUMNS)
    assert updates["normalize_source"] == NORMALIZE_SOURCE_MODEL
    assert "右肺下叶" in updates["model_findings"]
    assert "8.0mm" in updates["model_findings"]
    assert "磨玻璃" in updates["model_findings"]


def test_degraded_updates_mark_rule_source():
    """降级例的 normalize_source 标 rule —— 复核的人一眼看得出这例没走模型。"""

    gateway = StubGateway("not json")
    result = merge_by_model(GOLD_TEXT, gateway=gateway)
    updates = merge_updates_for_labels(result.labels)
    assert updates["normalize_source"] == NORMALIZE_SOURCE_RULE


def test_apply_updates_rejects_writing_gold_standard_text():
    """写金标准原文 → **直接抛错**（实现层断言，不是约定）。"""

    row = {
        "accession_no": "DEMO-0001",
        "gold_standard_text": GOLD_TEXT,
        "image_ref": "img://demo/1.png",
    }
    with pytest.raises(NoduleEvalError) as excinfo:
        apply_merge_updates(row, {"gold_standard_text": "改了"})
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "既有列" in excinfo.value.detail


def test_apply_updates_rejects_unknown_column():
    """写产品新增列以外的东西 → 抛错。"""

    row = {
        "accession_no": "DEMO-0001",
        "gold_standard_text": GOLD_TEXT,
        "image_ref": "img://demo/1.png",
    }
    with pytest.raises(NoduleEvalError) as excinfo:
        apply_merge_updates(row, {"verdict": "hit"})
    assert excinfo.value.code == ErrorCode.CONTRACT_INVALID
    assert "只允许写入" in excinfo.value.detail


def test_apply_updates_leaves_gold_standard_text_untouched():
    """写完之后金标准原文逐字不变。"""

    row = {
        "accession_no": "DEMO-0001",
        "gold_standard_text": GOLD_TEXT,
        "image_ref": "img://demo/1.png",
    }
    updated = apply_merge_updates(row, merge_updates_for_labels(
        merge_by_model(GOLD_TEXT, gateway=StubGateway(_reply(GOOD_PAYLOAD))).labels
    ))
    assert updated["gold_standard_text"] == GOLD_TEXT
    assert updated["accession_no"] == "DEMO-0001"
    assert updated["image_ref"] == "img://demo/1.png"
    assert updated["normalize_source"] == NORMALIZE_SOURCE_MODEL


def test_gold_standard_column_zero_change_cell_by_cell():
    """**逐单元格零改动** —— 一批行写完之后，原文列每一格都与写之前逐字相同。

    这是「金标准原文永不被改写」最直接的验收：不是比一行的摘要，
    是把整列每一格都拿出来比。
    """

    rows = [
        {
            "accession_no": "DEMO-0001",
            "gold_standard_text": "右下肺见一磨玻璃结节，最大径约8mm。",
            "image_ref": "img://demo/1.png",
        },
        {
            "accession_no": "DEMO-0002",
            "gold_standard_text": "双肺未见明显结节影。\n心影不大。",
            "image_ref": "img://demo/2.png",
        },
        {
            "accession_no": "DEMO-0003",
            "gold_standard_text": "左上肺见一实性结节，最大径约12mm。",
            "image_ref": "img://demo/3.png",
        },
    ]
    before = [dict(row) for row in rows]

    for index, row in enumerate(rows):
        payload = dict(GOOD_PAYLOAD)
        if index == 1:
            payload = {
                "presence": False,
                "location": None,
                "size_mm": None,
                "density": None,
                "evidence_span": "双肺未见明显结节影",
                "confidence": 0.9,
                "needs_human_review": False,
            }
        elif index == 2:
            payload = dict(
                GOOD_PAYLOAD,
                location="左肺上叶",
                size_mm=12.0,
                density="实性",
                evidence_span="左上肺见一实性结节，最大径约12mm",
            )
        result = merge_by_model(row["gold_standard_text"], gateway=StubGateway(_reply(payload)))
        rows[index] = apply_merge_updates(row, merge_updates_for_labels(result.labels))

    assert len(rows) == len(before)
    for index, (old, new) in enumerate(zip(before, rows)):
        assert new["gold_standard_text"] == old["gold_standard_text"], (
            f"第 {index + 1} 行的金标准原文被改写了"
        )
        assert new["accession_no"] == old["accession_no"]
        assert new["image_ref"] == old["image_ref"]
        assert new["normalize_source"] in (NORMALIZE_SOURCE_MODEL, NORMALIZE_SOURCE_RULE)


def test_presence_readable_only_by_model_is_accepted_but_flagged():
    """规则读不出存在性、模型读得出 → **接受**（这正是模型比规则强的地方），
    但挂人工复核（确定性口径复核不了，让人看一眼）。"""

    text = "右下肺见一淡薄高密度影。"
    payload = {
        "presence": True,
        "location": None,
        "size_mm": None,
        "density": None,
        "evidence_span": "右下肺见一淡薄高密度影",
        "confidence": 0.6,
        "needs_human_review": False,
    }
    result = merge_by_model(text, gateway=StubGateway(_reply(payload)))
    assert result.degraded is False
    assert result.labels.presence is True
    assert result.labels.needs_human_review is True


def test_merge_normalizer_plugs_into_engine_seam():
    """归并器能直接当引擎的归并口用 —— 且声明走模型端点（单独闸门）。"""

    from nodule_eval.contracts import CaseInput
    from nodule_eval.goldstd.merge import ModelMergeNormalizer

    normalizer = ModelMergeNormalizer(StubGateway(_reply(GOOD_PAYLOAD)))
    assert normalizer.uses_model_endpoint is True

    case = CaseInput(
        accession_no="DEMO-0001",
        gold_standard_text=GOLD_TEXT,
        gold_standard_normalized=GOLD_TEXT,
    )
    labels = normalizer(case)
    assert labels.source == NORMALIZE_SOURCE_MODEL
    assert labels.location == "右肺下叶"
    assert normalizer.last_result is not None
    assert normalizer.last_result.degraded is False


def test_merge_normalizer_degrades_without_raising():
    """模型端点挂了 → 归并口**不抛异常**，退回规则基线（归并失败 ≠ 这一例失败）。"""

    from nodule_eval.contracts import CaseInput
    from nodule_eval.goldstd.merge import ModelMergeNormalizer

    normalizer = ModelMergeNormalizer(StubGateway("", raises=RuntimeError("boom")))
    case = CaseInput(
        accession_no="DEMO-0001",
        gold_standard_text=GOLD_TEXT,
        gold_standard_normalized=GOLD_TEXT,
    )
    labels = normalizer(case)
    assert labels.source == NORMALIZE_SOURCE_RULE
    assert labels.presence is True
    assert normalizer.last_result.degraded is True


def test_merge_module_has_no_vendor_sdk_import():
    """业务层不 import 任何厂商 SDK（换端点只改网关实现）。"""

    source = (PROJECT_ROOT / "src" / "nodule_eval" / "goldstd" / "merge.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("import openai", "from openai", "import anthropic", "from anthropic"):
        assert forbidden not in source, f"归并层不许直接依赖厂商 SDK（{forbidden}）"


def test_merge_module_has_no_local_endpoint_address():
    """归并层不含任何本地回环地址（默认路径必须真外发）。"""

    source = (PROJECT_ROOT / "src" / "nodule_eval" / "goldstd" / "merge.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("localhost", "127.0.0.1", "example.com", "http://"):
        assert forbidden not in source, f"归并层不许出现本地替身地址（{forbidden}）"
