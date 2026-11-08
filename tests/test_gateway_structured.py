"""结构化输出、协议兼容与可审计调用元数据（任务 23）。

院内模型端点的能力参差不齐，这三条断言对应的就是现场会遇到的四种情况：

1. **端点不认 ``response_format``** —— 回一句「不支持这个参数」。
   网关必须**自动回退**（去掉该参数 + 改用 strict JSON 提示词），
   而不是把这一例判死；
2. **端点认参数但不守规矩** —— 回一段带围栏的散文。
   网关必须**做 1 次修复重试**，仍不可解析才判解析失败；
3. **模型少给字段** —— 必填字段缺任一即判解析失败，**不猜默认值补齐**；
4. **事后要能查** —— 每次调用留一份元数据（调了哪个模型、哪个端点、
   耗时、token、提示词是第几版），且**只落哈希、不落原文**
   （提示词与返回里可能带金标准原文，可能含患者可识别信息）。

另有一条硬约束：**网关可替换性** —— 只依赖 OpenAI-compatible 一种协议，
换院内自建端点或云端点只改 ``base_url`` 与 ``model``，业务代码一行不改。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

import pytest
from conftest import make_env

from nodule_eval.adapters.http import HttpClient
from nodule_eval.config import load_config
from nodule_eval.errors import ErrorCode, NoduleEvalError
from nodule_eval.gateway import ModelGateway
from nodule_eval.goldstd.merge import (
    MERGE_JSON_SCHEMA,
    MERGE_OUTPUT_FIELDS,
    ModelMergeNormalizer,
    merge_by_model,
)
from nodule_eval.structured import (
    build_response_format,
    extract_json_payload,
    is_response_format_unsupported,
    required_fields_from_schema,
    validate_required_fields,
)
from stub_model import ModelStub

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

#: 一份**合法**的归并输出（七字段齐全）。
GOOD_PAYLOAD: dict[str, Any] = {
    "presence": True,
    "location": "右肺下叶",
    "size_mm": 8.0,
    "density": "磨玻璃",
    "evidence_span": "右下肺见一磨玻璃结节",
    "confidence": 0.9,
    "needs_human_review": False,
}

#: 一段真实胸片金标准原文。
GOLD_TEXT = "右下肺见一磨玻璃结节，最大径约8mm。"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _gateway(
    base_url: str | None = None,
    *,
    sleeps: list[float] | None = None,
    **env: str,
) -> ModelGateway:
    """造一个网关（环境变量全是占位值，无真实凭据）。

    ``sleeps`` 用来把退避秒数记下来（不真睡）—— 否则「前两次 5xx、第三次成功」
    这条用例要真的等 12 秒。
    """

    client = HttpClient(sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))
    return ModelGateway(load_config(make_env(**env)), client=client, base_url=base_url)


def _reply(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------
# 1. 请求侧：JSON Schema → response_format
# --------------------------------------------------------------------------


def test_json_schema_is_sent_as_response_format():
    """给了 JSON Schema 就按 OpenAI-compatible 的标准形状发出去。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete_structured(
            system="s",
            user="u",
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    body = stub.requests[0]["body"]
    assert "response_format" in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["required"] == [
        "presence",
        "location",
        "size_mm",
        "density",
        "evidence_span",
    ]


def test_merge_schema_declares_the_seven_field_contract():
    """归并 schema 的字段集与七字段输出契约**逐字对齐**（不各写一份）。"""

    assert set(MERGE_JSON_SCHEMA["properties"]) >= set(MERGE_OUTPUT_FIELDS)
    for name in MERGE_OUTPUT_FIELDS:
        assert name in MERGE_JSON_SCHEMA["properties"], f"{name} 不在 schema 里"


def test_response_format_helper_shape():
    """``build_response_format`` 的形状是标准形状。"""

    fmt = build_response_format({"type": "object"}, name="x")
    assert fmt == {
        "type": "json_schema",
        "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True},
    }


def test_required_fields_are_read_from_schema():
    """必填字段清单从 schema 的 ``required`` 读（不在代码里另写一份）。"""

    assert required_fields_from_schema(MERGE_JSON_SCHEMA) == (
        "presence",
        "location",
        "size_mm",
        "density",
        "evidence_span",
    )
    assert required_fields_from_schema({}) == ()
    assert required_fields_from_schema(None) == ()


# --------------------------------------------------------------------------
# 2. 兼容回退：端点不认 response_format
# --------------------------------------------------------------------------


def test_endpoint_rejecting_response_format_triggers_fallback():
    """端点回 400「不支持 response_format」→ **自动回退**，不判这一例死。"""

    stub = ModelStub(reject_response_format=True)
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="s",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 2, "回退应再问一次（一次带参数、一次不带）"
    assert "response_format" in stub.requests[0]["body"]
    assert "response_format" not in stub.requests[1]["body"], "回退请求必须去掉该参数"
    assert result.payload["presence"] is True
    assert result.fallback_used is True
    assert result.meta.response_format_fallback is True


def test_fallback_request_carries_strict_json_instruction():
    """回退时提示词里补上「只输出一个 JSON 对象」的口径。"""

    stub = ModelStub(reject_response_format=True)
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete_structured(
            system="你是归并器",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    system_prompt = stub.requests[1]["body"]["messages"][0]["content"]
    assert "JSON" in system_prompt
    assert "你是归并器" in system_prompt, "原来的口径不能丢"


def test_ordinary_client_error_is_not_treated_as_unsupported():
    """**普通** 4xx（提示词超长）不触发回退 —— 回退只会掩盖真问题。"""

    stub = ModelStub(
        status_sequence=[400],
        error_body={
            "error": {
                "code": "invalid_request_error",
                "param": "messages",
                "message": "system prompt exceeds context window",
            }
        },
    )
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete_structured(
                system="s",
                user="u",
                json_schema=MERGE_JSON_SCHEMA,
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.MODEL_REQUEST_REJECTED
    assert len(stub.requests) == 1, "普通 4xx 不该再问一次"
    assert "messages" in excinfo.value.message


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, "response_format is not supported by this endpoint", True),
        (400, "unsupported_value: param=response_format", True),
        (400, "unknown parameter json_schema", True),
        (422, "response_format not implemented", True),
        (400, "system prompt exceeds context window", False),
        (400, "invalid api key", False),
        (429, "response_format is not supported", False),
        (500, "response_format is not supported", False),
        (None, "response_format is not supported", False),
    ],
)
def test_unsupported_detection_table(status: int | None, body: str, expected: bool):
    """「端点不认这个参数」的判定表（认不出就不回退，不硬套猜测）。"""

    assert is_response_format_unsupported(status, body) is expected


# --------------------------------------------------------------------------
# 3. 修复重试：模型回的不是 JSON
# --------------------------------------------------------------------------


def test_non_json_reply_triggers_one_repair_retry():
    """模型回散文 → **做 1 次修复重试**（把上一轮原文与解析错误回给它）。"""

    stub = ModelStub(content_sequence=["根据影像所见，右下肺可见结节影。", _reply(GOOD_PAYLOAD)])
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="你是归并器",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 2, "只允许 1 次修复重试"
    repair_messages = stub.requests[1]["body"]["messages"]
    assert len(repair_messages) == 4, "修复重试要把上一轮原文与错误一起回给它"
    assert repair_messages[2]["role"] == "assistant"
    assert "结节影" in repair_messages[2]["content"], "要把上一轮原文原样回给它"
    assert "JSON" in repair_messages[3]["content"]
    assert result.payload["presence"] is True
    assert result.meta.repair_used is True
    assert result.fallback_used is True


def test_unparseable_after_repair_is_a_parse_failure():
    """修复重试后仍不是 JSON → 判**解析失败**（不猜、不填默认值）。"""

    stub = ModelStub(content_sequence=["还是散文，不是 JSON。", "依然不是 JSON。"])
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete_structured(
                system="s",
                user=GOLD_TEXT,
                json_schema=MERGE_JSON_SCHEMA,
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "修复重试" in excinfo.value.detail
    assert len(stub.requests) == 2, "修复重试**只做一次**（不许无限重试）"


def test_json_fence_is_tolerated():
    """模型爱加 ```json 围栏 —— 剥掉是格式规整，不算解析失败。"""

    stub = ModelStub(content="```json\n" + _reply(GOOD_PAYLOAD) + "\n```")
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="s",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    assert result.payload["location"] == "右肺下叶"
    assert len(stub.requests) == 1, "剥围栏不该触发修复重试"


def test_empty_reply_is_a_parse_failure():
    """模型回空串 → 判解析失败，**不用空串冒充「模型没说话」**。"""

    stub = ModelStub(content="")
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete_structured(
                system="s",
                user=GOLD_TEXT,
                json_schema=MERGE_JSON_SCHEMA,
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID


def test_extract_json_payload_rejects_non_object():
    """顶层不是对象 → 解析失败（数组也不行）。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        extract_json_payload("[1, 2, 3]")
    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "顶层" in excinfo.value.detail


# --------------------------------------------------------------------------
# 4. 输出契约校验：缺字段即判解析失败，不猜默认值
# --------------------------------------------------------------------------


def test_missing_required_field_is_a_parse_failure():
    """必填字段缺任一 → 判解析失败，并**写明缺哪个字段**。"""

    payload = dict(GOOD_PAYLOAD)
    payload.pop("evidence_span")
    stub = ModelStub(content=_reply(payload))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError) as excinfo:
            gateway.complete_structured(
                system="s",
                user=GOLD_TEXT,
                json_schema=MERGE_JSON_SCHEMA,
            )
    finally:
        stub.stop()

    assert excinfo.value.code is ErrorCode.CONTRACT_INVALID
    assert "evidence_span" in excinfo.value.detail


def test_missing_field_is_not_filled_with_a_default():
    """缺字段**绝不被默认值补齐**（补了就等于替模型编结论）。"""

    payload = dict(GOOD_PAYLOAD)
    payload.pop("density")
    stub = ModelStub(content=_reply(payload))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        with pytest.raises(NoduleEvalError):
            gateway.complete_structured(
                system="s",
                user=GOLD_TEXT,
                json_schema=MERGE_JSON_SCHEMA,
            )
    finally:
        stub.stop()


def test_validate_required_fields_names_every_missing_field():
    """缺多个字段 → 一次把缺的全列出来。"""

    with pytest.raises(NoduleEvalError) as excinfo:
        validate_required_fields({"presence": True}, ["presence", "location", "density"])
    assert "location" in excinfo.value.detail
    assert "density" in excinfo.value.detail
    assert "presence" not in excinfo.value.detail.split("缺必填字段：")[1]


def test_null_value_is_allowed_but_missing_key_is_not():
    """值为 ``null`` 是合法的（读不出就 null）；**键不存在**才是缺字段。"""

    payload = dict(GOOD_PAYLOAD, location=None, size_mm=None, density=None)
    stub = ModelStub(content=_reply(payload))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="s",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    assert result.payload["location"] is None


# --------------------------------------------------------------------------
# 5. 可审计调用元数据
# --------------------------------------------------------------------------


def test_call_metadata_has_every_auditable_field():
    """元数据字段齐备（飞检问询「这例凭什么算漏判」要靠它）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="你是归并器",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    meta = result.meta.to_dict()
    for field in (
        "tool_call_id",
        "model",
        "base_url_host",
        "started_at",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "prompt_sha256",
        "raw_response_sha256",
        "fallback_used",
    ):
        assert field in meta, f"元数据缺字段 {field}"

    assert meta["model"] == "nodule-goldstd-merge"
    assert meta["base_url_host"] == "127.0.0.1"
    assert meta["prompt_tokens"] == 128
    assert meta["completion_tokens"] == 64
    assert meta["latency_ms"] >= 0
    assert meta["started_at"]
    assert meta["fallback_used"] is False


def test_metadata_hashes_instead_of_raw_text():
    """**只落哈希，不落原文** —— 提示词与返回里可能有患者可识别信息。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="你是归并器",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    meta = result.meta.to_dict()
    assert len(meta["prompt_sha256"]) == 64
    assert len(meta["raw_response_sha256"]) == 64
    blob = json.dumps(meta, ensure_ascii=False)
    assert GOLD_TEXT not in blob, "提示词原文不得落进元数据"
    assert "右下肺见一磨玻璃结节" not in blob, "模型返回原文不得落进元数据"


def test_prompt_hash_changes_with_the_prompt_and_not_with_the_answer():
    """提示词哈希跟提示词走（可对得上 prompts/ 目录里的具体版本）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        first = gateway.complete_structured(
            system="口径 v1",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
        second = gateway.complete_structured(
            system="口径 v2",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    assert first.meta.prompt_sha256 != second.meta.prompt_sha256
    assert first.meta.raw_response_sha256 == second.meta.raw_response_sha256


def test_tool_call_id_is_generated_and_stable_per_call():
    """``tool_call_id`` 每次调用各不相同（可与调用底账对上）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        first = gateway.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )
        second = gateway.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )
    finally:
        stub.stop()

    assert first.meta.tool_call_id != second.meta.tool_call_id
    assert first.meta.tool_call_id.startswith("tc_")


def test_caller_supplied_tool_call_id_is_kept():
    """调用方给了 id 就用它（引擎要把模型调用与逐例行挂上同一个 id）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="s",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
            tool_call_id="tc_fixed_0001",
        )
    finally:
        stub.stop()

    assert result.meta.tool_call_id == "tc_fixed_0001"


def test_metadata_is_exposed_on_the_gateway():
    """网关保留最近一次元数据（任务 33 落 model_calls.jsonl 时取它）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )
        assert gateway.last_meta is not None
        assert gateway.last_meta.tool_call_id.startswith("tc_")
    finally:
        stub.stop()


def test_plain_complete_also_records_metadata():
    """裸文本调用同样留元数据（两条出口都不能没有底账）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete(system="s", user=GOLD_TEXT)
    finally:
        stub.stop()

    assert gateway.last_meta is not None
    assert gateway.last_meta.fallback_used is False
    assert gateway.last_meta.prompt_tokens == 128


def test_missing_usage_is_not_reported_as_zero_tokens():
    """端点没报 token 计数 → 留 ``None``，**不填 0**（「没用 token」是另一回事）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        original = stub.build_payload

        def _without_usage(request: Mapping[str, Any] | None = None) -> dict[str, Any]:
            payload = original(request)
            payload.pop("usage", None)
            return payload

        stub.build_payload = _without_usage  # type: ignore[method-assign]
        result = gateway.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )
    finally:
        stub.stop()

    assert result.meta.prompt_tokens is None
    assert result.meta.completion_tokens is None


# --------------------------------------------------------------------------
# 6. 网关可替换性：换端点只改配置
# --------------------------------------------------------------------------


def test_swapping_endpoint_only_changes_url_and_model():
    """换院内自建端点 / 云端点 → 只改 base_url 与 model，**业务代码不动**。"""

    stub = ModelStub()
    stub.start()
    try:
        onprem = _gateway(stub.base_url, OPENAI_MODEL="院内自建模型")
        onprem.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )
        cloud = _gateway(
            None, OPENAI_MODEL="云端模型", OPENAI_BASE_URL=stub.base_url + "/v2"
        )
        cloud.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )
    finally:
        stub.stop()

    assert stub.requests[0]["body"]["model"] == "院内自建模型"
    assert stub.requests[1]["body"]["model"] == "云端模型"
    assert stub.requests[0]["path"] == "/chat/completions"
    assert stub.requests[1]["path"] == "/v2/chat/completions"


def _code_string_literals(path: Path) -> list[str]:
    """取模块里**代码里**的字符串字面量（**不含注释与文档字符串**）。

    文档里提到「response_format」是正常的说明文字；**代码里**出现它
    才意味着「这一层在管协议细节」。
    """

    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def test_only_the_protocol_layer_owns_the_model_endpoint_path():
    """模型端点路径只在网关与配置层出现（业务层不自己拼 URL）。"""

    owners = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in SRC_DIR.rglob("*.py")
        if any("chat/completions" in text for text in _code_string_literals(path))
    }
    assert owners == {"src/nodule_eval/gateway.py", "src/nodule_eval/config.py"}, (
        f"模型端点路径只该在网关与配置层出现，实际：{owners}"
    )


def test_only_the_gateway_sets_the_structured_output_knob():
    """``response_format`` 只在**协议层**出现（网关 + 协议助手），业务层不碰。"""

    owners = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in SRC_DIR.rglob("*.py")
        if any("response_format" in text for text in _code_string_literals(path))
    }
    assert owners == {
        "src/nodule_eval/gateway.py",
        "src/nodule_eval/structured.py",
    }, f"response_format 只该出现在协议层，实际：{owners}"


def test_business_layers_never_mention_the_structured_output_knob():
    """业务层（归并 / 判定 / 装配 / 契约）**不自己拼协议细节**。"""

    business = [
        path
        for path in SRC_DIR.rglob("*.py")
        if path.parent.name in {"goldstd", "metrics", "sheets"}
        or path.name in {"engine.py", "caseset.py", "contracts.py", "store.py"}
    ]
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in business
        if any("response_format" in text for text in _code_string_literals(path))
    ]
    assert offenders == [], f"业务层不得出现 response_format：{offenders}"


def test_business_layer_has_no_vendor_sdk_dependency():
    """业务层不 import 任何厂商 SDK（换端点只改环境变量）。"""

    banned = ("openai", "langchain", "anthropic", "dashscope", "zhipuai", "ollama", "litellm")
    pattern = re.compile(
        r"^\s*(?:from|import)\s+(?:" + "|".join(banned) + r")\b", re.MULTILINE
    )
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in SRC_DIR.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"src/ 里不得依赖厂商 SDK：{offenders}"


# --------------------------------------------------------------------------
# 7. 与语义归并层端到端（真 socket + 真 Bearer 头）
# --------------------------------------------------------------------------


def test_merge_uses_the_structured_channel():
    """归并层优先走结构化通道（请求里带 response_format）。"""

    stub = ModelStub(content=_reply(GOOD_PAYLOAD))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = merge_by_model(GOLD_TEXT, gateway=gateway)
    finally:
        stub.stop()

    assert "response_format" in stub.requests[0]["body"]
    assert result.degraded is False
    assert result.labels.location == "右肺下叶"
    assert result.labels.source == "model"


def test_merge_degrades_to_rules_when_structured_output_is_invalid():
    """结构化输出不合契约 → **降级到规则基线**（不是「这一例失败」）。"""

    payload = dict(GOOD_PAYLOAD)
    payload.pop("evidence_span")
    stub = ModelStub(content=_reply(payload))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = merge_by_model(GOLD_TEXT, gateway=gateway)
    finally:
        stub.stop()

    assert result.degraded is True
    assert result.labels.source == "rule"
    assert result.labels.presence is True, "降级后仍给出确定的结论"


def test_merge_survives_an_endpoint_without_response_format():
    """端点不认 response_format → 归并照样跑通（回退路径端到端）。"""

    stub = ModelStub(reject_response_format=True, content=_reply(GOOD_PAYLOAD))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = merge_by_model(GOLD_TEXT, gateway=gateway)
    finally:
        stub.stop()

    assert result.degraded is False
    assert result.labels.location == "右肺下叶"
    assert len(stub.requests) == 2


def test_merge_reports_metadata_after_a_structured_call():
    """归并走完结构化通道后，元数据可查（模型、端点、耗时都在）。"""

    stub = ModelStub(content=_reply(GOOD_PAYLOAD))
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        normalizer = ModelMergeNormalizer(gateway)
        normalizer(_Case())
    finally:
        stub.stop()

    meta = gateway.last_meta
    assert meta is not None
    assert meta.model == "nodule-goldstd-merge"
    assert meta.base_url_host == "127.0.0.1"
    assert meta.prompt_sha256


class _Case:
    """最小病例（归并口只需要金标准原文与它的归一副本）。"""

    gold_standard_text = GOLD_TEXT
    gold_standard_normalized = None


# --------------------------------------------------------------------------
# 8. 传输层失败不被结构化路径掩盖
# --------------------------------------------------------------------------


def test_unreachable_endpoint_still_raises_transport_error():
    """端点连不上 → 仍抛传输层错误（**不被结构化路径吞掉**）。"""

    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()

    import requests

    session = requests.Session()
    session.trust_env = False
    client = HttpClient(session=session, sleep=lambda _s: None)
    gateway = ModelGateway(load_config(make_env()), client=client, base_url=f"http://127.0.0.1:{port}")

    with pytest.raises(NoduleEvalError) as excinfo:
        gateway.complete_structured(
            system="s", user=GOLD_TEXT, json_schema=MERGE_JSON_SCHEMA
        )

    assert excinfo.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert excinfo.value.fields["failure_class"] == "connection_refused"


# --------------------------------------------------------------------------
# 9. 尝试次数如实计数（审计问「这例调了模型几次」）
# --------------------------------------------------------------------------


def test_transport_retry_is_counted_in_attempts():
    """前两次 5xx、第三次成功 → attempts 必须是 **3**，不是 1。

    只报「最后一次请求」的话，审计看到 ``attempts: 1`` 会以为只调了一次 ——
    端点其实被打了三次（厂商的计费与限流都按这个数）。
    """

    stub = ModelStub(status_sequence=[500, 500, 200])
    stub.start()
    try:
        sleeps: list[float] = []
        gateway = _gateway(stub.base_url, sleeps=sleeps)
        gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "替身确实收到了三次请求"
    assert gateway.last_stats.attempts == 3, "尝试次数必须如实数，不能只报最后一次"
    assert gateway.last_stats.to_dict()["attempts"] == 3


def test_structured_fallback_and_repair_are_counted_as_one_call():
    """回退 + 修复重试 = 这一次逻辑调用实际打了 **3** 次，attempts 记 3。"""

    # 替身的 ``content_sequence`` 按「已收到的第几个请求」取值，
    # 而**第一次请求被 400 拒掉了、没走到取正文那一步** ——
    # 所以正文序列要给两份散文：回退那一次拿到散文 → 解析失败 → 修复重试拿到 JSON。
    stub = ModelStub(
        reject_response_format=True,
        content_sequence=[
            "这一轮模型回的是一段散文，不是 JSON。",
            "这一轮模型回的是一段散文，不是 JSON。",
            _reply(GOOD_PAYLOAD),
        ],
    )
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        result = gateway.complete_structured(
            system="你是归并器",
            user=GOLD_TEXT,
            json_schema=MERGE_JSON_SCHEMA,
        )
    finally:
        stub.stop()

    assert len(stub.requests) == 3, "第一次（带参数被拒）+ 回退 + 修复重试 = 3 次"
    assert result.payload["presence"] is True
    assert result.meta.response_format_fallback is True
    assert result.meta.repair_used is True
    assert gateway.last_stats.attempts == 3, "审计看到的必须是这次逻辑调用的真实请求数"


def test_single_request_call_reports_one_attempt():
    """一次就成功 → attempts 就是 1（不多报）。"""

    stub = ModelStub()
    stub.start()
    try:
        gateway = _gateway(stub.base_url)
        gateway.complete(system="s", user="u")
    finally:
        stub.stop()

    assert len(stub.requests) == 1
    assert gateway.last_stats.attempts == 1
